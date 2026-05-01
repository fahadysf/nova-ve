# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-303 — QMP-driven hot-add NIC (with attach lock + slot reservation).

Acceptance criteria exercised here:

  * ``link_service.create_link`` dispatches running QEMU nodes to
    ``_attach_qemu_interface_locked`` (mirrors the docker hot-attach flow).
  * Hot-add executes the 4 QMP/host steps in order:
        query-pci → tap_add → link_master → netdev_add → device_add.
  * The QMP ``id=`` is ``net{interface_index}`` (NOT slot number) so the
    ``_read_qemu_live_mac`` invariant is preserved
    (``node_runtime_service.py:_read_qemu_live_mac`` looks up
    ``f"net{interface_index}"`` in ``query-rx-filter``).
  * ``driver=`` for ``device_add`` comes from ``extras.qemu_nic`` (or
    ``e1000`` default), NOT a hardcoded ``virtio-net-pci``.
  * Slot allocation scans ``rp{max_nics-1}`` downward (US-301 policy).
  * Per-(node, iface) runtime mutex is held end-to-end.
  * ``Link.runtime.attach_generation`` is stamped atomically with the QMP
    success (mirrors US-204).
  * Slot exhaustion surfaces a user-actionable error.
  * Six-step rollback enumerated in plan §US-303:
      Step 2 (query-pci) fails  → no kernel objects created.
      Step 3 (tap_add) fails    → nothing to clean.
      Step 4 (link_master) fails → tap_del.
      Step 5 (netdev_add) fails  → link_set_nomaster + tap_del.
      Step 6 (device_add) fails  → netdev_del + link_set_nomaster + tap_del.
  * Rollback step failure does not mask the original error.
  * Hot-plug rejected when ``capabilities.hotplug == false`` /
    ``machine != q35``.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import host_net
from app.services.link_service import LinkContentionError, LinkService
from app.services.node_runtime_service import (
    NodeRuntimeError,
    NodeRuntimeQMPTimeout,
    NodeRuntimeService,
)
from app.services.runtime_mutex import RuntimeMutexContention, runtime_mutex


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    NodeRuntimeService.reset_registry()
    runtime_mutex.reset()
    yield
    NodeRuntimeService.reset_registry()
    runtime_mutex.reset()


@pytest.fixture()
def _instance_id(monkeypatch, tmp_path):
    """Provision a fake instance_id file so host_net.tap_name works."""
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance_id").write_text("test-instance-303")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return "test-instance-303"


@pytest.fixture()
def lab_settings(tmp_path, monkeypatch):
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    settings = SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
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
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.link_service.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.services.network_service.get_settings", lambda: settings
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.get_settings", lambda: settings
    )
    return settings


def _seed_lab(labs_dir, lab_name: str, *, nodes=None, networks=None, links=None):
    payload = {
        "schema": 2,
        "id": lab_name.replace(".json", ""),
        "meta": {"name": lab_name},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes or {},
        "networks": networks or {},
        "links": links or [],
        "defaults": {"link_style": "orthogonal"},
    }
    (labs_dir / lab_name).write_text(json.dumps(payload))
    return lab_name


def _qemu_node(node_id: int, *, ethernet: int = 4, qemu_nic: str = "e1000") -> dict:
    return {
        "id": node_id,
        "name": f"vm{node_id}",
        "type": "qemu",
        "template": "vyos",
        "image": "vyos",
        "console": "telnet",
        "status": 0,
        "cpu": 1,
        "ram": 1024,
        "ethernet": ethernet,
        "left": 0,
        "top": 0,
        "icon": "Router.png",
        "firstmac": "52:54:00:00:00:00",
        "extras": {"qemu_nic": qemu_nic},
        "interfaces": [
            {"index": i, "name": f"eth{i}", "planned_mac": None, "port_position": None}
            for i in range(ethernet)
        ],
    }


def _network(net_id: int, name: str = "lan") -> dict:
    return {
        "id": net_id,
        "name": name,
        "type": "linux_bridge",
        "left": 0,
        "top": 0,
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
        "runtime": {"bridge_name": f"novebr{net_id:04x}"},
    }


def _seed_qemu_runtime(
    service: NodeRuntimeService,
    *,
    lab_id: str,
    node_id: int,
    qmp_socket: str = "/tmp/qmp.sock",
    hotplug_capable: bool = True,
    max_nics: int = 8,
    boot_slots: int = 4,
    iface_attachments=None,
    tap_names=None,
    monkeypatch=None,
) -> dict:
    """Seed a fake live qemu runtime record."""
    runtime = {
        "lab_id": lab_id,
        "node_id": node_id,
        "kind": "qemu",
        "pid": 9000 + node_id,
        "qmp_socket": qmp_socket,
        "machine": "q35",
        "max_nics": max_nics,
        "hotplug_capable": hotplug_capable,
        "allocated_slots": list(range(boot_slots)),
        "tap_names": list(tap_names or []),
        "interface_attachments": list(iface_attachments or []),
        "interface_runtime": {},
        "started_at": time.time(),
    }
    key = service._key(lab_id, node_id)
    with service._lock:
        service._registry[key] = runtime
    service._persist_runtime(runtime)
    if monkeypatch is not None:
        monkeypatch.setattr(
            NodeRuntimeService,
            "_is_runtime_alive",
            lambda _self, _rt: True,
        )
    return runtime


class _FakeQmp:
    """Records every QMP call in order; callers may register per-command
    responses or raise on a specific command. Behaves like the
    ``self._qmp_client`` callable that ``_qmp_command`` falls back to,
    accepting either ``(socket, command)`` or ``(socket, command, args)``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses: dict[str, Any] = {}
        self.raise_on: dict[str, Exception] = {}

    def __call__(self, socket_path, command, arguments=None):
        self.calls.append((socket_path, command, dict(arguments) if arguments else None))
        if command in self.raise_on:
            raise self.raise_on[command]
        if command in self.responses:
            return self.responses[command]
        return {"return": {}}


def _query_pci_response(occupied_slots: list[int]) -> dict:
    """Mock a ``query-pci`` response with the given root-port slots
    occupied (i.e. each ``rp{i}`` has a child device).
    """
    devices = []
    for slot in range(8):
        children = []
        if slot in occupied_slots:
            children.append({"qdev_id": f"dev{slot}"})
        devices.append(
            {
                "qdev_id": f"rp{slot}",
                "pci_bridge": {"devices": children},
            }
        )
    return {"return": [{"devices": devices}]}


def _patch_host_net(monkeypatch) -> dict[str, list]:
    """Capture host_net side-effecting calls."""
    calls: dict[str, list] = {
        "tap_add": [],
        "tap_del": [],
        "link_master": [],
        "link_set_nomaster": [],
        "link_up": [],
        "try_link_del": [],
        "bridge_exists": [],
    }

    def _bridge_exists(name):
        calls["bridge_exists"].append(name)
        return True

    monkeypatch.setattr(host_net, "tap_add", lambda n: calls["tap_add"].append(n))
    monkeypatch.setattr(host_net, "tap_del", lambda n: calls["tap_del"].append(n))
    monkeypatch.setattr(
        host_net, "link_master", lambda i, b: calls["link_master"].append((i, b))
    )
    monkeypatch.setattr(
        host_net, "link_set_nomaster", lambda i: calls["link_set_nomaster"].append(i)
    )
    monkeypatch.setattr(host_net, "link_up", lambda i: calls["link_up"].append(i))
    monkeypatch.setattr(
        host_net, "try_link_del", lambda n: calls["try_link_del"].append(n)
    )
    monkeypatch.setattr(host_net, "bridge_exists", _bridge_exists)
    return calls


# ---------------------------------------------------------------------------
# attach_qemu_interface — happy path
# ---------------------------------------------------------------------------


def test_us303_qmp_id_uses_interface_index_not_slot(
    lab_settings, monkeypatch, _instance_id
):
    """The QMP ``id=`` MUST be ``net{interface_index}`` (NOT slot number).

    This preserves the ``_read_qemu_live_mac`` invariant — line ~367 of
    node_runtime_service.py looks up ``f"net{interface_index}"`` in the
    ``query-rx-filter`` response. If hot-add used the slot number instead
    (e.g. ``net5``), the live-MAC reads would silently break.
    """
    lab_id = "lab303"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1, qemu_nic="e1000")},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, max_nics=8, boot_slots=2, monkeypatch=monkeypatch)

    # rp0 + rp1 occupied by the boot-time NICs; rp2..rp7 free → descending
    # scan picks rp7.
    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0, 1])
    svc._qmp_client = fake_qmp

    _patch_host_net(monkeypatch)

    # Use interface_index=2 (the next slot beyond the 2 boot NICs).
    attachment = svc.attach_qemu_interface(
        lab_id, 1, network_id=5, interface_index=2,
        bridge_name="novebr0005",
    )

    # Find the device_add call.
    device_add = next(c for c in fake_qmp.calls if c[1] == "device_add")
    netdev_add = next(c for c in fake_qmp.calls if c[1] == "netdev_add")

    # CRITICAL: id MUST be net{interface_index}, NOT net{slot}.
    assert netdev_add[2]["id"] == "net2", (
        f"netdev_add id must be 'net2' (interface_index), got {netdev_add[2]['id']!r}"
    )
    assert device_add[2]["id"] == "dev2", (
        f"device_add id must be 'dev2' (interface_index), got {device_add[2]['id']!r}"
    )
    # The slot is on bus= only.
    assert device_add[2]["bus"] == "rp7", (
        f"device_add bus must be 'rp7' (highest free slot), got {device_add[2]['bus']!r}"
    )
    # The driver MUST be the configured qemu_nic, not hardcoded virtio.
    assert device_add[2]["driver"] == "e1000", (
        f"driver must come from extras.qemu_nic (e1000), got {device_add[2]['driver']!r}"
    )
    # The netdev MUST point at the same id.
    assert device_add[2]["netdev"] == "net2"
    # Returned attachment captures slot + nic_model + attach_generation.
    assert attachment["slot"] == 7
    assert attachment["nic_model"] == "e1000"
    assert attachment["attach_generation"] == 1


def test_us303_driver_comes_from_extras_qemu_nic(
    lab_settings, monkeypatch, _instance_id
):
    """Codex critic finding #4: ``device_add driver=`` MUST come from
    ``extras.qemu_nic`` (matching the boot-time choice). Mixing
    ``virtio-net-pci`` hot-plug with ``e1000`` boot-time NICs confuses
    the guest's interface ordering.
    """
    lab_id = "labdrv"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1, qemu_nic="virtio-net-pci")},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=1)

    device_add = next(c for c in fake_qmp.calls if c[1] == "device_add")
    assert device_add[2]["driver"] == "virtio-net-pci"


def test_us303_descending_slot_scan_picks_highest_free(
    lab_settings, monkeypatch, _instance_id
):
    """US-301 policy: hot-add scans ``rp{max_nics-1}`` downward so
    additions never collide with the boot-time positional layout
    ``rp0..rp{ethernet-1}``.
    """
    lab_id = "labslot"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, max_nics=8, boot_slots=4, monkeypatch=monkeypatch)

    # rp0..rp3 occupied (boot), rp7 ALSO occupied — descending scan
    # should skip rp7 and pick rp6.
    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0, 1, 2, 3, 7])
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    attachment = svc.attach_qemu_interface(
        lab_id, 1, network_id=5, interface_index=4
    )
    assert attachment["slot"] == 6


def test_us303_slot_exhaustion_surfaces_user_actionable_error(
    lab_settings, monkeypatch, _instance_id
):
    """Plan §US-303 fallback: when ``max_nics`` slots are all occupied,
    the error message tells the operator how to grow the pool.
    """
    lab_id = "labexh"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, max_nics=4, monkeypatch=monkeypatch)

    # Every pre-allocated slot occupied.
    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0, 1, 2, 3])
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeError) as exc_info:
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=4)

    msg = str(exc_info.value)
    assert "All 4 hot-plug slots in use" in msg, msg
    assert "max_nics" in msg, msg


def test_us303_rejects_when_hotplug_capable_false(
    lab_settings, monkeypatch, _instance_id
):
    """Plan §US-303: hot-plug rejected when template's
    ``capabilities.hotplug == false`` (or machine is not q35).
    """
    lab_id = "labnohp"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, hotplug_capable=False, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    svc._qmp_client = fake_qmp

    with pytest.raises(NodeRuntimeError) as exc_info:
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)
    assert "hotplug" in str(exc_info.value).lower()
    # No QMP traffic at all.
    assert fake_qmp.calls == []


def test_us303_rejects_duplicate_interface_index(
    lab_settings, monkeypatch, _instance_id
):
    """Re-attaching the same interface_index to the SAME network is idempotent
    (returns the existing attachment, no QMP calls).  Re-attaching to a
    DIFFERENT network raises NodeRuntimeError naming the current network.
    """
    lab_id = "labdup"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5), "6": _network(6)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(
        svc,
        lab_id=lab_id,
        node_id=1,
        iface_attachments=[
            {"interface_index": 2, "network_id": 5, "tap_name": "nve0001d1i2"}
        ],
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    svc._qmp_client = fake_qmp

    # Same network — idempotent success, no QMP traffic.
    result = svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)
    assert result["interface_index"] == 2
    assert result["network_id"] == 5
    assert fake_qmp.calls == []

    # Different network — should raise naming the currently-attached network.
    with pytest.raises(NodeRuntimeError, match="already attached to network 5"):
        svc.attach_qemu_interface(lab_id, 1, network_id=6, interface_index=2)
    assert fake_qmp.calls == []


# ---------------------------------------------------------------------------
# 6-step rollback (the regression test the plan calls out)
# ---------------------------------------------------------------------------


def test_us303_rollback_query_pci_failure_cleans_nothing(
    lab_settings, monkeypatch, _instance_id
):
    """Step 2 (query-pci) fails BEFORE any kernel object exists, so
    rollback is a no-op (no tap_add, no link_master).
    """
    lab_id = "labrb2"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.raise_on["query-pci"] = OSError("connection refused")
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeError, match="query-pci failed"):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    assert calls["tap_add"] == []
    assert calls["tap_del"] == []
    assert calls["link_master"] == []
    assert calls["link_set_nomaster"] == []


def test_us303_rollback_link_master_failure_cleans_tap(
    lab_settings, monkeypatch, _instance_id
):
    """Step 4 (link_master) fails → tap_del (3 already ran)."""
    lab_id = "labrb4"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=2, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0, 1])
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    def _failing_link_master(_iface, _bridge):
        raise host_net.HostNetError("link_master failed", returncode=1, stderr="")

    monkeypatch.setattr(host_net, "link_master", _failing_link_master)

    with pytest.raises(host_net.HostNetError):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    # Step 3 (tap_add) ran; step 4 failed; rollback called tap_del.
    expected_tap = host_net.tap_name(lab_id, 1, 2)
    assert calls["tap_add"] == [expected_tap]
    assert calls["tap_del"] == [expected_tap]
    # No link_set_nomaster needed (master never attached).
    assert calls["link_set_nomaster"] == []
    # No QMP netdev/device traffic.
    assert not any(c[1] in ("netdev_add", "device_add", "netdev_del") for c in fake_qmp.calls)


def test_us303_rollback_netdev_add_failure_cleans_master_then_tap(
    lab_settings, monkeypatch, _instance_id
):
    """Step 5 (QMP netdev_add) fails → link_set_nomaster + tap_del (in
    that order: undo step 4, then step 3)."""
    lab_id = "labrb5"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([])
    fake_qmp.responses["netdev_add"] = {
        "error": {"class": "GenericError", "desc": "tap open failed"}
    }
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeError, match="netdev_add failed"):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    expected_tap = host_net.tap_name(lab_id, 1, 2)
    assert calls["tap_add"] == [expected_tap]
    assert calls["link_set_nomaster"] == [expected_tap]
    assert calls["tap_del"] == [expected_tap]
    # device_add MUST NOT have run.
    assert not any(c[1] == "device_add" for c in fake_qmp.calls)
    # No netdev_del cleanup needed (netdev_add failed → nothing to delete).
    assert not any(c[1] == "netdev_del" for c in fake_qmp.calls)


def test_us303_rollback_device_add_failure_full_six_step(
    lab_settings, monkeypatch, _instance_id
):
    """Step 6 (QMP device_add) fails → full rollback:
    netdev_del → link_set_nomaster → tap_del.

    THIS is the regression test the plan calls out: the failing 6th step
    must NOT leak the netdev, the bridge attachment, or the TAP. All
    rollback steps wrapped in try/except so a rollback-side failure
    doesn't mask the original error.
    """
    lab_id = "labrb6"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    fake_qmp.responses["netdev_add"] = {"return": {}}
    fake_qmp.responses["device_add"] = {
        "error": {"class": "GenericError", "desc": "no such bus"}
    }
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    expected_bridge = host_net.bridge_name(lab_id, 5)
    with pytest.raises(NodeRuntimeError, match="device_add failed"):
        svc.attach_qemu_interface(
            lab_id, 1, network_id=5, interface_index=3,
            bridge_name=expected_bridge,
        )

    # All 4 host-side / QMP cleanup actions must have happened, in the
    # correct order (reverse of attach order).
    expected_tap = host_net.tap_name(lab_id, 1, 3)
    assert calls["tap_add"] == [expected_tap]
    assert calls["link_master"] == [(expected_tap, expected_bridge)]
    assert calls["link_set_nomaster"] == [expected_tap]
    assert calls["tap_del"] == [expected_tap]
    # netdev_del was issued to undo netdev_add.
    netdev_del = [c for c in fake_qmp.calls if c[1] == "netdev_del"]
    assert len(netdev_del) == 1
    assert netdev_del[0][2] == {"id": "net3"}

    # CRITICAL: runtime record was NOT mutated (no leaked attachment).
    runtime = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert runtime is not None
    assert all(
        int(a.get("interface_index", -1)) != 3
        for a in runtime.get("interface_attachments") or []
    ), "rolled-back attachment must not be persisted on the runtime record"
    assert expected_tap not in (runtime.get("tap_names") or []), (
        "rolled-back TAP must not be persisted on the runtime record"
    )


def test_us303_rollback_logs_but_does_not_mask_when_cleanup_fails(
    lab_settings, monkeypatch, _instance_id, caplog
):
    """A failing rollback step must log but NOT mask the original error
    (plan §US-303: ``All rollback steps wrapped in try/except so a
    rollback failure logs but does not mask the original error``).
    """
    lab_id = "labrb6m"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    fake_qmp.responses["netdev_add"] = {"return": {}}
    fake_qmp.responses["device_add"] = {
        "error": {"class": "GenericError", "desc": "step 6 failure"}
    }
    fake_qmp.raise_on["netdev_del"] = OSError("rollback netdev_del cannot reach socket")
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeError, match="device_add failed"):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    # The original error is still raised even though netdev_del rollback
    # itself raised. The host-side cleanup still ran.
    expected_tap = host_net.tap_name(lab_id, 1, 2)
    assert expected_tap in calls["tap_del"]
    assert expected_tap in calls["link_set_nomaster"]


# ---------------------------------------------------------------------------
# attach_qemu_interface mutex contract
# ---------------------------------------------------------------------------


def test_us303_locked_helper_asserts_mutex_held(
    lab_settings, monkeypatch, _instance_id
):
    """``_attach_qemu_interface_locked`` MUST refuse to run without the
    per-(lab, node, iface) mutex held (defensive contract — Codex v5
    finding #1).
    """
    lab_id = "labmtx"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, monkeypatch=monkeypatch)

    with pytest.raises(AssertionError, match="mutex held"):
        svc._attach_qemu_interface_locked(
            lab_id, 1, network_id=5, interface_index=2
        )


def test_us303_concurrent_same_iface_second_caller_gets_contention_409(
    lab_settings, monkeypatch, _instance_id
):
    """US-303 codex iter1 MEDIUM (Step 1 of test backfill): the runtime
    mutex enforces a BOUNDED wait (default 2.0s). Two concurrent attach
    calls on the same ``(node, iface)`` — the second one MUST raise
    :class:`RuntimeMutexContention` once the wait expires, NOT block
    indefinitely.

    Replaces the previous ``…_serializes_on_same_iface`` test which
    asserted the wrong (unbounded) contract.
    """
    lab_id = "labconc"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    in_section = threading.Event()
    proceed = threading.Event()

    original = svc._qmp_command

    def _slow_qmp(socket_path, command, arguments=None):
        if command == "device_add":
            in_section.set()
            # Hold past the second caller's bounded-wait window.
            proceed.wait(timeout=5.0)
        return original(socket_path, command, arguments)

    monkeypatch.setattr(svc, "_qmp_command", _slow_qmp)

    results: list = []

    def _attach_first():
        try:
            r = svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)
            results.append(("ok", r))
        except Exception as exc:  # noqa: BLE001
            results.append(("err", exc))

    def _attach_second():
        try:
            # Bounded wait: 0.2s — much shorter than the 2.0s default
            # so the test runs in a few hundred ms.
            from app.services.runtime_mutex import runtime_mutex as _m

            with _m.acquire_sync(lab_id, 1, 2, timeout=0.2):
                r = svc._attach_qemu_interface_locked(
                    lab_id, 1, network_id=5, interface_index=2
                )
                results.append(("ok2", r))
        except RuntimeMutexContention as exc:
            results.append(("contention", exc))
        except Exception as exc:  # noqa: BLE001
            results.append(("err", exc))

    t1 = threading.Thread(target=_attach_first)
    t1.start()
    in_section.wait(timeout=2.0)
    # First thread is now blocked in the slow device_add, holding the
    # per-iface mutex. Launch the second attempt with a short timeout.
    t2 = threading.Thread(target=_attach_second)
    t2.start()
    t2.join(timeout=2.0)
    # Now release the first thread.
    proceed.set()
    t1.join(timeout=2.0)

    # The SECOND thread must have gotten contention, NOT serialized
    # success — we want bounded wait + 409, not unbounded blocking.
    statuses = [s for s, _ in results]
    assert "contention" in statuses, (
        f"second thread must hit RuntimeMutexContention within bounded wait; "
        f"got results={results!r}"
    )
    contention = next(v for s, v in results if s == "contention")
    assert isinstance(contention, RuntimeMutexContention)
    assert contention.timeout == 0.2
    # Sanity: first attach succeeded.
    assert "ok" in statuses


# ---------------------------------------------------------------------------
# link_service.create_link → QEMU dispatch + generation stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us303_create_link_dispatches_qemu_running_node(
    lab_settings, monkeypatch, _instance_id
):
    """``link_service.create_link`` for a running QEMU node calls
    ``_attach_qemu_interface_locked`` (NOT the docker path) and stamps
    ``Link.runtime.attach_generation`` atomically with the QMP success.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "labcr"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])

    # Patch the module-level default so every NodeRuntimeService() instance
    # — including the one link_service constructs internally — uses the
    # fake. The registry is class-level so the seeded runtime is visible
    # across instances.
    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client", fake_qmp
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    link_service = LinkService()
    link, network, replayed = await link_service.create_link(
        lab_name,
        {"node_id": 1, "interface_index": 2},
        {"network_id": 5},
    )

    # The QEMU path was used — query-pci + netdev_add + device_add issued.
    cmds = [c[1] for c in fake_qmp.calls]
    assert "query-pci" in cmds
    assert "netdev_add" in cmds
    assert "device_add" in cmds

    # Generation stamped on the link payload.
    assert link["runtime"]["attach_generation"] == 1

    # Persisted on lab.json too.
    saved = json.loads((lab_settings.LABS_DIR / f"{lab_id}.json").read_text())
    persisted_link = saved["links"][0]
    assert persisted_link["runtime"]["attach_generation"] == 1
    # And on node.interfaces[2].runtime.current_attach_generation.
    iface_runtime = saved["nodes"]["1"]["interfaces"][2].get("runtime") or {}
    assert iface_runtime.get("current_attach_generation") == 1


@pytest.mark.asyncio
async def test_us303_create_link_skips_stopped_qemu_node(
    lab_settings, monkeypatch, _instance_id
):
    """Stopped QEMU nodes have no runtime record — create_link writes the
    link to lab.json but does NOT touch QMP. The initial-attach at start
    time will wire it up.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "labstop"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    fake_qmp = _FakeQmp()
    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client", fake_qmp
    )
    svc = NodeRuntimeService()
    # Note: NO seeded runtime → node is "stopped".
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    link_service = LinkService()
    link, _, _ = await link_service.create_link(
        lab_name,
        {"node_id": 1, "interface_index": 2},
        {"network_id": 5},
    )
    assert link["runtime"]["attach_generation"] == 0
    assert fake_qmp.calls == []


@pytest.mark.asyncio
async def test_us303_create_link_rolls_back_lab_json_on_qmp_failure(
    lab_settings, monkeypatch, _instance_id
):
    """When QMP device_add fails on a running QEMU node, link_service
    MUST roll back lab.json (the link record is removed) so the JSON
    file leads kernel state.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "labrbjs"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    fake_qmp.responses["netdev_add"] = {"return": {}}
    fake_qmp.responses["device_add"] = {
        "error": {"class": "GenericError", "desc": "device add bus rp7 not found"}
    }
    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client", fake_qmp
    )
    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    link_service = LinkService()
    with pytest.raises(NodeRuntimeError):
        await link_service.create_link(
            lab_name,
            {"node_id": 1, "interface_index": 2},
            {"network_id": 5},
        )

    # lab.json reverted: no link record persisted.
    saved = json.loads((lab_settings.LABS_DIR / f"{lab_id}.json").read_text())
    assert saved["links"] == [], (
        f"link must be rolled back from lab.json on QMP failure; got {saved['links']!r}"
    )


# ---------------------------------------------------------------------------
# Codex iter1 backfill — Step 3 (tap_add) failure
# ---------------------------------------------------------------------------


def test_us303_rollback_step3_tap_add_failure_runs_nothing_else(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #2: Step 3 (``tap_add``) failure must
    leave NO kernel object behind and MUST NOT progress to step 4
    (``link_master``), step 5 (``netdev_add``), or step 6
    (``device_add``).
    """
    lab_id = "labrb3"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([])
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    def _failing_tap_add(_name):
        raise host_net.HostNetError("tap_add EBUSY", returncode=1, stderr="")

    monkeypatch.setattr(host_net, "tap_add", _failing_tap_add)

    with pytest.raises(host_net.HostNetError):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    # Step 4..6 MUST NOT have run.
    assert calls["link_master"] == []
    assert calls["link_set_nomaster"] == []
    assert calls["tap_del"] == []
    cmds = [c[1] for c in fake_qmp.calls]
    # query-pci ran (step 2 succeeded), but neither netdev_add nor
    # device_add did.
    assert "query-pci" in cmds
    assert "netdev_add" not in cmds
    assert "device_add" not in cmds
    # Slot reservation was released so a retry can pick it up.
    runtime = svc._runtime_record(lab_id, 1, include_stopped=True)
    # ``boot_slots=4`` default → only the boot reservations remain.
    assert sorted(runtime.get("allocated_slots") or []) == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Codex iter1 backfill — Step 5 transport-timeout (HIGH-1 case)
# ---------------------------------------------------------------------------


def test_us303_rollback_step5_netdev_add_transport_timeout_runs_full_chain(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #3 / HIGH-1: a transport-level timeout
    on ``netdev_add`` (vs an in-band command error) MUST cause rollback
    to issue both ``device_del`` (no-op since device wasn't created) AND
    ``netdev_del`` — we cannot tell whether QEMU applied the command
    over the lost connection.

    Without the fix, ``netdev_added`` stayed False on raw transport
    failure and rollback skipped ``netdev_del`` entirely → leaked
    netdev.
    """
    lab_id = "labrbtimeout5"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    # Inject a transport-level OSError on netdev_add — _qmp_command
    # MUST wrap it in NodeRuntimeQMPTimeout.
    fake_qmp.raise_on["netdev_add"] = OSError(
        "qmp socket closed during read"
    )
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeQMPTimeout, match="netdev_add transport"):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    expected_tap = host_net.tap_name(lab_id, 1, 2)
    # Steps 3 + 4 ran.
    assert calls["tap_add"] == [expected_tap]
    assert calls["link_master"]
    # Rollback: link_set_nomaster + tap_del.
    assert calls["link_set_nomaster"] == [expected_tap]
    assert calls["tap_del"] == [expected_tap]
    # CRITICAL: rollback issued netdev_del (idempotent) because we
    # don't know whether QEMU applied the command.
    netdev_del = [c for c in fake_qmp.calls if c[1] == "netdev_del"]
    assert len(netdev_del) == 1, (
        f"transport-timeout on netdev_add MUST issue idempotent "
        f"netdev_del; calls={fake_qmp.calls!r}"
    )
    assert netdev_del[0][2] == {"id": "net2"}
    # device_add must NOT have been issued (we didn't get past step 5).
    assert not any(c[1] == "device_add" for c in fake_qmp.calls)

    # Slot reservation released.
    runtime = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert int(7) not in (runtime.get("allocated_slots") or [])


# ---------------------------------------------------------------------------
# Codex iter1 backfill — Step 6 transport-timeout (HIGH-1 case)
# ---------------------------------------------------------------------------


def test_us303_rollback_step6_device_add_transport_timeout_runs_full_chain(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #4 / HIGH-1: a transport-level timeout
    on ``device_add`` MUST cause rollback to issue both ``device_del``
    AND ``netdev_del``.

    Without the fix, the catch clause only ran ``netdev_del`` (assumed
    netdev_added=True, device_added=False) and silently leaked any
    device QEMU may have created before the connection died.
    """
    lab_id = "labrbtimeout6"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    fake_qmp.responses["netdev_add"] = {"return": {}}
    fake_qmp.raise_on["device_add"] = OSError("qmp socket reset")
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeQMPTimeout, match="device_add transport"):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    # Both device_del AND netdev_del must have been issued (full chain).
    cmds = [c[1] for c in fake_qmp.calls]
    assert "device_del" in cmds, (
        "transport-timeout on device_add MUST issue idempotent device_del"
    )
    assert "netdev_del" in cmds, (
        "transport-timeout on device_add MUST issue netdev_del"
    )
    # Order: device_del before netdev_del (reverse of attach order).
    device_del_idx = cmds.index("device_del")
    netdev_del_idx = cmds.index("netdev_del")
    assert device_del_idx < netdev_del_idx
    expected_tap = host_net.tap_name(lab_id, 1, 2)
    assert calls["link_set_nomaster"] == [expected_tap]
    assert calls["tap_del"] == [expected_tap]


def test_us303_rollback_step6_timeout_idempotent_when_device_del_returns_no_such_device(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #4 (idempotency): when the timeout
    occurred BEFORE QEMU applied device_add, the follow-up rollback
    ``device_del`` returns "no such device". Rollback must swallow this
    and still issue ``netdev_del``.
    """
    lab_id = "labrbidem"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    fake_qmp.responses["netdev_add"] = {"return": {}}
    fake_qmp.raise_on["device_add"] = OSError("qmp socket reset")
    # device_del returns "no such device" — QEMU never applied device_add.
    fake_qmp.responses["device_del"] = {
        "error": {"class": "DeviceNotFound", "desc": "no device with id dev2"}
    }
    fake_qmp.responses["netdev_del"] = {"return": {}}
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeQMPTimeout):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    cmds = [c[1] for c in fake_qmp.calls]
    # The "no such device" reply on device_del must NOT prevent
    # netdev_del.
    assert "device_del" in cmds
    assert "netdev_del" in cmds


# ---------------------------------------------------------------------------
# Codex iter1 backfill — Multi-endpoint rollback on second-endpoint failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us303_multi_endpoint_create_rolls_back_first_on_second_timeout(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #5 / HIGH-1: implicit-bridge node↔node
    create must roll back endpoint A's successful attach when endpoint
    B raises a transport timeout.

    The pre-fix outer rollback only caught
    ``(NodeRuntimeError, host_net.HostNetError)`` — a raw transport
    timeout on the second endpoint bypassed rollback entirely and
    leaked endpoint A's TAP/netdev/device.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "labmulti"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1), "2": _qemu_node(2)},
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    fake_qmp.responses["netdev_add"] = {"return": {}}
    # device_add succeeds for the FIRST endpoint but raises a transport
    # OSError on the SECOND endpoint. Wrap fake_qmp via a callable
    # closure (monkeypatching __call__ on an instance does NOT work in
    # Python — the type's __call__ is what bound calls dispatch to).
    device_add_count = {"n": 0}

    def _selective_qmp(socket_path, command, arguments=None):
        if command == "device_add":
            device_add_count["n"] += 1
            if device_add_count["n"] == 2:
                raise OSError("qmp socket closed mid-device_add")
        return fake_qmp(socket_path, command, arguments)

    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client",
        _selective_qmp,
    )
    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=2, boot_slots=1, monkeypatch=monkeypatch)
    # Use the selective wrapper as the per-instance client too so the
    # test passes through the OSError → NodeRuntimeQMPTimeout wrapping
    # in ``_qmp_command``.
    svc._qmp_client = _selective_qmp
    calls = _patch_host_net(monkeypatch)

    link_service = LinkService()
    with pytest.raises(NodeRuntimeQMPTimeout):
        await link_service.create_link(
            lab_name,
            {"node_id": 1, "interface_index": 2},
            {"node_id": 2, "interface_index": 2},
        )

    # CRITICAL: endpoint A's QMP objects must have been torn down by
    # the outer rollback in link_service. We expect a device_del +
    # netdev_del with id derived from interface_index=2.
    cmds = [c[1] for c in fake_qmp.calls]
    assert "device_del" in cmds, (
        "outer rollback MUST issue device_del for the first (succeeded) "
        f"endpoint; calls={cmds!r}"
    )
    assert "netdev_del" in cmds, (
        "outer rollback MUST issue netdev_del for the first (succeeded) "
        f"endpoint; calls={cmds!r}"
    )
    # The host-side TAP for endpoint A must have been torn down too.
    expected_tap_a = host_net.tap_name(lab_id, 1, 2)
    assert expected_tap_a in calls["try_link_del"] or expected_tap_a in calls["link_set_nomaster"]


# ---------------------------------------------------------------------------
# Codex iter1 backfill — PCIe slot race regression (HIGH-2)
# ---------------------------------------------------------------------------


def test_us303_concurrent_attach_different_ifaces_get_different_slots(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #6 / HIGH-2: two concurrent attaches
    to DIFFERENT interfaces on the same VM must NOT race on slot
    allocation. Without the node-scoped lock both calls would see the
    same free ``rpN`` from query-pci and try to attach to the same bus.

    Pre-fix expected failure mode: both calls pick the same slot,
    second device_add fails with "bus already in use" or both succeed
    with corrupted topology.

    Post-fix: the per-(lab, node) slot lock serializes the slot-pick →
    device_add window so the two calls receive distinct slots.
    """
    lab_id = "labslotrace"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1, ethernet=8)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    # boot_slots=2 → rp0+rp1 occupied, rp2..rp7 free.
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, max_nics=8, boot_slots=2, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0, 1])
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    # Coordination: thread 1 enters the slot-pick window and holds
    # there. Thread 2 races in concurrently — without the node-scoped
    # lock it would call query-pci immediately and pick the SAME free
    # slot (rp7). With the lock thread 2 blocks until thread 1 finishes
    # and reserves its slot, then sees rp7 as taken and picks rp6.
    thread1_in_slot_pick = threading.Event()
    thread1_proceed = threading.Event()
    pick_call_count = {"n": 0}
    pick_call_lock = threading.Lock()

    original_find = svc._find_free_pcie_slot

    def _coordinated_find(socket_path, max_nics, *, reserved_slots=None):
        with pick_call_lock:
            pick_call_count["n"] += 1
            n = pick_call_count["n"]
        if n == 1:
            # Thread 1 enters and signals; then waits for green light so
            # we know thread 2 is parked at the node-lock.
            thread1_in_slot_pick.set()
            thread1_proceed.wait(timeout=5.0)
        return original_find(socket_path, max_nics, reserved_slots=reserved_slots)

    monkeypatch.setattr(svc, "_find_free_pcie_slot", _coordinated_find)

    results: list = []
    lock = threading.Lock()

    def _attach(iface_index):
        try:
            r = svc.attach_qemu_interface(
                lab_id, 1, network_id=5, interface_index=iface_index
            )
            with lock:
                results.append(("ok", iface_index, r))
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(("err", iface_index, exc))

    t1 = threading.Thread(target=_attach, args=(2,))
    t1.start()
    # Wait for thread 1 to enter the slot-pick window (already holding
    # the node-scoped lock).
    assert thread1_in_slot_pick.wait(timeout=2.0)
    # Now launch thread 2 — it MUST block on the node-scoped lock.
    t2 = threading.Thread(target=_attach, args=(3,))
    t2.start()
    # Give thread 2 a moment to attempt its slot-pick (it should be
    # blocked, not progressed).
    time.sleep(0.05)
    with pick_call_lock:
        # If the node lock is doing its job, only thread 1 has called
        # _find_free_pcie_slot so far. If the lock is missing, thread 2
        # would already have entered and picked the same rp7 → race.
        observed_calls_before_release = pick_call_count["n"]
    # Release thread 1.
    thread1_proceed.set()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    # CRITICAL: serialization — thread 2 MUST NOT have entered slot-pick
    # while thread 1 was inside it. Pre-fix it would have entered
    # immediately and the count would have been 2.
    assert observed_calls_before_release == 1, (
        f"node-scoped slot lock did not serialize slot-pick: thread 2 "
        f"entered while thread 1 still held the window "
        f"(observed_calls_before_release={observed_calls_before_release})"
    )

    statuses = [s for s, _, _ in results]
    assert statuses.count("ok") == 2, f"both attaches must succeed; got {results!r}"

    # CRITICAL: the two attaches must have received DIFFERENT slots.
    # Pre-fix this would have been the same slot (the race).
    slots = sorted(r["slot"] for s, _, r in results if s == "ok")
    assert len(set(slots)) == 2, (
        f"concurrent attaches to different ifaces on the same VM picked "
        f"the SAME slot — slot allocation race! slots={slots!r}"
    )


# ---------------------------------------------------------------------------
# Codex iter1 backfill — link_up failure branch
# ---------------------------------------------------------------------------


def test_us303_rollback_link_up_failure_cleans_master_and_tap(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #7: ``host_net.link_up`` runs between
    step 4 (link_master) and step 5 (netdev_add) and was previously
    untested. A failure here must roll back identically to a step-4
    failure: undo link_master + tap_add, no QMP cleanup needed.
    """
    lab_id = "labrblinkup"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    def _failing_link_up(_iface):
        raise host_net.HostNetError("link_up EBUSY", returncode=1, stderr="")

    monkeypatch.setattr(host_net, "link_up", _failing_link_up)

    with pytest.raises(host_net.HostNetError):
        svc.attach_qemu_interface(lab_id, 1, network_id=5, interface_index=2)

    expected_tap = host_net.tap_name(lab_id, 1, 2)
    # Steps 3 + 4 ran (tap + master).
    assert calls["tap_add"] == [expected_tap]
    assert calls["link_master"]
    # Rollback ran: link_set_nomaster + tap_del.
    assert calls["link_set_nomaster"] == [expected_tap]
    assert calls["tap_del"] == [expected_tap]
    # No QMP traffic past query-pci (step 5/6 never ran).
    cmds = [c[1] for c in fake_qmp.calls]
    assert "netdev_add" not in cmds
    assert "device_add" not in cmds
    assert "netdev_del" not in cmds
    assert "device_del" not in cmds


# ---------------------------------------------------------------------------
# Codex iter1 backfill — planned-MAC observability after hot-add
# ---------------------------------------------------------------------------


def test_us303_planned_mac_persisted_after_hot_add_for_firstmac_default(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #8: when ``interface.planned_mac`` is
    None (default ``firstmac+offset`` case), the value computed at
    hot-add time MUST be persisted onto ``node.interfaces[i].planned_mac``
    so the live-MAC mismatch detector (``_read_qemu_live_mac``) has a
    real value to compare against.

    Pre-fix the default case left ``planned_mac=None`` in lab.json and
    the mismatch detector silently compared the live MAC against an
    empty string → always reported "confirmed" regardless of the actual
    guest MAC.
    """
    lab_id = "labpm"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    # Hot-add interface_index=2.
    attachment = svc.attach_qemu_interface(
        lab_id, 1, network_id=5, interface_index=2
    )
    # The attachment record carries the computed planned_mac too.
    assert attachment["planned_mac"], (
        f"attachment must carry the planned_mac that was passed to "
        f"device_add; got {attachment!r}"
    )

    # device_add was issued with mac=<planned_mac>.
    device_add = next(c for c in fake_qmp.calls if c[1] == "device_add")
    assert device_add[2].get("mac") == attachment["planned_mac"]

    # CRITICAL: lab.json now has the planned_mac persisted on
    # node.interfaces[2].planned_mac so the mismatch detector can read
    # it back.
    saved = json.loads((lab_settings.LABS_DIR / f"{lab_id}.json").read_text())
    iface = saved["nodes"]["1"]["interfaces"][2]
    assert iface["planned_mac"] == attachment["planned_mac"], (
        f"planned_mac must be persisted on node.interfaces[2]; "
        f"got iface={iface!r}"
    )


def test_us303_planned_mac_does_not_overwrite_explicit_operator_value(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #8 (idempotency): if the operator
    explicitly set ``interface.planned_mac`` in lab.json, hot-add MUST
    use that value AND must not overwrite it with a recomputed default.
    """
    lab_id = "labpmexp"
    explicit_mac = "52:54:00:de:ad:42"
    node = _qemu_node(1)
    node["interfaces"][2]["planned_mac"] = explicit_mac
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": node},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    attachment = svc.attach_qemu_interface(
        lab_id, 1, network_id=5, interface_index=2
    )
    assert attachment["planned_mac"].lower() == explicit_mac.lower()

    # device_add used the explicit MAC.
    device_add = next(c for c in fake_qmp.calls if c[1] == "device_add")
    assert device_add[2]["mac"].lower() == explicit_mac.lower()

    # lab.json still carries the operator's value (idempotent — not
    # overwritten with a recomputed default).
    saved = json.loads((lab_settings.LABS_DIR / f"{lab_id}.json").read_text())
    assert saved["nodes"]["1"]["interfaces"][2]["planned_mac"] == explicit_mac


# ---------------------------------------------------------------------------
# Codex iter1 backfill — link_service 409 mapping for contention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us303_link_service_create_link_409_on_mutex_contention(
    lab_settings, monkeypatch, _instance_id
):
    """Codex iter1 test backfill #1 (Step 1): when the per-(lab, node,
    iface) mutex is already held by another in-flight call,
    ``link_service.create_link`` must raise :class:`LinkContentionError`
    so the router can return HTTP 409.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab409"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_qemu_runtime(svc, lab_id=lab_id, node_id=1, boot_slots=1, monkeypatch=monkeypatch)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["query-pci"] = _query_pci_response([0])
    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client", fake_qmp
    )
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    # Pre-acquire the mutex on another thread — release after the test
    # has exercised the contention path.
    proceed = threading.Event()
    holder_started = threading.Event()

    def _hold():
        with runtime_mutex.acquire_sync(lab_id, 1, 2, timeout=5.0):
            holder_started.set()
            proceed.wait(timeout=5.0)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holder_started.wait(timeout=2.0)

    link_service = LinkService()
    # Patch the mutex's acquire timeout via direct call: we cannot pass
    # a custom timeout through link_service.create_link, so we monkey-
    # patch the registry's default timeout for this test.
    monkeypatch.setattr(
        "app.services.runtime_mutex.DEFAULT_ACQUIRE_TIMEOUT_S", 0.2
    )

    try:
        with pytest.raises(LinkContentionError):
            await link_service.create_link(
                lab_name,
                {"node_id": 1, "interface_index": 2},
                {"network_id": 5},
            )
    finally:
        proceed.set()
        holder.join(timeout=2.0)
