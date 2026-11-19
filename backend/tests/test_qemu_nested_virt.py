# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Issue #249 — nested-virtualization opt-in on the QEMU boot path.

vEOS-class guests run their own hypervisor (KVM-in-KVM); nova-ve masks
``vmx``/``svm`` off by default so such guest data planes can never start.
With ``extras.nested_virt`` set under ``accel=kvm``, the boot command must
expose the host flags (``-cpu host``) instead. The TCG path keeps
``-cpu max`` regardless.
"""

from types import SimpleNamespace

import pytest

from app.services.node_runtime_service import NodeRuntimeService


@pytest.fixture()
def runtime_settings(tmp_path):
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
    )


@pytest.fixture()
def patched_settings(monkeypatch, runtime_settings):
    monkeypatch.setattr(
        "app.services.node_runtime_service.get_settings", lambda: runtime_settings
    )
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: runtime_settings)
    return runtime_settings


def _veos_node() -> dict:
    return {
        "id": 1,
        "name": "veos-1",
        "type": "qemu",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 2,
        "firstmac": "50:00:00:05:00:00",
        "interfaces": [
            {"index": 0, "name": "Et1"},
            {"index": 1, "name": "Et2"},
        ],
    }


def _build_cmd(monkeypatch, patched_settings, extras: dict, accel: str) -> list[str]:
    monkeypatch.setattr("app.services.node_runtime_service._qemu_accel", lambda: accel)
    service = NodeRuntimeService()
    return service._build_qemu_base_cmd(
        node=_veos_node(),
        extras=extras,
        lab_id="lab-249",
        qemu_binary="qemu-system-x86_64",
        console_port=55587,
        overlay_path="/tmp/overlay.qcow2",
        qmp_path="/tmp/qmp.sock",
        tap_names={0: "tap0", 1: "tap1"},
        attachment_by_index={},
    )


def _cpu_arg(cmd: list[str]) -> str:
    return cmd[cmd.index("-cpu") + 1]


def test_kvm_default_masks_nested_virt(monkeypatch, patched_settings) -> None:
    cmd = _build_cmd(monkeypatch, patched_settings, {}, accel="kvm")
    assert _cpu_arg(cmd) == "host,vmx=off,svm=off"


def test_kvm_nested_virt_optin_exposes_host_cpu(monkeypatch, patched_settings) -> None:
    cmd = _build_cmd(monkeypatch, patched_settings, {"nested_virt": "true"}, accel="kvm")
    assert _cpu_arg(cmd) == "host"


def test_kvm_nested_virt_false_keeps_masking(monkeypatch, patched_settings) -> None:
    cmd = _build_cmd(monkeypatch, patched_settings, {"nested_virt": "false"}, accel="kvm")
    assert _cpu_arg(cmd) == "host,vmx=off,svm=off"


def test_tcg_path_ignores_nested_virt(monkeypatch, patched_settings) -> None:
    cmd = _build_cmd(monkeypatch, patched_settings, {"nested_virt": "true"}, accel="tcg")
    assert _cpu_arg(cmd) == "max"
