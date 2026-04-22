from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from app.routers import labs, listing
from app.schemas.node import NodeCreate
from app.services.lab_service import LabService


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
console: telnet
icon: Router.png
cpulimit: 1
""",
    )
    _write_text(
        patched_template_settings.LABS_DIR / "demo.json",
        """{
  "id": "lab-templates",
  "meta": {"name": "demo"},
  "nodes": {},
  "networks": {},
  "topology": []
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
