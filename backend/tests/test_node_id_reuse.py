# SPDX-License-Identifier: Apache-2.0

"""#332: ids are never reused, and lab delete reads, checks, cleans up and
unlinks under one lock.

A lab remembers the highest id of each kind it has handed out, so a deleted
object never frees its id for the next one. A lab can therefore run out of
ids, which it could not when ids were recycled, and nothing that is rolled
back may give one back.
"""

from __future__ import annotations

import copy
import json
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import psutil

from app.routers import labs
from app.routers import links as link_routes
from app.schemas.network import NetworkCreate
from app.schemas.node import NodeBatchCreate, NodeCreate
from app.services import host_net
from app.services.cloud_inventory_service import SharedCloudReferenceError
from app.services.lab_lock import LabLockTimeout, lab_lock
from app.services.lab_service import (
    LabIdExhaustedError,
    LabService,
    allocate_link_id,
    allocate_network_id,
    allocate_node_id,
    locked_lab,
    preview_network_id,
)
from app.services.link_service import LinkService
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.runtime_mutex import runtime_mutex

LAB = "reuse.json"
LAB_ID = "reuse-lab"
ADMIN = SimpleNamespace(username="admin", role="admin", html5=True, folder="/")

_REAL_STOP_NODE = NodeRuntimeService.stop_node


@pytest.fixture()
def instance_id(tmp_path, monkeypatch):
    """Let the host-layer name helpers run: they need a per-deployment id."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "instance_id").write_text("test-instance")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance))


@pytest.fixture()
def settings(tmp_path, monkeypatch, instance_id):
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
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: s)

    async def no_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", no_publish)
    monkeypatch.setattr(NodeRuntimeService, "wipe_node", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(NodeRuntimeService, "stop_node", lambda *_args, **_kwargs: None)
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


@pytest.fixture()
def empty_runtime_registry():
    """The runtime registry is process-wide: start and end with nothing in it."""
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


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


def _write_lab(settings, filename, lab_id, *, nodes=None, networks=None, links=None, meta=None) -> None:
    (settings.LABS_DIR / filename).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": meta if meta is not None else {"name": "reuse", "revision": 0},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": nodes or {},
                "networks": networks or {},
                "links": links or [],
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )


def _seed(settings, **lab) -> None:
    _write_lab(settings, LAB, LAB_ID, **lab)


def _lab_file(settings):
    return settings.LABS_DIR / LAB


def _saved(settings) -> dict:
    return json.loads(_lab_file(settings).read_text())


def _network(net_id: int, **extra) -> dict:
    return {
        "id": net_id,
        "name": f"net{net_id}",
        "type": "linux_bridge",
        "visibility": True,
        "implicit": False,
        "config": {},
        **extra,
    }


def _row() -> SimpleNamespace:
    return SimpleNamespace(filename=LAB)


def _node_request(name: str) -> NodeCreate:
    return NodeCreate(
        name=name, type="docker", template="docker", image="nova-ve/alpine-telnet:latest"
    )


async def _create(settings, name: str) -> dict:
    response = await labs.create_node(LAB, _node_request(name), current_user=ADMIN)
    assert response["code"] == 200, response
    return response["data"]


@pytest.mark.asyncio
async def test_node_id_is_not_reused_after_deleting_the_highest(settings, docker_template):
    _seed(settings, nodes={"1": _docker_node(1, "a"), "2": _docker_node(2, "b")})

    created = await _create(settings, "third")
    assert created["id"] == 3

    deleted = await labs.delete_node(LAB, 3, current_user=ADMIN)
    assert deleted["code"] == 200, deleted

    recreated = await _create(settings, "fourth")
    assert recreated["id"] == 4, "deleted node id 3 was reused"
    assert _saved(settings)["meta"]["last_node_id"] == 4


@pytest.mark.asyncio
async def test_existing_lab_without_high_water_mark_starts_from_current_max(settings, docker_template):
    # Labs written before #332 have no meta.last_node_id: allocation must
    # start above the current maximum and then keep moving up.
    _seed(settings, nodes={"2": _docker_node(2, "a"), "5": _docker_node(5, "b")})

    created = await _create(settings, "sixth")
    assert created["id"] == 6

    deleted = await labs.delete_node(LAB, 6, current_user=ADMIN)
    assert deleted["code"] == 200, deleted

    recreated = await _create(settings, "seventh")
    assert recreated["id"] == 7


class _FakeDb:
    def __init__(self) -> None:
        self.deleted: list = []

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None

    async def delete(self, obj):
        self.deleted.append(obj)


@contextmanager
def _slow_locked_writer(mutate):
    holding = threading.Event()

    def run() -> None:
        with locked_lab(LAB) as lab:
            holding.set()
            time.sleep(0.15)
            mutate(lab.data)
            lab.save()

    thread = threading.Thread(target=run)
    thread.start()
    assert holding.wait(2), "writer never took the lock"
    try:
        yield
    finally:
        thread.join(5)


@pytest.mark.asyncio
async def test_delete_lab_cleans_up_state_added_while_it_waited(settings, monkeypatch):
    """A network/node created while delete waited for the lock must be cleaned."""
    _seed(settings, nodes={"1": _docker_node(1, "a")})
    seen: dict = {}

    def fake_network_cleanup(data, filename):
        seen["networks"] = sorted((data.get("networks") or {}).keys())

    def fake_node_cleanup(data, filename):
        seen["nodes"] = sorted((data.get("nodes") or {}).keys())

    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", fake_network_cleanup)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_node_runtime", fake_node_cleanup)

    def add_meanwhile(data: dict) -> None:
        data.setdefault("nodes", {})["9"] = _docker_node(9, "added-meanwhile")
        data.setdefault("networks", {})["9"] = {"id": 9, "name": "added", "type": "linux_bridge"}

    with _slow_locked_writer(add_meanwhile):
        await LabService(_FakeDb()).delete_lab(SimpleNamespace(filename=LAB))

    assert seen["nodes"] == ["1", "9"], seen
    assert seen["networks"] == ["9"], seen
    assert not (settings.LABS_DIR / LAB).exists()


@pytest.mark.asyncio
async def test_delete_lab_holds_the_lab_lock_while_cleanup_runs(settings, monkeypatch):
    """Cleanup must not race a writer: nothing can take the lab until it and
    the unlink are done."""
    _seed(settings, networks={"1": _network(1)})
    outcome: list[str] = []

    def cleanup(data, filename):
        try:
            with lab_lock(LAB, settings.LABS_DIR, timeout_s=0.2):
                outcome.append("lock was free")
        except LabLockTimeout:
            outcome.append("lock was held")

    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", cleanup)

    await LabService(_FakeDb()).delete_lab(_row())

    assert outcome == ["lock was held"]
    assert not _lab_file(settings).exists()


@pytest.mark.asyncio
async def test_contended_lab_delete_preserves_state_and_can_retry(settings, monkeypatch, empty_runtime_registry):
    _seed(settings, nodes={"1": _docker_node(1, "first"), "2": _docker_node(2, "busy")})
    work_root = settings.TMP_DIR / LAB_ID
    runtime_dir = settings.TMP_DIR / "node-runtime"
    runtime_dir.mkdir()
    for node_id in (1, 2):
        disk = work_root / str(node_id) / "disk.img"
        disk.parent.mkdir(parents=True)
        disk.write_text(f"disk {node_id}")
        (runtime_dir / f"{LAB_ID}-{node_id}.json").write_text(json.dumps({
            "lab_id": LAB_ID, "node_id": node_id, "kind": "docker", "container_name": f"fixture-{node_id}",
        }))
    lab_file = _lab_file(settings)
    original = lab_file.read_bytes()
    stopped = []
    networks = []

    def stop_fixture(service, _data, node_id, **_kwargs):
        with runtime_mutex.acquire_node_sync(LAB_ID, node_id, timeout=0):
            stopped.append(node_id)
            service._delete_runtime(LAB_ID, node_id)

    monkeypatch.setattr(NodeRuntimeService, "stop_node", stop_fixture)
    monkeypatch.setattr(NodeRuntimeService, "wipe_node", stop_fixture)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", lambda *_: networks.append(True))

    held = threading.Event()
    release = threading.Event()

    def reconcile_fixture():
        with runtime_mutex.acquire_node_sync(LAB_ID, 2):
            held.set()
            release.wait(timeout=8)

    worker = threading.Thread(target=reconcile_fixture)
    worker.start()
    db = _FakeDb()
    try:
        assert held.wait(timeout=2)
        with pytest.raises(LabLockTimeout):
            await LabService(db).delete_lab(_row())
        assert lab_file.read_bytes() == original
        assert db.deleted == [] and stopped == [] and networks == []
        for node_id in (1, 2):
            assert (work_root / str(node_id) / "disk.img").read_text() == f"disk {node_id}"
            assert (runtime_dir / f"{LAB_ID}-{node_id}.json").is_file()
    finally:
        release.set()
        worker.join(timeout=3)

    await LabService(db).delete_lab(_row())
    assert stopped == [1, 2] and networks == [True]
    assert len(db.deleted) == 1
    assert not lab_file.exists() and not work_root.exists()
    assert not list(runtime_dir.glob("*.json"))


@pytest.mark.asyncio
async def test_lab_delete_keeps_disks_if_any_node_stop_fails(settings, monkeypatch, empty_runtime_registry):
    _seed(settings, nodes={"1": _docker_node(1, "first"), "2": _docker_node(2, "second")})
    work_root = settings.TMP_DIR / LAB_ID
    for node_id in (1, 2):
        disk = work_root / str(node_id) / "disk.img"
        disk.parent.mkdir(parents=True)
        disk.write_text("disk fixture")

    stopped = []
    def stop_fixture(_service, _data, node_id, **_kwargs):
        if node_id == 2:
            raise RuntimeError("fixture could not stop node")
        stopped.append(node_id)

    monkeypatch.setattr(NodeRuntimeService, "stop_node", stop_fixture)
    monkeypatch.setattr(NodeRuntimeService, "wipe_node", stop_fixture)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", lambda *_: None)
    db = _FakeDb()

    with pytest.raises(RuntimeError, match="fixture could not stop node"):
        await LabService(db).delete_lab(_row())

    assert stopped == [1]
    assert _lab_file(settings).is_file() and db.deleted == []
    assert all((work_root / str(node_id) / "disk.img").is_file() for node_id in (1, 2))


@pytest.mark.asyncio
async def test_lab_delete_retries_a_failed_docker_removal(settings, monkeypatch, empty_runtime_registry):
    _seed(settings, nodes={"1": _docker_node(1, "runtime")})
    disk = settings.TMP_DIR / LAB_ID / "1" / "disk.img"
    disk.parent.mkdir(parents=True)
    disk.write_text("disk fixture")
    service = NodeRuntimeService()
    container_id = "a" * 64
    runtime = {
        "lab_id": LAB_ID, "node_id": 1, "kind": "docker",
        "container_name": "container-fixture", "container_id": container_id,
    }
    service._persist_runtime(runtime)
    record = service._state_path(LAB_ID, 1)
    stale_runtime = service._runtime_record(LAB_ID, 1, include_stopped=True)
    remove_fails = True

    def docker_fixture(args, **kwargs):
        assert args[0] == "docker-fixture" and args[-1] == container_id
        if args[3] == "rm" and remove_fails:
            return subprocess.CompletedProcess(args, 1, "", "daemon unavailable")
        assert args[3] in {"stop", "rm"}
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(NodeRuntimeService, "stop_node", _REAL_STOP_NODE)
    monkeypatch.setattr(NodeRuntimeService, "_resolve_binary", lambda *_: "docker-fixture")
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", docker_fixture)
    monkeypatch.setattr(host_net, "sweep_node_host_ifaces", lambda *_: None)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", lambda *_: None)
    db = _FakeDb()

    with pytest.raises(NodeRuntimeError, match="Could not remove the Docker container"):
        await LabService(db).delete_lab(_row())

    assert disk.is_file() and record.is_file() and _lab_file(settings).is_file()
    assert not db.deleted
    assert service._runtime_record(LAB_ID, 1) is None
    service._discard_inactive_runtime(stale_runtime, settings)
    NodeRuntimeService.reset_registry()
    assert NodeRuntimeService()._runtime_record(LAB_ID, 1) is None
    assert record.is_file() and json.loads(record.read_text())["lab_cleanup_pending"]

    remove_fails = False
    await LabService(db).delete_lab(_row())

    assert not disk.exists() and not record.exists() and not _lab_file(settings).exists()
    assert len(db.deleted) == 1


@pytest.mark.asyncio
async def test_lab_delete_checks_process_exit_when_qemu_group_is_absent(
    settings, monkeypatch, empty_runtime_registry,
):
    _seed(settings, nodes={"1": {"id": 1, "name": "qemu fixture", "type": "qemu"}})
    disk = settings.TMP_DIR / LAB_ID / "1" / "disk.img"
    disk.parent.mkdir(parents=True)
    disk.write_text("disk fixture")
    service = NodeRuntimeService()
    runtime = {"lab_id": LAB_ID, "node_id": 1, "kind": "qemu", "pid": 123, "pid_create_time": 1000}
    service._persist_runtime(runtime)
    record = service._state_path(LAB_ID, 1)
    calls = []

    def absent_group(pid, sig):
        calls.append((pid, sig))
        raise ProcessLookupError

    def live_process(pid, expected):
        assert (pid, expected) == (123, 1000)

        def wait(timeout):
            raise psutil.TimeoutExpired(timeout)

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(NodeRuntimeService, "stop_node", _REAL_STOP_NODE)
    monkeypatch.setattr(NodeRuntimeService, "_cleanup_process", staticmethod(live_process))
    monkeypatch.setattr("app.services.node_runtime_service.os.killpg", absent_group)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", lambda *_: None)
    db = _FakeDb()

    with pytest.raises(NodeRuntimeError, match="Could not confirm"):
        await LabService(db).delete_lab(_row())

    assert calls == [(123, signal.SIGTERM)]
    assert disk.is_file() and record.is_file() and _lab_file(settings).is_file()
    assert not db.deleted


@pytest.mark.asyncio
async def test_lab_delete_can_retry_legacy_ubridge_after_primary_iol_exits(
    settings, monkeypatch, empty_runtime_registry,
):
    from app.services.runtime.iol import IolLauncher

    _seed(settings, nodes={"1": {"id": 1, "name": "iol fixture", "type": "iol"}})
    disk = settings.TMP_DIR / LAB_ID / "1" / "disk.img"
    disk.parent.mkdir(parents=True)
    disk.write_text("disk fixture")
    service = NodeRuntimeService()
    service._persist_runtime({
        "lab_id": LAB_ID, "node_id": 1, "kind": "iol",
        "pid": 123, "pid_create_time": 1000, "ubridge_pid": 124,
    })
    record = service._state_path(LAB_ID, 1)
    alive = {123: 1000, 124: 1200}
    calls = []

    def process_fixture(pid, expected):
        if pid not in alive:
            return None
        assert expected in {None, alive[pid]}
        start_time = alive[pid]

        def wait(timeout):
            raise psutil.TimeoutExpired(timeout)

        return SimpleNamespace(wait=wait, create_time=lambda: start_time)

    def stop_fixture(runtime):
        calls.append((runtime["pid"], runtime["ubridge_pid"]))
        if runtime["pid"] is not None:
            alive.pop(runtime["pid"])
        else:
            alive.pop(runtime["ubridge_pid"])

    monkeypatch.setattr(NodeRuntimeService, "stop_node", _REAL_STOP_NODE)
    monkeypatch.setattr(NodeRuntimeService, "_cleanup_process", staticmethod(process_fixture))
    monkeypatch.setattr(IolLauncher, "owns_live_process", lambda pid: pid == 124 and not calls)
    monkeypatch.setattr(IolLauncher, "stop_runtime", stop_fixture)
    monkeypatch.setattr(NodeRuntimeService, "_unregister_pid", lambda *_: None)
    monkeypatch.setattr(host_net, "sweep_node_host_ifaces", lambda *_: None)
    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", lambda *_: None)
    db = _FakeDb()

    with pytest.raises(NodeRuntimeError, match="Could not confirm"):
        await LabService(db).delete_lab(_row())

    assert disk.is_file() and not db.deleted and calls == [(123, 124)]
    assert json.loads(record.read_text())["ubridge_create_time"] == 1200
    NodeRuntimeService.reset_registry()
    assert NodeRuntimeService()._runtime_record(LAB_ID, 1) is None

    await LabService(db).delete_lab(_row())

    assert not disk.exists() and not record.exists() and not _lab_file(settings).exists()
    assert calls == [(123, 124), (None, 124)] and len(db.deleted) == 1


@pytest.mark.asyncio
async def test_delete_lab_stops_and_wipes_its_nodes(settings, monkeypatch, empty_runtime_registry):
    """The real node cleanup runs under the delete. Only the layer that would
    talk to Docker is stubbed: node 2 is running, node 1 is only on disk."""
    monkeypatch.setattr(NodeRuntimeService, "stop_node", _REAL_STOP_NODE)
    monkeypatch.setattr(NodeRuntimeService, "_resolve_binary", lambda *_: "docker-fixture")
    monkeypatch.setattr(NodeRuntimeService, "_docker_container_absent", lambda *_: True)
    stopped: list[str] = []
    monkeypatch.setattr(
        NodeRuntimeService,
        "_stop_docker_runtime",
        lambda self, runtime, **_: stopped.append(runtime["container_name"]),
    )
    _seed(settings, nodes={"1": _docker_node(1, "stopped"), "2": _docker_node(2, "running")})

    work_root = settings.TMP_DIR / LAB_ID
    for node_id in (1, 2):
        (work_root / str(node_id)).mkdir(parents=True)
        (work_root / str(node_id) / "hda.qcow2").write_text("disk")
    records = settings.TMP_DIR / "node-runtime"
    records.mkdir()
    # 9 is a node the lab no longer has: its record goes with the lab's.
    for node_id in (2, 9):
        (records / f"{LAB_ID}-{node_id}.json").write_text(
            json.dumps(
                {"lab_id": LAB_ID, "node_id": node_id, "kind": "docker", "container_name": f"c-{node_id}"}
            )
        )

    await LabService(_FakeDb()).delete_lab(_row())

    assert stopped == ["c-2", "c-9"]
    assert not work_root.exists()
    assert list(records.glob("*.json")) == []


# ---------------------------------------------------------------------------
# Lab delete: only a lab that cannot be read is deleted without its cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_lab_keeps_file_and_row_when_cleanup_fails(settings, monkeypatch):
    """Bridges and disks outlive a lab only while its file names them. Once
    the lab was read, a cleanup failure must surface instead of deleting
    anyway and leaving them behind with nothing to clean them up."""
    _seed(settings, networks={"1": _network(1)})
    db = _FakeDb()

    def unreachable_host(data, filename):
        raise OSError("bridge_del: host unreachable")

    monkeypatch.setattr("app.services.lab_service._cleanup_lab_network_runtime", unreachable_host)

    with pytest.raises(OSError, match="host unreachable"):
        await LabService(db).delete_lab(_row())

    assert _lab_file(settings).exists()
    assert db.deleted == []


@pytest.mark.asyncio
async def test_delete_lab_still_removes_a_lab_it_cannot_read(settings):
    """A failed read is the one case that still deletes without a cleanup."""
    _lab_file(settings).write_text("{not json")
    db = _FakeDb()

    await LabService(db).delete_lab(_row())

    assert not _lab_file(settings).exists()
    assert len(db.deleted) == 1


@pytest.mark.asyncio
async def test_delete_lab_judges_sharing_on_the_lab_as_it_is_under_the_lock(settings):
    """A network shared, and joined from another lab, while the delete waited
    for the lock. The delete must refuse rather than take the shared bridge
    away from the lab that joined it."""
    _seed(settings, networks={"1": _network(1)})
    db = _FakeDb()

    def shared_meanwhile(data: dict) -> None:
        data["networks"]["1"]["shared"] = True
        _write_lab(
            settings, "member.json", "lab-member", meta={"name": "member"},
            networks={"1": _network(1, config={"shared_network_id": f"shared-net:{LAB_ID}:1"})},
        )

    with _slow_locked_writer(shared_meanwhile):
        with pytest.raises(SharedCloudReferenceError) as blocked:
            await LabService(db).delete_lab(_row())

    assert "member" in blocked.value.message
    assert _lab_file(settings).exists()
    assert db.deleted == []


# ---------------------------------------------------------------------------
# An id the host layer cannot name is never handed out
# ---------------------------------------------------------------------------

_LAST_NODES = (
    f"This lab has used all {host_net.MAX_NODE_ID} node ids. Ids are never reused, "
    "so start a new lab to add more nodes."
)
_LAST_NETWORKS = (
    f"This lab has used all {host_net.MAX_NETWORK_ID} network ids. Ids are never reused, "
    "so start a new lab to add more networks."
)


def test_the_last_node_id_is_given_out_and_marked(instance_id):
    """999 still fits an interface name; 1000 is the first one that does not."""
    data = {"nodes": {"998": {"id": 998}}, "meta": {"name": "edge"}}

    assert allocate_node_id(data) == 999 == host_net.MAX_NODE_ID
    assert data["meta"]["last_node_id"] == 999
    assert host_net.mgmt_veth_host_name("some-lab", 999)
    with pytest.raises(AssertionError):
        host_net.mgmt_veth_host_name("some-lab", 1000)


def test_no_node_id_past_the_last_one_is_given_out():
    """Refused before anything is recorded, from a mark or from the nodes alone."""
    for meta in ({"last_node_id": 999}, {}):
        data = {"nodes": {"999": {"id": 999}}, "meta": dict(meta, name="edge")}

        with pytest.raises(LabIdExhaustedError) as refused:
            allocate_node_id(data)

        assert (refused.value.code, refused.value.message) == (409, _LAST_NODES)
        assert data == {"nodes": {"999": {"id": 999}}, "meta": dict(meta, name="edge")}


def test_the_last_network_id_is_given_out_marked_and_then_refused(instance_id):
    """99,999 still fits a bridge name; 100,000 is the first one that does not."""
    last = 99999
    data = {"networks": {}, "meta": {"name": "edge", "last_network_id": last - 1}}

    assert preview_network_id(data) == last == host_net.MAX_NETWORK_ID
    assert allocate_network_id(data) == last
    assert data["meta"]["last_network_id"] == last
    assert host_net.bridge_name("some-lab", last)
    with pytest.raises(AssertionError):
        host_net.bridge_name("some-lab", last + 1)

    for take in (allocate_network_id, preview_network_id):
        with pytest.raises(LabIdExhaustedError) as refused:
            take(data)
        assert (refused.value.code, refused.value.message) == (409, _LAST_NETWORKS)
    assert data["meta"] == {"name": "edge", "last_network_id": last}


def _refused(response) -> tuple[int, str]:
    """``(status, message)`` of the 409 a route answers past the last id."""
    return response.status_code, json.loads(response.body)["message"]


@pytest.mark.asyncio
async def test_node_create_refuses_past_the_last_id_and_leaves_the_lab_alone(settings, docker_template):
    _seed(settings, meta={"name": "reuse", "revision": 0, "last_node_id": 998})

    assert (await _create(settings, "last"))["id"] == 999
    assert _saved(settings)["meta"]["last_node_id"] == 999
    before = _lab_file(settings).read_bytes()

    refused = await labs.create_node(LAB, _node_request("one too many"), current_user=ADMIN)

    assert _refused(refused) == (409, _LAST_NODES)
    assert _lab_file(settings).read_bytes() == before


@pytest.mark.asyncio
async def test_node_batch_takes_every_id_it_needs_or_none(settings, docker_template):
    """No partial batch, and no id burned by a batch that cannot fit."""
    _seed(settings, meta={"name": "reuse", "revision": 0, "last_node_id": 997})
    before = _lab_file(settings).read_bytes()

    def batch(count):
        return NodeBatchCreate(
            name_prefix="n", count=count, type="docker", template="docker",
            image="nova-ve/alpine-telnet:latest",
        )

    refused = await labs.create_nodes_batch(LAB, batch(3), current_user=ADMIN)

    assert _refused(refused) == (409, _LAST_NODES)
    assert _lab_file(settings).read_bytes() == before

    fitted = await labs.create_nodes_batch(LAB, batch(2), current_user=ADMIN)
    assert [node["id"] for node in fitted["data"]["nodes"]] == [998, 999]
    assert _saved(settings)["meta"]["last_node_id"] == 999


@pytest.mark.asyncio
async def test_network_create_refuses_past_the_last_id_and_leaves_the_lab_alone(settings):
    _seed(settings, meta={"name": "reuse", "revision": 0, "last_network_id": 99998})

    created = await labs.create_network(LAB, NetworkCreate(name="last"), current_user=ADMIN)
    assert created["data"]["id"] == 99999
    assert _saved(settings)["meta"]["last_network_id"] == 99999
    before = _lab_file(settings).read_bytes()

    refused = await labs.create_network(LAB, NetworkCreate(name="one too many"), current_user=ADMIN)

    assert _refused(refused) == (409, _LAST_NETWORKS)
    assert _lab_file(settings).read_bytes() == before


@pytest.mark.asyncio
async def test_node_to_node_link_refuses_once_no_implicit_network_id_is_left(settings):
    """A node-to-node link brings its own network into being."""
    _seed(
        settings, nodes={"1": _docker_node(1, "a"), "2": _docker_node(2, "b")},
        meta={"name": "reuse", "revision": 0, "last_network_id": 99999},
    )
    before = _lab_file(settings).read_bytes()

    refused = await link_routes.create_link(
        LAB,
        {"from": {"node_id": 1, "interface_index": 0}, "to": {"node_id": 2, "interface_index": 0}},
        idempotency_key=None,
        current_user=ADMIN,
    )

    assert _refused(refused) == (409, _LAST_NETWORKS)
    assert _lab_file(settings).read_bytes() == before


# ---------------------------------------------------------------------------
# A rollback is not an edit: it gives no id back
# ---------------------------------------------------------------------------


def test_restore_keeps_the_ids_allocated_since_the_snapshot(settings):
    _seed(settings)

    with locked_lab(LAB) as lab:
        snapshot = copy.deepcopy(lab.data)
        assert allocate_node_id(lab.data) == 1
        lab.restore(snapshot)

    saved = _saved(settings)
    assert saved["nodes"] == {}
    assert saved["meta"]["last_node_id"] == 1
    assert allocate_node_id(saved) == 2


def test_restore_keeps_newer_in_memory_mark(settings):
    _seed(settings)
    with locked_lab(LAB) as lab:
        for _ in range(3):
            allocate_node_id(lab.data)
        lab.save()
        snapshot = copy.deepcopy(lab.data)
        assert allocate_node_id(lab.data) == 4
        lab.restore(snapshot)
    assert _saved(settings)["meta"]["last_node_id"] == 4


def test_restore_does_not_write_when_disk_read_fails(settings, monkeypatch):
    _seed(settings)
    with locked_lab(LAB) as lab:
        snapshot = copy.deepcopy(lab.data)
        before = _lab_file(settings).read_bytes()
        def fail_read(_path):
            raise OSError("read failed")
        monkeypatch.setattr(LabService, "read_lab_json_static", fail_read)
        with pytest.raises(OSError, match="read failed"):
            lab.restore(snapshot)
        assert _lab_file(settings).read_bytes() == before


def test_restore_keeps_ids_a_borrower_of_the_lock_allocated(settings):
    """Paired-template creation lends its lock to the link service, which
    re-reads the file and allocates in a copy of its own."""
    _seed(settings)

    with locked_lab(LAB) as lab:
        snapshot = copy.deepcopy(lab.data)
        borrowed = LabService.read_lab_json_static(LAB)
        allocate_network_id(borrowed)
        allocate_link_id(borrowed)
        LabService.write_lab_json_static(LAB, borrowed)
        lab.restore(snapshot)

    saved = _saved(settings)
    assert (saved["networks"], saved["links"]) == ({}, [])
    assert (saved["meta"]["last_network_id"], saved["meta"]["last_link_id"]) == (1, 1)
    assert (allocate_network_id(saved), allocate_link_id(saved)) == (2, "lnk_002")


@pytest.mark.asyncio
async def test_failed_hot_attach_rollback_keeps_the_allocated_link_id(settings, monkeypatch):
    _seed(settings, nodes={"1": _docker_node(1, "a")}, networks={"1": _network(1)})

    def refused(*_args, **_kwargs):
        raise RuntimeError("attach refused")

    monkeypatch.setattr(LinkService, "_hot_attach_running_endpoints", refused)

    with pytest.raises(RuntimeError, match="attach refused"):
        await LinkService().create_link(
            LAB, {"node_id": 1, "interface_index": 0}, {"network_id": 1}
        )

    saved = _saved(settings)
    assert saved["links"] == []
    assert saved["meta"]["last_link_id"] == 1
    assert allocate_link_id(saved) == "lnk_002"


# ---------------------------------------------------------------------------
# Links built from a legacy topology take ids like every other link
# ---------------------------------------------------------------------------


def _topology_entry(source: str, destination: str, network_id: int) -> dict:
    return {"source": source, "destination": destination, "network_id": network_id}


@pytest.mark.asyncio
async def test_topology_links_never_take_an_earlier_links_id(settings):
    """PUT /topology on a lab without links rebuilds them from the topology."""
    _seed(
        settings, nodes={"1": _docker_node(1, "a")}, networks={"1": _network(1)},
        meta={"name": "reuse", "revision": 0, "last_link_id": 1},
    )

    response = await labs.update_topology(
        LAB, SimpleNamespace(headers={}),
        {"topology": [_topology_entry("node1", "network1", 1)]},
        current_user=ADMIN,
    )

    assert response["code"] == 200, response
    saved = _saved(settings)
    assert [link["id"] for link in saved["links"]] == ["lnk_002"]
    assert saved["meta"]["last_link_id"] == 2


def test_topology_links_skip_and_record_the_ids_the_client_supplied(settings):
    _seed(settings, nodes={"1": _docker_node(1, "a")}, networks={"1": _network(1)})
    data = LabService.read_lab_json_static(LAB)
    data["topology"] = [
        dict(_topology_entry("node1", "network1", 1), id="lnk_007"),
        _topology_entry("node1", "network1", 1),
        # Not a link: it names no node, so it must not use up an id.
        _topology_entry("nowhere", "network1", 1),
        dict(_topology_entry("node1", "network1", 1), id="lnk_001"),
    ]

    LabService.write_lab_json_static(LAB, data)

    saved = _saved(settings)
    assert [link["id"] for link in saved["links"]] == ["lnk_007", "lnk_002", "lnk_001"]
    assert saved["meta"]["last_link_id"] == 7
    assert allocate_link_id(saved) == "lnk_008"


# ---------------------------------------------------------------------------
# The marks are the server's own bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_lab_without_a_db_row_keeps_the_marks_to_itself(settings, monkeypatch):
    _seed(
        settings,
        meta={"name": "reuse", "revision": 3, "last_node_id": 7, "last_network_id": 2, "last_link_id": 9},
    )

    async def no_row(self, filename):
        return None

    monkeypatch.setattr(LabService, "get_lab_by_filename", no_row)
    response = await labs.get_lab(LAB, current_user=ADMIN, db=None)

    # The revision stays: clients guard their writes with it.
    assert (response["data"]["name"], response["data"]["revision"]) == ("reuse", 3)
    assert not {"last_node_id", "last_network_id", "last_link_id"} & response["data"].keys()


def test_a_lab_without_links_gains_no_link_mark(settings):
    """Every save of a link-less lab runs the legacy conversion. With nothing
    to record it must leave the lab's meta alone."""
    _seed(settings)

    with locked_lab(LAB) as lab:
        lab.save()

    assert "last_link_id" not in _saved(settings)["meta"]
