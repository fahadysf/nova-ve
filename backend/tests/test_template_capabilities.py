# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for US-305: template capabilities.{hotplug, max_nics, machine}."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.template_service import (
    TemplateError,
    TemplateService,
    _default_capabilities,
    _validate_capabilities,
)


# ---------------------------------------------------------------------------
# Unit tests for _default_capabilities (backward-compat inference)
# ---------------------------------------------------------------------------


def test_default_capabilities_docker():
    caps = _default_capabilities("docker")
    assert caps["hotplug"] is True
    assert caps["max_nics"] == 99
    assert caps["machine"] is None


def test_default_capabilities_qemu():
    caps = _default_capabilities("qemu")
    assert caps["hotplug"] is False
    assert caps["max_nics"] == 8
    assert caps["machine"] == "pc"


def test_default_capabilities_iol():
    caps = _default_capabilities("iol")
    assert caps["hotplug"] is False
    assert caps["max_nics"] == 8


# ---------------------------------------------------------------------------
# Unit tests for _validate_capabilities
# ---------------------------------------------------------------------------


def test_validate_capabilities_none_returns_defaults_for_qemu():
    caps = _validate_capabilities(None, "qemu", "test.yml")
    assert caps == {"hotplug": False, "max_nics": 8, "machine": "pc"}


def test_validate_capabilities_none_returns_defaults_for_docker():
    caps = _validate_capabilities(None, "docker", "test.yml")
    assert caps == {"hotplug": True, "max_nics": 99, "machine": None}


def test_validate_capabilities_full_qemu():
    caps = _validate_capabilities(
        {"hotplug": True, "max_nics": 8, "machine": "q35"}, "qemu", "test.yml"
    )
    assert caps["hotplug"] is True
    assert caps["max_nics"] == 8
    assert caps["machine"] == "q35"


def test_validate_capabilities_hotplug_false_with_pc_is_ok():
    caps = _validate_capabilities(
        {"hotplug": False, "max_nics": 4, "machine": "pc"}, "qemu", "test.yml"
    )
    assert caps["hotplug"] is False
    assert caps["machine"] == "pc"


def test_validate_capabilities_hotplug_true_with_pc_raises():
    with pytest.raises(TemplateError, match="hotplug=true requires machine='q35'"):
        _validate_capabilities(
            {"hotplug": True, "machine": "pc"}, "qemu", "test.yml"
        )


def test_validate_capabilities_invalid_machine_raises():
    with pytest.raises(TemplateError, match="must be one of"):
        _validate_capabilities({"machine": "i440fx"}, "qemu", "test.yml")


def test_validate_capabilities_machine_on_docker_raises():
    with pytest.raises(TemplateError, match="only valid for qemu"):
        _validate_capabilities({"machine": "q35"}, "docker", "test.yml")


def test_validate_capabilities_max_nics_over_cap_raises():
    with pytest.raises(TemplateError, match="exceeds the hard cap"):
        _validate_capabilities({"max_nics": 9}, "qemu", "test.yml")


def test_validate_capabilities_max_nics_over_cap_docker_allowed():
    caps = _validate_capabilities({"max_nics": 50}, "docker", "test.yml")
    assert caps["max_nics"] == 50


def test_validate_capabilities_max_nics_zero_raises():
    with pytest.raises(TemplateError, match="at least 1"):
        _validate_capabilities({"max_nics": 0}, "qemu", "test.yml")


def test_validate_capabilities_non_bool_hotplug_raises():
    with pytest.raises(TemplateError, match="must be a boolean"):
        _validate_capabilities({"hotplug": 1}, "qemu", "test.yml")


def test_validate_capabilities_non_int_max_nics_raises():
    with pytest.raises(TemplateError, match="must be an integer"):
        _validate_capabilities({"max_nics": "8"}, "qemu", "test.yml")


def test_validate_capabilities_not_dict_raises():
    with pytest.raises(TemplateError, match="must be an object"):
        _validate_capabilities("true", "qemu", "test.yml")


# ---------------------------------------------------------------------------
# Integration tests via TemplateService (file loading)
# ---------------------------------------------------------------------------


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        TEMPLATES_DIR=tmp_path / "templates",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        LABS_DIR=tmp_path / "labs",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        SESSION_MAX_AGE=14400,
    )
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    return TemplateService()


def test_as_response_includes_capabilities(svc, tmp_path):
    _write_text(
        svc.templates_dir / "qemu" / "vyos.yml",
        """type: qemu
name: VyOS
cpu: 1
ram: 1024
ethernet: 4
console_type: telnet
capabilities:
  hotplug: true
  max_nics: 8
  machine: q35
""",
    )
    tmpl = svc.get_template("qemu", "vyos")
    resp = tmpl.as_response()
    assert "capabilities" in resp
    caps = resp["capabilities"]
    assert caps["hotplug"] is True
    assert caps["max_nics"] == 8
    assert caps["machine"] == "q35"


def test_build_node_catalog_includes_capabilities(svc):
    _write_text(
        svc.templates_dir / "docker" / "alpine.yml",
        """type: docker
name: Alpine
cpu: 1
ram: 512
ethernet: 1
console_type: telnet
""",
    )
    catalog = svc.build_node_catalog()
    assert len(catalog["templates"]) == 1
    tmpl_entry = catalog["templates"][0]
    assert "capabilities" in tmpl_entry
    caps = tmpl_entry["capabilities"]
    # Docker defaults: hotplug=True, max_nics=99, machine=None
    assert caps["hotplug"] is True
    assert caps["max_nics"] == 99
    assert caps["machine"] is None


def test_template_without_capabilities_uses_inferred_defaults_qemu(svc):
    _write_text(
        svc.templates_dir / "qemu" / "legacy.yml",
        """type: qemu
name: Legacy Router
cpu: 1
ram: 512
ethernet: 2
console_type: telnet
""",
    )
    tmpl = svc.get_template("qemu", "legacy")
    caps = tmpl.capabilities
    # Inferred defaults: hotplug=False, max_nics=8, machine=pc
    assert caps["hotplug"] is False
    assert caps["max_nics"] == 8
    assert caps["machine"] == "pc"


def test_template_invalid_capabilities_raises_on_load(svc):
    _write_text(
        svc.templates_dir / "qemu" / "bad.yml",
        """type: qemu
name: Bad Template
cpu: 1
ram: 512
ethernet: 1
console_type: telnet
capabilities:
  hotplug: true
  machine: pc
""",
    )
    # _load_templates raises TemplateError because hotplug=True + machine=pc is inconsistent
    with pytest.raises(TemplateError, match="hotplug=true requires machine='q35'"):
        svc.get_template("qemu", "bad")


def test_list_templates_response_includes_capabilities(svc):
    _write_text(
        svc.templates_dir / "qemu" / "vyos.yml",
        """type: qemu
name: VyOS
cpu: 1
ram: 1024
ethernet: 4
console_type: telnet
capabilities:
  hotplug: true
  max_nics: 8
  machine: q35
""",
    )
    result = svc.list_templates("qemu")
    assert "vyos" in result
    assert "capabilities" in result["vyos"]
    assert result["vyos"]["capabilities"]["hotplug"] is True
