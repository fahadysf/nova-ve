# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""KSM support (#336): live /api/status reporting and the host boot contract.

Covers:
- ``_ksm_facts``/``_ksm_status`` against a fake sysfs tree: enabled,
  disabled, unsupported (no sysfs), dedup-ratio math, OFF reads as a
  warning note, never raises on missing/garbage knobs.
- ``GET /api/status`` surfacing the live values.
- The provision/deploy contract: tuner script with measured scan rates,
  ``run`` set last, every knob optional; boot unit ordering + ksmtuned
  masking; provision persisting KSM_ENABLED=1.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.dependencies import get_current_admin, get_current_user
from app.routers.system import _ksm_facts, _ksm_status

REPO_ROOT = Path(__file__).resolve().parents[2]
KSM_SH = REPO_ROOT / "deploy" / "scripts" / "nova-ve-ksm.sh"
KSM_UNIT = REPO_ROOT / "deploy" / "scripts" / "nova-ve-ksm.service.tpl"
PROVISION = REPO_ROOT / "deploy" / "scripts" / "provision-ubuntu-2604.sh"


def _fake_sysfs(tmp_path: Path, *, run, saving=(0, 0), missing=()) -> Path:
    base = tmp_path / "ksm"
    base.mkdir()
    page_size = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
    (base / "run").write_text(str(run))
    (base / "pages_to_scan").write_text("1250")
    (base / "sleep_millisecs").write_text("10")
    (base / "pages_sharing").write_text(str(saving[0]))
    (base / "pages_shared").write_text(str(saving[1]))
    for name in missing:
        (base / name).unlink(missing_ok=True)
    return base


# ---------------------------------------------------------------------------
# _ksm_facts / _ksm_status
# ---------------------------------------------------------------------------


def test_enabled_reports_ratio_and_saved(tmp_path):
    base = _fake_sysfs(tmp_path, run=1, saving=(5000, 1000))
    facts = _ksm_facts(base_path=base)
    assert facts["available"] is True
    assert facts["enabled"] is True
    assert facts["pages_to_scan"] == 1250
    assert facts["sleep_millisecs"] == 10
    assert facts["dedup_ratio"] == 5.0
    # (5000 - 1000) pages saved at the host page size.
    page_size = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
    assert facts["saved_gb"] == round(4000 * page_size / 1024**3, 2)
    assert "note" not in facts


def test_disabled_reads_as_warning(tmp_path):
    base = _fake_sysfs(tmp_path, run=0)
    facts = _ksm_facts(base_path=base)
    assert facts["enabled"] is False
    assert "OFF" in facts["note"]


def test_missing_sysfs_is_unsupported(tmp_path):
    facts = _ksm_facts(base_path=tmp_path / "does-not-exist")
    assert facts == {"available": False}


def test_never_raises_on_garbage(tmp_path):
    base = tmp_path / "ksm"
    base.mkdir()
    (base / "run").write_text("not-a-number")
    (base / "pages_sharing").write_text("")
    facts = _ksm_facts(base_path=base)
    assert facts["available"] is True
    assert facts["enabled"] is False  # unparsable run reads as off, not a crash


def test_status_string_states(tmp_path, monkeypatch):
    base = _fake_sysfs(tmp_path, run=1)
    monkeypatch.setattr("app.routers.system.KSM_SYSFS", base)
    assert _ksm_status() == "enabled"
    (base / "run").write_text("0")
    assert _ksm_status() == "disabled"
    monkeypatch.setattr("app.routers.system.KSM_SYSFS", tmp_path / "nope")
    assert _ksm_status() == "unsupported"


# ---------------------------------------------------------------------------
# /api/status endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def sys_settings(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        BASE_DATA_DIR=tmp_path / "base",
        TMP_DIR=tmp_path / "tmp",
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TEMPLATES_DIR=tmp_path / "templates",
        USER_TEMPLATES_DIR=tmp_path / "user-templates",
        DISCOVERY_CADENCE_SECONDS=30,
        NAT_CLOUD_POOL="10.255.0.0/16",
        SESSION_MAX_AGE=14400,
        EXTERNAL_CONSOLE_HOST="192.168.7.9",
        SESSION_COOKIE_NAME="nova_session",
        COOKIE_SECURE=False,
        ACCESS_TOKEN_EXPIRE_MINUTES=60,
        DOCKER_HOST="unix:///var/run/docker.sock",
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
        SECRET_KEY="abcdefgh-very-secret-value-for-tests-000",
    )
    settings.TMP_DIR.mkdir(parents=True)
    settings.BASE_DATA_DIR.mkdir(parents=True)
    monkeypatch.setattr("app.routers.system.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    return settings


@pytest_asyncio.fixture()
async def client(sys_settings):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_status_surfaces_live_ksm(client, tmp_path, monkeypatch):
    user = SimpleNamespace(username="admin", role="admin", html5=True, folder="/")
    app.dependency_overrides[get_current_user] = lambda: user
    base = _fake_sysfs(tmp_path, run=1, saving=(8000, 2000))
    monkeypatch.setattr("app.routers.system.KSM_SYSFS", base)

    response = await client.get("/api/status")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ksm"] == "enabled"
    assert data["ksm_facts"]["available"] is True
    assert data["ksm_facts"]["dedup_ratio"] == 4.0
    assert data["uksm"] == "unsupported"


# ---------------------------------------------------------------------------
# Provision / boot contract
# ---------------------------------------------------------------------------


def test_tuner_is_executable_with_measured_rates():
    assert KSM_SH.exists()
    assert KSM_SH.stat().st_mode & stat.S_IXUSR
    text = KSM_SH.read_text()
    assert "1250" in text
    assert "NOVA_VE_KSM_SLEEP_MS:-10" in text
    assert "/etc/nova-ve/nova-ve.env" in text


def test_every_knob_is_optional():
    text = KSM_SH.read_text()
    assert "not available on this kernel" in text


def test_run_is_set_last():
    text = KSM_SH.read_text()
    assert text.index("pages_to_scan") < text.index("set_knob run")


def test_unit_orders_before_backend_and_masks_ksmtuned():
    assert KSM_UNIT.exists()
    text = KSM_UNIT.read_text()
    assert "ConditionPathIsDirectory=/sys/kernel/mm/ksm" in text
    assert "Before=nova-ve-backend.service" in text
    assert "mask ksmtuned.service" in text
    assert "Conflicts=ksmtuned.service" in text


def test_provision_persists_kvm_enabled_and_installs_unit():
    text = PROVISION.read_text()
    assert "KSM_ENABLED=1" in text
    assert "ensure_qemu_kvm_ksm" in text
    assert "nova-ve-ksm.service" in text
    assert "mask ksmtuned.service" in text
