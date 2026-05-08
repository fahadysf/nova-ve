"""Tests for USER_TEMPLATES_DIR + the from-template POST endpoint (#185)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers import labs
from app.schemas.node import NodeFromTemplate
from app.services.template_service import TemplateService


def _admin():
    return SimpleNamespace(username="admin", role="admin", html5=True)


@pytest.fixture()
def split_template_dirs(tmp_path):
    """Builtin TEMPLATES_DIR + USER_TEMPLATES_DIR side-by-side, both pointed into tmp_path."""
    builtin = tmp_path / "builtin_templates"
    user = tmp_path / "user_templates"
    builtin.mkdir()
    user.mkdir()
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    for d in (labs_dir, images_dir, tmp_dir):
        d.mkdir()
    return SimpleNamespace(
        BASE_DATA_DIR=tmp_path,
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        TEMPLATES_DIR=builtin,
        USER_TEMPLATES_DIR=user,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        SESSION_MAX_AGE=14400,
    )


@pytest.fixture()
def patched_split(monkeypatch, split_template_dirs):
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: split_template_dirs)
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: split_template_dirs)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: split_template_dirs)
    return split_template_dirs


# ---- USER_TEMPLATES_DIR walking & shadowing ----------------------------


def test_user_dir_template_appears_in_listing(patched_split, caplog: pytest.LogCaptureFixture):
    """A YAML in USER_TEMPLATES_DIR/qemu/ shows up via list_templates()."""
    settings = patched_split
    (settings.TEMPLATES_DIR / "qemu").mkdir()
    (settings.TEMPLATES_DIR / "qemu" / "csr.yml").write_text(
        "type: qemu\nname: CSR1000v\ncpu: 2\nram: 4096\nethernet: 2\nconsole_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )
    (settings.USER_TEMPLATES_DIR / "qemu").mkdir()
    (settings.USER_TEMPLATES_DIR / "qemu" / "vyos.yml").write_text(
        "type: qemu\nname: VyOS-imported\ncpu: 1\nram: 1024\nethernet: 4\nconsole_type: serial\nicon_type: router\ncpulimit: 1\n"
    )

    listed = TemplateService().list_templates("qemu")
    assert "csr" in listed  # builtin
    assert "vyos" in listed  # user-imported


def test_user_dir_shadows_builtin_with_warning(patched_split, caplog: pytest.LogCaptureFixture):
    """If user-dir contains a key the builtin already has, the user entry wins + WARNING is logged."""
    settings = patched_split
    (settings.TEMPLATES_DIR / "qemu").mkdir()
    (settings.TEMPLATES_DIR / "qemu" / "csr.yml").write_text(
        "type: qemu\nname: CSR1000v-builtin\ncpu: 2\nram: 4096\nethernet: 2\nconsole_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )
    (settings.USER_TEMPLATES_DIR / "qemu").mkdir()
    (settings.USER_TEMPLATES_DIR / "qemu" / "csr.yml").write_text(
        "type: qemu\nname: CSR1000v-OPERATOR-CUSTOMISED\ncpu: 4\nram: 8192\nethernet: 4\nconsole_type: serial\nicon_type: router\ncpulimit: 1\n"
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="nova-ve.template_service"):
        listed = TemplateService().list_templates("qemu")

    assert listed["csr"]["name"] == "CSR1000v-OPERATOR-CUSTOMISED"
    assert listed["csr"]["ram"] == 8192
    shadow_logs = [r for r in caplog.records if "shadows builtin" in r.message]
    assert len(shadow_logs) == 1
    assert shadow_logs[0].template_type == "qemu"
    assert shadow_logs[0].template_key == "csr"


def test_user_dir_missing_does_not_break_listing(patched_split):
    """A non-existent USER_TEMPLATES_DIR is fine — service still lists builtin templates."""
    settings = patched_split
    settings.USER_TEMPLATES_DIR.rmdir()  # delete the user dir entirely
    (settings.TEMPLATES_DIR / "qemu").mkdir()
    (settings.TEMPLATES_DIR / "qemu" / "csr.yml").write_text(
        "type: qemu\nname: CSR1000v\ncpu: 2\nram: 4096\nethernet: 2\nconsole_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )
    listed = TemplateService().list_templates("qemu")
    assert "csr" in listed


# ---- is_paired_user_template -------------------------------------------


def test_is_paired_user_template_returns_true_for_paired_json(patched_split):
    settings = patched_split
    (settings.USER_TEMPLATES_DIR / "juniper-vmx.json").write_text(
        json.dumps({
                "schema": 1,
                "id": "juniper-vmx",
                "kind": "paired",
                "nodes": [
                    {"id": "vcp", "name": "vMX VCP", "kind": "qemu", "image": "vmx-vcp.qcow2"},
                    {"id": "vfp", "name": "vMX VFP", "kind": "qemu", "image": "vmx-vfp.qcow2"},
                ],
                "links": [{"from_node": "vcp", "from_iface": "fxp0", "to_node": "vfp", "to_iface": "em0"}],
            })
    )
    assert TemplateService().is_paired_user_template("juniper-vmx") is True


def test_is_paired_user_template_returns_false_for_single_node_json(patched_split):
    settings = patched_split
    (settings.USER_TEMPLATES_DIR / "csr.json").write_text(
        json.dumps({"schema": 1, "id": "csr", "kind": "qemu", "image": "csr1000v"})
    )
    assert TemplateService().is_paired_user_template("csr") is False


def test_is_paired_user_template_returns_false_when_user_dir_empty(patched_split):
    assert TemplateService().is_paired_user_template("anything") is False


def test_is_paired_user_template_tolerates_malformed_json(patched_split):
    settings = patched_split
    (settings.USER_TEMPLATES_DIR / "broken.json").write_text("not valid json {{{")
    # Returns False (not raises) per the read-only-tolerant contract.
    assert TemplateService().is_paired_user_template("broken") is False


# ---- POST /api/labs/{lab}/nodes/from-template --------------------------


@pytest.mark.asyncio
async def test_from_template_endpoint_creates_node(patched_split):
    settings = patched_split
    # Seed a builtin csr template.
    (settings.TEMPLATES_DIR / "qemu").mkdir()
    (settings.TEMPLATES_DIR / "qemu" / "csr.yml").write_text(
        "type: qemu\nname: CSR1000v\ncpu: 2\nram: 4096\nethernet: 2\nconsole_type: telnet\nicon_type: router\ncpulimit: 1\nextras:\n  architecture: x86_64\n  qemu_nic: virtio-net-pci\n"
    )
    # Seed a CSR1000v image dir so create_node's image-validation passes.
    image_dir = settings.IMAGES_DIR / "qemu" / "csr1000v"
    image_dir.mkdir(parents=True)
    (image_dir / "hda.qcow2").write_text("base")
    # Seed an empty lab.
    (settings.LABS_DIR / "demo.json").write_text(json.dumps({
        "schema": 2,
        "id": "lab-from-template",
        "meta": {"name": "demo"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {},
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }))

    response = await labs.create_node_from_template(
        "demo.json",
        NodeFromTemplate(
            template_type="qemu",
            template_key="csr",
            name="csr-from-template",
            image="csr1000v",
            left=42,
            top=84,
        ),
        current_user=_admin(),
    )
    assert response["code"] == 200
    node = response["data"]
    assert node["name"] == "csr-from-template"
    assert node["type"] == "qemu"
    assert node["template"] == "csr"
    assert node["cpu"] == 2  # inherited from template
    assert node["ram"] == 4096  # inherited from template


@pytest.mark.asyncio
async def test_from_template_rejects_paired_with_400(patched_split):
    """Paired-node templates (kind='paired' in user-dir JSON) must be rejected with 400."""
    settings = patched_split
    (settings.USER_TEMPLATES_DIR / "juniper-vmx.json").write_text(
        json.dumps({
                "schema": 1,
                "id": "juniper-vmx",
                "kind": "paired",
                "nodes": [
                    {"id": "vcp", "name": "vMX VCP", "kind": "qemu", "image": "vmx-vcp.qcow2"},
                    {"id": "vfp", "name": "vMX VFP", "kind": "qemu", "image": "vmx-vfp.qcow2"},
                ],
                "links": [{"from_node": "vcp", "from_iface": "fxp0", "to_node": "vfp", "to_iface": "em0"}],
            })
    )
    (settings.LABS_DIR / "demo.json").write_text(json.dumps({
        "schema": 2,
        "id": "lab-paired-rejection",
        "meta": {"name": "demo"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {},
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }))

    response = await labs.create_node_from_template(
        "demo.json",
        NodeFromTemplate(
            template_type="qemu",
            template_key="juniper-vmx",
            name="vmx-1",
            image="vmx-vcp.qcow2",
        ),
        current_user=_admin(),
    )
    assert response["code"] == 400
    assert "paired" in response["message"].lower()


@pytest.mark.asyncio
async def test_from_template_returns_404_when_template_missing(patched_split):
    settings = patched_split
    (settings.LABS_DIR / "demo.json").write_text(json.dumps({
        "schema": 2,
        "id": "lab-template-missing",
        "meta": {"name": "demo"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {},
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }))

    response = await labs.create_node_from_template(
        "demo.json",
        NodeFromTemplate(
            template_type="qemu",
            template_key="nonexistent-template",
            name="x",
            image="y",
        ),
        current_user=_admin(),
    )
    assert response["code"] == 404


# ---- existing GET /api/list/templates/{type} still works ---------------


def test_existing_list_endpoint_returns_user_dir_templates(patched_split):
    """No new endpoint added — operator-imported templates flow through the existing list endpoint."""
    settings = patched_split
    (settings.USER_TEMPLATES_DIR / "qemu").mkdir()
    (settings.USER_TEMPLATES_DIR / "qemu" / "imported.yml").write_text(
        "type: qemu\nname: ImportedFromUserDir\ncpu: 1\nram: 512\nethernet: 1\nconsole_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )
    listed = TemplateService().list_templates("qemu")
    assert "imported" in listed
    assert listed["imported"]["name"] == "ImportedFromUserDir"
