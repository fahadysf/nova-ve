"""Tests for the Juniper adapters + paired-node template result-shape (#188)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.import_eveng.adapters import (
    JuniperVMXAdapter,
    JuniperVQFXAdapter,
    JuniperVSRXAdapter,
    iter_adapters,
    select_adapter,
)
from scripts.import_eveng.adapters.base import NeedsManualReview
from scripts.import_eveng.template_schema import (
    TemplateSchemaError,
    is_paired,
    validate_template,
)


# ---- vSRX (single-node) -----------------------------------------------


def test_juniper_vsrx_matches_canonical_filename() -> None:
    a = JuniperVSRXAdapter()
    assert a.match({"image": "media-vsrx-vmdisk-15.1X49.qcow2"}) is True
    assert a.match({"image": "media-vsrx-vmdisk-3-15.1X49-D80.qcow2"}) is True


def test_juniper_vsrx_rejects_other_juniper_images() -> None:
    a = JuniperVSRXAdapter()
    assert a.match({"image": "vmx-bundle-22.4R1.qcow2"}) is False
    assert a.match({"image": "vqfx-10000-re-bsd.qcow2"}) is False


def test_juniper_vsrx_convert_emits_single_node_template() -> None:
    a = JuniperVSRXAdapter()
    raw = {"image": "media-vsrx-vmdisk-15.1X49.qcow2", "ram": 4096, "cpu": 2}
    out = a.convert(raw, Path("."))
    assert out["kind"] == "qemu"
    assert out["vendor"] == "juniper"
    assert is_paired(out) is False
    validate_template(out)  # must be valid single-node


# ---- vMX (paired-node) ------------------------------------------------


def test_juniper_vmx_matches_bundle_filenames() -> None:
    a = JuniperVMXAdapter()
    assert a.match({"image": "vmx-bundle-22.4R1.S1.qcow2"}) is True
    assert a.match({"image": "vmx-vcp-22.4.qcow2"}) is True
    # Match also fires when an explicit vmx_paired flag is set:
    assert a.match({"image": "anything", "vmx_paired": True}) is True


def test_juniper_vmx_convert_emits_paired_template_with_vcp_vfp_link() -> None:
    a = JuniperVMXAdapter()
    raw = {
        "image": "vmx-bundle-22.4R1.qcow2",
        "image_vcp": "vmx-vcp-22.4.qcow2",
        "image_vfp": "vmx-vfp-22.4.qcow2",
        "ram_vcp": 2048,
        "ram_vfp": 4096,
    }
    out = a.convert(raw, Path("."))
    assert is_paired(out) is True
    assert len(out["nodes"]) == 2
    node_ids = {n["id"] for n in out["nodes"]}
    assert node_ids == {"vcp", "vfp"}
    assert len(out["links"]) >= 1
    link = out["links"][0]
    assert link["from_node"] == "vcp"
    assert link["to_node"] == "vfp"
    assert link["from_iface"] == "fxp0"
    assert link["to_iface"] == "em0"
    validate_template(out)  # paired template must validate


def test_juniper_vmx_validate_raises_on_missing_paired_fields() -> None:
    """Without image_vcp / image_vfp / ram_vcp / ram_vfp, validate() raises."""
    a = JuniperVMXAdapter()
    with pytest.raises(NeedsManualReview):
        a.convert({"image": "vmx-bundle-22.4.qcow2"}, Path("."))


# ---- vQFX (paired-node) ------------------------------------------------


def test_juniper_vqfx_matches_canonical_filename() -> None:
    a = JuniperVQFXAdapter()
    assert a.match({"image": "vqfx-10000-re-bsd-20180228.qcow2"}) is True
    assert a.match({"image": "vqfx10k-re-19.4R1.qcow2"}) is True


def test_juniper_vqfx_rejects_unrelated_images() -> None:
    a = JuniperVQFXAdapter()
    assert a.match({"image": "vmx-bundle-22.4.qcow2"}) is False
    assert a.match({"image": "media-vsrx-vmdisk-15.1.qcow2"}) is False


def test_juniper_vqfx_convert_emits_paired_re_pfe_template() -> None:
    a = JuniperVQFXAdapter()
    raw = {
        "image": "vqfx-10000-re-bsd-20180228.qcow2",
        "image_re": "vqfx-10000-re.qcow2",
        "image_pfe": "vqfx-10000-pfe.qcow2",
        "ram_re": 1024,
        "ram_pfe": 2048,
    }
    out = a.convert(raw, Path("."))
    assert is_paired(out) is True
    assert len(out["nodes"]) == 2
    node_ids = {n["id"] for n in out["nodes"]}
    assert node_ids == {"re", "pfe"}
    validate_template(out)


# ---- template_schema validator ----------------------------------------


def test_template_schema_accepts_valid_single_node() -> None:
    template = {
        "schema": 1,
        "id": "vyos",
        "kind": "qemu",
        "image": "vyos-1.4.qcow2",
    }
    validate_template(template)  # no raise


def test_template_schema_rejects_missing_image_in_single_node() -> None:
    with pytest.raises(TemplateSchemaError):
        validate_template({"schema": 1, "id": "x", "kind": "qemu"})


def test_template_schema_rejects_unknown_kind() -> None:
    with pytest.raises(TemplateSchemaError):
        validate_template({"schema": 1, "id": "x", "kind": "exotic", "image": "y"})


def test_template_schema_rejects_unsupported_schema_version() -> None:
    with pytest.raises(TemplateSchemaError):
        validate_template({"schema": 99, "id": "x", "kind": "qemu", "image": "y"})


def test_template_schema_rejects_paired_with_too_few_nodes() -> None:
    template = {
        "schema": 1,
        "id": "x",
        "kind": "paired",
        "nodes": [{"id": "only", "kind": "qemu"}],
        "links": [],
    }
    with pytest.raises(TemplateSchemaError):
        validate_template(template)


def test_template_schema_rejects_paired_without_links_list() -> None:
    template = {
        "schema": 1,
        "id": "x",
        "kind": "paired",
        "nodes": [
            {"id": "a", "kind": "qemu"},
            {"id": "b", "kind": "qemu"},
        ],
    }
    with pytest.raises(TemplateSchemaError):
        validate_template(template)


def test_existing_single_node_templates_still_validate_back_compat() -> None:
    """Back-compat regression: single-node templates from #186/#187/#189 must still pass."""
    cisco_iosv = {
        "schema": 1,
        "id": "vios-adventerprise.qcow2",
        "kind": "qemu",
        "image": "vios-adventerprise-15.6.qcow2",
        "ram": 512,
    }
    arista_veos = {
        "schema": 1,
        "id": "vEOS-lab-4.21.qcow2",
        "kind": "qemu",
        "image": "vEOS-lab-4.21.qcow2",
        "ram": 2048,
    }
    cisco_iol = {
        "schema": 1,
        "id": "i86bi-linux-l3.bin",
        "kind": "iol",
        "image": "i86bi-linux-l3.bin",
        "ram": 1024,
    }
    for t in (cisco_iosv, arista_veos, cisco_iol):
        validate_template(t)  # no raise


# ---- registry integration --------------------------------------------


def test_all_three_juniper_adapters_register_before_generic_linux() -> None:
    names = [a.name for a in iter_adapters()]
    expected = {"juniper_vsrx", "juniper_vmx", "juniper_vqfx"}
    assert expected <= set(names)
    assert names[-1] == "generic_linux"
    for name in expected:
        assert names.index(name) < names.index("generic_linux")


def test_select_adapter_routes_vsrx_to_juniper() -> None:
    matched = select_adapter({"image": "media-vsrx-vmdisk-15.1X49.qcow2"})
    assert matched is not None and matched.name == "juniper_vsrx"


def test_select_adapter_routes_vmx_to_juniper() -> None:
    matched = select_adapter({"image": "vmx-bundle-22.4R1.qcow2"})
    assert matched is not None and matched.name == "juniper_vmx"


def test_select_adapter_routes_vqfx_to_juniper() -> None:
    matched = select_adapter({"image": "vqfx-10000-re-bsd-20180228.qcow2"})
    assert matched is not None and matched.name == "juniper_vqfx"
