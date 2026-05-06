"""Manifest schema for the EVE-NG importer (#183).

The manifest is the durable record of what the importer did: every file copied
(``imported``), every template emitted or flagged for review (``templates``),
every destination skipped because the bytes already matched (``skipped``), and
every failure (``errors``). Re-running the importer reads the previous manifest
to power idempotency.

Schema is JSON-stable and round-trips through :meth:`ImportManifest.to_dict`
and :meth:`ImportManifest.from_dict`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ImportedEntry:
    """One file successfully copied from source to destination."""

    src: str
    dst: str
    sha256: str
    bytes: int
    mode: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportedEntry":
        return cls(
            src=str(data["src"]),
            dst=str(data["dst"]),
            sha256=str(data["sha256"]),
            bytes=int(data["bytes"]),
            mode=str(data.get("mode", "default")),
        )


@dataclass
class TemplateEntry:
    """One template emitted or flagged for manual review."""

    name: str
    status: str  # "ok" | "needs-manual-review"
    json: str | None = None
    reason: str | None = None
    eveng_raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.json is not None:
            out["json"] = self.json
        if self.reason is not None:
            out["reason"] = self.reason
        if self.eveng_raw is not None:
            out["_eveng_raw"] = self.eveng_raw
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TemplateEntry":
        return cls(
            name=str(data["name"]),
            status=str(data["status"]),
            json=str(data["json"]) if data.get("json") is not None else None,
            reason=str(data["reason"]) if data.get("reason") is not None else None,
            eveng_raw=dict(data["_eveng_raw"]) if data.get("_eveng_raw") is not None else None,
        )


@dataclass
class SkippedEntry:
    """One destination skipped because it already matched (idempotency)."""

    src: str
    dst: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkippedEntry":
        return cls(src=str(data["src"]), dst=str(data["dst"]), reason=str(data["reason"]))


@dataclass
class ErrorEntry:
    """One failure that did not abort the run."""

    path: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "error": self.error}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorEntry":
        return cls(path=str(data["path"]), error=str(data["error"]))


@dataclass
class ImportManifest:
    """Durable record of one importer run."""

    version: int = MANIFEST_VERSION
    started_at: str = field(default_factory=_utc_now_iso)
    finished_at: str | None = None
    imported: list[ImportedEntry] = field(default_factory=list)
    templates: list[TemplateEntry] = field(default_factory=list)
    skipped: list[SkippedEntry] = field(default_factory=list)
    errors: list[ErrorEntry] = field(default_factory=list)

    def mark_finished(self) -> None:
        self.finished_at = _utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "imported": [e.to_dict() for e in self.imported],
            "templates": [e.to_dict() for e in self.templates],
            "skipped": [e.to_dict() for e in self.skipped],
            "errors": [e.to_dict() for e in self.errors],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportManifest":
        return cls(
            version=int(data.get("version", MANIFEST_VERSION)),
            started_at=str(data.get("started_at") or _utc_now_iso()),
            finished_at=(str(data["finished_at"]) if data.get("finished_at") is not None else None),
            imported=[ImportedEntry.from_dict(e) for e in data.get("imported", [])],
            templates=[TemplateEntry.from_dict(e) for e in data.get("templates", [])],
            skipped=[SkippedEntry.from_dict(e) for e in data.get("skipped", [])],
            errors=[ErrorEntry.from_dict(e) for e in data.get("errors", [])],
        )

    def write(self, path: Path) -> None:
        """Atomically write the manifest as JSON to ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n")
        tmp.replace(path)

    @classmethod
    def read(cls, path: Path) -> "ImportManifest":
        return cls.from_dict(json.loads(path.read_text()))
