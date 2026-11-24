from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import UploadFile

from app.dependencies import get_current_user
from app.main import app
from app.routers import labs, listing
from app.schemas.node import NodeBatchCreate, NodeCreate, NodeFromTemplate, NodeUpdate
from app.services.lab_service import LabService
from app.services.template_service import TemplateService


REPO_ROOT = Path(__file__).resolve().parents[2]


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
async def test_canvas_icon_catalog_serving_and_override(prepared_template_data, monkeypatch):
    settings = prepared_template_data
    settings.USER_TEMPLATES_DIR = settings.TEMPLATES_DIR.parent / "user-templates"
    icons = settings.USER_TEMPLATES_DIR / "icons"
    icons.mkdir(parents=True)
    png = b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAIAAAB7QOjdAAAADUlEQVR4nGP4DwQMDAAR9QP9WWIlXQAAAABJRU5ErkJggg=="
    )
    (icons / "default.png").write_bytes(png)
    (icons / "custom node.png").write_bytes(png)
    (icons / "unsupported.svg").write_text("<svg/>")
    (icons / "linked.png").symlink_to(icons / "default.png")
    template = settings.TEMPLATES_DIR / "qemu" / "csr.yml"
    template.write_text(template.read_text() + "icon: default.png\n")

    current_user = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: current_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/list/icons/custom%20node.png")
        assert response.status_code == 200
        assert response.content == png
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        for filename in ("missing.png", "linked.png", "unsupported.svg"):
            response = await client.get(f"/api/list/icons/{filename}")
            assert response.status_code == 404

    catalog = await labs.node_catalog("demo.json", current_user=current_user)
    assert catalog["data"]["templates"][0]["defaults"]["icon"] == "default.png"
    assert catalog["data"]["icon_assets"] == ["custom node.png", "default.png"]

    created = await labs.create_node(
        "demo.json",
        NodeCreate(name="edge", type="qemu", template="csr", image="csr1000v", icon=""),
        current_user=current_user,
    )
    assert created["data"]["icon"] == ""

    for icon in ("custom node.png", ""):
        updated = await labs.update_node(
            "demo.json", 1, NodeUpdate(icon=icon), current_user=current_user
        )
        assert updated["data"]["icon"] == icon
        lab_data = LabService.read_lab_json_static("demo.json")
        assert lab_data["nodes"]["1"]["icon"] == icon


def test_canvas_icon_rejects_a_symlinked_registry(patched_template_settings, tmp_path):
    settings = patched_template_settings
    settings.USER_TEMPLATES_DIR = tmp_path / "user-templates"
    settings.USER_TEMPLATES_DIR.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.png").touch()
    (settings.USER_TEMPLATES_DIR / "icons").symlink_to(outside, target_is_directory=True)

    service = TemplateService()
    assert service.list_icon_assets() == []
    assert service.icon_asset("outside.png") is None


@pytest.mark.asyncio
async def test_api_node_creation_inherits_omitted_custom_icon(prepared_template_data):
    template = prepared_template_data.TEMPLATES_DIR / "qemu" / "csr.yml"
    template.write_text(template.read_text() + "icon: missing.png\n")
    current_user = SimpleNamespace(username="admin", role="admin")

    created = await labs.create_node(
        "demo.json",
        NodeCreate(name="edge", type="qemu", template="csr", image="csr1000v"),
        current_user=current_user,
    )
    assert created["data"]["icon"] == ""

    batch = await labs.create_nodes_batch(
        "demo.json",
        NodeBatchCreate(type="qemu", template="csr", image="csr1000v"),
        current_user=current_user,
    )
    assert batch["data"]["nodes"][0]["icon"] == ""

    from_template = await labs.create_node_from_template(
        "demo.json",
        NodeFromTemplate(template_type="qemu", template_key="csr", name="template-edge", image="csr1000v"),
        current_user=current_user,
    )
    assert from_template["data"]["icon"] == ""

    saved = LabService.read_lab_json_static("demo.json")
    assert len(saved["nodes"]) == 3
    assert all(node["icon"] == "" for node in saved["nodes"].values())
    catalog = TemplateService().build_node_catalog()
    assert catalog["templates"][0]["defaults"]["icon"] == "missing.png"
    assert "missing.png" not in catalog["icon_assets"]


def test_builtin_paloalto_template_matches_eve_pnetlab_image_layout(
    monkeypatch, template_settings
):
    template_settings.TEMPLATES_DIR = REPO_ROOT / "backend" / "templates"
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: template_settings)

    image_dir = template_settings.IMAGES_DIR / "qemu" / "paloalto-10.1.1"
    image_dir.mkdir(parents=True)
    (image_dir / "virtioa.qcow2").write_text("image")

    service = TemplateService()
    templates = service.list_templates("qemu")
    paloalto = templates["paloalto"]

    assert paloalto["name"] == "PaloAlto"
    assert paloalto["icon"] == "Firewall.png"
    assert paloalto["cpu"] == 4
    assert paloalto["ram"] == 8192
    assert paloalto["ethernet"] == 4
    assert paloalto["console_type"] == "telnet"
    # virtio-net-pci default (was e1000): modern PAN-OS ships virtio
    # drivers and virtio NICs avoid the e1000 device-limit issues.
    assert paloalto["extras"]["qemu_nic"] == "virtio-net-pci"
    assert paloalto["extras"]["qemu_version"] == "2.12.0"
    assert paloalto["capabilities"] == {
        "hotplug": True,
        "max_nics": 25,
        "machine": "q35",
    }
    # Same port order as the EVE-NG template (management first, then the
    # data ports numbered from 1); only the label is the four-letter short
    # form of ``ethernet1/1``.
    assert service.interface_naming("qemu", "paloalto") == {
        "format": "mgmt,e1/{port}"
    }
    assert "paloalto-10.1.1" in service.list_images("qemu", "paloalto")


def test_builtin_vyos_template_defaults_to_virtio_nic(
    monkeypatch, template_settings
):
    """vyos ships virtio drivers; the template pins virtio-net-pci instead
    of relying on the global e1000 fallback. Panorama (same PAN-OS
    family) pins it too."""
    template_settings.TEMPLATES_DIR = REPO_ROOT / "backend" / "templates"
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: template_settings)

    service = TemplateService()
    templates = service.list_templates("qemu")

    assert templates["vyos"]["extras"]["qemu_nic"] == "virtio-net-pci"
    assert templates["panorama"]["extras"]["qemu_nic"] == "virtio-net-pci"


def test_builtin_fortinet_template_matches_fgt_image_aliases(
    monkeypatch, template_settings
):
    template_settings.TEMPLATES_DIR = REPO_ROOT / "backend" / "templates"
    monkeypatch.setattr(
        "app.services.template_service.get_settings", lambda: template_settings
    )

    image_names = [
        "fortinet-FGT-v7.4.3",
        "fortinet-5.2",
        "FortiGate-VM64-KVM-7.4",
        "vendor-FGT-v7.2.8",
        "fortinet-FMG-v7.4",
        "fortinet-FAZ-v7.4",
        "paloalto-10.1.1",
    ]
    for image_name in image_names:
        image_dir = template_settings.IMAGES_DIR / "qemu" / image_name
        image_dir.mkdir(parents=True)
        (image_dir / "virtioa.qcow2").write_text("image")

    service = TemplateService()
    fortinet = service.list_templates("qemu")["fortinet"]
    images = service.list_images("qemu", "fortinet")

    assert fortinet["name"] == "Fortinet FortiGate"
    assert "fortinet-fgt-*" in fortinet["image_aliases"]
    assert set(images) == set(image_names[:4])


def test_builtin_iol_template_matches_imported_i86bi_linux_folders(
    monkeypatch, template_settings
):
    template_settings.TEMPLATES_DIR = REPO_ROOT / "backend" / "templates"
    monkeypatch.setattr(
        "app.services.template_service.get_settings", lambda: template_settings
    )

    image_names = [
        "i86bi_Linux-L3-AdvEnterpriseK9-M2-157-18-May-2018",
        "i86bi-linux-l2-adventerprisek9-15.2",
        "L2-ADVENTERPRISEK9-M-15.2-20150703",
        "L3-ADVENTERPRISEK9-M-15.4-2T",
        "c7200-adventerprisek9",
    ]
    for image_name in image_names:
        image_dir = template_settings.IMAGES_DIR / "iol" / image_name
        image_dir.mkdir(parents=True)
        (image_dir / f"{image_name}.bin").write_text("image")

    service = TemplateService()
    iol = service.list_templates("iol")["iol"]
    images = service.list_images("iol", "iol")

    assert iol["name"] == "IOL"
    assert "l2-adventerprisek9-*" in iol["image_aliases"]
    assert set(images) == set(image_names[:-1])


@pytest.mark.parametrize(
    ("template_key", "matching", "other"),
    [
        ("winserver", ["WinServer-2019"], ["win-11-x64", "backup-winserver-2019"]),
        ("win", ["win-11-x64", "Win10-rs5-ltsc"], ["winserver-2019"]),
        ("vios", ["iosv-159-3", "vios-15.5"], ["iosvl2-2020", "viosl2-15.2"]),
        ("viosl2", ["iosvl2-2020", "viosl2-15.2"], ["iosv-159-3", "vios-15.5"]),
        ("xrv9k", ["iosxrv9000-7-11-1", "xrv9k-24.3"], ["xrv-6.1"]),
        ("xrv", ["xrv-6.1"], ["iosxrv9000-7-11-1", "xrv9k-24.3"]),
        ("csr1000vng", ["csr1000vng-universalk9.16.12"], ["csr1000v-universalk9-15.4"]),
        ("fmc7", ["fmc7-7.7.0"], ["ftd7-FTD-7.7.0", "firepower6-FMC-7.0.0"]),
        ("aruba", ["aruba-VMC_8.4.0"], ["arubacx-10.14", "arubamm-8.12.0"]),
    ],
)
def test_builtin_qemu_image_patterns_match_the_appliance_family(
    monkeypatch, template_settings, template_key, matching, other
):
    template_settings.TEMPLATES_DIR = REPO_ROOT / "backend" / "templates"
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: template_settings)
    for image_name in [*matching, *other]:
        _write_text(template_settings.IMAGES_DIR / "qemu" / image_name / "virtioa.qcow2", "image")

    assert set(TemplateService().list_images("qemu", template_key)) == set(matching)


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
async def test_create_node_applies_template_explicit_interface_names(prepared_template_data):
    template_path = prepared_template_data.TEMPLATES_DIR / "qemu" / "csr.yml"
    with template_path.open("a") as template_file:
        template_file.write("interface_naming:\n  explicit: [management, uplink]\n")

    response = await labs.create_node(
        "demo.json",
        NodeCreate(name="csr", type="qemu", template="csr", image="csr1000v"),
        current_user=SimpleNamespace(username="admin", role="admin"),
    )

    assert response["code"] == 200
    assert [port["name"] for port in response["data"]["interfaces"]] == [
        "management", "uplink", "Gi3", "Gi4"
    ]


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
async def test_list_images_includes_only_marked_docker_images(
    monkeypatch, patched_template_settings
):
    """The node-creation modal only sees images carrying a marker tag.

    Unmarked system images (e.g. ``postgres``) must not appear so the modal
    is not polluted by guacamole/system containers; the only thing that
    qualifies is an image with a ``nova-ve-lab/`` reverse tag.
    """
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

    # Two rows share an image ID — one is the original tag, one is the marker.
    docker_ls_stdout = "\n".join(
        [
            '{"ID":"sha256:a1","Repository":"nova-ve/alpine-telnet","Tag":"latest","Size":"5MB","CreatedAt":"t1"}',
            '{"ID":"sha256:a1","Repository":"nova-ve-lab/nova-ve/alpine-telnet","Tag":"latest","Size":"5MB","CreatedAt":"t1"}',
            # Unmarked image must not surface.
            '{"ID":"sha256:b2","Repository":"postgres","Tag":"16-alpine","Size":"80MB","CreatedAt":"t2"}',
        ]
    )

    def fake_run(cmd, capture_output=False, text=False, check=False, env=None):
        assert "docker" in cmd[0]
        return SimpleNamespace(returncode=0, stdout=docker_ls_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    current_user = SimpleNamespace(username="admin", role="admin")
    images_response = await listing.list_images("docker", "docker", current_user=current_user)

    assert images_response["code"] == 200
    assert "nova-ve/alpine-telnet:latest" in images_response["data"]
    assert images_response["data"]["nova-ve/alpine-telnet:latest"]["source"] == "docker"
    assert "postgres:16-alpine" not in images_response["data"]
