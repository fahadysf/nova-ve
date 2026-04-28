# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-304 — QMP-driven hot-remove NIC (with bounded poll + forced fallback).

Acceptance criteria exercised here (mirrors plan §US-304 lines 437-442):

  * ``link_service.delete_link`` dispatches a running QEMU node to
    ``_detach_qemu_interface_locked`` (mirrors the docker hot-detach flow).
  * Hot-remove executes the QMP/host steps in order:
        device_del -> bounded query-pci poll -> netdev_del -> tap_del.
  * Bounded poll: ``query-pci`` every 500ms for up to 8s (16 iterations).
  * Forced fallback at timeout: ``host_net.link_set_nomaster(tap)`` runs,
    ``netdev_del`` and ``tap_del`` are NOT issued, and a ``node_warning``
    WS event is broadcast.
  * Race A (``DeviceNotFound`` on first ``device_del``): retry once after
    0.5s, treat second-time DeviceNotFound as already-detached idempotent
    success.
  * Idempotent double-delete: second detach call on the same iface
    no-ops cleanly (returns ``state='absent'``).
  * Mutex contention: concurrent attach + detach on the same iface — the
    second caller raises :class:`RuntimeMutexContention` after the
    bounded wait expires; ``link_service.delete_link`` translates this to
    :class:`LinkContentionError` (HTTP 409).
  * Transport timeout on ``device_del`` propagates as
    :class:`NodeRuntimeQMPTimeout` and is NOT swallowed as a forced
    fallback (transport timeout != guest didn't eject).
  * Transport timeout on ``netdev_del`` after the device is gone still
    runs ``host_net.tap_del`` (host-side cleanup is preserved); error
    raised after.
  * Generation token is bumped under the lab_lock-equivalent runtime
    lock after the kernel objects are removed.
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
# Shared fixtures (mirrors test_us303_qemu_hot_add.py)
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
    (instance_dir / "instance_id").write_text("test-instance-304")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return "test-instance-304"


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


def _link(
    link_id: str,
    node_id: int,
    iface_idx: int,
    net_id: int,
    *,
    attach_generation: int = 1,
) -> dict:
    return {
        "id": link_id,
        "from": {"node_id": node_id, "interface_index": iface_idx},
        "to": {"network_id": net_id},
        "style_override": None,
        "label": "",
        "color": "",
        "width": "1",
        "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
        "runtime": {"attach_generation": attach_generation},
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
    responses (or response sequences) or raise on a specific command.
    Behaves like the ``self._qmp_client`` callable that ``_qmp_command``
    falls back to, accepting either ``(socket, command)`` or
    ``(socket, command, args)``.

    For US-304 query-pci polling support, a per-command list of
    responses cycles in order: each call pops the next response (last
    response repeats indefinitely once exhausted).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses: dict[str, Any] = {}
        self.response_seqs: dict[str, list[Any]] = {}
        self.raise_on: dict[str, Exception] = {}
        self.raise_on_nth: dict[str, list[tuple[int, Exception]]] = {}

    def __call__(self, socket_path, command, arguments=None):
        self.calls.append((socket_path, command, dict(arguments) if arguments else None))
        # Per-call raise (e.g. "raise on first device_del, succeed on retry").
        if command in self.raise_on_nth and self.raise_on_nth[command]:
            n_called = sum(1 for c in self.calls if c[1] == command)
            for i, (target_n, exc) in enumerate(self.raise_on_nth[command]):
                if target_n == n_called:
                    self.raise_on_nth[command].pop(i)
                    raise exc
        if command in self.raise_on:
            raise self.raise_on[command]
        if command in self.response_seqs and self.response_seqs[command]:
            seq = self.response_seqs[command]
            response = seq[0] if len(seq) == 1 else seq.pop(0)
            return response
        if command in self.responses:
            return self.responses[command]
        return {"return": {}}


def _query_pci_response_with_device(device_id: str | None) -> dict:
    """Mock a ``query-pci`` response that does or does not include the
    given ``device_id`` (e.g. ``"dev2"``) under ``rp7``.

    Returns a tree shape compatible with both
    :meth:`_find_free_pcie_slot` and :meth:`_qemu_device_gone`.
    """
    children = []
    if device_id is not None:
        children.append({"qdev_id": device_id})
    return {
        "return": [
            {
                "devices": [
                    {
                        "qdev_id": "rp7",
                        "pci_bridge": {"devices": children},
                    }
                ]
            }
        ]
    }


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

    def _tap_del(name):
        calls["tap_del"].append(name)

    monkeypatch.setattr(host_net, "tap_add", lambda n: calls["tap_add"].append(n))
    monkeypatch.setattr(host_net, "tap_del", _tap_del)
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


def _seed_attached_qemu(
    svc: NodeRuntimeService,
    *,
    lab_id: str,
    node_id: int,
    interface_index: int,
    network_id: int,
    slot: int = 7,
    monkeypatch,
) -> dict:
    """Seed a qemu runtime with one already-attached interface so the
    detach path has something to undo. Returns the runtime dict.
    """
    tap = host_net.tap_name(lab_id, node_id, interface_index)
    bridge = f"novebr{network_id:04x}"
    runtime = _seed_qemu_runtime(
        svc,
        lab_id=lab_id,
        node_id=node_id,
        boot_slots=2,
        iface_attachments=[
            {
                "interface_index": interface_index,
                "network_id": network_id,
                "bridge_name": bridge,
                "tap_name": tap,
                "slot": slot,
                "nic_model": "e1000",
                "attach_generation": 1,
                "planned_mac": "52:54:00:00:00:02",
            }
        ],
        tap_names=[tap],
        monkeypatch=monkeypatch,
    )
    runtime["allocated_slots"] = [0, 1, slot]
    runtime["interface_runtime"] = {
        str(interface_index): {"current_attach_generation": 1}
    }
    svc._persist_runtime(runtime)
    return runtime


# ---------------------------------------------------------------------------
# detach_qemu_interface — happy path
# ---------------------------------------------------------------------------


def test_us304_happy_path_device_del_then_query_pci_then_netdev_and_tap(
    lab_settings, monkeypatch, _instance_id
):
    """Plan §US-304 happy path: device_del -> query-pci shows gone after
    a brief delay -> netdev_del + tap_del in order.
    """
    lab_id = "lab304ok"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    # query-pci: first call still shows dev2; second call is empty.
    fake_qmp.response_seqs["query-pci"] = [
        _query_pci_response_with_device("dev2"),
        _query_pci_response_with_device(None),
    ]
    fake_qmp.responses["netdev_del"] = {"return": {}}
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    expected_tap = host_net.tap_name(lab_id, 1, 2)
    result = svc.detach_qemu_interface(lab_id, 1, 2)

    assert result["state"] == "detached"
    assert result["tap_name"] == expected_tap
    # Generation bumped from 1 to 2.
    assert result["current_attach_generation"] == 2

    # Order of QMP commands.
    cmds = [c[1] for c in fake_qmp.calls]
    assert cmds[0] == "device_del"
    assert "query-pci" in cmds
    assert "netdev_del" in cmds
    netdev_idx = cmds.index("netdev_del")
    qpci_first = cmds.index("query-pci")
    assert qpci_first < netdev_idx, (
        f"query-pci poll must run before netdev_del; cmds={cmds!r}"
    )

    # Args.
    device_del = next(c for c in fake_qmp.calls if c[1] == "device_del")
    assert device_del[2] == {"id": "dev2"}
    netdev_del = next(c for c in fake_qmp.calls if c[1] == "netdev_del")
    assert netdev_del[2] == {"id": "net2"}

    # tap_del was called with the canonical TAP name.
    assert calls["tap_del"] == [expected_tap]
    # No forced-fallback link_set_nomaster.
    assert calls["link_set_nomaster"] == []

    # Runtime record cleaned up.
    runtime = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert runtime is not None
    assert all(
        int(a.get("interface_index", -1)) != 2
        for a in runtime.get("interface_attachments") or []
    )
    assert expected_tap not in (runtime.get("tap_names") or [])
    # Slot reservation released.
    assert 7 not in (runtime.get("allocated_slots") or [])


# ---------------------------------------------------------------------------
# Bounded poll cadence — at most 16 query-pci calls
# ---------------------------------------------------------------------------


def test_us304_bounded_poll_at_most_16_query_pci_calls(
    lab_settings, monkeypatch, _instance_id
):
    """Plan §US-304: bounded poll runs ``query-pci`` every 500ms for up
    to 8s (16 iterations). When the device never disappears, the count
    must NOT exceed 16.
    """
    lab_id = "lab304poll"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    # Always shows the device — forced fallback.
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device("dev2")
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    # Speed up the test: replace the 500ms sleep with a no-op.
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    svc.detach_qemu_interface(lab_id, 1, 2)

    qpci_count = sum(1 for c in fake_qmp.calls if c[1] == "query-pci")
    assert qpci_count <= 16, (
        f"bounded poll must not exceed 16 query-pci calls; got {qpci_count}"
    )
    # And it should have run the full 16 since the device never leaves.
    assert qpci_count == 16, (
        f"expected exactly 16 poll iterations on forced-fallback path; "
        f"got {qpci_count}"
    )


# ---------------------------------------------------------------------------
# Forced fallback — guest never ejects
# ---------------------------------------------------------------------------


def test_us304_forced_fallback_when_device_persists_past_timeout(
    lab_settings, monkeypatch, _instance_id
):
    """Plan §US-304: when ``query-pci`` still shows the device after 8s,
    ``host_net.link_set_nomaster(tap)`` runs, ``netdev_del`` and
    ``tap_del`` are NOT issued, and a ``node_warning`` WS event is
    broadcast.
    """
    lab_id = "lab304forced"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device("dev2")
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    # Capture WS publishes.
    published: list[tuple[str, str, dict]] = []

    async def _capture_publish(lab, event_type, payload, rev=""):
        published.append((lab, event_type, payload))

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _capture_publish)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    expected_tap = host_net.tap_name(lab_id, 1, 2)
    result = svc.detach_qemu_interface(lab_id, 1, 2)

    assert result["state"] == "forced", result

    # Forced fallback: link_set_nomaster MUST have run, tap_del MUST NOT
    # have, netdev_del MUST NOT have been issued.
    assert calls["link_set_nomaster"] == [expected_tap], (
        f"forced fallback must call link_set_nomaster({expected_tap!r}); "
        f"got {calls['link_set_nomaster']!r}"
    )
    assert calls["tap_del"] == [], (
        f"forced fallback MUST NOT call tap_del; got {calls['tap_del']!r}"
    )
    cmds = [c[1] for c in fake_qmp.calls]
    assert "netdev_del" not in cmds, (
        f"forced fallback MUST NOT issue netdev_del; cmds={cmds!r}"
    )

    # ws_hub.publish was called with a node_warning event.
    warning_events = [e for e in published if e[1] == "node_warning"]
    assert warning_events, (
        f"forced fallback must publish a node_warning event; "
        f"published={published!r}"
    )
    _lab_arg, _event_type, payload = warning_events[0]
    assert payload["node_id"] == 1
    assert payload["interface_index"] == 2
    assert "restart" in payload["message"].lower()


# ---------------------------------------------------------------------------
# Race A — DeviceNotFound on first device_del, retry succeeds
# ---------------------------------------------------------------------------


def test_us304_device_not_found_race_a_retries_and_succeeds(
    lab_settings, monkeypatch, _instance_id
):
    """Plan §US-304: ``device_del`` returning ``DeviceNotFound`` on the
    first call (race A — delete arrived before the create-side flushed
    device_add). After a 0.5s wait, retry once. Second-time
    ``DeviceNotFound`` means the device was never created; treat as
    already-detached idempotent success.
    """
    lab_id = "lab304racea"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    # First device_del: DeviceNotFound. Second: also DeviceNotFound
    # (idempotent absent — device was never created).
    fake_qmp.response_seqs["device_del"] = [
        {"error": {"class": "DeviceNotFound", "desc": "no device with id dev2"}},
        {"error": {"class": "DeviceNotFound", "desc": "no device with id dev2"}},
    ]
    # query-pci shouldn't matter — device is already gone.
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.responses["netdev_del"] = {"return": {}}
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    expected_tap = host_net.tap_name(lab_id, 1, 2)
    result = svc.detach_qemu_interface(lab_id, 1, 2)

    # Treated as detached (idempotent success path).
    assert result["state"] == "detached"
    assert result.get("device_already_gone") is True

    # Two device_del calls.
    device_del_calls = [c for c in fake_qmp.calls if c[1] == "device_del"]
    assert len(device_del_calls) == 2, (
        f"DeviceNotFound race must trigger exactly one retry; "
        f"got {len(device_del_calls)} device_del calls"
    )
    # Host-side: tap_del still ran (idempotent cleanup of host objects).
    assert calls["tap_del"] == [expected_tap]


# ---------------------------------------------------------------------------
# Idempotent double-delete — second call no-ops
# ---------------------------------------------------------------------------


def test_us304_double_delete_second_call_is_absent_no_op(
    lab_settings, monkeypatch, _instance_id
):
    """Calling ``detach_qemu_interface`` twice on the same iface must
    succeed cleanly the second time: no QMP traffic, returns
    ``state='absent'``.
    """
    lab_id = "lab304dbl"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.responses["netdev_del"] = {"return": {}}
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    # First detach succeeds.
    first = svc.detach_qemu_interface(lab_id, 1, 2)
    assert first["state"] == "detached"

    # Snapshot QMP call count after the first detach.
    first_call_count = len(fake_qmp.calls)

    # Second detach: must no-op cleanly.
    second = svc.detach_qemu_interface(lab_id, 1, 2)
    assert second["state"] == "absent", second

    # No additional QMP traffic on the no-op call.
    assert len(fake_qmp.calls) == first_call_count, (
        f"second (no-op) detach issued QMP traffic; calls grew "
        f"{first_call_count} -> {len(fake_qmp.calls)}"
    )


# ---------------------------------------------------------------------------
# Mutex contention — concurrent attach + detach
# ---------------------------------------------------------------------------


def test_us304_concurrent_attach_and_detach_second_caller_gets_contention(
    lab_settings, monkeypatch, _instance_id
):
    """Concurrent ``attach`` + ``detach`` on the same ``(node, iface)``:
    the second caller MUST raise :class:`RuntimeMutexContention` once
    the bounded wait expires (US-303 contract carries forward to US-304).
    """
    lab_id = "lab304conc"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.responses["netdev_del"] = {"return": {}}
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    # Pre-acquire the mutex on a holder thread so the detach call sees
    # contention.
    proceed = threading.Event()
    holder_started = threading.Event()

    def _hold():
        with runtime_mutex.acquire_sync(lab_id, 1, 2, timeout=5.0):
            holder_started.set()
            proceed.wait(timeout=5.0)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holder_started.wait(timeout=2.0)

    try:
        with pytest.raises(RuntimeMutexContention):
            # Bounded wait via the public entrypoint; we monkey-patch
            # the registry default to keep the test fast.
            monkeypatch.setattr(
                "app.services.runtime_mutex.DEFAULT_ACQUIRE_TIMEOUT_S", 0.2
            )
            svc.detach_qemu_interface(lab_id, 1, 2)
    finally:
        proceed.set()
        holder.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Transport timeout — NodeRuntimeQMPTimeout NOT swallowed
# ---------------------------------------------------------------------------


def test_us304_transport_timeout_on_device_del_propagates(
    lab_settings, monkeypatch, _instance_id
):
    """Transport-level QMP failures on ``device_del`` MUST propagate as
    :class:`NodeRuntimeQMPTimeout` — they are NOT swallowed as a
    forced-fallback (transport timeout != guest didn't eject).
    """
    lab_id = "lab304ddtimeout"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    fake_qmp.raise_on["device_del"] = OSError("qmp socket reset")
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    with pytest.raises(NodeRuntimeQMPTimeout, match="device_del transport"):
        svc.detach_qemu_interface(lab_id, 1, 2)

    # No host-side cleanup ran (transport failure aborted the path
    # before the bounded poll / netdev_del / tap_del).
    assert calls["tap_del"] == []
    assert calls["link_set_nomaster"] == []


def test_us304_transport_timeout_on_netdev_del_after_device_gone_still_cleans_tap(
    lab_settings, monkeypatch, _instance_id
):
    """When ``device_del`` succeeds and the device is gone, but the
    follow-up ``netdev_del`` raises a transport timeout, the host-side
    ``tap_del`` MUST still run so the kernel TAP is cleaned up. The
    error is raised AFTER the host-side cleanup.
    """
    lab_id = "lab304ndtimeout"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.raise_on["netdev_del"] = OSError("qmp socket dropped")
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    expected_tap = host_net.tap_name(lab_id, 1, 2)

    with pytest.raises(NodeRuntimeQMPTimeout, match="netdev_del transport"):
        svc.detach_qemu_interface(lab_id, 1, 2)

    # tap_del MUST have run BEFORE the exception.
    assert calls["tap_del"] == [expected_tap], (
        f"netdev_del transport timeout must still run tap_del; "
        f"calls['tap_del']={calls['tap_del']!r}"
    )


# ---------------------------------------------------------------------------
# link_service.delete_link → QEMU dispatch + 409 mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us304_link_service_delete_link_dispatches_to_qemu_detach(
    lab_settings, monkeypatch, _instance_id
):
    """``link_service.delete_link`` for a running QEMU node calls
    ``_detach_qemu_interface_locked`` (NOT the docker path).
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab304ls"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
        links=[_link("lnk_001", 1, 2, 5, attach_generation=1)],
    )

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.responses["netdev_del"] = {"return": {}}
    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client", fake_qmp
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    link_service = LinkService()
    ok, deleted_net = await link_service.delete_link(lab_name, "lnk_001")
    assert ok is False
    assert deleted_net is None

    # QEMU detach path was taken.
    cmds = [c[1] for c in fake_qmp.calls]
    assert "device_del" in cmds
    assert "netdev_del" in cmds
    expected_tap = host_net.tap_name(lab_id, 1, 2)
    assert calls["tap_del"] == [expected_tap]

    # Link removed from lab.json.
    saved = json.loads((lab_settings.LABS_DIR / f"{lab_id}.json").read_text())
    assert saved["links"] == []


@pytest.mark.asyncio
async def test_us304_link_service_delete_link_409_on_qemu_mutex_contention(
    lab_settings, monkeypatch, _instance_id
):
    """``link_service.delete_link`` translates a
    :class:`RuntimeMutexContention` raised inside
    ``_detach_qemu_interface_locked`` into a :class:`LinkContentionError`
    so the router can return HTTP 409.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab304409"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
        links=[_link("lnk_001", 1, 2, 5, attach_generation=1)],
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )
    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.responses["netdev_del"] = {"return": {}}
    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client", fake_qmp
    )
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)

    # Pre-acquire the mutex on a holder thread.
    proceed = threading.Event()
    holder_started = threading.Event()

    def _hold():
        with runtime_mutex.acquire_sync(lab_id, 1, 2, timeout=5.0):
            holder_started.set()
            proceed.wait(timeout=5.0)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holder_started.wait(timeout=2.0)

    monkeypatch.setattr(
        "app.services.runtime_mutex.DEFAULT_ACQUIRE_TIMEOUT_S", 0.2
    )
    link_service = LinkService()
    try:
        with pytest.raises(LinkContentionError):
            await link_service.delete_link(lab_name, "lnk_001")
    finally:
        proceed.set()
        holder.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Generation token bumped after kernel objects removed
# ---------------------------------------------------------------------------


def test_us304_generation_token_bumped_on_happy_path(
    lab_settings, monkeypatch, _instance_id
):
    """Plan §US-304: ``current_attach_generation`` MUST be bumped after
    the kernel objects are removed so a stale concurrent attach does
    not race a freshly torn-down NIC. The bump happens under the
    runtime lock (per-node ``self._lock`` in NodeRuntimeService) inside
    the per-(lab, node, iface) mutex window.
    """
    lab_id = "lab304gen"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    runtime = _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )
    # Pre-detach: generation token = 1.
    pre_gen = svc._interface_attach_generation(runtime, 2)
    assert pre_gen == 1

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.responses["netdev_del"] = {"return": {}}
    svc._qmp_client = fake_qmp
    _patch_host_net(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    result = svc.detach_qemu_interface(lab_id, 1, 2)

    # Post-detach: generation token bumped to 2.
    runtime_after = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert runtime_after is not None
    post_gen = svc._interface_attach_generation(runtime_after, 2)
    assert post_gen == 2, (
        f"generation token must bump from 1 to 2 after kernel cleanup; "
        f"got {post_gen}"
    )
    assert result["current_attach_generation"] == 2


# ---------------------------------------------------------------------------
# Locked helper asserts mutex held (defensive contract)
# ---------------------------------------------------------------------------


def test_us304_locked_helper_asserts_mutex_held(
    lab_settings, monkeypatch, _instance_id
):
    """``_detach_qemu_interface_locked`` MUST refuse to run without the
    per-(lab, node, iface) mutex held (defensive contract — mirrors
    US-303 / US-204b).
    """
    lab_id = "lab304mtx"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )

    with pytest.raises(AssertionError, match="mutex held"):
        svc._detach_qemu_interface_locked(lab_id, 1, 2)


# ---------------------------------------------------------------------------
# Codex hotfix HIGH-1 — stale generation does NOT detach the QEMU NIC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us304_stale_generation_does_not_detach_qemu_nic(
    lab_settings, monkeypatch, _instance_id
):
    """When the link's ``attach_generation`` is older than the runtime's
    ``current_attach_generation``, the QEMU detach path MUST return
    ``state='stale_noop'`` and NOT issue ``device_del`` / ``netdev_del`` /
    ``tap_del`` — mirroring the docker freshness contract from US-205.

    This is the regression for codex hotfix HIGH-1: QMP IDs reuse
    ``dev{iface}`` / ``net{iface}`` deterministically, so without the
    generation check a stale rollback would tear down a NEWER QEMU NIC
    that is still live on the same iface.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab304stalegen"
    stale_gen = 1
    current_gen = 2

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
        # Link carries the OLD generation (stale_gen=1).
        links=[_link("lnk_001", 1, 2, 5, attach_generation=stale_gen)],
    )

    svc = NodeRuntimeService()
    runtime = _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )
    # Bump the runtime generation to current_gen so the link's stamped
    # value is older than the live attachment.
    runtime["interface_runtime"] = {
        "2": {"current_attach_generation": current_gen}
    }
    runtime["interface_attachments"][0]["attach_generation"] = current_gen
    svc._persist_runtime(runtime)

    fake_qmp = _FakeQmp()
    fake_qmp.responses["device_del"] = {"return": {}}
    fake_qmp.responses["query-pci"] = _query_pci_response_with_device(None)
    fake_qmp.responses["netdev_del"] = {"return": {}}
    monkeypatch.setattr(
        "app.services.node_runtime_service._default_qmp_client", fake_qmp
    )
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda _s: None
    )

    link_service = LinkService()
    await link_service.delete_link(lab_name, "lnk_001")

    # The JSON link IS removed (we committed the delete) — same contract
    # as US-205 stale path.
    saved = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    assert saved["links"] == []

    # No QMP traffic at all — stale check short-circuits BEFORE device_del.
    qmp_cmds = [c[1] for c in fake_qmp.calls]
    assert "device_del" not in qmp_cmds, (
        f"stale-gen detach must not issue device_del; cmds={qmp_cmds!r}"
    )
    assert "netdev_del" not in qmp_cmds, (
        f"stale-gen detach must not issue netdev_del; cmds={qmp_cmds!r}"
    )

    # No host-side TAP cleanup either.
    assert calls["tap_del"] == [], (
        f"stale-gen detach must not call tap_del; got {calls['tap_del']!r}"
    )
    assert calls["link_set_nomaster"] == [], (
        f"stale-gen detach must not call link_set_nomaster; "
        f"got {calls['link_set_nomaster']!r}"
    )

    # The newer attachment row is still in the runtime record.
    updated_rt = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert updated_rt is not None
    matching = [
        a for a in (updated_rt.get("interface_attachments") or [])
        if int(a.get("interface_index", -1)) == 2
    ]
    assert len(matching) == 1, (
        f"stale-gen detach must not drop the newer attachment; "
        f"interface_attachments={updated_rt.get('interface_attachments')!r}"
    )
    assert matching[0]["attach_generation"] == current_gen


def test_us304_stale_generation_locked_helper_returns_stale_noop(
    lab_settings, monkeypatch, _instance_id
):
    """Direct call into ``_detach_qemu_interface_locked`` with an
    ``expected_generation`` older than the runtime's current generation
    must short-circuit and return ``state='stale_noop'`` without
    touching QMP, host_net, or runtime state.

    Companion regression for HIGH-1 that exercises the locked helper
    contract independently of ``link_service.delete_link``.
    """
    lab_id = "lab304stalelck"
    _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _qemu_node(1)},
        networks={"5": _network(5)},
    )

    svc = NodeRuntimeService()
    runtime = _seed_attached_qemu(
        svc,
        lab_id=lab_id,
        node_id=1,
        interface_index=2,
        network_id=5,
        monkeypatch=monkeypatch,
    )
    runtime["interface_runtime"] = {
        "2": {"current_attach_generation": 5}
    }
    svc._persist_runtime(runtime)

    fake_qmp = _FakeQmp()
    svc._qmp_client = fake_qmp
    calls = _patch_host_net(monkeypatch)

    # Call the public entrypoint with stale gen.
    result = svc.detach_qemu_interface(
        lab_id, 1, 2, expected_generation=1
    )

    assert result["state"] == "stale_noop"
    assert result["expected_generation"] == 1
    assert result["current_attach_generation"] == 5

    # No QMP, no host_net, no runtime mutation.
    assert fake_qmp.calls == []
    assert calls["tap_del"] == []
    assert calls["link_set_nomaster"] == []

    updated_rt = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert updated_rt is not None
    assert len(updated_rt.get("interface_attachments") or []) == 1
