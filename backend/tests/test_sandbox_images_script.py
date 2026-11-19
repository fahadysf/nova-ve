# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""sandbox_images script (#253): one dummy image per builtin qemu/iol/
dynamips template, idempotent, never overwrites existing files."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.sandbox_images import main as sandbox_images_main


@pytest.fixture()
def run_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def _run() -> int:
        monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "images"))
        monkeypatch.setenv("TEMPLATES_DIR", str(Path(__file__).resolve().parents[1] / "templates"))
        return sandbox_images_main()

    return _run


def test_creates_dummy_per_template(run_script, tmp_path: Path) -> None:
    assert run_script() == 0
    images = tmp_path / "images"
    qemu = images / "qemu"
    assert qemu.is_dir()
    # veos.yml name "Arista vEOS" -> stem must contain the name + marker
    assert (qemu / "arista veos sandbox.qcow2").exists()
    assert (images / "iol" / "iol sandbox.bin").exists()
    assert (images / "dynamips" / "c7200 sandbox.image").exists()
    # every builtin qemu template display name has a matching dummy (two
    # templates share "Cisco Catalyst 8000V" — one file serves both)
    builtins_dir = Path(__file__).resolve().parents[1] / "templates" / "qemu"
    import yaml

    display_names = set()
    for path in builtins_dir.glob("*.yml"):
        data = yaml.safe_load(path.read_text())
        display_names.add(data["name"].strip().lower())
    assert display_names
    for name in display_names:
        assert (qemu / f"{name} sandbox.qcow2").exists(), name


def test_idempotent_and_never_overwrites(run_script, tmp_path: Path) -> None:
    images = tmp_path / "images"
    sentinel = images / "qemu" / "arista veos sandbox.qcow2"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"REAL IMAGE DATA")
    assert run_script() == 0
    assert sentinel.read_bytes() == b"REAL IMAGE DATA"
    assert run_script() == 0  # second run: all skipped, exit 0


def test_skips_paired_templates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tdir = tmp_path / "templates" / "qemu"
    tdir.mkdir(parents=True)
    (tdir / "paired.yml").write_text(
        "kind: paired\nname: Paired Thing\nnodes: []\nlinks: []\n"
    )
    (tdir / "plain.yml").write_text("type: qemu\nname: Plain Thing\n")
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("TEMPLATES_DIR", str(tmp_path / "templates"))
    assert sandbox_images_main() == 0
    out = tmp_path / "images" / "qemu"
    assert (out / "plain thing sandbox.qcow2").exists()
    assert not (out / "paired thing sandbox.qcow2").exists()
