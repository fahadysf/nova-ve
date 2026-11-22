# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for #291 console recording: telnet relay + cast writer + API."""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import telnetlib3
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user
from app.main import app
from app.services.console_recording import ConsoleRecordingService, lab_recordings_dir
from app.services.folder_service import FolderService


@pytest.fixture()
def recordings_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LABS_DIR", str(tmp_path / "labs"))
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("RECORDINGS_ENABLED", "true")
    get_settings.cache_clear()
    yield tmp_path / "recordings"
    get_settings.cache_clear()


@pytest.fixture()
def lab_file(recordings_dir):
    path = get_settings().LABS_DIR / "test-lab.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"schema": 2, "id": "lab-uuid-1", "nodes": {"3": {"name": "r1"}}}))
    return path


class FakeNodeConsole:
    """Minimal telnet server standing in for a QEMU serial console."""

    def __init__(self):
        self.received = []

    async def shell(self, reader, writer):
        writer.write("Welcome to fake-node\r\n")
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                self.received.append(data)
                writer.write("echo:ok\r\n")
        finally:
            writer.close()


async def _wait_closed(session, timeout=5.0):
    for _ in range(int(timeout * 20)):
        if session._closed:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("recording session did not close")


@pytest.mark.asyncio
async def test_proxy_records_only_between_start_and_stop(lab_file):
    node = FakeNodeConsole()
    node_server = await telnetlib3.create_server(
        host="127.0.0.1", port=0, shell=node.shell, term="xterm"
    )
    node_port = node_server.sockets[0].getsockname()[1]

    service = ConsoleRecordingService()
    session = await service.start_session(
        lab_id="lab-uuid-1",
        lab_file=lab_file,
        lab_name="Test Lab",
        node_id=3,
        node_name="r1",
        target_host="127.0.0.1",
        target_port=node_port,
        username="admin",
        listen_host="127.0.0.1",
    )
    assert session is not None

    reader, writer = await telnetlib3.open_connection(
        host="127.0.0.1", port=session.listen_port, term="xterm"
    )
    banner = await asyncio.wait_for(reader.read(1024), timeout=5)
    assert "Welcome to fake-node" in banner
    assert not session.status()["recording"]
    directory = lab_recordings_dir(lab_file, "lab-uuid-1")
    assert list(directory.rglob("*.cast")) == []  # nothing recorded on demand-less relay

    session.start_recording()
    assert session.status()["recording"] is True

    writer.write("show version\r")
    await writer.drain()
    echoed = await asyncio.wait_for(reader.read(1024), timeout=5)
    assert "echo:ok" in echoed

    status = session.stop_recording()
    assert status["recording"] is False
    cast_path = session._cast_path
    assert cast_path.parent == directory / "3"
    assert re.fullmatch(r"r1-\d{8}T\d{6}Z-[0-9a-f]{8}-1\.cast", cast_path.name)
    log_path = cast_path.with_suffix(".log")

    lines = cast_path.read_text(encoding="utf-8").strip().splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["title"] == "Test Lab / r1"
    assert header["timestamp"] > 0
    assert header["duration"] >= 0  # stamped when stop was pressed
    assert header["nova"]["username"] == "admin"
    assert header["nova"]["node_name"] == "r1"
    assert header["nova"]["console"] == "primary"
    assert header["nova"]["stopped_at"] >= header["nova"]["started_at"]
    assert cast_path.stat().st_mode & 0o777 == 0o600
    events = [json.loads(line) for line in lines[1:]]
    output = "".join(e[2] for e in events if e[1] == "o")
    keystrokes = "".join(e[2] for e in events if e[1] == "i")
    assert "echo:ok" in output
    assert "Welcome to fake-node" not in output  # banner predates record press
    assert "show version" in keystrokes

    log_text = log_path.read_text(encoding="utf-8")
    assert log_text.startswith("# Recording started ")
    assert "# Recording stopped " in log_text
    assert "echo:ok" in log_text
    assert "show version" not in log_text  # raw log is output-only
    assert not service.is_active(cast_path)

    # A second start/stop cycle produces a separate file pair.
    session.start_recording()
    writer.write("second cycle\r")
    await writer.drain()
    await asyncio.wait_for(reader.read(1024), timeout=5)
    session.stop_recording()
    assert session._cast_path != cast_path
    assert len(list(directory.rglob("*.cast"))) == 2

    writer.close()
    await _wait_closed(session)
    node_server.close()


@pytest.mark.asyncio
async def test_start_session_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDINGS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        service = ConsoleRecordingService()
        assert await service.start_session(
            lab_id="x", lab_file=tmp_path / "x.json", lab_name="x", node_id=1, node_name="n",
            target_host="127.0.0.1", target_port=1, username="u",
        ) is None
    finally:
        get_settings.cache_clear()


def _write_recording(base, node_id=3, name="20261122-120000-abcd1234", *, metadata=None):
    directory = base / "lab-uuid-1" / str(node_id)
    directory.mkdir(parents=True, exist_ok=True)
    cast = directory / f"{name}.cast"
    header = {"version": 2, "width": 120, "height": 30, "timestamp": 1763822400, "title": "Test Lab / r1"}
    if metadata is not None:
        header["nova"] = metadata
    cast.write_text(
        json.dumps(header) + "\n"
        + json.dumps([0.0, "o", "hello "]) + "\n"
        + json.dumps([5.0, "o", "world\r\n"]) + "\n",
        encoding="utf-8",
    )
    log = directory / f"{name}.log"
    log.write_bytes(b"hello \x1b[31mred\x1b[0m world\r\nrouter# show run\r\n")
    return cast, log


def _override_auth():
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="admin", role="admin", html5=True, folder="/"
    )


@pytest.fixture()
def api_client(recordings_dir, lab_file):
    _write_recording(recordings_dir)
    _override_auth()
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_recordings(api_client):
    async with api_client as client:
        response = await client.get("/api/labs/test-lab.json/recordings")
    assert response.status_code == 200
    entries = response.json()["data"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == "3--20261122-120000-abcd1234.cast"
    assert entry["node_id"] == 3
    assert entry["node_name"] == "r1"
    assert entry["console"] is None
    assert entry["duration"] == 5.0
    assert entry["title"] == "Test Lab / r1"
    assert entry["started_at"].startswith("2025-11-22T")
    assert entry["stopped_at"].startswith("2025-11-22T14:40:05")
    assert entry["cast_size"] > 0 and entry["log_size"] > 0
    assert entry["in_progress"] is False


@pytest.mark.asyncio
async def test_node_filter_reads_local_and_legacy_recordings(api_client, lab_file, recordings_dir):
    root = lab_recordings_dir(lab_file, "lab-uuid-1").parent
    metadata = {"node_name": "r2", "username": "admin", "console": "second"}
    cast, _ = _write_recording(root, node_id=4, name="r2-20261122T120000Z-abcd1234-1", metadata=metadata)

    async with api_client as client:
        listed = await client.get("/api/labs/test-lab.json/recordings")
        filtered = await client.get("/api/labs/test-lab.json/recordings?node_id=4")
        legacy = await client.get("/api/labs/test-lab.json/recordings?node_id=3")
        response = await client.get(f"/api/labs/test-lab.json/recordings/4--{cast.name}/cast")

    assert {item["node_id"] for item in listed.json()["data"]} == {3, 4}
    assert [item["node_name"] for item in filtered.json()["data"]] == ["r2"]
    assert [item["console"] for item in filtered.json()["data"]] == ["second"]
    assert [item["node_id"] for item in legacy.json()["data"]] == [3]
    assert response.text == cast.read_text()
    assert response.headers["cache-control"] == "private, no-store"
    assert not list((recordings_dir / "lab-uuid-1" / "4").glob("*.cast"))
    assert not any(".recordings" in folder["path"] for folder in FolderService.list_folder(recursive=True)["folders"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("username", "can_view"), [("recorder", True), ("owner", True), ("other", False)])
async def test_shared_recording_permissions(username, can_view, monkeypatch, lab_file, recordings_dir):
    shared = lab_file.parent / "Shared"
    shared.mkdir()
    lab_file = lab_file.rename(shared / lab_file.name)
    root = lab_recordings_dir(lab_file, "lab-uuid-1").parent
    cast, _ = _write_recording(root, name="r1-20261122T120000Z-abcd1234-1", metadata={"username": "recorder"})
    legacy_cast, legacy_log = _write_recording(recordings_dir)
    legacy_log.write_text("# Recording started 2026-11-22T12:00:00+00:00 by recorder\nhello")

    async def get_lab(self, filename):
        assert filename == "Shared/test-lab.json"
        return SimpleNamespace(owner="owner")

    monkeypatch.setattr("app.routers.recordings.LabService.get_lab_by_filename", get_lab)
    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: None)
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: SimpleNamespace(
        role="user", folder=f"/users/{username}", username=username,
    ))

    base = "/api/labs/Shared/test-lab.json/recordings"
    ids = {f"3--{cast.name}", f"3--{legacy_cast.name}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        listed = await client.get(base)
        assert {item["id"] for item in listed.json()["data"]} == (ids if can_view else set())
        for recording_id in ids:
            for kind in ("cast", "log", "content"):
                response = await client.get(f"{base}/{recording_id}/{kind}")
                if can_view:
                    assert response.status_code == 200
                    if kind == "content":
                        assert response.json()["code"] == 200
                    else:
                        assert "attachment" in response.headers["content-disposition"]
                else:
                    assert response.json()["code"] == 403
            deleted = await client.delete(f"{base}/{recording_id}")
            assert deleted.json()["code"] == (200 if can_view else 403)

    for path in (cast, cast.with_suffix(".log"), legacy_cast, legacy_log):
        assert path.exists() is not can_view


@pytest.mark.asyncio
async def test_delete_recording_requires_authentication(lab_file, recordings_dir):
    cast, log = _write_recording(recordings_dir)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.delete(f"/api/labs/{lab_file.name}/recordings/3--{cast.name}")
    assert response.status_code == 401
    assert cast.is_file() and log.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["local", "legacy"])
async def test_delete_recording_removes_only_selected_pair(api_client, lab_file, recordings_dir, location):
    local = lab_recordings_dir(lab_file, "lab-uuid-1").parent
    root = local if location == "local" else recordings_dir
    cast, log = _write_recording(root, name="target")
    unrelated = _write_recording(root, name="keep")
    other = _write_recording(recordings_dir if root == local else local, name="other")

    base = f"/api/labs/{lab_file.name}/recordings"
    url = f"{base}/3--{cast.name}"
    async with api_client as client:
        results = await asyncio.gather(client.delete(url), client.delete(url))
        assert sorted(response.json()["code"] for response in results) == [200, 404]
        success = next(response.json() for response in results if response.json()["code"] == 200)
        assert success["data"] == {"id": f"3--{cast.name}"}
        for kind in ("cast", "log", "content"):
            response = await client.get(f"{url}/{kind}")
            assert response.json()["code"] == 404
        listed = await client.get(base)
        assert f"3--{cast.name}" not in {entry["id"] for entry in listed.json()["data"]}

    assert not cast.exists() and not log.exists()
    assert all(path.is_file() for path in unrelated + other)
    assert not list(cast.parent.glob(".delete-*"))


@pytest.mark.asyncio
async def test_delete_recording_with_missing_log(api_client, lab_file, recordings_dir):
    cast, log = _write_recording(recordings_dir, name="without-log")
    log.unlink()
    async with api_client as client:
        response = await client.delete(f"/api/labs/{lab_file.name}/recordings/3--{cast.name}")
    assert response.json()["code"] == 200
    assert not cast.exists()


@pytest.mark.asyncio
async def test_delete_recording_rejects_invalid_paths_and_symlinks(api_client, lab_file, recordings_dir):
    cast, log = _write_recording(recordings_dir, name="target")
    other_cast, other_log = _write_recording(recordings_dir, node_id=4, name="elsewhere")
    fake_cast = cast.with_name("symlink.cast")
    fake_cast.symlink_to(other_cast)
    log.unlink()
    log.symlink_to(other_log)

    base = f"/api/labs/{lab_file.name}/recordings"
    async with api_client as client:
        for recording_id in (f"..--{cast.name}", f"3--{fake_cast.name}"):
            response = await client.delete(f"{base}/{recording_id}")
            assert response.json()["code"] == 404
        response = await client.delete(f"{base}/3--{cast.name}")
        assert response.json()["code"] == 409

    assert cast.is_file() and other_cast.is_file() and other_log.is_file()
    assert log.is_symlink() and fake_cast.is_symlink()


@pytest.mark.asyncio
async def test_failed_cast_removal_preserves_log_and_can_be_retried(api_client, lab_file, recordings_dir, monkeypatch):
    cast, log = _write_recording(recordings_dir, name="retry")
    raw = log.read_bytes()
    original_unlink = Path.unlink

    def fail_cast_unlink(path, *args, **kwargs):
        if path == cast:
            raise OSError("cast deletion temporarily unavailable")
        return original_unlink(path, *args, **kwargs)

    url = f"/api/labs/{lab_file.name}/recordings/3--{cast.name}"
    async with api_client as client:
        with monkeypatch.context() as failing:
            failing.setattr(Path, "unlink", fail_cast_unlink)
            response = await client.delete(url)
            assert response.json()["code"] == 500
            assert cast.is_file() and log.read_bytes() == raw
            assert not list(cast.parent.glob(".delete-*"))
        response = await client.delete(url)
        assert response.json()["code"] == 200
    assert not cast.exists() and not log.exists()


@pytest.mark.asyncio
async def test_content_strips_ansi(api_client):
    async with api_client as client:
        response = await client.get(
            "/api/labs/test-lab.json/recordings/3--20261122-120000-abcd1234.cast/content"
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["text"] == "hello red world\nrouter# show run\n"
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_download_cast_and_log(api_client):
    async with api_client as client:
        cast = await client.get(
            "/api/labs/test-lab.json/recordings/3--20261122-120000-abcd1234.cast/cast"
        )
        log = await client.get(
            "/api/labs/test-lab.json/recordings/3--20261122-120000-abcd1234.cast/log"
        )
    assert cast.status_code == 200
    assert json.loads(cast.text.splitlines()[0])["version"] == 2
    assert "attachment" in cast.headers.get("content-disposition", "")
    assert log.status_code == 200
    assert "router# show run" in log.text


@pytest.mark.asyncio
async def test_unknown_or_traversal_recording_ids_return_404(api_client):
    async with api_client as client:
        missing = await client.get("/api/labs/test-lab.json/recordings/3--nope.cast/cast")
        traversal = await client.get(
            "/api/labs/test-lab.json/recordings/..--20261122-120000-abcd1234.cast/cast"
        )
        bad_node = await client.get("/api/labs/test-lab.json/recordings/x--nope.cast/cast")
    assert missing.json()["code"] == 404
    assert traversal.json()["code"] == 404
    assert bad_node.json()["code"] == 404


@pytest.mark.asyncio
async def test_permission_denied(api_client):
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role="user", folder="/other", username="outsider"
    )
    async with api_client as client:
        response = await client.get("/api/labs/test-lab.json/recordings")
    assert response.json()["code"] == 403


@pytest.mark.asyncio
async def test_recording_control_api_start_stop(monkeypatch, lab_file):
    service = ConsoleRecordingService()
    session = await service.start_session(
        lab_id="lab-uuid-1", lab_file=lab_file, lab_name="Test Lab", node_id=3, node_name="r1",
        target_host="127.0.0.1", target_port=1, username="admin", listen_host="127.0.0.1",
    )
    assert session is not None
    monkeypatch.setattr("app.routers.recordings.get_console_recorder", lambda: service)
    _override_auth()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            status = await client.get("/api/labs/test-lab.json/nodes/3/recording")
            assert status.json()["data"]["console_active"] is True
            assert status.json()["data"]["recording"] is False

            started = await client.post(
                "/api/labs/test-lab.json/nodes/3/recording", json={"action": "start"}
            )
            assert started.json()["data"]["recording"] is True
            assert started.json()["data"]["recording_id"].startswith("3--")
            assert started.json()["data"]["started_at"] is not None
            recording_id = started.json()["data"]["recording_id"]
            active = await client.delete(f"/api/labs/test-lab.json/recordings/{recording_id}")
            assert active.json()["code"] == 409

            stopped = await client.post(
                "/api/labs/test-lab.json/nodes/3/recording", json={"action": "stop"}
            )
            assert stopped.json()["data"]["recording"] is False

        casts = list(lab_recordings_dir(lab_file, "lab-uuid-1").rglob("*.cast"))
        assert len(casts) == 1
        header = json.loads(casts[0].read_text(encoding="utf-8").splitlines()[0])
        assert "duration" in header and "timestamp" in header
        log = casts[0].with_suffix(".log").read_text(encoding="utf-8")
        assert "# Recording started " in log
        assert "# Recording stopped " in log
    finally:
        app.dependency_overrides.clear()
        await service.close_all()


@pytest.mark.asyncio
async def test_recording_control_without_console_returns_404(monkeypatch, lab_file):
    service = ConsoleRecordingService()
    monkeypatch.setattr("app.routers.recordings.get_console_recorder", lambda: service)
    _override_auth()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/api/labs/test-lab.json/nodes/3/recording", json={"action": "start"}
            )
        assert response.json()["code"] == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_failed_recording_start_cleans_partial_file_pair(monkeypatch, lab_file):
    import app.services.console_recording as recording_module

    service = ConsoleRecordingService()
    session = await service.start_session(
        lab_id="lab-uuid-1", lab_file=lab_file, lab_name="Test Lab", node_id=3, node_name="r1",
        target_host="127.0.0.1", target_port=1, username="admin", listen_host="127.0.0.1",
    )
    assert session is not None
    open_private = recording_module._private_file

    def fail_log_open(path, flags):
        if path.endswith(".log"):
            raise OSError("unavailable log file")
        return open_private(path, flags)

    try:
        with monkeypatch.context() as failing:
            failing.setattr(recording_module, "_private_file", fail_log_open)
            with pytest.raises(OSError):
                session.start_recording()
            assert session.status()["recording"] is False
            assert not list(lab_recordings_dir(lab_file, "lab-uuid-1").rglob("*.cast"))
        assert session.start_recording()["recording"] is True
        assert session.stop_recording()["recording"] is False
    finally:
        await service.close_all()
