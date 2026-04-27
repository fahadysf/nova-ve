# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-063 — bulk PUT /layout endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app


def _seed_lab(lab_dir, name: str = "lab.json") -> str:
    payload = {
        "schema": 2,
        "id": name.replace(".json", ""),
        "meta": {"name": name},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1, "name": "n1", "type": "docker", "template": "docker",
                "image": "nova-ve-alpine-telnet:latest", "console": "telnet",
                "status": 0, "cpu": 1, "ram": 256, "ethernet": 1,
                "left": 100, "top": 100, "icon": "Server.png",
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                ],
            }
        },
        "networks": {
            "1": {
                "id": 1, "name": "lan", "type": "linux_bridge",
                "left": 0, "top": 0, "icon": "01-Cloud-Default.svg",
                "width": 0, "style": "Solid", "linkstyle": "Straight",
                "color": "", "label": "", "visibility": True, "implicit": False,
                "smart": -1, "config": {},
            }
        },
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }
    (lab_dir / name).write_text(json.dumps(payload))
    return name


@pytest.fixture()
def route_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    for d in (labs_dir, images_dir, tmp_dir, templates_dir):
        d.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        TEMPLATES_DIR=templates_dir,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        GUACAMOLE_DATABASE_URL="",
        GUACAMOLE_DATA_SOURCE="postgresql",
        GUACAMOLE_INTERNAL_URL="http://127.0.0.1:8081/html5/",
        GUACAMOLE_JSON_SECRET_KEY="x" * 32,
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
    )


@pytest.fixture()
def patched_route_settings(monkeypatch, route_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.guacamole_db_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.link_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.network_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.routers.labs.get_settings", lambda: route_settings)
    return route_settings


@pytest.fixture()
def auth_override():
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="admin", role="admin", html5=True, folder="/",
    )
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def ws_recorder(monkeypatch):
    recorded: list[tuple[str, str, dict]] = []

    async def fake_publish(lab_id, event_type, payload, rev=""):
        recorded.append((lab_id, event_type, payload))
        return SimpleNamespace(seq=len(recorded), type=event_type, rev=rev, payload=payload)

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", fake_publish)
    return recorded


@pytest.mark.asyncio
async def test_layout_accepts_position_only(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(patched_route_settings.LABS_DIR)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.put(
            f"/api/labs/{lab_name}/layout",
            json={
                "nodes": [{"id": 1, "left": 250, "top": 300}],
                "networks": [{"id": 1, "left": 400, "top": 400}],
                "viewport": {"x": 10, "y": 20, "zoom": 1.5},
                "defaults": {"link_style": "bezier"},
            },
        )
    assert resp.status_code == 200, resp.text
    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    assert saved["nodes"]["1"]["left"] == 250
    assert saved["nodes"]["1"]["top"] == 300
    assert saved["networks"]["1"]["left"] == 400
    assert saved["viewport"]["zoom"] == 1.5
    assert saved["defaults"]["link_style"] == "bezier"

    types = [t for _, t, _ in ws_recorder]
    assert "layout_updated" in types


@pytest.mark.asyncio
async def test_layout_rejects_link_change_409(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(patched_route_settings.LABS_DIR)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.put(
            f"/api/labs/{lab_name}/layout",
            json={
                "nodes": [{"id": 1, "left": 0, "top": 0}],
                "links": [{"id": "lnk_001"}],
            },
        )
    assert resp.status_code == 409
    body = resp.json()
    assert "links" in body["forbidden_fields"]


@pytest.mark.asyncio
async def test_layout_rejects_interface_mac_change_409(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(patched_route_settings.LABS_DIR)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.put(
            f"/api/labs/{lab_name}/layout",
            json={
                "nodes": [{
                    "id": 1, "left": 100, "top": 100,
                    "interfaces": [{"index": 0, "planned_mac": "aa:bb:cc:dd:ee:ff"}],
                }],
            },
        )
    assert resp.status_code == 409
    body = resp.json()
    assert any("interfaces" in f for f in body["forbidden_fields"])


@pytest.mark.asyncio
async def test_layout_rejects_network_type_change_409(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(patched_route_settings.LABS_DIR)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.put(
            f"/api/labs/{lab_name}/layout",
            json={
                "networks": [{"id": 1, "left": 0, "top": 0, "type": "ovs_bridge"}],
            },
        )
    assert resp.status_code == 409
    body = resp.json()
    assert any("type" in f for f in body["forbidden_fields"])
