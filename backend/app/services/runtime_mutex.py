# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-204b: per-(lab_id, node_id, interface_index) runtime mutex registry.

Implements the Codex critic v5 finding #1 split-public/private locking
discipline. The per-lab :func:`lab_lock` is too coarse to serialize the
veth/IP work that runs *outside* the lab_lock window (US-204c moves
``nsenter ip addr add`` outside the lock); a fast detach can free / reuse
an IP while a prior add is still in flight on the same ``(node, iface)``.

The registry exposes a single mutex per ``(lab_id, node_id, interface_index)``
tuple. Hot-attach and hot-detach callers acquire the mutex BEFORE entering
``lab_lock`` so the kernel-side sequence — including any IPAM work — is
atomic with respect to other operations on the same interface. Unrelated
``(node, iface)`` pairs acquire distinct mutexes and never block each
other.

The lock is :class:`threading.Lock` rather than :class:`asyncio.Lock`
because the lock has to span both **synchronous** start-path callers
(``_start_docker_node``, ``_start_qemu_node`` are sync) and **async**
``link_service.create_link`` / ``delete_link`` callers, all sharing the
same lock instance. A bare :class:`asyncio.Lock` is bound to the loop it
was first acquired in and breaks for sync callers in a fresh loop. The
critical section is short enough that holding a thread-level lock across
``await`` boundaries does not measurably starve the event loop.

Locking discipline (split public / private — Codex v5 finding #1):

  * PUBLIC ``attach_docker_interface(...)`` / ``detach_docker_interface(...)``
    on :class:`NodeRuntimeService` acquire the mutex internally and then
    delegate to the corresponding private ``*_locked`` helper. Used by
    start-path callers that do NOT hold the mutex on entry.
  * PRIVATE ``_attach_docker_interface_locked(...)`` etc. assert at entry
    that the mutex IS held; they never acquire. Used by
    ``link_service.create_link`` / ``delete_link`` which acquire the
    mutex themselves before calling the private helper. This split
    eliminates the start-path-bypass while keeping ``link_service`` from
    double-acquiring (the deadlock case).

The registry is intentionally process-local — there is exactly one
backend process and the mutex protects in-process kernel sequencing only.
A restart drops every mutex (no leaked state to clean up).
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Dict, Tuple


_MutexKey = Tuple[str, int, int]


class RuntimeMutexRegistry:
    """Process-local registry of :class:`threading.Lock` keyed by
    ``(lab_id, node_id, interface_index)``.

    Locks are created lazily on first acquisition and never reaped — the
    set of in-flight ``(lab, node, iface)`` keys is bounded by the lab's
    declared topology (max 999 nodes per lab × 99 interfaces per node)
    and the cost of a stale lock is negligible.
    """

    def __init__(self) -> None:
        # Guards ``_locks`` itself.
        self._registry_lock = threading.Lock()
        self._locks: Dict[_MutexKey, threading.Lock] = {}

    def _key(self, lab_id: str, node_id: int, interface_index: int) -> _MutexKey:
        return (str(lab_id), int(node_id), int(interface_index))

    def _get_or_create(
        self, lab_id: str, node_id: int, interface_index: int
    ) -> threading.Lock:
        key = self._key(lab_id, node_id, interface_index)
        with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def acquire_sync(
        self, lab_id: str, node_id: int, interface_index: int
    ):
        """Synchronous mutex acquire. Use from sync callers
        (``_start_docker_node``, ``_start_qemu_node``, the public
        ``attach_*_interface`` / ``detach_*_interface`` entrypoints).
        """
        lock = self._get_or_create(lab_id, node_id, interface_index)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def acquire(
        self, lab_id: str, node_id: int, interface_index: int
    ):
        """Async mutex acquire. Use from async callers
        (``link_service.create_link`` / ``delete_link``). Internally
        delegates to the same :class:`threading.Lock` so async + sync
        callers serialize against each other.

        The ``threading.Lock.acquire(blocking=False)`` poll keeps the
        event loop responsive on contention; on success we return
        immediately without an executor hop.
        """
        import asyncio

        lock = self._get_or_create(lab_id, node_id, interface_index)
        # Fast path: try non-blocking first to avoid the executor hop on
        # the uncontended common case.
        if lock.acquire(blocking=False):
            try:
                yield
                return
            finally:
                lock.release()

        # Contended: hop to the default executor so blocking acquire does
        # not stall the event loop.
        await asyncio.get_running_loop().run_in_executor(None, lock.acquire)
        try:
            yield
        finally:
            lock.release()

    def is_held(self, lab_id: str, node_id: int, interface_index: int) -> bool:
        """Return ``True`` if the mutex for this key currently exists and
        is locked. Used by private ``*_locked`` helpers to assert the
        caller acquired the mutex (Codex v5 finding #1 defensive contract).

        Note: ``threading.Lock.locked()`` does NOT distinguish between
        "locked by current thread" and "locked by another thread"; the
        defensive contract here is "someone is in the critical section",
        which is sufficient because the only way to enter the section is
        via :meth:`acquire` / :meth:`acquire_sync`.
        """
        key = self._key(lab_id, node_id, interface_index)
        lock = self._locks.get(key)
        if lock is None:
            return False
        return lock.locked()

    def reset(self) -> None:
        """Drop every registered lock. Used by tests; never called in
        production.
        """
        with self._registry_lock:
            self._locks.clear()


# Module-level singleton mirrors the ``link_service`` / ``mac_registry``
# pattern: there is exactly one mutex registry per backend process.
runtime_mutex = RuntimeMutexRegistry()
