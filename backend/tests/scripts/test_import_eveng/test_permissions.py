"""Tests for the permissions / chown helper (#184)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.import_eveng._app_owner import AppOwner
from scripts.import_eveng.permissions import fixup, fixup_tree


def _self_owner() -> AppOwner:
    """Return an AppOwner pointing at the current process uid/gid.

    Tests cannot chown to arbitrary users without root, so the test fixture is
    always the process owner. The chmod path is exercised by writing files
    with permissive modes and asserting they are tightened.
    """
    return AppOwner(
        name="self",
        uid=os.getuid(),
        group="self",
        gid=os.getgid(),
        home=str(Path.home()),
        source="test",
    )


def test_fixup_tightens_dir_mode_to_0755(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir(mode=0o777)
    fixup(d, _self_owner())
    assert (d.stat().st_mode & 0o777) == 0o755


def test_fixup_tightens_file_mode_to_0644(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"x")
    f.chmod(0o600)
    fixup(f, _self_owner())
    assert (f.stat().st_mode & 0o777) == 0o644


def test_fixup_idempotent(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"x")
    fixup(f, _self_owner())
    mode_first = f.stat().st_mode
    fixup(f, _self_owner())
    assert f.stat().st_mode == mode_first


def test_fixup_tree_walks_recursively(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True, mode=0o700)
    (root / "a" / "b" / "x").write_bytes(b"x")
    (root / "a" / "y").write_bytes(b"y")
    (root / "a" / "b" / "x").chmod(0o600)
    (root / "a" / "y").chmod(0o600)
    root.chmod(0o700)

    count = fixup_tree(root, _self_owner())
    assert count >= 5  # root + a + b + x + y
    assert (root.stat().st_mode & 0o777) == 0o755
    assert ((root / "a" / "b" / "x").stat().st_mode & 0o777) == 0o644
    assert ((root / "a" / "y").stat().st_mode & 0o777) == 0o644


def test_fixup_tree_returns_zero_when_root_missing(tmp_path: Path) -> None:
    assert fixup_tree(tmp_path / "missing", _self_owner()) == 0


def test_fixup_handles_symlink_without_chmod(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(target.name)
    # No raise; symlink mode is OS-dependent and not asserted (per fixup's
    # documented behaviour: chown only).
    fixup(link, _self_owner())
