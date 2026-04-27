# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-063 + US-064 — per-resource link API + implicit-bridge state machine."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    path = lab_dir / name
    path.write_text(json.dumps(payload))
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
def reset_idempotency():
    """Reset the link service's idempotency cache between tests."""
    from app.services.link_service import link_service

    link_service._idempotency.clear()
    yield
    link_service._idempotency.clear()


@pytest.fixture()
def ws_recorder(monkeypatch):
    """Capture every ws_hub.publish call as (lab_id, type, payload)."""
    recorded: list[tuple[str, str, dict]] = []

    async def fake_publish(lab_id, event_type, payload, rev=""):
        recorded.append((lab_id, event_type, payload))
        return SimpleNamespace(seq=len(recorded), type=event_type, rev=rev, payload=payload)

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", fake_publish)
    return recorded


def _node(node_id: int, name: str = "n", ethernet: int = 2) -> dict:
    return {
        "id": node_id,
        "name": name,
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


@pytest.mark.asyncio
async def test_create_link_with_existing_network(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/labs/{lab_name}/links",
            json={"from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 5}},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == 201
    assert body["link"]["id"]
    assert body["link"]["state"] == "configured"

    # Verify lab.json contains the link
    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    assert len(saved["links"]) == 1

    types = [t for _, t, _ in ws_recorder]
    assert "link_created" in types


@pytest.mark.asyncio
async def test_create_link_idempotency_key_replay(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    body = {"from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 5}}
    headers = {"Idempotency-Key": "abc123"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp1 = await c.post(f"/api/labs/{lab_name}/links", json=body, headers=headers)
        resp2 = await c.post(f"/api/labs/{lab_name}/links", json=body, headers=headers)

    assert resp1.status_code == 201
    assert resp2.status_code == 200
    assert resp1.json()["link"]["id"] == resp2.json()["link"]["id"]

    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    assert len(saved["links"]) == 1

    publishes = [t for _, t, _ in ws_recorder if t == "link_created"]
    assert len(publishes) == 1


@pytest.mark.asyncio
async def test_create_link_creates_implicit_network(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1), "2": _node(2)},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/labs/{lab_name}/links",
            json={
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"node_id": 2, "interface_index": 0},
            },
        )
    assert resp.status_code == 201, resp.text

    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    assert len(saved["links"]) == 2
    assert len(saved["networks"]) == 1

    net = list(saved["networks"].values())[0]
    assert net["implicit"] is True
    assert net["visibility"] is False

    types = [t for _, t, _ in ws_recorder]
    assert types[0] == "network_created"
    assert types.count("link_created") == 2


@pytest.mark.asyncio
async def test_promote_on_third_attach(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
):
    # Pre-build an implicit network at refcount 2 (node1, node2).
    impl_net_id = 1
    implicit_net = _explicit_network(impl_net_id, "")
    implicit_net["implicit"] = True
    implicit_net["visibility"] = False
    implicit_net["name"] = ""

    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1), "2": _node(2), "3": _node(3)},
        networks={str(impl_net_id): implicit_net},
        links=[
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": impl_net_id},
                "style_override": None, "label": "", "color": "", "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
            {
                "id": "lnk_002",
                "from": {"node_id": 2, "interface_index": 0},
                "to": {"network_id": impl_net_id},
                "style_override": None, "label": "", "color": "", "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
        ],
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/labs/{lab_name}/links",
            json={
                "from": {"node_id": 3, "interface_index": 0},
                "to": {"network_id": impl_net_id},
            },
        )
    assert resp.status_code == 201, resp.text

    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    promoted = saved["networks"][str(impl_net_id)]
    assert promoted["implicit"] is False
    assert promoted["visibility"] is True
    assert promoted["name"] == f"bridge-{impl_net_id}"

    types = [t for _, t, _ in ws_recorder]
    assert "network_promoted" in types
    assert "link_created" in types


@pytest.mark.asyncio
async def test_promotion_is_one_way(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
):
    impl_net_id = 1
    promoted_net = _explicit_network(impl_net_id, f"bridge-{impl_net_id}")
    # State: already promoted (implicit=False); refcount==3 from previous step.
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1), "2": _node(2), "3": _node(3)},
        networks={str(impl_net_id): promoted_net},
        links=[
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": impl_net_id},
                "style_override": None, "label": "", "color": "", "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
            {
                "id": "lnk_002",
                "from": {"node_id": 2, "interface_index": 0},
                "to": {"network_id": impl_net_id},
                "style_override": None, "label": "", "color": "", "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
            {
                "id": "lnk_003",
                "from": {"node_id": 3, "interface_index": 0},
                "to": {"network_id": impl_net_id},
                "style_override": None, "label": "", "color": "", "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
        ],
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # Drop one link → refcount 2; network must remain explicit.
        del_resp = await c.delete(f"/api/labs/{lab_name}/links/lnk_003")
        assert del_resp.status_code == 200
        saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
        assert saved["networks"][str(impl_net_id)]["implicit"] is False

        # Drop another → refcount 1; still explicit.
        del_resp = await c.delete(f"/api/labs/{lab_name}/links/lnk_002")
        assert del_resp.status_code == 200
        saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
        assert saved["networks"][str(impl_net_id)]["implicit"] is False

        # Drop the last → refcount 0; explicit network is NOT auto-GC'd.
        del_resp = await c.delete(f"/api/labs/{lab_name}/links/lnk_001")
        assert del_resp.status_code == 200
        saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
        assert str(impl_net_id) in saved["networks"]


@pytest.mark.asyncio
async def test_implicit_bridge_gc_on_last_delete(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
):
    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1), "2": _node(2)},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # Create implicit network (2 links).
        await c.post(
            f"/api/labs/{lab_name}/links",
            json={
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"node_id": 2, "interface_index": 0},
            },
        )
        saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
        assert len(saved["networks"]) == 1
        assert len(saved["links"]) == 2
        link_ids = [l["id"] for l in saved["links"]]

        # Delete one — implicit net stays.
        await c.delete(f"/api/labs/{lab_name}/links/{link_ids[0]}")
        saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
        assert len(saved["networks"]) == 1

        # Delete the last — implicit net is GC'd.
        await c.delete(f"/api/labs/{lab_name}/links/{link_ids[1]}")
        saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
        assert saved["networks"] == {}

    types = [t for _, t, _ in ws_recorder]
    assert "network_deleted" in types


@pytest.mark.asyncio
async def test_delete_idempotent(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
):
    lab_name = _seed_lab(patched_route_settings.LABS_DIR)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.delete(f"/api/labs/{lab_name}/links/lnk_999")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body.get("already_deleted") is True

    types = [t for _, t, _ in ws_recorder]
    assert "link_deleted" not in types


@pytest.mark.asyncio
async def test_patch_link_style_override(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
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
        resp = await c.patch(
            f"/api/labs/{lab_name}/links/lnk_001",
            json={"style_override": "bezier"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["link"]["style_override"] == "bezier"

    saved = json.loads((patched_route_settings.LABS_DIR / lab_name).read_text())
    assert saved["links"][0]["style_override"] == "bezier"


@pytest.mark.asyncio
async def test_get_links_returns_state_field(
    patched_route_settings, auth_override, reset_idempotency, ws_recorder,
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
        resp = await c.get(f"/api/labs/{lab_name}/links")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert all(l["state"] == "configured" for l in data)


@pytest.mark.asyncio
async def test_create_link_acquires_lock_and_publishes_ws_event(
    patched_route_settings, auth_override, reset_idempotency, monkeypatch,
):
    # Use a dedicated AsyncMock to count publish calls.
    publish_mock = AsyncMock()
    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish_mock)

    lab_name = _seed_lab(
        patched_route_settings.LABS_DIR,
        nodes={"1": _node(1)},
        networks={"5": _explicit_network(5, "lan")},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/labs/{lab_name}/links",
            json={"from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 5}},
        )
    assert resp.status_code == 201
    assert publish_mock.await_count >= 1
    # Lock file should exist.
    assert (patched_route_settings.LABS_DIR / f"{lab_name}.lock").exists()
