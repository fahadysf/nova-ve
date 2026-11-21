# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``app.config.Settings``."""

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """``get_settings`` is ``lru_cache``-d; clear before/after each test so
    env-var mutations are not masked by a stale cached instance."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_discovery_cadence_default_is_30(monkeypatch):
    """Default value is 30 seconds when no env var is set."""
    monkeypatch.delenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", raising=False)
    monkeypatch.delenv("DISCOVERY_CADENCE_SECONDS", raising=False)
    settings = Settings()
    assert settings.DISCOVERY_CADENCE_SECONDS == 30
    assert settings.NAT_CLOUD_POOL == "10.255.0.0/16"


def test_discovery_cadence_env_override(monkeypatch):
    """``NOVA_VE_DISCOVERY_CADENCE_SECONDS=60`` is honored on construction."""
    monkeypatch.setenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", "60")
    settings = Settings()
    assert settings.DISCOVERY_CADENCE_SECONDS == 60


def test_discovery_cadence_below_minimum_raises(monkeypatch):
    """Values below 5 raise on startup (Pydantic ValidationError)."""
    monkeypatch.setenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", "4")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "DISCOVERY_CADENCE_SECONDS" in str(exc.value)


def test_discovery_cadence_above_maximum_raises(monkeypatch):
    """Values above 300 raise on startup (Pydantic ValidationError)."""
    monkeypatch.setenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", "301")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "DISCOVERY_CADENCE_SECONDS" in str(exc.value)


def test_discovery_cadence_at_boundaries(monkeypatch):
    """The clamp is inclusive on both ends — 5 and 300 must be accepted."""
    monkeypatch.setenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", "5")
    assert Settings().DISCOVERY_CADENCE_SECONDS == 5
    monkeypatch.setenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", "300")
    assert Settings().DISCOVERY_CADENCE_SECONDS == 300


def test_get_settings_reload_picks_up_env_change(monkeypatch):
    """``get_settings`` is cached, but ``cache_clear`` makes the next call
    pick up an updated env var — this is the live-reload contract that
    ``_discovery_loop`` relies on for in-flight cadence edits."""
    monkeypatch.setenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", "30")
    assert get_settings().DISCOVERY_CADENCE_SECONDS == 30
    monkeypatch.setenv("NOVA_VE_DISCOVERY_CADENCE_SECONDS", "45")
    # Stale cached value still wins until cleared.
    assert get_settings().DISCOVERY_CADENCE_SECONDS == 30
    get_settings.cache_clear()
    assert get_settings().DISCOVERY_CADENCE_SECONDS == 45


def test_nat_cloud_pool_env_override(monkeypatch):
    monkeypatch.setenv("NOVA_VE_NAT_CLOUD_POOL", "10.88.0.0/16")
    assert Settings().NAT_CLOUD_POOL == "10.88.0.0/16"


def test_nat_cloud_pool_too_small_raises(monkeypatch):
    monkeypatch.setenv("NOVA_VE_NAT_CLOUD_POOL", "10.88.0.0/25")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "NAT_CLOUD_POOL" in str(exc.value)


# ---------------------------------------------------------------------------
# Console port ranges (#276)
# ---------------------------------------------------------------------------
def test_console_port_range_defaults(monkeypatch):
    for key in (
        "CONSOLE_PORT_RANGE_TELNET",
        "NOVA_VE_CONSOLE_PORT_RANGE_TELNET",
        "CONSOLE_PORT_RANGE_VNC",
        "NOVA_VE_CONSOLE_PORT_RANGE_VNC",
        "CONSOLE_PORT_RANGE_RDP",
        "NOVA_VE_CONSOLE_PORT_RANGE_RDP",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.CONSOLE_PORT_RANGE_TELNET == "15000-20000"
    assert settings.CONSOLE_PORT_RANGE_VNC == "25000-30000"
    assert settings.CONSOLE_PORT_RANGE_RDP == "35000-40000"


def test_console_port_range_env_override(monkeypatch):
    monkeypatch.setenv("NOVA_VE_CONSOLE_PORT_RANGE_TELNET", "16000-17000")
    monkeypatch.setenv("CONSOLE_PORT_RANGE_VNC", "26000-27000")
    settings = Settings()
    assert settings.CONSOLE_PORT_RANGE_TELNET == "16000-17000"
    assert settings.CONSOLE_PORT_RANGE_VNC == "26000-27000"


def test_console_port_range_malformed_raises(monkeypatch):
    monkeypatch.setenv("NOVA_VE_CONSOLE_PORT_RANGE_TELNET", "16000")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "CONSOLE_PORT_RANGE_TELNET" in str(exc.value)


def test_console_port_range_out_of_bounds_raises(monkeypatch):
    monkeypatch.setenv("NOVA_VE_CONSOLE_PORT_RANGE_TELNET", "1023-2000")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("NOVA_VE_CONSOLE_PORT_RANGE_TELNET", "16000-16000")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("NOVA_VE_CONSOLE_PORT_RANGE_TELNET", "16000-65536")
    with pytest.raises(ValidationError):
        Settings()


def test_console_port_range_vnc_floor_raises(monkeypatch):
    """QEMU binds VNC via display arithmetic (port = 5900 + display), so the
    VNC range must start at 5900 or above."""
    monkeypatch.setenv("NOVA_VE_CONSOLE_PORT_RANGE_VNC", "4000-5000")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "CONSOLE_PORT_RANGE_VNC" in str(exc.value)


# ---------------------------------------------------------------------------
# Persisted overrides applied at boot (#265 fix; blocks #276 otherwise — the
# external-console host could never take effect through the settings UI)
# ---------------------------------------------------------------------------
def test_get_settings_applies_persisted_overrides(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))
    (tmp_path / "db_password").write_text("strong-db-password", encoding="ascii")
    (tmp_path / "system-settings.json").write_text(
        json.dumps(
            {
                "EXTERNAL_CONSOLE_HOST": "192.168.100.236",
                "SESSION_MAX_AGE": 9999,
                "NAT_CLOUD_POOL": "10.88.0.0/16",
            }
        )
    )

    settings = get_settings()

    assert settings.EXTERNAL_CONSOLE_HOST == "192.168.100.236"
    assert settings.SESSION_MAX_AGE == 9999
    assert settings.NAT_CLOUD_POOL == "10.88.0.0/16"


def test_get_settings_skips_invalid_persisted_overrides(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))
    (tmp_path / "db_password").write_text("strong-db-password", encoding="ascii")
    (tmp_path / "system-settings.json").write_text(
        json.dumps(
            {
                # Not in EDITABLE_SETTING_KEYS — ignored.
                "SECRET_KEY": "x" * 64,
                # Wrong type for an int field — skipped, not fatal.
                "SESSION_MAX_AGE": "not-a-number",
                "EXTERNAL_CONSOLE_HOST": "",
            }
        )
    )

    settings = get_settings()

    assert settings.EXTERNAL_CONSOLE_HOST == ""
    assert settings.SESSION_MAX_AGE == 14400
    assert settings.SECRET_KEY != "x" * 64


def test_get_settings_without_override_file_boots_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))
    (tmp_path / "db_password").write_text("strong-db-password", encoding="ascii")

    settings = get_settings()

    assert settings.EXTERNAL_CONSOLE_HOST == ""


def test_get_settings_resolves_missing_secrets_from_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))
    (tmp_path / "db_password").write_text("strong-db-password", encoding="ascii")

    settings = get_settings()

    assert (
        settings.DATABASE_URL
        == "postgresql+asyncpg://nova:strong-db-password@localhost:5432/novadb"
    )
    secret_file = tmp_path / "secret_key"
    assert settings.SECRET_KEY == secret_file.read_text(encoding="ascii")
    assert len(settings.SECRET_KEY) == 64


def test_get_settings_reuses_persisted_secret_key(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))
    (tmp_path / "db_password").write_text("strong-db-password", encoding="ascii")
    (tmp_path / "secret_key").write_text("a" * 64, encoding="ascii")

    assert get_settings().SECRET_KEY == "a" * 64


def test_get_settings_rejects_known_database_default(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nova:nova@localhost:5432/novadb"
    )

    with pytest.raises(ValidationError) as exc:
        get_settings()

    assert "nova:nova" in str(exc.value)


def test_get_settings_requires_database_url_or_password_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))

    with pytest.raises(ValueError) as exc:
        get_settings()

    assert "db_password" in str(exc.value)
