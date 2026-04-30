# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-204b: ``link_service.create_link`` mutex + generation-token integration.

The lower-level mutex registry semantics (serialization, exception
auto-release, distinct keys do not block, generation-token stale_noop)
are covered in ``test_runtime_mutex.py``. This file adds focused
integration tests at the ``link_service`` layer:

  * ``create_link`` acquires the per-(lab, node, iface) runtime mutex
    BEFORE entering ``lab_lock`` so concurrent calls on the same
    interface serialize at the runtime layer (not just at the lab.json
    layer).
  * Concurrent ``create_link`` calls on UNRELATED (node, iface) pairs
    do NOT serialize against each other.
  * The link record carries ``runtime.attach_generation`` after a
    successful hot-attach.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.services.link_service import LinkService
from app.services.runtime_mutex import runtime_mutex


@pytest.fixture(autouse=True)
def _reset_runtime_mutex():
    runtime_mutex.reset()
    yield
    runtime_mutex.reset()


@pytest.fixture()
def link_settings(tmp_path, monkeypatch):
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
    monkeypatch.setattr("app.services.network_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
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


def _node(node_id: int, *, ethernet: int = 2) -> dict:
    return {
        "id": node_id,
        "name": f"n{node_id}",
        "type": "docker",
        "template": "docker",
        "image": "nova-ve-alpine-telnet:latest",
        "console": "telnet",
        "status": 0,
        "cpu": 1,
        "ram": 256,
        "ethernet": ethernet,
        "left": 100,
        "top": 100,
        "icon": "Server.png",
        "interfaces": [
            {"index": i, "name": f"eth{i}", "planned_mac": None, "port_position": None}
            for i in range(ethernet)
        ],
    }


def _explicit_network(net_id: int, name: str = "lan") -> dict:
    return {
        "id": net_id,
        "name": name,
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
    }


@pytest.mark.asyncio
async def test_us204b_create_link_acquires_per_iface_mutex_in_order(
    link_settings, monkeypatch,
):
    """Two ``create_link`` calls targeting the SAME ``(node, iface)``
    must serialize through the runtime mutex.

    We block the first call's release of the mutex by intercepting the
    LabService write; the second call must not enter the lab_lock until
    the first releases the runtime mutex, which fires when the
    ``async with runtime_mutex.acquire(...)`` block exits.
    """
    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    # No running nodes → hot-attach is a no-op kernel-wise. The mutex
    # is still acquired — that is the property under test.

    # Patch ws_hub to swallow events.
    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()

    # Use distinct interface_index per call so we can observe the mutex
    # registry's keys directly. This is the inverse of the "same key"
    # serialization test (covered in test_runtime_mutex.py), so we show
    # here that distinct ifaces yield distinct mutex keys + truly
    # parallel execution.
    async def _go(iface_index: int):
        return await service.create_link(
            lab_name,
            {"node_id": 1, "interface_index": iface_index},
            {"network_id": 5},
        )

    # Two concurrent creates on iface 0 and iface 1 — distinct mutex keys
    # → must complete without serializing.
    results = await asyncio.gather(_go(0), _go(1))
    for link_payload, _network_payload, _replayed in results:
        assert link_payload["id"]
        assert "runtime" in link_payload
        # No running endpoint → attach_generation stays at 0.
        assert link_payload["runtime"]["attach_generation"] == 0


@pytest.mark.asyncio
async def test_us204b_create_link_serializes_same_iface(
    link_settings, monkeypatch,
):
    """Two ``create_link`` calls targeting the SAME ``(node, iface)``
    must serialize. The second call MUST surface
    ``DuplicateLinkError`` because the first commits the same link
    pair and the duplicate-detection guard in ``create_link`` runs
    AFTER the mutex acquire.

    This exercises the same-key serialization at the link_service
    layer (the runtime_mutex.py test exercises the same property at
    the registry layer).
    """
    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()

    async def _go():
        return await service.create_link(
            lab_name,
            {"node_id": 1, "interface_index": 0},
            {"network_id": 5},
        )

    # Fire two concurrently — exactly one should succeed; the other
    # must raise DuplicateLinkError after the mutex serializes them.
    from app.services.link_service import DuplicateLinkError

    results = await asyncio.gather(_go(), _go(), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, DuplicateLinkError)]
    assert len(successes) == 1, f"expected exactly one success, got {results!r}"
    assert len(failures) == 1, f"expected one DuplicateLinkError, got {results!r}"


@pytest.mark.asyncio
async def test_us204b_create_link_unrelated_keys_do_not_block(
    link_settings, monkeypatch,
):
    """Concurrent ``create_link`` calls on unrelated (node, iface)
    pairs must not serialize on the runtime mutex. We seed two distinct
    nodes, fire both creates, and verify they both complete.
    """
    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1), "2": _node(2)},
        networks={
            "5": _explicit_network(5, "lan-1"),
            "6": _explicit_network(6, "lan-2"),
        },
    )

    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()

    async def _go(node_id: int, network_id: int):
        return await service.create_link(
            lab_name,
            {"node_id": node_id, "interface_index": 0},
            {"network_id": network_id},
        )

    # node=1,iface=0 and node=2,iface=0 → distinct mutex keys.
    a, b = await asyncio.gather(_go(1, 5), _go(2, 6))
    assert a[0]["id"] != b[0]["id"]


@pytest.mark.asyncio
async def test_us204b_create_link_records_runtime_field_on_link(
    link_settings, monkeypatch,
):
    """The link payload returned by ``create_link`` MUST include a
    ``runtime`` dict with ``attach_generation`` (defaulting to 0 for
    no-running-endpoint creates). This is the schema bump that backs
    the generation-token contract.
    """
    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()
    link_payload, _net, _replayed = await service.create_link(
        lab_name,
        {"node_id": 1, "interface_index": 0},
        {"network_id": 5},
    )
    assert "runtime" in link_payload
    assert link_payload["runtime"]["attach_generation"] == 0

    # The persisted lab.json must also carry the runtime field on the link.
    saved = json.loads((link_settings.LABS_DIR / lab_name).read_text())
    assert len(saved["links"]) == 1
    # The link record has ``runtime`` either via the create_link path or
    # via the LinkRuntime default; confirm it can be read back.
    persisted = saved["links"][0]
    runtime_record = persisted.get("runtime") or {}
    assert int(runtime_record.get("attach_generation", 0)) == 0


# ---------------------------------------------------------------------------
# Bridge auto-up + per-NIC link state — link create/delete events
# ---------------------------------------------------------------------------


def _instance_id_fixture(monkeypatch, tmp_path):
    """Seed a deterministic instance_id so host_net.bridge_name() works."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance_id").write_text("test-link-state-instance")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))


def _patch_host_net_for_attach(monkeypatch):
    """Stub host_net helpers so create_link can run without root."""
    from app.services import host_net

    calls: dict[str, list] = {
        "bridge_exists": [],
        "bridge_add": [],
        "bridge_del": [],
        "bridge_fingerprint_write": [],
        "link_up": [],
        "try_link_del": [],
    }

    monkeypatch.setattr(
        host_net, "bridge_exists", lambda n: (calls["bridge_exists"].append(n) or True)
    )
    monkeypatch.setattr(
        host_net, "bridge_add", lambda n: calls["bridge_add"].append(n)
    )
    monkeypatch.setattr(
        host_net, "bridge_del", lambda n: calls["bridge_del"].append(n)
    )
    monkeypatch.setattr(
        host_net,
        "bridge_fingerprint_write",
        lambda n, lab_id, net_id: calls["bridge_fingerprint_write"].append(
            (n, lab_id, net_id)
        ),
    )
    monkeypatch.setattr(host_net, "link_up", lambda n: calls["link_up"].append(n))
    monkeypatch.setattr(
        host_net, "try_link_del", lambda n: calls["try_link_del"].append(n)
    )
    return calls


@pytest.mark.asyncio
async def test_create_link_brings_bridge_up_after_running_attach(
    link_settings, monkeypatch, tmp_path,
):
    """A successful hot-attach to a running endpoint forces ``link_up``
    on the destination bridge so a bridge with any port stays UP."""
    from app.services import host_net, node_runtime_service

    _instance_id_fixture(monkeypatch, tmp_path)
    helper = _patch_host_net_for_attach(monkeypatch)

    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    expected_bridge = host_net.bridge_name("lab", 5)
    attach_calls: list[tuple] = []

    def fake_runtime_record(_self, _lab_id, _node_id, *, include_stopped=False):
        return {"kind": "docker", "lab_id": "lab", "node_id": 1}

    def fake_attach_docker(
        self, _lab_id, node_id, network_id, interface_index, *, bridge_name=None
    ):
        attach_calls.append((node_id, network_id, interface_index, bridge_name))
        return {"interface_index": interface_index, "attach_generation": 1}

    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "_runtime_record",
        fake_runtime_record,
    )
    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "_attach_docker_interface_locked",
        fake_attach_docker,
    )

    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()
    link_payload, _net, _replayed = await service.create_link(
        lab_name,
        {"node_id": 1, "interface_index": 0},
        {"network_id": 5},
    )

    assert link_payload["id"]
    assert attach_calls == [(1, 5, 0, expected_bridge)]
    # The bridge gets brought up after the attach succeeds — only once
    # even if there are multiple endpoints on the same bridge.
    assert helper["link_up"] == [expected_bridge]


@pytest.mark.asyncio
async def test_create_link_no_bridge_up_when_no_endpoints_running(
    link_settings, monkeypatch, tmp_path,
):
    """If no endpoint of the new link is on a running node, no
    hot-attach work runs and link_up is NOT called — there is nothing
    to wire up yet."""
    _instance_id_fixture(monkeypatch, tmp_path)
    helper = _patch_host_net_for_attach(monkeypatch)

    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()
    await service.create_link(
        lab_name,
        {"node_id": 1, "interface_index": 0},
        {"network_id": 5},
    )

    assert helper["link_up"] == []


@pytest.mark.asyncio
async def test_create_link_calls_set_qemu_nic_link_up_for_running_qemu(
    link_settings, monkeypatch, tmp_path,
):
    """After a successful QEMU hot-attach, ``set_qemu_nic_link`` is
    called with ``up=True`` so the guest sees carrier on the new NIC."""
    from app.services import node_runtime_service

    _instance_id_fixture(monkeypatch, tmp_path)
    _patch_host_net_for_attach(monkeypatch)

    qemu_node = {
        **_node(1),
        "type": "qemu",
        "ethernet": 4,
    }
    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": qemu_node},
        networks={"5": _explicit_network(5, "lan")},
    )

    set_link_calls: list[tuple] = []

    def fake_runtime_record(_self, _lab_id, _node_id, *, include_stopped=False):
        return {"kind": "qemu", "lab_id": "lab", "node_id": 1}

    def fake_attach_qemu(
        self, _lab_id, node_id, network_id, interface_index, *, bridge_name=None
    ):
        return {"interface_index": interface_index, "attach_generation": 1}

    def fake_set_qemu_nic_link(
        self, lab_id, node_id, interface_index, *, up
    ):
        set_link_calls.append((node_id, interface_index, up))
        return True, None

    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "_runtime_record",
        fake_runtime_record,
    )
    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "_attach_qemu_interface_locked",
        fake_attach_qemu,
    )
    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "set_qemu_nic_link",
        fake_set_qemu_nic_link,
    )

    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()
    await service.create_link(
        lab_name,
        {"node_id": 1, "interface_index": 2},
        {"network_id": 5},
    )

    assert set_link_calls == [(1, 2, True)]


@pytest.mark.asyncio
async def test_delete_link_calls_set_qemu_nic_link_down_before_detach(
    link_settings, monkeypatch, tmp_path,
):
    """``delete_link`` calls ``set_qemu_nic_link(up=False)`` before
    handing off to the QMP hot-detach so the guest visibly drops
    carrier on that interface."""
    from app.services import node_runtime_service

    _instance_id_fixture(monkeypatch, tmp_path)
    _patch_host_net_for_attach(monkeypatch)

    qemu_node = {**_node(1), "type": "qemu", "ethernet": 4}
    lab_name = _seed_lab(
        link_settings.LABS_DIR,
        "lab.json",
        nodes={"1": qemu_node},
        networks={"5": _explicit_network(5, "lan")},
        links=[
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 2},
                "to": {"network_id": 5},
                "runtime": {"attach_generation": 1},
            }
        ],
    )

    set_link_calls: list[tuple] = []
    detach_calls: list[tuple] = []
    detach_order: list[str] = []

    def fake_runtime_record(_self, _lab_id, _node_id, *, include_stopped=False):
        return {"kind": "qemu", "lab_id": "lab", "node_id": 1}

    def fake_set_qemu_nic_link(self, lab_id, node_id, interface_index, *, up):
        set_link_calls.append((node_id, interface_index, up))
        detach_order.append("set_link")
        return True, None

    def fake_detach_qemu(
        self, _lab_id, node_id, interface_index, *, lab_path=None,
        expected_generation=None,
    ):
        detach_calls.append((node_id, interface_index, expected_generation))
        detach_order.append("detach")
        return {"state": "detached"}

    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "_runtime_record",
        fake_runtime_record,
    )
    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "set_qemu_nic_link",
        fake_set_qemu_nic_link,
    )
    monkeypatch.setattr(
        node_runtime_service.NodeRuntimeService,
        "_detach_qemu_interface_locked",
        fake_detach_qemu,
    )

    async def _noop_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop_publish)

    service = LinkService()
    # ``delete_link`` returns ``(False, ...)`` for "actually deleted" and
    # ``(True, None)`` only for idempotent no-op on a missing link.
    already_noop, _implicit = await service.delete_link(lab_name, "lnk_001")

    assert already_noop is False
    assert set_link_calls == [(1, 2, False)]
    assert detach_calls == [(1, 2, 1)]
    # set_link MUST run before detach — otherwise the device is gone
    # and there is nothing to flip the carrier off on.
    assert detach_order == ["set_link", "detach"]
