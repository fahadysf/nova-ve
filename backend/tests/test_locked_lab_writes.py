"""Every lab-file write takes ``lab_lock`` and re-reads under it (#328).

Each test holds the lab's lock in a worker thread — the way NAT-Cloud saves
and lab-open reconciliation do — and changes the lab while a route runs.
Both changes must be in the file afterwards, on different revisions.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.routers import folders, labs
from app.schemas.folder import FolderRenameRequest
from app.schemas.lab import LabMoveRequest
from app.schemas.network import NetworkCreate, NetworkUpdate
from app.schemas.node import NodeBatchCreate, NodeCreate, NodeUpdate
from app.services import network_service as network_service_mod
from app.services.cloud_inventory_service import nat_cloud_ipam_lock
from app.services.lab_lock import LabLockTimeout, lab_lock, lab_lock_async
from app.services.lab_service import LabService, locked_lab
from app.services.node_runtime_service import NodeRuntimeService
from app.services.network_service import NetworkService

LAB = "race.json"
HOLD_S = 0.3
ADMIN = SimpleNamespace(username="admin", role="admin", html5=True, folder="/")


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    s = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for directory in (s.LABS_DIR, s.IMAGES_DIR, s.TMP_DIR, s.TEMPLATES_DIR):
        directory.mkdir()
    for module in (
        "app.routers.labs",
        "app.services.lab_service",
        "app.services.template_service",
        "app.services.node_runtime_service",
        "app.services.network_service",
        "app.services.link_service",
        "app.services.cloud_inventory_service",
        "app.services.folder_service",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: s)

    async def no_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", no_publish)
    return s


@pytest.fixture()
def docker_template(settings, monkeypatch):
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
    template_dir = settings.TEMPLATES_DIR / "docker"
    template_dir.mkdir()
    (template_dir / "docker.yml").write_text(
        "type: docker\nname: Docker Host\ncpu: 1\nram: 1024\nethernet: 1\n"
        "console_type: telnet\nicon_type: server\ncpulimit: 1\n"
    )


def _seed(settings, *, nodes=None, networks=None) -> None:
    (settings.LABS_DIR / LAB).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": "race-lab",
                "meta": {"name": "race", "revision": 0},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": nodes or {},
                "networks": networks or {},
                "links": [],
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )


def _saved(settings) -> dict:
    return json.loads((settings.LABS_DIR / LAB).read_text())


def _docker_node(node_id: int, name: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "type": "docker",
        "template": "docker",
        "image": "nova-ve/alpine-telnet:latest",
        "console": "telnet",
        "ethernet": 1,
        "left": 0,
        "top": 0,
        "interfaces": [{"index": 0, "name": "eth0", "network_id": 0}],
    }


def _bridge(network_id: int, name: str) -> dict:
    return {
        "id": network_id,
        "name": name,
        "type": "linux_bridge",
        "left": 0,
        "top": 0,
        "visibility": True,
        "implicit": False,
        "config": {},
    }


def _add_node_9(data: dict) -> None:
    data.setdefault("nodes", {})["9"] = _docker_node(9, "added-meanwhile")


def _add_network_9(data: dict) -> None:
    data.setdefault("networks", {})["9"] = _bridge(9, "added-meanwhile")


@contextmanager
def _slow_locked_writer(mutate):
    """Hold the lab's lock in a thread, change the lab, write it on release."""
    holding = threading.Event()
    result: dict = {}

    def run() -> None:
        with locked_lab(LAB) as lab:
            holding.set()
            time.sleep(HOLD_S)
            mutate(lab.data)
            result["revision"] = lab.save()

    thread = threading.Thread(target=run)
    thread.start()
    assert holding.wait(2), "writer never took the lock"
    try:
        yield result
    finally:
        thread.join(5)


def _assert_distinct_revisions(writer: dict, route_revision, settings) -> None:
    assert writer["revision"] is not None and route_revision is not None
    assert writer["revision"] != route_revision
    assert _saved(settings)["meta"]["revision"] == max(writer["revision"], route_revision)


@pytest.mark.asyncio
async def test_create_node_keeps_a_concurrent_locked_edit(settings, docker_template):
    _seed(settings)

    with _slow_locked_writer(_add_network_9) as writer:
        response = await labs.create_node(
            LAB,
            NodeCreate(
                name="new", type="docker", template="docker", image="nova-ve/alpine-telnet:latest"
            ),
            current_user=ADMIN,
        )

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert "9" in saved["networks"]
    assert saved["nodes"]["1"]["name"] == "new"
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_create_node_takes_the_next_id_from_the_locked_read(settings, docker_template):
    _seed(settings)

    with _slow_locked_writer(_add_node_9) as writer:
        response = await labs.create_node(
            LAB,
            NodeCreate(
                name="new", type="docker", template="docker", image="nova-ve/alpine-telnet:latest"
            ),
            current_user=ADMIN,
        )

    saved = _saved(settings)
    assert saved["nodes"]["9"]["name"] == "added-meanwhile"
    assert saved["nodes"]["10"]["name"] == "new"
    assert response["data"]["id"] == 10
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_create_nodes_batch_keeps_a_concurrent_locked_edit(settings, docker_template):
    _seed(settings)

    with _slow_locked_writer(_add_network_9) as writer:
        response = await labs.create_nodes_batch(
            LAB,
            NodeBatchCreate(
                name_prefix="batch",
                count=2,
                type="docker",
                template="docker",
                image="nova-ve/alpine-telnet:latest",
            ),
            current_user=ADMIN,
        )

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert "9" in saved["networks"]
    assert sorted(saved["nodes"]) == ["1", "2"]
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_update_node_keeps_a_concurrent_locked_edit(settings):
    _seed(settings, nodes={"1": _docker_node(1, "old")})

    with _slow_locked_writer(_add_network_9) as writer:
        response = await labs.update_node(LAB, 1, NodeUpdate(name="renamed"), current_user=ADMIN)

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert "9" in saved["networks"]
    assert saved["nodes"]["1"]["name"] == "renamed"
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_update_node_applies_the_edit_to_the_locked_read(settings):
    """The writer's change to the same node is kept; only the edited field moves."""
    _seed(settings, nodes={"1": _docker_node(1, "old")})

    def move_node(data: dict) -> None:
        data["nodes"]["1"]["left"] = 400

    with _slow_locked_writer(move_node):
        await labs.update_node(LAB, 1, NodeUpdate(name="renamed"), current_user=ADMIN)

    node = _saved(settings)["nodes"]["1"]
    assert (node["name"], node["left"]) == ("renamed", 400)


@pytest.mark.asyncio
async def test_delete_node_does_not_bring_back_or_lose_state(settings):
    _seed(settings, nodes={"1": _docker_node(1, "doomed")})

    with _slow_locked_writer(_add_network_9) as writer:
        response = await labs.delete_node(LAB, 1, current_user=ADMIN)

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert "1" not in saved["nodes"]
    assert "9" in saved["networks"]
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_legacy_create_network_keeps_a_concurrent_locked_edit(settings):
    _seed(settings)

    with _slow_locked_writer(_add_network_9) as writer:
        response = await labs.create_network(LAB, NetworkCreate(name="lan"), current_user=ADMIN)

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert saved["networks"]["9"]["name"] == "added-meanwhile"
    assert saved["networks"]["10"]["name"] == "lan"
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_legacy_update_network_keeps_a_concurrent_locked_edit(settings):
    _seed(settings, networks={"1": _bridge(1, "old")})

    with _slow_locked_writer(_add_node_9) as writer:
        response = await labs.update_network(
            LAB, 1, NetworkUpdate(name="renamed"), current_user=ADMIN
        )

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert "9" in saved["nodes"]
    assert saved["networks"]["1"]["name"] == "renamed"
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_legacy_delete_network_keeps_a_concurrent_locked_edit(settings):
    _seed(settings, networks={"1": _bridge(1, "doomed")})

    with _slow_locked_writer(_add_node_9) as writer:
        response = await labs.delete_network(LAB, 1, current_user=ADMIN)

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert "9" in saved["nodes"]
    assert "1" not in saved["networks"]
    _assert_distinct_revisions(writer, response["revision"], settings)


@pytest.mark.asyncio
async def test_update_lab_meta_keeps_a_concurrent_locked_edit(settings):
    _seed(settings)
    lab_row = SimpleNamespace(
        filename=LAB,
        name="race",
        author=None,
        description=None,
        body=None,
        version=None,
        scripttimeout=None,
        countdown=None,
        linkwidth=None,
        grid=None,
        lock=None,
    )

    class FakeDb:
        async def commit(self):
            return None

        async def refresh(self, _obj):
            return None

    with _slow_locked_writer(_add_node_9) as writer:
        await LabService(FakeDb()).update_lab(lab_row, name="renamed")

    saved = _saved(settings)
    assert "9" in saved["nodes"]
    assert saved["meta"]["name"] == "renamed"
    assert saved["meta"]["revision"] == writer["revision"] + 1


@pytest.mark.asyncio
async def test_patch_network_runs_off_the_event_loop_and_keeps_a_node_edit(settings, monkeypatch):
    """#320 regression, now with the PATCH in a worker thread (#328)."""
    _seed(
        settings,
        nodes={"1": _docker_node(1, "old")},
        networks={
            "1": {
                **_bridge(1, "internet"),
                "type": "nat_cloud",
                "config": {"cidr": "10.91.0.0/24", "dhcp": True},
                "runtime": {"bridge_name": "nve-race-n1"},
            }
        },
    )
    dhcp_busy = threading.Event()

    def slow_dhcp(_bridge, _config):
        dhcp_busy.set()
        time.sleep(HOLD_S)
        return 0

    monkeypatch.setattr(network_service_mod, "_reconcile_nat_cloud_dhcp", slow_dhcp)

    patch = asyncio.create_task(
        NetworkService().patch_network(LAB, 1, {"config": {"dhcp": False}})
    )
    assert await asyncio.to_thread(dhcp_busy.wait, 2)
    # The event loop is free while dnsmasq stops.
    assert not patch.done()
    response = await labs.update_node(LAB, 1, NodeUpdate(name="renamed"), current_user=ADMIN)
    await patch

    saved = _saved(settings)
    assert saved["nodes"]["1"]["name"] == "renamed"
    assert saved["networks"]["1"]["config"]["dhcp"] is False
    assert response["revision"] == 2
    assert saved["meta"]["revision"] == 2


@pytest.mark.asyncio
async def test_lab_lock_async_waits_for_a_thread_holder_without_blocking_the_loop(tmp_path):
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lab_lock("lab.json", tmp_path):
            holding.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(2)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    async def waiter() -> float:
        async with lab_lock_async("lab.json", tmp_path):
            return time.monotonic()

    ticker = asyncio.create_task(tick())
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0.2)
    assert not waiting.done()
    released_at = time.monotonic()
    release.set()
    acquired_at = await waiting
    await ticker
    thread.join(2)

    assert acquired_at >= released_at
    assert ticks >= 5


@pytest.mark.asyncio
async def test_lab_lock_async_times_out(tmp_path):
    with lab_lock("lab.json", tmp_path):
        with pytest.raises(LabLockTimeout):
            async with lab_lock_async("lab.json", tmp_path, timeout_s=0.1):
                pass


async def _ticks_while(coro):
    """Run ``coro``; count how often the event loop got to run meanwhile."""
    ticks = 0
    finished = False

    async def tick() -> None:
        nonlocal ticks
        while not finished:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        result = await coro
    finally:
        finished = True
        await ticker
    return result, ticks


class _FakeDb:
    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None

    async def delete(self, _obj):
        return None


def test_locked_lab_save_keeps_its_bump_for_a_later_bookkeeping_save(settings):
    _seed(settings)

    with locked_lab(LAB) as lab:
        assert lab.save() == 1
        lab.data["nodes"]["1"] = _docker_node(1, "stamp")
        assert lab.save(bump_revision=False) == 1

    assert _saved(settings)["meta"]["revision"] == 1


@pytest.mark.asyncio
async def test_layout_and_topology_report_the_revision_they_wrote(settings):
    _seed(settings, nodes={"1": _docker_node(1, "n1")})
    request = SimpleNamespace(headers={})

    layout = await labs.put_layout(
        LAB, request, body={"nodes": [{"id": 1, "left": 5, "top": 6}]}, current_user=ADMIN
    )
    assert layout["revision"] == _saved(settings)["meta"]["revision"] == 1

    topology = await labs.update_topology(
        LAB, request, payload={"nodes": {"1": {"name": "renamed"}}}, current_user=ADMIN
    )
    assert topology["revision"] == _saved(settings)["meta"]["revision"] == 2


@pytest.mark.asyncio
async def test_delete_node_returns_404_when_the_node_went_away_meanwhile(settings):
    _seed(settings, nodes={"1": _docker_node(1, "doomed")})

    def remove_node(data: dict) -> None:
        data["nodes"].pop("1")

    with _slow_locked_writer(remove_node) as writer:
        response = await labs.delete_node(LAB, 1, current_user=ADMIN)

    assert response["code"] == 404, response
    assert _saved(settings)["meta"]["revision"] == writer["revision"]


@pytest.mark.asyncio
async def test_delete_node_keeps_a_new_node_that_reused_the_id(settings):
    _seed(settings, nodes={"1": {**_docker_node(1, "doomed"), "uuid": "old-uuid"}})

    def replace_node(data: dict) -> None:
        data["nodes"]["1"] = {**_docker_node(1, "newcomer"), "uuid": "new-uuid"}

    with _slow_locked_writer(replace_node):
        response = await labs.delete_node(LAB, 1, current_user=ADMIN)

    assert response["code"] == 404, response
    assert _saved(settings)["nodes"]["1"]["name"] == "newcomer"


@pytest.mark.asyncio
async def test_delete_lab_is_not_undone_by_an_in_flight_writer(settings):
    _seed(settings)
    lab_row = SimpleNamespace(filename=LAB)

    with _slow_locked_writer(_add_node_9):
        await LabService(_FakeDb()).delete_lab(lab_row)

    assert not (settings.LABS_DIR / LAB).exists()


class _RecordingDeleteDb(_FakeDb):
    def __init__(self):
        self.deleted = []
        self.commits = 0

    async def delete(self, lab):
        self.deleted.append(lab)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_request", [False, True])
async def test_delete_cleanup_keeps_loop_responsive_and_holds_lock(
    settings, monkeypatch, cancel_request
):
    _seed(settings)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()

    def cleanup(_data, _filename):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=3)
        finished.set()

    monkeypatch.setattr("app.services.lab_service.assert_no_external_members_for_scope", lambda _: None)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", cleanup)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_node_runtime", lambda *_: None)

    db = _RecordingDeleteDb()
    row = SimpleNamespace(filename=LAB)
    delete = asyncio.create_task(LabService(db).delete_lab(row))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not finished.is_set(), "The event loop could not run until cleanup finished"
        if cancel_request:
            delete.cancel()
            await asyncio.sleep(0)
        assert not delete.done()
        with pytest.raises(LabLockTimeout):
            with lab_lock(LAB, settings.LABS_DIR, timeout_s=0):
                pytest.fail("The lab lock was released while cleanup was in flight")
        with pytest.raises(LabLockTimeout):
            with nat_cloud_ipam_lock(timeout_s=0):
                pytest.fail("A shared-network join could proceed during cleanup")
        assert (settings.LABS_DIR / LAB).is_file()
        assert not db.deleted
    finally:
        release.set()
        cancelled = await asyncio.wait_for(delete, timeout=5)

    assert cancelled is cancel_request
    assert db.deleted == [row]
    assert db.commits == 1
    assert not (settings.LABS_DIR / LAB).exists()


@pytest.mark.asyncio
async def test_delete_cleanup_failure_preserves_state(settings, monkeypatch):
    _seed(settings)

    def fail(_data, _filename):
        raise OSError("Cleanup failed")

    monkeypatch.setattr("app.services.lab_service.assert_no_external_members_for_scope", lambda _: None)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", lambda *_: None)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_node_runtime", fail)
    db = _RecordingDeleteDb()
    with pytest.raises(OSError, match="Cleanup failed"):
        await LabService(db).delete_lab(SimpleNamespace(filename=LAB))

    assert (settings.LABS_DIR / LAB).is_file()
    assert not db.deleted
    assert db.commits == 0


@pytest.mark.asyncio
async def test_lab_delete_ipam_lock_failure_leaves_file_and_row(settings, monkeypatch):
    _seed(settings)

    def unavailable(_settings):
        raise OSError("IPAM lock unavailable")

    monkeypatch.setattr("app.services.lab_service.nat_cloud_ipam_lock_async", unavailable)
    db = _RecordingDeleteDb()
    with pytest.raises(OSError, match="IPAM lock unavailable"):
        await LabService(db).delete_lab(SimpleNamespace(filename=LAB))

    assert (settings.LABS_DIR / LAB).is_file()
    assert not db.deleted
    assert db.commits == 0


@pytest.mark.asyncio
async def test_cancelled_lab_delete_finishes_commit_and_audit(settings, monkeypatch):
    _seed(settings)
    committing = asyncio.Event()
    allow_commit = asyncio.Event()
    row = SimpleNamespace(filename=LAB, owner=ADMIN.username, path=f"/{LAB}", name="Test lab")
    audits = []

    class SuspendingDb(_RecordingDeleteDb):
        async def commit(self):
            committing.set()
            await allow_commit.wait()
            await super().commit()

    async def get_lab(_self, _filename):
        return row

    async def audit(event, **_details):
        audits.append(event)

    monkeypatch.setattr(LabService, "get_lab_by_filename", get_lab)
    monkeypatch.setattr(labs.audit_service, "record", audit)
    monkeypatch.setattr("app.services.lab_service.assert_no_external_members_for_scope", lambda _: None)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", lambda *_: None)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_node_runtime", lambda *_: None)

    db = SuspendingDb()
    delete = asyncio.create_task(labs.delete_lab(LAB, current_user=ADMIN, db=db))
    try:
        await asyncio.wait_for(committing.wait(), timeout=5)
        delete.cancel()
        await asyncio.sleep(0)
        delete.cancel()
        await asyncio.sleep(0)
        assert not delete.done()
        assert not audits
    finally:
        allow_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(delete, timeout=5)

    assert db.deleted == [row]
    assert db.commits == 1
    assert audits == ["lab.delete"]
    assert not (settings.LABS_DIR / LAB).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rename", "delete"])
async def test_folder_mutation_waits_for_nested_lab_writer(settings, monkeypatch, operation):
    _seed(settings)
    source_name = f"group/nested/{LAB}"
    source = settings.LABS_DIR / source_name
    source.parent.mkdir(parents=True)
    (settings.LABS_DIR / LAB).rename(source)
    row = SimpleNamespace(filename=source_name, path=f"/{source_name}")
    db = _RecordingDeleteDb()
    started = asyncio.Event()

    async def list_labs(_service):
        return [row]

    async def no_audit(*_args, **_kwargs):
        pass

    monkeypatch.setattr(LabService, "list_labs", list_labs)
    monkeypatch.setattr(folders.audit_service, "record", no_audit)
    monkeypatch.setattr(folders, "assert_no_external_members_for_scope", lambda *_: None)

    async def mutate_folder():
        started.set()
        if operation == "rename":
            return await folders.rename_folder(
                "group", FolderRenameRequest(path="renamed"), current_user=ADMIN, db=db,
            )
        return await folders.delete_folder("group", current_user=ADMIN, db=db)

    async with lab_lock_async(source_name, settings.LABS_DIR):
        pending = asyncio.create_task(mutate_folder())
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.sleep(0)
        if pending.done():
            await pending
            pytest.fail("Folder changed before the nested lab writer released its lock")
        assert source.is_file()
        assert db.commits == 0
        state = LabService.read_lab_json_static(source_name)
        state["meta"]["name"] = "saved by writer"
        LabService.write_lab_json_static(source_name, state)

    result = await asyncio.wait_for(pending, timeout=5)
    assert result["code"] == 200
    assert not source.exists()
    assert db.commits == 1
    if operation == "rename":
        assert row.filename == f"renamed/nested/{LAB}"
        assert LabService.read_lab_json_static(row.filename)["meta"]["name"] == "saved by writer"
    else:
        assert db.deleted == [row]


@pytest.mark.asyncio
async def test_folder_rename_waits_for_new_lab_database_commit(settings, monkeypatch):
    committing = asyncio.Event()
    allow_commit = asyncio.Event()
    rename_started = asyncio.Event()
    visible_labs = []

    class SuspendedCreateDb(_FakeDb):
        def add(self, row):
            self.row = row

        async def commit(self):
            committing.set()
            await allow_commit.wait()
            visible_labs.append(self.row)

    async def list_labs(_service):
        return list(visible_labs)

    async def no_audit(*_args, **_kwargs):
        pass

    monkeypatch.setattr(LabService, "list_labs", list_labs)
    monkeypatch.setattr(folders.audit_service, "record", no_audit)

    async def rename():
        rename_started.set()
        return await folders.rename_folder(
            "group", FolderRenameRequest(path="renamed"), current_user=ADMIN, db=_FakeDb(),
        )

    create_task = asyncio.create_task(
        LabService(SuspendedCreateDb()).create_lab(owner=ADMIN.username, name="child", path="group/nested")
    )
    rename_task = None
    try:
        await asyncio.wait_for(committing.wait(), timeout=3)
        rename_task = asyncio.create_task(rename())
        await asyncio.wait_for(rename_started.wait(), timeout=3)
        await asyncio.sleep(0)
        assert (settings.LABS_DIR / "group" / "nested" / "child.json").is_file()
        assert not (settings.LABS_DIR / "renamed").exists()
    finally:
        allow_commit.set()
        row = await asyncio.wait_for(create_task, timeout=5)
        if rename_task is not None:
            result = await asyncio.wait_for(rename_task, timeout=5)

    assert result["code"] == 200
    assert row.filename == "renamed/nested/child.json"
    assert (settings.LABS_DIR / row.filename).is_file()


@pytest.mark.asyncio
async def test_heartbeat_status_write_waits_off_the_loop_and_keeps_an_edit(settings, monkeypatch):
    _seed(settings, nodes={"1": {**_docker_node(1, "n1"), "status": 2}})
    NodeRuntimeService.reset_registry()
    with NodeRuntimeService._lock:
        NodeRuntimeService._registry["race-lab:1"] = {
            "lab_id": "race-lab", "node_id": 1, "kind": "docker"
        }
        NodeRuntimeService._loaded = True
    monkeypatch.setattr(
        NodeRuntimeService, "_check_alive_sync", staticmethod(lambda *_args: False)
    )
    try:
        with _slow_locked_writer(_add_network_9) as writer:
            _, ticks = await _ticks_while(NodeRuntimeService._run_heartbeat_cycle())
    finally:
        NodeRuntimeService.reset_registry()

    saved = _saved(settings)
    assert saved["nodes"]["1"]["status"] == 0
    assert "9" in saved["networks"]
    # Bookkeeping: the writer's revision stands.
    assert saved["meta"]["revision"] == writer["revision"]
    assert ticks >= 10


@pytest.mark.asyncio
async def test_move_lab_waits_off_the_loop_and_moves_the_latest_file(settings, monkeypatch):
    _seed(settings)

    class NoRowLabService(LabService):
        async def get_lab_by_filename(self, _filename):
            return None

    monkeypatch.setattr("app.routers.labs.LabService", NoRowLabService)

    with _slow_locked_writer(_add_node_9):
        response, ticks = await _ticks_while(
            labs.move_lab(
                LAB, LabMoveRequest(target_folder="/Shared"), current_user=ADMIN, db=_FakeDb()
            )
        )

    assert response["code"] == 200, response
    assert not (settings.LABS_DIR / LAB).exists()
    moved = json.loads((settings.LABS_DIR / "Shared" / LAB).read_text())
    assert "9" in moved["nodes"]
    assert ticks >= 10
