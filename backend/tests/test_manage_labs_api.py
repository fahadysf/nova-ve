# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lab management API tests (#262): listing scope, state summary, and the
stop/wipe lifecycle actions."""

import asyncio
import json
import threading
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
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
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


class _FakeListRuntime:
    """Registry stand-in for the listing: running node ids per lab id."""

    def __init__(self, running_by_lab=None):
        self.running_by_lab = running_by_lab or {}

    def is_node_running(self, lab_id, node_id):
        return node_id in self.running_by_lab.get(lab_id, set())


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
async def test_listing_reports_state_and_allocations(client, manage_settings, monkeypatch):
    _write_lab(manage_settings, "arc-lab.json", running=2, total=3, cpu=2, ram=2048)
    _write_lab(manage_settings, "idle-lab.json", running=0, total=1)
    _as("admin")

    import app.routers.manage as manage_module

    # Registry-authoritative: nodes 1 and 2 of arc-lab hold runtimes; the
    # persisted status fields are deliberately stale (all status=2 on
    # arc-lab, one status=2 on idle-lab) and must be ignored.
    monkeypatch.setattr(
        manage_module,
        "NodeRuntimeService",
        lambda: _FakeListRuntime({"arc-lab": {1, 2}}),
    )

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
    assert labs["idle-lab.json"]["nodes_running"] == 0
    assert data["totals"]["running"] == 1
    assert data["totals"]["nodes_running"] == 2


@pytest.mark.asyncio
async def test_disk_scan_does_not_block_event_loop(client, manage_settings, monkeypatch):
    _write_lab(manage_settings, "lab.json")
    _as("admin")
    monkeypatch.setattr("app.routers.manage.NodeRuntimeService", lambda: _FakeListRuntime())
    started = threading.Event()
    release = threading.Event()

    def slow_scan(lab_id):
        started.set()
        release.wait(timeout=5)
        return 0

    monkeypatch.setattr("app.routers.manage._lab_disk_bytes", slow_scan)
    request = asyncio.create_task(client.get("/api/manage/labs"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert not request.done()
    finally:
        release.set()
    assert (await request).status_code == 200


@pytest.mark.asyncio
async def test_non_admin_listing_scoped_to_folder_root(client, manage_settings, monkeypatch):
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

    import app.routers.manage as manage_module

    monkeypatch.setattr(
        manage_module, "NodeRuntimeService", lambda: _FakeListRuntime()
    )

    response = await client.get("/api/manage/labs")

    files = [lab["file"] for lab in response.json()["data"]["labs"]]
    assert files == ["scoped.json"]


@pytest.mark.asyncio
async def test_disk_usage_counts_allocated_files_for_stopped_labs_and_scoped_totals(client, manage_settings, monkeypatch):
    nested = manage_settings.LABS_DIR / "trainee"
    nested.mkdir()
    (nested / "scoped.json").write_text(json.dumps({"id": "scoped", "nodes": {}}))
    _write_lab(manage_settings, "admin-lab.json")
    scoped_dir = manage_settings.TMP_DIR / "scoped" / "1"
    scoped_dir.mkdir(parents=True)
    disk = scoped_dir / "virtioa.qcow2"
    with disk.open("wb") as f:
        f.write(b"data")
        f.truncate(64 * 1024 * 1024)
    (scoped_dir / "duplicate").hardlink_to(disk)
    admin_dir = manage_settings.TMP_DIR / "admin-lab" / "1"
    admin_dir.mkdir(parents=True)
    admin_disk = admin_dir / "disk"
    admin_disk.write_bytes(b"admin")
    (scoped_dir / "external").symlink_to(admin_disk)
    monkeypatch.setattr("app.routers.manage.NodeRuntimeService", lambda: _FakeListRuntime())

    _as("admin")
    data = (await client.get("/api/manage/labs")).json()["data"]
    labs = {lab["file"]: lab for lab in data["labs"]}
    scoped_bytes = disk.stat().st_blocks * 512
    admin_bytes = admin_disk.stat().st_blocks * 512
    assert labs["scoped.json"]["state"] == "stopped"
    assert labs["scoped.json"]["disk_bytes"] == scoped_bytes
    assert scoped_bytes < disk.stat().st_size
    assert data["totals"]["disk_bytes"] == scoped_bytes + admin_bytes

    _as("user", username="trainee", folder="/trainee")
    scoped_data = (await client.get("/api/manage/labs")).json()["data"]
    assert scoped_data["totals"]["disk_bytes"] == scoped_bytes
    assert len(scoped_data["labs"]) == 1


@pytest.mark.asyncio
async def test_disk_usage_unknown_for_unsafe_storage_and_zero_for_missing(client, manage_settings, monkeypatch):
    _write_lab(manage_settings, "missing.json")
    (manage_settings.LABS_DIR / "unsafe.json").write_text(json.dumps({"id": "../outside", "nodes": {}}))
    monkeypatch.setattr("app.routers.manage.NodeRuntimeService", lambda: _FakeListRuntime())
    _as("admin")
    data = (await client.get("/api/manage/labs")).json()["data"]
    labs = {lab["file"]: lab for lab in data["labs"]}
    assert labs["missing.json"]["disk_bytes"] == 0
    assert labs["unsafe.json"]["disk_bytes"] is None
    assert data["totals"]["disk_bytes"] is None


def test_disk_usage_reports_unreadable_storage_as_unknown(manage_settings, monkeypatch):
    from app.routers.manage import _lab_disk_bytes

    (manage_settings.TMP_DIR / "lab").mkdir()

    def inaccessible(*args, onerror, **kwargs):
        onerror(PermissionError("unreadable"))

    monkeypatch.setattr("app.routers.manage.os.walk", inaccessible)
    assert _lab_disk_bytes("lab") is None


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


# ----------------------------------------------------------------------
# Node management (#263)
# ----------------------------------------------------------------------
def _write_running_lab(settings, filename, *, lab_id, running_ids, stopped_ids=(), template="veos", node_type="qemu", cpu=2, ram=2048):
    nodes = {}
    for i in sorted(running_ids + tuple(stopped_ids)):
        nodes[str(i)] = {
            "id": i,
            "name": f"node-{i}",
            "type": node_type,
            "template": template,
            "image": "some-image",
            "status": 2 if i in running_ids else 0,
            "cpu": cpu,
            "ram": ram,
            "console": "telnet",
        }
    lab = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": filename.replace(".json", "")},
        "nodes": nodes,
    }
    (settings.LABS_DIR / filename).write_text(json.dumps(lab))


@pytest.mark.asyncio
async def test_node_inventory_lists_running_nodes_with_usage(client, manage_settings, monkeypatch):
    _write_running_lab(
        manage_settings, "arc-lab.json", lab_id="arc-id", running_ids=(1,), stopped_ids=(2,)
    )
    _write_running_lab(
        manage_settings, "other-lab.json", lab_id="other-id", running_ids=(5,), template="alpine", node_type="docker"
    )
    _as("admin")

    import app.routers.manage as manage_module

    calls: list[tuple[str, int]] = []

    class _FakeRuntime:
        def enrich_node(self, lab_id, node_id, node):
            running = node.get("status") == 2
            enriched = dict(node)
            enriched["status"] = 2 if running else 0
            enriched["url"] = "telnet://host:port" if running else None
            enriched["cpu_usage"] = 12.5 if running else 0
            enriched["ram_usage"] = 256 if running else 0
            enriched["disk_usage"] = 1024
            calls.append((lab_id, node_id))
            return enriched

    monkeypatch.setattr(manage_module, "NodeRuntimeService", lambda: _FakeRuntime())

    response = await client.get("/api/manage/nodes")

    assert response.status_code == 200
    data = response.json()["data"]
    # Default filter: running only.
    assert [row["node_id"] for row in data["nodes"]] == [1, 5]
    assert data["totals"]["running"] == 2
    assert data["totals"]["nodes"] == 2
    assert data["nodes"][0]["lab_name"] == "arc-lab"
    assert data["nodes"][0]["cpu_usage"] == 12.5
    assert data["nodes"][0]["url"] == "telnet://host:port"
    assert calls  # every node was probed through the runtime service


@pytest.mark.asyncio
async def test_node_inventory_filters(client, manage_settings, monkeypatch):
    _write_running_lab(
        manage_settings, "arc-lab.json", lab_id="arc-id", running_ids=(1,), stopped_ids=(2,)
    )
    _as("admin")

    class _FakeRuntime:
        def enrich_node(self, lab_id, node_id, node):
            enriched = dict(node)
            enriched["status"] = 2 if node.get("status") == 2 else 0
            enriched["url"] = None
            return enriched

    import app.routers.manage as manage_module

    monkeypatch.setattr(manage_module, "NodeRuntimeService", lambda: _FakeRuntime())

    all_response = await client.get("/api/manage/nodes", params={"state": "all"})
    assert sorted(row["node_id"] for row in all_response.json()["data"]["nodes"]) == [1, 2]

    stopped_response = await client.get("/api/manage/nodes", params={"state": "stopped"})
    assert [row["node_id"] for row in stopped_response.json()["data"]["nodes"]] == [2]

    template_response = await client.get("/api/manage/nodes", params={"state": "all", "template": "veos"})
    assert len(template_response.json()["data"]["nodes"]) == 2

    lab_response = await client.get("/api/manage/nodes", params={"state": "all", "lab": "arc"})
    assert len(lab_response.json()["data"]["nodes"]) == 2


@pytest.mark.asyncio
async def test_node_inventory_requires_admin(client, manage_settings):
    _as("user", username="trainee", folder="/trainee")

    response = await client.get("/api/manage/nodes")

    assert response.status_code == 200
    assert response.json()["code"] == 403
