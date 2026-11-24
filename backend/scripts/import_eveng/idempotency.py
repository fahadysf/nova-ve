"""Compare existing destinations for dry runs and repeated imports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ._hash import sha256_file


_SAMPLE_BYTES = 64 * 1024


@dataclass
class IdempotencyDecision:
    """Result of comparing one source/destination pair."""

    skip: bool
    reason: str
    src_sha256: str | None = None
    dst_sha256: str | None = None


def _matching_boundaries(src: Path, dst: Path) -> bool:
    with src.open("rb") as source, dst.open("rb") as destination:
        size = os.fstat(source.fileno()).st_size
        if size != os.fstat(destination.fileno()).st_size:
            return False

        head_size = min(size, _SAMPLE_BYTES)
        head = source.read(head_size)
        if len(head) != head_size or head != destination.read(head_size):
            return False
        if size <= _SAMPLE_BYTES:
            return True

        source.seek(size - _SAMPLE_BYTES)
        destination.seek(size - _SAMPLE_BYTES)
        tail = source.read(_SAMPLE_BYTES)
        return len(tail) == _SAMPLE_BYTES and tail == destination.read(_SAMPLE_BYTES)


def evaluate(src: Path, dst: Path, *, verify_full: bool = False) -> IdempotencyDecision:
    """Treat equal sizes and matching file boundaries as an existing copy."""
    if not src.exists():
        return IdempotencyDecision(skip=False, reason="source missing")
    if not dst.exists():
        return IdempotencyDecision(skip=False, reason="destination missing")

    if not verify_full and _matching_boundaries(src, dst):
        return IdempotencyDecision(skip=True, reason="exists, size and boundary bytes match")

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
