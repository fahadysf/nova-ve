"""Tests for the copy engine (#184)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.import_eveng._hash import sha256_file
from scripts.import_eveng.copy_engine import (
    CopyEngineError,
    CopyMode,
    perform_copy,
)


def test_default_mode_copies_and_preserves_source(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    dst = tmp_path / "subdir" / "dst.bin"
    src.write_bytes(b"hello world")

    outcome = perform_copy(src, dst, mode=CopyMode.DEFAULT)
    assert outcome.status == "imported"
    assert outcome.mode == "default"
    assert outcome.source_deleted is False
    assert src.exists(), "default mode must NEVER delete source"
    assert dst.read_bytes() == b"hello world"
    assert outcome.sha256 == sha256_file(src)


def test_delete_source_only_unlinks_after_verify(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"some bytes")

    outcome = perform_copy(src, dst, mode=CopyMode.DELETE_SOURCE)
    assert outcome.status == "imported"
    assert outcome.source_deleted is True
    assert not src.exists()
    assert dst.exists()


def test_move_skips_verify_and_deletes(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"unsafe move")

    outcome = perform_copy(src, dst, mode=CopyMode.MOVE)
    assert outcome.status == "imported"
    assert outcome.source_deleted is True
    assert outcome.mode == "move"
    assert not src.exists()
    assert dst.exists()
    # MOVE skips verify -> no sha256 reported
    assert outcome.sha256 == ""


def test_idempotent_skip_when_dst_matches(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"identical")
    dst.write_bytes(b"identical")

    for mode in (CopyMode.DEFAULT, CopyMode.DELETE_SOURCE, CopyMode.MOVE):
        outcome = perform_copy(src, dst, mode=mode)
        assert outcome.status == "skipped"
        assert outcome.reason == "exists, sha256 match"
        # Idempotency NEVER deletes the source, even in destructive modes.
        assert src.exists(), f"src deleted on idempotent skip in mode={mode.value}"


def test_dst_exists_with_mismatch_requires_force(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")

    with pytest.raises(CopyEngineError) as exc_info:
        perform_copy(src, dst, mode=CopyMode.DEFAULT, force=False)
    assert "different sha256" in str(exc_info.value)
    assert "--force" in str(exc_info.value)
    # Source untouched even on mismatch failure.
    assert src.read_bytes() == b"new"


def test_force_overwrites_on_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"new content")
    dst.write_bytes(b"old content")

    outcome = perform_copy(src, dst, mode=CopyMode.DEFAULT, force=True)
    assert outcome.status == "imported"
    assert dst.read_bytes() == b"new content"


def test_verify_failure_does_not_delete_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a sha256 mismatch after copy: source must NOT be deleted."""
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"will be tampered")

    real_copy2 = __import__("shutil").copy2

    def tampering_copy2(s, d):
        real_copy2(s, d)
        Path(d).write_bytes(b"DIFFERENT BYTES")  # tamper post-copy to force verify mismatch

    monkeypatch.setattr("scripts.import_eveng.copy_engine.shutil.copy2", tampering_copy2)

    with pytest.raises(CopyEngineError) as exc_info:
        perform_copy(src, dst, mode=CopyMode.DELETE_SOURCE)
    assert "sha256 mismatch after copy" in str(exc_info.value)
    assert src.exists(), "source MUST NOT be deleted when verify fails"
