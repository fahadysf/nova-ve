# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Secondary console support (#268).

Covers the three backend layers the feature touches:

* schema validation — a template/node may declare a second console mode
  alongside the primary; invalid combinations are rejected;
* template surface — ``second_console`` is an editable template field and
  flows through to nodes created from the template;
* runtime — the QEMU boot path allocates and exposes a second console port,
  the runtime record persists both, the argv parse-back recovers both, and
  the HTML5 endpoint selects the requested session.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.routers.labs import (
    _build_node_payload,
    _validate_node_update_request,
)
from app.schemas.node import (
    NodeBase,
    NodeBatchCreate,
    NodeCreate,
    NodeFromTemplate,
    NodeUpdate,
)
from app.services.host_net import console_proxy_log_id
from app.services.image_library_service import ImageLibraryService, TemplateUpdateInvalid
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.template_service import TemplateError, TemplateService
from tests.conftest import _stub_host_net_for_qemu_start
from tests.test_node_runtime import _FakeProcess, _fake_subprocess_run_factory, _mock_runtime_binaries


@pytest.fixture()
def runtime_settings(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    return settings


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestNodeCreateSecondConsole:
    def test_default_is_none(self):
        node = NodeCreate(name="n1", type="qemu", template="t", image="i")
        assert node.second_console is None

    def test_qemu_accepts_vnc_secondary_on_telnet_primary(self):
        node = NodeCreate(
            name="n1", type="qemu", template="t", image="i",
            console="telnet", second_console="vnc",
        )
        assert node.second_console == "vnc"

    def test_qemu_accepts_telnet_secondary_on_vnc_primary(self):
        node = NodeCreate(
            name="n1", type="qemu", template="t", image="i",
            console="vnc", second_console="telnet",
        )
        assert node.second_console == "telnet"

    def test_secondary_must_differ_from_primary(self):
        with pytest.raises(ValidationError) as exc_info:
            NodeCreate(
                name="n1", type="qemu", template="t", image="i",
                console="telnet", second_console="telnet",
            )
        assert "must differ" in str(exc_info.value)

    def test_qemu_rejects_rdp_secondary(self):
        with pytest.raises(ValidationError) as exc_info:
            NodeCreate(
                name="n1", type="qemu", template="t", image="i",
                console="telnet", second_console="rdp",
            )
        assert "Secondary console mode 'rdp' is not supported for qemu" in str(exc_info.value)

    def test_telnet_only_types_allow_no_secondary(self):
        # iol/dynamips only support telnet, so any secondary is rejected.
        with pytest.raises(ValidationError) as exc_info:
            NodeCreate(
                name="n1", type="iol", template="t", image="i",
                console="telnet", second_console="vnc",
            )
        assert "Secondary console mode 'vnc' is not supported for iol" in str(exc_info.value)

    def test_docker_accepts_rdp_secondary(self):
        node = NodeCreate(
            name="n1", type="docker", template="t", image="i",
            console="telnet", second_console="rdp",
        )
        assert node.second_console == "rdp"


class TestNodeBatchCreateSecondConsole:
    def test_accepts_secondary(self):
        node = NodeBatchCreate(
            name_prefix="R", type="qemu", template="t", image="i",
            console="telnet", second_console="vnc",
        )
        assert node.second_console == "vnc"

    def test_rejects_same_as_primary(self):
        with pytest.raises(ValidationError) as exc_info:
            NodeBatchCreate(
                name_prefix="R", type="qemu", template="t", image="i",
                console="vnc", second_console="vnc",
            )
        assert "must differ" in str(exc_info.value)


class TestNodeFromTemplateSecondConsole:
    def test_accepts_secondary_override(self):
        request = NodeFromTemplate(
            template_type="qemu", template_key="t", name="n1", image="i",
            console="telnet", second_console="vnc",
        )
        assert request.second_console == "vnc"

    def test_rejects_secondary_conflicting_with_default_primary(self):
        # No explicit primary: the template default (telnet) is the primary.
        with pytest.raises(ValidationError):
            NodeFromTemplate(
                template_type="qemu", template_key="t", name="n1", image="i",
                second_console="telnet",
            )


class TestNodeSerialPrimarySecondary:
    def test_paired_child_serial_primary_accepts_vnc_secondary(self):
        node = NodeBase(
            id=1, type="qemu", console="serial", second_console="vnc",
        )
        assert node.second_console == "vnc"

    def test_paired_child_serial_primary_accepts_telnet_secondary(self):
        node = NodeBase(id=1, type="qemu", console="serial", second_console="telnet")
        assert node.second_console == "telnet"

    def test_paired_child_serial_primary_rejects_serial_secondary(self):
        # ``serial`` is excluded from the secondary allowlist at the schema
        # level (a secondary is always one of telnet/vnc/rdp).
        with pytest.raises(ValidationError):
            NodeBase(id=1, type="qemu", console="serial", second_console="serial")

    def test_node_update_accepts_second_console(self):
        update = NodeUpdate(second_console="vnc")
        assert update.second_console == "vnc"

    def test_node_update_allows_clearing_via_none(self):
        update = NodeUpdate.model_validate({"second_console": None})
        assert update.second_console is None


# ---------------------------------------------------------------------------
# Node payload building + update validation (labs router)
# ---------------------------------------------------------------------------


class _FakeTemplate:
    def __init__(self, console_type="telnet", second_console=None):
        self.console_type = console_type
        self.second_console = second_console
        self.ethernet = 2
        self.cpu = 1
        self.ram = 1024
        self.cpulimit = 1
        self.icon_type = "router"
        self.interface_naming = {}


class TestBuildNodePayloadSecondConsole:
    def _request(self, **kwargs):
        defaults = dict(
            name="n1", type="qemu", template="veos", image="veos-image",
        )
        defaults.update(kwargs)
        return NodeCreate(**defaults)

    def test_template_secondary_flows_into_payload(self):
        request = self._request()
        payload = _build_node_payload(
            node_id=1, request=request, template=_FakeTemplate(second_console="vnc"),
            name="n1", left=0, top=0,
        )
        assert payload["second_console"] == "vnc"

    def test_request_override_wins(self):
        request = self._request(second_console="vnc")
        payload = _build_node_payload(
            node_id=1, request=request, template=_FakeTemplate(second_console=None),
            name="n1", left=0, top=0,
        )
        assert payload["second_console"] == "vnc"

    def test_no_secondary_anywhere_keeps_none(self):
        request = self._request()
        payload = _build_node_payload(
            node_id=1, request=request, template=_FakeTemplate(),
            name="n1", left=0, top=0,
        )
        assert payload["second_console"] is None

    def test_create_validation_rejects_template_conflict(self):
        # A template that declares a secondary equal to its own primary is
        # caught at create time even though the schema-level pair (request
        # fields only) would pass.
        from app.routers.labs import _validate_create_console_request

        request = self._request()
        error = _validate_create_console_request(
            request, _FakeTemplate(console_type="telnet", second_console="telnet")
        )
        assert error is not None
        assert "must differ" in error


class TestValidateNodeUpdateSecondConsole:
    def _node(self, **overrides):
        node = {
            "id": 1,
            "name": "n1",
            "type": "qemu",
            "console": "telnet",
            "second_console": None,
        }
        node.update(overrides)
        return node

    def test_valid_secondary_accepted(self):
        error = _validate_node_update_request(
            self._node(), NodeUpdate(second_console="vnc"), node_running=False
        )
        assert error is None

    def test_secondary_same_as_stored_primary_rejected(self):
        error = _validate_node_update_request(
            self._node(), NodeUpdate(second_console="telnet"), node_running=False
        )
        assert error is not None
        assert "must differ" in error

    def test_secondary_rejected_for_unsupported_type(self):
        error = _validate_node_update_request(
            self._node(), NodeUpdate(second_console="rdp"), node_running=False
        )
        assert error is not None
        assert "Secondary console mode 'rdp' is not supported for qemu" in error

    def test_secondary_validated_against_new_primary_when_both_change(self):
        error = _validate_node_update_request(
            self._node(console="telnet"),
            NodeUpdate(console="vnc", second_console="telnet"),
            node_running=False,
        )
        assert error is None

    def test_secondary_conflicting_with_new_primary_rejected(self):
        error = _validate_node_update_request(
            self._node(console="telnet"),
            NodeUpdate(console="vnc", second_console="vnc"),
            node_running=False,
        )
        assert error is not None
        assert "must differ" in error

    def test_serial_primary_allows_any_secondary(self):
        error = _validate_node_update_request(
            self._node(console="serial"), NodeUpdate(second_console="rdp"), node_running=False
        )
        assert error is None

    def test_second_console_is_stopped_only_field(self):
        # Changing a running node's secondary console must be refused like the
        # primary console field.
        error = _validate_node_update_request(
            self._node(), NodeUpdate(second_console="vnc"), node_running=True
        )
        assert error is not None
        assert "Stop the node" in error


# ---------------------------------------------------------------------------
# Template surface
# ---------------------------------------------------------------------------


def _seed_template(tmp_path, extra_yaml=""):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        USER_TEMPLATES_DIR=tmp_path / "user-templates",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR, settings.USER_TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    (settings.TEMPLATES_DIR / "qemu").mkdir(parents=True, exist_ok=True)
    (settings.TEMPLATES_DIR / "qemu" / "veos.yml").write_text(
        "type: qemu\nname: vEOS\ncpu: 1\nram: 2048\nethernet: 4\n"
        "console_type: telnet\nicon_type: router\ncpulimit: 1\n"
        f"{extra_yaml}"
    )
    return settings


class TestTemplateSecondConsole:
    def test_template_loads_second_console(self, tmp_path, monkeypatch):
        settings = _seed_template(tmp_path, "second_console: vnc\n")
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        template = TemplateService().get_template("qemu", "veos")
        assert template.second_console == "vnc"
        response = template.as_response()
        assert response["second_console"] == "vnc"

    def test_template_without_second_console_is_none(self, tmp_path, monkeypatch):
        settings = _seed_template(tmp_path)
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        template = TemplateService().get_template("qemu", "veos")
        assert template.second_console is None

    def test_invalid_secondary_mode_rejected_at_load(self, tmp_path, monkeypatch):
        settings = _seed_template(tmp_path, "second_console: serial\n")
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        with pytest.raises(TemplateError):
            TemplateService().get_template("qemu", "veos")

    def test_secondary_equal_to_primary_rejected_at_load(self, tmp_path, monkeypatch):
        settings = _seed_template(tmp_path, "second_console: telnet\n")
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        with pytest.raises(TemplateError):
            TemplateService().get_template("qemu", "veos")

    def test_secondary_not_supported_for_type_rejected_at_load(self, tmp_path, monkeypatch):
        settings = _seed_template(tmp_path, "second_console: rdp\n")
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        with pytest.raises(TemplateError):
            TemplateService().get_template("qemu", "veos")

    def test_catalog_defaults_expose_second_console(self, tmp_path, monkeypatch):
        settings = _seed_template(tmp_path, "second_console: vnc\n")
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        catalog = TemplateService().build_node_catalog()
        veos = next(t for t in catalog["templates"] if t["key"] == "veos")
        assert veos["defaults"]["second_console"] == "vnc"

    def test_paired_child_second_console_loads(self, tmp_path, monkeypatch):
        settings = SimpleNamespace(
            LABS_DIR=tmp_path / "labs",
            IMAGES_DIR=tmp_path / "images",
            TMP_DIR=tmp_path / "tmp",
            TEMPLATES_DIR=tmp_path / "templates",
            USER_TEMPLATES_DIR=tmp_path / "user-templates",
        )
        for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                     settings.TEMPLATES_DIR, settings.USER_TEMPLATES_DIR):
            path.mkdir(parents=True, exist_ok=True)
        (settings.USER_TEMPLATES_DIR / "juniper-vmx.yml").write_text(
            "kind: paired\nname: vMX\n"
            "nodes:\n"
            "  - id: vcp\n    kind: qemu\n    name: VCP\n    console: serial\n"
            "    second_console: vnc\n    image: vmx-vcp\n"
            "  - id: vfp\n    kind: qemu\n    name: VFP\n    console: serial\n"
            "    image: vmx-vfp\n"
            "links: []\n"
        )
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        template = TemplateService().get_template("qemu", "juniper-vmx__vcp")
        assert template.console_type == "serial"
        assert template.second_console == "vnc"


class TestUpdateTemplateSecondConsole:
    def _settings(self, tmp_path):
        settings = _seed_template(tmp_path)
        return settings

    def test_edit_sets_second_console(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr("app.services.image_library_service.get_settings", lambda: settings)
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        updated = ImageLibraryService().update_template("qemu", "veos", {"second_console": "vnc"})
        assert updated["second_console"] == "vnc"
        # The shadow YAML carries the field.
        shadow = (settings.USER_TEMPLATES_DIR / "veos.yml").read_text()
        assert "second_console: vnc" in shadow

    def test_edit_clears_second_console_with_none(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr("app.services.image_library_service.get_settings", lambda: settings)
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        service = ImageLibraryService()
        service.update_template("qemu", "veos", {"second_console": "vnc"})
        updated = service.update_template("qemu", "veos", {"second_console": None})
        assert updated["second_console"] is None

    def test_edit_rejects_invalid_mode(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr("app.services.image_library_service.get_settings", lambda: settings)
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        with pytest.raises(TemplateUpdateInvalid):
            ImageLibraryService().update_template("qemu", "veos", {"second_console": "serial"})

    def test_edit_rejects_combination_conflict(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr("app.services.image_library_service.get_settings", lambda: settings)
        monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
        with pytest.raises(TemplateUpdateInvalid) as exc_info:
            ImageLibraryService().update_template("qemu", "veos", {"second_console": "telnet"})
        assert "must differ" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Runtime: QEMU command matrix, port allocation, record, parse-back
# ---------------------------------------------------------------------------


def _qemu_node(console="telnet", second_console=None):
    return {
        "id": 1,
        "name": "node-1",
        "type": "qemu",
        "console": console,
        "second_console": second_console,
        "cpu": 1,
        "ram": 1024,
        "ethernet": 0,
    }


def _build_cmd(monkeypatch, node, console_port, second_console_port=None):
    monkeypatch.setattr("app.services.node_runtime_service._qemu_accel", lambda: "tcg")
    service = NodeRuntimeService()
    return service._build_qemu_base_cmd(
        node=node,
        extras={},
        lab_id="lab-268",
        qemu_binary="qemu-system-x86_64",
        console_port=console_port,
        overlay_path="/tmp/overlay.qcow2",
        qmp_path="/tmp/qmp.sock",
        tap_names={},
        attachment_by_index={},
        second_console_mode=node.get("second_console"),
        second_console_port=second_console_port,
    )


def _flag_values(cmd, flag):
    return [cmd[i + 1] for i, token in enumerate(cmd) if token == flag]


class TestQemuBaseCmdSecondaryMatrix:
    pytestmark = pytest.mark.usefixtures("runtime_settings")
    def test_telnet_primary_vnc_secondary_emits_both(self, monkeypatch):
        cmd = _build_cmd(monkeypatch, _qemu_node("telnet", "vnc"), 23000, 5905)
        assert _flag_values(cmd, "-serial") == ["telnet:0.0.0.0:23000,server,nowait"]
        assert _flag_values(cmd, "-vnc") == ["0.0.0.0:5"]

    def test_vnc_primary_telnet_secondary_emits_both(self, monkeypatch):
        cmd = _build_cmd(monkeypatch, _qemu_node("vnc", "telnet"), 5903, 23001)
        assert _flag_values(cmd, "-vnc") == ["0.0.0.0:3"]
        assert _flag_values(cmd, "-serial") == ["telnet:0.0.0.0:23001,server,nowait"]

    def test_serial_primary_telnet_secondary_emits_two_serials(self, monkeypatch):
        cmd = _build_cmd(monkeypatch, _qemu_node("serial", "telnet"), 23002, 23003)
        assert _flag_values(cmd, "-serial") == [
            "telnet:0.0.0.0:23002,server,nowait",
            "telnet:0.0.0.0:23003,server,nowait",
        ]
        assert not _flag_values(cmd, "-vnc")

    def test_serial_primary_vnc_secondary_emits_serial_and_vnc(self, monkeypatch):
        cmd = _build_cmd(monkeypatch, _qemu_node("serial", "vnc"), 23004, 5907)
        assert _flag_values(cmd, "-serial") == ["telnet:0.0.0.0:23004,server,nowait"]
        assert _flag_values(cmd, "-vnc") == ["0.0.0.0:7"]

    def test_no_secondary_keeps_single_console(self, monkeypatch):
        cmd = _build_cmd(monkeypatch, _qemu_node("telnet"), 23010)
        assert _flag_values(cmd, "-serial") == ["telnet:0.0.0.0:23010,server,nowait"]
        assert not _flag_values(cmd, "-vnc")

    def test_range_ports_use_display_arithmetic(self, monkeypatch):
        """#276: vnc range ports land on the allocated TCP port through
        display arithmetic — display = port - 5900 — instead of the legacy
        5900-5999 display pool."""
        cmd = _build_cmd(monkeypatch, _qemu_node("vnc", "vnc"), 25905, 26007)
        assert _flag_values(cmd, "-vnc") == ["0.0.0.0:20005", "0.0.0.0:20107"]


class TestParseBackSecondaryPorts:
    pytestmark = pytest.mark.usefixtures("runtime_settings")
    def _parse(self, node, cmdline, pid=4321):
        return NodeRuntimeService._runtime_from_qemu_cmdline(
            lab_id="lab-268",
            node_id=1,
            node=node,
            cmdline=cmdline,
            pid=pid,
            create_time=111.0,
        )

    def test_telnet_primary_vnc_secondary(self):
        record = self._parse(
            _qemu_node("telnet", "vnc"),
            ["qemu-system-x86_64", "-serial", "telnet::23000,server,nowait",
             "-vnc", ":5", "-qmp", "unix:/tmp/qmp.sock,server,nowait"],
        )
        assert record["console_port"] == 23000
        assert record["second_console"] == "vnc"
        assert record["second_console_port"] == 5905

    def test_vnc_primary_telnet_secondary(self):
        record = self._parse(
            _qemu_node("vnc", "telnet"),
            ["qemu-system-x86_64", "-vnc", ":3", "-serial",
             "telnet::23001,server,nowait"],
        )
        assert record["console_port"] == 5903
        assert record["second_console"] == "telnet"
        assert record["second_console_port"] == 23001

    def test_serial_primary_telnet_secondary(self):
        record = self._parse(
            _qemu_node("serial", "telnet"),
            ["qemu-system-x86_64", "-serial", "telnet::23002,server,nowait",
             "-serial", "telnet::23003,server,nowait"],
        )
        assert record["console_port"] == 23002
        assert record["second_console"] == "telnet"
        assert record["second_console_port"] == 23003

    def test_no_secondary_stays_none(self):
        record = self._parse(
            _qemu_node("telnet"),
            ["qemu-system-x86_64", "-serial", "telnet::23010,server,nowait"],
        )
        assert record["console_port"] == 23010
        assert record["second_console"] is None
        assert record["second_console_port"] is None

    def test_wildcard_binds_parse_primary_and_secondary(self):
        """#269 union: the explicit wildcard forms emitted while
        EXTERNAL_CONSOLE_HOST is set must parse back on restart, for both
        the primary and the secondary listener."""
        record = self._parse(
            _qemu_node("telnet", "vnc"),
            ["qemu-system-x86_64", "-serial", "telnet:0.0.0.0:23020,server,nowait",
             "-vnc", "0.0.0.0:6", "-qmp", "unix:/tmp/qmp.sock,server,nowait"],
        )
        assert record["console_port"] == 23020
        assert record["second_console"] == "vnc"
        assert record["second_console_port"] == 5906

    def test_wildcard_telnet_secondary_parses(self):
        record = self._parse(
            _qemu_node("vnc", "telnet"),
            ["qemu-system-x86_64", "-vnc", "0.0.0.0:2",
             "-serial", "telnet:0.0.0.0:23021,server,nowait"],
        )
        assert record["console_port"] == 5902
        assert record["second_console"] == "telnet"
        assert record["second_console_port"] == 23021


# ---------------------------------------------------------------------------
# Runtime: console_info mode selection + start-path dual allocation
# ---------------------------------------------------------------------------


def _seed_runtime_record(service, record):
    service._registry[service._key(record["lab_id"], record["node_id"])] = record


class TestConsoleInfoMode:
    pytestmark = pytest.mark.usefixtures("runtime_settings")
    def _lab_data(self, second_console="vnc"):
        node = {
            "id": 1,
            "name": "router-1",
            "type": "qemu",
            "console": "telnet",
            "second_console": second_console,
        }
        return {"schema": 2, "id": "lab-268", "nodes": {"1": node}}

    def test_primary_mode_unchanged(self, monkeypatch):
        service = NodeRuntimeService()
        monkeypatch.setattr(NodeRuntimeService, "_is_runtime_alive", lambda self, runtime: True)
        _seed_runtime_record(service, {
            "lab_id": "lab-268", "node_id": 1, "kind": "qemu",
            "console": "telnet", "console_port": 23000,
        })
        info = service.console_info(self._lab_data(), 1)
        assert info["console"] == "telnet"
        assert info["port"] == 23000
        assert info["mode"] == "primary"
        assert info["name"] == "router-1"

    def test_second_mode_returns_secondary_session(self, monkeypatch):
        service = NodeRuntimeService()
        monkeypatch.setattr(NodeRuntimeService, "_is_runtime_alive", lambda self, runtime: True)
        _seed_runtime_record(service, {
            "lab_id": "lab-268", "node_id": 1, "kind": "qemu",
            "console": "telnet", "console_port": 23000,
            "second_console": "vnc", "second_console_port": 5905,
        })
        info = service.console_info(self._lab_data(), 1, mode="second")
        assert info["console"] == "vnc"
        assert info["port"] == 5905
        assert info["mode"] == "second"
        assert info["name"] == "router-1-second"

    def test_second_mode_without_secondary_errors(self, monkeypatch):
        service = NodeRuntimeService()
        monkeypatch.setattr(NodeRuntimeService, "_is_runtime_alive", lambda self, runtime: True)
        _seed_runtime_record(service, {
            "lab_id": "lab-268", "node_id": 1, "kind": "qemu",
            "console": "telnet", "console_port": 23000,
        })
        with pytest.raises(NodeRuntimeError) as exc_info:
            service.console_info(self._lab_data(), 1, mode="second")
        assert "no secondary console" in str(exc_info.value)


@pytest.mark.asyncio
async def test_qemu_start_allocates_second_console_port(
    monkeypatch, tmp_path
):
    """Starting a node that declares a secondary console boots QEMU with BOTH
    console backends and stores both ports in the runtime record."""
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)

    image_dir = settings.IMAGES_DIR / "qemu" / "router-image"
    image_dir.mkdir(parents=True)
    (image_dir / "hda.qcow2").write_text("base-image")

    lab_data = {
        "schema": 2,
        "id": "lab-268",
        "nodes": {
            "1": {
                "id": 1,
                "name": "router-1",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "second_console": "vnc",
                "cpu": 1,
                "ram": 1024,
                "ethernet": 0,
            }
        },
        "networks": {},
        "links": [],
    }

    _mock_runtime_binaries(monkeypatch)
    _stub_host_net_for_qemu_start(monkeypatch)
    recorded_runs = []
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run",
        _fake_subprocess_run_factory(recorded_runs),
    )
    recorded_popen = []

    def fake_popen(cmd, cwd=None, stdin=None, stdout=None, stderr=None, start_new_session=None):
        recorded_popen.append({"cmd": cmd})
        return _FakeProcess(4321)

    monkeypatch.setattr("app.services.node_runtime_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.services.node_runtime_service.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(create_time=lambda: 111.0),
    )
    monkeypatch.setattr(NodeRuntimeService, "_is_runtime_alive", lambda self, runtime: True)

    service = NodeRuntimeService()
    runtime = service._start_qemu_node(lab_data, lab_data["nodes"]["1"])

    cmd = recorded_popen[0]["cmd"]
    serial_values = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-serial"]
    vnc_values = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-vnc"]
    # #276: both listeners bind the wildcard explicitly on their fixed-range
    # public ports, regardless of EXTERNAL_CONSOLE_HOST.
    assert serial_values == [f"telnet:0.0.0.0:{runtime['console_port']},server,nowait"]
    assert vnc_values == [f"0.0.0.0:{runtime['second_console_port'] - 5900}"]
    assert runtime["console"] == "telnet"
    assert runtime["second_console"] == "vnc"
    assert runtime["second_console_port"] != runtime["console_port"]
    # Ports come from the per-protocol ranges: telnet 15000-20000, vnc
    # 25000-30000 (the SimpleNamespace settings stub omits the range keys,
    # so the documented defaults apply).
    assert 15000 <= runtime["console_port"] <= 20000
    assert 25000 <= runtime["second_console_port"] <= 30000

    # The record round-trips through the persisted state file.
    service._persist_runtime(runtime)
    reloaded = json.loads(
        service._state_path("lab-268", 1).read_text()
    )
    assert reloaded["second_console"] == "vnc"
    assert reloaded["second_console_port"] == runtime["second_console_port"]


@pytest.mark.asyncio
async def test_qemu_start_rejects_rdp_secondary(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)

    image_dir = settings.IMAGES_DIR / "qemu" / "router-image"
    image_dir.mkdir(parents=True)
    (image_dir / "hda.qcow2").write_text("base-image")

    lab_data = {
        "schema": 2,
        "id": "lab-268",
        "nodes": {
            "1": {
                "id": 1,
                "name": "router-1",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "second_console": "rdp",
                "cpu": 1,
                "ram": 1024,
                "ethernet": 0,
            }
        },
        "networks": {},
        "links": [],
    }

    _mock_runtime_binaries(monkeypatch)
    _stub_host_net_for_qemu_start(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run",
        _fake_subprocess_run_factory([]),
    )

    service = NodeRuntimeService()
    with pytest.raises(NodeRuntimeError) as exc_info:
        service._start_qemu_node(lab_data, lab_data["nodes"]["1"])
    assert "QEMU RDP consoles are not supported" in str(exc_info.value)


# ---------------------------------------------------------------------------
# HTML5 endpoint console-mode selector
# ---------------------------------------------------------------------------
# Docker dual console ports
# ---------------------------------------------------------------------------


def _docker_second_console_lab() -> dict:
    return {
        "schema": 2,
        "id": "lab-268-docker",
        "nodes": {
            "1": {
                "id": 1,
                "name": "alpine-1",
                "type": "docker",
                "image": "nova-ve/alpine-telnet:latest",
                "console": "telnet",
                "second_console": "vnc",
                "cpu": 1,
                "ram": 256,
                "ethernet": 1,
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None},
                ],
            }
        },
        "networks": {},
        "links": [],
    }


def _patch_docker_psutil(monkeypatch):
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(create_time=lambda: 222.0),
    )


@pytest.fixture()
def _docker_instance_id(monkeypatch, tmp_path):
    """Seed the instance_id + runtime pid registry under tmp_path (same
    setup the docker tests in test_node_runtime rely on)."""
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance_id").write_text("test-instance-203")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    pids_path = tmp_path / "runtime" / "pids.json"
    pids_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(pids_path))


@pytest.mark.parametrize(("image", "primary", "secondary", "targets", "rdp_only_browser"), [
    ("alpine:latest", "telnet", "vnc", (23, 5900), False),
    ("nova-ve/browser:latest", "rdp", "telnet", (3389, 23), True),
    ("nova-ve/browser:latest", "telnet", "rdp", (23, 3389), True),
    ("nova-ve/browser:latest", "rdp", "vnc", (3389, 5900), False),
    ("example.com/browser:latest", "rdp", "telnet", (3389, 23), False),
])
def test_docker_start_maps_and_proxies_second_console(
    monkeypatch, tmp_path, _docker_instance_id, image, primary, secondary, targets, rdp_only_browser,
):
    """A docker node with a secondary console publishes and proxies a second
    host port into the container's secondary console service (#268)."""
    from tests.test_node_runtime import _us203_helper_mock, _us204_docker_run_factory

    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)

    lab_data = _docker_second_console_lab()
    lab_data["nodes"]["1"].update(image=image, console=primary, second_console=secondary)
    containers: dict[str, dict[str, object]] = {}
    _mock_runtime_binaries(monkeypatch)
    _us203_helper_mock(monkeypatch)
    _patch_docker_psutil(monkeypatch)
    proxy_calls: list[dict[str, int]] = []

    def fake_proxy_start(**kwargs):
        proxy_calls.append(dict(kwargs))
        return 9000 + len(proxy_calls)

    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.console_proxy_start", fake_proxy_start
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run",
        _us204_docker_run_factory(containers),
    )

    service = NodeRuntimeService()
    runtime = service._start_docker_node(lab_data, lab_data["nodes"]["1"])

    run_cmd = runtime["command"]
    publish = [
        run_cmd[i + 1]
        for i, tok in enumerate(run_cmd)
        if tok == "-p"
    ]
    assert publish == [
        f"{runtime['console_port']}:{targets[0]}",
        f"{runtime['second_console_port']}:{targets[1]}",
    ]
    assert ("NOVA_VE_BROWSER_VNC=0" in run_cmd) == rdp_only_browser
    assert runtime["console"] == primary
    assert runtime["second_console"] == secondary
    assert runtime["second_console_port"] != runtime["console_port"]
    assert runtime["second_console_proxy_pid"] == 9002
    assert len(proxy_calls) == 2
    assert proxy_calls[0]["listen_port"] == runtime["console_port"]
    assert proxy_calls[0]["target_port"] == targets[0]
    assert proxy_calls[1]["listen_port"] == runtime["second_console_port"]
    assert proxy_calls[1]["target_port"] == targets[1]

    # The record round-trips through the persisted state file.
    service._persist_runtime(runtime)
    reloaded = json.loads(service._state_path("lab-268-docker", 1).read_text())
    assert reloaded["second_console"] == secondary
    assert reloaded["second_console_port"] == runtime["second_console_port"]


def test_docker_console_info_second_mode_resolves_secondary_session(
    monkeypatch, tmp_path, _docker_instance_id
):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)

    lab_data = _docker_second_console_lab()
    _mock_runtime_binaries(monkeypatch)
    lab_data["nodes"]["1"].setdefault("extras", {})["vnc_port"] = 5901
    respawns: list[dict[str, int | str]] = []

    def fake_proxy_start(**kwargs):
        respawns.append(dict(kwargs))
        return 9100 + len(respawns)

    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.console_proxy_start", fake_proxy_start
    )
    # The primary proxy is alive; the secondary one is dead and must respawn.
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.console_proxy_alive",
        lambda pid: pid == 8000,
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.console_proxy_stop",
        lambda pid: None,
    )
    # The container-liveness probe inspects /proc/<pid>, which does not exist
    # on macOS — treat it as present.
    real_Path = Path

    def fake_Path(arg):
        if str(arg).startswith("/proc/"):
            return SimpleNamespace(is_dir=lambda: True)
        return real_Path(arg)

    monkeypatch.setattr("app.services.node_runtime_service.Path", fake_Path)

    service = NodeRuntimeService()
    # Keep the seeded record alive without a real docker daemon.
    monkeypatch.setattr(
        NodeRuntimeService, "_is_docker_running", staticmethod(lambda _runtime: True)
    )
    with service._lock:
        service._registry[service._key("lab-268-docker", 1)] = {
            "lab_id": "lab-268-docker",
            "node_id": 1,
            "kind": "docker",
            "console": "telnet",
            "console_port": 23001,
            "second_console": "vnc",
            "second_console_port": 5905,
            "console_proxy_pid": 8000,
            "second_console_proxy_pid": None,
            "pid": 4242,
            "started_at": 1.0,
        }

    primary = service.console_info(lab_data, 1)
    assert primary["port"] == 23001
    assert primary["console"] == "telnet"

    second = service.console_info(lab_data, 1, mode="second")
    assert second["mode"] == "second"
    assert second["console"] == "vnc"
    assert second["port"] == 5905
    assert second["name"] == "alpine-1-second"
    # The dead secondary proxy was re-spawned into the container netns.
    assert respawns == [{
        "node_pid": 4242, "listen_port": 5905, "target_port": 5901,
        "log_id": console_proxy_log_id("lab-268-docker", 1, "second"),
    }]
    with service._lock:
        assert (
            service._registry[service._key("lab-268-docker", 1)]["second_console_proxy_pid"]
            == 9101
        )


def test_docker_stop_stops_both_console_proxies(monkeypatch, tmp_path, _docker_instance_id):
    from tests.test_node_runtime import _us203_helper_mock, _us204_docker_run_factory

    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)

    lab_data = _docker_second_console_lab()
    containers: dict[str, dict[str, object]] = {}
    _mock_runtime_binaries(monkeypatch)
    _us203_helper_mock(monkeypatch)
    _patch_docker_psutil(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.console_proxy_start",
        lambda **_kwargs: 8123,
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run",
        _us204_docker_run_factory(containers),
    )
    stopped_pids: list[int] = []
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.console_proxy_stop",
        lambda pid: stopped_pids.append(pid),
    )

    service = NodeRuntimeService()
    runtime = service._start_docker_node(lab_data, lab_data["nodes"]["1"])
    # ``_start_docker_node`` is the raw builder; ``stop_node`` reads the
    # registry, so seed it with the freshly built record.
    with service._lock:
        service._registry[service._key("lab-268-docker", 1)] = runtime
    service.stop_node(lab_data, 1)

    assert sorted(stopped_pids) == sorted(
        [runtime["console_proxy_pid"], runtime["second_console_proxy_pid"]]
    )


def test_docker_reconcile_restores_secondary_proxy(monkeypatch, tmp_path, _docker_instance_id):
    """Backend restart adoption rebuilds the secondary console session from
    the node declaration (#268)."""
    from tests.test_node_runtime import _us203_helper_mock, _us204_docker_run_factory

    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)

    lab_data = _docker_second_console_lab()
    container_name = "nova-ve-lab268docker-1"
    containers: dict[str, dict[str, object]] = {
        container_name: {"running": True, "pid": 7100}
    }
    _mock_runtime_binaries(monkeypatch)
    _us203_helper_mock(monkeypatch)
    _patch_docker_psutil(monkeypatch)
    proxy_calls: list[dict[str, int]] = []

    def fake_proxy_start(**kwargs):
        proxy_calls.append(dict(kwargs))
        return 9200 + len(proxy_calls)

    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.console_proxy_start", fake_proxy_start
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run",
        _us204_docker_run_factory(containers),
    )

    service = NodeRuntimeService()
    runtime = service._reconcile_docker_node_runtime(lab_data, lab_data["nodes"]["1"])

    assert runtime is not None
    assert runtime["second_console"] == "vnc"
    assert runtime["second_console_port"] != runtime["console_port"]
    assert runtime["second_console_proxy_pid"] == 9202
    assert len(proxy_calls) == 2
    assert proxy_calls[0]["listen_port"] == runtime["console_port"]
    assert proxy_calls[1]["listen_port"] == runtime["second_console_port"]
    assert proxy_calls[1]["target_port"] == 5900


# ---------------------------------------------------------------------------


@pytest.fixture()
def html5_lab(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        GUACAMOLE_DATABASE_URL="",
        GUACAMOLE_DATA_SOURCE="postgresql",
        GUACAMOLE_INTERNAL_URL="http://127.0.0.1:8081/html5/",
        GUACAMOLE_JSON_SECRET_KEY="4c0b569e4c96df157eee1b65dd0e4d41",
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
    )
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.routers.labs.get_settings", lambda: settings)

    lab_data = {
        "schema": 2,
        "id": "lab-268",
        "nodes": {
            "1": {
                "id": 1,
                "name": "router-1",
                "type": "qemu",
                "console": "telnet",
                "second_console": "vnc",
            }
        },
        "networks": {},
        "links": [],
    }
    (settings.LABS_DIR / "sample.json").write_text(json.dumps(lab_data))
    return settings


@pytest.mark.asyncio
async def test_html5_route_rejects_unknown_console_mode(html5_lab):
    from app.routers import labs

    response = await labs.node_html5(
        "sample.json", 1, console="third",
        current_user=SimpleNamespace(username="admin", html5=True, pod=0),
    )
    assert response["code"] == 400
    assert "expected 'primary' or 'second'" in response["message"]


@pytest.mark.asyncio
async def test_html5_route_second_mode_uses_secondary_session(html5_lab, monkeypatch):
    from app.routers import labs

    requested_modes = []

    def fake_console_info(_lab_data, _node_id, host="127.0.0.1", mode="primary"):
        requested_modes.append(mode)
        if mode == "second":
            return {
                "name": "router-1-second",
                "console": "vnc",
                "host": "127.0.0.1",
                "port": 5905,
                "mode": mode,
                "url": "/html5/#/client/demo",
            }
        return {
            "name": "router-1",
            "console": "telnet",
            "host": "127.0.0.1",
            "port": 23000,
            "mode": mode,
            "url": "/html5/#/client/demo",
        }

    monkeypatch.setattr(
        "app.routers.labs.NodeRuntimeService",
        lambda: SimpleNamespace(console_info=fake_console_info),
    )

    async def fake_console_url(*_args, **_kwargs):
        return "/html5/#/client/test-client?token=test-token"

    monkeypatch.setattr(
        "app.services.html5_service.Html5SessionService.create_console_url",
        fake_console_url,
    )

    response = await labs.node_html5(
        "sample.json", 1, console="second",
        current_user=SimpleNamespace(username="admin", html5=True, pod=0),
    )
    assert requested_modes == ["second"]
    assert response.status_code == 307
    assert response.headers["location"] == "/html5/#/client/test-client?token=test-token"

    # Default stays primary.
    response = await labs.node_html5(
        "sample.json", 1,
        current_user=SimpleNamespace(username="admin", html5=True, pod=0),
    )
    assert requested_modes == ["second", "primary"]
    assert response.status_code == 307


@pytest.mark.asyncio
async def test_html5_route_second_mode_without_secondary_returns_error(html5_lab, monkeypatch):
    from app.routers import labs

    def fake_console_info(_lab_data, _node_id, host="127.0.0.1", mode="primary"):
        if mode == "second":
            raise NodeRuntimeError("Node has no secondary console session: 1")
        return {
            "name": "router-1",
            "console": "telnet",
            "host": "127.0.0.1",
            "port": 23000,
            "mode": mode,
            "url": "/html5/#/client/demo",
        }

    monkeypatch.setattr(
        "app.routers.labs.NodeRuntimeService",
        lambda: SimpleNamespace(console_info=fake_console_info),
    )

    response = await labs.node_html5(
        "sample.json", 1, console="second",
        current_user=SimpleNamespace(username="admin", html5=True, pod=0),
    )
    assert response["code"] == 400
    assert "no secondary console" in response["message"]
