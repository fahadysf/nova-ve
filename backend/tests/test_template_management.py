from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from starlette.datastructures import UploadFile

from app.routers import labs, listing
from app.schemas.node import NodeBatchCreate, NodeCreate
from app.services.lab_service import LabService
from app.services.template_service import TemplateService


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture()
def template_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    labs_dir.mkdir()
    images_dir.mkdir()
    tmp_dir.mkdir()
    templates_dir.mkdir()
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
def patched_template_settings(monkeypatch, template_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: template_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: template_settings)
    return template_settings


@pytest.fixture()
def prepared_template_data(patched_template_settings):
    _write_text(
        patched_template_settings.TEMPLATES_DIR / "qemu" / "csr.yml",
        """type: qemu
name: Cisco CSR1000v
cpu: 2
ram: 4096
ethernet: 4
console_type: telnet
icon_type: router
cpulimit: 1
""",
    )
    _write_text(
        patched_template_settings.LABS_DIR / "demo.json",
        """{
  "schema": 2,
  "id": "lab-templates",
  "meta": {"name": "demo"},
  "viewport": {"x": 0, "y": 0, "zoom": 1.0},
  "nodes": {},
  "networks": {},
  "links": [],
  "defaults": {"link_style": "orthogonal"}
}""",
    )
    image_dir = patched_template_settings.IMAGES_DIR / "qemu" / "csr1000v"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "hda.qcow2").write_text("image")
    return patched_template_settings


@pytest.mark.asyncio
async def test_list_templates_images_and_upload(prepared_template_data):
    current_user = SimpleNamespace(username="admin", role="admin")

    templates_response = await listing.list_templates("qemu", current_user=current_user)
    assert templates_response["code"] == 200
    assert templates_response["data"]["csr"]["ram"] == 4096

    images_response = await listing.list_images("qemu", "csr", current_user=current_user)
    assert images_response["code"] == 200
    assert "csr1000v" in images_response["data"]

    upload_response = await listing.upload_image(
        "qemu",
        "csr",
        image=UploadFile(filename="disk2.qcow2", file=__import__("io").BytesIO(b"qcow2")),
        image_name="csr1000v-alt",
        current_user=current_user,
    )
    assert upload_response["code"] == 200
    assert upload_response["data"]["image"] == "csr1000v-alt"
    assert (prepared_template_data.IMAGES_DIR / "qemu" / "csr1000v-alt" / "disk2.qcow2").exists()

    catalog = TemplateService().build_node_catalog()
    assert catalog["templates"][0]["defaults"]["icon"] == "Router.png"
    assert "Router.png" in catalog["icon_options"]


@pytest.mark.asyncio
async def test_create_node_uses_template_defaults_and_validates_image(prepared_template_data):
    current_user = SimpleNamespace(username="admin", role="admin")

    create_response = await labs.create_node(
        "demo.json",
        NodeCreate(
            name="csr-edge",
            type="qemu",
            template="csr",
            image="csr1000v",
        ),
        current_user=current_user,
    )
    assert create_response["code"] == 200
    assert create_response["data"]["cpu"] == 2
    assert create_response["data"]["ram"] == 4096
    assert create_response["data"]["ethernet"] == 4
    assert create_response["data"]["icon"] == "Router.png"
    assert create_response["data"]["delay"] == 0

    lab_data = LabService.read_lab_json_static("demo.json")
    assert lab_data["nodes"]["1"]["image"] == "csr1000v"

    missing_image_response = await labs.create_node(
        "demo.json",
        NodeCreate(
            name="csr-missing",
            type="qemu",
            template="csr",
            image="missing-image",
        ),
        current_user=current_user,
    )
    assert missing_image_response["code"] == 400
    assert "not available" in missing_image_response["message"]


@pytest.mark.asyncio
async def test_batch_create_uses_prefix_positions_and_optional_icon(prepared_template_data):
    current_user = SimpleNamespace(username="admin", role="admin")

    create_response = await labs.create_nodes_batch(
        "demo.json",
        NodeBatchCreate(
            name_prefix="csr",
            count=3,
            type="qemu",
            template="csr",
            image="csr1000v",
            left=100,
            top=120,
            icon="Server.png",
        ),
        current_user=current_user,
    )
    assert create_response["code"] == 200
    created = create_response["data"]["nodes"]
    assert [node["name"] for node in created] == ["csr-1", "csr-2", "csr-3"]
    assert created[0]["left"] == 100
    assert created[1]["left"] == 280
    assert created[2]["left"] == 460
    assert all(node["icon"] == "Server.png" for node in created)


@pytest.mark.asyncio
async def test_list_images_includes_local_docker_images(monkeypatch, patched_template_settings):
    _write_text(
        patched_template_settings.TEMPLATES_DIR / "docker" / "docker.yml",
        """type: docker
name: Docker Host
cpu: 1
ram: 1024
ethernet: 1
console_type: telnet
icon_type: server
cpulimit: 1
""",
    )

    def fake_run(cmd, capture_output=False, text=False, check=False, env=None):
        assert "docker" in cmd[0]
        return SimpleNamespace(
            returncode=0,
            stdout="nova-ve-alpine-telnet:latest\npostgres:16-alpine\n<none>:<none>\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    current_user = SimpleNamespace(username="admin", role="admin")
    images_response = await listing.list_images("docker", "docker", current_user=current_user)

    assert images_response["code"] == 200
    assert "nova-ve-alpine-telnet:latest" in images_response["data"]
    assert images_response["data"]["nova-ve-alpine-telnet:latest"]["source"] == "docker"
