"""End-to-end importer test (#190).

Drives the full pipeline (walker → migrate → adapter dispatch → manifest)
against the synthetic fake /opt/unetlab tree from conftest.py and asserts
the GH-spec'd invariants:

1. Manifest has all four top-level keys (imported, templates, skipped, errors).
2. Files copied with sha256 verified at destination (every imported entry
   carries a sha256).
3. Default-mode source tree is byte-identical pre/post-run.
4. Idempotent re-run lands every file in `skipped` with `reason: "exists, sha256 match"`.
5. `--delete-source` removes source after sha256 verification succeeds.
6. Manifest is shaped JSON (round-trips through ImportManifest.from_dict).

Adapter dispatch and per-vendor snapshot validations live in the per-vendor
test files (#187, #188, #189). The migration-guide doc lint lives in
test_migration_guide_doc_lint.py.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.import_eveng._app_owner import AppOwner
from scripts.import_eveng.copy_engine import CopyMode
from scripts.import_eveng.manifest import ImportManifest
from scripts.import_eveng.migrate import MigrateOptions, run_migration
from scripts.import_eveng.walker import walk_all


def _sha256_tree(root: Path) -> dict[str, str]:
    """Map every file under root (recursive) to its sha256 hex digest."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _self_owner() -> AppOwner:
    """Return an AppOwner pointing at the current process uid/gid (test-only)."""
    return AppOwner(
        name="self",
        uid=os.getuid(),
        group="self",
        gid=os.getgid(),
        home=str(Path.home()),
        source="test",
    )


# ---- 1 + 2 + 6: manifest schema + sha256 verification --------------------


def test_e2e_manifest_has_all_four_top_level_keys(synthetic_eveng_tree, tmp_path):
    """Assertion 1 + 6: manifest carries imported / templates / skipped / errors and round-trips through JSON."""
    dest = tmp_path / "nova-ve-images"
    manifest = ImportManifest()

    items = walk_all(synthetic_eveng_tree.source_root, dest)
    run_migration(items, options=MigrateOptions(), manifest=manifest)
    manifest.mark_finished()

    out = tmp_path / "import-manifest.json"
    manifest.write(out)

    raw = json.loads(out.read_text())
    assert {"imported", "templates", "skipped", "errors"} <= set(raw.keys())
    # Round-trip through from_dict (assertion 6 in spirit — the schema is stable).
    re_parsed = ImportManifest.from_dict(raw)
    assert re_parsed.to_dict() == manifest.to_dict()


def test_e2e_imported_entries_carry_sha256(synthetic_eveng_tree, tmp_path):
    """Assertion 2: every file copied to the destination carries a sha256."""
    dest = tmp_path / "nova-ve-images"
    manifest = ImportManifest()

    items = walk_all(synthetic_eveng_tree.source_root, dest)
    run_migration(
        items,
        options=MigrateOptions(),
        manifest=manifest,
        # docker_build mocked to a no-op since the synthetic tree has no docker context.
        docker_build=lambda ctx, tag: None,
    )

    assert len(manifest.imported) > 0
    for entry in manifest.imported:
        assert entry.sha256 != ""
        assert len(entry.sha256) == 64  # hex digest length


# ---- 3: default-mode source byte-identical pre/post-run ----------------


def test_e2e_default_mode_leaves_source_byte_identical(synthetic_eveng_tree, tmp_path):
    """Assertion 3: default-mode run does NOT mutate the source tree."""
    pre_hashes = _sha256_tree(synthetic_eveng_tree.source_root)
    assert pre_hashes  # sanity: tree is non-empty

    dest = tmp_path / "nova-ve-images"
    items = walk_all(synthetic_eveng_tree.source_root, dest)
    manifest = ImportManifest()
    run_migration(
        items,
        options=MigrateOptions(mode=CopyMode.DEFAULT),
        manifest=manifest,
        docker_build=lambda ctx, tag: None,
    )

    post_hashes = _sha256_tree(synthetic_eveng_tree.source_root)
    assert post_hashes == pre_hashes, "default mode must NEVER delete or mutate source files"


# ---- 4: idempotent re-run records every file as skipped -----------------


def test_e2e_idempotent_rerun_records_skipped(synthetic_eveng_tree, tmp_path):
    """Assertion 4: re-running the importer is a clean no-op (everything in skipped)."""
    dest = tmp_path / "nova-ve-images"

    # First run.
    items1 = walk_all(synthetic_eveng_tree.source_root, dest)
    m1 = ImportManifest()
    run_migration(items1, options=MigrateOptions(), manifest=m1, docker_build=lambda c, t: None)
    first_imported = len(m1.imported)
    assert first_imported > 0

    # Second run with the same source (default mode preserved sources, so files are still there).
    items2 = walk_all(synthetic_eveng_tree.source_root, dest)
    m2 = ImportManifest()
    run_migration(items2, options=MigrateOptions(), manifest=m2, docker_build=lambda c, t: None)
    assert len(m2.imported) == 0
    assert len(m2.skipped) == first_imported
    for skip_entry in m2.skipped:
        assert skip_entry.reason == "exists, sha256 match"


# ---- 5: --delete-source removes source after verify ---------------------


def test_e2e_delete_source_removes_source_after_verify(synthetic_eveng_tree, tmp_path):
    """Assertion 5: --delete-source unlinks each source file after sha256 verifies."""
    dest = tmp_path / "nova-ve-images"

    items = walk_all(synthetic_eveng_tree.source_root, dest)
    manifest = ImportManifest()
    run_migration(
        items,
        options=MigrateOptions(mode=CopyMode.DELETE_SOURCE),
        manifest=manifest,
        docker_build=lambda c, t: None,
    )

    # All source files in the addons tree should be gone (only directories may remain).
    leftover_files = [
        p for p in synthetic_eveng_tree.addons.rglob("*") if p.is_file() and not p.is_symlink()
    ]
    assert leftover_files == [], f"source files still present after --delete-source: {leftover_files}"


# ---- perm fixup applied -----------------------------------------------


def test_e2e_perm_fixup_applied_to_dest_tree(synthetic_eveng_tree, tmp_path):
    """The dest qemu/ subtree has the canonical 0755/0644 modes after perm fixup."""
    dest = tmp_path / "nova-ve-images"
    items = walk_all(synthetic_eveng_tree.source_root, dest)
    manifest = ImportManifest()
    run_migration(
        items,
        options=MigrateOptions(),
        manifest=manifest,
        owner=_self_owner(),
        dest_root=dest,
        docker_build=lambda c, t: None,
    )

    # Pick any imported qemu file and assert its mode.
    qemu_files = [p for p in (dest / "qemu").rglob("*") if p.is_file() and not p.is_symlink()]
    assert qemu_files, "expected at least one qemu file in dest tree"
    for f in qemu_files[:3]:  # spot-check
        mode = f.stat().st_mode & 0o777
        assert mode == 0o644, f"{f} mode is {oct(mode)}, expected 0o644"

    qemu_dir = dest / "qemu"
    dir_mode = qemu_dir.stat().st_mode & 0o777
    assert dir_mode == 0o755


# ---- iol iourc preserved alongside .bin --------------------------------


def test_e2e_iol_copies_iourc_alongside_bin(synthetic_eveng_tree, tmp_path):
    """The IOL walker copies iourc next to the .bin into the per-image dest dir."""
    dest = tmp_path / "nova-ve-images"
    items = walk_all(synthetic_eveng_tree.source_root, dest)
    manifest = ImportManifest()
    run_migration(items, options=MigrateOptions(), manifest=manifest, docker_build=lambda c, t: None)

    iol_dir = dest / "iol" / "i86bi-linux-l3-15.5"
    assert (iol_dir / "i86bi-linux-l3-15.5.bin").is_file()
    assert (iol_dir / "iourc").is_file()
