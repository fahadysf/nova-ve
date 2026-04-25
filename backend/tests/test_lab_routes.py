from pathlib import Path
from types import SimpleNamespace

import pytest
import json
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.dependencies import get_current_user


@pytest.fixture()
def route_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    labs_dir.mkdir()
    images_dir.mkdir()
    tmp_dir.mkdir()
    templates_dir.mkdir()
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
    )


@pytest.fixture()
def patched_route_settings(monkeypatch, route_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.guacamole_db_service.get_settings", lambda: route_settings)
    return route_settings


@pytest.mark.asyncio
async def test_delete_node_route_is_not_shadowed_by_delete_lab(monkeypatch, patched_route_settings):
    docker_template_dir = patched_route_settings.TEMPLATES_DIR / "docker"
    docker_template_dir.mkdir(parents=True, exist_ok=True)
    (docker_template_dir / "docker.yml").write_text(
        """type: docker
name: Docker Host
cpu: 1
ram: 1024
ethernet: 1
console: rdp
icon: Server.png
cpulimit: 1
"""
    )

    lab_path = patched_route_settings.LABS_DIR / "delete-probe.json"
    lab_path.write_text(
        """{
  "id": "delete-probe",
  "meta": {"name": "delete-probe"},
  "nodes": {},
  "networks": {},
  "topology": []
}"""
    )

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="admin",
        role="admin",
        html5=True,
        folder="/",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        create_response = await client.post(
            "/api/labs/delete-probe.json/nodes",
            json={
                "name": "probe-node",
                "type": "docker",
                "template": "docker",
                "image": "nova-ve-alpine-telnet:latest",
                "left": 100,
                "top": 100,
            },
        )
        assert create_response.status_code == 200
        delete_response = await client.delete("/api/labs/delete-probe.json/nodes/1")
        assert delete_response.status_code == 200
        payload = delete_response.json()
        assert payload["code"] == 200
        assert payload["message"] == "Node deleted successfully."

    app.dependency_overrides.clear()


def test_ui_reactivity_check_sample_lab_fixture_is_present_and_verifiable():
    sample_lab_path = Path(__file__).resolve().parents[1] / "labs" / "ui-reactivity-check.json"

    payload = json.loads(sample_lab_path.read_text())

    assert payload["meta"]["name"] == "ui-reactivity-check"
    assert payload["meta"]["lock"] is False
    assert "bottom-left launcher" in payload["meta"]["description"]

    nodes = payload["nodes"]
    networks = payload["networks"]
    topology = payload["topology"]

    assert len(nodes) == 3
    assert len(networks) == 2
    assert len(topology) == 3
    assert {node["status"] for node in nodes.values()} == {0, 2}
    assert all(network["visibility"] == 1 for network in networks.values())
    assert all(link["destination_type"] == "network" for link in topology)
