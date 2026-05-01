# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-204b: per-(lab_id, node_id, interface_index) mutex + generation tokens.

Tests cover:
  * two parallel attaches on the same ``(node, iface)`` serialize via the
    mutex (the kernel-side helper sequence never interleaves);
  * detach with a stale ``expected_generation`` no-ops + logs (generation
    token freshness check);
  * unrelated ``(node, iface)`` pairs do not block each other (per-key
    locking is genuine, not a global lock);
  * the mutex auto-releases on exception inside the critical section;
  * the private ``_attach_docker_interface_locked`` defensive assertion
    fires when a caller bypasses the public API.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.node_runtime_service import (
    NodeRuntimeError,
    NodeRuntimeService,
)
from app.services.runtime_mutex import RuntimeMutexRegistry, runtime_mutex


# ---------------------------------------------------------------------------
# Local fixtures (kept minimal — re-using ``test_node_runtime.py`` heavy
# helper plumbing would couple this file to that test's evolution).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runtime_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


@pytest.fixture(autouse=True)
def _reset_runtime_mutex():
    runtime_mutex.reset()
    yield
    runtime_mutex.reset()


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
        "app.services.node_runtime_service.get_settings",
        lambda: runtime_settings,
    )
    return runtime_settings


@pytest.fixture()
def _instance_id(monkeypatch, tmp_path):
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance_id").write_text("test-instance-204b")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return "test-instance-204b"


def _seed_runtime(
    service: NodeRuntimeService,
    *,
    lab_id: str,
    node_id: int,
    pid: int = 7000,
    monkeypatch=None,
) -> dict[str, Any]:
    """Seed a fake docker runtime record so attach helpers find a target.

    ``attach_docker_interface`` calls ``_runtime_record(include_stopped=False)``
    which routes through ``_is_runtime_alive`` → ``_is_docker_running`` →
    ``docker inspect``. The caller MUST also patch
    ``_is_runtime_alive`` (we do that here when ``monkeypatch`` is given)
    so the seeded runtime survives the liveness gate without spawning a
    real ``docker`` subprocess.
    """
    runtime = {
        "lab_id": lab_id,
        "node_id": node_id,
        "kind": "docker",
        "pid": pid,
        "container_name": f"{lab_id}-{node_id}",
        "interface_attachments": [],
        "veth_host_ends": [],
        "started_at": time.time(),
    }
    key = service._key(lab_id, node_id)
    with service._lock:
        service._registry[key] = runtime
    service._persist_runtime(runtime)
    if monkeypatch is not None:
        # Patch ``_is_runtime_alive`` on the service class so every subsequent
        # ``_runtime_record(include_stopped=False)`` call (from attach /
        # detach) treats the seeded record as live.
        monkeypatch.setattr(
            NodeRuntimeService,
            "_is_runtime_alive",
            lambda _self, _runtime: True,
        )
    return runtime


def _mock_host_net(monkeypatch, *, present_bridges: set[str], hold_event=None):
    """Capture every privileged-helper call.

    ``hold_event`` (optional) is a :class:`threading.Event` that, if set,
    causes ``veth_pair_add`` to block until the event fires — used by the
    serialization test to prove that a second attach on the same
    ``(node, iface)`` waits behind the first.
    """
    from app.services import host_net

    calls: dict[str, list] = {
        "bridge_exists": [],
        "veth_pair_add": [],
        "link_master": [],
        "link_up": [],
        "link_netns": [],
        "link_set_name_in_netns": [],
        "addr_up_in_netns": [],
        "link_del": [],
    }

    monkeypatch.setattr(
        host_net,
        "bridge_exists",
        lambda name: (calls["bridge_exists"].append(name), name in present_bridges)[1],
    )

    def fake_veth_pair_add(host_end: str, peer_end: str) -> None:
        calls["veth_pair_add"].append((host_end, peer_end, time.monotonic()))
        if hold_event is not None:
            hold_event.wait(timeout=10.0)

    monkeypatch.setattr(host_net, "veth_pair_add", fake_veth_pair_add)
    monkeypatch.setattr(
        host_net,
        "link_master",
        lambda iface, bridge: calls["link_master"].append((iface, bridge, time.monotonic())),
    )
    monkeypatch.setattr(
        host_net,
        "link_up",
        lambda iface: calls["link_up"].append((iface, time.monotonic())),
    )
    monkeypatch.setattr(
        host_net,
        "link_netns",
        lambda iface, pid: calls["link_netns"].append((iface, pid, time.monotonic())),
    )
    monkeypatch.setattr(
        host_net,
        "link_set_name_in_netns",
        lambda pid, oldname, newname: calls["link_set_name_in_netns"].append(
            (pid, oldname, newname, time.monotonic())
        ),
    )
    monkeypatch.setattr(
        host_net,
        "addr_up_in_netns",
        lambda pid, iface: calls["addr_up_in_netns"].append(
            (pid, iface, time.monotonic())
        ),
    )
    monkeypatch.setattr(
        host_net,
        "link_del",
        lambda name: calls["link_del"].append({"fn": "link_del", "name": name}),
    )
    monkeypatch.setattr(
        host_net,
        "try_link_del",
        lambda name: calls["link_del"].append({"fn": "try_link_del", "name": name}),
    )
    return calls


# ---------------------------------------------------------------------------
# RuntimeMutexRegistry unit tests
# ---------------------------------------------------------------------------


def test_runtime_mutex_distinct_keys_do_not_block():
    """Two acquires on different ``(lab, node, iface)`` keys must NOT
    serialize against each other.
    """
    registry = RuntimeMutexRegistry()
    held: list[tuple[str, int, int]] = []

    def acquire_and_hold(key, ready, release):
        with registry.acquire_sync(*key):
            held.append(key)
            ready.set()
            release.wait(timeout=5.0)

    ready_a = threading.Event()
    ready_b = threading.Event()
    release = threading.Event()
    t_a = threading.Thread(
        target=acquire_and_hold,
        args=(("lab", 1, 0), ready_a, release),
    )
    t_b = threading.Thread(
        target=acquire_and_hold,
        args=(("lab", 1, 1), ready_b, release),
    )
    t_a.start()
    t_b.start()

    # Both must reach the held state without one blocking the other.
    assert ready_a.wait(timeout=2.0), "thread A never acquired its lock"
    assert ready_b.wait(timeout=2.0), "thread B never acquired its lock"

    release.set()
    t_a.join(timeout=2.0)
    t_b.join(timeout=2.0)
    assert sorted(held) == sorted([("lab", 1, 0), ("lab", 1, 1)])


def test_runtime_mutex_same_key_serializes():
    """Two acquires on the SAME ``(lab, node, iface)`` key MUST serialize:
    the second only enters the critical section after the first releases.
    """
    registry = RuntimeMutexRegistry()
    enter_log: list[str] = []
    exit_log: list[str] = []
    a_in = threading.Event()
    a_release = threading.Event()
    b_finished = threading.Event()

    def thread_a():
        with registry.acquire_sync("lab", 1, 0):
            enter_log.append("A")
            a_in.set()
            a_release.wait(timeout=5.0)
            exit_log.append("A")

    def thread_b():
        # Wait until A is inside its critical section before trying.
        a_in.wait(timeout=2.0)
        with registry.acquire_sync("lab", 1, 0):
            enter_log.append("B")
            exit_log.append("B")
        b_finished.set()

    t_a = threading.Thread(target=thread_a)
    t_b = threading.Thread(target=thread_b)
    t_a.start()
    t_b.start()

    # B must be blocked on the lock while A holds it — give B a moment
    # to attempt the acquire; it should not yet have entered.
    assert a_in.wait(timeout=2.0)
    time.sleep(0.05)
    assert enter_log == ["A"], f"B entered too early: {enter_log!r}"

    a_release.set()
    assert b_finished.wait(timeout=2.0)
    t_a.join(timeout=2.0)
    t_b.join(timeout=2.0)

    assert enter_log == ["A", "B"]
    assert exit_log == ["A", "B"]


def test_runtime_mutex_releases_on_exception():
    """The mutex MUST auto-release if the body raises, otherwise a single
    bug strands the lock forever.
    """
    registry = RuntimeMutexRegistry()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with registry.acquire_sync("lab", 1, 0):
            raise _Boom()

    # A second acquire on the same key must succeed immediately.
    acquired = threading.Event()

    def acquire_again():
        with registry.acquire_sync("lab", 1, 0):
            acquired.set()

    t = threading.Thread(target=acquire_again)
    t.start()
    assert acquired.wait(timeout=2.0), "lock was not released after exception"
    t.join(timeout=2.0)


def test_runtime_mutex_is_held_reports_correctly():
    """``is_held`` must be ``False`` before acquire, ``True`` while held,
    and ``False`` again after release. Used by the defensive contract
    assertion in the private ``*_locked`` helpers.
    """
    registry = RuntimeMutexRegistry()
    assert registry.is_held("lab", 1, 0) is False
    with registry.acquire_sync("lab", 1, 0):
        assert registry.is_held("lab", 1, 0) is True
        # Other keys still report not held.
        assert registry.is_held("lab", 1, 1) is False
    assert registry.is_held("lab", 1, 0) is False


@pytest.mark.asyncio
async def test_runtime_mutex_async_and_sync_share_lock_instance():
    """Async ``acquire`` and sync ``acquire_sync`` MUST resolve to the
    same underlying lock instance so async + sync callers serialize
    against each other.
    """
    registry = RuntimeMutexRegistry()

    async with registry.acquire("lab", 1, 0):
        assert registry.is_held("lab", 1, 0) is True
        # A sync caller in another thread must NOT be able to enter the
        # critical section while the async holder owns the lock.
        sync_entered = threading.Event()

        def try_sync():
            with registry.acquire_sync("lab", 1, 0):
                sync_entered.set()

        t = threading.Thread(target=try_sync, daemon=True)
        t.start()
        # Give the sync thread a moment to attempt the acquire.
        await asyncio.sleep(0.05)
        assert not sync_entered.is_set(), "sync acquire entered while async held the lock"

    # After releasing the async lock, the sync thread must complete.
    deadline = time.monotonic() + 2.0
    while not sync_entered.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert sync_entered.is_set(), "sync acquire never completed after async release"


# ---------------------------------------------------------------------------
# NodeRuntimeService integration: parallel attach serializes
# ---------------------------------------------------------------------------


def test_us204b_parallel_attach_same_iface_serializes(
    monkeypatch, patched_settings, _instance_id
):
    """Two simultaneous ``attach_docker_interface`` calls on the SAME
    ``(node, iface)`` MUST serialize via the per-(lab, node, iface)
    mutex so that the kernel-side 6-step sequence does not interleave.
    """
    from app.services import host_net as host_net_module

    bridge = host_net_module.bridge_name("lab-204b", 1)
    hold = threading.Event()
    calls = _mock_host_net(monkeypatch, present_bridges={bridge}, hold_event=hold)

    service = NodeRuntimeService()
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000, monkeypatch=monkeypatch)

    # Track the order in which each thread enters the critical section.
    enter_order: list[str] = []
    enter_lock = threading.Lock()

    def _attach(label: str, iface_index: int):
        with enter_lock:
            enter_order.append(f"{label}:start")
        try:
            service.attach_docker_interface(
                "lab-204b", 1, network_id=1, interface_index=iface_index
            )
        except NodeRuntimeError as exc:
            with enter_lock:
                enter_order.append(f"{label}:err:{exc}")
            return
        with enter_lock:
            enter_order.append(f"{label}:done")

    # Both threads target ``(node=1, iface=0)`` — but the seeded runtime
    # already has no attachment for iface 0, so the first to acquire the
    # mutex wins; the second hits the duplicate-iface check after the
    # first commits and surfaces ``NodeRuntimeError("...already attached...")``.
    t_a = threading.Thread(target=_attach, args=("A", 0))
    t_b = threading.Thread(target=_attach, args=("B", 0))
    t_a.start()
    # Give A a head-start so it acquires the mutex first.
    time.sleep(0.05)
    t_b.start()

    # Wait briefly: both threads should reach ``start``; only A is in the
    # held veth_pair_add path. B must be blocked on the mutex (NOT in
    # veth_pair_add), so ``len(calls["veth_pair_add"])`` is exactly 1.
    deadline = time.monotonic() + 1.0
    while len(calls["veth_pair_add"]) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls["veth_pair_add"]) == 1, (
        "expected exactly one veth_pair_add in flight while the mutex serializes "
        f"the second call; saw {len(calls['veth_pair_add'])}"
    )

    # Release A. Now B's mutex acquire should unblock — but B will then
    # discover the duplicate-iface check and return idempotent success
    # (same network_id) without doing any further kernel work.
    hold.set()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)

    # A succeeded; B also succeeded idempotently (same-network re-attach).
    a_done = any(entry == "A:done" for entry in enter_order)
    b_done = any(entry == "B:done" for entry in enter_order)
    assert a_done, f"thread A did not complete: {enter_order!r}"
    assert b_done, f"thread B did not complete idempotently: {enter_order!r}"

    # The duplicate-iface check ran AFTER the mutex acquire, so exactly one
    # 6-step sequence happened.
    assert len(calls["veth_pair_add"]) == 1
    assert len(calls["link_master"]) == 1
    assert len(calls["addr_up_in_netns"]) == 1


def test_us204b_parallel_attach_distinct_ifaces_do_not_block(
    monkeypatch, patched_settings, _instance_id
):
    """Two simultaneous attaches on DIFFERENT ``(node, iface)`` pairs
    must run concurrently — the per-key mutex must NOT collapse to a
    global lock.
    """
    from app.services import host_net as host_net_module

    bridge_one = host_net_module.bridge_name("lab-204b", 1)
    bridge_two = host_net_module.bridge_name("lab-204b", 2)
    hold = threading.Event()
    calls = _mock_host_net(
        monkeypatch,
        present_bridges={bridge_one, bridge_two},
        hold_event=hold,
    )

    service = NodeRuntimeService()
    # Two distinct nodes so each attach targets its own (node, iface) key.
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000)
    _seed_runtime(service, lab_id="lab-204b", node_id=2, pid=7001, monkeypatch=monkeypatch)

    def _attach(node_id: int, network_id: int):
        service.attach_docker_interface(
            "lab-204b", node_id, network_id=network_id, interface_index=0
        )

    t_a = threading.Thread(target=_attach, args=(1, 1))
    t_b = threading.Thread(target=_attach, args=(2, 2))
    t_a.start()
    t_b.start()

    # Both threads should reach ``veth_pair_add`` concurrently — if the
    # mutex were over-broad (e.g. lab-wide), B would block until A
    # finishes.
    deadline = time.monotonic() + 1.0
    while len(calls["veth_pair_add"]) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls["veth_pair_add"]) == 2, (
        f"distinct (node, iface) pairs serialized; both should run "
        f"concurrently. veth_pair_add count={len(calls['veth_pair_add'])}"
    )

    hold.set()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)


def test_us204b_locked_helper_asserts_mutex_held(
    monkeypatch, patched_settings, _instance_id
):
    """The private ``_attach_docker_interface_locked`` MUST refuse to run
    if the per-(lab, node, iface) mutex is not held — defensive contract
    against start-path bypass.
    """
    from app.services import host_net as host_net_module

    bridge = host_net_module.bridge_name("lab-204b", 1)
    _mock_host_net(monkeypatch, present_bridges={bridge})

    service = NodeRuntimeService()
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000, monkeypatch=monkeypatch)

    with pytest.raises(AssertionError) as exc_info:
        service._attach_docker_interface_locked(
            "lab-204b", 1, 1, 0, bridge_name=bridge,
        )
    assert "mutex held" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Generation-token semantics
# ---------------------------------------------------------------------------


def test_us204b_attach_increments_generation(
    monkeypatch, patched_settings, _instance_id
):
    """Each successful attach to ``(node, iface)`` MUST bump
    ``current_attach_generation`` by 1.
    """
    from app.services import host_net as host_net_module

    bridge = host_net_module.bridge_name("lab-204b", 1)
    _mock_host_net(monkeypatch, present_bridges={bridge})

    service = NodeRuntimeService()
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000, monkeypatch=monkeypatch)

    # First attach.
    a1 = service.attach_docker_interface(
        "lab-204b", 1, network_id=1, interface_index=0
    )
    assert a1["attach_generation"] == 1

    # Detach so iface 0 is free again, then re-attach.
    service.detach_docker_interface(
        "lab-204b", 1, interface_index=0,
        expected_generation=1,
    )
    a2 = service.attach_docker_interface(
        "lab-204b", 1, network_id=1, interface_index=0
    )
    assert a2["attach_generation"] == 2


def test_us204b_detach_with_stale_generation_no_ops(
    monkeypatch, patched_settings, _instance_id
):
    """A detach with ``expected_generation`` LESS than the runtime's
    current value MUST log + no-op (a newer attach has happened).
    The kernel-side veth deletion must NOT fire.
    """
    from app.services import host_net as host_net_module

    bridge = host_net_module.bridge_name("lab-204b", 1)
    calls = _mock_host_net(monkeypatch, present_bridges={bridge})

    service = NodeRuntimeService()
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000, monkeypatch=monkeypatch)

    # First attach (gen=1), detach (gen now still 1, attachment removed),
    # second attach (gen=2). A stale detach captured at gen=1 must NOT
    # undo the gen=2 attach.
    service.attach_docker_interface(
        "lab-204b", 1, network_id=1, interface_index=0
    )
    service.detach_docker_interface(
        "lab-204b", 1, interface_index=0, expected_generation=1
    )
    service.attach_docker_interface(
        "lab-204b", 1, network_id=1, interface_index=0
    )

    # Reset link_del recording so we observe ONLY the stale-detach branch.
    calls["link_del"].clear()

    result = service.detach_docker_interface(
        "lab-204b", 1, interface_index=0, expected_generation=1
    )

    assert result["state"] == "stale_noop"
    assert result["expected_generation"] == 1
    assert result["current_attach_generation"] == 2
    # No kernel-side deletion fired.
    assert calls["link_del"] == [], (
        "stale detach must not delete the host-end veth; saw "
        f"{calls['link_del']!r}"
    )

    # The runtime record still carries the live gen=2 attachment.
    runtime = service._runtime_record("lab-204b", 1)
    assert runtime is not None
    assert len(runtime["interface_attachments"]) == 1
    assert runtime["interface_attachments"][0]["attach_generation"] == 2


def test_us204b_detach_with_matching_generation_proceeds(
    monkeypatch, patched_settings, _instance_id
):
    """A detach with ``expected_generation`` EQUAL to the current
    interface generation MUST proceed: kernel-side veth removed and the
    attachment dropped from the runtime record.
    """
    from app.services import host_net as host_net_module

    bridge = host_net_module.bridge_name("lab-204b", 1)
    calls = _mock_host_net(monkeypatch, present_bridges={bridge})

    service = NodeRuntimeService()
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000, monkeypatch=monkeypatch)

    attachment = service.attach_docker_interface(
        "lab-204b", 1, network_id=1, interface_index=0
    )
    expected_host_end = host_net_module.veth_host_name("lab-204b", 1, 0)

    result = service.detach_docker_interface(
        "lab-204b", 1, interface_index=0,
        expected_generation=attachment["attach_generation"],
    )
    assert result["state"] == "detached"
    assert result["host_end"] == expected_host_end

    # Kernel-side: link_del was called with the host_end name (Codex
    # critic v2 HIGH #2 — hot-detach now uses the raising variant so
    # non-EINVAL helper failures propagate to the caller).
    deleted_names = {
        entry["name"] for entry in calls["link_del"]
        if entry["fn"] == "link_del"
    }
    assert expected_host_end in deleted_names

    # Runtime record cleaned up.
    runtime = service._runtime_record("lab-204b", 1)
    assert runtime is not None
    assert runtime["interface_attachments"] == []
    assert expected_host_end not in (runtime.get("veth_host_ends") or [])


def test_us204b_detach_without_expected_generation_proceeds(
    monkeypatch, patched_settings, _instance_id
):
    """When ``expected_generation`` is None (e.g. an admin-driven
    detach that doesn't carry a link's generation token), the detach
    proceeds unconditionally — the gen-token check is opt-in.
    """
    from app.services import host_net as host_net_module

    bridge = host_net_module.bridge_name("lab-204b", 1)
    _mock_host_net(monkeypatch, present_bridges={bridge})

    service = NodeRuntimeService()
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000, monkeypatch=monkeypatch)

    service.attach_docker_interface(
        "lab-204b", 1, network_id=1, interface_index=0
    )
    result = service.detach_docker_interface(
        "lab-204b", 1, interface_index=0
    )
    assert result["state"] == "detached"


def test_us204b_detach_absent_attachment_is_idempotent(
    monkeypatch, patched_settings, _instance_id
):
    """Detaching an iface that has no attachment record returns
    ``state='absent'`` without raising — idempotent on duplicate calls.
    """
    from app.services import host_net as host_net_module

    bridge = host_net_module.bridge_name("lab-204b", 1)
    _mock_host_net(monkeypatch, present_bridges={bridge})

    service = NodeRuntimeService()
    _seed_runtime(service, lab_id="lab-204b", node_id=1, pid=7000, monkeypatch=monkeypatch)

    result = service.detach_docker_interface(
        "lab-204b", 1, interface_index=0
    )
    assert result["state"] == "absent"
