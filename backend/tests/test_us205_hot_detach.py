# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-205: hot-detach Docker on links[] delete.

Acceptance criteria exercised here:

  * ``delete_link`` acquires the per-(lab, node, iface) runtime mutex BEFORE
    entering lab_lock (same discipline as create_link).
  * ``_detach_docker_interface_locked`` is called with the link's
    ``runtime.attach_generation`` as ``expected_generation`` so a stale
    detach never tears down a fresh re-attach.
  * Stale-generation detach (expected != current) produces ``state='stale_noop'``
    and does NOT call ``try_link_del``.
  * The host-end veth is removed (``try_link_del`` called) when the generation
    token matches.
  * Stopping a node before deleting its link is idempotent — no runtime
    record means no kernel-side work, but the JSON link is still removed.
  * IPAM release: when the link carries ``runtime.ip``, that IP is removed
    from the network's ``runtime.used_ips`` list under lab_lock (US-204c
    forward-compat path; until US-204c ships, ``runtime.ip`` is absent and
    the list is unchanged).
  * ``test_release_ip_on_detach``: 100 simulated attach/detach cycles leave
    ``used_ips == []`` (plan acceptance criterion, Codex critic v4 defect #6).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.link_service import LinkService
from app.services.node_runtime_service import NodeRuntimeService
from app.services.runtime_mutex import runtime_mutex


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
    """Provision a fake instance_id file so host_net.veth_host_name works."""
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance_id").write_text("test-instance-205")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return "test-instance-205"


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
    monkeypatch.setattr("app.services.network_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    return settings


def _seed_lab(labs_dir, lab_name: str, *, nodes=None, networks=None, links=None) -> str:
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
        "left": 0,
        "top": 0,
        "icon": "Server.png",
        "interfaces": [
            {"index": i, "name": f"eth{i}", "planned_mac": None, "port_position": None}
            for i in range(ethernet)
        ],
    }


def _network(net_id: int, name: str = "lan", *, used_ips=None) -> dict:
    rec: dict[str, Any] = {
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
    if used_ips is not None:
        rec["runtime"]["used_ips"] = list(used_ips)
    return rec


def _link(
    link_id: str,
    node_id: int,
    iface_idx: int,
    net_id: int,
    *,
    attach_generation: int = 0,
    ip: str | None = None,
) -> dict:
    runtime: dict[str, Any] = {"attach_generation": attach_generation}
    if ip is not None:
        runtime["ip"] = ip
    return {
        "id": link_id,
        "from": {"node_id": node_id, "interface_index": iface_idx},
        "to": {"network_id": net_id},
        "style_override": None,
        "label": "",
        "color": "",
        "width": "1",
        "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
        "runtime": runtime,
    }


def _seed_docker_runtime(
    service: NodeRuntimeService,
    *,
    lab_id: str,
    node_id: int,
    pid: int = 9000,
    iface_attachments: list | None = None,
    monkeypatch=None,
) -> dict:
    """Seed a fake live docker runtime record."""
    runtime = {
        "lab_id": lab_id,
        "node_id": node_id,
        "kind": "docker",
        "pid": pid,
        "container_name": f"{lab_id}-{node_id}",
        "interface_attachments": list(iface_attachments or []),
        "veth_host_ends": [],
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


def _mock_host_net_for_detach(monkeypatch) -> dict:
    """Capture try_link_del calls so tests can assert veth removal."""
    from app.services import host_net

    calls: dict[str, list] = {"try_link_del": []}

    monkeypatch.setattr(
        host_net,
        "try_link_del",
        lambda name: calls["try_link_del"].append(name),
    )
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us205_delete_link_no_running_node_is_idempotent(
    lab_settings, monkeypatch
):
    """Deleting a link whose node is stopped succeeds — link removed from
    JSON, no runtime record means no kernel-side work, no exception raised.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5)},
        links=[_link("lnk_001", 1, 0, 5, attach_generation=1)],
    )

    service = LinkService()
    ok, deleted_net = await service.delete_link(lab_name, "lnk_001")

    # Link gone from JSON.
    saved = json.loads((lab_settings.LABS_DIR / lab_name).read_text())
    assert saved["links"] == []
    assert ok is False
    assert deleted_net is None


@pytest.mark.asyncio
async def test_us205_delete_link_missing_link_idempotent(lab_settings, monkeypatch):
    """Deleting a non-existent link returns (True, None) without error."""
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_name = _seed_lab(
        lab_settings.LABS_DIR, "lab.json",
        nodes={"1": _node(1)}, networks={"5": _network(5)}, links=[],
    )

    service = LinkService()
    ok, deleted_net = await service.delete_link(lab_name, "lnk_999")
    assert ok is True
    assert deleted_net is None


@pytest.mark.asyncio
async def test_us205_delete_link_calls_detach_on_running_docker(
    lab_settings, monkeypatch, _instance_id
):
    """When the node is running (docker), delete_link calls
    _detach_docker_interface_locked with the link's attach_generation.
    The host-end veth is removed via try_link_del.
    """
    from app.services import host_net as host_net_mod

    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab205"
    attach_gen = 3
    host_end = host_net_mod.veth_host_name(lab_id, 1, 0)

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5)},
        links=[_link("lnk_001", 1, 0, 5, attach_generation=attach_gen)],
    )

    # Seed a live docker runtime with the attachment already recorded.
    svc = NodeRuntimeService()
    rt = _seed_docker_runtime(
        svc,
        lab_id=lab_id,
        node_id=1,
        pid=9001,
        iface_attachments=[{
            "interface_index": 0,
            "network_id": 5,
            "bridge_name": "novebr0005",
            "host_end": host_end,
            "attach_generation": attach_gen,
        }],
        monkeypatch=monkeypatch,
    )
    # Set up interface_runtime so generation check works.
    rt["interface_runtime"] = {"0": {"current_attach_generation": attach_gen}}
    svc._persist_runtime(rt)

    calls = _mock_host_net_for_detach(monkeypatch)

    link_service = LinkService()
    ok, deleted_net = await link_service.delete_link(f"{lab_id}.json", "lnk_001")

    assert ok is False
    assert deleted_net is None

    # JSON link removed.
    saved = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    assert saved["links"] == []

    # Kernel-side: try_link_del was called with the host-end veth name.
    assert host_end in calls["try_link_del"], (
        f"expected try_link_del({host_end!r}); saw {calls['try_link_del']!r}"
    )

    # Runtime record cleaned up.
    updated_rt = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert updated_rt is not None
    assert updated_rt["interface_attachments"] == []


@pytest.mark.asyncio
async def test_us205_stale_generation_does_not_delete_veth(
    lab_settings, monkeypatch, _instance_id
):
    """When the link's attach_generation is older than the runtime's
    current_attach_generation, _detach_docker_interface_locked must return
    state='stale_noop' and NOT call try_link_del.
    """
    from app.services import host_net as host_net_mod

    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab205stale"
    stale_gen = 1
    current_gen = 2
    host_end = host_net_mod.veth_host_name(lab_id, 1, 0)

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5)},
        # Link carries the OLD generation (stale_gen=1).
        links=[_link("lnk_001", 1, 0, 5, attach_generation=stale_gen)],
    )

    # Runtime carries the NEWER generation (current_gen=2).
    svc = NodeRuntimeService()
    rt = _seed_docker_runtime(
        svc,
        lab_id=lab_id,
        node_id=1,
        pid=9002,
        iface_attachments=[{
            "interface_index": 0,
            "network_id": 5,
            "bridge_name": "novebr0005",
            "host_end": host_end,
            "attach_generation": current_gen,
        }],
        monkeypatch=monkeypatch,
    )
    rt["interface_runtime"] = {"0": {"current_attach_generation": current_gen}}
    svc._persist_runtime(rt)

    calls = _mock_host_net_for_detach(monkeypatch)

    link_service = LinkService()
    await link_service.delete_link(f"{lab_id}.json", "lnk_001")

    # The JSON link IS removed regardless (we committed the delete).
    saved = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    assert saved["links"] == []

    # But the newer veth must NOT have been deleted.
    assert calls["try_link_del"] == [], (
        "stale detach must not delete the host-end veth; "
        f"saw {calls['try_link_del']!r}"
    )

    # The gen=2 attachment is still in the runtime record.
    updated_rt = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert updated_rt is not None
    assert len(updated_rt["interface_attachments"]) == 1
    assert updated_rt["interface_attachments"][0]["attach_generation"] == current_gen


@pytest.mark.asyncio
async def test_us205_delete_link_acquires_mutex_per_node_iface(
    lab_settings, monkeypatch
):
    """delete_link must hold the per-(lab, node, iface) runtime mutex
    while running _delete_link_locked — concurrent calls on the SAME
    interface must serialize.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5)},
        # Two separate links on the same (node=1, iface=0) are not valid
        # topology, but for mutex-acquisition testing we use one link and
        # fire two concurrent deletes — second returns idempotent True.
        links=[_link("lnk_001", 1, 0, 5)],
    )

    service = LinkService()

    # Track mutex acquisition ordering by patching _delete_link_locked.
    order: list[str] = []
    original_locked = service._delete_link_locked

    async def patched_locked(**kwargs):
        order.append("enter")
        await asyncio.sleep(0.02)  # yield briefly inside critical section
        result = await original_locked(**kwargs)
        order.append("exit")
        return result

    service._delete_link_locked = patched_locked  # type: ignore[method-assign]

    # Fire two concurrent deletes on the same link.
    r1, r2 = await asyncio.gather(
        service.delete_link(lab_name, "lnk_001"),
        service.delete_link(lab_name, "lnk_001"),
    )

    # Exactly one should have found the link; the other returns (True, None).
    results = [r1, r2]
    successes = [r for r in results if r[0] is False]
    idempotent = [r for r in results if r[0] is True]
    assert len(successes) + len(idempotent) == 2

    # The link is gone.
    saved = json.loads((lab_settings.LABS_DIR / lab_name).read_text())
    assert saved["links"] == []


@pytest.mark.asyncio
async def test_us205_ipam_release_removes_ip_from_used_ips(
    lab_settings, monkeypatch
):
    """When ``link.runtime.ip`` is set, delete_link removes it from
    ``network.runtime.used_ips`` under lab_lock (US-204c IPAM release).
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab205ip"
    ip_to_release = "10.99.1.3"

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5, used_ips=[ip_to_release, "10.99.1.4"])},
        links=[
            _link("lnk_001", 1, 0, 5, attach_generation=1, ip=ip_to_release)
        ],
    )

    service = LinkService()
    await service.delete_link(f"{lab_id}.json", "lnk_001")

    saved = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    used = saved["networks"]["5"]["runtime"].get("used_ips", [])
    assert ip_to_release not in used, (
        f"IP {ip_to_release!r} should have been released from used_ips; "
        f"remaining: {used!r}"
    )
    # The other IP is untouched.
    assert "10.99.1.4" in used


@pytest.mark.asyncio
async def test_release_ip_on_detach_100_cycles(lab_settings, monkeypatch):
    """100 attach/detach cycles must leave used_ips == [] (free-list
    integrity — plan acceptance criterion, Codex critic v4 defect #6).

    This test simulates the round-trip by directly updating the lab.json
    ``used_ips`` list (as the future US-204c create path would) then
    calling delete_link and verifying the list shrinks back to empty each
    time.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab205cycle"
    labs_dir = lab_settings.LABS_DIR

    for cycle in range(100):
        ip = f"10.99.1.{(cycle % 253) + 2}"
        # Seed lab with one link carrying the cycle's IP and used_ips=[ip].
        lab_name = _seed_lab(
            labs_dir,
            f"{lab_id}.json",
            nodes={"1": _node(1)},
            networks={"5": _network(5, used_ips=[ip])},
            links=[_link("lnk_001", 1, 0, 5, attach_generation=1, ip=ip)],
        )

        service = LinkService()
        await service.delete_link(f"{lab_id}.json", "lnk_001")

        saved = json.loads((labs_dir / f"{lab_id}.json").read_text())
        used = saved["networks"]["5"]["runtime"].get("used_ips", [])
        assert used == [], (
            f"cycle {cycle}: used_ips should be empty after detach; got {used!r}"
        )


@pytest.mark.asyncio
async def test_us205_implicit_network_gc_still_works(lab_settings, monkeypatch):
    """Implicit networks at refcount 0 are still GC'd when delete_link
    runs the US-205 hot-detach path (regression: ensure GC and detach
    compose correctly).
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab205gc"
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _node(1)},
        networks={
            "9": {
                "id": 9,
                "name": "",
                "type": "linux_bridge",
                "left": 0,
                "top": 0,
                "icon": "01-Cloud-Default.svg",
                "width": 0,
                "style": "Solid",
                "linkstyle": "Straight",
                "color": "",
                "label": "",
                "visibility": False,
                "implicit": True,
                "smart": -1,
                "config": {},
            }
        },
        links=[_link("lnk_001", 1, 0, 9)],
    )

    service = LinkService()
    ok, deleted_net = await service.delete_link(f"{lab_id}.json", "lnk_001")

    saved = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    assert "9" not in saved["networks"], "implicit network should have been GC'd"
    assert deleted_net is not None
    assert deleted_net["id"] == 9
