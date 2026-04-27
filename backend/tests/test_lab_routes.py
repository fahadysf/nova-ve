from types import SimpleNamespace

import pytest
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
    monkeypatch.setattr(
        "app.services.template_service.TemplateService._docker_image_catalog",
        lambda _self: {
            "nova-ve-alpine-telnet:latest": {
                "image": "nova-ve-alpine-telnet:latest",
                "files": [],
                "path": "nova-ve-alpine-telnet:latest",
                "source": "docker",
            }
        },
    )
    docker_template_dir = patched_route_settings.TEMPLATES_DIR / "docker"
    docker_template_dir.mkdir(parents=True, exist_ok=True)
    (docker_template_dir / "docker.yml").write_text(
        """type: docker
name: Docker Host
cpu: 1
ram: 1024
ethernet: 1
console_type: telnet
icon_type: server
cpulimit: 1
"""
    )
    docker_image_dir = patched_route_settings.IMAGES_DIR / "docker" / "nova-ve-alpine-telnet:latest"
    docker_image_dir.mkdir(parents=True, exist_ok=True)

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
        create_payload = create_response.json()
        assert create_payload["code"] == 200
        delete_response = await client.delete("/api/labs/delete-probe.json/nodes/1")
        assert delete_response.status_code == 200
        payload = delete_response.json()
        assert payload["code"] == 200
        assert payload["message"] == "Node deleted successfully."

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_topology_route_is_not_shadowed_by_update_lab(monkeypatch, patched_route_settings):
    lab_path = patched_route_settings.LABS_DIR / "topology-probe.json"
    lab_path.write_text(
        """{
  "id": "topology-probe",
  "meta": {"name": "topology-probe"},
  "nodes": {
    "1": {
      "id": 1,
      "name": "node-1",
      "type": "docker",
      "template": "docker",
      "image": "nova-ve-alpine-telnet:latest",
      "console": "telnet",
      "status": 0,
      "cpu": 1,
      "ram": 1024,
      "ethernet": 1,
      "left": 100,
      "top": 100,
      "icon": "Server.png",
      "interfaces": [{"name": "eth0", "network_id": 0}]
    }
  },
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
        response = await client.put(
            "/api/labs/topology-probe.json/topology",
            json={
                "topology": [{"source": "node1", "destination": "network1", "network_id": 1}],
                "nodes": {"1": {"left": 180, "top": 220}},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["code"] == 200
        assert payload["message"] == "Topology saved successfully."

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_node_catalog_and_batch_routes_are_available(monkeypatch, patched_route_settings):
    monkeypatch.setattr(
        "app.services.template_service.TemplateService._docker_image_catalog",
        lambda _self: {
            "nova-ve-alpine-telnet:latest": {
                "image": "nova-ve-alpine-telnet:latest",
                "files": [],
                "path": "nova-ve-alpine-telnet:latest",
                "source": "docker",
            }
        },
    )
    docker_template_dir = patched_route_settings.TEMPLATES_DIR / "docker"
    docker_template_dir.mkdir(parents=True, exist_ok=True)
    (docker_template_dir / "docker.yml").write_text(
        """type: docker
name: Docker Host
description: Demo docker node
cpu: 1
ram: 1024
ethernet: 1
console_type: telnet
icon_type: server
cpulimit: 1
"""
    )
    lab_path = patched_route_settings.LABS_DIR / "catalog-probe.json"
    lab_path.write_text(
        """{
  "id": "catalog-probe",
  "meta": {"name": "catalog-probe"},
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
        catalog_response = await client.get("/api/labs/catalog-probe.json/node-catalog")
        assert catalog_response.status_code == 200
        catalog_payload = catalog_response.json()
        assert catalog_payload["code"] == 200
        assert "templates" in catalog_payload["data"]
        assert "runtime_editability" in catalog_payload["data"]

        batch_response = await client.post(
            "/api/labs/catalog-probe.json/nodes/batch",
            json={
                "name_prefix": "docker-node",
                "count": 2,
                "type": "docker",
                "template": "docker",
                "image": "nova-ve-alpine-telnet:latest",
                "left": 100,
                "top": 200,
            },
        )
        assert batch_response.status_code == 200
        batch_payload = batch_response.json()
        assert batch_payload["code"] == 200
        assert len(batch_payload["data"]["nodes"]) == 2
        assert batch_payload["data"]["nodes"][0]["name"] == "docker-node-1"

    app.dependency_overrides.clear()
