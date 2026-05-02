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

Issue #175: the per-(lab, node) "node-scoped" lock is :class:`threading.RLock`
(re-entrant) so the existing inner ``acquire_node_sync`` used by
``_attach_qemu_interface_locked`` for PCIe slot allocation can nest safely
inside an outer ``acquire_node_sync`` taken by the same thread for the
full attach / detach / start / reconcile body. The per-(lab, node, iface)
mutex (``_locks``) stays a plain :class:`threading.Lock` because per-iface
re-entrance is not needed and keeping it non-reentrant lets the
``is_held`` defensive contract catch accidental double-acquire bugs.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Dict, Tuple


_MutexKey = Tuple[str, int, int]
_NodeKey = Tuple[str, int]


# US-303 codex iter1 MEDIUM: bounded contention window.
DEFAULT_ACQUIRE_TIMEOUT_S = 2.0


class RuntimeMutexContention(Exception):
    """Raised when :meth:`RuntimeMutexRegistry.acquire` /
    :meth:`acquire_sync` cannot grab the mutex within the bounded
    timeout (default 2.0s).

    Translated to HTTP 409 by ``link_service.create_link`` /
    ``delete_link`` per the US-303 spec at
    ``.omc/plans/network-runtime-wiring.md``.
    """

    def __init__(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        timeout: float,
    ) -> None:
        self.lab_id = str(lab_id)
        self.node_id = int(node_id)
        self.interface_index = int(interface_index)
        self.timeout = float(timeout)
        super().__init__(
            f"Hot-attach already in progress on (node={self.node_id}, "
            f"iface={self.interface_index}); timed out after "
            f"{self.timeout:.1f}s"
        )


class RuntimeMutexRegistry:
    """Process-local registry of :class:`threading.Lock` keyed by
    ``(lab_id, node_id, interface_index)``.

    Locks are created lazily on first acquisition and never reaped — the
    set of in-flight ``(lab, node, iface)`` keys is bounded by the lab's
    declared topology (max 999 nodes per lab × 99 interfaces per node)
    and the cost of a stale lock is negligible.

    US-303 codex iter1 HIGH-2: also exposes a per-``(lab_id, node_id)``
    "node-scoped" lock for serializing PCIe slot allocation
    (``query-pci`` → ``device_add`` window). Without this, two concurrent
    attaches to *different* interfaces on the same VM can both see the
    same free ``rpN`` slot and race. The per-iface mutex is still
    required for the US-204b delete-vs-attach contract.
    """

    def __init__(self) -> None:
        # Guards ``_locks`` / ``_node_locks`` themselves.
        self._registry_lock = threading.Lock()
        self._locks: Dict[_MutexKey, threading.Lock] = {}
        self._node_locks: Dict[_NodeKey, threading.RLock] = {}

    def _key(self, lab_id: str, node_id: int, interface_index: int) -> _MutexKey:
        return (str(lab_id), int(node_id), int(interface_index))

    def _node_key(self, lab_id: str, node_id: int) -> _NodeKey:
        return (str(lab_id), int(node_id))

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

    def _get_or_create_node_lock(
        self, lab_id: str, node_id: int
    ) -> "threading.RLock":
        key = self._node_key(lab_id, node_id)
        with self._registry_lock:
            lock = self._node_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._node_locks[key] = lock
            return lock

    @contextmanager
    def acquire_sync(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        *,
        timeout: float = DEFAULT_ACQUIRE_TIMEOUT_S,
    ):
        """Synchronous mutex acquire. Use from sync callers
        (``_start_docker_node``, ``_start_qemu_node``, the public
        ``attach_*_interface`` / ``detach_*_interface`` entrypoints).

        ``timeout`` (seconds) bounds the wait; on expiry raises
        :class:`RuntimeMutexContention` instead of blocking indefinitely
        (US-303 spec).
        """
        lock = self._get_or_create(lab_id, node_id, interface_index)
        if not lock.acquire(timeout=float(timeout)):
            raise RuntimeMutexContention(lab_id, node_id, interface_index, timeout)
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def acquire(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        *,
        timeout: float = DEFAULT_ACQUIRE_TIMEOUT_S,
    ):
        """Async mutex acquire. Use from async callers
        (``link_service.create_link`` / ``delete_link``). Internally
        delegates to the same :class:`threading.Lock` so async + sync
        callers serialize against each other.

        The ``threading.Lock.acquire(blocking=False)`` poll keeps the
        event loop responsive on contention; on success we return
        immediately without an executor hop.

        ``timeout`` (seconds) bounds the wait; on expiry raises
        :class:`RuntimeMutexContention`.
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
        # not stall the event loop. Bounded wait per US-303 spec.
        acquired = await asyncio.get_running_loop().run_in_executor(
            None, lambda: lock.acquire(timeout=float(timeout))
        )
        if not acquired:
            raise RuntimeMutexContention(lab_id, node_id, interface_index, timeout)
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def acquire_node_sync(
        self,
        lab_id: str,
        node_id: int,
        *,
        timeout: float = DEFAULT_ACQUIRE_TIMEOUT_S,
    ):
        """US-303 codex iter1 HIGH-2: per-``(lab, node)`` slot-allocation
        lock. Acquire AROUND the ``query-pci → device_add`` window so
        concurrent attaches to different interfaces on the same VM
        cannot pick the same ``rpN`` slot.

        Bounded wait (raises :class:`RuntimeMutexContention` on expiry)
        — slot pick is a millisecond-scale operation, anything longer
        means the QMP socket itself is wedged.
        """
        lock = self._get_or_create_node_lock(lab_id, node_id)
        if not lock.acquire(timeout=float(timeout)):
            # interface_index=-1 sentinel marks "node-scoped" contention
            # so the message is still informative.
            raise RuntimeMutexContention(lab_id, node_id, -1, timeout)
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
            self._node_locks.clear()


# Module-level singleton mirrors the ``link_service`` / ``mac_registry``
# pattern: there is exactly one mutex registry per backend process.
runtime_mutex = RuntimeMutexRegistry()
