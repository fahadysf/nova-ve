"""Tests for the legacy un-nested IMAGES_DIR layout deprecation log (#184).

The two methods at backend/app/services/node_runtime_service.py — _resolve_qemu_image
(lines 4403-4422) and _resolve_qemu_iso (lines 4424-4443) — both have an un-nested
fallback path. When that fallback resolves a file/dir, a structured deprecation
log is emitted exactly once per (kind, path) pair per process. Tests assert on
the LogRecord.deprecation attribute (structured), NOT on log message substrings.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import node_runtime_service as nrs
from app.services.node_runtime_service import NodeRuntimeService


@pytest.fixture(autouse=True)
def _reset_legacy_layout_cache():
    """Each test gets a fresh once-per-process cache."""
    nrs._LEGACY_LAYOUT_LOGGED.clear()
    yield
    nrs._LEGACY_LAYOUT_LOGGED.clear()


def _service(images_dir: Path) -> NodeRuntimeService:
    """Construct a NodeRuntimeService with only the IMAGES_DIR setting we need."""
    svc = NodeRuntimeService.__new__(NodeRuntimeService)
    svc.settings = SimpleNamespace(IMAGES_DIR=images_dir)
    return svc


def _write_image(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------- _resolve_qemu_image -----------------------------------------


def test_nested_image_layout_does_not_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    images = tmp_path / "images"
    _write_image(images / "qemu" / "csr1000v" / "hda.qcow2")

    svc = _service(images)
    with caplog.at_level(logging.DEBUG, logger="nova-ve.legacy_image_layout"):
        result = svc._resolve_qemu_image({"image": "csr1000v"})
    assert result == images / "qemu" / "csr1000v" / "hda.qcow2"
    assert all(getattr(r, "deprecation", False) is False for r in caplog.records)


def test_legacy_image_layout_emits_structured_deprecation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    images = tmp_path / "images"
    _write_image(images / "csr1000v" / "hda.qcow2")  # legacy un-nested

    svc = _service(images)
    with caplog.at_level(logging.WARNING, logger="nova-ve.legacy_image_layout"):
        result = svc._resolve_qemu_image({"image": "csr1000v"})

    assert result == images / "csr1000v" / "hda.qcow2"
    deprecation_records = [r for r in caplog.records if getattr(r, "deprecation", False)]
    assert len(deprecation_records) == 1
    rec = deprecation_records[0]
    assert rec.deprecation is True
    assert rec.fallback_kind == "qemu_image_hda"
    assert rec.fallback_path == str(result)


def test_legacy_image_dir_glob_fallback_emits_deprecation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When neither hda.qcow2 nor cdrom.iso are found but the legacy *.qcow2 glob hits."""
    images = tmp_path / "images"
    _write_image(images / "exotic" / "boot-disk.qcow2")  # legacy dir, non-standard name

    svc = _service(images)
    with caplog.at_level(logging.WARNING, logger="nova-ve.legacy_image_layout"):
        result = svc._resolve_qemu_image({"image": "exotic"})

    assert result == images / "exotic" / "boot-disk.qcow2"
    deprecation = [r for r in caplog.records if getattr(r, "deprecation", False)]
    assert len(deprecation) == 1
    assert deprecation[0].fallback_kind == "qemu_image_dir"


def test_deprecation_is_once_per_process_per_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    images = tmp_path / "images"
    _write_image(images / "csr1000v" / "hda.qcow2")
    svc = _service(images)

    with caplog.at_level(logging.WARNING, logger="nova-ve.legacy_image_layout"):
        svc._resolve_qemu_image({"image": "csr1000v"})
        svc._resolve_qemu_image({"image": "csr1000v"})  # second hit must be silent
        svc._resolve_qemu_image({"image": "csr1000v"})

    deprecation = [r for r in caplog.records if getattr(r, "deprecation", False)]
    assert len(deprecation) == 1, "deprecation log must fire only once per process per path"


# ---------- _resolve_qemu_iso -------------------------------------------


def test_legacy_iso_layout_emits_structured_deprecation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    images = tmp_path / "images"
    _write_image(images / "vyos-1.4" / "cdrom.iso")  # legacy un-nested

    svc = _service(images)
    with caplog.at_level(logging.WARNING, logger="nova-ve.legacy_image_layout"):
        result = svc._resolve_qemu_iso({"image": "vyos-1.4"})

    assert result == images / "vyos-1.4" / "cdrom.iso"
    deprecation = [r for r in caplog.records if getattr(r, "deprecation", False)]
    assert len(deprecation) == 1
    assert deprecation[0].fallback_kind == "qemu_iso_cdrom"


def test_legacy_iso_dir_glob_fallback_emits_deprecation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    images = tmp_path / "images"
    _write_image(images / "vyos-1.4" / "boot.iso")

    svc = _service(images)
    with caplog.at_level(logging.WARNING, logger="nova-ve.legacy_image_layout"):
        result = svc._resolve_qemu_iso({"image": "vyos-1.4"})

    assert result == images / "vyos-1.4" / "boot.iso"
    deprecation = [r for r in caplog.records if getattr(r, "deprecation", False)]
    assert len(deprecation) == 1
    assert deprecation[0].fallback_kind == "qemu_iso_dir"


def test_image_and_iso_are_independently_cached(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A warn for the image fallback does not silence a separate iso fallback."""
    images = tmp_path / "images"
    _write_image(images / "csr" / "hda.qcow2")
    _write_image(images / "csr" / "cdrom.iso")

    svc = _service(images)
    with caplog.at_level(logging.WARNING, logger="nova-ve.legacy_image_layout"):
        svc._resolve_qemu_image({"image": "csr"})
        svc._resolve_qemu_iso({"image": "csr"})

    deprecation = [r for r in caplog.records if getattr(r, "deprecation", False)]
    kinds = sorted(r.fallback_kind for r in deprecation)
    assert kinds == ["qemu_image_hda", "qemu_iso_cdrom"]
