"""Behavior regressions for typed QEMU disk and ISO lookup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService


def _service(images_dir: Path) -> NodeRuntimeService:
    svc = NodeRuntimeService.__new__(NodeRuntimeService)
    svc.settings = SimpleNamespace(IMAGES_DIR=images_dir, QEMU_IMG_BINARY="unused-qemu-img")
    return svc


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")


def _resolve(svc: NodeRuntimeService, *, iso: bool) -> Path | None:
    node = {"image": "appliance"}
    return svc._resolve_qemu_iso(node) if iso else svc._resolve_qemu_image(node)


@pytest.mark.parametrize(
    ("iso", "canonical", "alternative"),
    [
        pytest.param(False, "hda.qcow2", "boot.qcow2", id="disk"),
        pytest.param(True, "cdrom.iso", "boot.iso", id="iso"),
    ],
)
def test_typed_canonical_media_resolves(tmp_path: Path, iso: bool, canonical: str, alternative: str) -> None:
    images = tmp_path / "images"
    expected = images / "qemu" / "appliance" / canonical
    _write_image(expected)

    assert _resolve(_service(images), iso=iso) == expected


@pytest.mark.parametrize(
    ("iso", "canonical"),
    [
        pytest.param(False, "hda.qcow2", id="disk"),
        pytest.param(True, "cdrom.iso", id="iso"),
    ],
)
def test_untyped_canonical_media_is_ignored(tmp_path: Path, iso: bool, canonical: str) -> None:
    images = tmp_path / "images"
    _write_image(images / "appliance" / canonical)

    assert _resolve(_service(images), iso=iso) is None


@pytest.mark.parametrize(
    ("iso", "alternative"),
    [
        pytest.param(False, "boot.qcow2", id="disk"),
        pytest.param(True, "boot.iso", id="iso"),
    ],
)
def test_untyped_glob_media_is_ignored(tmp_path: Path, iso: bool, alternative: str) -> None:
    images = tmp_path / "images"
    _write_image(images / "appliance" / alternative)

    assert _resolve(_service(images), iso=iso) is None


@pytest.mark.parametrize(
    ("iso", "canonical", "alternative"),
    [
        pytest.param(False, "hda.qcow2", "boot.qcow2", id="disk"),
        pytest.param(True, "cdrom.iso", "boot.iso", id="iso"),
    ],
)
def test_typed_canonical_media_has_precedence(
    tmp_path: Path, iso: bool, canonical: str, alternative: str
) -> None:
    images = tmp_path / "images"
    expected = images / "qemu" / "appliance" / canonical
    _write_image(expected)
    _write_image(images / "qemu" / "appliance" / alternative)
    _write_image(images / "appliance" / canonical)

    assert _resolve(_service(images), iso=iso) == expected


@pytest.mark.parametrize(
    ("iso", "extension"),
    [
        pytest.param(False, ".qcow2", id="disk"),
        pytest.param(True, ".iso", id="iso"),
    ],
)
def test_typed_glob_fallback_is_sorted(tmp_path: Path, iso: bool, extension: str) -> None:
    images = tmp_path / "images"
    typed = images / "qemu" / "appliance"
    _write_image(typed / f"z-last{extension}")
    expected = typed / f"a-first{extension}"
    _write_image(expected)

    assert _resolve(_service(images), iso=iso) == expected


@pytest.mark.parametrize(
    "untyped_filename",
    [
        pytest.param(None, id="empty"),
        pytest.param("hda.qcow2", id="untyped-canonical"),
        pytest.param("boot.qcow2", id="untyped-glob"),
    ],
)
def test_missing_disk_raises_without_creating_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, untyped_filename: str | None
) -> None:
    images = tmp_path / "images"
    if untyped_filename is not None:
        _write_image(images / "appliance" / untyped_filename)
    svc = _service(images)
    monkeypatch.setattr(svc, "_resolve_binary", lambda _binary: None)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(NodeRuntimeError, match="^QEMU base image not found for node image: appliance$"):
        svc._ensure_qemu_overlay(work_dir, {"image": "appliance"})

    assert not (work_dir / "virtioa.qcow2").exists()
