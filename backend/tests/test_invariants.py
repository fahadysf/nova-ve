"""Cross-cutting invariants for the v2 lab schema.

Currently asserts:
  - test_network_count_matches_link_filter — every network_id-containing
    endpoint in links[] contributes to the network's reported ``count``.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app


@pytest.fixture()
def invariant_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    for directory in (labs_dir, images_dir, tmp_dir, templates_dir):
        directory.mkdir()
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
        GUACAMOLE_JSON_SECRET_KEY="4c0b569e4c96df157eee1b65dd0e4d41",
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
        SESSION_MAX_AGE=14400,
    )


@pytest.fixture()
def patched_invariant_settings(monkeypatch, invariant_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: invariant_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: invariant_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: invariant_settings)
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: invariant_settings)
    monkeypatch.setattr("app.services.guacamole_db_service.get_settings", lambda: invariant_settings)
    return invariant_settings


def _admin():
    return SimpleNamespace(username="admin", role="admin", html5=True, folder="/")


def _build_lab(*, network_count_expected: dict[int, int]) -> dict:
    """Build a lab with 2 networks and 4 link endpoints distributed per ``network_count_expected``."""

    nodes = {}
    for index in range(1, 5):
        nodes[str(index)] = {
            "id": index,
            "name": f"R{index}",
            "type": "qemu",
            "template": "vyos",
            "image": "vyos-1.4",
            "console": "telnet",
            "status": 0,
            "ethernet": 1,
            "left": 100 + (index - 1) * 100,
            "top": 100,
            "icon": "Router.png",
            "interfaces": [
                {"index": 0, "name": "Gi0", "planned_mac": None, "port_position": None}
            ],
        }

    networks = {
        "1": {
            "id": 1,
            "name": "alpha",
            "type": "linux_bridge",
            "left": 200,
            "top": 300,
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
        },
        "2": {
            "id": 2,
            "name": "beta",
            "type": "linux_bridge",
            "left": 400,
            "top": 300,
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
        },
    }

    # Distribute the requested counts across the four available nodes.
    links: list[dict] = []
    cursor = 1
    for network_id, count in network_count_expected.items():
        for _ in range(count):
            links.append(
                {
                    "id": f"lnk_{len(links) + 1:03d}",
                    "from": {"node_id": cursor, "interface_index": 0},
                    "to": {"network_id": network_id},
                    "style_override": None,
                    "label": "",
                    "color": "",
                    "width": "1",
                    "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
                }
            )
            cursor += 1

    return {
        "schema": 2,
        "id": "invariant-lab",
        "meta": {"name": "invariant"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes,
        "networks": networks,
        "links": links,
        "defaults": {"link_style": "orthogonal"},
    }


@pytest.mark.asyncio
async def test_network_count_matches_link_filter(patched_invariant_settings):
    """The networks API must return ``count`` derived from the live links[]."""

    distribution = {1: 3, 2: 1}
    lab = _build_lab(network_count_expected=distribution)
    (patched_invariant_settings.LABS_DIR / "invariant.json").write_text(json.dumps(lab))

    app.dependency_overrides[get_current_user] = _admin
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/labs/invariant.json/networks")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    networks = payload["data"]
    for network_id, expected_count in distribution.items():
        record = networks.get(str(network_id))
        assert record is not None
        assert record["count"] == expected_count, (
            f"network {network_id} reported count={record['count']}, expected {expected_count}"
        )

    # Also verify the persisted file never carried a stale count.
    raw = json.loads((patched_invariant_settings.LABS_DIR / "invariant.json").read_text())
    for network in raw["networks"].values():
        assert "count" not in network
