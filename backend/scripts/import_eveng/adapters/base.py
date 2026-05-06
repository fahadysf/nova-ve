"""VendorAdapter ABC + REQUIRED_FIELDS contract (#186).

Each vendor adapter is a subclass declaring three class-level constants and
two methods:

- ``name`` (str) — the adapter identity used in logs and snapshots.
- ``priority`` (int) — load-bearing dispatch order. Higher wins. The registry
  sorts by descending priority on every dispatch; ties resolve by registry
  insertion order. ``generic_linux`` declares ``priority = 0`` and is
  structurally last.
- ``REQUIRED_FIELDS`` (set[str]) — **presence-only** contract. Fields named
  here MUST appear in the raw intermediate dict. Semantic validation
  (``cpu > 0``, valid ``nic_driver``, etc.) belongs in the adapter's
  ``convert()`` / ``validate()`` override, **not** in REQUIRED_FIELDS.

The ABC's ``validate(raw)`` runs the presence check and raises
:class:`NeedsManualReview` on failure; the importer translates that exception
into a manifest ``templates[].status="needs-manual-review"`` entry and
continues.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar


class NeedsManualReview(Exception):
    """Raised by ``VendorAdapter.validate()`` when REQUIRED_FIELDS are missing.

    Caught by the importer; translated to a manifest entry with
    ``status="needs-manual-review"`` so the run does not abort.
    """


class VendorAdapter(ABC):
    """Abstract base for every per-vendor adapter."""

    name: ClassVar[str] = ""
    priority: ClassVar[int] = 0
    REQUIRED_FIELDS: ClassVar[set[str]] = set()

    @abstractmethod
    def match(self, raw: dict[str, Any]) -> bool:
        """Return True iff this adapter claims ``raw``. First-truthy in
        descending priority wins. Same-priority ties resolve by registry
        insertion order.
        """
        raise NotImplementedError

    @abstractmethod
    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        """Translate ``raw`` into a nova-ve template dict.

        Implementations should call :meth:`validate` first and surface any
        semantic validation failures by raising :class:`NeedsManualReview`.
        """
        raise NotImplementedError

    def validate(self, raw: dict[str, Any]) -> None:
        """Presence-only check against ``REQUIRED_FIELDS``.

        Raises :class:`NeedsManualReview` listing the missing fields. Does NOT
        check semantic validity (e.g. that ``cpu > 0``); subclasses override
        with their own semantic checks.
        """
        missing = self.REQUIRED_FIELDS - set(raw.keys())
        if missing:
            raise NeedsManualReview(
                f"{self.name}: missing required fields: {sorted(missing)}"
            )
