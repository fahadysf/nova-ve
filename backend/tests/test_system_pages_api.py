# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""System pages API tests (#265): enriched status, log listing/tail with
path safety, and settings read/persist."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.dependencies import get_current_admin, get_current_user


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


def _as(role: str = "admin") -> None:
    user = SimpleNamespace(username="admin", role=role, html5=True, folder="/")
    app.dependency_overrides[get_current_user] = lambda: user

    def admin_dep():
        # Mirror the real admin guard instead of blindly passing.
        from fastapi import HTTPException

        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin role required.")
        return user

    app.dependency_overrides[get_current_admin] = admin_dep


@pytest.mark.asyncio
async def test_status_reports_live_host_facts(client, sys_settings):
    _as("admin")

    response = await client.get("/api/status")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kernel"]
    assert data["hostname"]
    assert data["uptime_seconds"] >= 0
    assert float(data["mem"]) >= 0.0
    assert "mem_total_gb" in data
    assert "qemu" in data  # node counters preserved


def _seed_runtime_log(settings, lab_id="arc-id", node_id=1, lines=5):
    log_path = settings.TMP_DIR / f"{lab_id}-{node_id}.log"
    log_path.write_text("\n".join(f"line {i}: hello world" for i in range(lines)) + "\n")
    records = settings.TMP_DIR / "node-runtime"
    records.mkdir(exist_ok=True)
    (records / f"{lab_id}-{node_id}.json").write_text(
        json.dumps(
            {
                "lab_id": lab_id,
                "node_id": node_id,
                "name": f"node-{node_id}",
                "kind": "qemu",
                "stdout_log": str(log_path),
            }
        )
    )
    return log_path


@pytest.mark.asyncio
async def test_log_listing_surfaces_runtime_logs(client, sys_settings):
    _seed_runtime_log(sys_settings)
    _as("admin")

    response = await client.get("/api/system/logs")

    assert response.status_code == 200
    logs = response.json()["data"]["logs"]
    assert len(logs) == 1
    assert logs[0]["lab_id"] == "arc-id"
    assert logs[0]["size"] > 0


@pytest.mark.asyncio
async def test_log_tail_filters_by_pattern(client, sys_settings):
    _seed_runtime_log(sys_settings, lines=10)
    _as("admin")

    logs_response = await client.get("/api/system/logs")
    log_path = logs_response.json()["data"]["logs"][0]["path"]

    response = await client.get(
        "/api/system/logs/tail", params={"path": log_path, "lines": 200, "pattern": "line 9"}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["lines"] == 1
    assert "line 9" in data["content"]


@pytest.mark.asyncio
async def test_log_tail_rejects_unregistered_paths(client, sys_settings):
    _as("admin")

    rogue = sys_settings.BASE_DATA_DIR / "secret.txt"
    rogue.write_text("top secret")
    response = await client.get("/api/system/logs/tail", params={"path": str(rogue)})

    # Unregistered paths are rejected at the HTTP layer.
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_settings_get_reports_effective_values(client, sys_settings):
    _as("admin")

    response = await client.get("/api/system/settings")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["reconciliation"]["discovery_cadence_seconds"] == 30
    assert data["networking"]["nat_cloud_pool"] == "10.255.0.0/16"
    assert data["restart_required"] is False


@pytest.mark.asyncio
async def test_settings_put_persists_overrides(client, sys_settings):
    _as("admin")

    response = await client.put(
        "/api/system/settings",
        json={"DISCOVERY_CADENCE_SECONDS": 60, "NAT_CLOUD_POOL": "10.250.0.0/16"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["restart_required"] is True

    persisted = json.loads((sys_settings.BASE_DATA_DIR / "system-settings.json").read_text())
    assert persisted["DISCOVERY_CADENCE_SECONDS"] == 60
    assert persisted["NAT_CLOUD_POOL"] == "10.250.0.0/16"

    # GET now reports the override + restart flag.
    get_response = await client.get("/api/system/settings")
    data = get_response.json()["data"]
    assert data["reconciliation"]["discovery_cadence_seconds"] == 60
    assert data["reconciliation"]["discovery_cadence_seconds_effective"] == 30
    assert data["restart_required"] is True


@pytest.mark.asyncio
async def test_settings_put_rejects_invalid_values(client, sys_settings):
    _as("admin")

    response = await client.put(
        "/api/system/settings",
        json={"DISCOVERY_CADENCE_SECONDS": 1},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_settings_put_requires_admin(client, sys_settings):
    _as("user")

    response = await client.put(
        "/api/system/settings",
        json={"SESSION_MAX_AGE": 7200},
    )

    assert response.status_code == 403
