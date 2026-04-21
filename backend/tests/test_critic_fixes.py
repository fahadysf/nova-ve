from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers import auth, folders, labs, system
from app.schemas.folder import FolderCreateRequest, FolderRenameRequest
from app.schemas.network import NetworkCreate, NetworkUpdate
from app.schemas.node import NodeCreate, NodeUpdate
from app.schemas.user import UserCreate
from app.services.auth_service import AuthService
from app.services.lab_service import LabService, build_relative_lab_path


class FakeResult:
    def __init__(self, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars)


class FakeDB:
    def __init__(self, execute_results=None):
        self.execute_results = list(execute_results or [])
        self.added = []
        self.deleted = []
        self.commit_count = 0

    async def execute(self, _query):
        if not self.execute_results:
            raise AssertionError("Unexpected execute() call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, _obj):
        return None

    async def delete(self, obj):
        self.deleted.append(obj)


@pytest.fixture()
def runtime_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    labs_dir.mkdir()
    images_dir.mkdir()
    tmp_dir.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        SESSION_MAX_AGE=14400,
    )


@pytest.fixture()
def patched_settings(monkeypatch, runtime_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: runtime_settings)
    monkeypatch.setattr("app.services.folder_service.get_settings", lambda: runtime_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: runtime_settings)
    return runtime_settings


def _write_lab(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.mark.asyncio
async def test_lab_service_preserves_nested_relative_paths(patched_settings):
    db = FakeDB()
    service = LabService(db)

    lab = await service.create_lab(
        owner="admin",
        name="Edge Lab",
        path="/Users/admin",
        author="author",
    )

    expected_path = patched_settings.LABS_DIR / "Users" / "admin" / "Edge-Lab.json"
    assert lab.filename == "Users/admin/Edge-Lab.json"
    assert lab.path == "/Users/admin/Edge-Lab.json"
    assert expected_path.exists()
    assert build_relative_lab_path("Edge Lab", path="/Users/admin") == "Users/admin/Edge-Lab.json"


@pytest.mark.asyncio
async def test_lab_router_supports_node_network_and_topology_mutations(patched_settings):
    lab_path = patched_settings.LABS_DIR / "nested" / "lab.json"
    _write_lab(
        lab_path,
        """{
  "id": "lab-1",
  "meta": {"name": "nested"},
  "nodes": {},
  "networks": {},
  "topology": []
}""",
    )

    current_user = SimpleNamespace(username="admin", role="admin")

    create_node_response = await labs.create_node(
        "nested/lab.json",
        NodeCreate(
            name="router-1",
            template="csr",
            image="csr1000v",
            ethernet=2,
        ),
        current_user=current_user,
    )
    assert create_node_response["code"] == 200
    assert create_node_response["data"]["interfaces"][0]["name"] == "Gi1"

    update_node_response = await labs.update_node(
        "nested/lab.json",
        1,
        NodeUpdate(ethernet=3, left=120, top=90),
        current_user=current_user,
    )
    assert update_node_response["data"]["ethernet"] == 3
    assert len(update_node_response["data"]["interfaces"]) == 3
    assert update_node_response["data"]["left"] == 120

    create_network_response = await labs.create_network(
        "nested/lab.json",
        NetworkCreate(name="net-a"),
        current_user=current_user,
    )
    assert create_network_response["code"] == 200

    update_network_response = await labs.update_network(
        "nested/lab.json",
        1,
        NetworkUpdate(left=400, top=250),
        current_user=current_user,
    )
    assert update_network_response["data"]["left"] == 400

    topology_response = await labs.update_topology(
        "nested/lab.json",
        {
            "topology": [{"source": "node1", "destination": "network1", "network_id": 1}],
            "nodes": {"1": {"left": 200, "top": 150}},
            "networks": {"1": {"left": 410, "top": 255}},
        },
        current_user=current_user,
    )
    assert topology_response["code"] == 200

    saved_lab = LabService.read_lab_json_static("nested/lab.json")
    assert saved_lab["nodes"]["1"]["left"] == 200
    assert saved_lab["networks"]["1"]["left"] == 410
    assert saved_lab["topology"][0]["network_id"] == 1

    delete_network_response = await labs.delete_network("nested/lab.json", 1, current_user=current_user)
    assert delete_network_response["code"] == 200
    saved_lab = LabService.read_lab_json_static("nested/lab.json")
    assert saved_lab["networks"] == {}
    assert saved_lab["topology"] == []

    delete_node_response = await labs.delete_node("nested/lab.json", 1, current_user=current_user)
    assert delete_node_response["code"] == 200
    assert LabService.read_lab_json_static("nested/lab.json")["nodes"] == {}


@pytest.mark.asyncio
async def test_folder_rename_and_delete_propagate_to_lab_records(patched_settings):
    _write_lab(patched_settings.LABS_DIR / "labs" / "edge" / "lab-a.json", "{}")

    lab_record = SimpleNamespace(filename="labs/edge/lab-a.json", path="/labs/edge/lab-a.json")
    db = FakeDB(execute_results=[FakeResult(scalars=[lab_record]), FakeResult(scalars=[lab_record])])
    current_user = SimpleNamespace(username="admin", role="admin")

    create_response = await folders.create_folder(
        FolderCreateRequest(path="/labs", name="edge"),
        current_user=current_user,
    )
    assert create_response["code"] == 400

    rename_response = await folders.rename_folder(
        "labs/edge",
        FolderRenameRequest(path="/labs/core"),
        current_user=current_user,
        db=db,
    )
    assert rename_response["code"] == 200
    assert lab_record.filename == "labs/core/lab-a.json"
    assert (patched_settings.LABS_DIR / "labs" / "core" / "lab-a.json").exists()

    delete_response = await folders.delete_folder("labs/core", current_user=current_user, db=db)
    assert delete_response["code"] == 200
    assert lab_record in db.deleted
    assert not (patched_settings.LABS_DIR / "labs" / "core").exists()


@pytest.mark.asyncio
async def test_register_allows_first_user_without_auth(monkeypatch):
    class FakeAuthService:
        def __init__(self, db):
            self.db = db

        async def create_user(self, **kwargs):
            return SimpleNamespace(**kwargs, extauth="internal", html5=True, online=0, ip=None, lab=None)

    monkeypatch.setattr("app.routers.auth.AuthService", FakeAuthService)
    db = FakeDB(execute_results=[FakeResult(scalar=0), FakeResult(scalar=None)])

    response = await auth.register(
        UserCreate(
            username="admin",
            password="admin123",
            email="admin@example.com",
            name="Admin",
        ),
        db=db,
        current_user=None,
    )
    assert response["code"] == 200
    assert response["data"]["role"] == "admin"


@pytest.mark.asyncio
async def test_auth_session_uses_configured_max_age(monkeypatch):
    monkeypatch.setattr("app.services.auth_service.settings", SimpleNamespace(SESSION_MAX_AGE=60))
    db = FakeDB()
    user = SimpleNamespace(session_token=None, session_expires=None, online=False)

    token = await AuthService(db).create_session(user)
    assert token
    assert user.online is True
    assert 0 < user.session_expires - int(__import__("time").time()) <= 60


@pytest.mark.asyncio
async def test_healthcheck_executes_database_probe():
    db = FakeDB(execute_results=[FakeResult(scalar=1)])
    response = await system.healthcheck(db=db)
    assert response["code"] == 200
    assert response["data"]["database"] == "ok"


def test_lab_route_order_keeps_catch_all_last():
    get_route_paths = [
        route.path
        for route in labs.router.routes
        if "GET" in getattr(route, "methods", set())
    ]
    assert get_route_paths[-1] == "/api/labs/{lab_path:path}"
    assert get_route_paths.index("/api/labs/{lab_path:path}/nodes") < get_route_paths.index("/api/labs/{lab_path:path}")
