import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace

import psutil
import pytest

from app.routers import labs
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.runtime.iol import IolError, IolLauncher


def _archive_payload(path: Path) -> bytes:
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        assert len(members) == 1
        return archive.extractfile(members[0]).read()


@pytest.fixture
def saved_runtime(monkeypatch, tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    runtime_dir = tmp_path / "runtime"
    labs_dir.mkdir()
    images_dir.mkdir()
    runtime_dir.mkdir()
    settings = SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=runtime_dir,
        DOCKER_HOST="unix:///unused",
    )
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: settings)

    image = tmp_path / "iol-fixture.bin"
    image.write_bytes(b"not an emulator")
    ubridge = tmp_path / "ubridge-fixture"
    ubridge.write_bytes(b"not a helper")
    lab_data = {
        "schema": 2,
        "id": "lab-saved-iol",
        "meta": {"name": "saved-iol"},
        "nodes": {
            "7": {
                "id": 7,
                "name": "iol-7",
                "type": "iol",
                "image": str(image),
                "console": "telnet",
                "ram": 1024,
                "ethernet": 4,
                "interfaces": [],
                "extras": {"nvram": 1024},
            }
        },
        "networks": {},
        "links": [],
    }
    work_dir = runtime_dir / lab_data["id"] / "7"
    work_dir.mkdir(parents=True)
    preferred_id = IolLauncher.application_id(lab_data["id"], 7, set())
    native_path = work_dir / f"nvram_{preferred_id:05d}"
    native_path.write_bytes(b"legacy-native-state")

    live_pids: set[int] = set()
    popen_calls = []

    class FakePopen:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None if self.pid in live_pids else 0

    class FakePsutilProcess:
        def __init__(self, pid):
            if pid not in live_pids:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def create_time(self):
            return 1000.0 + self.pid

        def is_running(self):
            return True

        def status(self):
            return psutil.STATUS_SLEEPING

        def wait(self, timeout=None):
            if self.pid in live_pids:
                raise psutil.TimeoutExpired(self.pid, timeout)
            return None

    def fake_popen(command, **kwargs):
        pid = 7000 + len(popen_calls)
        popen_calls.append((list(command), kwargs))
        live_pids.add(pid)
        return FakePopen(pid)

    class FakeUbridgeClient:
        def __init__(self, port):
            self.port = port

        def request(self, command):
            return None

        def close(self):
            return None

    class FakeConsoleBridge:
        def __init__(self, **kwargs):
            pass

        def start(self):
            return None

        def stop(self):
            return None

    def unexpected_signal(*args):
        raise AssertionError(f"unexpected process signal: {args}")

    monkeypatch.setattr("app.services.runtime.iol._UBRIDGE_BINARY", str(ubridge))
    monkeypatch.setattr("app.services.runtime.iol.subprocess.Popen", fake_popen)
    monkeypatch.setattr(IolLauncher, "_terminate_pid", staticmethod(live_pids.discard))
    monkeypatch.setattr("app.services.runtime.iol.os.killpg", unexpected_signal)
    monkeypatch.setattr("app.services.runtime.iol.os.kill", unexpected_signal)
    monkeypatch.setattr("app.services.node_runtime_service.os.killpg", unexpected_signal)
    monkeypatch.setattr("app.services.node_runtime_service.os.kill", unexpected_signal)
    monkeypatch.setattr("app.services.runtime.iol._UbridgeClient", FakeUbridgeClient)
    monkeypatch.setattr("app.services.runtime.iol.IolConsoleBridge", FakeConsoleBridge)
    monkeypatch.setattr(IolLauncher, "_free_tcp_port", staticmethod(lambda: 18000))
    monkeypatch.setattr(IolLauncher, "resolve_iourc", staticmethod(lambda image_path: None))
    monkeypatch.setattr("app.services.runtime.iol.os.openpty", lambda: (101, 102))
    monkeypatch.setattr("app.services.runtime.iol.os.close", lambda fd: None)
    monkeypatch.setattr("app.services.runtime.iol.time.sleep", lambda seconds: None)
    monkeypatch.setattr("psutil.Process", FakePsutilProcess)
    monkeypatch.setattr(IolLauncher, "_processes", {})
    monkeypatch.setattr(IolLauncher, "_bridges", {})
    monkeypatch.setattr("app.services.node_runtime_service.host_net.sandbox_mode", lambda: False)
    monkeypatch.setattr("app.services.node_runtime_service.host_net.try_link_del", lambda name: None)
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.sweep_node_host_ifaces",
        lambda lab_id, node_id: None,
    )

    NodeRuntimeService.reset_registry()
    service = NodeRuntimeService()
    yield SimpleNamespace(
        lab_data=lab_data,
        live_pids=live_pids,
        native_path=native_path,
        popen_calls=popen_calls,
        preferred_id=preferred_id,
        service=service,
        settings=settings,
        work_dir=work_dir,
    )
    NodeRuntimeService.reset_registry()


def test_iol_capture_uses_last_launch_and_saved_restarts_restore_explicit_snapshot(saved_runtime):
    fixture = saved_runtime
    ordinary = fixture.work_dir / "ordinary.txt"
    ordinary.write_bytes(b"preserve ordinary files")

    legacy_record = fixture.service.start_node(fixture.lab_data, 7)
    assert legacy_record["work_dir"] == str(fixture.work_dir)
    assert IolLauncher.read_source(fixture.work_dir).workspace == "."
    fixture.service.stop_node(fixture.lab_data, 7)

    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    snapshot = fixture.work_dir / "iol-snapshot.tar"
    assert _archive_payload(snapshot) == b"legacy-native-state"
    fixture.native_path.write_bytes(b"ordinary-native-after-capture")

    first_saved = fixture.service.start_node(fixture.lab_data, 7)
    first_work = Path(first_saved["work_dir"])
    assert first_work.parent == fixture.work_dir
    assert first_work != fixture.work_dir
    assert IolLauncher.read_source(fixture.work_dir).workspace == first_work.name
    (first_work / fixture.native_path.name).write_bytes(b"saved-run-state")
    fixture.service.stop_node(fixture.lab_data, 7)

    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Exported"
    fixture.native_path.write_bytes(b"root-must-not-be-captured")
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    assert _archive_payload(snapshot) == b"saved-run-state"
    assert fixture.native_path.read_bytes() == b"root-must-not-be-captured"

    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    second_saved = fixture.service.start_node(fixture.lab_data, 7)
    second_work = Path(second_saved["work_dir"])
    assert second_work != first_work
    assert not first_work.exists()
    assert (second_work / fixture.native_path.name).read_bytes() == b"saved-run-state"
    assert fixture.native_path.read_bytes() == b"root-must-not-be-captured"
    assert ordinary.read_bytes() == b"preserve ordinary files"
    fixture.service.stop_node(fixture.lab_data, 7)


def test_iol_legacy_capture_needs_no_marker_or_process_start(saved_runtime):
    fixture = saved_runtime

    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)

    assert _archive_payload(fixture.work_dir / "iol-snapshot.tar") == b"legacy-native-state"
    assert not (fixture.work_dir / ".iol-source.json").exists()
    assert fixture.popen_calls == []


def test_iol_invalid_numeric_settings_fail_before_launch_writes_or_popen(saved_runtime):
    fixture = saved_runtime
    fixture.lab_data["nodes"]["7"]["extras"]["serial_adapters"] = "not-a-number"

    with pytest.raises(NodeRuntimeError, match="numeric settings"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert fixture.popen_calls == []
    assert not (fixture.work_dir / "NETMAP").exists()
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    ) is None


def test_iol_capture_rejects_running_node_without_replacing_snapshot(saved_runtime):
    fixture = saved_runtime
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    snapshot = fixture.work_dir / "iol-snapshot.tar"
    previous = snapshot.read_bytes()
    fixture.service.start_node(fixture.lab_data, 7)

    with pytest.raises(NodeRuntimeError, match="stopped"):
        fixture.service.capture_iol_snapshot(fixture.lab_data, 7)

    assert snapshot.read_bytes() == previous
    assert len(fixture.popen_calls) == 2


def test_iol_capture_rejects_orphan_after_normal_stop_swallowed_failure(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    record = fixture.service.start_node(fixture.lab_data, 7)
    monkeypatch.setattr(
        IolLauncher,
        "stop_runtime",
        staticmethod(lambda runtime: (_ for _ in ()).throw(OSError("fixture stop failure"))),
    )

    fixture.service.stop_node(fixture.lab_data, 7)

    assert record["pid"] in fixture.live_pids
    with pytest.raises(NodeRuntimeError, match="stopped"):
        fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    assert not (fixture.work_dir / "iol-snapshot.tar").exists()


def test_iol_saved_start_rollback_retains_source_snapshot_and_ordinary_native(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    fixture.service.start_node(fixture.lab_data, 7)
    fixture.service.stop_node(fixture.lab_data, 7)
    marker = fixture.work_dir / ".iol-source.json"
    previous_marker = marker.read_bytes()
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    snapshot = fixture.work_dir / "iol-snapshot.tar"
    previous_snapshot = snapshot.read_bytes()
    monkeypatch.setattr(
        "app.services.node_runtime_service.runtime_pids.register",
        lambda *args: (_ for _ in ()).throw(OSError("fixture registry failure")),
    )

    with pytest.raises(NodeRuntimeError, match="Failed to register"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert marker.read_bytes() == previous_marker
    assert snapshot.read_bytes() == previous_snapshot
    assert fixture.native_path.read_bytes() == b"legacy-native-state"
    assert list(fixture.work_dir.glob(".saved-*")) == []


def test_iol_saved_archive_failure_never_opens_destination_fd(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    snapshot_path = fixture.work_dir / "iol-snapshot.tar"
    previous_snapshot = snapshot_path.read_bytes()
    snapshot = IolLauncher._read_saved_snapshot(fixture.work_dir, set())
    original_open = os.open
    destination_fds = []

    def track_destination_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path).parent.name.startswith(".saved-"):
            destination_fds.append(descriptor)
        return descriptor

    monkeypatch.setattr("app.services.runtime.iol.os.open", track_destination_open)
    monkeypatch.setattr(
        "app.services.runtime.iol.tarfile.open",
        lambda *args, **kwargs: (_ for _ in ()).throw(tarfile.TarError("fixture archive failure")),
    )

    try:
        with pytest.raises(tarfile.TarError, match="archive failure"):
            IolLauncher._materialize_saved_work(fixture.work_dir, snapshot)
    finally:
        for descriptor in destination_fds:
            os.fdopen(descriptor, "wb").close()

    assert destination_fds == []
    assert snapshot_path.read_bytes() == previous_snapshot
    assert list(fixture.work_dir.glob(".saved-*")) == []


def _start_stopped_saved_generation(fixture):
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    record = fixture.service.start_node(fixture.lab_data, 7)
    work_dir = Path(record["work_dir"])
    fixture.service.stop_node(fixture.lab_data, 7)
    return work_dir, (fixture.work_dir / ".iol-source.json").read_bytes()


def _seed_stopped_runtime_with_old_marker(fixture):
    fixture.service.start_node(fixture.lab_data, 7)
    fixture.service.stop_node(fixture.lab_data, 7)
    old_marker = (fixture.work_dir / ".iol-source.json").read_bytes()
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    record = IolLauncher.start_node(
        lab_id=fixture.lab_data["id"],
        node_id=7,
        node=fixture.lab_data["nodes"]["7"],
        images_root=fixture.settings.IMAGES_DIR / "iol",
        work_dir=fixture.work_dir,
        console_port=16000,
        attachments=[],
        tap_factory=lambda *_: pytest.fail("unexpected TAP setup"),
        active_application_ids=set(),
    )
    runtime = record.as_runtime()
    fixture.service._persist_runtime(runtime)
    fixture.service._registry[fixture.service._key(fixture.lab_data["id"], 7)] = runtime
    fixture.live_pids.discard(record.pid)
    fixture.live_pids.discard(record.ubridge_pid)
    (record.work_dir / fixture.native_path.name).write_bytes(b"crash-runtime-state")
    fixture.native_path.write_bytes(b"old-root-state")
    return record, old_marker


def test_iol_start_refuses_live_prior_saved_source_without_deleting_it(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    first = fixture.service.start_node(fixture.lab_data, 7)
    first_work = Path(first["work_dir"])
    marker = fixture.work_dir / ".iol-source.json"
    previous_marker = marker.read_bytes()
    monkeypatch.setattr(
        IolLauncher,
        "stop_runtime",
        staticmethod(lambda runtime: (_ for _ in ()).throw(OSError("fixture stop failure"))),
    )
    fixture.service.stop_node(fixture.lab_data, 7)
    popen_count = len(fixture.popen_calls)

    with pytest.raises(NodeRuntimeError, match="stopped"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert len(fixture.popen_calls) == popen_count
    assert first_work.exists()
    assert marker.read_bytes() == previous_marker


def test_iol_persist_failure_preserves_previous_source_and_cleans_dead_new_work(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    first_work, previous_marker = _start_stopped_saved_generation(fixture)
    marker = fixture.work_dir / ".iol-source.json"
    monkeypatch.setattr(
        fixture.service,
        "_persist_runtime",
        lambda runtime: (_ for _ in ()).throw(OSError("fixture persistence failure")),
    )

    with pytest.raises(OSError, match="persistence failure"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert marker.read_bytes() == previous_marker
    assert first_work.exists()
    assert list(fixture.work_dir.glob(".saved-*")) == [first_work]
    assert fixture.live_pids == set()
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    ) is None


def test_iol_marker_failure_after_persistence_preserves_previous_source(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    first_work, previous_marker = _start_stopped_saved_generation(fixture)
    marker = fixture.work_dir / ".iol-source.json"
    persisted = []
    original_persist = fixture.service._persist_runtime

    def record_persistence(runtime):
        persisted.append(runtime["work_dir"])
        return original_persist(runtime)

    monkeypatch.setattr(fixture.service, "_persist_runtime", record_persistence)
    monkeypatch.setattr(
        IolLauncher,
        "publish_source",
        staticmethod(lambda *args: (_ for _ in ()).throw(IolError("fixture marker failure"))),
    )

    with pytest.raises(NodeRuntimeError, match="launch source"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert len(persisted) == 1
    assert marker.read_bytes() == previous_marker
    assert first_work.exists()
    assert list(fixture.work_dir.glob(".saved-*")) == [first_work]
    assert fixture.live_pids == set()
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    ) is None


def test_iol_start_preserves_staged_work_when_runtime_restoration_fails(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    previous_record, _ = _seed_stopped_runtime_with_old_marker(fixture)
    first_work = previous_record.work_dir
    original_persist = fixture.service._persist_runtime

    def fail_previous_restore(runtime):
        if runtime["work_dir"] == str(first_work):
            raise OSError("fixture runtime restoration failure")
        return original_persist(runtime)

    monkeypatch.setattr(fixture.service, "_persist_runtime", fail_previous_restore)
    monkeypatch.setattr(
        IolLauncher,
        "publish_source",
        staticmethod(lambda *args: (_ for _ in ()).throw(IolError("fixture marker failure"))),
    )

    with pytest.raises(OSError, match="runtime restoration failure"):
        fixture.service.start_node(fixture.lab_data, 7)

    staged_work = [path for path in fixture.work_dir.glob(".saved-*") if path != first_work]
    assert len(staged_work) == 1
    assert (staged_work[0] / fixture.native_path.name).read_bytes() == b"legacy-native-state"
    assert first_work.exists()
    assert IolLauncher.read_source(fixture.work_dir).workspace == first_work.name
    persisted = json.loads(fixture.service._state_path(fixture.lab_data["id"], 7).read_text())
    assert Path(persisted["work_dir"]) == staged_work[0]
    assert fixture.live_pids == set()


def test_iol_start_rollback_retains_new_work_when_termination_fails(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    _, previous_marker = _start_stopped_saved_generation(fixture)
    monkeypatch.setattr(
        "app.services.node_runtime_service.runtime_pids.register",
        lambda *args: (_ for _ in ()).throw(OSError("fixture registry failure")),
    )
    monkeypatch.setattr(
        IolLauncher,
        "stop_runtime",
        staticmethod(lambda runtime: (_ for _ in ()).throw(OSError("fixture termination failure"))),
    )

    with pytest.raises(NodeRuntimeError, match="Failed to register"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert len(fixture.live_pids) == 2
    assert len(list(fixture.work_dir.glob(".saved-*"))) == 2
    assert (fixture.work_dir / ".iol-source.json").read_bytes() == previous_marker


def test_iol_post_popen_failure_retains_work_until_both_processes_exit(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    monkeypatch.setattr(IolLauncher, "_terminate_pid", staticmethod(lambda pid: None))

    class FailedBridge:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("fixture post-Popen failure")

        def stop(self):
            return None

    class WorkingBridge:
        def __init__(self, **kwargs):
            pass

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr("app.services.runtime.iol.IolConsoleBridge", FailedBridge)

    with pytest.raises(NodeRuntimeError, match="post-Popen failure"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert len(fixture.live_pids) == 2
    staged_work = list(fixture.work_dir.glob(".saved-*"))
    assert len(staged_work) == 1
    persisted = json.loads(fixture.service._state_path(fixture.lab_data["id"], 7).read_text())
    assert persisted["iol_start_incomplete"] is True
    assert Path(persisted["work_dir"]) == staged_work[0]
    marker = fixture.work_dir / ".iol-source.json"
    popen_count = len(fixture.popen_calls)

    with pytest.raises(NodeRuntimeError, match="previous IOL launch"):
        fixture.service.start_node(fixture.lab_data, 7)
    with pytest.raises(NodeRuntimeError, match="previous IOL launch"):
        fixture.service.capture_iol_snapshot(fixture.lab_data, 7)

    assert len(fixture.popen_calls) == popen_count
    assert staged_work[0].exists()
    assert (staged_work[0] / fixture.native_path.name).read_bytes() == b"legacy-native-state"
    assert not marker.exists()

    with pytest.raises(NodeRuntimeError, match="confirm"):
        fixture.service.stop_node(fixture.lab_data, 7)

    assert staged_work[0].exists()
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    )["iol_start_incomplete"] is True

    monkeypatch.setattr(
        IolLauncher, "_terminate_pid", staticmethod(fixture.live_pids.discard)
    )
    fixture.service.stop_node(fixture.lab_data, 7)

    assert fixture.live_pids == set()
    assert not staged_work[0].exists()
    assert not marker.exists()
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    ) is None

    monkeypatch.setattr("app.services.runtime.iol.IolConsoleBridge", WorkingBridge)
    retried = fixture.service.start_node(fixture.lab_data, 7)

    assert len(fixture.popen_calls) == popen_count + 2
    assert Path(retried["work_dir"]).exists()
    assert "iol_start_incomplete" not in retried


def test_iol_partial_ubridge_failure_persists_no_fake_primary_and_refuses_retry(
    saved_runtime, monkeypatch
):
    fixture = saved_runtime
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    fixture.lab_data["nodes"]["7"]["extras"]["config"] = "Saved"
    monkeypatch.setattr(IolLauncher, "_terminate_pid", staticmethod(lambda pid: None))

    class FailedUbridgeClient:
        def __init__(self, port):
            pass

        def request(self, command):
            raise RuntimeError("fixture partial uBridge failure")

        def close(self):
            return None

    class WorkingUbridgeClient:
        def __init__(self, port):
            pass

        def request(self, command):
            return None

        def close(self):
            return None

    monkeypatch.setattr("app.services.runtime.iol._UbridgeClient", FailedUbridgeClient)

    with pytest.raises(NodeRuntimeError, match="partial uBridge failure"):
        fixture.service.start_node(fixture.lab_data, 7)

    assert len(fixture.popen_calls) == 1
    staged_work = list(fixture.work_dir.glob(".saved-*"))
    assert len(staged_work) == 1
    persisted = json.loads(fixture.service._state_path(fixture.lab_data["id"], 7).read_text())
    assert persisted["iol_start_incomplete"] is True
    assert persisted["pid"] is None
    assert persisted["ubridge_pid"] in fixture.live_pids
    assert Path(persisted["work_dir"]) == staged_work[0]
    marker = fixture.work_dir / ".iol-source.json"

    with pytest.raises(NodeRuntimeError, match="previous IOL launch"):
        fixture.service.start_node(fixture.lab_data, 7)
    with pytest.raises(NodeRuntimeError, match="previous IOL launch"):
        fixture.service.capture_iol_snapshot(fixture.lab_data, 7)

    assert len(fixture.popen_calls) == 1
    assert staged_work[0].exists()
    assert not marker.exists()

    with pytest.raises(NodeRuntimeError, match="confirm"):
        fixture.service.stop_node(fixture.lab_data, 7)

    assert staged_work[0].exists()
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    )["iol_start_incomplete"] is True

    monkeypatch.setattr(
        IolLauncher, "_terminate_pid", staticmethod(fixture.live_pids.discard)
    )
    fixture.service.stop_node(fixture.lab_data, 7)

    assert fixture.live_pids == set()
    assert not staged_work[0].exists()
    assert not marker.exists()
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    ) is None

    monkeypatch.setattr("app.services.runtime.iol._UbridgeClient", WorkingUbridgeClient)
    retried = fixture.service.start_node(fixture.lab_data, 7)

    assert len(fixture.popen_calls) == 3
    assert Path(retried["work_dir"]).exists()
    assert "iol_start_incomplete" not in retried


def test_iol_capture_reconciles_valid_stopped_runtime_with_old_marker(saved_runtime):
    fixture = saved_runtime
    record, _ = _seed_stopped_runtime_with_old_marker(fixture)

    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)

    assert IolLauncher.read_source(fixture.work_dir).workspace == record.work_dir.name
    assert _archive_payload(fixture.work_dir / "iol-snapshot.tar") == b"crash-runtime-state"


def test_iol_stop_reconciles_valid_runtime_before_deleting_metadata(saved_runtime):
    fixture = saved_runtime
    record, _ = _seed_stopped_runtime_with_old_marker(fixture)

    fixture.service.stop_node(fixture.lab_data, 7, confirm_stopped=True)

    assert IolLauncher.read_source(fixture.work_dir).workspace == record.work_dir.name
    assert fixture.service._runtime_record(
        fixture.lab_data["id"], 7, include_stopped=True
    ) is None


@pytest.mark.parametrize(
    "marker_data",
    [
        [],
        None,
        {
            "workspace": ".",
            "application_id": 101,
            "pid": 7000,
            "pid_create_time": 10**400,
        },
    ],
    ids=["array", "null", "overflowing-timestamp"],
)
def test_iol_source_marker_shape_errors_are_iol_errors(saved_runtime, marker_data):
    marker = saved_runtime.work_dir / ".iol-source.json"
    marker.write_text(json.dumps(marker_data))

    with pytest.raises(IolError):
        IolLauncher.read_source(saved_runtime.work_dir)


def test_iol_capture_rejects_managed_workspace_directory_symlink(saved_runtime):
    fixture = saved_runtime
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    snapshot = fixture.work_dir / "iol-snapshot.tar"
    previous = snapshot.read_bytes()
    outside = fixture.settings.TMP_DIR / "outside"
    outside.mkdir()
    (outside / fixture.native_path.name).write_bytes(b"outside-state")
    (fixture.work_dir / ".saved-link").symlink_to(outside, target_is_directory=True)
    (fixture.work_dir / ".iol-source.json").write_text(
        json.dumps(
            {
                "workspace": ".saved-link",
                "application_id": fixture.preferred_id,
                "pid": 7999,
                "pid_create_time": 8999.0,
            }
        )
    )

    with pytest.raises(NodeRuntimeError):
        fixture.service.capture_iol_snapshot(fixture.lab_data, 7)

    assert snapshot.read_bytes() == previous


def test_iol_selected_native_file_disappearance_fails_safely(saved_runtime, monkeypatch):
    fixture = saved_runtime
    original_open = os.open

    def lose_selected_native(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and str(path).startswith("nvram_"):
            raise FileNotFoundError(path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("app.services.runtime.iol.os.open", lose_selected_native)

    with pytest.raises(IolError, match="could not inspect native NVRAM"):
        IolLauncher.application_id_for_start(
            fixture.lab_data["id"],
            7,
            fixture.lab_data["nodes"]["7"],
            fixture.work_dir,
            set(),
        )

    assert fixture.native_path.read_bytes() == b"legacy-native-state"
    assert fixture.popen_calls == []


def test_iol_capture_anchors_native_read_against_symlink_swap(saved_runtime, monkeypatch):
    fixture = saved_runtime
    fixture.service.capture_iol_snapshot(fixture.lab_data, 7)
    snapshot = fixture.work_dir / "iol-snapshot.tar"
    previous = snapshot.read_bytes()
    outside = fixture.settings.TMP_DIR / "outside-native"
    outside.write_bytes(b"swapped-state")
    original_lstat = Path.lstat
    swapped = False

    def swap_native(path):
        nonlocal swapped
        result = original_lstat(path)
        if path == fixture.native_path and not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(outside)
        return result

    monkeypatch.setattr(Path, "lstat", swap_native)

    with pytest.raises(NodeRuntimeError):
        fixture.service.capture_iol_snapshot(fixture.lab_data, 7)

    assert snapshot.read_bytes() == previous


@pytest.mark.asyncio
async def test_iol_snapshot_route_post_contract_auth_and_audit(
    saved_runtime, monkeypatch, audit_events
):
    fixture = saved_runtime
    lab_path = fixture.settings.LABS_DIR / "sample.json"
    lab_path.write_text(json.dumps(fixture.lab_data))
    calls = []

    class FakeRuntime:
        def capture_iol_snapshot(self, lab_data, node_id):
            calls.append((lab_data["id"], node_id))

    monkeypatch.setattr("app.routers.labs.NodeRuntimeService", FakeRuntime)
    route = next(
        route
        for route in labs.router.routes
        if route.path == "/api/labs/{lab_path:path}/nodes/{node_id}/iol-snapshot"
    )
    assert route.methods == {"POST"}

    denied = await labs.capture_iol_snapshot(
        "sample.json",
        7,
        current_user=SimpleNamespace(username="trainee", role="user", folder="/trainee"),
    )
    assert denied["code"] == 403
    assert calls == []

    response = await labs.capture_iol_snapshot(
        "sample.json",
        7,
        current_user=SimpleNamespace(username="admin", role="admin", folder=""),
    )

    assert response == {
        "code": 200,
        "status": "success",
        "message": "IOL snapshot captured.",
    }
    assert calls == [("lab-saved-iol", 7)]
    assert audit_events[-1].event == "node.iol_snapshot"
    assert "contents" not in response
    assert os.path.basename(route.endpoint.__name__) == "capture_iol_snapshot"
