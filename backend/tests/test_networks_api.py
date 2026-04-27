# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-063 + US-064 — per-resource network API endpoints."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app


def _seed_lab(lab_dir, name: str = "lab.json", *, nodes: dict | None = None,
              networks: dict | None = None, links: list | None = None) -> str:
    payload = {
        "schema": 2,
        "id": name.replace(".json", ""),
        "meta": {"name": name},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes or {},
        "networks": networks or {},
        "links": links or [],
        "defaults": {"link_style": "orthogonal"},
    }
    (lab_dir / name).write_text(json.dumps(payload))
    return name


def _explicit_network(net_id: int, name: str = "lan") -> dict:
    return {
        "id": net_id,
        "name": name,
        "type": "linux_bridge",
        "left": 200,
        "top": 200,
        "icon": "01-Cloud-Default.svg",
        "width": 0,
        "style": "Solid",
        "linkstyle": "Straight",
        "color": "",
        "label": "",
        "visibility": True,
        "implicit": False,
        "smart": -1,
        "config": {},
    }


def _implicit_network(net_id: int) -> dict:
    n = _explicit_network(net_id, "")
    n["implicit"] = True
    n["visibility"] = False
    n["name"] = ""
    return n


def _node(node_id: int, ethernet: int = 1) -> dict:
    return {
        "id": node_id,
        "name": f"n{node_id}",
        "type": "docker",
        "template": "docker",
        "image": "nova-ve-alpine-telnet:latest",
        "console": "telnet",
        "status": 0,
        "cpu": 1,
        "ram": 256,
        "ethernet": ethernet,
        "left": 100,
        "top": 100,
        "icon": "Server.png",
        "interfaces": [
            {"index": i, "name": f"eth{i}", "planned_mac": None, "port_position": None}
            for i in range(ethernet)
        ],
    }


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
async def test_create_network_explicit(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(patched_route_settings.LABS_DIR)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/labs/{lab_name}/networks",
            json={"name": "lan", "type": "linux_bridge"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["network"]["implicit"] is False
    assert body["network"]["visibility"] is True

    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    assert len(saved["networks"]) == 1


@pytest.mark.asyncio
async def test_delete_explicit_with_attachments_409(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
        links=[
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 5},
                "style_override": None, "label": "", "color": "", "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            }
        ],
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.delete(f"/api/labs/{lab_name}/networks/5")
    assert resp.status_code == 409
    body = resp.json()
    assert body["count"] == 1


@pytest.mark.asyncio
async def test_delete_explicit_no_attachments_204(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        networks={"5": _explicit_network(5, "lan")},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.delete(f"/api/labs/{lab_name}/networks/5")
    assert resp.status_code == 200

    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    assert "5" not in saved["networks"]

    types = [t for _, t, _ in ws_recorder]
    assert "network_deleted" in types


@pytest.mark.asyncio
async def test_patch_network_rename(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        networks={"5": _explicit_network(5, "lan")},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.patch(f"/api/labs/{lab_name}/networks/5", json={"name": "renamed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["network"]["name"] == "renamed"
    assert body["event"] == "network_updated"


@pytest.mark.asyncio
async def test_list_networks_excludes_implicit_by_default(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        networks={"1": _implicit_network(1), "2": _explicit_network(2, "lan")},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/api/labs/{lab_name}/networks")
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = {int(n["id"]) for n in data.values()}
    assert ids == {2}


@pytest.mark.asyncio
async def test_list_networks_include_hidden_true(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        networks={"1": _implicit_network(1), "2": _explicit_network(2, "lan")},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/api/labs/{lab_name}/networks?include_hidden=true")
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = {int(n["id"]) for n in data.values()}
    assert ids == {1, 2}


@pytest.mark.asyncio
async def test_patch_implicit_network_with_name_promotes(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        networks={"1": _implicit_network(1)},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.patch(
            f"/api/labs/{lab_name}/networks/1",
            json={"name": "my-bridge"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["network"]["implicit"] is False
    assert body["network"]["visibility"] is True
    assert body["network"]["name"] == "my-bridge"
    assert body["event"] == "network_promoted"

    types = [t for _, t, _ in ws_recorder]
    assert "network_promoted" in types


@pytest.mark.asyncio
async def test_patch_network_name_null_on_promoted_returns_422(
    patched_route_settings, auth_override, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        networks={"5": _explicit_network(5, "lan")},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.patch(f"/api/labs/{lab_name}/networks/5", json={"name": None})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 422
    assert "cannot un-name" in body["message"].lower()
