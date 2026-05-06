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

from .base import NeedsManualReview, VendorAdapter
from .cisco_csr1000v import CiscoCSR1000vAdapter
from .cisco_iol import CiscoIOLAdapter
from .cisco_iosv_l2 import CiscoIOSvL2Adapter
from .cisco_iosv_l3 import CiscoIOSvL3Adapter
from .generic_linux import GenericLinuxAdapter

ADAPTERS: list[VendorAdapter] = []


def register(adapter: VendorAdapter) -> None:
    """Append ``adapter`` to the registry. Subsequent dispatches see the new entry."""
    ADAPTERS.append(adapter)


def reset_registry_for_tests() -> None:
    """Clear and re-prime the registry. Test-only: do not call from production paths."""
    ADAPTERS.clear()
    register(CiscoCSR1000vAdapter())
    register(CiscoIOLAdapter())
    register(CiscoIOSvL2Adapter())
    register(CiscoIOSvL3Adapter())
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
register(CiscoCSR1000vAdapter())
register(CiscoIOLAdapter())
register(CiscoIOSvL2Adapter())
register(CiscoIOSvL3Adapter())
register(GenericLinuxAdapter())


__all__ = [
    "ADAPTERS",
    "CiscoCSR1000vAdapter",
    "CiscoIOLAdapter",
    "CiscoIOSvL2Adapter",
    "CiscoIOSvL3Adapter",
    "GenericLinuxAdapter",
    "NeedsManualReview",
    "VendorAdapter",
    "iter_adapters",
    "register",
    "reset_registry_for_tests",
    "select_adapter",
]
