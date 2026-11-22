# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for #291 console recording: telnet relay + cast writer + API."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import telnetlib3
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.dependencies import get_current_user
from app.main import app
from app.services.console_recording import ConsoleRecordingService


@pytest.fixture()
def recordings_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("RECORDINGS_ENABLED", "true")
    get_settings.cache_clear()
    yield tmp_path / "recordings"
    get_settings.cache_clear()


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
async def test_proxy_relays_and_records_cast_and_raw_log(recordings_dir):
    node = FakeNodeConsole()
    node_server = await telnetlib3.create_server(
        host="127.0.0.1", port=0, shell=node.shell, term="xterm"
    )
    node_port = node_server.sockets[0].getsockname()[1]

    service = ConsoleRecordingService()
    session = await service.start_session(
        lab_id="lab-uuid-1",
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

    writer.write("show version\r")
    await writer.drain()
    echoed = await asyncio.wait_for(reader.read(1024), timeout=5)
    assert "echo:ok" in echoed

    writer.close()
    await _wait_closed(session)

    lines = session.cast_path.read_text(encoding="utf-8").strip().splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["title"] == "Test Lab / r1"
    events = [json.loads(line) for line in lines[1:]]
    output = "".join(e[2] for e in events if e[1] == "o")
    keystrokes = "".join(e[2] for e in events if e[1] == "i")
    assert "Welcome to fake-node" in output
    assert "echo:ok" in output
    assert "show version" in keystrokes
    assert all(isinstance(e[0], float) for e in events)

    log_text = session.log_path.read_text(encoding="utf-8")
    assert "Welcome to fake-node" in log_text
    assert "echo:ok" in log_text
    assert "show version" not in log_text  # raw log is output-only
    assert not service.is_active(session.cast_path)

    node_server.close()


@pytest.mark.asyncio
async def test_start_session_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDINGS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        service = ConsoleRecordingService()
        assert await service.start_session(
            lab_id="x", lab_name="x", node_id=1, node_name="n",
            target_host="127.0.0.1", target_port=1, username="u",
        ) is None
    finally:
        get_settings.cache_clear()


def _write_recording(base, node_id=3, name="20261122-120000-abcd1234"):
    directory = base / "lab-uuid-1" / str(node_id)
    directory.mkdir(parents=True, exist_ok=True)
    cast = directory / f"{name}.cast"
    cast.write_text(
        json.dumps({"version": 2, "width": 120, "height": 30,
                    "timestamp": 1763822400, "title": "Test Lab / r1"}) + "\n"
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
def api_client(monkeypatch, recordings_dir):
    cast, log = _write_recording(recordings_dir)
    monkeypatch.setattr(
        "app.routers.recordings._lab_id_for", lambda user, path: "lab-uuid-1"
    )
    _override_auth()
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_recordings(api_client):
    async with api_client as client:
        response = await client.get("/api/labs/test-lab/recordings")
    assert response.status_code == 200
    entries = response.json()["data"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == "3--20261122-120000-abcd1234.cast"
    assert entry["node_id"] == 3
    assert entry["duration"] == 5.0
    assert entry["title"] == "Test Lab / r1"
    assert entry["started_at"].startswith("2025-11-22T")
    assert entry["cast_size"] > 0 and entry["log_size"] > 0
    assert entry["in_progress"] is False


@pytest.mark.asyncio
async def test_content_strips_ansi(api_client):
    async with api_client as client:
        response = await client.get(
            "/api/labs/test-lab/recordings/3--20261122-120000-abcd1234.cast/content"
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["text"] == "hello red world\nrouter# show run\n"
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_download_cast_and_log(api_client):
    async with api_client as client:
        cast = await client.get(
            "/api/labs/test-lab/recordings/3--20261122-120000-abcd1234.cast/cast"
        )
        log = await client.get(
            "/api/labs/test-lab/recordings/3--20261122-120000-abcd1234.cast/log"
        )
    assert cast.status_code == 200
    assert json.loads(cast.text.splitlines()[0])["version"] == 2
    assert "attachment" in cast.headers.get("content-disposition", "")
    assert log.status_code == 200
    assert "router# show run" in log.text


@pytest.mark.asyncio
async def test_unknown_or_traversal_recording_ids_return_404(api_client):
    async with api_client as client:
        missing = await client.get("/api/labs/test-lab/recordings/3--nope.cast/cast")
        traversal = await client.get(
            "/api/labs/test-lab/recordings/..--20261122-120000-abcd1234.cast/cast"
        )
        bad_node = await client.get("/api/labs/test-lab/recordings/x--nope.cast/cast")
    assert missing.json()["code"] == 404
    assert traversal.json()["code"] == 404
    assert bad_node.json()["code"] == 404


@pytest.mark.asyncio
async def test_permission_denied(monkeypatch, recordings_dir):
    def deny(user, path):
        raise PermissionError("not your lab")

    monkeypatch.setattr("app.routers.recordings._lab_id_for", deny)
    _override_auth()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/labs/test-lab/recordings")
        assert response.json()["code"] == 403
    finally:
        app.dependency_overrides.clear()
