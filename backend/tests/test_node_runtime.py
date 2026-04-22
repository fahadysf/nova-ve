import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers import labs
from app.services.node_runtime_service import NodeRuntimeService


@pytest.fixture(autouse=True)
def reset_runtime_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


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
    )


@pytest.fixture()
def sample_lab(runtime_settings):
    lab_path = runtime_settings.LABS_DIR / "sample.json"
    lab_data = {
        "id": "lab-123",
        "meta": {"name": "sample"},
        "nodes": {
            "1": {
                "id": 1,
                "name": "router-1",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "cpu": 2,
                "ram": 1024,
                "ethernet": 2,
                "firstmac": "50:00:00:01:00:00",
            }
        },
        "networks": {},
        "topology": [],
    }
    lab_path.write_text(json.dumps(lab_data))

    image_dir = runtime_settings.IMAGES_DIR / "qemu" / "router-image"
    image_dir.mkdir(parents=True)
    (image_dir / "hda.qcow2").write_text("base-image")
    return lab_data


@pytest.fixture()
def patched_settings(monkeypatch, runtime_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: runtime_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: runtime_settings)
    return runtime_settings


def _fake_subprocess_run_factory(recorded_calls):
    def _fake_run(cmd, capture_output=False, text=False):
        recorded_calls.append(cmd)
        if os.path.basename(cmd[0]) == "qemu-img":
            Path(cmd[-1]).write_text("overlay-image")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _fake_run


class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self):
        return None


class _RuntimeFakeDB:
    def __init__(self):
        self.executed = []
        self.added = []
        self.commit_count = 0

    async def execute(self, query):
        self.executed.append(query)
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1


def _mock_runtime_binaries(monkeypatch):
    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService._resolve_binary",
        staticmethod(lambda binary: binary),
    )


@pytest.mark.asyncio
async def test_start_stop_and_wipe_qemu_node(monkeypatch, patched_settings, sample_lab):
    recorded_runs = []
    recorded_popen = []
    killed = []

    _mock_runtime_binaries(monkeypatch)
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", _fake_subprocess_run_factory(recorded_runs))

    def fake_popen(cmd, cwd=None, stdin=None, stdout=None, stderr=None, start_new_session=None):
        recorded_popen.append({"cmd": cmd, "cwd": str(cwd)})
        if stdout is not None:
            stdout.write(b"boot ok\n")
        return _FakeProcess(4321)

    monkeypatch.setattr("app.services.node_runtime_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.services.node_runtime_service.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.services.node_runtime_service.psutil.Process", lambda pid: SimpleNamespace(
        create_time=lambda: 111.0,
        cpu_percent=lambda interval=0.0: 7.2,
        memory_info=lambda: SimpleNamespace(rss=2048),
        wait=lambda timeout=5: None,
        is_running=lambda: True,
        status=lambda: "sleeping",
    ))
    monkeypatch.setattr("app.services.node_runtime_service.os.killpg", lambda pid, sig: killed.append((pid, sig)))

    start_response = await labs.start_node("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert start_response["code"] == 200
    assert os.path.basename(recorded_popen[0]["cmd"][0]) == "qemu-system-x86_64"
    overlay_path = patched_settings.TMP_DIR / "lab-123" / "1" / "virtioa.qcow2"
    assert overlay_path.exists()

    nodes_response = await labs.list_nodes("sample.json", current_user=SimpleNamespace(username="admin"))
    node = nodes_response["data"]["1"]
    assert node["status"] == 2
    assert node["cpu_usage"] == 7
    assert node["ram_usage"] == 2048
    assert node["url"].startswith("/html5/#/client/")

    log_response = await labs.node_logs("sample.json", 1, tail=20, follow=False, current_user=SimpleNamespace(username="admin"))
    assert log_response["data"]["logs"] == "boot ok"

    console_response = await labs.node_console("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert console_response["code"] == 200
    assert console_response["data"]["console"] == "telnet"
    assert console_response["data"]["port"] > 0

    telnet_response = await labs.node_telnet("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert "telnet://127.0.0.1:" in telnet_response.body.decode()
    assert telnet_response.headers["content-disposition"].endswith('node-1.telnet"')

    html5_db = _RuntimeFakeDB()
    html5_response = await labs.node_html5(
        "sample.json",
        1,
        current_user=SimpleNamespace(username="admin", html5=True, pod=0),
        db=html5_db,
    )
    assert html5_response.status_code == 307
    assert "/html5/#/client/" in html5_response.headers["location"]
    assert "token=" in html5_response.headers["location"]
    assert html5_db.added

    stop_response = await labs.stop_node("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert stop_response["code"] == 200
    assert killed

    wipe_response = await labs.wipe_node("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert wipe_response["code"] == 200
    assert not overlay_path.exists()


@pytest.mark.asyncio
async def test_start_node_fails_when_qemu_image_missing(monkeypatch, patched_settings, sample_lab):
    _mock_runtime_binaries(monkeypatch)
    missing_image_lab = dict(sample_lab)
    missing_image_lab["nodes"] = {
        "1": {
            **sample_lab["nodes"]["1"],
            "image": "missing-image",
        }
    }
    (patched_settings.LABS_DIR / "sample.json").write_text(json.dumps(missing_image_lab))

    response = await labs.start_node("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert response["code"] == 400
    assert "base image not found" in response["message"].lower()


@pytest.mark.asyncio
async def test_rdp_file_generation_uses_console_runtime(monkeypatch, patched_settings, sample_lab):
    monkeypatch.setattr(
        "app.routers.labs.NodeRuntimeService",
        lambda: SimpleNamespace(
            console_info=lambda _lab_data, _node_id: {
                "console": "rdp",
                "host": "127.0.0.1",
                "port": 3391,
                "url": "/html5/#/client/demo",
            }
        ),
    )

    response = await labs.node_rdp("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    body = response.body.decode()
    assert "full address:s:127.0.0.1:3391" in body
    assert response.headers["content-disposition"].endswith('node-1.rdp"')


@pytest.mark.asyncio
async def test_html5_respects_user_flag(monkeypatch, patched_settings, sample_lab):
    monkeypatch.setattr(
        "app.routers.labs.NodeRuntimeService",
        lambda: SimpleNamespace(
            console_info=lambda _lab_data, _node_id: {
                "console": "telnet",
                "host": "127.0.0.1",
                "port": 2323,
                "url": "/html5/#/client/demo",
            }
        ),
    )

    response = await labs.node_html5(
        "sample.json",
        1,
        current_user=SimpleNamespace(username="user", html5=False, pod=0),
        db=_RuntimeFakeDB(),
    )
    assert response["code"] == 403
    assert "disabled" in response["message"].lower()
