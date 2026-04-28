# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for instance-id file resolution and naming helpers."""

from __future__ import annotations

import re

import pytest

import app.services.host_net as host_net
from app.services.host_net import _lab_hash


def test_file_override_wins(monkeypatch, tmp_path) -> None:
    instance_dir = tmp_path / "dir"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("dir-instance\n", encoding="ascii")
    instance_file = tmp_path / "instance-id-file"
    instance_file.write_text("file-instance\n", encoding="ascii")

    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_FILE", str(instance_file))
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))

    assert host_net.get_instance_id() == "file-instance"


def test_directory_override_is_used_when_file_override_unset(monkeypatch, tmp_path) -> None:
    instance_dir = tmp_path / "dir"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("dir-instance\n", encoding="ascii")

    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_FILE", raising=False)
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))

    assert host_net.get_instance_id() == "dir-instance"


def test_default_path_is_used_when_env_unset(monkeypatch, tmp_path) -> None:
    default_dir = tmp_path / "etc-nova-ve"
    default_dir.mkdir()
    (default_dir / "instance_id").write_text("default-instance\n", encoding="ascii")

    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_FILE", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_DIR", raising=False)
    monkeypatch.setattr(host_net, "_INSTANCE_DIR_DEFAULT", str(default_dir))

    assert host_net.get_instance_id() == "default-instance"


def test_missing_instance_file_raises(monkeypatch, tmp_path) -> None:
    missing_file = tmp_path / "missing-instance-id"

    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_FILE", str(missing_file))
    monkeypatch.delenv("NOVA_VE_INSTANCE_DIR", raising=False)

    with pytest.raises(host_net.HostNetInstanceIdMissing, match=str(missing_file)):
        host_net.get_instance_id()


def test_empty_instance_file_raises(monkeypatch, tmp_path) -> None:
    instance_file = tmp_path / "instance-id-file"
    instance_file.write_text("   \n", encoding="ascii")

    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_FILE", str(instance_file))
    monkeypatch.delenv("NOVA_VE_INSTANCE_DIR", raising=False)

    with pytest.raises(host_net.HostNetInstanceIdMissing, match="empty"):
        host_net.get_instance_id()


def test_lab_hash_is_deterministic() -> None:
    assert _lab_hash("lab-abc", "inst-xyz") == _lab_hash("lab-abc", "inst-xyz")


def test_lab_hash_changes_when_instance_changes() -> None:
    assert _lab_hash("same-lab", "instance-A") != _lab_hash("same-lab", "instance-B")


def test_lab_hash_fits_16_bits() -> None:
    for lab_id, instance_id in [("lab-abc", "inst-xyz"), ("x" * 64, "y" * 64), ("", "")]:
        h = _lab_hash(lab_id, instance_id)
        assert 0 <= h <= 0xFFFF


def test_bridge_name_fits_14_chars(monkeypatch, tmp_path) -> None:
    instance_dir = tmp_path / "dir"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("test-instance-id\n", encoding="ascii")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))

    name = host_net.bridge_name("some-lab-id", 99999)

    assert len(name) <= 14
    assert re.fullmatch(r"nove[0-9a-f]{4}n[0-9]+", name)


def test_tap_name_fits_14_chars(monkeypatch, tmp_path) -> None:
    instance_dir = tmp_path / "dir"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("test-instance-id\n", encoding="ascii")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))

    name = host_net.tap_name("some-lab-id", 999, 99)

    assert len(name) <= 14
    assert name.startswith("nve")
