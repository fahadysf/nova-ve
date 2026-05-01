import json
import os
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import psutil
import pytest

from app.routers import labs
from app.services.guacamole_db_service import GuacamoleDatabaseService, _AUTH_TOKEN_CACHE
from app.services.html5_service import Html5SessionService
from app.services.node_runtime_service import NodeRuntimeService


@pytest.fixture(autouse=True)
def reset_runtime_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


@pytest.fixture(autouse=True)
def _redirect_pids_registry(tmp_path_factory, monkeypatch):
    """Always redirect ``runtime_pids`` to a tmp_path so registration in
    QEMU/docker start paths cannot escape into ``/var/lib/nova-ve/runtime``.

    Tests that explicitly seed an instance_id via ``_us203_instance_id``
    override this with their own per-test path.
    """
    pids_dir = tmp_path_factory.mktemp("pids-default")
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(pids_dir / "pids.json"))
    yield


@pytest.fixture(autouse=True)
def reset_guacamole_token_cache():
    _AUTH_TOKEN_CACHE.clear()
    yield
    _AUTH_TOKEN_CACHE.clear()


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
        GUACAMOLE_DATABASE_URL="",
        GUACAMOLE_DATA_SOURCE="postgresql",
        GUACAMOLE_INTERNAL_URL="http://127.0.0.1:8081/html5/",
        GUACAMOLE_JSON_SECRET_KEY="4c0b569e4c96df157eee1b65dd0e4d41",
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
    )


@pytest.fixture()
def sample_lab(runtime_settings):
    lab_path = runtime_settings.LABS_DIR / "sample.json"
    lab_data = {
        "schema": 2,
        "id": "lab-123",
        "meta": {"name": "sample"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
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
        "links": [],
        "defaults": {"link_style": "orthogonal"},
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


def test_resolve_qemu_machine_uses_inferred_q35_defaults_when_template_omits_capabilities(
    monkeypatch, patched_settings, tmp_path
):
    templates_dir = tmp_path / "templates" / "qemu"
    templates_dir.mkdir(parents=True)
    (templates_dir / "legacy.yml").write_text(
        """type: qemu
name: Legacy Router
cpu: 1
ram: 512
ethernet: 2
console_type: telnet
"""
    )
    monkeypatch.setattr(
        "app.services.template_service.get_settings",
        lambda: SimpleNamespace(
            TEMPLATES_DIR=tmp_path / "templates",
            IMAGES_DIR=patched_settings.IMAGES_DIR,
        ),
    )

    service = NodeRuntimeService()
    machine, max_nics, hotplug_capable = service._resolve_qemu_machine(
        {"type": "qemu", "template": "legacy"}
    )

    assert machine == "q35"
    assert max_nics == 8
    assert hotplug_capable is True


@pytest.fixture()
def _us203_instance_id(monkeypatch, tmp_path):
    """Seed an instance_id so ``host_net.bridge_name`` does not blow up.

    Also redirects the runtime pid registry under ``tmp_path`` so the
    backend's ``runtime_pids.register`` writes never touch the real
    ``/var/lib/nova-ve/runtime/`` (which CI cannot write to).
    """
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance_id").write_text("test-instance-203")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))

    pids_path = tmp_path / "runtime" / "pids.json"
    pids_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(pids_path))
    return "test-instance-203"


def _us203_helper_mock(monkeypatch, *, present_bridges: set[str] | None = None):
    """Capture every privileged-helper call. Returns the calls dict.

    All US-203 tests MUST mock the helper — never spawn a real
    ``ip link add`` in CI.
    """
    from app.services import host_net

    if present_bridges is None:
        present_bridges = set()

    calls: dict[str, list] = {
        "bridge_exists": [],
        "veth_pair_add": [],
        "link_master": [],
        "link_up": [],
        "link_netns": [],
        "link_set_name_in_netns": [],
        "addr_up_in_netns": [],
        "link_del": [],
    }

    def fake_bridge_exists(name: str) -> bool:
        calls["bridge_exists"].append(name)
        return name in present_bridges

    def fake_veth_pair_add(host_end: str, peer_end: str) -> None:
        calls["veth_pair_add"].append((host_end, peer_end))

    def fake_link_master(iface: str, bridge: str) -> None:
        calls["link_master"].append((iface, bridge))

    def fake_link_up(iface: str) -> None:
        calls["link_up"].append(iface)

    def fake_link_netns(iface: str, pid: int) -> None:
        calls["link_netns"].append((iface, pid))

    def fake_link_set_name_in_netns(pid: int, oldname: str, newname: str) -> None:
        calls["link_set_name_in_netns"].append((pid, oldname, newname))

    def fake_addr_up_in_netns(pid: int, iface: str) -> None:
        calls["addr_up_in_netns"].append((pid, iface))

    def fake_link_del(name: str) -> None:
        calls["link_del"].append({"fn": "link_del", "name": name})

    def fake_try_link_del(name: str) -> None:
        calls["link_del"].append({"fn": "try_link_del", "name": name})

    monkeypatch.setattr(host_net, "bridge_exists", fake_bridge_exists)
    monkeypatch.setattr(host_net, "veth_pair_add", fake_veth_pair_add)
    monkeypatch.setattr(host_net, "link_master", fake_link_master)
    monkeypatch.setattr(host_net, "link_up", fake_link_up)
    monkeypatch.setattr(host_net, "link_netns", fake_link_netns)
    monkeypatch.setattr(host_net, "link_set_name_in_netns", fake_link_set_name_in_netns)
    monkeypatch.setattr(host_net, "addr_up_in_netns", fake_addr_up_in_netns)
    monkeypatch.setattr(host_net, "link_del", fake_link_del)
    monkeypatch.setattr(host_net, "try_link_del", fake_try_link_del)
    return calls


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
    original_token_hex = secrets.token_hex
    secrets.token_hex = lambda _n=8: "cafebabe"
    encrypted_payload = service._encrypted_payload(
        current_user=SimpleNamespace(username="admin"),
        host="host.docker.internal",
        port=3389,
        protocol="rdp",
        connection_name="rdp-node",
    )
    secrets.token_hex = original_token_hex

    payload = _decrypt_guacamole_payload_value(encrypted_payload, patched_settings.GUACAMOLE_JSON_SECRET_KEY)
    assert payload["username"] == "admin-cafebabe"
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
async def test_html5_route_prefers_db_backed_service_when_configured(monkeypatch, patched_settings, sample_lab):
    patched_settings.GUACAMOLE_DATABASE_URL = "postgresql+asyncpg://guacuser:guacuser@127.0.0.1:5433/guacdb"
    monkeypatch.setattr("app.routers.labs.get_settings", lambda: patched_settings)
    monkeypatch.setattr("app.services.guacamole_db_service.get_settings", lambda: patched_settings)

    monkeypatch.setattr(
        "app.routers.labs.NodeRuntimeService",
        lambda: SimpleNamespace(
            console_info=lambda _lab_data, _node_id: {
                "name": "alpine-a",
                "console": "telnet",
                "host": "127.0.0.1",
                "port": 2323,
                "url": "/html5/#/client/demo",
            }
        ),
    )

    async def fake_db_console_url(*_args, **_kwargs):
        return "/html5/#/client/db-backed?token=dbtoken"

    monkeypatch.setattr(
        "app.services.guacamole_db_service.GuacamoleDatabaseService.create_console_url",
        fake_db_console_url,
    )

    response = await labs.node_html5(
        "sample.json",
        1,
        current_user=SimpleNamespace(username="admin", html5=True, pod=0),
    )
    assert response.status_code == 307
    assert response.headers["location"] == "/html5/#/client/db-backed?token=dbtoken"


def test_guacamole_connection_name_ignores_transient_host_and_port():
    service = GuacamoleDatabaseService.__new__(GuacamoleDatabaseService)
    user = SimpleNamespace(username="admin")

    key_one = service._connection_name(user, "lab-1:1:telnet", "telnet")
    key_two = service._connection_name(user, "lab-1:1:telnet", "telnet")
    key_other = service._connection_name(user, "lab-1:2:telnet", "telnet")

    assert key_one == key_two
    assert key_one != key_other


def test_guacamole_connection_name_remains_unique_for_long_keys():
    service = GuacamoleDatabaseService.__new__(GuacamoleDatabaseService)
    user = SimpleNamespace(username="admin")
    prefix = "x" * 150

    key_one = service._connection_name(user, prefix + "alpha", "telnet")
    key_two = service._connection_name(user, prefix + "beta", "telnet")

    assert key_one != key_two


def test_guacamole_db_connection_parameters_include_terminal_font_defaults():
    service = GuacamoleDatabaseService.__new__(GuacamoleDatabaseService)
    service.settings = SimpleNamespace(
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
    )

    params = service._connection_parameters(host="host.docker.internal", port=2323, protocol="telnet")

    assert params["hostname"] == "host.docker.internal"
    assert params["port"] == "2323"
    assert params["disable-auth"] == "true"
    assert params["font-name"] == "Roboto Mono"
    assert params["font-size"] == "10"


def test_html5_connection_parameters_include_terminal_font_defaults(monkeypatch):
    monkeypatch.setattr(
        "app.services.html5_service.get_settings",
        lambda: SimpleNamespace(
            GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
            GUACAMOLE_TERMINAL_FONT_SIZE=10,
        ),
    )

    params = Html5SessionService._connection_parameters("host.docker.internal", 2323, "telnet")

    assert params["hostname"] == "host.docker.internal"
    assert params["port"] == "2323"
    assert params["font-name"] == "Roboto Mono"
    assert params["font-size"] == "10"


@pytest.mark.asyncio
async def test_guacamole_auth_token_reuses_cached_token_when_valid(monkeypatch, runtime_settings):
    service = GuacamoleDatabaseService.__new__(GuacamoleDatabaseService)
    service.settings = runtime_settings
    service.database_url = runtime_settings.GUACAMOLE_DATABASE_URL

    async def fake_request_auth_token(username: str, password: str) -> str:
        assert username == "admin"
        assert password == "pw"
        return "fresh-token"

    async def fake_token_is_valid(token: str) -> bool:
        return token == "cached-token"

    monkeypatch.setattr(service, "_request_auth_token", fake_request_auth_token)
    monkeypatch.setattr(service, "_token_is_valid", fake_token_is_valid)

    _AUTH_TOKEN_CACHE[(service.settings.GUACAMOLE_INTERNAL_URL.strip(), "admin")] = "cached-token"
    token = await service._auth_token("admin", "pw")

    assert token == "cached-token"


@pytest.mark.asyncio
async def test_guacamole_auth_token_refreshes_when_cached_token_is_invalid(monkeypatch, runtime_settings):
    service = GuacamoleDatabaseService.__new__(GuacamoleDatabaseService)
    service.settings = runtime_settings
    service.database_url = runtime_settings.GUACAMOLE_DATABASE_URL

    calls = []

    async def fake_request_auth_token(username: str, password: str) -> str:
        calls.append((username, password))
        return "fresh-token"

    async def fake_token_is_valid(token: str) -> bool:
        return False

    monkeypatch.setattr(service, "_request_auth_token", fake_request_auth_token)
    monkeypatch.setattr(service, "_token_is_valid", fake_token_is_valid)

    cache_key = (service.settings.GUACAMOLE_INTERNAL_URL.strip(), "admin")
    _AUTH_TOKEN_CACHE[cache_key] = "stale-token"

    token = await service._auth_token("admin", "pw")

    assert token == "fresh-token"
    assert calls == [("admin", "pw")]
    assert _AUTH_TOKEN_CACHE[cache_key] == "fresh-token"


@pytest.mark.asyncio
async def test_start_stop_docker_nodes_attach_to_shared_lab_network(monkeypatch, patched_settings, _us203_instance_id):
    """US-203: containers always start with ``--network=none`` and we drive
    veth setup manually via the privileged helper. No ``docker network
    create`` / ``docker network connect`` calls are made.
    """
    lab_data = {
        "schema": 2,
        "id": "lab-123",
        "meta": {"name": "docker-demo"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
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
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                ],
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
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                ],
            },
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lab-link",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
                "runtime": {"bridge_name": "nove0000n1"},
            }
        },
        "links": [
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 1},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
            {
                "id": "lnk_002",
                "from": {"node_id": 2, "interface_index": 0},
                "to": {"network_id": 1},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
        ],
        "defaults": {"link_style": "orthogonal"},
    }

    recorded_calls: list[list[str]] = []
    containers: dict[str, dict[str, object]] = {}
    helper_mock = _us203_helper_mock(monkeypatch, present_bridges={"nove0000n1"})

    _mock_runtime_binaries(monkeypatch)

    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        recorded_calls.append(cmd)
        if os.path.basename(cmd[0]) != "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        args = cmd[3:] if len(cmd) > 2 and cmd[1] == "--host" else cmd[1:]
        if args[0] == "run":
            container_name = args[args.index("--name") + 1]
            containers[container_name] = {
                "running": True,
                "pid": 2200 + len(containers),
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
    service.start_node(lab_data, 1)
    service.start_node(lab_data, 2)

    # No `docker network create` / `docker network connect` calls fired.
    assert not [c for c in recorded_calls if c[3:5] == ["network", "create"]]
    assert not [c for c in recorded_calls if c[3:5] == ["network", "connect"]]

    # Every `docker run` invocation has `--network none` and no aliasing.
    run_calls = [c for c in recorded_calls if c[3] == "run"]
    assert len(run_calls) == 2
    for call in run_calls:
        assert call[call.index("--network") + 1] == "none"
        assert "--network-alias" not in call

    # Manual veth path was driven by the helper for every interface.
    assert helper_mock["veth_pair_add"], "manual veth path not invoked"
    assert helper_mock["link_master"], "veth host-end was not attached to bridge"
    assert helper_mock["link_set_name_in_netns"], "peer was not renamed inside netns"

    # Renamed names match `eth{interface_index}` exactly (non-negotiable).
    for _pid, _old, new in helper_mock["link_set_name_in_netns"]:
        assert new == "eth0"

    service.stop_node(lab_data, 1)
    service.stop_node(lab_data, 2)

    # Stop path swept the host-end veths.
    swept = {entry["name"] for entry in helper_mock["link_del"] if entry["fn"] == "try_link_del"}
    assert swept, "veth host-ends were not swept on stop"


def test_linux_bridge_identifier_resolves_to_docker_bridge_driver(patched_settings):
    lab_data = {
        "schema": 2,
        "id": "lab-linuxbridge",
        "meta": {"name": "linux-bridge-test"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1,
                "name": "alpine-x",
                "type": "docker",
                "image": "nova-ve-alpine-telnet:latest",
                "console": "telnet",
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                ],
            }
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lab-link",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
            }
        },
        "links": [
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 1},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            }
        ],
        "defaults": {"link_style": "orthogonal"},
    }

    service = NodeRuntimeService()
    specs = service._docker_network_specs(lab_data, lab_data["nodes"]["1"])

    assert len(specs) == 1
    spec = specs[0]
    assert spec["id"] == 1
    assert spec["name"] == "nova-ve-lablinuxbrid-net1"
    assert spec["internal"] is False


def test_no_legacy_bridge_type_string_in_v2_lab(patched_settings):
    service = NodeRuntimeService()

    node_types = ["qemu", "docker", "iol", "dynamips"]
    networks = {
        "1": {
            "id": 1,
            "name": "net1",
            "type": "linux_bridge",
            "visibility": True,
            "implicit": False,
            "config": {},
        },
        "2": {
            "id": 2,
            "name": "net2",
            "type": "ovs_bridge",
            "visibility": True,
            "implicit": False,
            "config": {},
        },
        "3": {
            "id": 3,
            "name": "net3",
            "type": "internal",
            "visibility": True,
            "implicit": False,
            "config": {},
        },
    }

    def _walk_for_bridge_type(obj, path=""):
        """Recursively walk dict/list; fail if any value is exactly 'bridge' in a 'type' key."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "type" and v == "bridge":
                    raise AssertionError(
                        f"Legacy network type 'bridge' found at path '{path}.{k}': {obj}"
                    )
                _walk_for_bridge_type(v, path=f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _walk_for_bridge_type(item, path=f"{path}[{i}]")

    for node_type in node_types:
        lab_data = {
            "schema": 2,
            "id": f"lab-{node_type}",
            "meta": {"name": f"{node_type}-lab"},
            "viewport": {"x": 0, "y": 0, "zoom": 1.0},
            "nodes": {
                "1": {
                    "id": 1,
                    "name": f"{node_type}-node",
                    "type": node_type,
                    "image": "test-image",
                    "console": "telnet",
                    "cpu": 1,
                    "ram": 512,
                    "ethernet": 1,
                    "interfaces": [
                        {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                    ],
                }
            },
            "networks": networks,
            "links": [
                {
                    "id": "lnk_001",
                    "from": {"node_id": 1, "interface_index": 0},
                    "to": {"network_id": 1},
                    "style_override": None,
                    "label": "",
                    "color": "",
                    "width": "1",
                    "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
                }
            ],
            "defaults": {"link_style": "orthogonal"},
        }

        _walk_for_bridge_type(lab_data)

        if node_type == "docker":
            specs = service._docker_network_specs(lab_data, lab_data["nodes"]["1"])
            _walk_for_bridge_type(specs)


@pytest.mark.asyncio
async def test_docker_runtime_stays_running_without_host_pid_visibility(monkeypatch, patched_settings, _us203_instance_id):
    """Even when ``psutil`` cannot see the container PID (rootless docker
    on macOS), the manual veth setup runs to completion and the runtime is
    persisted as ``status == 2``.
    """
    lab_data = {
        "schema": 2,
        "id": "lab-123",
        "meta": {"name": "docker-demo"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
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
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None}
                ],
            }
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lab-link",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
                "runtime": {"bridge_name": "nove0000n1"},
            }
        },
        "links": [
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 1},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            }
        ],
        "defaults": {"link_style": "orthogonal"},
    }

    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        args = cmd[3:] if len(cmd) > 2 and cmd[1] == "--host" else cmd[1:]
        if args[0] == "run":
            return SimpleNamespace(returncode=0, stdout="container-id\n", stderr="")
        if args[:2] == ["inspect", "-f"] and args[2] == "{{.State.Pid}}":
            return SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        if args[:2] == ["inspect", "-f"] and args[2] == "{{.State.Running}}":
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if args[0] in {"stop", "rm"}:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _mock_runtime_binaries(monkeypatch)
    _us203_helper_mock(monkeypatch, present_bridges={"nove0000n1"})
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", fake_run)

    def fake_psutil_process(pid):
        if pid == 4321:
            raise psutil.Error("pid not visible on host")
        return SimpleNamespace(create_time=lambda: 1.0)

    monkeypatch.setattr("app.services.node_runtime_service.psutil.Process", fake_psutil_process)

    service = NodeRuntimeService()
    service.start_node(lab_data, 1)
    enriched = service.enrich_node("lab-123", 1, lab_data["nodes"]["1"])
    assert enriched["status"] == 2


# ---------------------------------------------------------------------------
# US-072: read_live_mac coverage
# ---------------------------------------------------------------------------

_QEMU_LAB_DATA = {
    "schema": 2,
    "id": "lab-live-qemu",
    "meta": {"name": "qemu-live"},
    "viewport": {"x": 0, "y": 0, "zoom": 1.0},
    "nodes": {
        "1": {
            "id": 1,
            "name": "router-1",
            "type": "qemu",
            "image": "router-image",
            "console": "telnet",
            "cpu": 1,
            "ram": 1024,
            "ethernet": 1,
            "interfaces": [
                {
                    "index": 0,
                    "name": "Gi1",
                    "planned_mac": "aa:bb:cc:dd:ee:01",
                    "port_position": None,
                    "network_id": 0,
                }
            ],
        }
    },
    "networks": {},
    "links": [],
    "defaults": {"link_style": "orthogonal"},
}


def _seed_qemu_runtime(service, qmp_socket: str | None = "/tmp/fake-qmp.sock") -> None:
    runtime = {
        "lab_id": "lab-live-qemu",
        "node_id": 1,
        "kind": "qemu",
        "name": "router-1",
        "console": "telnet",
        "console_port": 12345,
        "pid": 9999,
        "pid_create_time": 1.0,
        "work_dir": "/tmp/live-mac-qemu",
        "stdout_log": "/tmp/live-mac-qemu/stdout.log",
        "stderr_log": "/tmp/live-mac-qemu/stderr.log",
        "qmp_socket": qmp_socket,
        "command": [],
        "started_at": 1.0,
    }
    service._registry[service._key("lab-live-qemu", 1)] = runtime


def test_qemu_live_mac_read_confirmed(patched_settings):
    service = NodeRuntimeService()
    _seed_qemu_runtime(service)
    service._qmp_client = lambda socket_path, command: {
        "return": [{"name": "net0", "main-mac": "AA:BB:CC:DD:EE:01"}]
    }

    result = service.read_live_mac("lab-live-qemu", 1, 0, lab_data=_QEMU_LAB_DATA)

    assert result["state"] == "confirmed"
    assert result["runtime_type"] == "qemu"
    assert result["planned_mac"] == "aa:bb:cc:dd:ee:01"
    assert result["live_mac"].lower() == "aa:bb:cc:dd:ee:01"


def test_qemu_live_mac_read_mismatch(patched_settings):
    service = NodeRuntimeService()
    _seed_qemu_runtime(service)
    service._qmp_client = lambda socket_path, command: {
        "return": [{"name": "net0", "main-mac": "11:22:33:44:55:66"}]
    }

    result = service.read_live_mac("lab-live-qemu", 1, 0, lab_data=_QEMU_LAB_DATA)

    assert result["state"] == "mismatch"
    assert result["live_mac"].lower() == "11:22:33:44:55:66"
    assert result["reason"]


def test_qemu_live_mac_read_unavailable_when_qmp_socket_missing(patched_settings):
    service = NodeRuntimeService()
    _seed_qemu_runtime(service, qmp_socket=None)

    def _raises(socket_path, command):
        raise FileNotFoundError("qmp socket not found")

    service._qmp_client = _raises

    result = service.read_live_mac("lab-live-qemu", 1, 0, lab_data=_QEMU_LAB_DATA)

    assert result["state"] == "unavailable"
    assert "qmp" in (result["reason"] or "").lower()


_DOCKER_LAB_DATA = {
    "schema": 2,
    "id": "lab-live-docker",
    "meta": {"name": "docker-live"},
    "viewport": {"x": 0, "y": 0, "zoom": 1.0},
    "nodes": {
        "5": {
            "id": 5,
            "name": "alpine-c",
            "type": "docker",
            "image": "nova-ve-alpine-telnet:latest",
            "console": "telnet",
            "cpu": 1,
            "ram": 256,
            "ethernet": 2,
            "interfaces": [
                {
                    "index": 0,
                    "name": "eth0",
                    "planned_mac": "02:42:ac:11:00:05",
                    "port_position": None,
                    "network_id": 1,
                },
                {
                    "index": 1,
                    "name": "eth1",
                    "planned_mac": "02:42:ac:22:00:05",
                    "port_position": None,
                    "network_id": 2,
                },
            ],
        }
    },
    "networks": {
        "1": {"id": 1, "name": "lab-link", "type": "linux_bridge", "visibility": True, "implicit": False, "config": {}},
        "2": {"id": 2, "name": "lab-mgmt", "type": "linux_bridge", "visibility": True, "implicit": False, "config": {}},
    },
    "links": [
        {
            "id": "lnk-d-1",
            "from": {"node_id": 5, "interface_index": 0},
            "to": {"network_id": 1},
            "style_override": None,
            "label": "",
            "color": "",
            "width": "1",
            "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
        },
        {
            "id": "lnk-d-2",
            "from": {"node_id": 5, "interface_index": 1},
            "to": {"network_id": 2},
            "style_override": None,
            "label": "",
            "color": "",
            "width": "1",
            "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
        },
    ],
    "defaults": {"link_style": "orthogonal"},
}


def _seed_docker_runtime(service) -> None:
    runtime = {
        "lab_id": "lab-live-docker",
        "node_id": 5,
        "kind": "docker",
        "name": "alpine-c",
        "console": "telnet",
        "console_port": 22000,
        "container_name": "nova-ve-lablivedocke-5",
        "container_id": "deadbeef",
        "pid": 8888,
        "pid_create_time": 1.0,
        "work_dir": "/tmp/live-mac-docker",
        "stdout_log": "/tmp/live-mac-docker/stdout.log",
        "stderr_log": "/tmp/live-mac-docker/stderr.log",
        "command": [],
        "network_names": ["nova-ve-lablivedocke-net1", "nova-ve-lablivedocke-net2"],
        "started_at": 1.0,
    }
    service._registry[service._key("lab-live-docker", 5)] = runtime


def test_docker_live_mac_read_confirmed(monkeypatch, patched_settings):
    """US-205b: live MAC read via netns sysfs returns confirmed match."""
    _mock_runtime_binaries(monkeypatch)
    service = NodeRuntimeService()
    _seed_docker_runtime(service)

    # PID is resolved fresh via ``docker inspect {{.State.Pid}}`` on every read.
    monkeypatch.setattr(service, "_docker_container_pid", lambda _b, _n: 8888)

    calls: list[tuple[int, str]] = []

    def _read_iface_mac(pid: int, iface: str) -> str:
        calls.append((pid, iface))
        return "02:42:ac:11:00:05\n"

    service._read_iface_mac = _read_iface_mac

    result = service.read_live_mac("lab-live-docker", 5, 0, lab_data=_DOCKER_LAB_DATA)

    assert result["state"] == "confirmed"
    assert result["runtime_type"] == "docker"
    assert result["live_mac"].lower() == "02:42:ac:11:00:05"
    # MAC is read inside the container's netns for eth{interface_index}.
    assert calls == [(8888, "eth0")]


def test_docker_live_mac_read_mismatch_multi_network(monkeypatch, patched_settings):
    """US-205b: per-iface netns read returns the correct NIC's MAC across multi-NIC nodes."""
    _mock_runtime_binaries(monkeypatch)
    service = NodeRuntimeService()
    _seed_docker_runtime(service)

    monkeypatch.setattr(service, "_docker_container_pid", lambda _b, _n: 8888)

    macs_by_iface = {
        "eth0": "02:42:ac:11:00:05",
        "eth1": "ff:ff:ff:ff:ff:ff",
    }

    def _read_iface_mac(pid: int, iface: str) -> str:
        return macs_by_iface[iface]

    service._read_iface_mac = _read_iface_mac

    result = service.read_live_mac("lab-live-docker", 5, 1, lab_data=_DOCKER_LAB_DATA)

    assert result["state"] == "mismatch"
    assert result["live_mac"].lower() == "ff:ff:ff:ff:ff:ff"
    assert result["planned_mac"] == "02:42:ac:22:00:05"


def test_docker_live_mac_read_unavailable_when_helper_fails(monkeypatch, patched_settings):
    """US-205b: helper failure (e.g. nsenter EPERM) degrades to unavailable, never raises."""
    _mock_runtime_binaries(monkeypatch)
    service = NodeRuntimeService()
    _seed_docker_runtime(service)

    monkeypatch.setattr(service, "_docker_container_pid", lambda _b, _n: 8888)

    def _read_iface_mac(pid: int, iface: str) -> str:
        raise RuntimeError("nsenter: failed to enter netns")

    service._read_iface_mac = _read_iface_mac

    result = service.read_live_mac("lab-live-docker", 5, 0, lab_data=_DOCKER_LAB_DATA)

    assert result["state"] == "unavailable"
    assert "read-iface-mac" in (result["reason"] or "")


def test_docker_live_mac_does_not_parse_network_settings(monkeypatch, patched_settings):
    """US-205b regression: post-US-207 ``--network=none`` containers have an
    empty ``.NetworkSettings.Networks`` so live-MAC MUST come from sysfs via
    nsenter — parsing ``.NetworkSettings`` is forbidden because it would
    always return null and silently break Wave 4's MAC-mismatch detection.

    Note: ``docker inspect {{.State.Pid}}`` is now called on every read to
    refresh the container's kernel PID (codex critic finding); only the
    ``.NetworkSettings``-style inspect (``_docker_inspect`` hook) is forbidden.
    """
    _mock_runtime_binaries(monkeypatch)
    service = NodeRuntimeService()
    _seed_docker_runtime(service)

    monkeypatch.setattr(service, "_docker_container_pid", lambda _b, _n: 8888)

    def _inspect_must_not_be_called(*_args, **_kwargs):
        raise AssertionError(
            "_read_docker_live_mac must not parse .NetworkSettings post-US-207 "
            "(containers run with --network=none and .NetworkSettings.Networks "
            "is permanently empty)"
        )

    service._docker_inspect = _inspect_must_not_be_called

    def _read_iface_mac(pid: int, iface: str) -> str:
        assert pid == 8888
        assert iface == "eth0"
        return "02:42:ac:11:00:05"

    service._read_iface_mac = _read_iface_mac

    result = service.read_live_mac("lab-live-docker", 5, 0, lab_data=_DOCKER_LAB_DATA)

    assert result["state"] == "confirmed"
    assert result["live_mac"].lower() == "02:42:ac:11:00:05"


def test_docker_live_mac_unavailable_when_runtime_pid_missing(monkeypatch, patched_settings):
    """US-205b: when ``docker inspect {{.State.Pid}}`` returns 0 (container
    exited / never ran) the live-MAC read must degrade to unavailable rather
    than passing pid=0 to nsenter.

    Codex critic follow-up: this also exercises the stale-PID guard on the
    fresh-inspect path (``_docker_container_pid`` returns 0 — read_iface_mac
    must NOT be invoked).
    """
    _mock_runtime_binaries(monkeypatch)
    service = NodeRuntimeService()
    _seed_docker_runtime(service)

    # Freshly-inspected PID is 0 (container exited / not running).
    monkeypatch.setattr(service, "_docker_container_pid", lambda _b, _n: 0)

    def _read_iface_mac_must_not_be_called(*_args, **_kwargs):
        raise AssertionError(
            "read_iface_mac must not be invoked when fresh inspect returns pid=0"
        )

    service._read_iface_mac = _read_iface_mac_must_not_be_called

    result = service.read_live_mac("lab-live-docker", 5, 0, lab_data=_DOCKER_LAB_DATA)

    assert result["state"] == "unavailable"
    assert "pid" in (result["reason"] or "").lower()


def test_docker_live_mac_unavailable_when_helper_returns_empty(monkeypatch, patched_settings):
    """US-205b: empty stdout from helper (e.g. iface not yet attached) -> unavailable."""
    _mock_runtime_binaries(monkeypatch)
    service = NodeRuntimeService()
    _seed_docker_runtime(service)

    monkeypatch.setattr(service, "_docker_container_pid", lambda _b, _n: 8888)

    service._read_iface_mac = lambda pid, iface: "   \n"

    result = service.read_live_mac("lab-live-docker", 5, 0, lab_data=_DOCKER_LAB_DATA)

    assert result["state"] == "unavailable"
    assert "empty" in (result["reason"] or "").lower()


def test_docker_live_mac_resolves_pid_freshly_on_every_read(monkeypatch, patched_settings):
    """US-205b codex critic finding: PID must be resolved via fresh
    ``docker inspect {{.State.Pid}}`` on every read — never from
    ``runtime["pid"]`` (captured once at start time).

    Docker restart policies are explicitly supported (see ``start_node`` step
    1333 — ``--restart unless-stopped``), so the kernel PID can change after
    ``docker restart`` / crash-restart / PID rollover.  Using the cached
    runtime PID would either read a stale netns or — worst case — inspect an
    unrelated process's namespace if the PID was reused.

    Two consecutive reads against the same runtime simulate a container
    restart by returning a different PID from ``_docker_container_pid``: each
    read must invoke the helper again and pass the latest PID to
    ``read_iface_mac``.
    """
    _mock_runtime_binaries(monkeypatch)
    service = NodeRuntimeService()
    _seed_docker_runtime(service)

    pid_inspect_calls: list[tuple[str, str]] = []
    pid_sequence = iter([8888, 9999])

    def _fake_pid(docker_binary: str, container_name: str) -> int:
        pid_inspect_calls.append((docker_binary, container_name))
        return next(pid_sequence)

    monkeypatch.setattr(service, "_docker_container_pid", _fake_pid)

    iface_calls: list[tuple[int, str]] = []

    def _read_iface_mac(pid: int, iface: str) -> str:
        iface_calls.append((pid, iface))
        return "02:42:ac:11:00:05"

    service._read_iface_mac = _read_iface_mac

    # First read: fresh inspect returns pid=8888.
    r1 = service.read_live_mac("lab-live-docker", 5, 0, lab_data=_DOCKER_LAB_DATA)
    # Second read: fresh inspect returns pid=9999 (simulated restart).
    r2 = service.read_live_mac("lab-live-docker", 5, 0, lab_data=_DOCKER_LAB_DATA)

    assert r1["state"] == "confirmed"
    assert r2["state"] == "confirmed"

    # Each read called _docker_container_pid exactly once (no caching).
    assert len(pid_inspect_calls) == 2, pid_inspect_calls
    # Container name plumbs through unchanged.
    assert all(call[1] == "nova-ve-lablivedocke-5" for call in pid_inspect_calls)

    # Each read passed the latest fresh PID to read_iface_mac (no stale value).
    assert iface_calls == [(8888, "eth0"), (9999, "eth0")]

    # The cached runtime["pid"] (8888) was never reused — proven by the
    # second read using 9999 even though _seed_docker_runtime stored 8888.
    assert service._registry[service._key("lab-live-docker", 5)]["pid"] == 8888


@pytest.mark.skipif(
    os.geteuid() != 0 or subprocess.run(
        ["sh", "-c", "command -v docker"], capture_output=True
    ).returncode != 0,
    reason=(
        "privileged integration test: requires root (for nsenter into a "
        "container netns) and a working docker CLI on PATH"
    ),
)
def test_docker_live_mac_privileged_against_real_network_none_container(
    monkeypatch, patched_settings, tmp_path
):
    """US-205b plan requirement (network-runtime-wiring.md:342): start a
    real ``alpine`` container with ``--network=none`` and exercise
    ``_read_docker_live_mac`` against it end-to-end.

    Containers started with ``--network=none`` have no ``eth0`` (the only
    in-netns iface is ``lo``), so the legitimate post-US-207 outcome is that
    the function returns ``unavailable`` rather than a stale string.  We
    cross-check by entering the container netns directly with ``nsenter``
    and confirming there is no ``/sys/class/net/eth0/address`` to read.
    """
    container_name = f"nova-ve-test-us205b-{secrets.token_hex(4)}"
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--network=none",
            "--name", container_name,
            "alpine", "sleep", "60",
        ],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"docker run failed (no docker daemon?): {run.stderr.strip()}")
    container_id = run.stdout.strip()
    try:
        pid_inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Pid}}", container_name],
            capture_output=True, text=True, check=True,
        )
        real_pid = int(pid_inspect.stdout.strip())
        assert real_pid > 0

        # Cross-check via nsenter: --network=none means no eth0.
        nsenter = subprocess.run(
            ["nsenter", "-t", str(real_pid), "-n",
             "cat", "/sys/class/net/eth0/address"],
            capture_output=True, text=True,
        )
        assert nsenter.returncode != 0, (
            f"--network=none container unexpectedly has eth0: {nsenter.stdout!r}"
        )

        service = NodeRuntimeService()
        # Seed a runtime with a deliberately stale (and wrong) cached pid to
        # prove the function does NOT read it; only fresh docker inspect.
        runtime = {
            "lab_id": "lab-live-real",
            "node_id": 7,
            "kind": "docker",
            "name": "alpine-real",
            "console": "telnet",
            "console_port": 22000,
            "container_name": container_name,
            "container_id": container_id,
            "pid": 1,  # deliberately wrong / stale — must be ignored
            "pid_create_time": 0.0,
            "work_dir": str(tmp_path),
            "stdout_log": str(tmp_path / "stdout.log"),
            "stderr_log": str(tmp_path / "stderr.log"),
            "command": [],
            "network_names": [],
            "started_at": 1.0,
        }
        service._registry[service._key("lab-live-real", 7)] = runtime

        lab_data = {
            "schema": 2,
            "id": "lab-live-real",
            "meta": {"name": "live-real"},
            "viewport": {"x": 0, "y": 0, "zoom": 1.0},
            "nodes": {
                "7": {
                    "id": 7,
                    "name": "alpine-real",
                    "type": "docker",
                    "image": "alpine",
                    "console": "telnet",
                    "cpu": 1,
                    "ram": 256,
                    "ethernet": 1,
                    "interfaces": [
                        {
                            "index": 0,
                            "name": "eth0",
                            "planned_mac": "02:42:ac:11:00:07",
                            "port_position": None,
                            "network_id": 1,
                        },
                    ],
                }
            },
            "networks": {
                "1": {"id": 1, "name": "lab-link", "type": "linux_bridge",
                      "visibility": True, "implicit": False, "config": {}},
            },
            "links": [],
            "defaults": {"link_style": "orthogonal"},
        }

        result = service.read_live_mac("lab-live-real", 7, 0, lab_data=lab_data)

        # --network=none -> no eth0 inside the netns -> function must report
        # unavailable, not a stale or fabricated MAC.
        assert result["state"] == "unavailable", result
        assert result["live_mac"] is None, result
        assert result["runtime_type"] == "docker"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, text=True,
        )


def test_iol_and_dynamips_return_unavailable(patched_settings):
    service = NodeRuntimeService()

    for runtime_type in ("iol", "dynamips"):
        lab_data = {
            "schema": 2,
            "id": f"lab-{runtime_type}",
            "meta": {"name": runtime_type},
            "viewport": {"x": 0, "y": 0, "zoom": 1.0},
            "nodes": {
                "9": {
                    "id": 9,
                    "name": f"{runtime_type}-node",
                    "type": runtime_type,
                    "image": "ignored",
                    "console": "telnet",
                    "cpu": 1,
                    "ram": 256,
                    "ethernet": 1,
                    "interfaces": [
                        {
                            "index": 0,
                            "name": "e0/0",
                            "planned_mac": "aa:bb:cc:dd:ee:09",
                            "port_position": None,
                            "network_id": 0,
                        }
                    ],
                }
            },
            "networks": {},
            "links": [],
            "defaults": {"link_style": "orthogonal"},
        }

        result = service.read_live_mac(f"lab-{runtime_type}", 9, 0, lab_data=lab_data)
        assert result["state"] == "unavailable"
        assert result["runtime_type"] == runtime_type
        assert "not implemented" in (result["reason"] or "").lower()


# ---------------------------------------------------------------------------
# US-204: hot-attach Docker on links[] create
# ---------------------------------------------------------------------------


def _us204_lab_data() -> dict:
    """Two-network lab: alpine-a starts with one interface on net 1; the
    hot-attach tests add a second interface on net 2.

    Bridge names are derived from ``host_net.bridge_name`` so they match
    what the runtime computes under the active ``_us203_instance_id``
    fixture (instance_id=test-instance-203 → blake2b → 16-bit suffix).
    """
    from app.services import host_net as host_net_module

    bridge_one = host_net_module.bridge_name("lab-204", 1)
    bridge_two = host_net_module.bridge_name("lab-204", 2)
    return {
        "schema": 2,
        "id": "lab-204",
        "meta": {"name": "us204"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1,
                "name": "alpine-a",
                "type": "docker",
                "image": "nova-ve-alpine-telnet:latest",
                "console": "telnet",
                "cpu": 1,
                "ram": 256,
                "ethernet": 2,
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None},
                    {"index": 1, "name": "eth1", "planned_mac": None, "port_position": None},
                ],
            }
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lab-link",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
                "runtime": {"bridge_name": bridge_one},
            },
            "2": {
                "id": 2,
                "name": "lab-mgmt",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
                "runtime": {"bridge_name": bridge_two},
            },
        },
        "links": [
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 1},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            }
        ],
        "defaults": {"link_style": "orthogonal"},
    }


def _us204_docker_run_factory(containers: dict[str, dict[str, object]]):
    """Returns a fake ``subprocess.run`` that mimics the docker daemon:
    handles ``docker run``, ``docker inspect``, ``docker stop``, ``docker rm``.

    All four hot-attach tests start one container then mutate that container
    map across calls.
    """

    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        if os.path.basename(cmd[0]) != "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        args = cmd[3:] if len(cmd) > 2 and cmd[1] == "--host" else cmd[1:]
        if args[0] == "run":
            container_name = args[args.index("--name") + 1]
            containers[container_name] = {
                "running": True,
                "pid": 7000 + len(containers),
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
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def _us204_start_alpine_a(
    monkeypatch,
    patched_settings,
    *,
    include_net2_bridge: bool = True,
):
    """Boot alpine-a (node 1) via the US-203 path so we have a live runtime
    record to hot-attach to. Returns ``(service, lab_data, helper_calls,
    containers, recorded_calls)``.

    ``include_net2_bridge`` toggles whether net 2's bridge is reported as
    present on the host — used by the bridge-missing test to drive the
    pre-flight rejection branch.
    """
    from app.services import host_net as host_net_module

    lab_data = _us204_lab_data()
    bridge_one = host_net_module.bridge_name("lab-204", 1)
    bridge_two = host_net_module.bridge_name("lab-204", 2)
    present_bridges = {bridge_one}
    if include_net2_bridge:
        present_bridges.add(bridge_two)

    containers: dict[str, dict[str, object]] = {}
    helper_calls = _us203_helper_mock(monkeypatch, present_bridges=present_bridges)

    _mock_runtime_binaries(monkeypatch)
    recorded_calls: list[list[str]] = []

    docker_fake = _us204_docker_run_factory(containers)

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        recorded_calls.append(cmd)
        return docker_fake(cmd, capture_output=capture_output, text=text, **kwargs)

    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(create_time=lambda: float(pid)),
    )

    service = NodeRuntimeService()
    service.start_node(lab_data, 1)
    # Reset helper-call recording so tests only see hot-attach activity.
    for key in helper_calls:
        helper_calls[key].clear()
    return service, lab_data, helper_calls, containers, recorded_calls


def test_us204_hot_attach_docker_happy_path(monkeypatch, patched_settings, _us203_instance_id):
    """Hot-attaching a new link to a running container creates a veth pair,
    masters the host end on the bridge, moves the peer into the container's
    netns, and renames it to ``eth{interface_index}``. The runtime record
    is updated so stop-time cleanup sweeps the new host-end veth.
    """
    from app.services import host_net as host_net_module

    service, lab_data, helper_calls, _containers, _recorded = _us204_start_alpine_a(
        monkeypatch, patched_settings
    )
    bridge_two = host_net_module.bridge_name("lab-204", 2)

    attachment = service.attach_docker_interface(
        "lab-204", 1, network_id=2, interface_index=1
    )

    # The 6-step sequence ran (same one used by initial-attach in US-203).
    assert helper_calls["veth_pair_add"], "veth pair was not created"
    host_end, peer_end = helper_calls["veth_pair_add"][0]
    assert host_end.endswith("h"), f"host-end suffix is wrong: {host_end!r}"
    assert peer_end.endswith("p"), f"peer-end suffix is wrong: {peer_end!r}"
    assert helper_calls["link_master"] == [(host_end, bridge_two)]
    assert helper_calls["link_up"] == [host_end]
    assert len(helper_calls["link_netns"]) == 1
    assert helper_calls["link_netns"][0][0] == peer_end
    assert helper_calls["link_set_name_in_netns"] == [
        (helper_calls["link_netns"][0][1], peer_end, "eth1"),
    ]
    assert helper_calls["addr_up_in_netns"] == [
        (helper_calls["link_netns"][0][1], "eth1"),
    ]

    # Returned attachment record carries the new host_end + bridge.
    assert attachment["interface_index"] == 1
    assert attachment["network_id"] == 2
    assert attachment["bridge_name"] == bridge_two
    assert attachment["host_end"] == host_end

    # The runtime record now lists both attachments and both host-ends so
    # stop-time cleanup will sweep them.
    runtime = service._runtime_record("lab-204", 1)
    assert runtime is not None
    indices = sorted(int(a["interface_index"]) for a in runtime["interface_attachments"])
    assert indices == [0, 1]
    assert host_end in runtime["veth_host_ends"]


def test_us204_hot_attach_rejects_when_container_stopped(monkeypatch, patched_settings, _us203_instance_id):
    """Hot-attach must reject when the target container is not running —
    no runtime record means no container to attach into.
    """
    service, lab_data, helper_calls, containers, _recorded = _us204_start_alpine_a(
        monkeypatch, patched_settings
    )

    # Stop the container so its runtime record is purged.
    service.stop_node(lab_data, 1)
    helper_calls["veth_pair_add"].clear()

    from app.services.node_runtime_service import NodeRuntimeError

    with pytest.raises(NodeRuntimeError) as exc_info:
        service.attach_docker_interface("lab-204", 1, network_id=2, interface_index=1)

    assert "not running" in str(exc_info.value).lower() or "no runtime" in str(exc_info.value).lower()
    # No helper-side state was touched.
    assert not helper_calls["veth_pair_add"]
    assert not helper_calls["link_master"]


def test_us204_hot_attach_rejects_when_bridge_missing(monkeypatch, patched_settings, _us203_instance_id):
    """When the target network's bridge is not yet provisioned on the host,
    hot-attach must surface a typed ``NodeRuntimeError`` instead of failing
    deeper in the helper sequence.
    """
    from app.services import host_net as host_net_module

    # Only network 1's bridge is present; we'll attempt to attach to
    # network 2 — its bridge is missing.
    service, _lab_data, helper_calls, _containers, _recorded = _us204_start_alpine_a(
        monkeypatch, patched_settings, include_net2_bridge=False
    )
    bridge_two = host_net_module.bridge_name("lab-204", 2)

    from app.services.node_runtime_service import NodeRuntimeError

    with pytest.raises(NodeRuntimeError) as exc_info:
        service.attach_docker_interface("lab-204", 1, network_id=2, interface_index=1)

    assert bridge_two in str(exc_info.value)
    assert "not present" in str(exc_info.value).lower()
    # No veth was created — the pre-flight bridge check must run BEFORE
    # any helper call.
    assert not helper_calls["veth_pair_add"]


def test_us204_hot_attach_rolls_back_on_helper_failure(monkeypatch, patched_settings, _us203_instance_id):
    """If a helper step fails mid-sequence (e.g. ``link_set_name_in_netns``
    blows up), the partially-created host-end veth is swept and the runtime
    record is NOT mutated to include the failed attachment.
    """
    service, _lab_data, helper_calls, _containers, _recorded = _us204_start_alpine_a(
        monkeypatch, patched_settings
    )

    from app.services import host_net as host_net_module

    def fake_link_set_name_in_netns(_pid, _old, _new):
        raise host_net_module.HostNetEINVAL(
            "kernel rejected rename", returncode=1, stderr="kernel rejected"
        )

    monkeypatch.setattr(host_net_module, "link_set_name_in_netns", fake_link_set_name_in_netns)

    with pytest.raises(host_net_module.HostNetEINVAL):
        service.attach_docker_interface("lab-204", 1, network_id=2, interface_index=1)

    # The partially-created host-end veth was swept by ``try_link_del``.
    swept = {entry["name"] for entry in helper_calls["link_del"] if entry["fn"] == "try_link_del"}
    expected_host_end = host_net_module.veth_host_name("lab-204", 1, 1)
    assert expected_host_end in swept, (
        f"rollback did not sweep the partial host-end veth {expected_host_end!r}; "
        f"swept set was {swept!r}"
    )

    # The runtime record only carries the original (interface_index=0)
    # attachment; the failed (interface_index=1) attachment was NOT added.
    runtime = service._runtime_record("lab-204", 1)
    assert runtime is not None
    indices = sorted(int(a["interface_index"]) for a in runtime["interface_attachments"])
    assert indices == [0]
    assert expected_host_end not in runtime.get("veth_host_ends", [])


def test_us204_hot_attach_symmetric_with_initial_attach(monkeypatch, patched_settings, _us203_instance_id):
    """Both initial and hot attachments must drive the SAME 6-step sequence
    against the privileged helper, with identical host-end naming. This is
    the plan's "no special-case for the first NIC" property.
    """
    service, _lab_data, helper_calls, _containers, _recorded = _us204_start_alpine_a(
        monkeypatch, patched_settings
    )

    # Capture the initial-attach sequence (interface 0 was attached during
    # ``_us204_start_alpine_a`` BEFORE the helper-call lists were cleared,
    # so we re-run an attach on a fresh interface and compare verb shapes).
    service.attach_docker_interface("lab-204", 1, network_id=2, interface_index=1)

    # Verb counts MUST match the initial-attach 6-step sequence exactly.
    assert len(helper_calls["veth_pair_add"]) == 1
    assert len(helper_calls["link_master"]) == 1
    assert len(helper_calls["link_up"]) == 1
    assert len(helper_calls["link_netns"]) == 1
    assert len(helper_calls["link_set_name_in_netns"]) == 1
    assert len(helper_calls["addr_up_in_netns"]) == 1

    # Naming pattern is the same regex shape as initial attach (see US-203
    # ``_us203_helper_mock`` assertions): host-end suffix ``h``, peer-end
    # suffix ``p``, renamed in-netns to ``eth{interface_index}``.
    host_end, peer_end = helper_calls["veth_pair_add"][0]
    from app.services import host_net as host_net_module

    assert host_end == host_net_module.veth_host_name("lab-204", 1, 1)
    assert peer_end == host_net_module.veth_peer_name("lab-204", 1, 1)
    assert host_end.endswith("h")
    assert peer_end.endswith("p")

    # Verb invocation ORDER matters: the 6-step sequence must run in the
    # documented order. The mock records each verb in invocation order, so
    # we can reconstruct the order by comparing list lengths captured
    # before / after each conceptual step. The simpler check below asserts
    # the rename uses the renamed-into-netns peer name.
    assert helper_calls["link_set_name_in_netns"][0][2] == "eth1"


@pytest.mark.asyncio
async def test_live_mac_endpoint_publishes_ws_event(monkeypatch, patched_settings):
    lab_path = patched_settings.LABS_DIR / "live-mac.json"
    lab_data = {
        "schema": 2,
        "id": "lab-live-qemu",
        "meta": {"name": "qemu-live"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1,
                "name": "router-1",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "cpu": 1,
                "ram": 1024,
                "ethernet": 1,
                "interfaces": [
                    {
                        "index": 0,
                        "name": "Gi1",
                        "planned_mac": "aa:bb:cc:dd:ee:01",
                        "port_position": None,
                        "network_id": 0,
                    }
                ],
            }
        },
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }
    lab_path.write_text(json.dumps(lab_data))

    captured: list[dict] = []

    async def _capture_publish(lab_id, event_type, payload, rev=""):
        captured.append({"lab_id": lab_id, "type": event_type, "payload": payload, "rev": rev})
        return SimpleNamespace(seq=1, type=event_type, rev=rev, payload=payload)

    monkeypatch.setattr("app.routers.labs.ws_hub.publish", _capture_publish)

    fixed_result = {
        "state": "confirmed",
        "planned_mac": "aa:bb:cc:dd:ee:01",
        "live_mac": "aa:bb:cc:dd:ee:01",
        "runtime_type": "qemu",
        "reason": None,
    }
    monkeypatch.setattr(
        "app.routers.labs.NodeRuntimeService",
        lambda: SimpleNamespace(read_live_mac=lambda *_args, **_kwargs: fixed_result),
    )

    response = await labs.get_interface_live_mac(
        "live-mac.json",
        1,
        0,
        current_user=SimpleNamespace(username="admin"),
    )

    assert response == fixed_result
    assert len(captured) == 1
    event = captured[0]
    assert event["type"] == "interface_live_mac"
    assert event["payload"]["node_id"] == 1
    assert event["payload"]["interface_index"] == 0
    assert event["payload"]["state"] == "confirmed"
    assert event["payload"]["planned_mac"] == "aa:bb:cc:dd:ee:01"
    assert event["payload"]["live_mac"] == "aa:bb:cc:dd:ee:01"


# ---------------------------------------------------------------------------
# US-302 — per-NIC TAP + bridge attach at QEMU start (replaces SLIRP)
# ---------------------------------------------------------------------------


def _us302_helper_mock(
    monkeypatch,
    *,
    present_bridges: set[str] | None = None,
    fail_on: tuple[str, str] | None = None,
):
    """Capture every privileged-helper call relevant to US-302.

    ``fail_on`` is ``(verb, name)`` — when the verb is invoked with the
    matching iface name, the fake raises ``HostNetEINVAL`` so the start
    path can exercise its rollback.
    """
    from app.services import host_net

    if present_bridges is None:
        present_bridges = set()

    calls: dict[str, list] = {
        "bridge_exists": [],
        "tap_exists": [],
        "tap_add": [],
        "tap_del": [],
        "link_master": [],
        "link_up": [],
        "try_link_del": [],
    }

    def _maybe_fail(verb: str, name: str) -> None:
        if fail_on and fail_on == (verb, name):
            raise host_net.HostNetEINVAL(
                f"injected failure: {verb} {name}",
                returncode=1,
                stderr="injected",
            )

    def fake_bridge_exists(name: str) -> bool:
        calls["bridge_exists"].append(name)
        return name in present_bridges

    def fake_tap_exists(name: str) -> bool:
        calls["tap_exists"].append(name)
        return False  # normal case: TAP does not pre-exist

    def fake_tap_add(name: str) -> None:
        calls["tap_add"].append(name)
        _maybe_fail("tap_add", name)

    def fake_tap_del(name: str) -> None:
        calls["tap_del"].append(name)

    def fake_link_master(iface: str, bridge: str) -> None:
        calls["link_master"].append((iface, bridge))
        _maybe_fail("link_master", iface)

    def fake_link_up(iface: str) -> None:
        calls["link_up"].append(iface)
        _maybe_fail("link_up", iface)

    def fake_try_link_del(name: str) -> None:
        calls["try_link_del"].append(name)

    monkeypatch.setattr(host_net, "bridge_exists", fake_bridge_exists)
    monkeypatch.setattr(host_net, "tap_exists", fake_tap_exists)
    monkeypatch.setattr(host_net, "tap_add", fake_tap_add)
    monkeypatch.setattr(host_net, "tap_del", fake_tap_del)
    monkeypatch.setattr(host_net, "link_master", fake_link_master)
    monkeypatch.setattr(host_net, "link_up", fake_link_up)
    monkeypatch.setattr(host_net, "try_link_del", fake_try_link_del)
    return calls


def _us302_names() -> dict:
    """Compute the canonical bridge/tap names for the US-302 fixture."""
    from app.services import host_net

    lab_id = "lab-302"
    return {
        "lab_id": lab_id,
        "bridge1": host_net.bridge_name(lab_id, 1),
        "bridge2": host_net.bridge_name(lab_id, 2),
        "tap0": host_net.tap_name(lab_id, 1, 0),
        "tap1": host_net.tap_name(lab_id, 1, 1),
    }


def _us302_lab_data() -> dict:
    """Two-NIC QEMU node attached to a single linux_bridge network."""
    names = _us302_names()
    return {
        "schema": 2,
        "id": names["lab_id"],
        "meta": {"name": "us302"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1,
                "name": "vyos-1",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "cpu": 1,
                "ram": 1024,
                "ethernet": 2,
                "firstmac": "50:00:00:01:00:00",
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None},
                    {"index": 1, "name": "eth1", "planned_mac": None, "port_position": None},
                ],
            }
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lab-link",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
                "runtime": {"bridge_name": names["bridge1"]},
            },
            "2": {
                "id": 2,
                "name": "mgmt",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
                "runtime": {"bridge_name": names["bridge2"]},
            },
        },
        "links": [
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 1},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
            {
                "id": "lnk_002",
                "from": {"node_id": 1, "interface_index": 1},
                "to": {"network_id": 2},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            },
        ],
        "defaults": {"link_style": "orthogonal"},
    }


def _setup_us302_qemu_runtime(monkeypatch, runtime_settings):
    """Stage shared mocks for the QEMU start path: image dir, popen, psutil."""
    image_dir = runtime_settings.IMAGES_DIR / "qemu" / "router-image"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "hda.qcow2").write_text("base-image")

    _mock_runtime_binaries(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run",
        _fake_subprocess_run_factory([]),
    )
    recorded_popen: list[dict] = []

    def fake_popen(cmd, cwd=None, stdin=None, stdout=None, stderr=None, start_new_session=None):
        recorded_popen.append({"cmd": list(cmd), "cwd": str(cwd)})
        return _FakeProcess(7777)

    monkeypatch.setattr("app.services.node_runtime_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "app.services.node_runtime_service.time.sleep", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(
            create_time=lambda: 222.0,
            cpu_percent=lambda interval=0.0: 1.0,
            memory_info=lambda: SimpleNamespace(rss=1024),
            wait=lambda timeout=5: None,
            is_running=lambda: True,
            status=lambda: "sleeping",
        ),
    )
    return recorded_popen


def test_us302_qemu_start_creates_per_nic_tap_and_no_slirp(
    monkeypatch, patched_settings, _us203_instance_id
):
    """Each declared interface gets its own TAP, attached to the right
    bridge. No `-netdev user` (SLIRP) is in the QEMU argv when every NIC
    has a network attachment.
    """
    names = _us302_names()
    bridges = {names["bridge1"], names["bridge2"]}
    helper = _us302_helper_mock(monkeypatch, present_bridges=bridges)
    recorded_popen = _setup_us302_qemu_runtime(monkeypatch, patched_settings)

    service = NodeRuntimeService()
    runtime = service.start_node(_us302_lab_data(), 1)

    cmd = recorded_popen[0]["cmd"]
    netdev_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-netdev"]

    # Two NICs => two -netdev tap entries, no -netdev user.
    tap_args = [a for a in netdev_args if a.startswith("tap,")]
    user_args = [a for a in netdev_args if a.startswith("user,")]
    assert len(tap_args) == 2, f"expected 2 -netdev tap entries, got {netdev_args}"
    assert user_args == [], f"SLIRP must not be used when bridges exist: {user_args}"

    # Each tap arg references the canonical TAP name and the no-script flags.
    for arg in tap_args:
        assert "ifname=" in arg
        assert ",script=no" in arg
        assert ",downscript=no" in arg

    # Helper sequence: bridge_exists pre-flight, tap_add → link_master → link_up.
    assert set(helper["bridge_exists"]) == bridges
    assert len(helper["tap_add"]) == 2
    assert len(helper["link_master"]) == 2
    assert len(helper["link_up"]) == 2
    # link_master pairs each TAP with its declared bridge.
    masters = dict(helper["link_master"])
    assert set(masters.values()) == bridges

    # Runtime record carries the TAP names so stop-path can sweep them.
    assert len(runtime["tap_names"]) == 2
    assert all(name.startswith("nve") for name in runtime["tap_names"])
    assert len(runtime["interface_attachments"]) == 2


def test_us302_qemu_start_aborts_when_bridge_missing(
    monkeypatch, patched_settings, _us203_instance_id
):
    """Pre-flight bridge presence check raises before QEMU spawns."""
    helper = _us302_helper_mock(monkeypatch, present_bridges=set())
    recorded_popen = _setup_us302_qemu_runtime(monkeypatch, patched_settings)

    service = NodeRuntimeService()
    with pytest.raises(Exception) as excinfo:
        service.start_node(_us302_lab_data(), 1)

    assert "bridge" in str(excinfo.value).lower()
    # QEMU never started.
    assert recorded_popen == []
    # No TAP work happened.
    assert helper["tap_add"] == []
    assert helper["link_master"] == []


def test_us302_qemu_start_rolls_back_taps_on_helper_failure(
    monkeypatch, patched_settings, _us203_instance_id
):
    """A mid-loop helper failure sweeps already-created TAPs.

    With ``link_master`` failing on the second NIC, the first TAP must
    be deleted (try_link_del), the second TAP appended via tap_add must
    also be deleted, QEMU must NOT have spawned, and the runtime PID
    registry must stay empty (no register call survives rollback).
    """
    from app.services import runtime_pids

    names = _us302_names()
    bridges = {names["bridge1"], names["bridge2"]}
    helper = _us302_helper_mock(
        monkeypatch,
        present_bridges=bridges,
        fail_on=("link_master", names["tap1"]),
    )
    recorded_popen = _setup_us302_qemu_runtime(monkeypatch, patched_settings)

    service = NodeRuntimeService()
    with pytest.raises(Exception):
        service.start_node(_us302_lab_data(), 1)

    # Both TAPs got an add attempt — first succeeded, second failed at link_master.
    assert helper["tap_add"] == [names["tap0"], names["tap1"]]
    # Both TAPs are swept on rollback.
    assert set(helper["try_link_del"]) == {names["tap0"], names["tap1"]}
    # QEMU never spawned and no PID was registered.
    assert recorded_popen == []
    assert runtime_pids.list_entries() == []


def test_us302_qemu_argv_contains_netdev_tap_not_user(
    monkeypatch, patched_settings, _us203_instance_id
):
    """Sanity assertion isolated from helper choreography: the assembled
    QEMU argv uses ``-netdev tap,ifname=...`` and never ``-netdev user``
    when networks resolve."""
    names = _us302_names()
    _us302_helper_mock(
        monkeypatch, present_bridges={names["bridge1"], names["bridge2"]}
    )
    recorded_popen = _setup_us302_qemu_runtime(monkeypatch, patched_settings)

    service = NodeRuntimeService()
    service.start_node(_us302_lab_data(), 1)

    flat = " ".join(recorded_popen[0]["cmd"])
    assert "-netdev tap," in flat
    assert ",ifname=nve" in flat
    assert "-netdev user," not in flat


def test_us302_rollback_symmetry_with_docker_start(
    monkeypatch, patched_settings, _us203_instance_id
):
    """When the registry write fails AFTER QEMU spawned, we kill the
    process and sweep TAPs (mirrors docker step-4 rollback)."""
    from app.services import runtime_pids

    names = _us302_names()
    _us302_helper_mock(
        monkeypatch, present_bridges={names["bridge1"], names["bridge2"]}
    )
    recorded_popen = _setup_us302_qemu_runtime(monkeypatch, patched_settings)

    killed: list = []
    monkeypatch.setattr(
        "app.services.node_runtime_service.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    def boom(*_a, **_kw):
        raise RuntimeError("registry write blocked")

    monkeypatch.setattr(runtime_pids, "register", boom)

    service = NodeRuntimeService()
    with pytest.raises(Exception) as excinfo:
        service.start_node(_us302_lab_data(), 1)

    assert "registry" in str(excinfo.value).lower()
    # QEMU spawned exactly once and was killed during rollback.
    assert len(recorded_popen) == 1
    assert killed and killed[0][0] == 7777
    # No leaked entry in the registry (register raised, unregister noop).
    assert runtime_pids.list_entries() == []


def test_us302_qemu_stop_sweeps_tap_names(
    monkeypatch, patched_settings, _us203_instance_id
):
    """Stop path must call ``host_net.try_link_del`` for every TAP the
    start path created (parity with US-203 veth sweep)."""
    names = _us302_names()
    helper = _us302_helper_mock(
        monkeypatch, present_bridges={names["bridge1"], names["bridge2"]}
    )
    _setup_us302_qemu_runtime(monkeypatch, patched_settings)
    monkeypatch.setattr(
        "app.services.node_runtime_service.os.killpg",
        lambda pid, sig: None,
    )

    service = NodeRuntimeService()
    lab_data = _us302_lab_data()
    runtime = service.start_node(lab_data, 1)
    expected_taps = set(runtime["tap_names"])
    assert expected_taps  # sanity

    helper["try_link_del"].clear()
    service.stop_node(lab_data, 1)
    assert set(helper["try_link_del"]) == expected_taps


# ---------------------------------------------------------------------------
# Boot-time NIC link state: connected NICs default link=on, unconnected NICs
# get link=off so the guest does not see phantom carrier on hubport-backed
# devices.
# ---------------------------------------------------------------------------


def _qemu_link_state_lab() -> dict:
    """4-NIC QEMU node with only ``Gi3`` (interface_index=2) linked."""
    from app.services import host_net

    lab_id = "lab-link-state"
    bridge1 = host_net.bridge_name(lab_id, 1)
    return {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": "link-state"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1,
                "name": "vyos-1",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "cpu": 1,
                "ram": 1024,
                "ethernet": 4,
                "firstmac": "50:00:00:01:00:00",
                "interfaces": [
                    {"index": 0, "name": "Gi1"},
                    {"index": 1, "name": "Gi2"},
                    {"index": 2, "name": "Gi3"},
                    {"index": 3, "name": "Gi4"},
                ],
            }
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lan",
                "type": "linux_bridge",
                "visibility": True,
                "implicit": False,
                "config": {},
                "runtime": {"bridge_name": bridge1},
            }
        },
        "links": [
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 2},
                "to": {"network_id": 1},
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
            }
        ],
        "defaults": {"link_style": "orthogonal"},
    }


def _device_args_by_index(cmd: list[str]) -> dict[int, str]:
    """Return ``{index: device_arg}`` for every ``-device <nic_model>,...``
    that targets ``netdev=net{index}``."""
    out: dict[int, str] = {}
    for i, tok in enumerate(cmd):
        if tok != "-device":
            continue
        arg = cmd[i + 1] if i + 1 < len(cmd) else ""
        if "netdev=net" not in arg:
            continue
        for piece in arg.split(","):
            if piece.startswith("netdev=net"):
                try:
                    idx = int(piece[len("netdev=net"):])
                except ValueError:
                    continue
                out[idx] = arg
                break
    return out


def _patch_qmp_capture(monkeypatch) -> list[tuple]:
    """Patch ``NodeRuntimeService._qmp_command`` to capture set_link calls."""
    captured: list[tuple] = []

    def fake_qmp(self, socket_path, command, arguments=None):
        captured.append((command, arguments))
        return {"return": {}}

    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService._qmp_command",
        fake_qmp,
    )
    return captured


def test_qemu_unconnected_nics_get_set_link_off_after_spawn(
    monkeypatch, patched_settings, _us203_instance_id
):
    """Connected NIC keeps the QEMU default (no link=); unconnected NICs
    have ``set_link {up: False}`` issued via QMP right after spawn.
    Boot-time argv must NOT carry ``link=off`` because e1000 (and most
    NIC models) reject it as a device property."""
    from app.services import host_net

    lab_id = "lab-link-state"
    bridge = host_net.bridge_name(lab_id, 1)
    _us302_helper_mock(monkeypatch, present_bridges={bridge})
    recorded_popen = _setup_us302_qemu_runtime(monkeypatch, patched_settings)
    set_link_calls = _patch_qmp_capture(monkeypatch)

    NodeRuntimeService().start_node(_qemu_link_state_lab(), 1)
    cmd = recorded_popen[0]["cmd"]
    by_index = _device_args_by_index(cmd)

    assert set(by_index.keys()) == {0, 1, 2, 3}
    # No NIC carries ``link=`` on the device line — that property does
    # not exist on e1000 and would crash QEMU at boot.
    for idx, arg in by_index.items():
        assert "link=" not in arg, f"unexpected link= on idx {idx}: {arg!r}"
    # Sanity: connected NIC is tap-backed, others are hubport.
    flat = " ".join(cmd)
    assert "-netdev tap," in flat
    assert "-netdev hubport," in flat

    # Post-spawn set_link issued for the three unconnected indices only.
    set_link_args = [
        args for cmd_name, args in set_link_calls if cmd_name == "set_link"
    ]
    assert {a["name"] for a in set_link_args} == {"net0", "net1", "net3"}
    assert all(a["up"] is False for a in set_link_args)


def test_qemu_all_unconnected_nics_get_set_link_off_after_spawn(
    monkeypatch, patched_settings, _us203_instance_id
):
    """Sample lab (no networks, no links) — both NICs unconnected →
    set_link issued for net0 and net1 with ``up=False``."""
    _us302_helper_mock(monkeypatch, present_bridges=set())
    recorded_popen = _setup_us302_qemu_runtime(monkeypatch, patched_settings)
    set_link_calls = _patch_qmp_capture(monkeypatch)

    lab_data = {
        "schema": 2,
        "id": "lab-empty",
        "meta": {"name": "empty"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1,
                "name": "router",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "cpu": 1,
                "ram": 512,
                "ethernet": 2,
                "firstmac": "50:00:00:01:00:00",
            }
        },
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }

    NodeRuntimeService().start_node(lab_data, 1)
    by_index = _device_args_by_index(recorded_popen[0]["cmd"])
    assert set(by_index.keys()) == {0, 1}
    for idx, arg in by_index.items():
        assert "link=" not in arg, f"unexpected link= on idx {idx}: {arg!r}"

    set_link_args = [
        args for cmd_name, args in set_link_calls if cmd_name == "set_link"
    ]
    assert {a["name"] for a in set_link_args} == {"net0", "net1"}
    assert all(a["up"] is False for a in set_link_args)
