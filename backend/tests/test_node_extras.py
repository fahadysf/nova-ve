"""Tests for per-type node extras (tracker #48 / sub-issues #52-#56)."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers import labs
from app.schemas.node import NodeBatchCreate, NodeCreate, NodeUpdate
from app.services.lab_service import LabService
from app.services.node_runtime_service import NodeRuntimeService
from app.services.template_service import TemplateService


@pytest.fixture(autouse=True)
def reset_runtime_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture()
def extras_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    for directory in (labs_dir, images_dir, tmp_dir, templates_dir):
        directory.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        TEMPLATES_DIR=templates_dir,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        SESSION_MAX_AGE=14400,
    )


@pytest.fixture()
def patched_extras_settings(monkeypatch, extras_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: extras_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: extras_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: extras_settings)
    return extras_settings


@pytest.fixture()
def populated_templates(patched_extras_settings):
    settings = patched_extras_settings
    _write_text(
        settings.TEMPLATES_DIR / "qemu" / "csr.yml",
        """type: qemu
name: CSR1000v
cpu: 2
ram: 4096
ethernet: 2
console_type: telnet
icon_type: router
cpulimit: 1
extras:
  architecture: x86_64
  qemu_nic: virtio-net-pci
""",
    )
    _write_text(
        settings.TEMPLATES_DIR / "docker" / "docker.yml",
        """type: docker
name: Docker
cpu: 1
ram: 256
ethernet: 1
console_type: telnet
icon_type: server
cpulimit: 1
""",
    )
    _write_text(
        settings.TEMPLATES_DIR / "iol" / "iol.yml",
        """type: iol
name: IOL
cpu: 1
ram: 1024
ethernet: 4
console_type: telnet
icon_type: router
cpulimit: 1
""",
    )
    _write_text(
        settings.TEMPLATES_DIR / "dynamips" / "c7200.yml",
        """type: dynamips
name: C7200
cpu: 1
ram: 512
ethernet: 2
console_type: telnet
icon_type: router
cpulimit: 1
""",
    )

    image_dir = settings.IMAGES_DIR / "qemu" / "csr1000v"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "hda.qcow2").write_text("base")

    iol_image = settings.IMAGES_DIR / "iol" / "iol-i86bi"
    iol_image.mkdir(parents=True, exist_ok=True)
    (iol_image / "i86bi.bin").write_text("iol")

    dynamips_image = settings.IMAGES_DIR / "dynamips" / "c7200-image"
    dynamips_image.mkdir(parents=True, exist_ok=True)
    (dynamips_image / "c7200.image").write_text("dynamips")

    _write_text(
        settings.LABS_DIR / "demo.json",
        json.dumps(
            {
                "id": "lab-extras",
                "meta": {"name": "demo"},
                "nodes": {},
                "networks": {},
                "topology": [],
            }
        ),
    )
    return settings


def _admin():
    return SimpleNamespace(username="admin", role="admin", html5=True)


def test_catalog_surfaces_extras_schema_per_type(populated_templates):
    catalog = TemplateService().build_node_catalog()
    by_type = {(template["type"], template["key"]): template for template in catalog["templates"]}

    qemu_template = by_type[("qemu", "csr")]
    assert qemu_template["defaults"]["extras"]["architecture"] == "x86_64"
    assert qemu_template["defaults"]["extras"]["qemu_nic"] == "virtio-net-pci"
    qemu_keys = {field["key"] for field in qemu_template["extras_schema"]}
    assert {"architecture", "qemu_nic", "qemu_options", "qemu_version", "uuid", "firstmac", "cpulimit"} <= qemu_keys

    docker_template = by_type[("docker", "docker")]
    docker_keys = {field["key"] for field in docker_template["extras_schema"]}
    assert {"cpulimit", "restart_policy", "environment", "extra_args"} <= docker_keys

    iol_template = by_type[("iol", "iol")]
    iol_keys = {field["key"] for field in iol_template["extras_schema"]}
    assert {"serial", "nvram", "config", "idle_pc"} <= iol_keys

    dynamips_template = by_type[("dynamips", "c7200")]
    dynamips_keys = {field["key"] for field in dynamips_template["extras_schema"]}
    assert {"slot0", "slot1", "slot6", "nvram", "npe", "midplane", "idlepc"} <= dynamips_keys


@pytest.mark.asyncio
async def test_create_node_persists_extras_round_trip(populated_templates):
    response = await labs.create_node(
        "demo.json",
        NodeCreate(
            name="csr-1",
            type="qemu",
            template="csr",
            image="csr1000v",
            extras={"qemu_options": "-nographic", "architecture": "aarch64"},
        ),
        current_user=_admin(),
    )
    assert response["code"] == 200
    assert response["data"]["extras"]["qemu_options"] == "-nographic"
    assert response["data"]["extras"]["architecture"] == "aarch64"
    assert response["data"]["extras"]["qemu_nic"] == "virtio-net-pci"

    lab_data = LabService.read_lab_json_static("demo.json")
    persisted = lab_data["nodes"]["1"]
    assert persisted["extras"]["qemu_options"] == "-nographic"


@pytest.mark.asyncio
async def test_node_update_accepts_extras_when_stopped(populated_templates):
    create = await labs.create_node(
        "demo.json",
        NodeCreate(name="csr-1", type="qemu", template="csr", image="csr1000v"),
        current_user=_admin(),
    )
    node_id = create["data"]["id"]

    update = await labs.update_node(
        "demo.json",
        node_id,
        NodeUpdate(extras={"qemu_options": "-enable-kvm", "qemu_nic": "e1000"}),
        current_user=_admin(),
    )
    assert update["code"] == 200
    assert update["data"]["extras"]["qemu_options"] == "-enable-kvm"
    assert update["data"]["extras"]["qemu_nic"] == "e1000"


def _make_qemu_node_lab(populated_templates, extras: dict) -> dict:
    lab_data = {
        "id": "lab-qemu",
        "meta": {"name": "qemu"},
        "nodes": {
            "1": {
                "id": 1,
                "name": "csr-1",
                "type": "qemu",
                "image": "csr1000v",
                "console": "telnet",
                "cpu": 2,
                "ram": 1024,
                "ethernet": 2,
                "firstmac": "50:00:00:01:00:00",
                "extras": extras,
            }
        },
        "networks": {},
        "topology": [],
    }
    (populated_templates.LABS_DIR / "qemu.json").write_text(json.dumps(lab_data))
    return lab_data


def _stub_qemu_subprocess(monkeypatch, recorded):
    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        recorded.append(cmd)
        if os.path.basename(cmd[0]) == "qemu-img":
            Path(cmd[-1]).write_text("overlay")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_popen(cmd, cwd=None, stdin=None, stdout=None, stderr=None, start_new_session=None):
        recorded.append(cmd)
        if stdout is not None:
            stdout.write(b"boot\n")
        return SimpleNamespace(pid=4321, poll=lambda: None)

    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService._resolve_binary",
        staticmethod(lambda binary: binary),
    )
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", fake_run)
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.services.node_runtime_service.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(
            create_time=lambda: 111.0,
            cpu_percent=lambda interval=0.0: 0,
            memory_info=lambda: SimpleNamespace(rss=0),
            wait=lambda timeout=5: None,
            is_running=lambda: True,
            status=lambda: "running",
        ),
    )


@pytest.mark.asyncio
async def test_qemu_runtime_honors_qemu_options_and_nic(monkeypatch, populated_templates):
    _make_qemu_node_lab(
        populated_templates,
        {"qemu_options": "-nographic -enable-kvm", "qemu_nic": "virtio-net-pci"},
    )
    recorded = []
    _stub_qemu_subprocess(monkeypatch, recorded)

    response = await labs.start_node("qemu.json", 1, current_user=_admin())
    assert response["code"] == 200

    qemu_cmd = next(cmd for cmd in recorded if os.path.basename(cmd[0]).startswith("qemu-system"))
    assert "-nographic" in qemu_cmd
    assert "-enable-kvm" in qemu_cmd
    nic_devices = [arg for arg in qemu_cmd if arg.startswith("virtio-net-pci,")]
    assert nic_devices, f"expected virtio-net-pci nic, got {qemu_cmd}"


@pytest.mark.asyncio
async def test_qemu_runtime_uses_arch_specific_binary(monkeypatch, populated_templates):
    _make_qemu_node_lab(populated_templates, {"architecture": "aarch64"})
    recorded = []
    _stub_qemu_subprocess(monkeypatch, recorded)

    response = await labs.start_node("qemu.json", 1, current_user=_admin())
    assert response["code"] == 200
    qemu_cmd = next(cmd for cmd in recorded if "qemu-system" in os.path.basename(cmd[0]))
    assert os.path.basename(qemu_cmd[0]) == "qemu-system-aarch64"


@pytest.mark.asyncio
async def test_qemu_runtime_rejects_invalid_qemu_options(monkeypatch, populated_templates):
    _make_qemu_node_lab(populated_templates, {"qemu_options": '-name "unterminated'})
    recorded = []
    _stub_qemu_subprocess(monkeypatch, recorded)

    response = await labs.start_node("qemu.json", 1, current_user=_admin())
    assert response["code"] == 400
    assert "qemu_options" in response["message"].lower()


def _stub_docker_subprocess(monkeypatch, recorded, containers):
    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        recorded.append(cmd)
        args = cmd[3:] if len(cmd) > 2 and cmd[1] == "--host" else cmd[1:]
        if args[:2] == ["network", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        if args[:2] == ["network", "create"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "run":
            container_name = args[args.index("--name") + 1]
            containers[container_name] = {"running": True, "pid": 1234}
            return SimpleNamespace(returncode=0, stdout=f"{container_name}-cid\n", stderr="")
        if args[:2] == ["inspect", "-f"]:
            template = args[2]
            container = containers.get(args[3])
            if not container:
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            if template == "{{.State.Pid}}":
                return SimpleNamespace(returncode=0, stdout=f"{container['pid']}\n", stderr="")
            if template == "{{.State.Running}}":
                return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if args[0] == "stop":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "rm":
            containers.pop(args[-1], None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService._resolve_binary",
        staticmethod(lambda binary: binary),
    )
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(create_time=lambda: 111.0),
    )


@pytest.mark.asyncio
async def test_docker_runtime_honors_env_restart_and_extra_args(monkeypatch, populated_templates):
    lab_data = {
        "id": "lab-docker",
        "meta": {"name": "docker"},
        "nodes": {
            "1": {
                "id": 1,
                "name": "alpine-a",
                "type": "docker",
                "image": "alpine:latest",
                "console": "telnet",
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "interfaces": [],
                "extras": {
                    "environment": [
                        {"key": "FOO", "value": "bar"},
                        {"key": "BAZ", "value": "qux"},
                    ],
                    "restart_policy": "unless-stopped",
                    "extra_args": "--cap-add=NET_ADMIN",
                },
            }
        },
        "networks": {},
        "topology": [],
    }
    (populated_templates.LABS_DIR / "docker.json").write_text(json.dumps(lab_data))

    recorded: list[list[str]] = []
    containers: dict[str, dict] = {}
    _stub_docker_subprocess(monkeypatch, recorded, containers)

    response = await labs.start_node("docker.json", 1, current_user=_admin())
    assert response["code"] == 200

    run_cmd = next(cmd for cmd in recorded if "run" in cmd and "--name" in cmd)
    assert "--restart" in run_cmd
    assert run_cmd[run_cmd.index("--restart") + 1] == "unless-stopped"

    env_pairs = [run_cmd[idx + 1] for idx, value in enumerate(run_cmd) if value == "-e"]
    assert "FOO=bar" in env_pairs
    assert "BAZ=qux" in env_pairs

    assert "--cap-add=NET_ADMIN" in run_cmd


@pytest.mark.asyncio
async def test_docker_runtime_rejects_invalid_restart_policy(monkeypatch, populated_templates):
    lab_data = {
        "id": "lab-docker",
        "meta": {"name": "docker"},
        "nodes": {
            "1": {
                "id": 1,
                "name": "alpine-a",
                "type": "docker",
                "image": "alpine:latest",
                "console": "telnet",
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "interfaces": [],
                "extras": {"restart_policy": "bogus"},
            }
        },
        "networks": {},
        "topology": [],
    }
    (populated_templates.LABS_DIR / "docker.json").write_text(json.dumps(lab_data))
    recorded: list[list[str]] = []
    containers: dict[str, dict] = {}
    _stub_docker_subprocess(monkeypatch, recorded, containers)

    response = await labs.start_node("docker.json", 1, current_user=_admin())
    assert response["code"] == 400
    assert "restart_policy" in response["message"].lower()


@pytest.mark.asyncio
async def test_iol_node_round_trips_extras(populated_templates):
    response = await labs.create_node(
        "demo.json",
        NodeCreate(
            name="iol-1",
            type="iol",
            template="iol",
            image="iol-i86bi",
            extras={"serial": 2, "nvram": 2048, "config": "Saved"},
        ),
        current_user=_admin(),
    )
    assert response["code"] == 200
    extras = response["data"]["extras"]
    assert extras["serial"] == 2
    assert extras["nvram"] == 2048
    assert extras["config"] == "Saved"


@pytest.mark.asyncio
async def test_dynamips_node_round_trips_slot_extras(populated_templates):
    response = await labs.create_node(
        "demo.json",
        NodeCreate(
            name="r1",
            type="dynamips",
            template="c7200",
            image="c7200-image",
            extras={"slot0": "C7200-IO-2FE", "slot1": "PA-FE-TX", "npe": "npe-g2"},
        ),
        current_user=_admin(),
    )
    assert response["code"] == 200
    extras = response["data"]["extras"]
    assert extras["slot0"] == "C7200-IO-2FE"
    assert extras["slot1"] == "PA-FE-TX"
    assert extras["npe"] == "npe-g2"


@pytest.mark.asyncio
async def test_node_update_blocks_extras_changes_while_running(monkeypatch, populated_templates):
    create = await labs.create_node(
        "demo.json",
        NodeCreate(name="csr-1", type="qemu", template="csr", image="csr1000v"),
        current_user=_admin(),
    )
    node_id = create["data"]["id"]

    monkeypatch.setattr("app.routers.labs._node_is_running", lambda data, node_id: True)

    update = await labs.update_node(
        "demo.json",
        node_id,
        NodeUpdate(extras={"qemu_options": "-nographic"}),
        current_user=_admin(),
    )
    assert update["code"] == 400
    assert "stop the node" in update["message"].lower()
