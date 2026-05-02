"""Issue #175 (S3): regression coverage for ``reconcile_qemu_node_links``
plus the boot-NIC detach failure contract and the start-time orphan-TAP
cleanup branch.

The reconciler tests construct a synthetic in-process runtime record
(no real QEMU process) and stub :mod:`host_net` so the test runs without
the privileged helper. They exercise the PUBLIC outer methods
(``reconcile_qemu_node_links``, ``detach_qemu_interface``) so the
node-scoped + per-iface mutex acquisition is included in coverage.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import host_net
from app.services.node_runtime_service import NodeRuntimeService
from tests.conftest import _stub_host_net_for_qemu_start


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runtime_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


@pytest.fixture()
def runtime_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    labs_dir.mkdir()
    images_dir.mkdir()
    tmp_dir.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        GUACAMOLE_DATABASE_URL="",
        GUACAMOLE_DATA_SOURCE="postgresql",
        GUACAMOLE_INTERNAL_URL="http://127.0.0.1:8081/html5/",
        GUACAMOLE_JSON_SECRET_KEY="4c0b569e4c96df157eee1b65dd0e4d41",
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
    )


@pytest.fixture()
def patched_settings(monkeypatch, runtime_settings):
    monkeypatch.setattr(
        "app.services.lab_service.get_settings", lambda: runtime_settings
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.get_settings", lambda: runtime_settings
    )
    return runtime_settings


def _seed_running_qemu_runtime(
    service: NodeRuntimeService,
    *,
    lab_id: str,
    node_id: int,
    boot_ethernet: int = 2,
    interface_attachments: list[dict] | None = None,
    interface_runtime: dict[str, dict] | None = None,
    qmp_socket: str = "/tmp/test-qmp.sock",
) -> dict:
    """Insert a 'running' QEMU runtime record into the in-process registry.

    Mirrors the shape produced by :meth:`_start_qemu_node` but skips the
    real spawn — sufficient for reconciler / detach unit tests because
    the target methods read only the registry + lab.json input.
    """
    runtime = {
        "lab_id": lab_id,
        "node_id": int(node_id),
        "kind": "qemu",
        "name": f"node-{node_id}",
        "console": "telnet",
        "console_port": 12345,
        "pid": 4321,
        "pid_create_time": 1.0,
        "work_dir": f"/tmp/lab-{lab_id}-node-{node_id}",
        "stdout_log": f"/tmp/lab-{lab_id}-node-{node_id}/stdout.log",
        "stderr_log": f"/tmp/lab-{lab_id}-node-{node_id}/stderr.log",
        "qmp_socket": qmp_socket,
        "command": [],
        "started_at": 1.0,
        "boot_ethernet": int(boot_ethernet),
        "interface_attachments": list(interface_attachments or []),
        "interface_runtime": dict(interface_runtime or {}),
        "tap_names": [],
        "allocated_slots": [],
    }
    service._registry[service._key(lab_id, int(node_id))] = runtime
    # Make ``_runtime_record`` treat the synthetic record as alive so the
    # public reconcile/detach paths don't reap it.
    service._is_runtime_alive = lambda _r: True  # type: ignore[assignment]
    return runtime


def _make_lab_data(
    *, lab_id: str, node_id, network_id: int, attach_generation: int = 1
) -> dict:
    """Minimal lab.json shape with a single (node, iface 0) -> network link."""
    return {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": "reconcile-tests"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            str(node_id): {
                "id": int(node_id) if not isinstance(node_id, str) else int(node_id),
                "name": "vm-1",
                "type": "qemu",
                "ethernet": 2,
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None},
                    {"index": 1, "name": "eth1", "planned_mac": None},
                ],
            }
        },
        "networks": {
            str(network_id): {
                "id": network_id,
                "name": "lab-link",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
            }
        },
        "links": [
            {
                "id": "lnk-1",
                "from": {"node_id": node_id, "interface_index": 0},
                "to": {"network_id": network_id},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {
                    "delay_ms": 0,
                    "loss_pct": 0,
                    "bandwidth_kbps": 0,
                    "jitter_ms": 0,
                },
                "runtime": {"attach_generation": int(attach_generation)},
            }
        ],
        "defaults": {"link_style": "orthogonal"},
    }


def _stub_reconciler_host_net(monkeypatch, *, tap_exists: bool = True):
    """Stub the ``host_net`` surface used by the reconciler.

    The reconciler reads ``bridge_name`` / ``tap_name`` (deterministic
    name derivation) and writes via ``link_master`` / ``link_up`` /
    ``link_set_nomaster``. We capture writes so tests can assert call
    sequences.
    """
    calls: dict[str, list] = {
        "tap_exists": [],
        "tap_add": [],
        "link_master": [],
        "link_up": [],
        "link_set_nomaster": [],
    }

    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.bridge_name",
        lambda lab_id, network_id: f"nve-test-bridge-{network_id}",
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_name",
        lambda lab_id, node_id, iface: f"nve-test-d{node_id}i{iface}",
    )

    def fake_tap_exists(name: str) -> bool:
        calls["tap_exists"].append(name)
        return tap_exists

    def fake_tap_add(name: str) -> None:
        calls["tap_add"].append(name)

    def fake_link_master(iface: str, bridge: str) -> None:
        calls["link_master"].append((iface, bridge))

    def fake_link_up(iface: str) -> None:
        calls["link_up"].append(iface)

    def fake_link_set_nomaster(iface: str) -> None:
        calls["link_set_nomaster"].append(iface)

    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_exists", fake_tap_exists
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_add", fake_tap_add
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.link_master", fake_link_master
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.link_up", fake_link_up
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.link_set_nomaster",
        fake_link_set_nomaster,
    )
    return calls


def _capture_qmp(service: NodeRuntimeService) -> list[dict]:
    """Replace ``_qmp_command`` with a recorder so reconciler / detach
    tests can assert ``set_link`` invocations without needing a live
    QMP socket. Returns the captured-calls list.
    """
    captured: list[dict] = []

    def fake_qmp_command(socket_path, command, arguments=None):
        captured.append(
            {"socket": socket_path, "command": command, "arguments": arguments}
        )
        return {"return": {}}

    service._qmp_command = fake_qmp_command  # type: ignore[assignment]
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconciler_phase1_attaches_missing_iface(monkeypatch, patched_settings):
    """Phase 1: a runtime record with no attachments + a lab.json link
    drives ``link_master`` + ``link_up`` + QMP ``set_link up=true`` and
    appends a fresh ``interface_attachments`` entry.
    """
    lab_id = "lab-recon-A"
    node_id = 1
    service = NodeRuntimeService()
    runtime = _seed_running_qemu_runtime(
        service, lab_id=lab_id, node_id=node_id, interface_attachments=[]
    )
    lab_data = _make_lab_data(
        lab_id=lab_id, node_id=node_id, network_id=1, attach_generation=1
    )

    host_calls = _stub_reconciler_host_net(monkeypatch, tap_exists=True)
    qmp_calls = _capture_qmp(service)

    result = service.reconcile_qemu_node_links(
        lab_id, lab_data, lab_data["nodes"]["1"]
    )

    expected_tap = f"nve-test-d{node_id}i0"
    expected_bridge = "nve-test-bridge-1"

    assert result["node_id"] == node_id
    assert len(result["applied"]) == 1
    assert result["applied"][0]["interface_index"] == 0
    assert result["applied"][0]["tap"] == expected_tap
    assert result["applied"][0]["bridge"] == expected_bridge
    assert result["removed"] == []

    # Runtime now has the attachment record with boot_nic=True.
    attachments = runtime["interface_attachments"]
    assert len(attachments) == 1
    assert attachments[0]["interface_index"] == 0
    assert attachments[0]["network_id"] == 1
    assert attachments[0]["boot_nic"] is True
    assert attachments[0]["tap_name"] == expected_tap
    assert attachments[0]["bridge_name"] == expected_bridge

    # host_net was driven on the (tap, bridge) pair.
    assert (expected_tap, expected_bridge) in host_calls["link_master"]
    assert expected_tap in host_calls["link_up"]
    # tap_add MUST NOT be called when tap_exists=True (idempotency).
    assert host_calls["tap_add"] == []

    # QMP ``set_link`` issued for net0 with up=True.
    set_link_calls = [c for c in qmp_calls if c["command"] == "set_link"]
    assert any(
        c["arguments"] == {"name": "net0", "up": True} for c in set_link_calls
    ), f"expected set_link(net0, up=True) in {set_link_calls}"


def test_reconciler_phase2_tears_down_stale_attachment(
    monkeypatch, patched_settings
):
    """Phase 2: a stale boot-NIC attachment with no matching link in
    ``lab.json`` is torn down via ``link_set_nomaster`` + QMP ``set_link
    up=false``, and the entry is dropped from ``interface_attachments``.
    """
    lab_id = "lab-recon-B"
    node_id = 2
    stale_tap = f"nve-test-d{node_id}i0"
    service = NodeRuntimeService()
    runtime = _seed_running_qemu_runtime(
        service,
        lab_id=lab_id,
        node_id=node_id,
        interface_attachments=[
            {
                "interface_index": 0,
                "network_id": 1,
                "bridge_name": "nve-test-bridge-1",
                "tap_name": stale_tap,
                "slot": None,
                "boot_nic": True,
                "nic_model": "e1000",
                "attach_generation": 1,
                "planned_mac": "",
            }
        ],
    )
    # lab.json has NO links — the attachment is stale (link was deleted
    # while the node was running and the stale-gen guard skipped detach).
    lab_data = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": "phase2"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "2": {
                "id": node_id,
                "name": "vm-2",
                "type": "qemu",
                "ethernet": 2,
                "interfaces": [],
            }
        },
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }

    host_calls = _stub_reconciler_host_net(monkeypatch, tap_exists=True)
    qmp_calls = _capture_qmp(service)

    result = service.reconcile_qemu_node_links(
        lab_id, lab_data, lab_data["nodes"]["2"]
    )

    assert result["applied"] == []
    assert len(result["removed"]) == 1
    assert result["removed"][0]["interface_index"] == 0
    assert result["removed"][0]["tap"] == stale_tap

    # Attachment is gone from runtime.
    assert runtime["interface_attachments"] == []

    # link_set_nomaster called with the stale TAP.
    assert stale_tap in host_calls["link_set_nomaster"]

    # QMP set_link with up=False issued for net0.
    set_link_calls = [c for c in qmp_calls if c["command"] == "set_link"]
    assert any(
        c["arguments"] == {"name": "net0", "up": False} for c in set_link_calls
    ), f"expected set_link(net0, up=False) in {set_link_calls}"


def test_reconciler_handles_string_node_id_in_labjson(
    monkeypatch, patched_settings
):
    """Issue #175 (C2): lab.json may carry ``from.node_id`` as a string
    (manual edit / migration / 3rd-party tooling). The reconciler MUST
    accept the string and treat the link as expected; otherwise Phase 2
    tears down all matching boot-NIC attachments.
    """
    lab_id = "lab-recon-C"
    node_id = 3
    expected_tap = f"nve-test-d{node_id}i0"
    service = NodeRuntimeService()
    # Pre-existing attachment record (e.g. from a previous reconcile pass).
    runtime = _seed_running_qemu_runtime(
        service,
        lab_id=lab_id,
        node_id=node_id,
        interface_attachments=[
            {
                "interface_index": 0,
                "network_id": 1,
                "bridge_name": "nve-test-bridge-1",
                "tap_name": expected_tap,
                "slot": None,
                "boot_nic": True,
                "nic_model": "e1000",
                "attach_generation": 1,
                "planned_mac": "",
            }
        ],
    )
    # lab.json with from.node_id as STRING "3".
    lab_data = _make_lab_data(
        lab_id=lab_id,
        node_id="3",
        network_id=1,
        attach_generation=1,
    )

    _stub_reconciler_host_net(monkeypatch, tap_exists=True)
    _capture_qmp(service)

    result = service.reconcile_qemu_node_links(
        lab_id, lab_data, {"id": node_id, "type": "qemu"}
    )

    # Before issue #175 (C2) the int(...) cast was missing: ``applied``
    # would be empty AND ``removed`` would tear down the boot-NIC. With
    # the fix, the link is recognised and the attachment is refreshed.
    assert len(result["applied"]) == 1, (
        f"reconciler must accept str node_id in lab.json; got {result}"
    )
    assert result["applied"][0]["interface_index"] == 0
    assert result["removed"] == [], (
        f"removed must be empty (link is expected, not stale); got {result}"
    )
    # Attachment record retained / refreshed (not torn down).
    assert len(runtime["interface_attachments"]) == 1
    assert runtime["interface_attachments"][0]["interface_index"] == 0


def test_reconciler_phase1_force_syncs_generation(monkeypatch, patched_settings):
    """Issue #175 (M1): Phase 1 must FORCE-SYNC
    ``current_attach_generation`` to the lab.json link's
    ``attach_generation`` — NOT bump it. Bumping past the lab.json value
    breaks the next user-driven ``delete_link`` stale-gen guard.
    """
    lab_id = "lab-recon-D"
    node_id = 4
    service = NodeRuntimeService()
    # Stale runtime gen=3, no prior attachment record. lab.json gen=7.
    runtime = _seed_running_qemu_runtime(
        service,
        lab_id=lab_id,
        node_id=node_id,
        interface_attachments=[],
        interface_runtime={"0": {"current_attach_generation": 3}},
    )
    lab_data = _make_lab_data(
        lab_id=lab_id, node_id=node_id, network_id=1, attach_generation=7
    )

    _stub_reconciler_host_net(monkeypatch, tap_exists=True)
    _capture_qmp(service)

    result = service.reconcile_qemu_node_links(
        lab_id, lab_data, lab_data["nodes"]["4"]
    )

    assert len(result["applied"]) == 1

    # Force-sync semantics: runtime gen == lab.json gen, exactly.
    assert (
        runtime["interface_runtime"]["0"]["current_attach_generation"] == 7
    ), (
        "expected force-sync to 7 (lab.json gen); got "
        f"{runtime['interface_runtime']['0']['current_attach_generation']}"
    )

    # The newly-written attachment carries the same gen.
    attachments = runtime["interface_attachments"]
    assert len(attachments) == 1
    assert attachments[0]["attach_generation"] == 7


def test_detach_boot_nic_raises_on_link_set_nomaster_failure(
    monkeypatch, patched_settings
):
    """Issue #175 (M3): boot-NIC detach must RAISE on
    ``link_set_nomaster`` failure. Swallowing leaves the TAP on the
    bridge (packets still flow) while the system thinks detach
    succeeded — generation bumped, attachment dropped, lab.json link
    removed by ``delete_link``, no way to retry.

    On raise, ``interface_attachments`` and
    ``current_attach_generation`` MUST be unchanged.
    """
    lab_id = "lab-recon-E"
    node_id = 5
    boot_tap = f"nve-test-d{node_id}i0"
    service = NodeRuntimeService()
    runtime = _seed_running_qemu_runtime(
        service,
        lab_id=lab_id,
        node_id=node_id,
        interface_attachments=[
            {
                "interface_index": 0,
                "network_id": 1,
                "bridge_name": "nve-test-bridge-1",
                "tap_name": boot_tap,
                "slot": None,
                "boot_nic": True,
                "nic_model": "e1000",
                "attach_generation": 4,
                "planned_mac": "",
            }
        ],
        interface_runtime={"0": {"current_attach_generation": 4}},
    )

    # Stub QMP set_link (best-effort path — must succeed so we reach the
    # link_set_nomaster call below).
    _capture_qmp(service)

    # Stub ``host_net.tap_name`` to match the seeded TAP name (the detach
    # path falls back to ``host_net.tap_name(...)`` if ``target.tap_name``
    # is missing, but we set it on the attachment so this is purely
    # defensive — keep it for parity with the start-path stubs).
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_name",
        lambda lab_id, node_id, iface: f"nve-test-d{node_id}i{iface}",
    )

    # The failure injection: ``link_set_nomaster`` raises HostNetError.
    nomaster_calls: list[str] = []

    def failing_link_set_nomaster(iface: str) -> None:
        nomaster_calls.append(iface)
        raise host_net.HostNetError("simulated bridge detach failure")

    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.link_set_nomaster",
        failing_link_set_nomaster,
    )

    # Detach must propagate the HostNetError.
    with pytest.raises(host_net.HostNetError, match="simulated"):
        service.detach_qemu_interface(lab_id, node_id, 0)

    # link_set_nomaster was attempted on the boot TAP exactly once.
    assert nomaster_calls == [boot_tap]

    # Runtime is UNCHANGED: attachment retained, generation NOT bumped.
    assert len(runtime["interface_attachments"]) == 1
    assert runtime["interface_attachments"][0]["interface_index"] == 0
    assert (
        runtime["interface_runtime"]["0"]["current_attach_generation"] == 4
    ), (
        "current_attach_generation must NOT be bumped when detach raises; "
        f"got {runtime['interface_runtime']['0']['current_attach_generation']}"
    )


def test_start_qemu_orphan_tap_cleanup(
    monkeypatch, patched_settings, tmp_path
):
    """Commit 7f8f8bf: a pre-existing TAP with a stale bridge master from
    a prior crashed start MUST be detached via ``link_set_nomaster`` in
    the boot loop's ``else`` branch. The branch fires when:

      1. ``host_net.tap_exists`` is True (TAP pre-existed; ``tap_add`` is
         skipped).
      2. The interface index is NOT in ``attachment_by_index`` (no link
         declared in lab.json for this iface), so the loop falls into the
         orphan-cleanup ``else`` branch.

    Symptom of the bug: a re-start after a crash left orphan boot TAPs
    on whatever bridge they were last attached to, so packets continued
    to flow on a now-disconnected lab link.
    """
    lab_id = "lab-recon-F"
    node_id = 6
    expected_orphan_tap = f"nve-test-d{node_id}i0"

    service = NodeRuntimeService()

    # ---- Lab data: NO link for iface 0 (so it's an orphan candidate). --
    # The node has ethernet=1 (just one boot iface so the test is tightly
    # scoped to the orphan branch).
    lab_data = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": "orphan-test"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            str(node_id): {
                "id": node_id,
                "name": "vm-orphan",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "firstmac": "50:00:00:01:00:00",
                "interfaces": [
                    {
                        "index": 0,
                        "name": "eth0",
                        "planned_mac": None,
                        "port_position": None,
                    }
                ],
            }
        },
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }
    (patched_settings.LABS_DIR / "orphan.json").write_text(json.dumps(lab_data))
    image_dir = patched_settings.IMAGES_DIR / "qemu" / "router-image"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "hda.qcow2").write_text("base-image")

    # ---- Track host_net calls. Start from the canonical stub, then
    # override ``tap_exists`` to return True (orphan scenario) and
    # ``link_set_nomaster`` so we can assert it was invoked. ---------
    _stub_host_net_for_qemu_start(monkeypatch)

    # Counters.
    tap_add_calls: list[str] = []
    link_set_nomaster_calls: list[str] = []

    def fake_tap_exists(name: str) -> bool:
        return True  # orphan — pre-existing TAP from a crashed prior start

    def fake_tap_add(name: str) -> None:
        tap_add_calls.append(name)

    def fake_link_set_nomaster(iface: str) -> None:
        # Production code wraps this in ``except host_net.HostNetError:
        # pass`` for the orphan-cleanup branch. We DO NOT raise here so
        # the call is observed; the swallow path is the OPPOSITE branch.
        link_set_nomaster_calls.append(iface)

    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_exists", fake_tap_exists
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_add", fake_tap_add
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.link_set_nomaster",
        fake_link_set_nomaster,
    )

    # ---- Stub QEMU subprocess + binaries + clock so spawn is inert. ---
    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService._resolve_binary",
        staticmethod(lambda binary: binary),
    )

    class _FakeProcess:
        def __init__(self, pid: int = 4242):
            self.pid = pid

        def poll(self):
            return None

    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        # qemu-img create writes the overlay file; mimic that.
        if cmd and Path(cmd[0]).name == "qemu-img":
            Path(cmd[-1]).write_text("overlay-image")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_popen(
        cmd, cwd=None, stdin=None, stdout=None, stderr=None, start_new_session=None
    ):
        return _FakeProcess()

    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(
            create_time=lambda: 222.0,
            cpu_percent=lambda interval=0.0: 0,
            memory_info=lambda: SimpleNamespace(rss=0),
            wait=lambda timeout=5: None,
            is_running=lambda: True,
            status=lambda: "sleeping",
        ),
    )

    # Seed an instance_id so ``host_net.bridge_name`` / ``tap_name``
    # don't blow up.
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance_id").write_text("test-instance-S3")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))

    # ---- Run the boot path. ---------------------------------------------
    runtime = service._start_qemu_node(lab_data, lab_data["nodes"][str(node_id)])

    # The orphan branch fired: link_set_nomaster called for iface 0's TAP.
    assert expected_orphan_tap in link_set_nomaster_calls, (
        f"expected link_set_nomaster({expected_orphan_tap!r}) in "
        f"{link_set_nomaster_calls}"
    )

    # tap_add MUST NOT be called for an iface whose TAP already exists.
    assert expected_orphan_tap not in tap_add_calls, (
        f"tap_add must be skipped when tap_exists=True; got {tap_add_calls}"
    )

    # Sanity: the runtime record was created (the start path completed
    # past the boot loop).
    assert runtime["lab_id"] == lab_id
    assert runtime["node_id"] == node_id
