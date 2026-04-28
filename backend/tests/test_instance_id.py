# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""
Regression tests for US-100: per-instance ID provisioning.

Covers:
  - First-run (file missing) + env override → returns env value with WARNING
  - Second-run (file present) → loads from file (deterministic)
  - File present + env var set (no override flag) → file wins, no warning
  - File missing + no env → raises HostNetInstanceIdMissing
  - File missing + env-only (no OVERRIDE_OK) → raises HostNetInstanceIdMissing
  - _lab_hash is deterministic across calls
  - _lab_hash returns a 16-bit value (0 ≤ x ≤ 0xFFFF)
  - bridge_name fits the 14-char budget for edge-case network_id=99999
  - tap_name fits the 14-char budget for edge-case node_id=999, iface=99
"""

import importlib
import logging
import os

import pytest

import app.services.host_net as host_net
from app.services.host_net import (
    HostNetInstanceIdMissing,
    _lab_hash,
    bridge_name,
    get_instance_id,
    tap_name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_and_patch(monkeypatch, tmp_path, instance_dir=None, env_id=None, override_ok=None):
    """Patch NOVA_VE_INSTANCE_DIR and optional env vars, then reload the module."""
    if instance_dir is not None:
        monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    else:
        monkeypatch.delenv("NOVA_VE_INSTANCE_DIR", raising=False)

    if env_id is not None:
        monkeypatch.setenv("NOVA_VE_INSTANCE_ID", env_id)
    else:
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)

    if override_ok is not None:
        monkeypatch.setenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", override_ok)
    else:
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)

    importlib.reload(host_net)


# ---------------------------------------------------------------------------
# get_instance_id — file-based resolution
# ---------------------------------------------------------------------------


def test_file_present_returns_file_value(monkeypatch, tmp_path):
    """File present and non-empty → returns file value."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("test-uuid-1234\n")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    result = host_net.get_instance_id()
    assert result == "test-uuid-1234"


def test_file_present_second_run_is_identical(monkeypatch, tmp_path):
    """Second call with same file → same value (idempotent load)."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("stable-uuid-abcd")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    assert host_net.get_instance_id() == host_net.get_instance_id()


def test_file_missing_no_env_raises(monkeypatch, tmp_path):
    """File missing + no env vars → raises HostNetInstanceIdMissing."""
    instance_dir = tmp_path / "no-such-dir"

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    with pytest.raises(host_net.HostNetInstanceIdMissing):
        host_net.get_instance_id()


def test_file_missing_env_only_no_override_flag_raises(monkeypatch, tmp_path):
    """Env var alone (no OVERRIDE_OK=1) → still raises, even if NOVA_VE_INSTANCE_ID set."""
    instance_dir = tmp_path / "no-such-dir"

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.setenv("NOVA_VE_INSTANCE_ID", "some-env-uuid")
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    with pytest.raises(host_net.HostNetInstanceIdMissing):
        host_net.get_instance_id()


def test_file_missing_env_with_override_flag_honored(monkeypatch, tmp_path, caplog):
    """Env var + OVERRIDE_OK=1 (no file) → honored, WARNING emitted."""
    instance_dir = tmp_path / "no-such-dir"

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.setenv("NOVA_VE_INSTANCE_ID", "override-uuid-5678")
    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", "1")
    importlib.reload(host_net)

    with caplog.at_level(logging.WARNING, logger="nova-ve"):
        result = host_net.get_instance_id()

    assert result == "override-uuid-5678"
    assert any("WARNING" in r.message for r in caplog.records)


def test_file_present_env_set_no_override_flag_file_wins_no_warning(
    monkeypatch, tmp_path, caplog
):
    """File present + env var set (no OVERRIDE_OK) → file wins, no WARNING logged."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("file-uuid-wins")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.setenv("NOVA_VE_INSTANCE_ID", "env-uuid-ignored")
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    with caplog.at_level(logging.WARNING, logger="nova-ve"):
        result = host_net.get_instance_id()

    assert result == "file-uuid-wins"
    assert not any("WARNING" in r.message for r in caplog.records)


def test_file_empty_falls_through_to_env_override(monkeypatch, tmp_path):
    """Empty file + env override + OVERRIDE_OK=1 → env value used."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("   \n")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.setenv("NOVA_VE_INSTANCE_ID", "env-uuid-fallback")
    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", "1")
    importlib.reload(host_net)

    result = host_net.get_instance_id()
    assert result == "env-uuid-fallback"


# ---------------------------------------------------------------------------
# _lab_hash — determinism and bit-width
# ---------------------------------------------------------------------------


def test_lab_hash_is_deterministic():
    """Same inputs always produce the same hash."""
    h1 = _lab_hash("lab-abc", "inst-xyz")
    h2 = _lab_hash("lab-abc", "inst-xyz")
    assert h1 == h2


def test_lab_hash_different_instance_ids_produce_different_hashes():
    """Different instance IDs → different hashes (collision resistance)."""
    h1 = _lab_hash("same-lab", "instance-A")
    h2 = _lab_hash("same-lab", "instance-B")
    assert h1 != h2


def test_lab_hash_different_lab_ids_produce_different_hashes():
    """Different lab IDs on the same instance → different hashes."""
    h1 = _lab_hash("lab-1", "same-instance")
    h2 = _lab_hash("lab-2", "same-instance")
    assert h1 != h2


def test_lab_hash_fits_16_bits():
    """Hash value is always in [0, 0xFFFF]."""
    for lab_id, instance_id in [
        ("lab-abc", "inst-xyz"),
        ("x" * 64, "y" * 64),
        ("", ""),
    ]:
        h = _lab_hash(lab_id, instance_id)
        assert 0 <= h <= 0xFFFF, f"hash {h} out of 16-bit range"


# ---------------------------------------------------------------------------
# bridge_name / tap_name — IFNAMSIZ budget
# ---------------------------------------------------------------------------


def test_bridge_name_max_network_id_fits_14_chars(monkeypatch, tmp_path):
    """bridge_name with network_id=99999 must be ≤14 chars."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("test-instance-id")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    name = host_net.bridge_name("some-lab-id", 99999)
    assert len(name) <= 14, f"bridge_name too long: {name!r} ({len(name)} chars)"
    assert name.startswith("nove")


def test_tap_name_max_node_iface_fits_14_chars(monkeypatch, tmp_path):
    """tap_name with node_id=999, iface=99 must be ≤14 chars."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("test-instance-id")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    name = host_net.tap_name("some-lab-id", 999, 99)
    assert len(name) <= 14, f"tap_name too long: {name!r} ({len(name)} chars)"
    assert name.startswith("nve")


def test_bridge_name_format(monkeypatch, tmp_path):
    """bridge_name produces the expected prefix+hash+suffix structure."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("fixed-instance")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    name = host_net.bridge_name("my-lab", 7)
    # nove + 4 hex chars + n + network_id
    import re
    assert re.fullmatch(r"nove[0-9a-f]{4}n[0-9]+", name), f"unexpected format: {name!r}"


def test_tap_name_format(monkeypatch, tmp_path):
    """tap_name produces the expected prefix+hash+suffix structure."""
    instance_dir = tmp_path / "etc-nova-ve"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("fixed-instance")

    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    importlib.reload(host_net)

    name = host_net.tap_name("my-lab", 3, 1)
    import re
    assert re.fullmatch(r"nve[0-9a-f]{4}d[0-9]+i[0-9]+", name), f"unexpected format: {name!r}"
