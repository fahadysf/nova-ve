"""Tests for the migrate orchestrator (#184)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.import_eveng._app_owner import AppOwner
from scripts.import_eveng.copy_engine import CopyMode
from scripts.import_eveng.manifest import ImportManifest
from scripts.import_eveng.migrate import (
    MigrateOptions,
    migrate_iol,
    migrate_qemu,
    run_migration,
)
from scripts.import_eveng.walker import walk_all


def _self_owner() -> AppOwner:
    return AppOwner(
        name="self",
        uid=os.getuid(),
        group="self",
        gid=os.getgid(),
        home=str(Path.home()),
        source="test",
    )


def _seed(path: Path, contents: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def test_reuses_builtin_wildcard_and_generates_for_nonmatching_images(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "images"
    templates = tmp_path / "templates"
    for image in ["WinServer-2025", "sample-winserver-2025"]:
        _seed(source / "addons" / "qemu" / image / "virtioa.qcow2")

    manifest = ImportManifest()
    items = walk_all(source, destination)
    run_migration(items, options=MigrateOptions(), manifest=manifest, templates_dir=templates)

    entries = {entry.name: entry for entry in manifest.templates}
    assert entries["WinServer-2025"].status == "reused"
    assert entries["WinServer-2025"].template_key == "winserver"
    assert entries["WinServer-2025"].template_type == "qemu"
    assert not (templates / "qemu" / "WinServer-2025.yml").exists()

    generated = templates / "qemu" / "sample-winserver-2025.yml"
    assert entries["sample-winserver-2025"].status == "generated"
    original_bytes = generated.read_bytes() + b"\n# Operator note\n"
    generated.write_bytes(original_bytes)
    assert (destination / "qemu" / "WinServer-2025" / "virtioa.qcow2").is_file()

    retry = ImportManifest()
    run_migration(items, options=MigrateOptions(), manifest=retry, templates_dir=templates)

    assert [entry.status for entry in retry.templates] == ["reused", "reused"]
    assert generated.read_bytes() == original_bytes
    assert ImportManifest.from_dict(retry.to_dict()).templates == retry.templates


def test_reuses_specific_same_type_user_template(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "images"
    builtins = tmp_path / "builtins"
    templates = tmp_path / "templates"
    image = "Acme-Edge-7"
    _seed(source / "addons" / "qemu" / image / "virtioa.qcow2")
    _seed(builtins / "qemu" / "acme.yml", b'type: qemu\nimage_aliases: ["acme-*"]\n')
    _seed(templates / "iol" / "exact.yml", b'type: iol\nimage_aliases: ["acme-edge-7"]\n')
    custom = templates / "operator-router.yml"
    contents = b'type: qemu\nname: Office edge router\nimage_aliases: ["acme-edge-*"]\n'
    _seed(custom, contents)

    manifest = ImportManifest()
    run_migration(
        walk_all(source, destination),
        options=MigrateOptions(),
        manifest=manifest,
        templates_dir=templates,
        builtin_templates_dir=builtins,
    )

    assert manifest.templates[0].status == "reused"
    assert manifest.templates[0].template_key == "operator-router"
    assert custom.read_bytes() == contents
    assert not (templates / "qemu" / f"{image}.yml").exists()


def test_unmatched_image_does_not_shadow_builtin_or_reuse_a_name_fragment(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "images"
    templates = tmp_path / "templates"
    for image in ("server", "winserver"):
        _seed(source / "addons" / "qemu" / image / "virtioa.qcow2")
    items = walk_all(source, destination)

    manifest = ImportManifest()
    run_migration(items, options=MigrateOptions(), manifest=manifest, templates_dir=templates)

    expected = {"server": "server", "winserver": "winserver-imported"}
    assert {entry.name: entry.template_key for entry in manifest.templates} == expected
    assert {entry.status for entry in manifest.templates} == {"generated"}
    assert not (templates / "qemu" / "winserver.yml").exists()

    retry = ImportManifest()
    run_migration(items, options=MigrateOptions(), manifest=retry, templates_dir=templates)
    assert {entry.name: entry.template_key for entry in retry.templates} == expected
    assert {entry.status for entry in retry.templates} == {"reused"}


def test_user_shadow_can_inherit_or_clear_builtin_aliases(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "images"
    builtins = tmp_path / "builtins"
    templates = tmp_path / "templates"
    image = "custom-family-7"
    _seed(source / "addons" / "qemu" / image / "virtioa.qcow2")
    _seed(builtins / "qemu" / "router.yml", b'type: qemu\nimage_aliases: ["custom-family-*"]\n')
    override = templates / "qemu" / "router.yml"
    contents = b"type: qemu\nname: Office router\n"
    _seed(override, contents)
    items = walk_all(source, destination)

    before = ImportManifest()
    run_migration(
        items,
        options=MigrateOptions(),
        manifest=before,
        templates_dir=templates,
        builtin_templates_dir=builtins,
    )
    assert before.templates[0].template_key == "router"
    assert before.templates[0].status == "reused"

    override.write_bytes(contents + b"image_aliases: []\n")
    after = ImportManifest()
    run_migration(
        items,
        options=MigrateOptions(),
        manifest=after,
        templates_dir=templates,
        builtin_templates_dir=builtins,
    )
    assert after.templates[0].template_key == image
    assert after.templates[0].status == "generated"


def test_qemu_migration_creates_cdrom_symlink(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "vyos-1.4"
    _seed(vendor / "virtioa.qcow2", b"virtio")
    _seed(vendor / "hda.qcow2", b"hda")

    items = walk_all(src, dst)
    manifest = ImportManifest()
    run_migration(items, options=MigrateOptions(), manifest=manifest)

    out_dir = dst / "qemu" / "vyos-1.4"
    assert (out_dir / "virtioa.qcow2").read_bytes() == b"virtio"
    cdrom = out_dir / "cdrom.iso"
    assert cdrom.is_symlink()
    assert cdrom.readlink().name == "virtioa.qcow2"


def test_qemu_migration_default_mode_preserves_sources(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "exotic"
    _seed(vendor / "virtioa.qcow2", b"v")

    items = walk_all(src, dst)
    manifest = ImportManifest()
    run_migration(items, options=MigrateOptions(mode=CopyMode.DEFAULT), manifest=manifest)

    # Default mode = non-destructive: source MUST still be present.
    assert (vendor / "virtioa.qcow2").exists(), "default mode must not delete source"
    assert (dst / "qemu" / "exotic" / "virtioa.qcow2").read_bytes() == b"v"


def test_qemu_migration_delete_source_removes_after_verify(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "vy"
    _seed(vendor / "virtioa.qcow2", b"vyos")

    items = walk_all(src, dst)
    manifest = ImportManifest()
    run_migration(
        items,
        options=MigrateOptions(mode=CopyMode.DELETE_SOURCE),
        manifest=manifest,
    )

    assert not (vendor / "virtioa.qcow2").exists()
    assert (dst / "qemu" / "vy" / "virtioa.qcow2").read_bytes() == b"vyos"


def test_iol_missing_iourc_marks_needs_manual_review(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _seed(src / "addons" / "iol" / "bin" / "iol.bin", b"iol-bin")

    items = walk_all(src, dst)
    manifest = ImportManifest()
    run_migration(items, options=MigrateOptions(), manifest=manifest)

    assert any(t.status == "needs-manual-review" and "iourc" in (t.reason or "")
               for t in manifest.templates)


def test_idempotent_rerun_records_skipped(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "vy"
    _seed(vendor / "virtioa.qcow2", b"vyos")

    items1 = walk_all(src, dst)
    m1 = ImportManifest()
    run_migration(items1, options=MigrateOptions(), manifest=m1)
    assert len(m1.imported) == 1
    assert len(m1.skipped) == 0

    # Re-walk the source (default mode preserves sources, so files are still there).
    items2 = walk_all(src, dst)
    m2 = ImportManifest()
    run_migration(items2, options=MigrateOptions(), manifest=m2)
    assert len(m2.imported) == 0
    assert len(m2.skipped) == 1
    assert m2.skipped[0].reason == "exists, sha256 match"


def test_perm_fixup_applied_to_dest_tree(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "vy"
    _seed(vendor / "hda.qcow2", b"hda")
    _seed(vendor / "extra", b"extra")

    items = walk_all(src, dst)
    manifest = ImportManifest()
    run_migration(
        items,
        options=MigrateOptions(),
        manifest=manifest,
        owner=_self_owner(),
        dest_root=dst,
    )

    out_dir = dst / "qemu" / "vy"
    assert (out_dir.stat().st_mode & 0o777) == 0o755
    assert ((out_dir / "hda.qcow2").stat().st_mode & 0o777) == 0o644
    # Parent qemu/ dir also receives fixup (heals the GH-noted root-owned bug).
    assert ((dst / "qemu").stat().st_mode & 0o777) == 0o755


def test_docker_invokes_build_and_writes_marker(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    ctx = src / "addons" / "docker" / "alpine-telnet"
    _seed(ctx / "Dockerfile", b"FROM alpine\n")
    _seed(ctx / "entrypoint.sh", b"#!/bin/sh\n")

    items = walk_all(src, dst)
    manifest = ImportManifest()

    invocations: list[tuple[Path, str]] = []

    def fake_build(context: Path, tag: str) -> None:
        invocations.append((context, tag))

    run_migration(
        items,
        options=MigrateOptions(),
        manifest=manifest,
        docker_build=fake_build,
    )

    assert len(invocations) == 1
    assert invocations[0][1] == "nova-ve/alpine-telnet:latest"
    marker = dst / "docker" / "alpine-telnet" / "image.txt"
    assert marker.read_text().strip() == "nova-ve/alpine-telnet:latest"


def test_docker_build_failure_records_error_without_aborting(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    ctx = src / "addons" / "docker" / "broken"
    _seed(ctx / "Dockerfile", b"FROM bad-image\n")

    items = walk_all(src, dst)
    manifest = ImportManifest()

    def failing_build(context: Path, tag: str) -> None:
        raise RuntimeError("simulated docker daemon failure")

    run_migration(
        items,
        options=MigrateOptions(),
        manifest=manifest,
        docker_build=failing_build,
    )
    assert len(manifest.errors) == 1
    assert "docker build failed" in manifest.errors[0].error
    # No marker written because build failed.
    assert not (dst / "docker" / "broken" / "image.txt").exists()


# ---- move + symlink default (#271) ---------------------------------------


def test_move_symlink_run_moves_images_and_keeps_shared_iourc_as_copy(tmp_path: Path) -> None:
    """MOVE_SYMLINK run: images moved + symlinked back; shared iourc stays a copy."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "vy"
    _seed(vendor / "hda.qcow2", b"vyos")
    _seed(src / "addons" / "dynamips" / "c7200.image", b"c7200")
    _seed(src / "addons" / "iol" / "bin" / "iol.bin", b"iol")
    _seed(src / "addons" / "iol" / "bin" / "iourc", b"license")

    items = walk_all(src, dst)
    manifest = ImportManifest()
    run_migration(
        items, options=MigrateOptions(mode=CopyMode.MOVE_SYMLINK), manifest=manifest
    )

    moved_pairs = [
        (vendor / "hda.qcow2", dst / "qemu" / "vy" / "hda.qcow2"),
        (src / "addons" / "dynamips" / "c7200.image", dst / "dynamips" / "c7200" / "c7200.image"),
        (src / "addons" / "iol" / "bin" / "iol.bin", dst / "iol" / "iol" / "iol.bin"),
    ]
    for original, moved in moved_pairs:
        assert moved.is_file() and not moved.is_symlink()
        assert original.is_symlink(), f"{original} must be symlinked back at the EVE-NG path"
        assert os.path.realpath(original) == str(moved)

    # iourc is shared metadata: plain copy, the source file stays real so
    # sibling IOL items can still read it.
    iourc_src = src / "addons" / "iol" / "bin" / "iourc"
    assert iourc_src.is_file() and not iourc_src.is_symlink()
    assert (dst / "iol" / "iol" / "iourc").read_bytes() == b"license"

    iourc_entry = next(e for e in manifest.imported if e.dst.endswith("iourc"))
    assert iourc_entry.mode == "default"
    assert iourc_entry.symlinked is False
    moved_entry = next(e for e in manifest.imported if e.dst.endswith("hda.qcow2"))
    assert moved_entry.mode == "move-symlink"
    assert moved_entry.symlinked is True
