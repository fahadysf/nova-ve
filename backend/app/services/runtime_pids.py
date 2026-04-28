# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""runtime_pids — atomic pid registry for nova-ve runtime.

US-201/US-203: the privileged ``nova-ve-net.py`` helper authorizes pid-taking
verbs against ``/var/lib/nova-ve/runtime/pids.json``. This module owns the
read-modify-write cycle for that file.

Schema (one entry per running container/QEMU process)::

    [
        {
            "pid": int,
            "kind": "docker" | "qemu",
            "lab_id": str,
            "node_id": int,
            "started_at": float,
            "generation": int,
        },
        ...
    ]

Atomic-write contract (Codex v4 new defect #1):
  * A separate ``pids.json.lock`` flock guards the read-modify-write cycle
    (5 s timeout, mirrors ``lab_lock.py``).
  * Inside the lock: read the current registry, mutate, write to
    ``pids.json.tmp.<uuid>``, ``fsync``, ``os.replace`` to ``pids.json``.

Sequencing contract (Codex v5 finding #3):
  * ``register`` MUST be called BEFORE any helper-verb invocation (the
    helper would reject the pid otherwise).
  * ``unregister`` runs synchronously inside the stop path — never deferred.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


# Path to the registry. Settable via env var so tests can point at a tmp_path.
_DEFAULT_REGISTRY_PATH = Path("/var/lib/nova-ve/runtime/pids.json")


def _registry_path() -> Path:
    override = os.environ.get("NOVA_VE_PIDS_JSON")
    if override:
        return Path(override)
    return _DEFAULT_REGISTRY_PATH


def _lock_path() -> Path:
    return _registry_path().with_suffix(".json.lock")


@contextmanager
def _registry_lock(timeout_s: float = 5.0) -> Iterator[None]:
    """Acquire an exclusive flock on the pids.json.lock sentinel.

    Mirrors ``lab_lock.py`` semantics: 'a+' open mode (no truncation),
    ``LOCK_EX | LOCK_NB`` polled in 50 ms increments, ``LOCK_UN`` in finally.
    """
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    with open(lock_path, "a+") as fd:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire pids.json lock within {timeout_s}s"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _read_registry() -> list[dict]:
    """Return the current registry as a list. Missing/corrupt → []."""
    path = _registry_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("pid"), int):
            out.append(entry)
    return out


def _atomic_write(entries: list[dict]) -> None:
    """Atomic-rename write of the registry."""
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
    payload = json.dumps(entries, indent=2)
    with open(tmp_path, "w") as fd:
        fd.write(payload)
        fd.flush()
        os.fsync(fd.fileno())
    os.replace(tmp_path, path)


def register(
    pid: int,
    kind: str,
    lab_id: str,
    node_id: int,
    *,
    generation: int = 0,
) -> None:
    """Register a runtime pid into the registry.

    Replaces any prior entry for the same pid (recycled-pid case). The caller
    MUST invoke this BEFORE any helper-verb call referencing the pid.
    """
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError(f"invalid pid: {pid!r}")
    if kind not in ("docker", "qemu"):
        raise ValueError(f"unknown kind: {kind!r}")
    entry = {
        "pid": pid,
        "kind": kind,
        "lab_id": str(lab_id),
        "node_id": int(node_id),
        "started_at": time.time(),
        "generation": int(generation),
    }
    with _registry_lock():
        entries = [e for e in _read_registry() if e.get("pid") != pid]
        entries.append(entry)
        _atomic_write(entries)


def unregister(pid: int) -> None:
    """Drop the entry for ``pid`` from the registry. Idempotent."""
    if not isinstance(pid, int) or pid <= 0:
        return
    with _registry_lock():
        entries = [e for e in _read_registry() if e.get("pid") != pid]
        _atomic_write(entries)


def lookup(pid: int) -> dict | None:
    """Return the registry entry for ``pid`` or ``None`` if absent."""
    for entry in _read_registry():
        if entry.get("pid") == pid:
            return entry
    return None


def list_entries() -> list[dict]:
    """Return a copy of every registry entry."""
    return list(_read_registry())
