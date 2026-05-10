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


# ---- #206 synthetic per-child template identity -------------------------


def test_206_synthetic_paired_child_templates_appear_in_listing(patched_split):
    """Each child of a paired template surfaces as a real entry in
    list_templates(<child_kind>) keyed ``<paired_key>__<child_id>`` with
    ``paired_parent`` set to the originating paired template key."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)

    listed = TemplateService().list_templates("qemu")
    assert "juniper-vmx__vcp" in listed
    assert "juniper-vmx__vfp" in listed
    vcp_entry = listed["juniper-vmx__vcp"]
    assert vcp_entry["name"] == "vMX VCP"
    assert vcp_entry["cpu"] == 1
    assert vcp_entry["ram"] == 2048
    assert vcp_entry["ethernet"] == 1


def test_206_synthetic_entries_carry_paired_parent_in_catalog(patched_split):
    """Catalog ``templates: [...]`` carries paired_parent on synthetic entries
    so the frontend can hide them from the standalone create-flow type tabs."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)

    catalog = TemplateService().build_node_catalog()
    by_key = {t["key"]: t for t in catalog["templates"]}
    assert by_key["juniper-vmx__vcp"]["paired_parent"] == "juniper-vmx"
    assert by_key["juniper-vmx__vfp"]["paired_parent"] == "juniper-vmx"


def test_206_real_templates_have_paired_parent_none(patched_split):
    """Non-synthetic (regular) catalog entries surface paired_parent: None."""
    settings = patched_split
    (settings.TEMPLATES_DIR / "qemu").mkdir()
    (settings.TEMPLATES_DIR / "qemu" / "csr.yml").write_text(
        "type: qemu\nname: CSR1000v\ncpu: 2\nram: 4096\nethernet: 2\nconsole_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )
    catalog = TemplateService().build_node_catalog()
    by_key = {t["key"]: t for t in catalog["templates"]}
    assert by_key["csr"]["paired_parent"] is None


def test_206_get_template_resolves_synthetic_paired_child(patched_split):
    """get_template lookup must succeed for the synthetic key — required by
    edit-mode image validation in update_node."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)

    svc = TemplateService()
    vcp = svc.get_template("qemu", "juniper-vmx__vcp")
    assert vcp.key == "juniper-vmx__vcp"
    assert vcp.paired_parent == "juniper-vmx"
    # The synthetic template surfaces the child's declared image (single-entry).
    images = svc.list_images("qemu", "juniper-vmx__vcp")
    assert "vmx-vcp-22.4.qcow2" in images


def test_206_validate_node_request_passes_for_synthetic_child(patched_split):
    """validate_node_request — used by node creation/edit flows for image
    validation against node.template — must accept the synthetic key + the
    child's declared image. This is the path that broke pre-#206 when child
    nodes carried the paired template key as ``template`` and image
    validation 400'd because the paired template wasn't in the catalog."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)

    svc = TemplateService()
    template = svc.validate_node_request("qemu", "juniper-vmx__vcp", "vmx-vcp-22.4.qcow2")
    assert template.paired_parent == "juniper-vmx"


@pytest.mark.asyncio
async def test_206_paired_create_writes_synthetic_template_key_on_children(patched_split):
    """End-to-end: paired-create on the endpoint must store the synthetic
    per-child template key on every child node, not the paired template key."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 200
    nodes = response["data"]["nodes"]
    by_name = {n["name"]: n for n in nodes}
    assert by_name["vMX VCP"]["template"] == "juniper-vmx__vcp"
    assert by_name["vMX VFP"]["template"] == "juniper-vmx__vfp"


def test_206_synthetic_key_helper_is_stable(patched_split):
    """synthetic_paired_child_key must produce the documented format and be
    importable from app.services.template_service so callers (including
    routers/labs.py and tests) can construct keys consistently."""
    from app.services.template_service import synthetic_paired_child_key

    assert synthetic_paired_child_key("juniper-vmx", "vcp") == "juniper-vmx__vcp"
    assert synthetic_paired_child_key("paired-with-dashes", "child-x") == "paired-with-dashes__child-x"


@pytest.mark.asyncio
async def test_206_synthetic_child_key_rejected_by_single_node_endpoint(patched_split):
    """Codex-iter2 fix: POST /nodes/from-template must reject synthetic
    per-child keys (paired_parent set) with 400 + a pointer to the paired
    endpoint. Pre-fix the route fell through to NodeCreate construction and
    raised an uncaught Pydantic ValidationError when the child's console_type
    (e.g. ``serial`` for Juniper) violated NodeCreate.console's Literal.
    """
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_node_from_template(
        "demo.json",
        NodeFromTemplate(
            template_type="qemu",
            template_key="juniper-vmx__vcp",  # synthetic child key
            name="x",
            image="vmx-vcp-22.4.qcow2",
        ),
        current_user=_admin(),
    )
    assert response["code"] == 400, response
    assert "synthetic" in response["message"].lower()
    assert "juniper-vmx" in response["message"]
    assert "from-paired-template" in response["message"]


def test_206_paired_catalog_children_carry_synthetic_template_key(patched_split):
    """The catalog's paired_templates[].children entries carry the synthetic
    template_key so the frontend's paired-template "Will create" panel can
    cross-reference the matching catalog entry for capabilities/extras
    schema."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)

    catalog = TemplateService().build_node_catalog()
    assert len(catalog["paired_templates"]) == 1
    paired = catalog["paired_templates"][0]
    assert paired["key"] == "juniper-vmx"
    by_id = {c["id"]: c for c in paired["children"]}
    assert by_id["vcp"]["template_key"] == "juniper-vmx__vcp"
    assert by_id["vfp"]["template_key"] == "juniper-vmx__vfp"


# ---- #207 paired-template pre-flight validation -------------------------


# Pre-#202-shape vMX paired template: no interface_naming.explicit per child,
# vQFX-style ethernet=1 on the RE so em1 link target is out of range. This is
# what the original importer (before commit 6acd72c) produced. Should fail
# pre-flight under #207 and surface valid:false in the catalog.
LEGACY_PRE_202_VMX_TEMPLATE = {
    "schema": 1,
    "id": "juniper-vmx",
    "name": "Juniper vMX (legacy import)",
    "vendor": "juniper",
    "kind": "paired",
    "nodes": [
        {
            "id": "vcp",
            "name": "vMX VCP (legacy)",
            "kind": "qemu",
            "image": "vmx-vcp-22.4.qcow2",
            "cpu": 1,
            "ram": 2048,
            "ethernet": 1,
            "console": "serial",
            # No interface_naming.explicit — this is the bug pre-#202 importer
            # had. Generic Gi1 naming will be used at creation; fxp0/em0 link
            # refs won't resolve.
        },
        {
            "id": "vfp",
            "name": "vMX VFP (legacy)",
            "kind": "qemu",
            "image": "vmx-vfp-22.4.qcow2",
            "cpu": 3,
            "ram": 4096,
            "ethernet": 4,
            "console": "serial",
        },
    ],
    "links": [
        {"from_node": "vcp", "from_iface": "fxp0", "to_node": "vfp", "to_iface": "em0"},
    ],
}


def test_207_legacy_paired_template_flagged_invalid_in_catalog(patched_split, caplog):
    """A pre-#202-shape paired template (no interface_naming.explicit on the
    children) appears in the catalog but with valid:false + a clear
    invalid_reason naming the missing interface."""
    import logging

    settings = patched_split
    _seed_paired_template(settings, LEGACY_PRE_202_VMX_TEMPLATE)

    with caplog.at_level(logging.WARNING, logger="nova-ve.template_service"):
        catalog = TemplateService().build_node_catalog()

    assert len(catalog["paired_templates"]) == 1
    entry = catalog["paired_templates"][0]
    assert entry["valid"] is False
    assert entry["invalid_reason"] is not None
    # The reason should name the unresolvable interface
    assert "fxp0" in entry["invalid_reason"]
    # And the WARNING log fired exactly once for this template
    pre_flight_warnings = [
        r for r in caplog.records if "pre-flight failed" in r.message
    ]
    assert len(pre_flight_warnings) == 1


def test_207_valid_paired_template_marked_valid_in_catalog(patched_split):
    """The properly-shaped vMX template (with interface_naming.explicit) gets
    valid:true and invalid_reason:None."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)

    catalog = TemplateService().build_node_catalog()
    entry = catalog["paired_templates"][0]
    assert entry["valid"] is True
    assert entry["invalid_reason"] is None


@pytest.mark.asyncio
async def test_207_endpoint_returns_422_for_invalid_template(patched_split):
    """POST /nodes/from-paired-template against an invalid (legacy-import-shape)
    template returns 422 (operator-correctable) with the invalid_reason in
    the message — not 500 (server error)."""
    settings = patched_split
    _seed_paired_template(settings, LEGACY_PRE_202_VMX_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 422, response
    assert "fxp0" in response["message"]
    assert "interface_naming.explicit" in response["message"]


@pytest.mark.asyncio
async def test_207_endpoint_still_works_for_valid_template(patched_split):
    """Sanity: a valid paired template still creates 2 nodes + 1 link end-to-end."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 200, response
    assert len(response["data"]["nodes"]) == 2
    assert len(response["data"]["links"]) == 1


def test_207_validate_helper_returns_none_on_valid(patched_split):
    """The validate_paired_template helper is importable and returns None
    when the template is fully resolvable."""
    from app.services.template_service import validate_paired_template

    assert validate_paired_template(VMX_PAIRED_TEMPLATE) is None


def test_207_validate_helper_returns_reason_on_invalid(patched_split):
    """The validate_paired_template helper returns a clear reason string
    when a link references an unresolvable interface."""
    from app.services.template_service import validate_paired_template

    reason = validate_paired_template(LEGACY_PRE_202_VMX_TEMPLATE)
    assert reason is not None
    assert "fxp0" in reason
    assert "interface_naming.explicit" in reason


def test_207_validate_helper_returns_reason_for_unknown_child(patched_split):
    """Pre-flight catches link refs to non-existent child ids, not just
    interface names."""
    from app.services.template_service import validate_paired_template

    bad = {
        "kind": "paired",
        "nodes": [
            {"id": "vcp", "kind": "qemu", "ethernet": 1,
             "interface_naming": {"explicit": ["fxp0"]}},
        ],
        "links": [
            {"from_node": "vcp", "from_iface": "fxp0",
             "to_node": "ghost", "to_iface": "em0"},
        ],
    }
    reason = validate_paired_template(bad)
    assert reason is not None
    assert "ghost" in reason


def test_207_predictor_handles_interface_naming_format_string(patched_split):
    """Architect review fix: the predictor must mirror runtime
    _default_interfaces, which honors interface_naming.format (string
    shape) — NOT just .explicit. Otherwise a paired template using format
    gets mis-classified as invalid even though it instantiates correctly."""
    from app.services.template_service import validate_paired_template

    template = {
        "kind": "paired",
        "nodes": [
            {"id": "a", "kind": "qemu", "ethernet": 2,
             "interface_naming": {"format": "eth{n}"}},
            {"id": "b", "kind": "qemu", "ethernet": 2,
             "interface_naming": {"format": "eth{n}"}},
        ],
        "links": [
            {"from_node": "a", "from_iface": "eth0",
             "to_node": "b", "to_iface": "eth1"},
        ],
    }
    # Pre-#207-fix this returned an "interface 'eth0' not in [Gi1, Gi2]" reason
    # because the predictor only honored .explicit and fell back to Gi{n+1}.
    assert validate_paired_template(template) is None


def test_207_predictor_handles_interface_naming_format_list(patched_split):
    """The predictor must also handle the pre-normalization list shape from
    #179 (e.g. ``format: [\"fxp0\", \"ge-0/0/{n}\"]``) — it should join and
    render the same way the runtime does."""
    from app.services.template_service import validate_paired_template

    template = {
        "kind": "paired",
        "nodes": [
            {"id": "a", "kind": "qemu", "ethernet": 3,
             "interface_naming": {"format": ["fxp0", "ge-0/0/{n}"]}},
            {"id": "b", "kind": "qemu", "ethernet": 1,
             "interface_naming": {"explicit": ["em0"]}},
        ],
        "links": [
            # Link references the FIRST trailing-format port: index 1 → ge-0/0/0
            {"from_node": "a", "from_iface": "ge-0/0/0",
             "to_node": "b", "to_iface": "em0"},
        ],
    }
    assert validate_paired_template(template) is None


def test_207_predictor_overlays_explicit_onto_kind_default_base(patched_split):
    """Codex-iter2 fix: when interface_naming.explicit is shorter than
    ethernet count, runtime overlays explicit onto a kind-default base
    (qemu→Gi{n+1}, others→eth{n}) at positions 0..len(explicit)-1. The
    predictor must mirror this — pre-fix it returned just the explicit
    list and wrongly rejected links to the un-overlaid trailing
    interfaces.
    """
    from app.services.template_service import validate_paired_template

    # qemu child: ethernet=2, explicit=["mgmt0"] → ["mgmt0", "Gi2"]
    template = {
        "kind": "paired",
        "nodes": [
            {"id": "a", "kind": "qemu", "ethernet": 2,
             "interface_naming": {"explicit": ["mgmt0"]}},
            {"id": "b", "kind": "qemu", "ethernet": 1,
             "interface_naming": {"explicit": ["mgmt0"]}},
        ],
        "links": [
            # Link references the runtime-overlaid trailing iface "Gi2"
            {"from_node": "a", "from_iface": "Gi2",
             "to_node": "b", "to_iface": "mgmt0"},
        ],
    }
    assert validate_paired_template(template) is None


def test_207_predictor_overlay_matches_runtime_for_docker_kind(patched_split):
    """Same overlay semantics for non-qemu kinds (eth{n} base)."""
    from app.services.template_service import validate_paired_template

    template = {
        "kind": "paired",
        "nodes": [
            {"id": "a", "kind": "docker", "ethernet": 3,
             "interface_naming": {"explicit": ["mgmt0", "data0"]}},
            {"id": "b", "kind": "docker", "ethernet": 1,
             "interface_naming": {"explicit": ["mgmt0"]}},
        ],
        "links": [
            # Links target overlaid name (mgmt0), explicit-name (data0),
            # and the un-overlaid base (eth2).
            {"from_node": "a", "from_iface": "data0", "to_node": "b", "to_iface": "mgmt0"},
        ],
    }
    assert validate_paired_template(template) is None
    # And eth2 (the un-overlaid trailing index) should also resolve:
    template2 = dict(template)
    template2["links"] = [
        {"from_node": "a", "from_iface": "eth2", "to_node": "b", "to_iface": "mgmt0"},
    ]
    assert validate_paired_template(template2) is None


def test_207_predictor_overlay_preserves_runtime_match_for_full_explicit(patched_split):
    """Sanity: when explicit covers the full ethernet count (the existing
    juniper-vmx shape), the overlay produces the explicit list verbatim."""
    from app.services.template_service import validate_paired_template

    # Same as VMX_PAIRED_TEMPLATE shape (vfp explicit = 4 names, ethernet=4)
    assert validate_paired_template(VMX_PAIRED_TEMPLATE) is None


def test_207_predictor_format_string_still_catches_unresolvable(patched_split):
    """A format-shape template that references an interface name OUTSIDE the
    rendered set must still be flagged invalid (not silently passed)."""
    from app.services.template_service import validate_paired_template

    template = {
        "kind": "paired",
        "nodes": [
            {"id": "a", "kind": "qemu", "ethernet": 2,
             "interface_naming": {"format": "eth{n}"}},
            {"id": "b", "kind": "qemu", "ethernet": 1,
             "interface_naming": {"format": "eth{n}"}},
        ],
        "links": [
            # eth5 doesn't exist — only eth0/eth1 are rendered.
            {"from_node": "a", "from_iface": "eth5",
             "to_node": "b", "to_iface": "eth0"},
        ],
    }
    reason = validate_paired_template(template)
    assert reason is not None
    assert "eth5" in reason


# ---- #208a paired-create holds lab_lock end-to-end ----------------------


@pytest.mark.asyncio
async def test_208a_paired_create_acquires_lab_lock_around_phases(patched_split, monkeypatch):
    """The paired-create endpoint must acquire ``lab_lock`` exactly once around
    the snapshot+node-creation+link-creation+restore window. We instrument the
    lab_lock context manager and assert: (a) the outer paired-create lock was
    acquired exactly once, (b) the inner LinkService.create_link did NOT
    re-acquire it (would deadlock on Linux fcntl flock).
    """
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    acquire_count = {"n": 0}

    from app.services import lab_lock as lab_lock_module
    real_lab_lock = lab_lock_module.lab_lock

    from contextlib import contextmanager

    @contextmanager
    def counting_lab_lock(lab_id, labs_dir, timeout_s=5.0):
        acquire_count["n"] += 1
        with real_lab_lock(lab_id, labs_dir, timeout_s=timeout_s):
            yield

    # Patch the symbol everywhere the routers and services import it from.
    monkeypatch.setattr("app.routers.labs.lab_lock", counting_lab_lock)
    monkeypatch.setattr("app.services.link_service.lab_lock", counting_lab_lock)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 200, response
    # Exactly one lab_lock acquisition for the entire paired-create flow.
    # (Inner LinkService.create_link receives _lab_lock_held=True and skips its
    # own lab_lock context.)
    assert acquire_count["n"] == 1


@pytest.mark.asyncio
async def test_208a_no_deadlock_when_link_service_called_with_lab_lock_held(patched_split):
    """Smoke: invoking link_service.create_link with _lab_lock_held=True from
    inside an outer lab_lock must not block. Without the kwarg this would
    block on the inner fcntl flock acquisition (LabLockTimeout after 5s)."""
    import json
    from app.services.lab_lock import lab_lock
    from app.services.link_service import LinkService

    settings = patched_split
    # Seed a 2-node lab so the link has real endpoints to attach to.
    lab_path = settings.LABS_DIR / "demo.json"
    lab_path.write_text(json.dumps({
        "schema": 2,
        "id": "lab-208a",
        "meta": {"name": "demo"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {"id": 1, "name": "n1", "type": "qemu", "template": "csr",
                  "image": "csr", "ethernet": 1, "console": "telnet",
                  "interfaces": [{"index": 0, "name": "Gi1", "network_id": 0}]},
            "2": {"id": 2, "name": "n2", "type": "qemu", "template": "csr",
                  "image": "csr", "ethernet": 1, "console": "telnet",
                  "interfaces": [{"index": 0, "name": "Gi1", "network_id": 0}]},
        },
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }))

    link_service = LinkService()
    # Hold outer lab_lock and call create_link with _lab_lock_held=True.
    with lab_lock("demo.json", settings.LABS_DIR, timeout_s=2.0):
        link_payload, network_payload, replayed = await link_service.create_link(
            "demo.json",
            {"node_id": 1, "interface_index": 0},
            {"node_id": 2, "interface_index": 0},
            _lab_lock_held=True,
        )
    # If the lab_lock had not been short-circuited the call would have raised
    # LabLockTimeout before completing, so reaching this point IS the test.
    assert link_payload is not None
    assert replayed is False


# ---- #208b multi-link partial-failure compensation ----------------------


# Three-node paired template with TWO auto-links: a→b and a→c. When the
# second link's creation fails, the first must be torn down before snapshot
# restore so host-side attach work + implicit bridge are released.
THREE_NODE_TWO_LINK_TEMPLATE = {
    "schema": 1,
    "id": "three-node-two-link",
    "name": "Three-Node Test (2 links)",
    "vendor": "synthetic",
    "kind": "paired",
    "nodes": [
        {"id": "a", "name": "Node A", "kind": "qemu", "image": "img.qcow2",
         "cpu": 1, "ram": 512, "ethernet": 2,
         "interface_naming": {"explicit": ["eth0", "eth1"]}},
        {"id": "b", "name": "Node B", "kind": "qemu", "image": "img.qcow2",
         "cpu": 1, "ram": 512, "ethernet": 1,
         "interface_naming": {"explicit": ["eth0"]}},
        {"id": "c", "name": "Node C", "kind": "qemu", "image": "img.qcow2",
         "cpu": 1, "ram": 512, "ethernet": 1,
         "interface_naming": {"explicit": ["eth0"]}},
    ],
    "links": [
        {"from_node": "a", "from_iface": "eth0", "to_node": "b", "to_iface": "eth0"},
        {"from_node": "a", "from_iface": "eth1", "to_node": "c", "to_iface": "eth0"},
    ],
}


@pytest.mark.asyncio
async def test_208b_compensates_first_link_when_second_fails(patched_split, monkeypatch):
    """Multi-link paired-create: if link 2 fails, link 1's delete_link must
    be called (with _lab_lock_held=True) before snapshot restore so the
    implicit bridge + host-side attach work get torn down. Without
    compensation, lab.json would revert but kernel state would persist."""
    settings = patched_split
    _seed_paired_template(settings, THREE_NODE_TWO_LINK_TEMPLATE, key="three-node-two-link")
    _seed_empty_lab(settings)

    from app.services.link_service import LinkService

    real_create = LinkService.create_link
    real_delete = LinkService.delete_link

    create_calls = {"n": 0}
    delete_args: list[tuple] = []
    fake_first_link_id = "fake-first-link-id"

    async def flaky_create_link(self, lab_path, from_endpoint, to_endpoint,
                                 *, style_override=None, idempotency_key=None,
                                 _lab_lock_held=False):
        create_calls["n"] += 1
        if create_calls["n"] == 1:
            # Simulate a successful link creation; return a payload but DON'T
            # actually mutate lab.json (that's not what this test is about).
            return ({"id": fake_first_link_id, "from": from_endpoint,
                     "to": {"network_id": 999}}, None, False)
        # Second call fails — should trigger compensation of the first.
        raise RuntimeError("simulated link 2 failure")

    async def spy_delete_link(self, lab_path, link_id, *, _lab_lock_held=False):
        delete_args.append((lab_path, link_id, _lab_lock_held))
        # No-op success — the compensation contract is "best effort".
        return (True, None)

    monkeypatch.setattr(LinkService, "create_link", flaky_create_link)
    monkeypatch.setattr(LinkService, "delete_link", spy_delete_link)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="three-node-two-link"),
        current_user=_admin(),
    )

    assert response["code"] == 500, response
    assert "simulated link 2 failure" in response["message"]
    # Compensation: delete_link called once for the first (successful) link,
    # with _lab_lock_held=True (we're inside the outer lab_lock).
    assert len(delete_args) == 1, delete_args
    _, comp_link_id, comp_held = delete_args[0]
    assert comp_link_id == fake_first_link_id
    assert comp_held is True

    # Snapshot restore: lab.json has zero nodes, networks, links.
    import json
    lab_data = json.loads((settings.LABS_DIR / "demo.json").read_text())
    assert lab_data["nodes"] == {}
    assert lab_data["networks"] == {}
    assert lab_data["links"] == []

    # Restore real methods (paranoia for downstream tests, monkeypatch will
    # do this automatically but be explicit).
    monkeypatch.setattr(LinkService, "create_link", real_create)
    monkeypatch.setattr(LinkService, "delete_link", real_delete)


@pytest.mark.asyncio
async def test_208b_compensation_failure_logged_but_does_not_block_restore(
    patched_split, monkeypatch, caplog
):
    """If delete_link itself raises during compensation, the failure is
    logged and snapshot restore still proceeds — leaving the lab in the
    cleanest possible state (lab.json reverted, kernel state may need
    manual cleanup but that's surfaced in logs)."""
    import logging

    settings = patched_split
    _seed_paired_template(settings, THREE_NODE_TWO_LINK_TEMPLATE, key="three-node-two-link")
    _seed_empty_lab(settings)

    from app.services.link_service import LinkService

    create_calls = {"n": 0}

    async def flaky_create_link(self, lab_path, from_endpoint, to_endpoint,
                                 *, style_override=None, idempotency_key=None,
                                 _lab_lock_held=False):
        create_calls["n"] += 1
        if create_calls["n"] == 1:
            return ({"id": "lid1", "from": from_endpoint, "to": {"network_id": 1}}, None, False)
        raise RuntimeError("link 2 failed")

    async def angry_delete_link(self, lab_path, link_id, *, _lab_lock_held=False):
        raise RuntimeError("delete_link blew up")

    monkeypatch.setattr(LinkService, "create_link", flaky_create_link)
    monkeypatch.setattr(LinkService, "delete_link", angry_delete_link)

    with caplog.at_level(logging.WARNING):
        response = await labs.create_nodes_from_paired_template(
            "demo.json",
            NodeFromPairedTemplate(template_key="three-node-two-link"),
            current_user=_admin(),
        )

    # Endpoint still returns 500 with the original error (not the compensation error).
    assert response["code"] == 500
    assert "link 2 failed" in response["message"]
    # Compensation failure was logged but did NOT prevent snapshot restore.
    comp_warnings = [r for r in caplog.records if "compensation delete_link" in r.message]
    assert len(comp_warnings) == 1
    # Snapshot restore still happened.
    import json
    lab_data = json.loads((settings.LABS_DIR / "demo.json").read_text())
    assert lab_data["nodes"] == {}


# ---- #208e error code mapping ------------------------------------------


@pytest.mark.asyncio
async def test_208e_paired_iface_lookup_error_returns_422(patched_split, monkeypatch):
    """When _PairedIfaceLookupError raises during the link-creation phase
    (downstream of pre-flight, e.g. template was edited concurrently), the
    endpoint returns 422 (template defect, operator can fix) — not 500."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    from app.routers import labs as labs_module

    real_resolve = labs_module._resolve_iface_index

    def angry_resolve(node, iface_name):
        raise labs_module._PairedIfaceLookupError(
            f"Simulated post-pre-flight defect: interface {iface_name!r} on "
            f"node {node.get('name')!r} disappeared."
        )

    monkeypatch.setattr(labs_module, "_resolve_iface_index", angry_resolve)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 422, response
    assert "interface lookup" in response["message"].lower()
    monkeypatch.setattr(labs_module, "_resolve_iface_index", real_resolve)


@pytest.mark.asyncio
async def test_208e_duplicate_link_returns_409(patched_split, monkeypatch):
    """When LinkService.create_link raises DuplicateLinkError, the endpoint
    returns 409 (conflict, operator can resolve by removing the existing
    link or picking a different template)."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    from app.services.link_service import LinkService, DuplicateLinkError

    async def dupe_create_link(self, lab_path, from_endpoint, to_endpoint,
                                *, style_override=None, idempotency_key=None,
                                _lab_lock_held=False):
        raise DuplicateLinkError({"id": "existing-link-id"})

    monkeypatch.setattr(LinkService, "create_link", dupe_create_link)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 409, response
    assert "conflict" in response["message"].lower() or "existing" in response["message"].lower()


@pytest.mark.asyncio
async def test_208e_link_contention_returns_409(patched_split, monkeypatch):
    """Bounded-wait contention timeout (LinkContentionError) → 409, signaling
    the operator can retry."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    from app.services.link_service import LinkService, LinkContentionError

    async def contended_create_link(self, lab_path, from_endpoint, to_endpoint,
                                     *, style_override=None, idempotency_key=None,
                                     _lab_lock_held=False):
        raise LinkContentionError("simulated mutex contention")

    monkeypatch.setattr(LinkService, "create_link", contended_create_link)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 409, response
    assert "concurrent" in response["message"].lower() or "blocked" in response["message"].lower()


@pytest.mark.asyncio
async def test_208a_release_ip_does_not_deadlock_when_lab_lock_held(patched_split):
    """Architect review fix: NetworkService._release_ip used to acquire
    lab_lock unconditionally — that's a latent deadlock when called from
    inside paired-create's outer lab_lock (during compensation of an
    IPAM-bearing link). With the architect-iter1 fix, _release_ip now
    accepts _lab_lock_held=True and skips the inner acquisition.

    This test directly exercises the deadlock-prone path: hold the outer
    lab_lock, seed a network with a used IP, call _release_ip with
    _lab_lock_held=True, assert it returns within the lab_lock timeout
    (~5s) instead of timing out."""
    import json
    from app.services.lab_lock import lab_lock
    from app.services.network_service import NetworkService

    settings = patched_split
    # Seed a network with an IPv4 ``runtime.used_ips`` so _release_ip's
    # mutation path (the one that acquires lab_lock) is actually entered.
    lab_path = settings.LABS_DIR / "demo.json"
    lab_path.write_text(json.dumps({
        "schema": 2,
        "id": "lab-208a-ipam",
        "meta": {"name": "demo"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {},
        "networks": {
            "1": {
                "id": 1,
                "name": "n1",
                "type": "linux_bridge",
                "left": 0, "top": 0, "icon": "Bridge.png",
                "width": 0, "style": "", "linkstyle": "", "color": "",
                "label": "", "visibility": True, "implicit": False,
                "smart": 0, "config": {"cidr": "10.0.0.0/24"},
                "runtime": {"used_ips": ["10.0.0.5", "10.0.0.6"]},
            },
        },
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }))

    # Hold outer lab_lock and call _release_ip with _lab_lock_held=True.
    # If the kwarg isn't honored the call would block on inner fcntl flock
    # and hit LabLockTimeout (default 5s) raising LabLockTimeout.
    with lab_lock("demo.json", settings.LABS_DIR, timeout_s=2.0):
        removed = NetworkService()._release_ip(
            "demo.json", 1, "10.0.0.5", _lab_lock_held=True
        )
    assert removed is True
    # The IP was actually removed.
    after = json.loads(lab_path.read_text())
    assert after["networks"]["1"]["runtime"]["used_ips"] == ["10.0.0.6"]


@pytest.mark.asyncio
async def test_208e_unexpected_exception_still_returns_500(patched_split, monkeypatch):
    """Exception classes outside the documented mapping (operator-meaningful
    error subset) keep returning 500 so they show up in error monitoring as
    actual server errors needing investigation."""
    settings = patched_split
    _seed_paired_template(settings, VMX_PAIRED_TEMPLATE)
    _seed_empty_lab(settings)

    from app.services.link_service import LinkService

    async def angry_create_link(self, lab_path, from_endpoint, to_endpoint,
                                 *, style_override=None, idempotency_key=None,
                                 _lab_lock_held=False):
        raise RuntimeError("filesystem went on holiday")

    monkeypatch.setattr(LinkService, "create_link", angry_create_link)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 500, response
    assert "filesystem" in response["message"]


# ---- #208-MEDIUM: bad child scalars surface as 422 (template defect) ----


@pytest.mark.asyncio
async def test_208medium_bad_ethernet_scalar_returns_422_via_preflight(patched_split):
    """``ethernet: "not-an-int"`` is a template defect, not a server bug.
    The pre-flight predictor casts ethernet to int; without the #208-MEDIUM
    guard, the resulting ValueError propagates uncaught from
    validate_paired_template back to the FastAPI handler → 500. Now it
    surfaces as a reason string and the endpoint returns 422 with a
    fix-the-template message."""
    settings = patched_split
    bad = json.loads(json.dumps(VMX_PAIRED_TEMPLATE))
    bad["nodes"][1]["ethernet"] = "not-an-int"
    _seed_paired_template(settings, bad)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 422, response
    assert "malformed scalar" in response["message"]
    assert "ethernet" in response["message"]


@pytest.mark.asyncio
async def test_208medium_bad_cpu_scalar_returns_422_via_node_phase(patched_split):
    """``cpu``/``ram``/``cpulimit`` are not consulted by the pre-flight
    predictor — they only blow up in ``_build_paired_child_payload`` during
    the node-creation phase. The new ``(ValueError, TemplateError)`` handler
    in that phase maps the failure to 422 instead of 500."""
    settings = patched_split
    bad = json.loads(json.dumps(VMX_PAIRED_TEMPLATE))
    bad["nodes"][0]["cpu"] = "not-an-int"
    _seed_paired_template(settings, bad)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 422, response
    assert "malformed child data" in response["message"]


@pytest.mark.asyncio
async def test_208medium_bad_ram_scalar_returns_422_via_node_phase(patched_split):
    """Same path as bad cpu — verifies ram is also covered by the
    node-creation phase exception mapping."""
    settings = patched_split
    bad = json.loads(json.dumps(VMX_PAIRED_TEMPLATE))
    bad["nodes"][0]["ram"] = "huge"
    _seed_paired_template(settings, bad)
    _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 422, response
    assert "malformed child data" in response["message"]


@pytest.mark.asyncio
async def test_208medium_node_phase_422_path_rolls_back_lab(patched_split):
    """Bad-scalar 422 must still trigger snapshot restore — partial node
    payloads written before the failing child must not survive in lab.json."""
    settings = patched_split
    bad = json.loads(json.dumps(VMX_PAIRED_TEMPLATE))
    # Make the SECOND child fail so the first has already been written into
    # the in-memory ``nodes_map`` before the ValueError fires.
    bad["nodes"][1]["cpu"] = "not-an-int"
    _seed_paired_template(settings, bad)
    lab_path = _seed_empty_lab(settings)

    response = await labs.create_nodes_from_paired_template(
        "demo.json",
        NodeFromPairedTemplate(template_key="juniper-vmx"),
        current_user=_admin(),
    )
    assert response["code"] == 422, response

    # Snapshot restore: lab.json must be back to empty nodes/links.
    restored = json.loads(lab_path.read_text())
    assert restored.get("nodes") == {}, restored
    assert restored.get("links") == [], restored
