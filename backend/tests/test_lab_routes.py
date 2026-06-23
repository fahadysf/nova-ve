from types import SimpleNamespace

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.database import get_db
from app.dependencies import get_current_user
from app.routers.labs import _validate_node_update_request, delete_node, update_node
from app.schemas.node import NodeUpdate


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
            "nova-ve/alpine-telnet:latest": {
                "image": "nova-ve/alpine-telnet:latest",
                "files": [],
                "path": "nova-ve/alpine-telnet:latest",
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
    docker_image_dir = patched_route_settings.IMAGES_DIR / "docker" / "nova-ve/alpine-telnet:latest"
    docker_image_dir.mkdir(parents=True, exist_ok=True)

    lab_path = patched_route_settings.LABS_DIR / "delete-probe.json"
    lab_path.write_text(
        """{
  "schema": 2,
  "id": "delete-probe",
  "meta": {"name": "delete-probe"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {},
  "networks": {},
  "links": [],
  "defaults": {"link_style": "orthogonal"}
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
                "image": "nova-ve/alpine-telnet:latest",
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
async def test_delete_qemu_node_discards_stopped_overlay(patched_route_settings):
    lab_path = patched_route_settings.LABS_DIR / "qemu-delete-probe.json"
    lab_path.write_text(
        """{
  "schema": 2,
  "id": "qemu-delete-probe",
  "meta": {"name": "qemu-delete-probe"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {
    "1": {
      "id": 1,
      "name": "router-1",
      "type": "qemu",
      "template": "csr",
      "image": "csr1000v",
      "left": 100,
      "top": 100
    }
  },
  "networks": {},
  "links": [],
  "defaults": {"link_style": "orthogonal"}
}"""
    )
    overlay_path = patched_route_settings.TMP_DIR / "qemu-delete-probe" / "1" / "virtioa.qcow2"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text("stale writable overlay")

    response = await delete_node(
        "qemu-delete-probe.json",
        1,
        current_user=SimpleNamespace(username="admin", role="admin", html5=True, folder="/"),
    )

    assert response["code"] == 200
    assert not overlay_path.exists()


def test_validate_node_update_running_allows_rename_only_payload():
    """Modal re-submits all edit fields on save; running-state guard must
    block a stopped-only field only when its value actually changed."""
    node = {
        "name": "old-name",
        "type": "qemu",
        "template": "vyos",
        "image": "vyos-rolling",
        "cpu": 2,
        "ram": 1536,
        "ethernet": 4,
        "console": "telnet",
        "delay": 0,
        "extras": {"arch": "x86_64"},
        "icon": "Router.png",
    }
    request = NodeUpdate(
        name="upstream-isp-net",
        image=node["image"],
        icon=node["icon"],
        cpu=node["cpu"],
        ram=node["ram"],
        ethernet=node["ethernet"],
        console=node["console"],
        delay=node["delay"],
        extras=node["extras"],
        interface_naming_scheme=None,
    )
    assert _validate_node_update_request(node, request, node_running=True) is None


def test_validate_node_update_running_blocks_actual_cpu_change():
    node = {
        "name": "old-name",
        "type": "qemu",
        "template": "vyos",
        "image": "vyos-rolling",
        "cpu": 2,
        "ram": 1536,
        "ethernet": 4,
        "console": "telnet",
        "delay": 0,
        "extras": {},
        "icon": "Router.png",
    }
    request = NodeUpdate(
        name=node["name"],
        cpu=4,
        ram=node["ram"],
        ethernet=node["ethernet"],
    )
    error = _validate_node_update_request(node, request, node_running=True)
    assert error == "Stop the node before changing: cpu."


def test_validate_node_update_rejects_qemu_rdp_console_change():
    node = {
        "name": "win",
        "type": "qemu",
        "template": "win",
        "image": "win",
        "cpu": 4,
        "ram": 4096,
        "ethernet": 1,
        "console": "vnc",
        "delay": 0,
        "extras": {},
        "icon": "Server.png",
    }
    request = NodeUpdate(console="rdp")
    error = _validate_node_update_request(node, request, node_running=False)
    assert "Console mode 'rdp' is not supported for qemu nodes" in str(error)


@pytest.mark.asyncio
async def test_update_node_renames_running_node(monkeypatch, patched_route_settings):
    """End-to-end PUT /labs/.../nodes/{id} must succeed when the modal
    re-submits the full edit payload but only `name` changed."""
    lab_path = patched_route_settings.LABS_DIR / "rename-probe.json"
    lab_path.write_text(
        """{
  "schema": 2,
  "id": "renameprobe",
  "meta": {"name": "rename-probe"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {
    "1": {
      "id": 1,
      "name": "old-name",
      "type": "qemu",
      "template": "vyos",
      "image": "vyos-rolling",
      "cpu": 2,
      "ram": 1536,
      "ethernet": 4,
      "console": "telnet",
      "delay": 0,
      "extras": {},
      "icon": "Router.png",
      "left": 100,
      "top": 100,
      "interfaces": []
    }
  },
  "networks": {},
  "links": [],
  "defaults": {"link_style": "orthogonal"}
}"""
    )
    monkeypatch.setattr("app.routers.labs._node_is_running", lambda lab_data, node_id: True)
    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService.enrich_node",
        lambda self, lab_id, node_id, node: {**node, "status": 2},
    )

    response = await update_node(
        "rename-probe.json",
        1,
        request=NodeUpdate(
            name="upstream-isp-net",
            image="vyos-rolling",
            icon="Router.png",
            cpu=2,
            ram=1536,
            ethernet=4,
            console="telnet",
            delay=0,
            extras={},
            interface_naming_scheme=None,
        ),
        current_user=SimpleNamespace(username="admin", role="admin", html5=True, folder="/"),
    )

    assert response["code"] == 200
    assert response["data"]["name"] == "upstream-isp-net"
    # JSON is persisted with the new name.
    import json
    persisted = json.loads(lab_path.read_text())
    assert persisted["nodes"]["1"]["name"] == "upstream-isp-net"


@pytest.mark.asyncio
async def test_update_node_running_rejects_actual_cpu_change(monkeypatch, patched_route_settings):
    """If the user really does change a stopped-only field, the guard must
    still refuse the request with the precise blocked-field list."""
    lab_path = patched_route_settings.LABS_DIR / "cpu-block-probe.json"
    lab_path.write_text(
        """{
  "schema": 2,
  "id": "cpublockprobe",
  "meta": {"name": "cpu-block-probe"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {
    "1": {
      "id": 1,
      "name": "rtr",
      "type": "qemu",
      "template": "vyos",
      "image": "vyos-rolling",
      "cpu": 2,
      "ram": 1536,
      "ethernet": 4,
      "console": "telnet",
      "delay": 0,
      "extras": {},
      "icon": "Router.png",
      "left": 100,
      "top": 100,
      "interfaces": []
    }
  },
  "networks": {},
  "links": [],
  "defaults": {"link_style": "orthogonal"}
}"""
    )
    monkeypatch.setattr("app.routers.labs._node_is_running", lambda lab_data, node_id: True)

    response = await update_node(
        "cpu-block-probe.json",
        1,
        request=NodeUpdate(name="rtr", cpu=4, ram=1536, ethernet=4),
        current_user=SimpleNamespace(username="admin", role="admin", html5=True, folder="/"),
    )

    assert response["code"] == 400
    assert response["message"] == "Stop the node before changing: cpu."


@pytest.mark.asyncio
async def test_update_topology_route_is_not_shadowed_by_update_lab(monkeypatch, patched_route_settings):
    lab_path = patched_route_settings.LABS_DIR / "topology-probe.json"
    lab_path.write_text(
        """{
  "schema": 2,
  "id": "topology-probe",
  "meta": {"name": "topology-probe"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {
    "1": {
      "id": 1,
      "name": "node-1",
      "type": "docker",
      "template": "docker",
      "image": "nova-ve/alpine-telnet:latest",
      "console": "telnet",
      "status": 0,
      "cpu": 1,
      "ram": 1024,
      "ethernet": 1,
      "left": 100,
      "top": 100,
      "icon": "Server.png",
      "interfaces": [{"index": 0, "name": "eth0", "planned_mac": null, "port_position": null}]
    }
  },
  "networks": {},
  "links": [],
  "defaults": {"link_style": "orthogonal"}
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
            "nova-ve/alpine-telnet:latest": {
                "image": "nova-ve/alpine-telnet:latest",
                "files": [],
                "path": "nova-ve/alpine-telnet:latest",
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
  "schema": 2,
  "id": "catalog-probe",
  "meta": {"name": "catalog-probe"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {},
  "networks": {},
  "links": [],
  "defaults": {"link_style": "orthogonal"}
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
                "image": "nova-ve/alpine-telnet:latest",
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


async def _stub_db():
    yield None


def _orphan_lab_overrides(role: str) -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="user1" if role != "admin" else "admin",
        role=role,
        html5=True,
        folder="/",
    )
    app.dependency_overrides[get_db] = _stub_db


@pytest.mark.asyncio
async def test_delete_lab_orphan_admin_unlinks_file(monkeypatch, patched_route_settings):
    async def _no_db_row(self, filename):
        return None

    monkeypatch.setattr(
        "app.services.lab_service.LabService.get_lab_by_filename", _no_db_row
    )

    orphan = patched_route_settings.LABS_DIR / "orphan.json"
    orphan.write_text('{"schema": 2, "id": "orphan"}')

    _orphan_lab_overrides("admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete("/api/labs/_/orphan.json")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["status"] == "success"
    assert not orphan.exists()


@pytest.mark.asyncio
async def test_delete_lab_orphan_non_admin_denied(monkeypatch, patched_route_settings):
    async def _no_db_row(self, filename):
        return None

    monkeypatch.setattr(
        "app.services.lab_service.LabService.get_lab_by_filename", _no_db_row
    )

    orphan = patched_route_settings.LABS_DIR / "orphan.json"
    orphan.write_text('{"schema": 2, "id": "orphan"}')

    _orphan_lab_overrides("user")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete("/api/labs/_/orphan.json")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 403
    assert payload["status"] == "fail"
    assert orphan.exists()


@pytest.mark.asyncio
async def test_delete_lab_missing_returns_404(monkeypatch, patched_route_settings):
    async def _no_db_row(self, filename):
        return None

    monkeypatch.setattr(
        "app.services.lab_service.LabService.get_lab_by_filename", _no_db_row
    )

    _orphan_lab_overrides("admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete("/api/labs/_/does-not-exist.json")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 404
    assert payload["status"] == "fail"


@pytest.mark.asyncio
async def test_update_topology_refuses_to_forge_network_runtime_or_type(
    monkeypatch, patched_route_settings
):
    """Bridge-Cloud security regression — codex critic CRIT-1.

    PUT /api/labs/{path}/topology used to blindly write every field from
    each network_patch onto the existing network record.  An
    authenticated lab editor could thus forge
    ``runtime.driver = "bridge_cloud"`` +
    ``runtime.bridge_name = "br-eth0"`` to trick ``link_master_any`` into
    routing a TAP onto the host's physical LAN.  The router now
    whitelists layout-only fields; semantic fields (``type``, ``runtime``,
    ``config``, ``id``, ``implicit``) MUST come through
    ``NetworkService``.
    """
    lab_path = patched_route_settings.LABS_DIR / "forge-probe.json"
    lab_path.write_text(
        """{
  "schema": 2,
  "id": "forge-probe",
  "meta": {"name": "forge-probe"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {},
  "networks": {
    "1": {
      "id": 1,
      "name": "lab-net",
      "type": "linux_bridge",
      "left": 100,
      "top": 100,
      "runtime": {}
    }
  },
  "links": [],
  "defaults": {"link_style": "orthogonal"}
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
            "/api/labs/forge-probe.json/topology",
            json={
                "topology": [],
                "networks": {
                    "1": {
                        # Layout fields (allowed) — should be persisted.
                        "left": 222,
                        "top": 333,
                        "color": "#ff0000",
                        # Semantic fields (forbidden) — must NOT be persisted.
                        "type": "bridge_cloud",
                        "runtime": {"driver": "bridge_cloud", "bridge_name": "br-eth0"},
                        "config": {"host_bridge": "br-eth0"},
                        "id": 999,
                        "implicit": True,
                    }
                },
            },
        )
        assert response.status_code == 200, response.text

    app.dependency_overrides.clear()

    # Reload from disk and verify the forgery did NOT land.
    persisted = json.loads(lab_path.read_text())
    record = persisted["networks"]["1"]
    # Allowed fields persisted:
    assert record["left"] == 222
    assert record["top"] == 333
    assert record["color"] == "#ff0000"
    # Forbidden fields refused:
    assert record["type"] == "linux_bridge", "type forgery must be rejected"
    assert record["runtime"] == {}, "runtime forgery must be rejected"
    assert "config" not in record or record.get("config") in (None, {}), \
        "config forgery must be rejected"
    assert record["id"] == 1, "id forgery must be rejected"
    assert record.get("implicit") in (None, False), \
        "implicit forgery must be rejected"
