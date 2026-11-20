# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lab management API tests (#262): listing scope, state summary, and the
stop/wipe lifecycle actions."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.dependencies import get_current_user


@pytest.fixture()
def manage_settings(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        USER_TEMPLATES_DIR=tmp_path / "user-templates",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR, settings.TEMPLATES_DIR, settings.USER_TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.routers.manage._labs_dir_from_config", lambda: settings.LABS_DIR)
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    return settings


def _write_lab(settings, filename, *, running=0, total=1, cpu=1, ram=1024, author="admin"):
    nodes = {}
    for i in range(1, total + 1):
        nodes[str(i)] = {
            "id": i,
            "name": f"node-{i}",
            "type": "qemu",
            "status": 2 if i <= running else 0,
            "cpu": cpu,
            "ram": ram,
        }
    lab = {
        "schema": 2,
        "id": filename.replace(".json", ""),
        "meta": {"name": filename.replace(".json", ""), "author": author},
        "nodes": nodes,
    }
    (settings.LABS_DIR / filename).write_text(json.dumps(lab))


@pytest_asyncio.fixture()
async def client(manage_settings):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


def _as(role: str, username: str = "admin", folder: str = "/") -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username=username, role=role, html5=True, folder=folder
    )


@pytest.mark.asyncio
async def test_listing_reports_state_and_allocations(client, manage_settings):
    _write_lab(manage_settings, "arc-lab.json", running=2, total=3, cpu=2, ram=2048)
    _write_lab(manage_settings, "idle-lab.json", running=0, total=1)
    _as("admin")

    response = await client.get("/api/manage/labs")

    assert response.status_code == 200
    data = response.json()["data"]
    labs = {lab["file"]: lab for lab in data["labs"]}
    assert labs["arc-lab.json"]["state"] == "running"
    assert labs["arc-lab.json"]["nodes_running"] == 2
    assert labs["arc-lab.json"]["nodes_total"] == 3
    assert labs["arc-lab.json"]["cpu_allocated"] == 6
    assert labs["arc-lab.json"]["ram_allocated_mb"] == 6144
    assert labs["idle-lab.json"]["state"] == "stopped"
    assert data["totals"]["running"] == 1
    assert data["totals"]["nodes_running"] == 2


@pytest.mark.asyncio
async def test_non_admin_listing_scoped_to_folder_root(client, manage_settings):
    nested = manage_settings.LABS_DIR / "trainee"
    nested.mkdir()
    lab = {
        "schema": 2,
        "id": "scoped",
        "meta": {"name": "scoped"},
        "nodes": {"1": {"id": 1, "name": "n1", "type": "qemu", "status": 0, "cpu": 1, "ram": 512}},
    }
    (nested / "scoped.json").write_text(json.dumps(lab))
    _write_lab(manage_settings, "admin-lab.json")
    _as("user", username="trainee", folder="/trainee")

    response = await client.get("/api/manage/labs")

    files = [lab["file"] for lab in response.json()["data"]["labs"]]
    assert files == ["scoped.json"]


@pytest.mark.asyncio
async def test_stop_lab_stops_running_nodes_only(client, manage_settings, monkeypatch):
    _write_lab(manage_settings, "arc-lab.json", running=2, total=3)
    _as("admin")

    stopped_ids: list[int] = []

    class _FakeRuntime:
        def stop_node(self, lab_data, node_id):
            stopped_ids.append(node_id)

    monkeypatch.setattr("app.routers.manage.NodeRuntimeService", _FakeRuntime)

    response = await client.post("/api/manage/labs/arc-lab.json/stop")

    assert response.status_code == 200
    data = response.json()["data"]
    assert sorted(data["stopped"]) == [1, 2]
    assert data["failed"] == []


@pytest.mark.asyncio
async def test_wipe_lab_wipes_every_node(client, manage_settings, monkeypatch):
    _write_lab(manage_settings, "arc-lab.json", running=1, total=2)
    _as("admin")

    wiped_ids: list[int] = []

    class _FakeRuntime:
        def wipe_node(self, lab_data, node_id):
            wiped_ids.append(node_id)

    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService", _FakeRuntime
    )

    response = await client.post("/api/manage/labs/arc-lab.json/wipe")

    assert response.status_code == 200
    assert sorted(response.json()["data"]["wiped"]) == [1, 2]


@pytest.mark.asyncio
async def test_stop_requires_admin(client, manage_settings):
    _write_lab(manage_settings, "arc-lab.json")
    _as("user", username="trainee", folder="/trainee")

    response = await client.post("/api/manage/labs/arc-lab.json/stop")

    assert response.status_code == 200
    assert response.json()["code"] == 403


@pytest.mark.asyncio
async def test_stop_missing_lab_returns_404(client, manage_settings):
    _as("admin")

    response = await client.post("/api/manage/labs/ghost.json/stop")

    assert response.status_code == 200
    assert response.json()["code"] == 404


@pytest.mark.asyncio
async def test_stop_path_traversal_is_rejected(client, manage_settings):
    _as("admin")

    response = await client.post("/api/manage/labs/..%2Fghost.json/stop")

    assert response.status_code == 200
    assert response.json()["code"] == 404
