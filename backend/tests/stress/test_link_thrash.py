# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""US-405 — Stress test: link thrash invariants.

100-iteration tight loop creating then deleting a link to a running container,
then a running QEMU VM. End-of-loop assertions cover ALL of:

    - ``links[]`` is empty.
    - ``bridge link show master {bridge}`` lists no ``nve…d…i…`` entries
      for the test node.
    - ``ip link show`` lists no veth/TAP for the test node.
    - QMP ``query-pci`` (via ``scripts/qmp_query.py``) shows no ``dev{iface}``
      for the test node.
    - No leaked TAP/veth across the entire iteration loop.
    - ``network.runtime.used_ips`` is back to its initial baseline (typically
      ``[]``) — IPAM leak detection per Critic iter-2 gap-analysis.

The test is gated behind the ``RUN_STRESS_TESTS=1`` env var. PR-CI runners
do NOT have privileged Docker or KVM, so this test will skip cleanly there.

Run on a privileged Linux host with KVM::

    RUN_STRESS_TESTS=1 python -m pytest backend/tests/stress/test_link_thrash.py -v

See ``backend/tests/stress/README.md`` for full operating instructions.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Skip the entire module unless explicitly opted-in. ``pytestmark`` is
# evaluated at collection time, so the test will be reported as SKIPPED
# (with a clear reason string) on every PR-CI run rather than failing
# with a "docker not found" error.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_STRESS_TESTS", "0") != "1",
    reason=(
        "Set RUN_STRESS_TESTS=1 to opt in. Requires privileged Linux host "
        "with Docker (privileged mode), KVM, ip(8), and bridge(8). "
        "See backend/tests/stress/README.md."
    ),
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ITERATIONS = 100
ITERATION_DELAY_S = 0.05  # 50ms

# Real images required by US-405 acceptance criteria.
DOCKER_BUSYBOX_IMAGE = os.environ.get("STRESS_BUSYBOX_IMAGE", "busybox")
QEMU_VYOS_IMAGE = os.environ.get("STRESS_VYOS_IMAGE", "vyos-1.4")

# Test lab + node identifiers.
TEST_LAB_NAME = "stress_link_thrash"
CONTAINER_NODE_ID = 1
QEMU_NODE_ID = 2
TEST_NETWORK_ID = 1
NODE_IFACE_INDEX = 0


# ---------------------------------------------------------------------------
# Privileged-host capability probes (used by sub-skips)
# ---------------------------------------------------------------------------


def _has_kvm() -> bool:
    """Return True iff /dev/kvm exists and KVM_AVAILABLE != "0"."""
    if os.environ.get("KVM_AVAILABLE", "1") == "0":
        return False
    return Path("/dev/kvm").exists()


def _has_privileged_docker() -> bool:
    """Return True iff ``docker info`` succeeds (privileged daemon reachable)."""
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _has_ip_tools() -> bool:
    """Return True iff ``ip`` and ``bridge`` binaries are on PATH."""
    return shutil.which("ip") is not None and shutil.which("bridge") is not None


# ---------------------------------------------------------------------------
# Kernel-state scans (used for end-of-loop invariants + leak detection)
# ---------------------------------------------------------------------------


def _list_node_ifaces(host_prefix: str) -> list[str]:
    """Return all host-side ifaces whose name starts with ``host_prefix``.

    ``host_prefix`` matches the per-instance ``nve<hash>d<node_id>i`` prefix
    that ``host_net.veth_host_name`` and ``host_net.tap_name`` produce.
    """
    proc = subprocess.run(
        ["ip", "-o", "link", "show"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        # Format: "<index>: <name>[@<peer>]: <flags> ..."
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        raw = parts[1].strip().split("@")[0]
        if raw.startswith(host_prefix):
            out.append(raw)
    return out


def _bridge_members_for_node(bridge: str, host_prefix: str) -> list[str]:
    """Return members of ``bridge`` whose name starts with ``host_prefix``.

    Uses ``bridge link show master <bridge>`` (unprivileged read).
    """
    proc = subprocess.run(
        ["bridge", "link", "show", "master", bridge],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return []
    members: list[str] = []
    for line in proc.stdout.splitlines():
        # Format: "<index>: <iface>@<peer>: <flags> master <bridge> ..."
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        raw = parts[1].strip().split("@")[0]
        if raw.startswith(host_prefix):
            members.append(raw)
    return members


def _qmp_query_pci(socket_path: str) -> dict[str, Any]:
    """Invoke ``scripts/qmp_query.py <sock> query-pci`` and return the parsed JSON."""
    repo_root = Path(__file__).resolve().parents[3]
    qmp_script = repo_root / "scripts" / "qmp_query.py"
    proc = subprocess.run(
        [sys.executable, str(qmp_script), socket_path, "query-pci"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"qmp_query.py failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _qmp_pci_has_dev_for_iface(socket_path: str, iface_index: int) -> bool:
    """Return True iff QMP ``query-pci`` reports a ``dev{iface_index}`` device."""
    payload = _qmp_query_pci(socket_path)
    target = f"dev{iface_index}"
    # Walk the (potentially nested) bus structure flat-style and look for any
    # ``qdev_id == target`` entry. QEMU 6+ returns ``devices`` lists that may
    # nest under ``pci_bridge`` ports.
    stack: list[Any] = list(payload.get("return") or [])
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("qdev_id") == target:
                return True
            for value in item.values():
                if isinstance(value, (list, dict)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return False


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def stress_settings(tmp_path):
    """Build a minimal Settings stub matching the production routes."""
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    for d in (labs_dir, images_dir, tmp_dir, templates_dir):
        d.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        TEMPLATES_DIR=templates_dir,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
        GUACAMOLE_DATABASE_URL="",
        GUACAMOLE_DATA_SOURCE="postgresql",
        GUACAMOLE_INTERNAL_URL="http://127.0.0.1:8081/html5/",
        GUACAMOLE_JSON_SECRET_KEY="x" * 32,
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
    )


@pytest.fixture()
def patched_stress_settings(monkeypatch, stress_settings):
    monkeypatch.setattr(
        "app.services.lab_service.get_settings", lambda: stress_settings
    )
    monkeypatch.setattr(
        "app.services.template_service.get_settings", lambda: stress_settings
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.get_settings", lambda: stress_settings
    )
    monkeypatch.setattr(
        "app.services.html5_service.get_settings", lambda: stress_settings
    )
    monkeypatch.setattr(
        "app.services.guacamole_db_service.get_settings", lambda: stress_settings
    )
    monkeypatch.setattr(
        "app.services.link_service.get_settings", lambda: stress_settings
    )
    monkeypatch.setattr(
        "app.services.network_service.get_settings", lambda: stress_settings
    )
    monkeypatch.setattr(
        "app.routers.labs.get_settings", lambda: stress_settings
    )
    return stress_settings


@pytest.fixture()
def auth_override():
    """Authorise a synthetic admin user for the duration of the test."""
    from app.dependencies import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="admin", role="admin", html5=True, folder="/",
    )
    yield
    app.dependency_overrides.clear()


def _build_stress_lab() -> dict:
    """Return a v2 lab payload with one container node + one QEMU node + one explicit network."""
    return {
        "schema": 2,
        "id": TEST_LAB_NAME,
        "meta": {"name": TEST_LAB_NAME},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            str(CONTAINER_NODE_ID): {
                "id": CONTAINER_NODE_ID,
                "name": "ctr1",
                "type": "docker",
                "template": "docker",
                "image": DOCKER_BUSYBOX_IMAGE,
                "console": "telnet",
                "status": 0,
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "left": 100,
                "top": 100,
                "icon": "Server.png",
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                ],
            },
            str(QEMU_NODE_ID): {
                "id": QEMU_NODE_ID,
                "name": "vm1",
                "type": "qemu",
                "template": "vyos",
                "image": QEMU_VYOS_IMAGE,
                "console": "telnet",
                "status": 0,
                "ethernet": 1,
                "left": 300,
                "top": 100,
                "icon": "Router.png",
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                ],
            },
        },
        "networks": {
            str(TEST_NETWORK_ID): {
                "id": TEST_NETWORK_ID,
                "name": "stress-net",
                "type": "linux_bridge",
                "left": 200,
                "top": 200,
                "icon": "01-Cloud-Default.svg",
                "width": 0,
                "style": "Solid",
                "linkstyle": "Straight",
                "color": "",
                "label": "",
                "visibility": True,
                "implicit": False,
                "smart": -1,
                "config": {},
                # Initial IPAM baseline — empty until the link service allocates.
                "runtime": {"used_ips": []},
            },
        },
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }


# ---------------------------------------------------------------------------
# Thrash loop (one node at a time) + invariant assertions
# ---------------------------------------------------------------------------


async def _thrash_one_node(
    *,
    client,
    lab_name: str,
    node_id: int,
    iface_index: int,
    iterations: int,
    delay_s: float,
) -> None:
    """Run ``iterations`` of (POST link → sleep → DELETE link → sleep) for one node.

    Raises on the first non-2xx response so the caller's ``finally`` block
    still tears the lab down.
    """
    for i in range(iterations):
        post_resp = await client.post(
            f"/api/labs/{lab_name}/links",
            json={
                "from": {"node_id": node_id, "interface_index": iface_index},
                "to": {"network_id": TEST_NETWORK_ID},
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert post_resp.status_code in (200, 201), (
            f"iteration {i} POST failed (rc={post_resp.status_code}): "
            f"{post_resp.text}"
        )
        link_id = post_resp.json()["link"]["id"]
        await asyncio.sleep(delay_s)

        del_resp = await client.delete(f"/api/labs/{lab_name}/links/{link_id}")
        assert del_resp.status_code == 200, (
            f"iteration {i} DELETE failed (rc={del_resp.status_code}): "
            f"{del_resp.text}"
        )
        await asyncio.sleep(delay_s)


def _assert_invariants(
    *,
    lab_path: Path,
    bridge_name_value: str,
    container_host_prefix: str,
    qemu_host_prefix: str,
    qmp_socket_path: str | None,
    initial_used_ips_baseline: list[str],
) -> None:
    """All-of assertion bundle described in US-405 acceptance criteria."""
    saved = json.loads(lab_path.read_text())

    # 1. links[] empty.
    assert saved.get("links") == [], (
        f"links[] not empty after thrash: {saved.get('links')!r}"
    )

    # 2. bridge link show master <bridge> reports no nve…d…i… entries
    #    for either node.
    container_bridge_members = _bridge_members_for_node(
        bridge_name_value, container_host_prefix
    )
    qemu_bridge_members = _bridge_members_for_node(
        bridge_name_value, qemu_host_prefix
    )
    assert container_bridge_members == [], (
        f"container ifaces still attached to bridge {bridge_name_value!r}: "
        f"{container_bridge_members!r}"
    )
    assert qemu_bridge_members == [], (
        f"QEMU ifaces still attached to bridge {bridge_name_value!r}: "
        f"{qemu_bridge_members!r}"
    )

    # 3. ip link show reports no veth/TAP for either test node.
    container_host_ifaces = _list_node_ifaces(container_host_prefix)
    qemu_host_ifaces = _list_node_ifaces(qemu_host_prefix)
    assert container_host_ifaces == [], (
        f"container veths leaked: {container_host_ifaces!r}"
    )
    assert qemu_host_ifaces == [], (
        f"QEMU TAPs leaked: {qemu_host_ifaces!r}"
    )

    # 4. QMP query-pci on the running QEMU VM reports no dev{iface}.
    if qmp_socket_path is not None:
        assert not _qmp_pci_has_dev_for_iface(
            qmp_socket_path, NODE_IFACE_INDEX
        ), f"QMP query-pci still reports dev{NODE_IFACE_INDEX} on {qmp_socket_path}"

    # 5. (covered above by 2+3) — both bridge and ip-link scans are empty,
    #    proving no per-iteration leakage accumulated.

    # 6. IPAM leak detection: network.runtime.used_ips back to baseline.
    network_record = (saved.get("networks") or {}).get(str(TEST_NETWORK_ID), {})
    runtime_record = network_record.get("runtime") or {}
    used_ips = list(runtime_record.get("used_ips") or [])
    assert used_ips == list(initial_used_ips_baseline), (
        f"IPAM leak detected: used_ips={used_ips!r}, "
        f"baseline={initial_used_ips_baseline!r}"
    )


# ---------------------------------------------------------------------------
# Real container + VM lifecycle helpers
# ---------------------------------------------------------------------------


def _docker_run_busybox() -> str:
    """Spawn a real busybox container with --network=none and return its ID."""
    proc = subprocess.run(
        [
            "docker", "run", "--network=none", "--rm", "-d",
            DOCKER_BUSYBOX_IMAGE, "sleep", "600",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker run busybox failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _docker_kill(container_id: str) -> None:
    """Best-effort kill+remove of a stress container."""
    if not container_id:
        return
    subprocess.run(
        ["docker", "kill", container_id],
        capture_output=True, text=True, timeout=15, check=False,
    )


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_thrash_invariants(
    patched_stress_settings, auth_override, tmp_path, monkeypatch,
):
    """100-iteration link thrash for a running container + a running QEMU VM.

    See module docstring for the full invariant list. The test wraps the loop
    in ``try/finally`` so cleanup runs even if a single iteration fails.
    """
    # -----------------------------------------------------------------
    # Sub-skips: this test makes RUN_STRESS_TESTS=1 the entry gate, but
    # individual capability requirements (KVM, privileged Docker, ip(8))
    # may still be missing on a runner that only has *some* of them. We
    # skip with a precise reason in that case rather than failing.
    # -----------------------------------------------------------------
    if not _has_privileged_docker():
        pytest.skip(
            "Privileged Docker not available (need `docker info` to succeed). "
            "Run on a host with Docker daemon access."
        )
    if not _has_ip_tools():
        pytest.skip(
            "Linux ip(8)/bridge(8) tools not available (need iproute2). "
            "Run on a Linux host."
        )
    if not _has_kvm():
        pytest.skip(
            "KVM not available (/dev/kvm missing or KVM_AVAILABLE=0). "
            "Run on a KVM-capable Linux host."
        )

    # ``host_net`` requires an instance ID. Provision a per-test override so
    # the bridge / TAP / veth name helpers do not depend on
    # /etc/nova-ve/instance_id existing on the runner.
    instance_id_file = tmp_path / "instance_id"
    instance_id_file.write_text("stress-test-instance-id\n")
    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_FILE", str(instance_id_file))

    # Persist a per-test runtime registry so docker container PIDs registered
    # by NodeRuntimeService do not escape into /var/lib/nova-ve.
    pids_file = tmp_path / "pids.json"
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(pids_file))
    monkeypatch.setenv("NOVA_VE_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(tmp_path / "bridges"))

    # Build the test lab on disk.
    lab_payload = _build_stress_lab()
    lab_filename = f"{TEST_LAB_NAME}.json"
    lab_path = patched_stress_settings.LABS_DIR / lab_filename
    lab_path.write_text(json.dumps(lab_payload))

    # Resolve per-instance kernel-object names used by the invariant scans.
    from app.services import host_net  # local import — depends on env above.

    bridge_name_value = host_net.bridge_name(TEST_LAB_NAME, TEST_NETWORK_ID)
    instance_id = host_net.get_instance_id()
    lab_hash = host_net._lab_hash(TEST_LAB_NAME, instance_id)
    container_host_prefix = f"nve{lab_hash:04x}d{CONTAINER_NODE_ID}i"
    qemu_host_prefix = f"nve{lab_hash:04x}d{QEMU_NODE_ID}i"

    # Baseline used_ips — SHOULD be the empty list seeded above. We snapshot
    # rather than hard-coding so the test is robust to a future seed-time
    # IPAM reservation (gateway, etc.).
    initial_used_ips_baseline = list(
        ((lab_payload.get("networks") or {})
         .get(str(TEST_NETWORK_ID), {})
         .get("runtime") or {})
        .get("used_ips") or []
    )

    # Lazy imports to keep the module import-time fast in the skipped path.
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.services.node_runtime_service import NodeRuntimeService

    runtime_service = NodeRuntimeService()
    container_id: str | None = None
    qmp_socket_path: str | None = None

    try:
        # ---------------------------------------------------------------
        # Spin up the real container (US-203 architecture: --network=none).
        # ---------------------------------------------------------------
        container_id = _docker_run_busybox()
        # Surface the container PID via NodeRuntimeService's start path so
        # link_service's hot-attach finds a valid runtime record. The same
        # service is exercised in production via GET /labs/.../start.
        lab_data = json.loads(lab_path.read_text())
        runtime_service.start_node(lab_data, CONTAINER_NODE_ID)

        # ---------------------------------------------------------------
        # Spin up the real QEMU VM with the vyos image.
        # ---------------------------------------------------------------
        runtime_service.start_node(lab_data, QEMU_NODE_ID)
        qemu_runtime = runtime_service._runtime_record(TEST_LAB_NAME, QEMU_NODE_ID) or {}
        qmp_socket_path = qemu_runtime.get("qmp_socket")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://stress") as client:
            # ----- Phase 1: 100 iterations against the running container ---
            await _thrash_one_node(
                client=client,
                lab_name=lab_filename,
                node_id=CONTAINER_NODE_ID,
                iface_index=NODE_IFACE_INDEX,
                iterations=ITERATIONS,
                delay_s=ITERATION_DELAY_S,
            )

            # ----- Phase 2: 100 iterations against the running QEMU VM -----
            await _thrash_one_node(
                client=client,
                lab_name=lab_filename,
                node_id=QEMU_NODE_ID,
                iface_index=NODE_IFACE_INDEX,
                iterations=ITERATIONS,
                delay_s=ITERATION_DELAY_S,
            )

    finally:
        # End-of-loop assertions live in the finally so we always observe
        # the kernel state — even if a single iteration raised mid-loop.
        # We surface the original exception (if any) by NOT swallowing here.
        try:
            _assert_invariants(
                lab_path=lab_path,
                bridge_name_value=bridge_name_value,
                container_host_prefix=container_host_prefix,
                qemu_host_prefix=qemu_host_prefix,
                qmp_socket_path=qmp_socket_path,
                initial_used_ips_baseline=initial_used_ips_baseline,
            )
        finally:
            # Tear down the real runtime regardless of assertion outcome.
            try:
                lab_data = json.loads(lab_path.read_text())
                try:
                    runtime_service.stop_node(lab_data, QEMU_NODE_ID)
                except Exception:
                    pass
                try:
                    runtime_service.stop_node(lab_data, CONTAINER_NODE_ID)
                except Exception:
                    pass
            finally:
                _docker_kill(container_id or "")
                # Sweep any host-side leftovers so the next test run starts
                # from a clean kernel state.
                host_net.sweep_lab_host_ifaces(TEST_LAB_NAME)
                host_net.try_bridge_del(bridge_name_value)
