# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``app.config.Settings`` — US-402 discovery cadence knob."""

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
