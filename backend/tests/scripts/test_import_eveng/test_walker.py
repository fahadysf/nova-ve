"""Tests for the walker (#184)."""

from __future__ import annotations

from pathlib import Path

from scripts.import_eveng.walker import (
    KIND_DOCKER,
    KIND_DYNAMIPS,
    KIND_IOL,
    KIND_QEMU,
    walk_all,
    walk_docker,
    walk_dynamips,
    walk_iol,
    walk_qemu,
)


def _seed(path: Path, contents: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


def test_walker_returns_empty_for_empty_source(tmp_path: Path) -> None:
    assert walk_all(tmp_path / "empty", tmp_path / "dst") == []


def test_qemu_boot_disk_precedence_cdrom_first(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "vyos-1.4"
    _seed(vendor / "cdrom.iso")
    _seed(vendor / "virtioa.qcow2")
    _seed(vendor / "hda.qcow2")

    items = walk_qemu(src, dst)
    assert len(items) == 1
    assert items[0].kind == KIND_QEMU
    assert items[0].image_key == "vyos-1.4"
    assert items[0].meta["boot_disk"] == "cdrom.iso"


def test_qemu_boot_disk_precedence_falls_through(tmp_path: Path) -> None:
    """virtioa.qcow2 wins over hda.qcow2 when no cdrom.iso present."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "csr1000v"
    _seed(vendor / "virtioa.qcow2")
    _seed(vendor / "hda.qcow2")

    items = walk_qemu(src, dst)
    assert items[0].meta["boot_disk"] == "virtioa.qcow2"


def test_qemu_boot_disk_falls_to_first_qcow2_lex(tmp_path: Path) -> None:
    """First *.qcow2 lex when none of the named precedences match."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "exotic"
    _seed(vendor / "z-disk.qcow2")
    _seed(vendor / "a-disk.qcow2")

    items = walk_qemu(src, dst)
    assert items[0].meta["boot_disk"] == "a-disk.qcow2"


def test_qemu_walker_emits_dst_paths_under_qemu_subtree(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    vendor = src / "addons" / "qemu" / "vyos-1.4"
    _seed(vendor / "hda.qcow2")
    _seed(vendor / "iso.dat")

    items = walk_qemu(src, dst)
    item = items[0]
    assert item.dst_dir == dst / "qemu" / "vyos-1.4"
    src_paths = sorted(s.name for s, _ in item.files)
    assert src_paths == ["hda.qcow2", "iso.dat"]
    for src_p, dst_p in item.files:
        assert dst_p == item.dst_dir / src_p.name


def test_dynamips_walker(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _seed(src / "addons" / "dynamips" / "c7200-image.image")
    _seed(src / "addons" / "dynamips" / "ignored.txt")

    items = walk_dynamips(src, dst)
    assert len(items) == 1
    item = items[0]
    assert item.kind == KIND_DYNAMIPS
    assert item.image_key == "c7200-image"
    assert item.dst_dir == dst / "dynamips" / "c7200-image"
    assert item.files == [
        (src / "addons" / "dynamips" / "c7200-image.image", item.dst_dir / "c7200-image.image"),
    ]


def test_iol_walker_with_iourc(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    iol_bin = src / "addons" / "iol" / "bin"
    _seed(iol_bin / "i86bi-linux-l3.bin")
    _seed(iol_bin / "iourc")

    items = walk_iol(src, dst)
    assert len(items) == 1
    item = items[0]
    assert item.kind == KIND_IOL
    assert item.image_key == "i86bi-linux-l3"
    assert item.meta["iourc_present"] is True
    assert (iol_bin / "iourc", item.dst_dir / "iourc") in item.files


def test_iol_walker_without_iourc_records_missing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    iol_bin = src / "addons" / "iol" / "bin"
    _seed(iol_bin / "i86bi-linux-l2.bin")

    items = walk_iol(src, dst)
    assert items[0].meta["iourc_present"] is False
    assert len(items[0].files) == 1


def test_docker_walker_only_takes_dirs_with_dockerfile(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _seed(src / "addons" / "docker" / "alpine-telnet" / "Dockerfile", b"FROM alpine\n")
    (src / "addons" / "docker" / "no-dockerfile").mkdir(parents=True)
    _seed(src / "addons" / "docker" / "alpine-telnet" / "entrypoint.sh", b"#!/bin/sh\n")

    items = walk_docker(src, dst)
    assert len(items) == 1
    item = items[0]
    assert item.kind == KIND_DOCKER
    assert item.image_key == "alpine-telnet"
    assert item.meta["image_tag"] == "nova-ve-alpine-telnet:latest"
    assert item.files == []  # docker copier builds + writes image.txt; no file copies


def test_walk_all_combines_every_kind(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _seed(src / "addons" / "qemu" / "vyos-1.4" / "hda.qcow2")
    _seed(src / "addons" / "dynamips" / "c7200.image")
    _seed(src / "addons" / "iol" / "bin" / "iol.bin")
    _seed(src / "addons" / "docker" / "alpine-telnet" / "Dockerfile")

    kinds = sorted({item.kind for item in walk_all(src, dst)})
    assert kinds == [KIND_DOCKER, KIND_DYNAMIPS, KIND_IOL, KIND_QEMU]
