# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Image & template library API tests (#264).

Seeds a temp images/templates/labs tree and exercises the inventory,
checksum verify+cache, reference-guarded delete, and template edit
(shadow-edit for builtin templates) through the admin endpoints.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.dependencies import get_current_admin
from app.services.audit_service import AuditEvent
from app.services.template_service import TemplateService


@pytest.fixture()
def lib_settings(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        USER_TEMPLATES_DIR=tmp_path / "user-templates",
    )
    for path in (
        settings.LABS_DIR,
        settings.IMAGES_DIR,
        settings.TMP_DIR,
        settings.TEMPLATES_DIR,
        settings.USER_TEMPLATES_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.image_library_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    return settings


def _seed_veos(settings):
    """A qemu template with one matching image dir + one non-matching."""
    templates = settings.TEMPLATES_DIR / "qemu"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "veos.yml").write_text(
        "type: qemu\nname: vEOS\ncpu: 1\nram: 2048\nethernet: 4\n"
        "console_type: telnet\nicon_type: router\ncpulimit: 1\n"
        "image_aliases:\n  - veos\n"
    )
    (templates / "vyos.yml").write_text(
        "type: qemu\nname: VyOS\ncpu: 1\nram: 1024\nethernet: 2\n"
        "console_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )
    image_dir = settings.IMAGES_DIR / "qemu" / "veos"
    image_dir.mkdir(parents=True, exist_ok=True)
    content = b"fake-qcow2-bytes"
    (image_dir / "veos-4.31.qcow2").write_bytes(content)
    return image_dir / "veos-4.31.qcow2", hashlib.sha256(content).hexdigest()


def _seed_referencing_lab(settings):
    lab = {
        "schema": 2,
        "id": "arc-lab",
        "meta": {"name": "Arc Lab"},
        "nodes": {
            "1": {
                "id": 1,
                "name": "sw1",
                "type": "qemu",
                "template": "veos",
                "image": "veos",
            }
        },
    }
    (settings.LABS_DIR / "arc-lab.json").write_text(json.dumps(lab))


@pytest_asyncio.fixture()
async def client(lib_settings):
    admin = SimpleNamespace(username="admin", role="admin", html5=True, folder="/")
    app.dependency_overrides[get_current_admin] = lambda: admin
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


def _audited(audit_events, event):
    """Entries of one kind left by the test (#300: the autouse capture)."""
    return [entry for entry in audit_events if entry.event == event]


@pytest.mark.asyncio
async def test_inventory_lists_images_with_size_and_references(client, lib_settings):
    image_file, _sha = _seed_veos(lib_settings)
    _seed_referencing_lab(lib_settings)

    response = await client.get("/api/library/images")

    assert response.status_code == 200
    images = response.json()["data"]["images"]
    veos = next(image for image in images if image["name"] == "veos")
    assert veos["type"] == "qemu"
    assert veos["size"] == image_file.stat().st_size
    assert veos["state"] == "available"
    assert veos["sha256"] is None  # never computed — checksums are lazy
    assert veos["referenced_by"][0]["lab"] == "arc-lab.json"
    assert veos["referenced_by"][0]["node"] == "sw1"


@pytest.mark.asyncio
async def test_same_named_images_keep_references_with_their_runtime(client, lib_settings):
    qemu_file, _ = _seed_veos(lib_settings)
    _seed_referencing_lab(lib_settings)
    iol_dir = lib_settings.IMAGES_DIR / "iol" / "veos"
    iol_dir.mkdir(parents=True)
    (iol_dir / "veos.bin").write_bytes(b"iol-image")
    templates = lib_settings.TEMPLATES_DIR / "iol"
    templates.mkdir()
    (templates / "fixture.yml").write_text(
        "type: iol\nname: IOL fixture\ncpu: 1\nram: 512\nethernet: 4\n"
        "console_type: telnet\nicon_type: router\nimage_aliases: [veos]\n"
    )

    response = await client.get("/api/library/images")
    images = {
        image["type"]: image
        for image in response.json()["data"]["images"]
        if image["name"] == "veos"
    }
    assert [item["lab"] for item in images["qemu"]["referenced_by"]] == ["arc-lab.json"]
    assert images["iol"]["referenced_by"] == []
    assert [(item["type"], item["key"]) for item in images["qemu"]["templates"]] == [("qemu", "veos")]
    assert [(item["type"], item["key"]) for item in images["iol"]["templates"]] == [("iol", "fixture")]

    blocked = await client.delete("/api/library/images/qemu/veos")
    assert blocked.json()["code"] == 409
    deleted = await client.delete("/api/library/images/iol/veos")
    assert deleted.json()["code"] == 200
    assert qemu_file.is_file()
    assert not iol_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", ["iou", ""])
async def test_legacy_reference_still_protects_iol_image(client, lib_settings, node_type):
    iol_dir = lib_settings.IMAGES_DIR / "iol" / "veos"
    iol_dir.mkdir(parents=True)
    (iol_dir / "veos.bin").write_bytes(b"iol-image")
    lab = {"nodes": {"1": {"id": 1, "image": "veos"}}}
    if node_type:
        lab["nodes"]["1"]["type"] = node_type
    (lib_settings.LABS_DIR / "legacy.json").write_text(json.dumps(lab))

    response = await client.delete("/api/library/images/iol/veos")

    assert response.json()["code"] == 409
    assert response.json()["data"]["references"][0]["lab"] == "legacy.json"
    assert iol_dir.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locator",
    ["shared-directory", "legacy-backing", "absolute-file", "file-link", "cross-file", "chained-directory"],
)
async def test_delete_protects_the_resolved_image_file(client, lib_settings, locator):
    iol_dir = lib_settings.IMAGES_DIR / "iol" / "veos"
    iol_dir.mkdir(parents=True)
    if locator == "chained-directory":
        media_dir = lib_settings.IMAGES_DIR.parent / "other-storage"
        media_dir.mkdir()
        disk = media_dir / "hda.qcow2"
        intermediary = iol_dir / "alias"
        intermediary.symlink_to(media_dir, target_is_directory=True)
        qemu_dir = lib_settings.IMAGES_DIR / "qemu"
        qemu_dir.mkdir()
        (qemu_dir / "veos").symlink_to(intermediary, target_is_directory=True)
        node = {"type": "qemu", "image": "veos"}
    elif locator == "cross-file":
        disk = iol_dir / "actual.qcow2"
        alias_dir = lib_settings.IMAGES_DIR / "qemu" / "veos"
        alias_dir.mkdir(parents=True)
        (alias_dir / "hda.qcow2").symlink_to(disk)
        node = {"type": "qemu", "image": "veos"}
    elif locator in {"shared-directory", "legacy-backing"}:
        disk = iol_dir / "hda.qcow2"
        (lib_settings.IMAGES_DIR / "veos").symlink_to(iol_dir, target_is_directory=True)
        node = {"type": "qemu", "image": "veos"}
        if locator == "legacy-backing":
            _seed_veos(lib_settings)
            preferred = lib_settings.IMAGES_DIR / "qemu" / "veos" / "hda.qcow2"
            preferred.write_bytes(b"new-image")
    else:
        disk = iol_dir / "veos.bin"
        node = {"type": "iou", "image": str(disk)}
    if locator == "file-link":
        media = lib_settings.IMAGES_DIR.parent / "original.bin"
        media.write_bytes(b"in-use-image")
        disk.symlink_to(media)
    else:
        disk.write_bytes(b"in-use-image")
    (lib_settings.LABS_DIR / "saved.json").write_text(json.dumps({"nodes": {"1": node}}))

    images = (await client.get("/api/library/images")).json()["data"]["images"]
    image = next(image for image in images if image["type"] == "iol")
    assert [ref["lab"] for ref in image["referenced_by"]] == ["saved.json"]
    response = await client.delete("/api/library/images/iol/veos")
    assert response.json()["code"] == 409
    assert disk.read_bytes() == b"in-use-image"


@pytest.mark.asyncio
@pytest.mark.parametrize("image_type, suffix", [("qemu", "qcow2"), ("iol", "bin")])
async def test_cyclic_disk_symlink_does_not_break_inventory(client, lib_settings, image_type, suffix):
    image_dir = lib_settings.IMAGES_DIR / image_type / "veos"
    image_dir.mkdir(parents=True)
    filename = f"disk.{suffix}"
    (image_dir / filename).symlink_to(filename)
    lab = {"nodes": {"1": {"name": "fixture", "type": image_type, "image": "veos"}}}
    (lib_settings.LABS_DIR / "saved.json").write_text(json.dumps(lab))

    images = (await client.get("/api/library/images")).json()["data"]["images"]
    image = next(image for image in images if image["type"] == image_type)
    assert image["state"] == "missing"
    assert [reference["node"] for reference in image["referenced_by"]] == ["fixture"]


@pytest.mark.asyncio
async def test_templates_list_with_matches_and_missing_state(client, lib_settings):
    _seed_veos(lib_settings)

    response = await client.get("/api/library/templates")

    assert response.status_code == 200
    templates = response.json()["data"]["templates"]
    veos = next(t for t in templates if t["key"] == "veos")
    assert [image["name"] for image in veos["images"]] == ["veos"]
    vyos = next(t for t in templates if t["key"] == "vyos")
    assert vyos["images"] == []  # explicit missing-image state


@pytest.mark.asyncio
@pytest.mark.parametrize("second_kind", ["flat", "directory"])
async def test_ambiguous_image_names_do_not_verify_or_delete(client, lib_settings, audit_events, second_kind):
    root = lib_settings.IMAGES_DIR / "qemu"
    root.mkdir(parents=True)
    first = root / "fixture.qcow2"
    first.write_bytes(b"first disk")
    if second_kind == "flat":
        second = root / "fixture.iso"
    else:
        (root / "fixture").mkdir()
        second = root / "fixture" / "hda.qcow2"
    second.write_bytes(b"second disk")

    verify = await client.post("/api/library/images/qemu/fixture/verify")
    assert verify.json()["code"] == 409
    assert "Rename a conflicting file or folder" in verify.json()["message"]
    assert not (lib_settings.TMP_DIR / "image-checksums.json").exists()

    delete = await client.delete("/api/library/images/qemu/fixture")
    assert delete.json()["code"] == 409
    assert delete.json()["message"] == verify.json()["message"]
    assert first.read_bytes() == b"first disk"
    assert second.read_bytes() == b"second disk"
    assert _audited(audit_events, "image.delete") == []

    entry = second if second_kind == "flat" else second.parent
    renamed = root / ("rescue.iso" if second_kind == "flat" else "rescue")
    entry.rename(renamed)
    verified = await client.post("/api/library/images/qemu/fixture/verify")
    assert verified.json()["code"] == 200
    assert verified.json()["data"]["file"] == first.name
    deleted = await client.delete("/api/library/images/qemu/rescue")
    assert deleted.json()["code"] == 200
    assert not renamed.exists()
    assert first.read_bytes() == b"first disk"


@pytest.mark.asyncio
async def test_verify_checksum_computes_and_caches(client, lib_settings):
    image_file, expected = _seed_veos(lib_settings)

    response = await client.post("/api/library/images/qemu/veos/verify")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sha256"] == expected
    assert data["file"] == "veos-4.31.qcow2"

    # The cached value now flows through the inventory without recompute.
    images_response = await client.get("/api/library/images")
    veos = next(
        image
        for image in images_response.json()["data"]["images"]
        if image["name"] == "veos"
    )
    assert veos["sha256"] == expected


@pytest.mark.asyncio
async def test_cached_checksum_invalidated_by_mtime_change(client, lib_settings):
    image_file, expected = _seed_veos(lib_settings)

    await client.post("/api/library/images/qemu/veos/verify")

    # Rewrite the same content at a later mtime → cache entry stale.
    import os

    stat_before = image_file.stat()
    os.utime(image_file, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 1_000_000))

    images_response = await client.get("/api/library/images")
    veos = next(
        image
        for image in images_response.json()["data"]["images"]
        if image["name"] == "veos"
    )
    assert veos["sha256"] is None


@pytest.mark.asyncio
async def test_delete_referenced_image_is_rejected(client, lib_settings, audit_events):
    _seed_veos(lib_settings)
    _seed_referencing_lab(lib_settings)

    response = await client.delete("/api/library/images/qemu/veos")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 409
    assert payload["data"]["references"][0]["lab"] == "arc-lab.json"
    assert (lib_settings.IMAGES_DIR / "qemu" / "veos").exists()
    # Refused, so as far as the trail is concerned it never happened.
    assert _audited(audit_events, "image.delete") == []


@pytest.mark.asyncio
async def test_delete_unreferenced_image_removes_files(client, lib_settings, audit_events):
    _seed_veos(lib_settings)

    response = await client.delete("/api/library/images/qemu/veos")

    assert response.status_code == 200
    assert response.json()["code"] == 200
    assert not (lib_settings.IMAGES_DIR / "qemu" / "veos").exists()
    assert _audited(audit_events, "image.delete") == [
        AuditEvent(
            event="image.delete",
            actor="admin",
            target_type="image",
            target_id="qemu/veos",
            target_name="veos",
            detail={"image_type": "qemu"},
        )
    ]


@pytest.mark.asyncio
async def test_delete_docker_image_is_rejected(client, lib_settings):
    response = await client.delete("/api/library/images/docker/alpine:3.20")

    assert response.status_code == 200
    assert response.json()["code"] == 400
    assert "docker daemon" in response.json()["message"]


@pytest.mark.asyncio
async def test_patch_user_template_edits_in_place(client, lib_settings, audit_events):
    user_dir = lib_settings.USER_TEMPLATES_DIR
    (user_dir / "custom.yml").write_text(
        "type: qemu\nname: Custom\ncpu: 1\nram: 1024\nethernet: 2\n"
        "console_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )

    response = await client.patch(
        "/api/library/templates/qemu/custom",
        json={"ram": 4096, "name": "Custom X"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ram"] == 4096
    assert data["name"] == "Custom X"
    # Edited in place — no shadow copy.
    assert not (user_dir / "custom.yml").read_text().count("Custom X") == 2
    assert "ram: 4096" in (user_dir / "custom.yml").read_text()
    # #300: the trail carries what the edit changed, before → after.
    (event,) = _audited(audit_events, "template.update")
    assert event.actor == "admin"
    assert event.target_id == "qemu/custom"
    assert event.target_name == "Custom X"
    assert event.detail == {
        "template_type": "qemu",
        "changes": {
            "ram": {"from": 1024, "to": 4096},
            "name": {"from": "Custom", "to": "Custom X"},
        },
    }

    # Saving the very same values again changes nothing — and says nothing.
    audit_events.clear()
    await client.patch("/api/library/templates/qemu/custom", json={"ram": 4096})
    assert _audited(audit_events, "template.update") == []


@pytest.mark.asyncio
async def test_patch_builtin_template_writes_shadow(client, lib_settings, audit_events):
    _seed_veos(lib_settings)  # builtin veos.yml under TEMPLATES_DIR

    response = await client.patch(
        "/api/library/templates/qemu/veos",
        json={"ram": 8192, "image_aliases": ["veos", "arista"]},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ram"] == 8192
    assert data["image_aliases"] == ["veos", "arista"]

    shadow = lib_settings.USER_TEMPLATES_DIR / "veos.yml"
    assert shadow.exists()
    assert "ram: 8192" in shadow.read_text()
    # Builtin file untouched.
    assert "ram: 8192" not in (lib_settings.TEMPLATES_DIR / "qemu" / "veos.yml").read_text()

    (event,) = _audited(audit_events, "template.update")
    assert event.target_id == "qemu/veos"
    assert event.target_name == "vEOS"
    assert event.detail["changes"] == {
        "ram": {"from": 2048, "to": 8192},
        "image_aliases": {"from": ["veos"], "to": ["veos", "arista"]},
    }


@pytest.mark.asyncio
async def test_patch_template_round_trips_runtime_settings(client, lib_settings, audit_events):
    template_path = lib_settings.USER_TEMPLATES_DIR / "qemu" / "appliance.json"
    template_path.parent.mkdir()
    template_path.write_text(json.dumps({
        "name": "Appliance",
        "description": "Original description",
        "ethernet": 2,
        "qemu_nic": "rtl8139",
        "extras": {"qemu_nic": "e1000", "custom_value": "preserve"},
        "capabilities": {"machine": "q35", "hotplug": True, "max_nics": 8},
    }))
    endpoint = "/api/library/templates/qemu/appliance"
    response = await client.patch(endpoint, json={
        "description": "Updated appliance",
        "icon_type": "switch",
        "cpulimit": 0,
        "ethernet": 3,
        "interface_naming": {"explicit": ["management", "uplink", "downlink"]},
        "capabilities": {"max_nics": 12},
        "extras": {"qemu_nic": "virtio-net-pci", "disk_bus": "sata"},
    })

    assert response.json()["code"] == 200
    info = (await client.get("/api/library/templates")).json()["data"]["templates"][0]
    assert info["description"] == "Updated appliance"
    assert info["icon_type"] == "switch"
    assert info["cpulimit"] == 0
    assert info["interface_naming"] == {"explicit": ["management", "uplink", "downlink"]}
    assert info["extras"] == {"disk_bus": "sata", "qemu_nic": "virtio-net-pci"}
    assert "qemu_nic" in {field["key"] for field in info["extras_schema"]}

    service = TemplateService()
    defaults = service.build_node_catalog()["templates"][0]
    assert defaults["capabilities"] == {"machine": "q35", "hotplug": True, "max_nics": 12}
    assert defaults["defaults"]["icon"] == "Switch.png"
    assert defaults["defaults"]["ethernet"] == 3
    assert defaults["defaults"]["cpulimit"] == 0
    assert defaults["defaults"]["extras"]["qemu_nic"] == "virtio-net-pci"
    assert defaults["defaults"]["extras"]["custom_value"] == "preserve"
    (updated,) = _audited(audit_events, "template.update")
    assert updated.detail["changed_engine_options"] == ["disk_bus", "qemu_nic"]
    assert "extras" not in updated.detail["changes"]

    before_invalid = template_path.read_bytes()
    invalid = await client.patch(endpoint, json={"ram": 4096, "capabilities": {"machine": "pc"}})
    assert invalid.json()["code"] == 422
    assert template_path.read_bytes() == before_invalid

    cleared = await client.patch(endpoint, json={
        "description": "",
        "interface_naming": None,
        "extras": {"qemu_nic": None},
    })
    assert cleared.json()["code"] == 200
    on_disk = json.loads(template_path.read_text())
    assert "qemu_nic" not in on_disk
    assert on_disk["extras"] == {"disk_bus": "sata", "custom_value": "preserve"}
    assert sorted(lib_settings.USER_TEMPLATES_DIR.rglob("*.yml")) == []
    assert service.template_extras("qemu", "appliance")["qemu_nic"] == "e1000"
    assert service.interface_naming("qemu", "appliance") == {}


@pytest.mark.asyncio
async def test_patch_qemu_machine_agrees_with_versioned_runtime_args(client, lib_settings):
    _seed_veos(lib_settings)
    builtin = lib_settings.TEMPLATES_DIR / "qemu" / "veos.yml"
    with builtin.open("a") as stream:
        stream.write("capabilities:\n  machine: pc\n  hotplug: false\nqemu_options: -machine type=pc-i440fx-8.2,accel=kvm -cpu host\n")
    endpoint = "/api/library/templates/qemu/veos"

    invalid = await client.patch(endpoint, json={"capabilities": {"machine": "q35"}})
    assert invalid.json()["code"] == 422
    assert "Extra QEMU args" in invalid.json()["message"]
    assert not (lib_settings.USER_TEMPLATES_DIR / "veos.yml").exists()

    valid = await client.patch(endpoint, json={
        "capabilities": {"machine": "q35", "hotplug": True},
        "extras": {"qemu_options": "-machine pc-q35-8.2,accel=kvm -cpu host"},
    })
    assert valid.json()["code"] == 200
    template = TemplateService().get_template("qemu", "veos")
    assert template.capabilities["machine"] == "q35"
    assert template.extras["qemu_options"] == "-machine pc-q35-8.2,accel=kvm -cpu host"


@pytest.mark.asyncio
async def test_patch_image_wildcard_filters_picker_and_library_until_cleared(client, lib_settings):
    _seed_veos(lib_settings)
    for name in ("EDGE-1", "other-edge-1"):
        image = lib_settings.IMAGES_DIR / "qemu" / name
        image.mkdir()
        (image / "disk.qcow2").touch()
    endpoint = "/api/library/templates/qemu/veos"

    for aliases, expected in [(["edge-*"], {"EDGE-1", "veos"}), ([], {"veos"})]:
        saved = await client.patch(endpoint, json={"image_aliases": aliases})
        assert saved.json()["data"]["image_aliases"] == aliases
        listed = (await client.get("/api/library/templates")).json()["data"]["templates"]
        veos = next(template for template in listed if template["key"] == "veos")
        assert {image["name"] for image in veos["images"]} == expected
        assert set(TemplateService().list_images("qemu", "veos")) == expected


@pytest.mark.asyncio
async def test_user_template_inherits_builtin_aliases_until_explicitly_replaced(client, lib_settings):
    _seed_veos(lib_settings)
    shadow = lib_settings.USER_TEMPLATES_DIR / "qemu" / "veos.yml"
    shadow.parent.mkdir()
    shadow.write_text("name: Operator appliance\nram: 4096\n")
    endpoint = "/api/library/templates/qemu/veos"
    service = TemplateService()

    assert set(service.list_images("qemu", "veos")) == {"veos"}
    saved = await client.patch(endpoint, json={"description": "Operator settings"})
    assert saved.json()["data"]["image_aliases"] == ["veos"]
    assert "image_aliases" not in shadow.read_text()

    cleared = await client.patch(endpoint, json={"image_aliases": []})
    assert cleared.json()["data"]["image_aliases"] == []
    assert "image_aliases: []" in shadow.read_text()
    assert service.list_images("qemu", "veos") == {}


@pytest.mark.asyncio
async def test_builtin_edit_leaves_aliases_inherited_when_omitted(client, lib_settings):
    _seed_veos(lib_settings)
    endpoint = "/api/library/templates/qemu/veos"
    saved = await client.patch(endpoint, json={"name": "Operator appliance"})
    assert saved.json()["data"]["image_aliases"] == ["veos"]
    assert "image_aliases" not in (lib_settings.USER_TEMPLATES_DIR / "veos.yml").read_text()
    assert set(TemplateService().list_images("qemu", "veos")) == {"veos"}


@pytest.mark.asyncio
async def test_patch_template_rejects_invalid_fields(client, lib_settings):
    _seed_veos(lib_settings)

    # Pydantic's ge=1 rejects non-positive integers at request-validation
    # level → native FastAPI 422.
    response = await client.patch(
        "/api/library/templates/qemu/veos",
        json={"ram": 0},
    )
    assert response.status_code == 422

    # A well-typed but empty edit is rejected by the service with the
    # body-wrapped 422 shape.
    response = await client.patch("/api/library/templates/qemu/veos", json={})
    assert response.status_code == 200
    assert response.json()["code"] == 422


@pytest.mark.asyncio
async def test_patch_missing_template_returns_404(client, lib_settings):
    response = await client.patch(
        "/api/library/templates/qemu/ghost",
        json={"ram": 2048},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 404


@pytest.mark.asyncio
async def test_create_and_delete_custom_template_without_removing_its_image(
    client, lib_settings, audit_events
):
    image = lib_settings.IMAGES_DIR / "qemu" / "custom-os-1"
    image.mkdir(parents=True)
    disk = image / "virtioa.qcow2"
    disk.touch()
    payload = {
        "type": "qemu",
        "key": "custom.os",
        "name": "Custom appliance",
        "cpu": 2,
        "image_aliases": ["custom-os-*"],
    }
    created = (await client.post("/api/library/templates", json=payload)).json()
    assert created["code"] == 200
    assert created["data"]["source"] == "user"
    assert created["data"]["cpu"] == 2
    assert set(TemplateService().list_images("qemu", "custom.os")) == {"custom-os-1"}
    path = lib_settings.USER_TEMPLATES_DIR / "qemu" / "custom.os.yml"
    before_duplicate = path.read_bytes()
    duplicate = await client.post("/api/library/templates", json={**payload, "name": "Duplicate"})
    assert duplicate.json()["code"] == 409
    assert path.read_bytes() == before_duplicate

    endpoint = "/api/library/templates/qemu/custom.os"
    assert (await client.patch(endpoint, json={"ram": 3072})).json()["code"] == 200
    listed = (await client.get("/api/library/templates")).json()["data"]["templates"]
    assert listed[0]["source"] == "user"
    assert listed[0]["ram"] == 3072
    assert [entry["name"] for entry in listed[0]["images"]] == ["custom-os-1"]

    assert (await client.delete(endpoint)).json()["code"] == 200
    assert not path.exists()
    assert disk.is_file()
    assert (await client.delete(endpoint)).json()["code"] == 404
    assert _audited(audit_events, "template.create")[0].detail["via"] == "form"
    assert _audited(audit_events, "template.delete")[0].target_id == "qemu/custom.os"


@pytest.mark.asyncio
async def test_upload_yaml_reads_type_and_key_and_preserves_custom_settings(
    client, lib_settings, audit_events
):
    yaml_text = (
        "type: qemu\nname: Upload demo\nram: 2048\n"
        "image_aliases: [upload-demo-*]\n"
        "interface_naming: {explicit: [management, uplink]}\n"
        "extras: {disk_bus: sata, appliance_setting: keep-me}\n"
    )
    files = {"file": ("upload-demo.yaml", yaml_text, "application/yaml")}
    response = (await client.post("/api/library/templates/upload", files=files)).json()
    assert response["code"] == 200
    assert response["data"]["id"] == "upload-demo"
    template = TemplateService().get_template("qemu", "upload-demo")
    assert template.extras == {"disk_bus": "sata", "appliance_setting": "keep-me"}
    assert template.interface_naming == {"explicit": ["management", "uplink"]}
    assert template.image_aliases == ["upload-demo-*"]
    assert (lib_settings.USER_TEMPLATES_DIR / "qemu" / "upload-demo.yml").is_file()
    assert (await client.post("/api/library/templates/upload", files=files)).json()["code"] == 409
    assert _audited(audit_events, "template.create")[0].detail["via"] == "yaml"


@pytest.mark.parametrize(
    ("filename", "yaml_text"),
    [
        ("../outside.yml", "type: qemu"),
        ("wrong.txt", "type: qemu"),
        ("bad.yml", "type: ["),
        ("array.yml", "- type: qemu"),
        ("missing-type.yml", "name: No type"),
        ("bad-console.yml", "type: iol\nconsole_type: vnc"),
        ("bad-interfaces.yml", "type: qemu\ninterface_naming: {format: no-placeholder}"),
        ("recursive.yml", "type: qemu\nextras: &self {loop: *self}"),
        ("large.yml", "#" * (256 * 1024 + 1)),
    ],
)
@pytest.mark.asyncio
async def test_rejects_invalid_template_upload_without_writing_files(
    client, lib_settings, filename, yaml_text
):
    response = await client.post(
        "/api/library/templates/upload",
        files={"file": (filename, yaml_text, "application/yaml")},
    )
    assert response.json()["code"] == 422
    assert list(lib_settings.USER_TEMPLATES_DIR.rglob("*.yml")) == []


@pytest.mark.asyncio
async def test_create_and_upload_do_not_replace_shipped_templates(client, lib_settings):
    _seed_veos(lib_settings)
    endpoint = "/api/library/templates"
    form = {"type": "qemu", "key": "veos", "name": "Replacement"}
    files = {"file": ("veos.yml", "type: qemu\nname: Replacement\n")}
    assert (await client.post(endpoint, json=form)).json()["code"] == 409
    assert (await client.post(f"{endpoint}/upload", files=files)).json()["code"] == 409
    assert list(lib_settings.USER_TEMPLATES_DIR.rglob("*.yml")) == []
    assert TemplateService().get_template("qemu", "veos").name == "vEOS"


@pytest.mark.asyncio
async def test_delete_blocks_a_referenced_custom_template_without_requiring_an_image(
    client, lib_settings, audit_events
):
    path = lib_settings.USER_TEMPLATES_DIR / "custom.yml"
    path.write_text("type: qemu\nname: Custom\n")
    nested = lib_settings.LABS_DIR / "nested"
    nested.mkdir()
    (nested / "saved.json").write_text(json.dumps({
        "meta": {"name": "Saved lab"},
        "nodes": {"1": {"id": 1, "name": "Edge", "type": "qemu", "template": "custom"}},
    }))
    response = (await client.delete("/api/library/templates/qemu/custom")).json()
    assert response["code"] == 409
    assert response["data"]["references"][0]["lab_path"] == "/nested/saved.json"
    assert path.exists()
    assert _audited(audit_events, "template.delete") == []


@pytest.mark.asyncio
async def test_remove_override_reveals_builtin_and_blocks_deleting_shipped_template(
    client, lib_settings, audit_events
):
    _seed_veos(lib_settings)
    path = lib_settings.USER_TEMPLATES_DIR / "qemu" / "veos.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"name": "Operator settings", "ram": 4096}))
    endpoint = "/api/library/templates/qemu/veos"

    inventory = (await client.get("/api/library/templates")).json()["data"]["templates"]
    assert next(row for row in inventory if row["key"] == "veos")["source"] == "override"
    removed = (await client.delete(endpoint)).json()
    assert removed["code"] == 200
    assert removed["data"]["reverted"] is True
    assert not path.exists()
    assert (await client.delete(endpoint)).json()["code"] == 409
    inventory = (await client.get("/api/library/templates")).json()["data"]["templates"]
    veos = next(row for row in inventory if row["key"] == "veos")
    assert veos["source"] == "builtin"
    assert veos["name"] == "vEOS"
    assert _audited(audit_events, "template.delete")[0].detail["reverted_override"] is True
