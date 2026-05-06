"""Migration orchestrator for the EVE-NG importer (#184).

Consumes :class:`MigrationItem` records from :mod:`walker` and, for each one,
runs the kind-appropriate copy logic against the :mod:`copy_engine`. Records
every outcome on the :class:`ImportManifest`.

Per-kind specifics:

- ``qemu``: copies all files in the template dir; picks the boot disk per
  precedence (``cdrom.iso`` > ``virtioa.qcow2`` > ``hda.qcow2`` > first
  ``*.qcow2``); creates a stable ``cdrom.iso`` symlink in the destination dir
  pointing at whatever was chosen.
- ``dynamips``: copies the ``*.image`` file verbatim into a per-image dir.
- ``iol``: copies the ``*.bin`` plus ``iourc`` license; if ``iourc`` is missing
  from the source, the migration record is flagged ``needs-manual-review``.
- ``docker``: invokes ``docker build -t nova-ve-<image>:latest <ctx>`` and
  emits a ``image.txt`` marker file in the destination dir with the resolved
  image tag. The build command is mockable for tests.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ._app_owner import AppOwner
from .copy_engine import CopyEngineError, CopyMode, CopyOutcome, perform_copy
from .manifest import (
    ErrorEntry,
    ImportManifest,
    ImportedEntry,
    SkippedEntry,
    TemplateEntry,
)
from .permissions import fixup, fixup_tree
from .walker import (
    KIND_DOCKER,
    KIND_DYNAMIPS,
    KIND_IOL,
    KIND_QEMU,
    MigrationItem,
)

_logger = logging.getLogger("nova_ve.import_eveng")


@dataclass
class MigrateOptions:
    mode: CopyMode = CopyMode.DEFAULT
    force: bool = False


# ---- docker build wrapper (mockable for tests) ---------------------------


def _default_docker_build(context: Path, tag: str) -> None:
    """Invoke ``docker build -t <tag> <context>``. Raises CalledProcessError on failure."""
    subprocess.run(
        ["docker", "build", "-t", tag, str(context)],
        check=True,
        capture_output=True,
    )


DockerBuildFn = Callable[[Path, str], None]


# ---- core --------------------------------------------------------------


def _record_imported_files(
    manifest: ImportManifest, outcomes: Iterable[CopyOutcome]
) -> None:
    for outcome in outcomes:
        if outcome.status == "imported":
            manifest.imported.append(
                ImportedEntry(
                    src=outcome.src,
                    dst=outcome.dst,
                    sha256=outcome.sha256,
                    bytes=outcome.bytes,
                    mode=outcome.mode,
                )
            )
        elif outcome.status == "skipped":
            manifest.skipped.append(
                SkippedEntry(src=outcome.src, dst=outcome.dst, reason=outcome.reason or "")
            )


def _ensure_qemu_cdrom_symlink(item: MigrationItem) -> None:
    """Create the stable ``cdrom.iso`` symlink in the destination dir."""
    boot_disk = item.meta.get("boot_disk")
    if not isinstance(boot_disk, str) or not boot_disk:
        return
    link_path = item.dst_dir / "cdrom.iso"
    if link_path.is_symlink() or link_path.exists():
        # Idempotent: replace if pointing at a different name; leave alone otherwise.
        if link_path.is_symlink() and link_path.readlink().name == boot_disk:
            return
        link_path.unlink()
    link_path.symlink_to(boot_disk)


def _migrate_files(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> bool:
    """Copy every file pair on ``item.files``. Returns True if every file landed."""
    outcomes: list[CopyOutcome] = []
    for src, dst in item.files:
        try:
            outcome = perform_copy(src, dst, mode=options.mode, force=options.force)
        except CopyEngineError as exc:
            manifest.errors.append(ErrorEntry(path=str(src), error=str(exc)))
            _logger.warning(
                "migrate.copy_failed", extra={"src": str(src), "dst": str(dst), "error": str(exc)}
            )
            return False
        outcomes.append(outcome)
    _record_imported_files(manifest, outcomes)
    return True


def migrate_qemu(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> None:
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    if not _migrate_files(item, options, manifest):
        return
    _ensure_qemu_cdrom_symlink(item)


def migrate_dynamips(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> None:
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    _migrate_files(item, options, manifest)


def migrate_iol(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> None:
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    if not _migrate_files(item, options, manifest):
        return
    if not item.meta.get("iourc_present"):
        manifest.templates.append(
            TemplateEntry(
                name=item.image_key,
                status="needs-manual-review",
                reason="iol image found but iourc license file is missing",
            )
        )


def migrate_docker(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
    docker_build: DockerBuildFn,
) -> None:
    """Run ``docker build`` and emit the ``image.txt`` marker."""
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    image_tag = str(item.meta.get("image_tag", f"nova-ve-{item.image_key}:latest"))
    build_context = Path(str(item.meta.get("build_context", item.src_dir)))

    try:
        docker_build(build_context, image_tag)
    except Exception as exc:  # noqa: BLE001 — surface any docker error to manifest
        manifest.errors.append(
            ErrorEntry(path=str(build_context), error=f"docker build failed: {exc}")
        )
        return

    marker = item.dst_dir / "image.txt"
    marker.write_text(image_tag + "\n")
    manifest.imported.append(
        ImportedEntry(
            src=str(build_context),
            dst=str(marker),
            sha256="",
            bytes=marker.stat().st_size,
            mode=options.mode.value,
        )
    )


# ---- top-level entry ----------------------------------------------------


def run_migration(
    items: list[MigrationItem],
    *,
    options: MigrateOptions,
    manifest: ImportManifest,
    owner: AppOwner | None = None,
    docker_build: DockerBuildFn | None = None,
    dest_root: Path | None = None,
) -> None:
    """Migrate every item; record outcomes on ``manifest``.

    If ``owner`` is provided, every destination dir / file gets owner+perm
    fixup applied. If ``dest_root`` is provided, the entire dest tree is
    re-walked for fixup at the end (catches the pre-existing root-owned
    ``<dest>/qemu/`` parent dir bug).
    """
    docker_build = docker_build or _default_docker_build

    for item in items:
        if item.kind == KIND_QEMU:
            migrate_qemu(item, options, manifest)
        elif item.kind == KIND_DYNAMIPS:
            migrate_dynamips(item, options, manifest)
        elif item.kind == KIND_IOL:
            migrate_iol(item, options, manifest)
        elif item.kind == KIND_DOCKER:
            migrate_docker(item, options, manifest, docker_build)
        else:  # pragma: no cover — defensive
            manifest.errors.append(
                ErrorEntry(path=str(item.src_dir), error=f"unknown kind: {item.kind}")
            )

    if owner is not None:
        if dest_root is not None and dest_root.exists():
            fixup_tree(dest_root, owner)
        else:
            for item in items:
                if item.dst_dir.exists():
                    fixup_tree(item.dst_dir, owner)
