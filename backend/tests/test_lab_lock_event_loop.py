# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""#331: waiting for a lab lock must not block the event loop.

Worker threads hold the same locks for seconds at a time (a network patch,
the lab-open reconcile). While one of them holds the lock, an async route
that waits with the threaded ``lab_lock`` or ``nat_cloud_ipam_lock`` stalls
every other request until the lock is released or the timeout passes.

Each test holds the lock in a thread, starts the operation and checks that
the loop keeps a 10 ms timer on schedule while the operation is still
waiting. The host layer is stubbed throughout.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import host_net, network_info_service
from app.services.cloud_inventory_service import nat_cloud_ipam_lock
from app.services.lab_lock import lab_lock
from app.services.link_service import LinkService
from app.services.network_service import NetworkService

# A thread holds the lock until the measurement window ends, and only the
# test coroutine can release it. A wait that blocks the loop therefore lasts
# the whole lock timeout (seconds), far past the stall limit.
STALL_LIMIT_S = 0.2
WINDOW_S = 0.15

LAB = "lab.json"
LAB_ID = "lab-331"


@pytest.fixture()
def labs_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "labs"
    directory.mkdir()
    return directory


@pytest.fixture()
def settings(monkeypatch, labs_dir):
    value = SimpleNamespace(
        LABS_DIR=labs_dir,
        BASE_DATA_DIR=labs_dir.parent,
        TMP_DIR=labs_dir.parent / "tmp",
        NAT_CLOUD_POOL="10.255.0.0/16",
    )
    for module in (
        "network_service", "cloud_inventory_service", "lab_service", "link_service", "node_runtime_service",
    ):
        monkeypatch.setattr(f"app.services.{module}.get_settings", lambda: value)
    monkeypatch.setattr("app.config.get_settings", lambda: value)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: value)
    return value


@pytest.fixture()
def host(monkeypatch, tmp_path):
    """Fake kernel: every host call is recorded, nothing is spawned."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "instance_id").write_text("test-instance")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance))
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(tmp_path / "bridges"))
    state = SimpleNamespace(bridges=set(), calls=[])
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: name in state.bridges)
    monkeypatch.setattr(host_net, "bridge_add", lambda name: (state.bridges.add(name), state.calls.append(("bridge_add", name))) and None)
    monkeypatch.setattr(host_net, "bridge_del", lambda name: state.bridges.discard(name))
    for name in (
        "link_state", "bridge_addr_add", "bridge_addr_del", "link_up", "ipv4_forward_enable", "nat_remove",
        "forward_apply", "forward_remove", "dnsmasq_stop", "bridge_guard_apply", "bridge_guard_remove",
    ):
        monkeypatch.setattr(host_net, name, lambda *a, **k: state.calls.append((name,) + a))
    monkeypatch.setattr(host_net, "nat_apply", lambda bridge, cidr, egress: state.calls.append(("nat_apply", bridge)))
    monkeypatch.setattr(host_net, "default_egress_iface", lambda: "eth0")
    monkeypatch.setattr(host_net, "list_ipv4_addrs", lambda: [])
    monkeypatch.setattr(host_net, "dnsmasq_start", lambda *a: 4321)
    return state


@pytest.fixture()
def stub_publish(monkeypatch):
    async def publish(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish)


def _lab(labs_dir: Path, *, networks=None, links=None) -> None:
    node = {
        "id": 1, "name": "r1", "type": "qemu", "template": "linux", "image": "linux",
        "console": "telnet", "status": 0, "ethernet": 2,
        "interfaces": [{"index": 0, "name": "eth0", "planned_mac": None}],
    }
    (labs_dir / LAB).write_text(json.dumps({
        "schema": 2, "id": LAB_ID, "meta": {"name": "lab"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {"1": node}, "networks": networks or {}, "links": links or [],
        "defaults": {"link_style": "orthogonal"},
    }))


def _bridge(network_id: int = 1) -> dict:
    return {"id": network_id, "name": f"lan{network_id}", "type": "linux_bridge", "visibility": True}


def _nat_cloud() -> dict:
    return {
        "id": 1, "name": "inet", "type": "nat_cloud", "visibility": True,
        "config": {"cidr": "10.255.0.0/24", "gateway": "10.255.0.1", "dhcp": True,
                   "dhcp_start": "10.255.0.100", "dhcp_end": "10.255.0.254"},
        "runtime": {"bridge_name": "nove0000n1", "driver": "nat_cloud", "used_ips": ["10.255.0.2"]},
    }


def _link(*, with_ip: bool = False) -> dict:
    runtime = {"ip": "10.255.0.2", "attach_generation": 0} if with_ip else {}
    return {
        "id": "lnk_001",
        "from": {"node_id": 1, "interface_index": 0},
        "to": {"network_id": 1},
        "runtime": runtime,
    }


class HeldElsewhere:
    """A worker thread holds a lock until the test lets go."""

    def __init__(self, enter):
        self.entered = threading.Event()
        self._release = threading.Event()
        self._enter = enter
        self._thread = threading.Thread(target=self._hold, daemon=True)
        self._thread.start()
        assert self.entered.wait(5.0), "the holder thread never acquired the lock"

    def _hold(self) -> None:
        with self._enter():
            self.entered.set()
            self._release.wait(10.0)

    def let_go(self) -> None:
        self._release.set()
        self._thread.join(5.0)


async def _assert_waits_without_stalling(task) -> None:
    """The loop stays responsive while ``task`` is still waiting."""
    worst = 0.0
    deadline = time.monotonic() + WINDOW_S
    while time.monotonic() < deadline:
        due = time.monotonic() + 0.01
        await asyncio.sleep(0.01)
        worst = max(worst, time.monotonic() - due)
    assert worst < STALL_LIMIT_S, (
        f"the event loop was frozen for {worst * 1000:.0f} ms while the operation "
        "waited for a lock"
    )
    assert not task.done(), "the operation finished although another holder still had the lock"


async def _run_while_held(task, held) -> None:
    """Measure the loop while ``held`` keeps the lock, then let go."""
    try:
        await _assert_waits_without_stalling(task)
    finally:
        held.let_go()
        if task.done() and not task.cancelled():
            task.exception()  # never leave the failure unretrieved


async def _finish(task):
    return await asyncio.wait_for(task, 10.0)


def _lab_held(labs_dir: Path) -> HeldElsewhere:
    return HeldElsewhere(lambda: lab_lock(LAB, labs_dir))


def _subnets_held() -> HeldElsewhere:
    return HeldElsewhere(nat_cloud_ipam_lock)


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "network_request",
    [{"name": "new", "type": "linux_bridge"}, {"name": "inet", "type": "nat_cloud"}],
    ids=["bridge", "nat_cloud"],
)
async def test_create_network_waits_for_the_lab_lock_off_the_loop(
    settings, host, stub_publish, labs_dir, network_request
):
    _lab(labs_dir)
    svc = NetworkService()
    held = _lab_held(labs_dir)
    task = asyncio.create_task(svc.create_network(LAB, dict(network_request)))
    await _run_while_held(task, held)
    assert (await _finish(task))["name"] == network_request["name"]


@pytest.mark.asyncio
async def test_create_network_waits_for_the_subnet_lock_off_the_loop(settings, host, stub_publish, labs_dir):
    """A NAT-Cloud also takes the host-wide subnet lock before the lab lock."""
    _lab(labs_dir)
    svc = NetworkService()
    held = _subnets_held()
    task = asyncio.create_task(svc.create_network(LAB, {"name": "inet", "type": "nat_cloud"}))
    await _run_while_held(task, held)
    assert (await _finish(task))["type"] == "nat_cloud"


@pytest.mark.asyncio
@pytest.mark.parametrize("held_by", ["lab", "subnets"])
async def test_delete_network_waits_for_locks_off_the_loop(settings, host, stub_publish, labs_dir, held_by):
    _lab(labs_dir, networks={"1": _nat_cloud()})
    svc = NetworkService()
    held = _lab_held(labs_dir) if held_by == "lab" else _subnets_held()
    task = asyncio.create_task(svc.delete_network(LAB, 1))
    await _run_while_held(task, held)
    assert (await _finish(task))["id"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("held_by", ["lab", "subnets"])
async def test_set_sharing_waits_for_locks_off_the_loop(settings, host, stub_publish, labs_dir, held_by):
    _lab(labs_dir, networks={"1": _bridge()})
    svc = NetworkService()
    held = _lab_held(labs_dir) if held_by == "lab" else _subnets_held()
    task = asyncio.create_task(svc.set_sharing(LAB, 1, True))
    await _run_while_held(task, held)
    payload, changed = await _finish(task)
    assert payload["shared"] is True and changed is True


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_link_waits_for_the_lab_lock_off_the_loop(settings, host, stub_publish, labs_dir):
    _lab(labs_dir, networks={"1": _bridge()})
    svc = LinkService()
    held = _lab_held(labs_dir)
    task = asyncio.create_task(
        svc.create_link(LAB, {"node_id": 1, "interface_index": 0}, {"network_id": 1})
    )
    await _run_while_held(task, held)
    link, network, replayed = await _finish(task)
    assert link["to"] == {"network_id": 1} and not replayed


@pytest.mark.asyncio
@pytest.mark.parametrize("with_ip", [False, True], ids=["no_ip", "releases_ip"])
async def test_delete_link_waits_for_the_lab_lock_off_the_loop(settings, host, stub_publish, labs_dir, with_ip):
    """Deleting a link that holds a DHCP address releases it first, under its own locks."""
    _lab(labs_dir, networks={"1": _nat_cloud()}, links=[_link(with_ip=with_ip)])
    svc = LinkService()
    held = _lab_held(labs_dir)
    task = asyncio.create_task(svc.delete_link(LAB, "lnk_001"))
    await _run_while_held(task, held)
    already_deleted, deleted_network = await _finish(task)
    assert already_deleted is False and deleted_network is None


@pytest.mark.asyncio
async def test_delete_link_releases_the_ip_off_the_loop(settings, host, stub_publish, labs_dir):
    """The address release takes the host-wide subnet lock, off the loop."""
    _lab(labs_dir, networks={"1": _nat_cloud()}, links=[_link(with_ip=True)])
    svc = LinkService()
    held = _subnets_held()
    task = asyncio.create_task(svc.delete_link(LAB, "lnk_001"))
    await _run_while_held(task, held)
    assert await _finish(task) == (False, None)
    saved = json.loads((labs_dir / LAB).read_text())
    assert saved["networks"]["1"]["runtime"]["used_ips"] == []


@pytest.mark.asyncio
async def test_patch_link_waits_for_the_lab_lock_off_the_loop(settings, host, stub_publish, labs_dir):
    _lab(labs_dir, networks={"1": _bridge()}, links=[_link()])
    svc = LinkService()
    held = _lab_held(labs_dir)
    task = asyncio.create_task(svc.patch_link(LAB, "lnk_001", {"label": "uplink"}))
    await _run_while_held(task, held)
    assert (await _finish(task))["label"] == "uplink"


# ---------------------------------------------------------------------------
# Lab stop / wipe (management routes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "wipe"])
async def test_lab_stop_and_wipe_wait_for_the_lab_lock_off_the_loop(settings, host, labs_dir, monkeypatch, action):
    _lab(labs_dir)
    monkeypatch.setattr(network_info_service, "subprocess", SimpleNamespace(run=lambda *a, **k: None))
    from app.routers import manage

    admin = SimpleNamespace(username="admin", role="admin")
    route = manage.stop_lab if action == "stop" else manage.wipe_lab
    held = _lab_held(labs_dir)
    task = asyncio.create_task(route(LAB, admin))
    await _run_while_held(task, held)
    result = await _finish(task)
    assert result["code"] == 200, result


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "delete", "sharing"])
@pytest.mark.parametrize("cancel_children", [False, True])
async def test_cancelled_network_mutation_finishes_and_publishes(monkeypatch, operation, cancel_children):
    entered, release = threading.Event(), threading.Event()
    events = []
    svc = NetworkService()
    payload = {"id": 1}

    def worker(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return (payload, True) if operation == "sharing" else payload

    async def publish(*args):
        events.append(args)

    monkeypatch.setattr(svc, {"create": "_create_network_locked", "delete": "_delete_network_locked", "sharing": "_set_sharing_locked"}[operation], worker)
    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish)
    coroutines = {
        "create": lambda: svc.create_network(LAB, {}),
        "delete": lambda: svc.delete_network(LAB, 1),
        "sharing": lambda: svc.set_sharing(LAB, 1, True),
    }
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(coroutines[operation]())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for pending in (asyncio.all_tasks() - existing_tasks if cancel_children else {task}):
            pending.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(events) == 1
    assert events[0][0] == LAB
    assert events[0][1] == {"create": "network_created", "delete": "network_deleted", "sharing": "network_updated"}[operation]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "wipe"])
@pytest.mark.parametrize("cancel_children", [False, True])
async def test_cancelled_lab_action_finishes_and_audits(monkeypatch, action, cancel_children):
    from app.routers import manage

    entered, release = threading.Event(), threading.Event()
    audit = []

    def worker(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"code": 200, "data": {"lab": LAB, "name": LAB, "stopped": [1]}}

    async def record(event, **kwargs):
        audit.append(event)

    monkeypatch.setattr(manage, "_stop_or_wipe", worker)
    monkeypatch.setattr(manage.audit_service, "record", record)
    route = manage.stop_lab if action == "stop" else manage.wipe_lab
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(route(LAB, SimpleNamespace(username="admin", role="admin")))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for pending in (asyncio.all_tasks() - existing_tasks if cancel_children else {task}):
            pending.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert audit == [f"lab.{action}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_children", [False, True])
async def test_cancelled_borrowed_link_delete_keeps_outer_lock(settings, host, stub_publish, labs_dir, monkeypatch, cancel_children):
    from app.services.lab_lock import LabLockTimeout, lab_lock_async

    _lab(labs_dir, networks={"1": _nat_cloud()}, links=[_link(with_ip=True)])
    entered, release = threading.Event(), threading.Event()

    def release_ip(*args, **kwargs):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(NetworkService, "_release_ip", release_ip)

    async def delete_under_lock():
        async with lab_lock_async(LAB, labs_dir):
            await LinkService().delete_link(LAB, "lnk_001", _lab_lock_held=True)

    def competing_lock():
        with lab_lock(LAB, labs_dir, timeout_s=0.1):
            pass

    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(delete_under_lock())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for pending in (asyncio.all_tasks() - existing_tasks if cancel_children else {task}):
            pending.cancel()
        await asyncio.sleep(0.05)
        with pytest.raises(LabLockTimeout):
            await asyncio.to_thread(competing_lock)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert json.loads((labs_dir / LAB).read_text())["links"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["network", "audit"])
async def test_shutdown_during_notification_does_not_retry(monkeypatch, kind):
    from app.routers import manage

    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []
    started = []

    async def notification(*args, **kwargs):
        started.append(args[0])
        entered.set()
        await release.wait()
        delivered.append(args[0])

    if kind == "network":
        svc = NetworkService()
        monkeypatch.setattr(svc, "_create_network_locked", lambda *a, **k: {"id": 1})
        monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", notification)
        action = svc.create_network(LAB, {})
    else:
        monkeypatch.setattr(manage, "_stop_or_wipe", lambda *a, **k: {
            "code": 200, "data": {"lab": LAB, "name": LAB, "stopped": [1]}
        })
        monkeypatch.setattr(manage.audit_service, "record", notification)
        action = manage.stop_lab(LAB, SimpleNamespace(username="admin", role="admin"))

    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(action)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for pending in asyncio.all_tasks() - existing_tasks:
            pending.cancel()
        await asyncio.sleep(0.05)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started == [LAB if kind == "network" else "lab.stop"]
    assert delivered == []


@pytest.mark.asyncio
async def test_shutdown_during_link_delete_lock_wait_finishes_cleanup(settings, host, stub_publish, labs_dir, monkeypatch):
    _lab(labs_dir, networks={"1": _nat_cloud()}, links=[_link(with_ip=True)])
    entered, release_worker = threading.Event(), threading.Event()

    def release_ip(*args, **kwargs):
        entered.set()
        assert release_worker.wait(5)

    monkeypatch.setattr(NetworkService, "_release_ip", release_ip)
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(LinkService().delete_link(LAB, "lnk_001"))
    held = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        held = _lab_held(labs_dir)
        release_worker.set()
        await asyncio.sleep(0.1)
        for pending in asyncio.all_tasks() - existing_tasks:
            pending.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        release_worker.set()
        if held is not None:
            held.let_go()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert json.loads((labs_dir / LAB).read_text())["links"] == []


@pytest.mark.asyncio
async def test_notification_internal_timeout_is_not_suppressed(monkeypatch):
    svc = NetworkService()
    monkeypatch.setattr(svc, "_create_network_locked", lambda *a, **k: {"id": 1})

    async def publish(*args):
        async with asyncio.timeout(0.01):
            await asyncio.Event().wait()

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish)
    with pytest.raises(TimeoutError):
        await svc.create_network(LAB, {})


@pytest.mark.asyncio
async def test_cancelled_mutation_bounds_notification_wait(monkeypatch):
    from app.services import async_utils

    entered, release = threading.Event(), threading.Event()
    notification_cancelled = asyncio.Event()
    svc = NetworkService()

    def worker(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"id": 1}

    async def publish(*args):
        try:
            await asyncio.Event().wait()
        finally:
            notification_cancelled.set()

    monkeypatch.setattr(svc, "_create_network_locked", worker)
    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish)
    monkeypatch.setattr(async_utils, "CANCELLED_NOTIFICATION_TIMEOUT_S", 0.05)
    task = asyncio.create_task(svc.create_network(LAB, {}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert notification_cancelled.is_set()


@pytest.mark.asyncio
async def test_mutation_deadline_is_reported_after_worker_finishes(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    events = []
    svc = NetworkService()

    def worker(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"id": 1}

    async def publish(*args):
        events.append(args)

    async def action():
        async with asyncio.timeout(0.05):
            await svc.create_network(LAB, {})

    monkeypatch.setattr(svc, "_create_network_locked", worker)
    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish)
    task = asyncio.create_task(action())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(TimeoutError):
        await task
    assert len(events) == 1


@pytest.mark.asyncio
async def test_worker_failure_after_cancellation_is_reported_without_event(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    events = []
    svc = NetworkService()

    def worker(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("worker failed")

    async def publish(*args):
        events.append(args)

    monkeypatch.setattr(svc, "_create_network_locked", worker)
    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish)
    task = asyncio.create_task(svc.create_network(LAB, {}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="worker failed"):
        await task
    assert events == []
