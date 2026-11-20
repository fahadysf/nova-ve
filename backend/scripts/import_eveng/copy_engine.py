"""Copy engine for the EVE-NG importer (#184, #271).

Implements the copy modes specified in GH #184 (post-R-OOB-1) plus the #271
default::

    move-symlink     — rename the file into <dest> and leave a symlink at the
                       original path (the CLI default since #271); falls back
                       to copy + sha256 verify when the source and destination
                       live on different filesystems
    default          — copy + sha256 verify; source preserved
    --delete-source  — copy + sha256 verify + delete source after match
    --move           — copy + delete source; SKIP sha256 verify (unsafe)
    --force          — overwrite an existing destination on sha256 mismatch
                       (combinable with the above)

The engine-level default stays ``CopyMode.DEFAULT`` (conservative copy) so
programmatic callers keep the pre-#271 contract; the CLI passes
``MOVE_SYMLINK`` unless the operator opts out with ``--copy-only``.

Idempotency: when the destination already exists with a matching sha256, the
copy is recorded as ``skipped`` and the source is left untouched regardless of
mode. In ``move-symlink`` mode a source that is already a symlink pointing at
the destination is the fast-path skip (no hashing at all). This is what lets
operators re-run the importer freely.
"""

from __future__ import annotations

import enum
import errno
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ._hash import sha256_file
from .idempotency import evaluate

_logger = logging.getLogger("nova_ve.import_eveng")


class CopyMode(enum.Enum):
    """Operator-facing copy mode."""

    MOVE_SYMLINK = "move-symlink"
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
    symlinked: bool = False


class CopyEngineError(RuntimeError):
    """Raised on unrecoverable copy failures (e.g. verify mismatch in default mode)."""


def _ensure_parent(dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)


def _src_is_link_to(src: Path, dst: Path) -> bool:
    """True when ``src`` is a symlink that already resolves to ``dst``."""
    if not src.is_symlink():
        return False
    try:
        return os.path.samefile(src, dst)
    except OSError:
        return False


def _copy_verify_unlink(src: Path, dst: Path) -> str:
    """Cross-filesystem fallback for ``move-symlink``: copy + verify, then unlink.

    Returns the sha256 (computed for the verification anyway). The source is
    never unlinked when verification fails — the
    :class:`CopyEngineError` propagates first.
    """
    shutil.copy2(src, dst)
    src_hash = sha256_file(src)
    dst_hash = sha256_file(dst)
    if src_hash != dst_hash:
        raise CopyEngineError(
            f"sha256 mismatch after copy: {src} ({src_hash}) -> {dst} ({dst_hash})"
        )
    src.unlink()
    return src_hash


def _move_and_symlink(src: Path, dst: Path, *, force: bool) -> CopyOutcome:
    """Move ``src`` to ``dst`` and leave a symlink at the original path (#271).

    Same-filesystem moves are a plain ``os.rename`` — no bytes copied, no
    verify needed. Cross-filesystem sources fall back to copy + sha256
    verify + unlink. Either way ``src`` ends up as a symlink pointing at
    ``dst`` so the EVE-NG tree keeps resolving and nodes keep booting.
    """
    mode = CopyMode.MOVE_SYMLINK

    # Fast idempotency: the source is already a symlink to the destination.
    if _src_is_link_to(src, dst):
        return CopyOutcome(
            src=str(src),
            dst=str(dst),
            bytes=dst.stat().st_size,
            sha256="",
            mode=mode.value,
            status="skipped",
            reason="already migrated: source is a symlink to the destination",
            symlinked=True,
        )
    if src.is_symlink():
        raise CopyEngineError(
            f"source is a symlink to another file; refusing to move+symlink: {src}"
        )

    if dst.exists():
        decision = evaluate(src, dst)
        if decision.skip:
            # Destination already holds the bytes; leave the source file in
            # place — removing it would be a destructive dedupe the operator
            # did not ask for (--delete-source exists for exactly that).
            return CopyOutcome(
                src=str(src),
                dst=str(dst),
                bytes=dst.stat().st_size,
                sha256=decision.dst_sha256 or "",
                mode=mode.value,
                status="skipped",
                reason=decision.reason,
            )
        if not force:
            raise CopyEngineError(
                f"destination exists with different sha256: {dst} "
                f"(use --force to overwrite)"
            )

    _ensure_parent(dst)
    notes: list[str] = []
    src_hash = ""
    cross_fs = src.stat().st_dev != dst.parent.stat().st_dev
    if cross_fs:
        src_hash = _copy_verify_unlink(src, dst)
        notes.append("cross-filesystem: copied + sha256 verified")
    else:
        try:
            os.rename(src, dst)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise CopyEngineError(f"move failed: {src} -> {dst}: {exc}") from exc
            src_hash = _copy_verify_unlink(src, dst)
            notes.append("cross-filesystem: copied + sha256 verified")

    try:
        src.symlink_to(os.path.abspath(dst))
    except OSError as exc:
        raise CopyEngineError(f"failed to symlink back {src} -> {dst}: {exc}") from exc
    notes.append(f"symlinked back to {src}")

    return CopyOutcome(
        src=str(src),
        dst=str(dst),
        bytes=dst.stat().st_size,
        sha256=src_hash,
        mode=mode.value,
        status="imported",
        reason="; ".join(notes),
        symlinked=True,
    )


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

    if mode is CopyMode.MOVE_SYMLINK:
        return _move_and_symlink(src, dst, force=force)

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
