# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""#365: the labs explorer shows what is running straight from the runtime
registry — the set the node heartbeat already keeps true — with no second
way to decide what "running" means, no probing on the listing path and no
lab file parsed unless a running lab has no DB row to map its id to a path."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.dependencies import get_current_user
from app.main import app
from app.routers.folders import _merge_db_labs
from app.services.folder_service import FolderService
from app.services.lab_service import LabService
from app.services.node_runtime_service import NodeRuntimeService


@pytest.fixture()
def labs(tmp_path, monkeypatch):
    """LABS_DIR with only the settings the listing reads."""
    labs = tmp_path / "labs"
    labs.mkdir()
    settings = SimpleNamespace(LABS_DIR=labs)
    for module in ("app.services.folder_service", "app.services.lab_service"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    return labs


@pytest.fixture()
def registry():
    """A seeded class-level registry, emptied again afterwards."""
    NodeRuntimeService.reset_registry()
    with NodeRuntimeService._lock:
        NodeRuntimeService._registry.update(
            {
                "lab-a:1": {"lab_id": "lab-a", "node_id": 1, "kind": "qemu"},
                "lab-a:2": {"lab_id": "lab-a", "node_id": 2, "kind": "docker"},
                "lab-b:1": {"lab_id": "lab-b", "node_id": 1, "kind": "qemu"},
            }
        )
        NodeRuntimeService._loaded = True
    yield NodeRuntimeService._registry
    NodeRuntimeService.reset_registry()


def _lab(labs: Path, relative: str, lab_id: str) -> None:
    path = labs / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": lab_id, "nodes": {}}))


def _registry_reports(monkeypatch, running: dict[str, int]) -> None:
    """The registry already knows the running labs; nothing else is touched."""
    monkeypatch.setattr(NodeRuntimeService, "__init__", lambda self: None)
    monkeypatch.setattr(NodeRuntimeService, "running_nodes_by_lab", lambda self: dict(running))


def _track_lab_file_reads(monkeypatch) -> list[Path]:
    reads: list[Path] = []
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        reads.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return reads


def _scan() -> list[dict]:
    return FolderService.list_folder("", recursive=True)["labs"]


def test_running_nodes_by_lab_reads_the_registry_without_probing(registry, monkeypatch):
    """Each probe of a docker node is a ``docker inspect`` subprocess; with
    several of them running, probing turned the labs listing from ~13 ms
    into ~230 ms on the test host."""
    def no_probe(*_args, **_kwargs):
        raise AssertionError("the labs listing must not probe liveness")

    monkeypatch.setattr(NodeRuntimeService, "_runtime_record", no_probe)
    monkeypatch.setattr(NodeRuntimeService, "_check_alive_sync", staticmethod(no_probe))
    service = object.__new__(NodeRuntimeService)

    assert service.running_nodes_by_lab() == {"lab-a": 2, "lab-b": 1}


def test_running_nodes_by_lab_forgets_a_lab_once_its_last_record_is_gone(registry):
    """A stop through the app removes the record at once; the heartbeat
    removes the record of a workload that died. Both drop the lab."""
    service = object.__new__(NodeRuntimeService)
    nowhere = SimpleNamespace(TMP_DIR=Path("/nonexistent-365"))
    assert service.running_nodes_by_lab() == {"lab-a": 2, "lab-b": 1}

    registry.pop("lab-a:1")
    assert service.running_nodes_by_lab() == {"lab-a": 1, "lab-b": 1}

    NodeRuntimeService._discard_inactive_runtime(registry["lab-a:2"], nowhere)
    assert service.running_nodes_by_lab() == {"lab-b": 1}

    NodeRuntimeService._discard_inactive_runtime(registry["lab-b:1"], nowhere)
    assert service.running_nodes_by_lab() == {}


def test_labs_with_a_db_row_are_resolved_without_parsing_a_lab_file(labs, monkeypatch):
    _lab(labs, "tracked.json", "id-tracked")
    _lab(labs, "nested/idle.json", "id-idle")
    _registry_reports(monkeypatch, {"id-tracked": 3})
    reads = _track_lab_file_reads(monkeypatch)
    listed = _scan()

    FolderService.annotate_running(listed, {"/tracked.json": "id-tracked", "/nested/idle.json": "id-idle"})

    assert {lab["path"]: lab["nodes_running"] for lab in listed} == {
        "/nested/idle.json": 0,
        "/tracked.json": 3,
    }
    assert reads == []


def test_running_lab_without_a_db_row_is_resolved_from_its_file(labs, monkeypatch):
    _lab(labs, "alpha.json", "id-alpha")
    _lab(labs, "imported.json", "id-imported")
    _lab(labs, "omega.json", "id-omega")
    _registry_reports(monkeypatch, {"id-imported": 2})
    reads = _track_lab_file_reads(monkeypatch)
    listed = _scan()

    FolderService.annotate_running(listed, {})

    assert {lab["path"]: lab["nodes_running"] for lab in listed} == {
        "/alpha.json": 0,
        "/imported.json": 2,
        "/omega.json": 0,
    }
    # Scanning stops once every running lab is accounted for.
    assert reads == [labs / "alpha.json", labs / "imported.json"]


def test_nothing_running_leaves_every_lab_file_alone(labs, monkeypatch):
    _lab(labs, "alpha.json", "id-alpha")
    _lab(labs, "omega.json", "id-omega")
    _registry_reports(monkeypatch, {})
    reads = _track_lab_file_reads(monkeypatch)
    listed = _scan()

    FolderService.annotate_running(listed, {})

    assert [lab["nodes_running"] for lab in listed] == [0, 0]
    assert reads == []


def test_unreadable_or_malformed_labs_do_not_break_the_listing(labs, monkeypatch):
    (labs / "alpha.json").write_text("{not json")
    (labs / "list.json").write_text("[]")
    _lab(labs, "omega.json", "id-omega")
    _registry_reports(monkeypatch, {"id-omega": 1, "id-gone": 4})
    listed = _scan()

    FolderService.annotate_running(listed, {})

    assert {lab["path"]: lab["nodes_running"] for lab in listed} == {
        "/alpha.json": 0,
        "/list.json": 0,
        "/omega.json": 1,
    }


class _FakeDb:
    pass


class _FakeLabService(LabService):
    """Subclasses the real service so static helpers (file_exists) stay genuine."""

    rows: list = []

    def __init__(self, db):
        pass

    async def list_labs(self):
        return list(self.rows)


def _row(filename: str, lab_id: str):
    return SimpleNamespace(id=lab_id, filename=filename, path=f"/{filename}", owner="admin")


@pytest.mark.asyncio
async def test_merge_db_labs_collects_ids_of_rows_with_a_backing_file(labs, monkeypatch):
    _lab(labs, "tracked.json", "id-tracked")
    _FakeLabService.rows = [_row("tracked.json", "id-tracked"), _row("ghost.json", "id-ghost")]
    monkeypatch.setattr("app.routers.folders.LabService", _FakeLabService)
    lab_ids: dict[str, str] = {}

    merged = await _merge_db_labs({"folders": [], "labs": []}, _FakeDb(), "", recursive=True, lab_ids=lab_ids)

    assert [lab["file"] for lab in merged["labs"]] == ["tracked.json"]
    assert lab_ids == {"/tracked.json": "id-tracked"}


def _admin():
    return SimpleNamespace(
        id=1, username="admin", role="admin", html5=True, folder="/",
        email="admin@example.invalid", name="Admin",
    )


def _scoped_user():
    return SimpleNamespace(
        id=2, username="alice", role="user", html5=True, folder="/alice",
        email="alice@example.invalid", name="Alice",
    )


async def _get(path: str) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get(path)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    return body["data"]


@pytest.fixture()
def listing(labs, monkeypatch):
    _lab(labs, "tracked.json", "id-tracked")
    _lab(labs, "imported.json", "id-imported")
    _lab(labs, "idle.json", "id-idle")
    _lab(labs, "alice/own.json", "id-own")
    _lab(labs, "Shared/team.json", "id-team")
    _FakeLabService.rows = [
        _row("tracked.json", "id-tracked"),
        _row("idle.json", "id-idle"),
        _row("Shared/team.json", "id-team"),
    ]
    monkeypatch.setattr("app.routers.folders.LabService", _FakeLabService)
    _registry_reports(
        monkeypatch, {"id-tracked": 5, "id-imported": 1, "id-own": 2, "id-team": 9}
    )
    app.dependency_overrides[get_db] = lambda: None
    yield
    app.dependency_overrides.clear()


def _running_by_path(data: dict) -> dict[str, int]:
    return {lab["path"]: lab["nodes_running"] for lab in data["labs"]}


@pytest.mark.asyncio
async def test_root_listing_carries_nodes_running_for_every_lab(listing, monkeypatch):
    app.dependency_overrides[get_current_user] = _admin

    data = await _get("/api/folders/")

    assert _running_by_path(data) == {
        "/Shared/team.json": 9,
        "/alice/own.json": 2,
        "/idle.json": 0,
        "/imported.json": 1,
        "/tracked.json": 5,
    }


@pytest.mark.asyncio
async def test_scoped_user_sees_running_counts_for_shared_labs_too(listing, monkeypatch):
    """/api/manage/labs scopes to the user's own root only; the folder
    listing annotates exactly what it lists, Shared included (#285)."""
    app.dependency_overrides[get_current_user] = _scoped_user

    own = await _get("/api/folders/")
    shared = await _get("/api/folders/Shared")

    assert _running_by_path(own) == {"/alice/own.json": 2}
    assert _running_by_path(shared) == {"/Shared/team.json": 9}
