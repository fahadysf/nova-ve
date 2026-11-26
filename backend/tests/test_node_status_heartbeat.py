# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for US-104 — node status heartbeat reconciliation."""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.node_runtime_service import NodeRuntimeService, _TRANSITION_SUPPRESS_S


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


@pytest.fixture()
def tmp_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        TMP_DIR=tmp_dir,
        DOCKER_HOST="unix:///var/run/docker.sock",
    )


def _write_lab(labs_dir: Path, lab_id: str, nodes: dict) -> str:
    """Write a minimal v2 lab.json; return relative filename."""
    data = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": "test-lab"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes,
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }
    filename = f"{lab_id}.json"
    (labs_dir / filename).write_text(json.dumps(data))
    return filename


def _register_runtime(settings, lab_id: str, node_id: int, kind: str, **extra) -> dict:
    """Inject a runtime record into the registry and write a state file."""
    runtime_dir = settings.TMP_DIR / "node-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime = {
        "lab_id": lab_id,
        "node_id": node_id,
        "kind": kind,
        **extra,
    }
    key = f"{lab_id}:{node_id}"
    with NodeRuntimeService._lock:
        NodeRuntimeService._registry[key] = runtime
        NodeRuntimeService._loaded = True
    state_path = runtime_dir / f"{lab_id}-{node_id}.json"
    state_path.write_text(json.dumps(runtime))
    return runtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_settings(monkeypatch, tmp_settings):
    monkeypatch.setattr(
        "app.services.node_runtime_service.get_settings", lambda: tmp_settings
    )
    monkeypatch.setattr(
        "app.services.lab_service.get_settings", lambda: tmp_settings
    )


# ---------------------------------------------------------------------------
# Test: Docker node — container stopped externally → status flips 2→0 + WS publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docker_node_stopped_externally_triggers_reconcile(monkeypatch, tmp_settings):
    """When a Docker container is found not-running, heartbeat writes status=0 to lab.json
    and publishes a node_status_reconciled WS event."""
    lab_id = "lab-docker-test"
    node_id = 1
    container_name = f"nova-ve-{lab_id.replace('-', '')[:12]}-{node_id}"

    # Write lab.json with status=2 (running)
    nodes = {str(node_id): {"id": node_id, "name": "alpine", "type": "docker", "status": 2}}
    _write_lab(tmp_settings.LABS_DIR, lab_id, nodes)

    # Register a running runtime
    _register_runtime(tmp_settings, lab_id, node_id, "docker", container_name=container_name)

    _patch_settings(monkeypatch, tmp_settings)

    # Mock: container is NOT running (externally stopped)
    def fake_check_alive(runtime, kind, settings):
        return False  # container stopped

    monkeypatch.setattr(NodeRuntimeService, "_check_alive_sync", staticmethod(fake_check_alive))

    # Capture WS publishes
    published = []

    async def fake_publish(lab_id_arg, event_type, payload, rev="", *, guard, identity):
        async with guard as valid:
            if valid:
                published.append({"lab_id": lab_id_arg, "type": event_type, "payload": payload})

    from app.services import ws_hub as ws_hub_mod
    monkeypatch.setattr(ws_hub_mod.ws_hub, "publish", fake_publish)

    await NodeRuntimeService._run_heartbeat_cycle()

    # lab.json should now have status=0
    updated = json.loads((tmp_settings.LABS_DIR / f"{lab_id}.json").read_text())
    assert updated["nodes"][str(node_id)]["status"] == 0

    # WS event published
    assert len(published) == 1
    assert published[0]["lab_id"] == f"{lab_id}.json"
    assert published[0]["type"] == "node_status_reconciled"
    assert published[0]["payload"]["node_id"] == node_id
    assert published[0]["payload"]["status"] == 0

    # Registry entry cleaned up for dead node
    assert f"{lab_id}:{node_id}" not in NodeRuntimeService._registry


# ---------------------------------------------------------------------------
# Test: Docker node — container running, status already correct → no write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_write_when_status_matches(monkeypatch, tmp_settings):
    """Heartbeat does nothing when lab.json status already matches live state."""
    lab_id = "lab-nodiff"
    node_id = 2

    nodes = {str(node_id): {"id": node_id, "name": "router", "type": "docker", "status": 2}}
    _write_lab(tmp_settings.LABS_DIR, lab_id, nodes)
    _register_runtime(tmp_settings, lab_id, node_id, "docker", container_name="c1")

    _patch_settings(monkeypatch, tmp_settings)

    def fake_check_alive(runtime, kind, settings):
        return True  # still running

    monkeypatch.setattr(NodeRuntimeService, "_check_alive_sync", staticmethod(fake_check_alive))

    published = []

    async def fake_publish(*args, **kwargs):
        published.append(args)

    from app.services import ws_hub as ws_hub_mod
    monkeypatch.setattr(ws_hub_mod.ws_hub, "publish", fake_publish)

    original_mtime = (tmp_settings.LABS_DIR / f"{lab_id}.json").stat().st_mtime

    await NodeRuntimeService._run_heartbeat_cycle()

    # File not touched
    assert (tmp_settings.LABS_DIR / f"{lab_id}.json").stat().st_mtime == original_mtime
    assert published == []


# ---------------------------------------------------------------------------
# Test #373: dead workload with no status to flip → the record is still removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"id": 1, "name": "alpine", "type": "docker", "status": 0}, id="stored-status-already-stopped"),
        pytest.param(None, id="node-gone-from-the-lab"),
    ],
)
@pytest.mark.asyncio
async def test_dead_node_is_removed_even_with_no_status_to_flip(monkeypatch, tmp_settings, stored):
    """The backend's start path never writes status=2 to lab.json, so a node
    started through the API alone reaches the heartbeat with the stored status
    already 0 — or no longer in the lab at all. Its record must not outlive
    its workload."""
    lab_id = "lab-dead-noflip"
    node_id = 1
    nodes = (
        {str(node_id): stored}
        if stored
        else {"2": {"id": 2, "name": "other", "type": "docker", "status": 2}}
    )
    filename = _write_lab(tmp_settings.LABS_DIR, lab_id, nodes)
    _register_runtime(tmp_settings, lab_id, node_id, "docker", container_name="c1")
    state_path = tmp_settings.TMP_DIR / "node-runtime" / f"{lab_id}-{node_id}.json"

    _patch_settings(monkeypatch, tmp_settings)
    monkeypatch.setattr(
        NodeRuntimeService, "_check_alive_sync", staticmethod(lambda runtime, kind, settings: False)
    )
    published = []

    async def fake_publish(*args, **kwargs):
        published.append(args)

    from app.services import ws_hub as ws_hub_mod
    monkeypatch.setattr(ws_hub_mod.ws_hub, "publish", fake_publish)

    lab_path = tmp_settings.LABS_DIR / filename
    original = lab_path.read_text()

    await NodeRuntimeService._run_heartbeat_cycle()

    # The workload is gone, so its record is gone with it.
    assert f"{lab_id}:{node_id}" not in NodeRuntimeService._registry
    assert not state_path.exists()
    # Nothing changed in the lab, so nothing was written or announced.
    assert lab_path.read_text() == original
    assert published == []


@pytest.mark.asyncio
async def test_docker_heartbeat_observes_saved_id_and_retains_stopped_container(
    monkeypatch, tmp_settings,
):
    lab_id = "lab-container-rename"
    container_id = "a" * 64
    _write_lab(tmp_settings.LABS_DIR, lab_id, {"1": {"id": 1, "type": "docker", "status": 2}})
    _register_runtime(
        tmp_settings, lab_id, 1, "docker",
        container_name="old-name", container_id=container_id,
    )
    _patch_settings(monkeypatch, tmp_settings)
    running = True
    calls = []

    def docker_fixture(args, **kwargs):
        assert args[0] == "docker-fixture"
        calls.append(args[-1])
        if args[-1] != container_id:
            return subprocess.CompletedProcess(args, 1, "", "No such container")
        return subprocess.CompletedProcess(args, 0, str(running).lower(), "")

    monkeypatch.setattr("app.services.node_runtime_service.shutil.which", lambda *_: "docker-fixture")
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", docker_fixture)
    service = NodeRuntimeService()

    await service._run_heartbeat_cycle()
    assert service.running_nodes_by_lab() == {lab_id: 1}

    running = False
    await service._run_heartbeat_cycle()
    assert service.running_nodes_by_lab() == {}
    assert json.loads(service._state_path(lab_id, 1).read_text())["container_id"] == container_id
    assert calls == [container_id, container_id]


# ---------------------------------------------------------------------------
# Test: QEMU node — process alive, status=0 in lab → flips to 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qemu_node_started_externally_triggers_reconcile(monkeypatch, tmp_settings):
    """When QEMU pid exists but lab.json has status=0, heartbeat flips to status=2."""
    lab_id = "lab-qemu-test"
    node_id = 3
    pid = 99999

    nodes = {str(node_id): {"id": node_id, "name": "vyos", "type": "qemu", "status": 0}}
    _write_lab(tmp_settings.LABS_DIR, lab_id, nodes)
    _register_runtime(
        tmp_settings, lab_id, node_id, "qemu",
        pid=pid, pid_create_time=12345.0,
    )

    _patch_settings(monkeypatch, tmp_settings)

    def fake_check_alive(runtime, kind, settings):
        return True  # process is running

    monkeypatch.setattr(NodeRuntimeService, "_check_alive_sync", staticmethod(fake_check_alive))

    published = []

    async def fake_publish(lab_id_arg, event_type, payload, rev="", **kwargs):
        published.append({"type": event_type, "payload": payload})

    from app.services import ws_hub as ws_hub_mod
    monkeypatch.setattr(ws_hub_mod.ws_hub, "publish", fake_publish)

    await NodeRuntimeService._run_heartbeat_cycle()

    updated = json.loads((tmp_settings.LABS_DIR / f"{lab_id}.json").read_text())
    assert updated["nodes"][str(node_id)]["status"] == 2

    assert len(published) == 1
    assert published[0]["type"] == "node_status_reconciled"
    assert published[0]["payload"]["status"] == 2


# ---------------------------------------------------------------------------
# Test: suppression window — recent start/stop is not reconciled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppressed_within_transition_window(monkeypatch, tmp_settings):
    """Node within 30s transition window is skipped by heartbeat."""
    lab_id = "lab-suppress"
    node_id = 4

    nodes = {str(node_id): {"id": node_id, "name": "box", "type": "docker", "status": 2}}
    _write_lab(tmp_settings.LABS_DIR, lab_id, nodes)
    _register_runtime(tmp_settings, lab_id, node_id, "docker", container_name="c2")

    # Record a transition just now → suppressed
    NodeRuntimeService._record_transition(lab_id, node_id)

    _patch_settings(monkeypatch, tmp_settings)

    def fake_check_alive(runtime, kind, settings):
        return False  # externally stopped — would normally trigger reconcile

    monkeypatch.setattr(NodeRuntimeService, "_check_alive_sync", staticmethod(fake_check_alive))

    published = []

    async def fake_publish(*args, **kwargs):
        published.append(args)

    from app.services import ws_hub as ws_hub_mod
    monkeypatch.setattr(ws_hub_mod.ws_hub, "publish", fake_publish)

    await NodeRuntimeService._run_heartbeat_cycle()

    # status should NOT have changed — suppression window active
    lab_data = json.loads((tmp_settings.LABS_DIR / f"{lab_id}.json").read_text())
    assert lab_data["nodes"][str(node_id)]["status"] == 2
    assert published == []


# ---------------------------------------------------------------------------
# Test: suppression window expires after _TRANSITION_SUPPRESS_S
# ---------------------------------------------------------------------------

def test_suppression_expires():
    """_is_suppressed returns False once the window has elapsed."""
    lab_id = "lab-exp"
    node_id = 5
    # Inject a timestamp older than the suppress window
    NodeRuntimeService._transition_timestamps[(lab_id, node_id)] = (
        time.monotonic() - _TRANSITION_SUPPRESS_S - 1
    )
    assert NodeRuntimeService._is_suppressed(lab_id, node_id) is False


# ---------------------------------------------------------------------------
# Test: empty registry → cycle is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_registry_no_op(monkeypatch, tmp_settings):
    """Heartbeat cycle with no registry entries does nothing."""
    _patch_settings(monkeypatch, tmp_settings)
    check_called = []

    monkeypatch.setattr(
        NodeRuntimeService,
        "_check_alive_sync",
        staticmethod(lambda *a, **kw: check_called.append(1) or True),
    )

    await NodeRuntimeService._run_heartbeat_cycle()
    assert check_called == []
