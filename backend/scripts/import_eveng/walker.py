"""Walker for the EVE-NG importer (#184).

Enumerates the four addon directories nova-ve cares about::

    /opt/unetlab/addons/qemu/<vendor-ver>/{*.qcow2, cdrom.iso, ...}
    /opt/unetlab/addons/dynamips/<image>.image
    /opt/unetlab/addons/iol/bin/<image>.bin   (+ iourc license)
    /opt/unetlab/addons/docker/<image>/Dockerfile

Each yields one or more :class:`MigrationItem` records describing the
source / destination layout. The actual byte-copy + sha256 verify lives in
``copy_engine.py``; vendor-specific deliverables (qemu boot-disk symlink,
docker ``image.txt`` marker) live in the per-kind copiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

KIND_QEMU = "qemu"
KIND_DYNAMIPS = "dynamips"
KIND_IOL = "iol"
KIND_DOCKER = "docker"


@dataclass
class MigrationItem:
    """One migration unit (template directory or single file).

    ``files`` is the concrete list of (src, dst) pairs to copy. ``meta``
    carries kind-specific data the copier may need (e.g. the chosen boot disk
    for qemu, the license-file path for iol).
    """

    kind: str
    image_key: str
    src_dir: Path
    dst_dir: Path
    files: list[tuple[Path, Path]] = field(default_factory=list)
    meta: dict[str, object] = field(default_factory=dict)


# Boot-disk precedence per #184 acceptance criteria.
_QEMU_BOOT_DISK_PRECEDENCE: tuple[str, ...] = (
    "cdrom.iso",
    "virtioa.qcow2",
    "hda.qcow2",
)


def _pick_qemu_boot_disk(disk_files: list[Path]) -> Path | None:
    """Pick the qemu boot disk per #184 precedence rules.

    Order: ``cdrom.iso`` > ``virtioa.qcow2`` > ``hda.qcow2`` > first ``*.qcow2``
    lexicographically. Returns ``None`` if no candidate is found.
    """
    by_name = {p.name: p for p in disk_files}
    for preferred in _QEMU_BOOT_DISK_PRECEDENCE:
        if preferred in by_name:
            return by_name[preferred]
    qcow2 = sorted(p for p in disk_files if p.suffix == ".qcow2")
    return qcow2[0] if qcow2 else None


def walk_qemu(source_root: Path, dest_root: Path) -> list[MigrationItem]:
    """Enumerate ``<source>/addons/qemu/<vendor-ver>/`` directories."""
    qemu_src = source_root / "addons" / "qemu"
    if not qemu_src.is_dir():
        return []

    items: list[MigrationItem] = []
    for vendor_dir in sorted(qemu_src.iterdir()):
        if not vendor_dir.is_dir():
            continue
        image_key = vendor_dir.name
        dst_dir = dest_root / "qemu" / image_key
        files: list[tuple[Path, Path]] = []
        disk_files: list[Path] = []
        for entry in sorted(vendor_dir.iterdir()):
            if entry.is_file():
                files.append((entry, dst_dir / entry.name))
                disk_files.append(entry)
        boot_disk = _pick_qemu_boot_disk(disk_files)
        meta: dict[str, object] = {}
        if boot_disk is not None:
            meta["boot_disk"] = boot_disk.name
        items.append(
            MigrationItem(
                kind=KIND_QEMU,
                image_key=image_key,
                src_dir=vendor_dir,
                dst_dir=dst_dir,
                files=files,
                meta=meta,
            )
        )
    return items


def walk_dynamips(source_root: Path, dest_root: Path) -> list[MigrationItem]:
    """Enumerate ``<source>/addons/dynamips/<image>.image`` files."""
    dyn_src = source_root / "addons" / "dynamips"
    if not dyn_src.is_dir():
        return []

    items: list[MigrationItem] = []
    for entry in sorted(dyn_src.iterdir()):
        if not entry.is_file() or entry.suffix != ".image":
            continue
        image_key = entry.stem
        dst_dir = dest_root / "dynamips" / image_key
        items.append(
            MigrationItem(
                kind=KIND_DYNAMIPS,
                image_key=image_key,
                src_dir=dyn_src,
                dst_dir=dst_dir,
                files=[(entry, dst_dir / entry.name)],
            )
        )
    return items


def walk_iol(source_root: Path, dest_root: Path) -> list[MigrationItem]:
    """Enumerate ``<source>/addons/iol/bin/<image>.bin`` files (+ ``iourc``)."""
    iol_src = source_root / "addons" / "iol" / "bin"
    if not iol_src.is_dir():
        return []

    iourc = iol_src / "iourc"
    items: list[MigrationItem] = []
    for entry in sorted(iol_src.iterdir()):
        if not entry.is_file() or entry.suffix != ".bin":
            continue
        image_key = entry.stem
        dst_dir = dest_root / "iol" / image_key
        files: list[tuple[Path, Path]] = [(entry, dst_dir / entry.name)]
        meta: dict[str, object] = {"iourc_present": iourc.is_file()}
        if iourc.is_file():
            files.append((iourc, dst_dir / "iourc"))
        items.append(
            MigrationItem(
                kind=KIND_IOL,
                image_key=image_key,
                src_dir=iol_src,
                dst_dir=dst_dir,
                files=files,
                meta=meta,
            )
        )
    return items


def walk_docker(source_root: Path, dest_root: Path) -> list[MigrationItem]:
    """Enumerate ``<source>/addons/docker/<image>/Dockerfile`` directories.

    For docker, the migration unit is the build context directory (which contains
    a Dockerfile). The copier invokes ``docker build`` and writes ``image.txt``
    instead of copying files; ``files`` is therefore empty here and meta carries
    the build context path.
    """
    docker_src = source_root / "addons" / "docker"
    if not docker_src.is_dir():
        return []

    items: list[MigrationItem] = []
    for ctx in sorted(docker_src.iterdir()):
        if not ctx.is_dir() or not (ctx / "Dockerfile").is_file():
            continue
        image_key = ctx.name
        dst_dir = dest_root / "docker" / image_key
        items.append(
            MigrationItem(
                kind=KIND_DOCKER,
                image_key=image_key,
                src_dir=ctx,
                dst_dir=dst_dir,
                files=[],
                meta={"build_context": str(ctx), "image_tag": f"nova-ve-{image_key}:latest"},
            )
        )
    return items


def walk_all(source_root: Path, dest_root: Path) -> list[MigrationItem]:
    """Enumerate every supported addon directory under ``source_root``."""
    return [
        *walk_qemu(source_root, dest_root),
        *walk_dynamips(source_root, dest_root),
        *walk_iol(source_root, dest_root),
        *walk_docker(source_root, dest_root),
    ]
