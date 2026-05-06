"""Copy engine for the EVE-NG importer (#184).

Implements the four copy modes specified in GH #184 (post-R-OOB-1)::

    default          — copy + sha256 verify; source preserved
    --delete-source  — copy + sha256 verify + delete source after match
    --move           — copy + delete source; SKIP sha256 verify (unsafe)
    --force          — overwrite an existing destination on sha256 mismatch
                       (combinable with the above)

Idempotency: when the destination already exists with a matching sha256, the
copy is recorded as ``skipped`` and the source is left untouched regardless of
mode. This is what lets operators re-run the importer freely.
"""

from __future__ import annotations

import enum
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ._hash import sha256_file
from .idempotency import evaluate

_logger = logging.getLogger("nova_ve.import_eveng")


class CopyMode(enum.Enum):
    """Operator-facing copy mode."""

    DEFAULT = "default"
    DELETE_SOURCE = "delete-source"
    MOVE = "move"


@dataclass
class CopyOutcome:
    """Result of one ``perform_copy`` call."""

    src: str
    dst: str
    bytes: int
    sha256: str
    mode: str
    status: str  # "imported" | "skipped"
    reason: str | None = None
    source_deleted: bool = False


class CopyEngineError(RuntimeError):
    """Raised on unrecoverable copy failures (e.g. verify mismatch in default mode)."""


def _ensure_parent(dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)


def perform_copy(
    src: Path,
    dst: Path,
    *,
    mode: CopyMode = CopyMode.DEFAULT,
    force: bool = False,
) -> CopyOutcome:
    """Copy ``src`` to ``dst`` honouring ``mode`` and ``force``.

    Idempotency runs first: an existing ``dst`` with matching sha256 yields a
    ``skipped`` outcome regardless of mode and never deletes the source.
    """
    if not src.is_file():
        raise CopyEngineError(f"source is not a regular file: {src}")

    decision = evaluate(src, dst)
    if decision.skip:
        return CopyOutcome(
            src=str(src),
            dst=str(dst),
            bytes=src.stat().st_size,
            sha256=decision.src_sha256 or "",
            mode=mode.value,
            status="skipped",
            reason=decision.reason,
        )

    if dst.exists() and not force and decision.src_sha256 != decision.dst_sha256:
        raise CopyEngineError(
            f"destination exists with different sha256: {dst} "
            f"(use --force to overwrite)"
        )

    _ensure_parent(dst)
    shutil.copy2(src, dst)

    if mode is CopyMode.MOVE:
        # MOVE explicitly skips verify per GH #184 + R-OOB-1 (documented unsafe).
        src.unlink()
        dst_size = dst.stat().st_size
        return CopyOutcome(
            src=str(src),
            dst=str(dst),
            bytes=dst_size,
            sha256="",
            mode=mode.value,
            status="imported",
            source_deleted=True,
        )

    # default and delete-source both verify before doing anything destructive.
    src_hash = sha256_file(src)
    dst_hash = sha256_file(dst)
    if src_hash != dst_hash:
        # Verify failed — never delete source. Surface as a hard error so the
        # CLI can record it under manifest.errors[] instead of silently passing.
        raise CopyEngineError(
            f"sha256 mismatch after copy: {src} ({src_hash}) -> {dst} ({dst_hash})"
        )

    deleted = False
    if mode is CopyMode.DELETE_SOURCE:
        src.unlink()
        deleted = True

    return CopyOutcome(
        src=str(src),
        dst=str(dst),
        bytes=dst.stat().st_size,
        sha256=src_hash,
        mode=mode.value,
        status="imported",
        source_deleted=deleted,
    )
