"""Vendor adapter registry for the EVE-NG importer (#186).

Each :class:`VendorAdapter` declares a class-level ``priority`` attribute
(higher wins) and a ``match(raw)`` predicate. The registry is sorted by
descending priority on every dispatch; same-priority ties resolve by the
order entries were appended to :data:`ADAPTERS` (Python's :func:`sorted` is
stable).

``generic_linux`` is the last-registered, lowest-priority adapter and exists
specifically to never-fail the dispatch loop. Tests assert that it is
structurally last (a defensive sanity check).

Per-vendor PRs (#187 Cisco, #188 Juniper, #189 Arista/Mikrotik/VyOS) each add:

1. one new module under ``adapters/`` with their concrete subclass.
2. one ``import`` line in alphabetical order in this file.
3. one ``register(...)`` call before the existing
   ``register(GenericLinuxAdapter())`` line.

This layout keeps the per-PR conflict surface here predictable and small.
"""

from __future__ import annotations

from .arista_veos import AristaVEosAdapter
from .base import NeedsManualReview, VendorAdapter
from .generic_linux import GenericLinuxAdapter
from .mikrotik_chr import MikrotikCHRAdapter
from .vyos import VyOSAdapter

ADAPTERS: list[VendorAdapter] = []


def register(adapter: VendorAdapter) -> None:
    """Append ``adapter`` to the registry. Subsequent dispatches see the new entry."""
    ADAPTERS.append(adapter)


def reset_registry_for_tests() -> None:
    """Clear and re-prime the registry. Test-only: do not call from production paths."""
    ADAPTERS.clear()
    register(AristaVEosAdapter())
    register(MikrotikCHRAdapter())
    register(VyOSAdapter())
    register(GenericLinuxAdapter())


def iter_adapters() -> list[VendorAdapter]:
    """Return a sorted snapshot of adapters: by descending priority, ties by insertion order."""
    return sorted(ADAPTERS, key=lambda a: -a.priority)


def select_adapter(raw: dict) -> VendorAdapter | None:
    """Return the highest-priority adapter whose ``match(raw)`` is truthy, or None."""
    for adapter in iter_adapters():
        if adapter.match(raw):
            return adapter
    return None


# Built-in registrations. Vendor PRs prepend their entries above this line.
register(AristaVEosAdapter())
register(MikrotikCHRAdapter())
register(VyOSAdapter())
register(GenericLinuxAdapter())


__all__ = [
    "ADAPTERS",
    "AristaVEosAdapter",
    "GenericLinuxAdapter",
    "MikrotikCHRAdapter",
    "NeedsManualReview",
    "VendorAdapter",
    "VyOSAdapter",
    "iter_adapters",
    "register",
    "reset_registry_for_tests",
    "select_adapter",
]
