"""Tests for ``POST /api/labs/{lab_path:path}/nodes/from-paired-template`` (#202).

Paired-node templates instantiate two-or-more child nodes plus the auto-link
between them in a single atomic call. The endpoint:

- 200 + ``{nodes:[...], links:[...]}`` on success
- 404 when the template_key isn't a paired template in USER_TEMPLATES_DIR
- 500 + lab.json rolled back when any phase fails (no orphan nodes)
- single-node ``/nodes/from-template`` keeps rejecting paired with 400 + new pointer

Tests build a synthetic vMX-shaped paired template directly under
``USER_TEMPLATES_DIR`` (no real Juniper images needed) and exercise the endpoint
against an empty lab.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers import labs
from app.schemas.node import NodeFromPairedTemplate, NodeFromTemplate
from app.services.template_service import TemplateService


def _admin():
    return SimpleNamespace(username="admin", role="admin", html5=True)


VMX_PAIRED_TEMPLATE = {
    "schema": 1,
    "id": "juniper-vmx",
    "name": "Juniper vMX",
    "vendor": "juniper",
    "kind": "paired",
    "nodes": [
        {
            "id": "vcp",
            "name": "vMX VCP",
            "kind": "qemu",
            "image": "vmx-vcp-22.4.qcow2",
            "cpu": 1,
            "ram": 2048,
            "ethernet": 1,
            "console": "serial",
            "interface_naming": {"explicit": ["fxp0"]},
            "extras": {"qemu_nic": "virtio-net-pci"},
        },
        {
            "id": "vfp",
            "name": "vMX VFP",
            "kind": "qemu",
            "image": "vmx-vfp-22.4.qcow2",
            "cpu": 3,
            "ram": 4096,
            "ethernet": 4,
            "console": "serial",
            "interface_naming": {"explicit": ["em0", "em1", "em2", "em3"]},
            "extras": {"qemu_nic": "virtio-net-pci"},
        },
    ],
    "links": [
        {"from_node": "vcp", "from_iface": "fxp0", "to_node": "vfp", "to_iface": "em0"},
    ],
}


@pytest.fixture()
def split_template_dirs(tmp_path):
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
    monkeypatch.setattr("app.services.link_service.get_settings", lambda: split_template_dirs)
    return split_template_dirs


def _seed_empty_lab(settings, lab_id: str = "lab-paired") -> Path:
    lab_path = settings.LABS_DIR / "demo.json"
    lab_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": {"name": "demo"},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": {},
                "networks": {},
                "links": [],
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )
    return lab_path


def _seed_paired_template(settings, template: dict, key: str = "juniper-vmx") -> None:
    (settings.USER_TEMPLATES_DIR / f"{key}.json").write_text(json.dumps(template))


# ---- get_paired_user_template -------------------------------------------


def test_get_paired_user_template_returns_full_dict(patched_split):
    _seed_paired_template(patched_split, VMX_PAIRED_TEMPLATE)
    paired = TemplateService().get_paired_user_template("juniper-vmx")
    assert paired is not None
    assert paired["kind"] == "paired"
    assert len(paired["nodes"]) == 2
    assert len(paired["links"]) == 1


def test_get_paired_user_template_returns_none_for_non_paired(patched_split):
    (patched_split.USER_TEMPLATES_DIR / "csr.json").write_text(
        json.dumps({"schema": 1, "id": "csr", "kind": "qemu", "image": "csr1000v.qcow2"})
    )
    assert TemplateService().get_paired_user_template("csr") is None


def test_get_paired_user_template_rejects_empty_nodes_list(patched_split):
    _seed_paired_template(
        patched_split,
        {"schema": 1, "id": "broken", "kind": "paired", "nodes": [], "links": []},
        key="broken",
    )
    assert TemplateService().get_paired_user_template("broken") is None


# ---- catalog response ---------------------------------------------------


def test_catalog_includes_paired_templates_block(patched_split):
    _seed_paired_template(patched_split, VMX_PAIRED_TEMPLATE)
    catalog = TemplateService().build_node_catalog()
    assert "paired_templates" in catalog
    paired_entries = catalog["paired_templates"]
    assert len(paired_entries) == 1
    entry = paired_entries[0]
    assert entry["key"] == "juniper-vmx"
    assert entry["child_count"] == 2
    assert entry["link_count"] == 1
    assert entry["vendor"] == "juniper"
    assert {c["id"] for c in entry["children"]} == {"vcp", "vfp"}


def test_catalog_paired_templates_empty_when_none_exist(patched_split):
    catalog = TemplateService().build_node_catalog()
    assert catalog["paired_templates"] == []


# ---- POST /nodes/from-paired-template -----------------------------------


@pytest.mark.asyncio
async def test_creates_two_nodes_and_one_link(patched_split):
    settings = patched_split  # noqa: F841 — used inside the lab.json read below
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(
            template_key="juniper-vmx",
            base_left=100,
            base_top=200,
        ),
        current_user=_admin(),
    )

    assert response["code"] == 200, response
    nodes = response["data"]["nodes"]
    links = response["data"]["links"]
    assert len(nodes) == 2
    assert len(links) == 1

    # Default child name comes from the template's per-child `name` field.
    node_by_name = {n["name"]: n for n in nodes}
    assert "vMX VCP" in node_by_name
    assert "vMX VFP" in node_by_name
    vcp = node_by_name["vMX VCP"]
    assert vcp["cpu"] == 1
    assert vcp["ram"] == 2048
    assert vcp["ethernet"] == 1
    # interface_naming.explicit honored
    assert vcp["interfaces"][0]["name"] == "fxp0"
    vfp = node_by_name["vMX VFP"]
    assert vfp["interfaces"][0]["name"] == "em0"
    assert vfp["interfaces"][3]["name"] == "em3"

    # LinkService synthesizes an implicit bridge for node↔node links and returns
    # the first node↔bridge half. Confirm the returned link points to VCP's
    # fxp0 (interface_index 0) and the bridge is the other endpoint.
    link = links[0]
    assert link["from"]["node_id"] == vcp["id"]
    assert link["from"]["interface_index"] == 0
    assert "network_id" in link["to"]

    # The full lab.json must contain BOTH halves of the paired link plus an
    # implicit bridge — paired creation is atomic across all link halves.
    lab_data = json.loads((settings.LABS_DIR / "demo.json").read_text())
    assert len(lab_data["links"]) == 2
    assert len(lab_data["networks"]) == 1
    bridge_id = next(iter(lab_data["networks"].keys()))
    node_iface_pairs = set()
    for raw_link in lab_data["links"]:
        from_ep = raw_link["from"]
        to_ep = raw_link["to"]
        # One endpoint must be the implicit bridge.
        if "network_id" in from_ep:
            assert str(from_ep["network_id"]) == bridge_id
            node_iface_pairs.add((to_ep["node_id"], to_ep["interface_index"]))
        else:
            assert str(to_ep["network_id"]) == bridge_id
            node_iface_pairs.add((from_ep["node_id"], from_ep["interface_index"]))
    # Both child nodes connect to the bridge on interface index 0
    # (VCP fxp0 / VFP em0 — the fxp0↔em0 link from the paired template).
    assert node_iface_pairs == {(vcp["id"], 0), (vfp["id"], 0)}


@pytest.mark.asyncio
async def test_returns_404_for_unknown_template(patched_split):
    _seed_empty_lab(patched_split)
    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="nonexistent"),
        current_user=_admin(),
    )
    assert response["code"] == 404
    assert "paired template" in response["message"].lower()


@pytest.mark.asyncio
async def test_returns_404_for_singleton_template(patched_split):
    """A single-node template (kind="qemu") must NOT match the paired endpoint."""
    settings = patched_split
    (settings.USER_TEMPLATES_DIR / "csr.json").write_text(
        json.dumps({"schema": 1, "id": "csr", "kind": "qemu", "image": "csr.qcow2"})
    )
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="csr"),
        current_user=_admin(),
    )
    assert response["code"] == 404


@pytest.mark.asyncio
async def test_rolls_back_when_link_creation_fails(patched_split, monkeypatch):
    """Partial-failure rollback: link-phase fails → lab.json restored, no orphans."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    lab_file = _seed_empty_lab(settings)

    # Force LinkService.create_link to blow up.
    from app.services import link_service

    async def _explode(*args, **kwargs):
        raise RuntimeError("simulated link creation failure")

    monkeypatch.setattr(link_service.LinkService, "create_link", _explode)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 500
    assert "simulated link creation failure" in response["message"]

    # Lab file must be back to the pre-call state — no orphan nodes left behind.
    on_disk = json.loads(lab_file.read_text())
    assert on_disk["nodes"] == {}
    assert on_disk["links"] == []


@pytest.mark.asyncio
async def test_name_overrides_apply_per_child(patched_split):
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(
            template_key="juniper-vmx",
            name_overrides={"vcp": "edge-router-cp", "vfp": "edge-router-fp"},
        ),
        current_user=_admin(),
    )
    assert response["code"] == 200
    names = {n["name"] for n in response["data"]["nodes"]}
    assert names == {"edge-router-cp", "edge-router-fp"}


@pytest.mark.asyncio
async def test_children_laid_out_in_row_relative_to_base(patched_split):
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx", base_left=500, base_top=300),
        current_user=_admin(),
    )
    nodes = response["data"]["nodes"]
    assert nodes[0]["left"] == 500
    assert nodes[0]["top"] == 300
    assert nodes[1]["left"] == 500 + 180
    assert nodes[1]["top"] == 300


# ---- regression: single-node /from-template still works -----------------


@pytest.mark.asyncio
async def test_single_template_endpoint_redirects_paired_to_new_endpoint(patched_split):
    """Existing /nodes/from-template must still 400 paired keys, with the new pointer."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_node_from_template(
        "demo.json",
        NodeFromTemplate(
            template_type="qemu",
            template_key="juniper-vmx",
            name="x",
            image="y",
        ),
        current_user=_admin(),
    )
    assert response["code"] == 400
    assert "from-paired-template" in response["message"]
    assert "#202" in response["message"]
