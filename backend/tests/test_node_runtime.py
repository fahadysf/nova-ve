import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.routers import labs
from app.services.html5_service import Html5SessionService
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
        GUACAMOLE_JSON_SECRET_KEY="4c0b569e4c96df157eee1b65dd0e4d41",
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
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
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: runtime_settings)
    return runtime_settings


def _fake_subprocess_run_factory(recorded_calls):
    real_run = subprocess.run

    def _fake_run(cmd, capture_output=False, text=False, **kwargs):
        if os.path.basename(cmd[0]) == "openssl":
            return real_run(cmd, capture_output=capture_output, text=text, **kwargs)
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


def _decrypt_guacamole_payload_value(payload: str, secret_key: str) -> dict:
    decrypted = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            "-d",
            "-K",
            secret_key,
            "-iv",
            "00000000000000000000000000000000",
            "-nosalt",
            "-base64",
            "-A",
        ],
        input=payload.encode("utf-8"),
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(decrypted[32:].decode("utf-8"))


def _decrypt_guacamole_payload(url: str, secret_key: str) -> dict:
    parsed = urlparse(url)
    payload = parse_qs(parsed.query)["data"][0]
    return _decrypt_guacamole_payload_value(payload, secret_key)


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
    async def fake_console_url(*_args, **_kwargs):
        return "/html5/#/client/test-client?token=test-token"

    monkeypatch.setattr(
        "app.services.html5_service.Html5SessionService.create_console_url",
        fake_console_url,
    )

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
    assert node["url"] == "/api/labs/sample.json/nodes/1/html5"

    log_response = await labs.node_logs("sample.json", 1, tail=20, follow=False, current_user=SimpleNamespace(username="admin"))
    assert log_response["data"]["logs"] == "boot ok"

    console_response = await labs.node_console("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert console_response["code"] == 200
    assert console_response["data"]["console"] == "telnet"
    assert console_response["data"]["port"] > 0
    assert console_response["data"]["url"] == "/api/labs/sample.json/nodes/1/html5"

    telnet_response = await labs.node_telnet("sample.json", 1, current_user=SimpleNamespace(username="admin"))
    assert "telnet://127.0.0.1:" in telnet_response.body.decode()
    assert telnet_response.headers["content-disposition"].endswith('node-1.telnet"')

    html5_response = await labs.node_html5(
        "sample.json",
        1,
        current_user=SimpleNamespace(username="admin", html5=True, pod=0),
    )
    assert html5_response.status_code == 307
    assert html5_response.headers["location"] == "/html5/#/client/test-client?token=test-token"

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
    )
    assert response["code"] == 403
    assert "disabled" in response["message"].lower()


@pytest.mark.asyncio
async def test_html5_session_service_encrypts_guacamole_json(patched_settings):
    service = Html5SessionService()
    encrypted_payload = service._encrypted_payload(
        current_user=SimpleNamespace(username="admin"),
        host="host.docker.internal",
        port=3389,
        protocol="rdp",
        connection_name="rdp-node",
    )

    payload = _decrypt_guacamole_payload_value(encrypted_payload, patched_settings.GUACAMOLE_JSON_SECRET_KEY)
    assert payload["username"] == "admin"
    assert payload["connections"]["rdp-node"]["protocol"] == "rdp"
    assert payload["connections"]["rdp-node"]["parameters"]["hostname"] == "host.docker.internal"
    assert payload["connections"]["rdp-node"]["parameters"]["port"] == "3389"
    assert payload["connections"]["rdp-node"]["parameters"]["ignore-cert"] == "true"


@pytest.mark.asyncio
async def test_html5_session_service_builds_direct_client_url(monkeypatch, patched_settings):
    service = Html5SessionService()

    async def fake_request_auth_token(_encrypted_payload: str):
        return "TOKEN-123", "json"

    async def fake_connection_identifier(_auth_token: str, _data_source: str, _connection_name: str):
        return "alpine-vnc"

    monkeypatch.setattr(service, "_request_auth_token", fake_request_auth_token)
    monkeypatch.setattr(service, "_connection_identifier", fake_connection_identifier)

    url = await service.create_console_url(
        SimpleNamespace(username="admin"),
        host="192.0.2.50",
        port=3389,
        protocol="rdp",
        connection_name="rdp-node",
    )

    assert url == "/html5/#/client/YWxwaW5lLXZuYwBjAGpzb24%3D?token=TOKEN-123"


@pytest.mark.asyncio
async def test_start_stop_docker_nodes_attach_to_shared_lab_network(monkeypatch, patched_settings):
    lab_data = {
        "id": "lab-123",
        "meta": {"name": "docker-demo"},
        "nodes": {
            "1": {
                "id": 1,
                "name": "alpine-a",
                "type": "docker",
                "image": "nova-ve-alpine-telnet:latest",
                "console": "telnet",
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "interfaces": [{"name": "eth0", "network_id": 1}],
            },
            "2": {
                "id": 2,
                "name": "alpine-b",
                "type": "docker",
                "image": "nova-ve-alpine-telnet:latest",
                "console": "telnet",
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "interfaces": [{"name": "eth0", "network_id": 1}],
            },
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lab-link",
                "type": "bridge",
            }
        },
        "topology": [],
    }

    recorded_calls: list[list[str]] = []
    existing_networks: set[str] = set()
    containers: dict[str, dict[str, object]] = {}

    _mock_runtime_binaries(monkeypatch)

    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        recorded_calls.append(cmd)
        if os.path.basename(cmd[0]) != "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        args = cmd[3:] if len(cmd) > 2 and cmd[1] == "--host" else cmd[1:]
        if args[:2] == ["network", "inspect"]:
            network_name = args[2]
            if network_name not in existing_networks:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such network")

            attached = {
                name: {}
                for name, container in containers.items()
                if network_name in container["networks"]
            }
            stdout = json.dumps([{"Name": network_name, "Containers": attached}])
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        if args[:2] == ["network", "create"]:
            network_name = args[-1]
            existing_networks.add(network_name)
            return SimpleNamespace(returncode=0, stdout=f"{network_name}\n", stderr="")

        if args[:2] == ["network", "connect"]:
            network_name = args[-2]
            container_name = args[-1]
            containers[container_name]["networks"].add(network_name)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        if args[:2] == ["network", "rm"]:
            network_name = args[-1]
            existing_networks.discard(network_name)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        if args[0] == "run":
            container_name = args[args.index("--name") + 1]
            network_name = args[args.index("--network") + 1]
            containers[container_name] = {
                "running": True,
                "pid": 2200 + len(containers),
                "networks": {network_name},
            }
            return SimpleNamespace(returncode=0, stdout=f"{container_name}-cid\n", stderr="")

        if args[:2] == ["inspect", "-f"]:
            template = args[2]
            container_name = args[3]
            container = containers.get(container_name)
            if not container:
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            if template == "{{.State.Pid}}":
                return SimpleNamespace(returncode=0, stdout=f"{container['pid']}\n", stderr="")
            if template == "{{.State.Running}}":
                return SimpleNamespace(
                    returncode=0,
                    stdout=("true" if container["running"] else "false") + "\n",
                    stderr="",
                )

        if args[0] == "stop":
            container_name = args[-1]
            if container_name in containers:
                containers[container_name]["running"] = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        if args[0] == "rm":
            container_name = args[-1]
            containers.pop(container_name, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        raise AssertionError(f"Unhandled docker invocation: {cmd}")

    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(create_time=lambda: float(pid)),
    )

    service = NodeRuntimeService()
    runtime_a = service.start_node(lab_data, 1)
    runtime_b = service.start_node(lab_data, 2)

    assert runtime_a["network_names"] == ["nova-ve-lab123-net1"]
    assert runtime_b["network_names"] == ["nova-ve-lab123-net1"]

    network_create_calls = [call for call in recorded_calls if call[3:5] == ["network", "create"]]
    assert len(network_create_calls) == 1

    run_calls = [call for call in recorded_calls if call[3] == "run"]
    assert len(run_calls) == 2
    assert all("--network" in call for call in run_calls)
    assert run_calls[0][run_calls[0].index("--network") + 1] == "nova-ve-lab123-net1"
    assert run_calls[1][run_calls[1].index("--network") + 1] == "nova-ve-lab123-net1"
    assert run_calls[0][run_calls[0].index("--network-alias") + 1] == "alpine-a"
    assert run_calls[1][run_calls[1].index("--network-alias") + 1] == "alpine-b"

    service.stop_node(lab_data, 1)
    assert "nova-ve-lab123-net1" in existing_networks

    service.stop_node(lab_data, 2)
    assert "nova-ve-lab123-net1" not in existing_networks
