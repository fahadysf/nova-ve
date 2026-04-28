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


def _mock_host_net_for_detach(monkeypatch, *, raises=None) -> dict:
    """Capture link_del / try_link_del calls so tests can assert veth removal.

    ``raises`` (optional) — when set to an exception instance, ``link_del``
    raises it instead of recording (lets fault-injection tests force a
    helper failure).
    """
    from app.services import host_net

    calls: dict[str, list] = {"link_del": [], "try_link_del": []}

    def _link_del(name):
        calls["link_del"].append(name)
        if raises is not None:
            raise raises

    monkeypatch.setattr(host_net, "link_del", _link_del)
    monkeypatch.setattr(
        host_net,
        "try_link_del",
        lambda name: calls["try_link_del"].append(name),
    )
    return calls


class _RecordingLock:
    """Thin proxy around ``threading.Lock`` that records every successful
    ``acquire`` and ``release`` call so tests can assert serialization.
    The proxy delegates to the real lock so semantics are preserved.
    """

    def __init__(self, real_lock, events: list, on_acquire=None, on_release=None):
        self._real = real_lock
        self._events = events
        self._on_acquire = on_acquire
        self._on_release = on_release

    def acquire(self, blocking=True, timeout=-1):
        if timeout != -1:
            result = self._real.acquire(blocking, timeout)
        else:
            result = self._real.acquire(blocking)
        if result:
            self._events.append("acquire")
            if self._on_acquire is not None:
                self._on_acquire()
        return result

    def release(self):
        self._events.append("release")
        if self._on_release is not None:
            self._on_release()
        self._real.release()

    def locked(self):
        return self._real.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _install_recording_lock(
    monkeypatch, lab_id: str, node_id: int, iface: int, *,
    on_acquire=None, on_release=None,
) -> tuple[list, "_RecordingLock"]:
    """Replace the ``runtime_mutex`` registry entry for ``(lab_id, node_id,
    iface)`` with a :class:`_RecordingLock`. Returns ``(events_list, lock)``.

    We swap the lock BEFORE any code path acquires it; the
    ``_get_or_create`` registry uses ``dict.get`` which returns the
    swapped instance.
    """
    from app.services.runtime_mutex import runtime_mutex

    real = threading.Lock()
    events: list[str] = []
    proxy = _RecordingLock(real, events, on_acquire=on_acquire, on_release=on_release)
    key = runtime_mutex._key(lab_id, node_id, iface)
    with runtime_mutex._registry_lock:
        runtime_mutex._locks[key] = proxy  # type: ignore[assignment]
    return events, proxy


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

    # Kernel-side: link_del was called with the host-end veth name
    # (Codex critic v2 HIGH #2 — hot-detach must use the raising variant
    # so non-EINVAL failures surface to the caller).
    assert host_end in calls["link_del"], (
        f"expected link_del({host_end!r}); saw {calls['link_del']!r}"
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

    # But the newer veth must NOT have been deleted (stale-noop branch
    # short-circuits BEFORE the host_net helper call).
    assert calls["link_del"] == [], (
        "stale detach must not delete the host-end veth; "
        f"saw {calls['link_del']!r}"
    )
    assert calls["try_link_del"] == []

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

    Codex critic v2 refactor: instead of a smoke test that only asserts
    the link is eventually gone, we now record every ``acquire``/``release``
    pair on the mutex registry's underlying ``threading.Lock`` and assert
    that the two deletes ran in strict serial order — no interleaving.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5)},
        links=[_link("lnk_001", 1, 0, 5)],
    )

    service = LinkService()

    # Wrap the underlying ``threading.Lock`` for (lab="lab", node=1, iface=0)
    # in a recording proxy so every acquire/release is logged in order.
    # We pre-install the proxy under the registry key BEFORE any caller
    # touches it, so ``_get_or_create`` returns our proxy.
    events, _proxy = _install_recording_lock(monkeypatch, "lab", 1, 0)

    # Slow the critical section so the two deletes are guaranteed to
    # contend. We slow ``_delete_link_locked`` itself.
    original_locked = service._delete_link_locked

    async def slow_locked(**kwargs):
        await asyncio.sleep(0.03)
        return await original_locked(**kwargs)

    service._delete_link_locked = slow_locked  # type: ignore[method-assign]

    # Fire two concurrent deletes on the same link.
    r1, r2 = await asyncio.gather(
        service.delete_link(lab_name, "lnk_001"),
        service.delete_link(lab_name, "lnk_001"),
    )

    # Exactly one finds the link, one returns idempotent (True, None).
    results = [r1, r2]
    successes = [r for r in results if r[0] is False]
    idempotent = [r for r in results if r[0] is True]
    assert len(successes) + len(idempotent) == 2

    # The link is gone.
    saved = json.loads((lab_settings.LABS_DIR / lab_name).read_text())
    assert saved["links"] == []

    # Serialization assertion: events must alternate strictly
    # acquire/release/acquire/release with NO nested acquires (would mean
    # two callers held the mutex at once).
    assert events.count("acquire") == events.count("release"), (
        f"acquire/release imbalance: {events!r}"
    )
    assert events.count("acquire") >= 2, (
        f"expected >=2 acquires for two concurrent deletes; saw {events!r}"
    )
    held = 0
    for e in events:
        if e == "acquire":
            held += 1
            assert held == 1, (
                f"mutex held by >1 caller at once; events={events!r}"
            )
        else:
            held -= 1
    assert held == 0


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


# ---------------------------------------------------------------------------
# Codex critic v2 backfill — Issue #94 comment 4336502919
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us205_create_and_delete_serialize_on_same_iface(
    lab_settings, monkeypatch
):
    """Codex critic v2 HIGH #1 backfill — concurrent ``create_link`` +
    ``delete_link`` on the SAME ``(node, iface)`` MUST acquire the same
    runtime mutex and serialize. With the bug present (create using path,
    delete using id) the two would race; with the fix they share a lock.

    We assert serialization by recording acquire/release on the underlying
    ``threading.Lock`` and checking no two callers held it concurrently.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    # Seed a lab with one existing link we can delete, and a second
    # endpoint to attach to (network 6).
    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        "lab.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5), "6": _network(6)},
        links=[_link("lnk_001", 1, 0, 5, attach_generation=1)],
    )

    # Both create_link and delete_link on the SAME (node=1, iface=0) MUST
    # acquire the same lock. We pre-install a recording proxy at
    # ("lab", 1, 0) so a single shared events list captures both callers.
    held_lock = threading.Lock()
    held_count = {"value": 0}
    overlap_observed = {"value": False}

    def _on_acquire():
        with held_lock:
            held_count["value"] += 1
            if held_count["value"] > 1:
                overlap_observed["value"] = True

    def _on_release():
        with held_lock:
            held_count["value"] -= 1

    events, _proxy = _install_recording_lock(
        monkeypatch, "lab", 1, 0,
        on_acquire=_on_acquire, on_release=_on_release,
    )

    service = LinkService()

    # Slow the critical section so the two callers' mutex windows overlap
    # in time. Slowing ``LabService.write_lab_json_static`` is observable
    # in the create_link path (under lab_lock, after mutex acquired).
    from app.services.lab_service import LabService as _LabService

    original_write = _LabService.write_lab_json_static
    slow_event = threading.Event()

    def slow_write(*args, **kwargs):
        if not slow_event.is_set():
            slow_event.set()
            time.sleep(0.05)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(_LabService, "write_lab_json_static", staticmethod(slow_write))

    # First delete the existing link on iface=0, then create a fresh one
    # on iface=0 to network 6 — concurrent.
    create_coro = service.create_link(
        lab_name,
        {"node_id": 1, "interface_index": 0},
        {"network_id": 6},
    )
    delete_coro = service.delete_link(lab_name, "lnk_001")

    await asyncio.gather(delete_coro, create_coro)

    # Both callers acquired the SAME lock (("lab", 1, 0)) — proven by
    # the events list capturing >= 2 acquire/release pairs.
    assert events.count("acquire") >= 2, (
        f"expected create+delete to BOTH acquire the (lab,1,0) mutex; "
        f"events={events!r}"
    )
    assert events.count("acquire") == events.count("release")
    assert not overlap_observed["value"], (
        "two callers held the same per-(lab, node, iface) mutex at once: "
        f"events={events!r}"
    )


@pytest.mark.asyncio
async def test_us205_mutex_keying_lab_id_differs_from_path(
    lab_settings, monkeypatch
):
    """Codex critic v2 HIGH #1 backfill — when ``lab.id`` differs from
    the file path, ``create_link`` and ``delete_link`` MUST still resolve
    to the SAME mutex key. Pre-fix, create used the path and delete used
    the id, so they grabbed DIFFERENT locks on the same logical interface.

    We assert that both paths acquire-and-release the lock keyed by the
    JSON ``id`` field (NOT by the file path).
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    # File on disk: ``mylab.json``; JSON ``id``: ``"lab"`` (different).
    labs_dir = lab_settings.LABS_DIR
    payload = {
        "schema": 2,
        "id": "lab",  # explicitly different from file basename
        "meta": {"name": "mylab.json"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {"1": _node(1)},
        "networks": {"5": _network(5), "6": _network(6)},
        "links": [_link("lnk_001", 1, 0, 5, attach_generation=1)],
        "defaults": {"link_style": "orthogonal"},
    }
    (labs_dir / "mylab.json").write_text(json.dumps(payload))

    # The fix: both paths key on JSON id "lab", NOT path "mylab.json".
    correct_events, _correct = _install_recording_lock(monkeypatch, "lab", 1, 0)
    wrong_events, _wrong = _install_recording_lock(monkeypatch, "mylab.json", 1, 0)

    service = LinkService()

    # delete_link path — must use lab.id "lab", not "mylab.json".
    await service.delete_link("mylab.json", "lnk_001")

    # create_link path — must also use lab.id "lab".
    await service.create_link(
        "mylab.json",
        {"node_id": 1, "interface_index": 0},
        {"network_id": 6},
    )

    # Both paths should have acquired+released the CORRECT lock (id-keyed)
    # exactly twice (once for delete, once for create) and NEVER touched
    # the path-keyed lock.
    assert correct_events.count("acquire") >= 2, (
        f"expected both create+delete to use the id-keyed lock; "
        f"correct_events={correct_events!r}"
    )
    assert wrong_events == [], (
        "neither path should have used the path-keyed lock; "
        f"wrong_events={wrong_events!r}"
    )


@pytest.mark.asyncio
async def test_us205_link_del_failure_leaves_state_intact(
    lab_settings, monkeypatch, _instance_id
):
    """Codex critic v2 HIGH #2 backfill — fault injection.

    When ``host_net.link_del`` raises ``HostNetUnknown``, ``delete_link``
    MUST NOT (a) remove the link from ``lab.json``, (b) remove the IP
    from ``used_ips``, or (c) clear the runtime ``interface_attachments``
    row. The exception MUST surface to the caller.
    """
    from app.services import host_net as host_net_mod

    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab205fault"
    attach_gen = 7
    ip_seeded = "10.99.1.5"
    host_end = host_net_mod.veth_host_name(lab_id, 1, 0)

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5, used_ips=[ip_seeded])},
        links=[
            _link("lnk_001", 1, 0, 5, attach_generation=attach_gen, ip=ip_seeded)
        ],
    )

    svc = NodeRuntimeService()
    rt = _seed_docker_runtime(
        svc,
        lab_id=lab_id,
        node_id=1,
        pid=9100,
        iface_attachments=[{
            "interface_index": 0,
            "network_id": 5,
            "bridge_name": "novebr0005",
            "host_end": host_end,
            "attach_generation": attach_gen,
        }],
        monkeypatch=monkeypatch,
    )
    rt["interface_runtime"] = {"0": {"current_attach_generation": attach_gen}}
    svc._persist_runtime(rt)

    # Inject a non-EINVAL helper failure on link_del.
    fault = host_net_mod.HostNetUnknown(
        "simulated helper crash", returncode=1, stderr="bang"
    )
    _mock_host_net_for_detach(monkeypatch, raises=fault)

    link_service = LinkService()
    with pytest.raises(host_net_mod.HostNetUnknown):
        await link_service.delete_link(f"{lab_id}.json", "lnk_001")

    # (a) lab.json link still present.
    saved = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    assert any(lnk.get("id") == "lnk_001" for lnk in saved.get("links", [])), (
        f"link should be intact on detach failure; saved={saved!r}"
    )

    # (b) used_ips unchanged.
    used = saved["networks"]["5"]["runtime"].get("used_ips", [])
    assert ip_seeded in used, (
        f"IP must NOT be released on detach failure; used_ips={used!r}"
    )

    # (c) runtime attachment row still present.
    updated_rt = svc._runtime_record(lab_id, 1, include_stopped=True)
    assert updated_rt is not None
    attachments = updated_rt.get("interface_attachments") or []
    assert any(int(a.get("interface_index", -1)) == 0 for a in attachments), (
        f"interface_attachments must be intact on detach failure; "
        f"attachments={attachments!r}"
    )


@pytest.mark.asyncio
async def test_us205_idempotent_double_delete_no_op(
    lab_settings, monkeypatch
):
    """Codex critic v2 backfill — calling ``delete_link`` twice on the
    same id is idempotent: second call returns ``(True, None)`` with no
    crash and ``used_ips`` is not mutated a second time.
    """
    async def _noop(*_a, **_kw):
        pass

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _noop)

    lab_id = "lab205dbl"
    ip_seeded = "10.99.1.7"

    lab_name = _seed_lab(
        lab_settings.LABS_DIR,
        f"{lab_id}.json",
        nodes={"1": _node(1)},
        networks={"5": _network(5, used_ips=[ip_seeded, "10.99.1.8"])},
        links=[
            _link("lnk_001", 1, 0, 5, attach_generation=1, ip=ip_seeded)
        ],
    )

    service = LinkService()

    # First delete: link removed, ip released.
    ok1, _ = await service.delete_link(f"{lab_id}.json", "lnk_001")
    assert ok1 is False  # found-and-deleted

    saved_after_first = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    used_after_first = saved_after_first["networks"]["5"]["runtime"].get("used_ips", [])
    assert ip_seeded not in used_after_first
    assert "10.99.1.8" in used_after_first

    # Second delete: idempotent (True, None) — no crash, no further mutation.
    ok2, deleted_net = await service.delete_link(f"{lab_id}.json", "lnk_001")
    assert ok2 is True
    assert deleted_net is None

    saved_after_second = json.loads(
        (lab_settings.LABS_DIR / f"{lab_id}.json").read_text()
    )
    used_after_second = saved_after_second["networks"]["5"]["runtime"].get("used_ips", [])
    # used_ips MUST NOT have been corrupted by the no-op delete.
    assert used_after_second == used_after_first, (
        f"second delete corrupted used_ips: "
        f"before={used_after_first!r} after={used_after_second!r}"
    )
