# SPDX-License-Identifier: Apache-2.0

"""#327: every route that takes a lab path refuses a lab the caller cannot open.

The sweep walks the registered routes, so a new ``{lab_path}`` route is
covered without editing this file.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.testclient import WebSocketDisconnect

from app.database import get_db
from app.dependencies import get_current_user
from app.main import app
from app.services.lab_service import LabService


SCOPED_FOLDER = "/lanes/alpha"
OWN_LAB = "lanes/alpha/lab.json"
FOREIGN_LAB = "lanes/beta/lab.json"
SHARED_LAB = "Shared/lab.json"

# Admin-only routes refuse every non-admin before they look at the lab, with
# the manage router's own envelope (HTTP 200, ``code`` 403). Nothing else
# belongs here: any other lab-path route must refuse through LAB_ACCESS.
ADMIN_ONLY_ROUTES = {
    ("POST", "/api/manage/labs/{lab_path:path}/stop"),
    ("POST", "/api/manage/labs/{lab_path:path}/wipe"),
}

# Previously unchecked (#327); the sweep must reach them.
FORMERLY_UNCHECKED = {
    ("GET", "/api/labs/{lab_path:path}/networks"),
    ("POST", "/api/labs/{lab_path:path}/networks"),
    ("PATCH", "/api/labs/{lab_path:path}/networks/{network_id}"),
    ("DELETE", "/api/labs/{lab_path:path}/networks/{network_id}"),
    ("POST", "/api/labs/{lab_path:path}/runtime/reconcile"),
    ("GET", "/api/labs/{lab_path:path}/links"),
    ("POST", "/api/labs/{lab_path:path}/links"),
    ("PATCH", "/api/labs/{lab_path:path}/links/{link_id}"),
    ("DELETE", "/api/labs/{lab_path:path}/links/{link_id}"),
}


def _lab_path_routes() -> list[tuple[str, str]]:
    found = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and "{lab_path" in route.path:
            for method in route.methods - {"HEAD"}:
                found.add((method, route.path))
    return sorted(found)


def _url(route_path: str, lab_path: str) -> str:
    url = route_path.replace("{lab_path:path}", lab_path)
    return re.sub(r"\{[^}]+\}", "1", url)


def _node(node_id: int) -> dict:
    return {
        "id": node_id,
        "name": f"n{node_id}",
        "type": "docker",
        "template": "docker",
        "image": "nova-ve/alpine-telnet:latest",
        "console": "telnet",
        "status": 0,
        "cpu": 1,
        "ram": 256,
        "ethernet": 2,
        "left": 100,
        "top": 100,
        "icon": "Server.png",
        "interfaces": [
            {"index": i, "name": f"eth{i}", "planned_mac": None, "port_position": None}
            for i in range(2)
        ],
    }


def _seed_lab(labs_dir, rel_path: str) -> None:
    path = labs_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "id": rel_path.replace("/", "-").replace(".json", ""),
                "meta": {"name": rel_path},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": {"1": _node(1)},
                "networks": {},
                "links": [],
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )


def _saved(labs_dir, rel_path: str) -> dict:
    return json.loads((labs_dir / rel_path).read_text())


def _scoped_user():
    return SimpleNamespace(
        id=2, username="alice", role="user", html5=True, folder=SCOPED_FOLDER,
        email="alice@example.invalid", name="Alice",
    )


def _admin():
    return SimpleNamespace(
        id=1, username="admin", role="admin", html5=True, folder="/",
        email="admin@example.invalid", name="Admin",
    )


@pytest.fixture()
def labs_dir(monkeypatch, tmp_path):
    labs = tmp_path / "labs"
    tmp_dir = tmp_path / "tmp"
    for d in (labs, tmp_dir, tmp_path / "images", tmp_path / "templates"):
        d.mkdir()
    settings = SimpleNamespace(
        LABS_DIR=labs,
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_dir,
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        NAT_CLOUD_POOL="10.255.0.0/16",
    )
    for target in (
        "app.services.lab_service.get_settings",
        "app.services.link_service.get_settings",
        "app.services.network_service.get_settings",
        "app.services.node_runtime_service.get_settings",
        "app.routers.labs.get_settings",
    ):
        monkeypatch.setattr(target, lambda: settings)

    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("test-instance-uuid")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    for name, stub in {
        "bridge_add": lambda name: None,
        "bridge_del": lambda name: None,
        "bridge_exists": lambda name: False,
        "link_up": lambda iface: None,
        "list_ipv4_addrs": lambda: [],
    }.items():
        monkeypatch.setattr(f"app.services.host_net.{name}", stub)

    async def _no_publish(lab_id, event_type, payload, rev=""):
        return SimpleNamespace(seq=0, type=event_type, rev=rev, payload=payload)

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", _no_publish)

    for rel in (OWN_LAB, FOREIGN_LAB, SHARED_LAB):
        _seed_lab(labs, rel)
    yield labs
    app.dependency_overrides.clear()


@pytest.fixture()
def lab_reads(monkeypatch):
    """Record every way a route can reach a lab file or its DB record."""
    calls: list[tuple[str, tuple]] = []

    def _recording(name, real):
        def wrapper(*args, **kwargs):
            calls.append((name, args))
            return real(*args, **kwargs)

        return wrapper

    import app.routers.labs as labs_router
    import app.routers.manage as manage_router
    import app.services.lab_service as lab_service

    monkeypatch.setattr(
        LabService,
        "read_lab_json_static",
        staticmethod(_recording("read_lab_json_static", LabService.read_lab_json_static)),
    )
    monkeypatch.setattr(
        lab_service, "_lab_file_path", _recording("_lab_file_path", lab_service._lab_file_path)
    )
    monkeypatch.setattr(
        labs_router, "_lab_file_path", _recording("labs._lab_file_path", labs_router._lab_file_path)
    )
    monkeypatch.setattr(
        manage_router,
        "_resolve_lab_file",
        _recording("manage._resolve_lab_file", manage_router._resolve_lab_file),
    )

    async def _get_lab_by_filename(self, filename):
        calls.append(("get_lab_by_filename", (filename,)))
        return None

    monkeypatch.setattr(LabService, "get_lab_by_filename", _get_lab_by_filename)
    return calls


def test_sweep_finds_the_formerly_unchecked_routes():
    routes = set(_lab_path_routes())
    assert FORMERLY_UNCHECKED <= routes
    assert ADMIN_ONLY_ROUTES <= routes


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "route_path"), _lab_path_routes())
async def test_scoped_user_is_refused_a_lab_outside_their_folder(
    method, route_path, labs_dir, lab_reads
):
    app.dependency_overrides[get_current_user] = _scoped_user
    app.dependency_overrides[get_db] = lambda: None
    before = (labs_dir / FOREIGN_LAB).read_text()

    kwargs = {"json": {}} if method in {"POST", "PUT", "PATCH"} else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.request(method, _url(route_path, FOREIGN_LAB), **kwargs)

    body = response.json()
    if (method, route_path) in ADMIN_ONLY_ROUTES:
        assert (response.status_code, body["code"]) == (200, 403), response.text
        assert body["message"] == "Admin role required."
    else:
        assert response.status_code == 403, response.text
        assert body == {"code": 403, "status": "fail", "message": "Access denied."}
    assert lab_reads == []
    assert (labs_dir / FOREIGN_LAB).read_text() == before


async def _network_and_link_round_trip(lab_path: str, labs_dir) -> None:
    """Drive every formerly unchecked route against ``lab_path``."""
    base = f"/api/labs/{lab_path}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        created = await c.post(f"{base}/networks", json={"name": "core", "type": "linux_bridge"})
        assert created.status_code == 201, created.text
        network_id = created.json()["network"]["id"]

        listed = await c.get(f"{base}/networks")
        assert listed.json()["code"] == 200, listed.text
        assert str(network_id) in {str(k) for k in listed.json()["data"]}

        patched = await c.patch(f"{base}/networks/{network_id}", json={"left": 321})
        assert patched.status_code == 200, patched.text

        link = await c.post(
            f"{base}/links",
            json={"from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": network_id}},
        )
        assert link.status_code == 201, link.text
        link_id = link.json()["link"]["id"]

        links = await c.get(f"{base}/links")
        assert [item["id"] for item in links.json()["data"]] == [link_id]

        styled = await c.patch(f"{base}/links/{link_id}", json={"style_override": "bezier"})
        assert styled.status_code == 200, styled.text

        unlinked = await c.delete(f"{base}/links/{link_id}")
        assert unlinked.json()["message"] == "Link deleted successfully.", unlinked.text

        reconciled = await c.post(f"{base}/runtime/reconcile")
        assert reconciled.json()["code"] == 200, reconciled.text

        removed = await c.delete(f"{base}/networks/{network_id}")
        assert removed.status_code == 200, removed.text

    saved = _saved(labs_dir, lab_path)
    assert saved["links"] == []
    assert str(network_id) not in saved["networks"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "lab_path"),
    [
        pytest.param(_scoped_user, OWN_LAB, id="owner-in-own-folder"),
        pytest.param(_scoped_user, SHARED_LAB, id="scoped-user-in-shared"),
        pytest.param(_admin, FOREIGN_LAB, id="admin-anywhere"),
    ],
)
async def test_callers_who_can_open_the_lab_still_succeed(user, lab_path, labs_dir):
    app.dependency_overrides[get_current_user] = user
    await _network_and_link_round_trip(lab_path, labs_dir)


@pytest.mark.asyncio
async def test_relative_lab_path_is_still_resolved_absolutely(labs_dir, lab_reads):
    # A scoped user naming a lab relative to their root is not silently
    # redirected into it: lab-path routes treat the path as absolute.
    app.dependency_overrides[get_current_user] = _scoped_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.get("/api/labs/lab.json/networks")
    assert response.status_code == 403
    assert lab_reads == []


def test_websocket_refuses_a_lab_outside_the_users_folder(monkeypatch, labs_dir):
    async def _validate_session(self, token, username):
        return _scoped_user()

    monkeypatch.setattr(
        "app.services.auth_service.AuthService.validate_session", _validate_session
    )
    monkeypatch.setattr("app.schemas.user.UserRead.model_validate", lambda obj: obj)
    _seed_lab(labs_dir, "root-lab.json")

    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.update({"nova_session": "token", "nova_user": "alice"})
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/labs/root-lab.json?last_seq=99"):
            pass
    assert exc_info.value.code == 1008
