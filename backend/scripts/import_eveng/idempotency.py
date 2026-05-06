"""sha256-based idempotency check for the EVE-NG importer (#183).

A destination is considered "already imported" when the file exists and its
sha256 matches the source. Used by both the dry-run plan emitter (to populate
the ``skipped`` list ahead of any FS mutation) and #184's actual copy logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ._hash import sha256_file


@dataclass
class IdempotencyDecision:
    """Result of comparing one source/destination pair."""

    skip: bool
    reason: str
    src_sha256: str | None = None
    dst_sha256: str | None = None


def evaluate(src: Path, dst: Path) -> IdempotencyDecision:
    """Decide whether ``dst`` is already a faithful copy of ``src``."""
    if not src.exists():
        return IdempotencyDecision(skip=False, reason="source missing")
    if not dst.exists():
        return IdempotencyDecision(skip=False, reason="destination missing")

    src_hash = sha256_file(src)
    dst_hash = sha256_file(dst)
    if src_hash == dst_hash:
        return IdempotencyDecision(
            skip=True,
            reason="exists, sha256 match",
            src_sha256=src_hash,
            dst_sha256=dst_hash,
        )
    return IdempotencyDecision(
        skip=False,
        reason="sha256 mismatch (use --force to overwrite)",
        src_sha256=src_hash,
        dst_sha256=dst_hash,
    )
