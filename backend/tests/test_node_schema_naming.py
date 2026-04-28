"""Tests for US-105 — node-type-aware interface naming (schema + router layer)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.routers.labs import _default_interfaces
from app.schemas.node import NodeBase, NodeCreate, NodeBatchCreate
from app.services.template_service import render_interface_name


# ---------------------------------------------------------------------------
# render_interface_name unit tests
# ---------------------------------------------------------------------------

class TestRenderInterfaceName:
    def test_n_placeholder_zero_based(self):
        assert render_interface_name("eth{n}", 0) == "eth0"
        assert render_interface_name("eth{n}", 3) == "eth3"

    def test_slot_alias_for_n(self):
        assert render_interface_name("Gi0/{slot}", 2) == "Gi0/2"

    def test_port_placeholder_one_based(self):
        assert render_interface_name("Gi{port}", 0) == "Gi1"
        assert render_interface_name("Gi{port}", 3) == "Gi4"

    def test_combined_placeholders(self):
        # Unlikely but supported — both {n} and {port} in same format
        assert render_interface_name("{n}-{port}", 0) == "0-1"

    def test_no_placeholder_passthrough(self):
        # A format without any placeholder is valid (already caught by _validate_interface_naming)
        # but render_interface_name doesn't add extra validation — it just returns the string.
        assert render_interface_name("loopback", 5) == "loopback"


# ---------------------------------------------------------------------------
# _default_interfaces priority tests
# ---------------------------------------------------------------------------

class TestDefaultInterfaces:
    def test_fallback_qemu_no_scheme(self):
        ifaces = _default_interfaces("qemu", 3)
        assert [i["name"] for i in ifaces] == ["Gi1", "Gi2", "Gi3"]

    def test_fallback_docker_no_scheme(self):
        ifaces = _default_interfaces("docker", 2)
        assert [i["name"] for i in ifaces] == ["eth0", "eth1"]

    def test_node_scheme_overrides_fallback(self):
        ifaces = _default_interfaces("qemu", 3, interface_naming_scheme="xe-0/0/{n}")
        assert [i["name"] for i in ifaces] == ["xe-0/0/0", "xe-0/0/1", "xe-0/0/2"]

    def test_node_scheme_overrides_template(self):
        ifaces = _default_interfaces(
            "qemu",
            2,
            interface_naming_scheme="eth{n}",
            template_interface_naming={"format": "Gi{port}"},
        )
        assert [i["name"] for i in ifaces] == ["eth0", "eth1"]

    def test_template_format_used_when_no_node_scheme(self):
        ifaces = _default_interfaces(
            "qemu",
            3,
            interface_naming_scheme=None,
            template_interface_naming={"format": "Gi{port}"},
        )
        assert [i["name"] for i in ifaces] == ["Gi1", "Gi2", "Gi3"]

    def test_template_explicit_not_used_for_generation(self):
        # template_interface_naming with 'explicit' (not 'format') should not
        # be applied by _default_interfaces; it falls back to the type default.
        ifaces = _default_interfaces(
            "qemu",
            2,
            interface_naming_scheme=None,
            template_interface_naming={"explicit": ["fa0/0", "fa0/1"]},
        )
        assert [i["name"] for i in ifaces] == ["Gi1", "Gi2"]

    def test_interface_structure(self):
        ifaces = _default_interfaces("docker", 1)
        assert ifaces[0] == {
            "index": 0,
            "name": "eth0",
            "planned_mac": None,
            "port_position": None,
            "network_id": 0,
        }

    def test_zero_count(self):
        assert _default_interfaces("qemu", 0) == []


# ---------------------------------------------------------------------------
# NodeBase / NodeCreate / NodeBatchCreate — model_validator tests
# ---------------------------------------------------------------------------

def _make_node_base(**kwargs):
    defaults = dict(
        id=1,
        name="n1",
        type="docker",
        template="alpine",
        image="alpine:latest",
        console="telnet",
    )
    defaults.update(kwargs)
    return NodeBase(**defaults)


class TestNodeBaseValidator:
    def test_docker_none_scheme_accepted(self):
        node = _make_node_base(type="docker", interface_naming_scheme=None)
        assert node.interface_naming_scheme is None

    def test_docker_eth_n_scheme_accepted(self):
        node = _make_node_base(type="docker", interface_naming_scheme="eth{n}")
        assert node.interface_naming_scheme == "eth{n}"

    def test_docker_custom_scheme_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            _make_node_base(type="docker", interface_naming_scheme="Gi{port}")
        assert "Docker nodes use eth{n} naming" in str(exc_info.value)

    def test_qemu_any_scheme_accepted(self):
        node = _make_node_base(type="qemu", interface_naming_scheme="xe-0/0/{n}")
        assert node.interface_naming_scheme == "xe-0/0/{n}"

    def test_iol_any_scheme_accepted(self):
        node = _make_node_base(type="iol", interface_naming_scheme="Gi{port}")
        assert node.interface_naming_scheme == "Gi{port}"

    def test_default_is_none(self):
        node = _make_node_base(type="qemu")
        assert node.interface_naming_scheme is None


class TestNodeCreateValidator:
    def test_docker_rejects_custom_scheme(self):
        with pytest.raises(ValidationError) as exc_info:
            NodeCreate(
                name="n1",
                type="docker",
                template="alpine",
                image="alpine:latest",
                interface_naming_scheme="Gi{port}",
            )
        assert "Docker nodes use eth{n} naming" in str(exc_info.value)

    def test_docker_accepts_eth_n(self):
        nc = NodeCreate(
            name="n1",
            type="docker",
            template="alpine",
            image="alpine:latest",
            interface_naming_scheme="eth{n}",
        )
        assert nc.interface_naming_scheme == "eth{n}"

    def test_qemu_accepts_arbitrary_scheme(self):
        nc = NodeCreate(
            name="n1",
            type="qemu",
            template="vyos",
            image="vyos-1.4",
            interface_naming_scheme="eth{n}",
        )
        assert nc.interface_naming_scheme == "eth{n}"


class TestNodeBatchCreateValidator:
    def test_docker_rejects_custom_scheme(self):
        with pytest.raises(ValidationError) as exc_info:
            NodeBatchCreate(
                name_prefix="R",
                type="docker",
                template="alpine",
                image="alpine:latest",
                interface_naming_scheme="Gi{n}",
            )
        assert "Docker nodes use eth{n} naming" in str(exc_info.value)

    def test_qemu_accepts_scheme(self):
        nb = NodeBatchCreate(
            name_prefix="R",
            type="qemu",
            template="vyos",
            image="vyos-1.4",
            interface_naming_scheme="eth{n}",
        )
        assert nb.interface_naming_scheme == "eth{n}"
