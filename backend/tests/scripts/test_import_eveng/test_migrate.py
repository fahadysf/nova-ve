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
