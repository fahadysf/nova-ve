# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Issue #278 — per-node side-by-side QEMU version selection.

Covers the four plumbing surfaces:

1. the version registry (scan / resolve / catalog) under ``QEMU_VERSIONS_ROOT``;
2. boot-path resolution precedence (``extras.qemu_version`` > distro default)
   including the typed "version not installed" boot error;
3. the runtime-record + command-preview payloads exposing what launched;
4. the ``GET /api/system/qemu-versions`` listing and the extras-schema
   dropdown options (dynamic, installed versions first-class).
"""

from types import SimpleNamespace

import pytest

from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services import qemu_versions
from tests.conftest import _stub_host_net_for_qemu_start
from tests.test_node_runtime import (
    _FakeProcess,
    _fake_subprocess_run_factory,
    _mock_runtime_binaries,
)


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture()
def registry_root(tmp_path, monkeypatch):
    """A populated /opt/nova-ve/qemu-style registry with 4.1.0 + 11.1.3."""
    root = tmp_path / "qemu-registry"
    for version in ("4.1.0", "11.1.3"):
        binary = root / version / "bin"
        binary.mkdir(parents=True)
        (binary / "qemu-system-x86_64").write_text("#!/bin/sh\n")
    settings = SimpleNamespace(
        QEMU_VERSIONS_ROOT=str(root),
        QEMU_BINARY="/usr/bin/qemu-system-x86_64",
    )
    monkeypatch.setattr("app.services.qemu_versions.get_settings", lambda: settings)
    return root


def _install_version(root, version: str) -> None:
    binary = root / version / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    (binary / "qemu-system-x86_64").write_text("#!/bin/sh\n")


# ---- 1. registry module -----------------------------------------------------


def test_installed_versions_scans_and_sorts_numeric_aware(registry_root):
    _install_version(registry_root, "10.2.0")
    _install_version(registry_root, "2.12.1")
    assert qemu_versions.installed_versions() == ["2.12.1", "4.1.0", "10.2.0", "11.1.3"]


def test_installed_versions_ignores_junk_and_empty_dirs(registry_root):
    (registry_root / "not-a-version").mkdir()
    (registry_root / "5.2.0").mkdir()  # no bin/qemu-system-x86_64 inside
    (registry_root / "garbage.txt").write_text("junk")
    assert qemu_versions.installed_versions() == ["4.1.0", "11.1.3"]


def test_installed_versions_empty_when_root_missing(tmp_path, monkeypatch):
    settings = SimpleNamespace(QEMU_VERSIONS_ROOT=str(tmp_path / "does-not-exist"))
    monkeypatch.setattr("app.services.qemu_versions.get_settings", lambda: settings)
    assert qemu_versions.installed_versions() == []


def test_resolve_version_binary_present_and_missing(registry_root):
    resolved = qemu_versions.resolve_version_binary("4.1.0")
    assert resolved == registry_root / "4.1.0" / "bin" / "qemu-system-x86_64"
    assert qemu_versions.resolve_version_binary("9.9.9") is None
    assert qemu_versions.resolve_version_binary("") is None


def test_catalog_shape(registry_root, monkeypatch):
    monkeypatch.setattr(
        "app.services.qemu_versions._cached_binary_version", lambda binary: "6.2.0"
    )
    payload = qemu_versions.catalog()
    assert payload["default"] == {
        "binary": "/usr/bin/qemu-system-x86_64",
        "version": "6.2.0",
    }
    assert [entry["version"] for entry in payload["versions"]] == ["4.1.0", "11.1.3"]
    assert payload["versions"][0]["binary"].endswith("/4.1.0/bin/qemu-system-x86_64")


# ---- 2. boot-path resolution ------------------------------------------------


def _runtime_settings(tmp_path, registry_root):
    return SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        QEMU_BINARY="/usr/bin/qemu-system-x86_64",
        QEMU_IMG_BINARY="/usr/bin/qemu-img",
        QEMU_VERSIONS_ROOT=str(registry_root),
        DOCKER_HOST="unix:///var/run/docker.sock",
    )


def _patch_runtime(monkeypatch, settings, recorded_popen):
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.qemu_versions.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.qemu_versions._cached_binary_version", lambda binary: "6.2.0")
    image_dir = settings.IMAGES_DIR / "qemu" / "router-image"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "hda.qcow2").write_text("base-image")

    _mock_runtime_binaries(monkeypatch)
    _stub_host_net_for_qemu_start(monkeypatch)
    monkeypatch.setattr(
        "app.services.node_runtime_service.subprocess.run",
        _fake_subprocess_run_factory([]),
    )

    def fake_popen(cmd, cwd=None, stdin=None, stdout=None, stderr=None, start_new_session=None):
        recorded_popen.append({"cmd": cmd})
        return _FakeProcess(4321)

    monkeypatch.setattr("app.services.node_runtime_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.services.node_runtime_service.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(create_time=lambda: 111.0),
    )
    monkeypatch.setattr(NodeRuntimeService, "_is_runtime_alive", lambda self, runtime: True)


def _lab_node(extras: dict) -> dict:
    return {
        "schema": 2,
        "id": "lab-278",
        "nodes": {
            "1": {
                "id": 1,
                "name": "router-1",
                "type": "qemu",
                "image": "router-image",
                "console": "telnet",
                "cpu": 1,
                "ram": 1024,
                "ethernet": 0,
                "extras": extras,
            }
        },
        "networks": {},
        "links": [],
    }


def test_boot_uses_registry_binary_for_pinned_version(monkeypatch, tmp_path, registry_root):
    settings = _runtime_settings(tmp_path, registry_root)
    recorded_popen: list = []
    _patch_runtime(monkeypatch, settings, recorded_popen)

    service = NodeRuntimeService()
    lab_data = _lab_node({"qemu_version": "4.1.0"})
    runtime = service._start_qemu_node(lab_data, lab_data["nodes"]["1"])

    cmd = recorded_popen[0]["cmd"]
    assert cmd[0] == str(registry_root / "4.1.0" / "bin" / "qemu-system-x86_64")
    assert runtime["qemu_binary"] == str(registry_root / "4.1.0" / "bin" / "qemu-system-x86_64")
    assert runtime["qemu_version"] == "4.1.0"


def test_boot_defaults_to_distro_binary_without_override(monkeypatch, tmp_path, registry_root):
    settings = _runtime_settings(tmp_path, registry_root)
    recorded_popen: list = []
    _patch_runtime(monkeypatch, settings, recorded_popen)

    service = NodeRuntimeService()
    lab_data = _lab_node({})
    runtime = service._start_qemu_node(lab_data, lab_data["nodes"]["1"])

    assert recorded_popen[0]["cmd"][0] == "/usr/bin/qemu-system-x86_64"
    assert runtime["qemu_version"] == qemu_versions.default_version()


def test_boot_missing_version_raises_typed_install_hint(monkeypatch, tmp_path, registry_root):
    settings = _runtime_settings(tmp_path, registry_root)
    recorded_popen: list = []
    _patch_runtime(monkeypatch, settings, recorded_popen)

    service = NodeRuntimeService()
    lab_data = _lab_node({"qemu_version": "9.9.9"})

    with pytest.raises(NodeRuntimeError) as exc_info:
        service._start_qemu_node(lab_data, lab_data["nodes"]["1"])
    message = str(exc_info.value)
    assert "QEMU version 9.9.9 is not installed" in message
    assert "nova-ve-qemu-9.9.9" in message
    assert recorded_popen == [], "no QEMU process may spawn for a missing version"


# ---- 3. command preview -----------------------------------------------------


def test_preview_reports_resolved_version(monkeypatch, tmp_path, registry_root):
    settings = _runtime_settings(tmp_path, registry_root)
    for path in (settings.LABS_DIR, settings.IMAGES_DIR, settings.TMP_DIR,
                 settings.TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.services.qemu_versions.default_version", lambda: "6.2.0"
    )

    service = NodeRuntimeService()
    lab_data = _lab_node({"qemu_version": "4.1.0"})
    preview = service.qemu_command_preview(lab_data, 1)

    assert preview["running"] is False
    assert preview["qemu_binary"] == str(registry_root / "4.1.0" / "bin" / "qemu-system-x86_64")
    assert preview["qemu_version"] == "4.1.0"
    assert preview["qemu_version_installed"] is True

    missing = service.qemu_command_preview(_lab_node({"qemu_version": "9.9.9"}), 1)
    assert missing["qemu_version_installed"] is False
    assert missing["qemu_version"] == "9.9.9"

    unversioned = service.qemu_command_preview(_lab_node({}), 1)
    assert unversioned["qemu_version_installed"] is None
    assert unversioned["qemu_version"] == "6.2.0"


# ---- 4. API + extras schema -------------------------------------------------


def test_system_qemu_versions_endpoint(registry_root, monkeypatch):
    """GET /api/system/qemu-versions lists installed builds + distro default."""
    import asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.dependencies import get_current_user
    from app.routers.system import router

    monkeypatch.setattr(
        "app.services.qemu_versions._cached_binary_version", lambda binary: "6.2.0"
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="tester", role="admin", html5=True, folder="/"
    )

    async def _call():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/system/qemu-versions")

    response = asyncio.run(_call())
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["default"]["version"] == "6.2.0"
    assert [entry["version"] for entry in payload["versions"]] == ["4.1.0", "11.1.3"]


def test_extras_schema_dropdown_lists_installed_versions(registry_root, monkeypatch, tmp_path):
    from app.services.template_service import _qemu_extras_schema

    monkeypatch.setattr(
        "app.services.qemu_versions._cached_binary_version", lambda binary: "6.2.0"
    )
    schema = {field["key"]: field for field in _qemu_extras_schema()}
    field = schema["qemu_version"]
    assert field["type"] == "select"
    assert field["options"][0] == {"value": "", "label": "Host default (6.2.0)"}
    assert [option["value"] for option in field["options"][1:]] == ["4.1.0", "11.1.3"]


# ---- 5. importer carry-through ----------------------------------------------


def test_generic_linux_adapter_carries_qemu_version():
    from scripts.import_eveng.adapters.generic_linux import GenericLinuxAdapter

    template = GenericLinuxAdapter().convert(
        {
            "image": "my-image",
            "name": "my-image",
            "qemu_version": "5.2.0",
            "qemu_nic": "virtio-net-pci",
        },
        "/tmp/whatever",
    )
    assert template["extras"]["qemu_version"] == "5.2.0"


def test_arista_adapter_carries_qemu_version():
    from scripts.import_eveng.adapters.arista_veos import AristaVEosAdapter

    template = AristaVEosAdapter().convert(
        {
            "image": "veos-image",
            "name": "veos-image",
            "ram": 2048,
            "cpu": 1,
            "ethernet": 2,
            "qemu_version": "4.1.0",
            "qemu_nic": "virtio-net-pci",
        },
        "/tmp/whatever",
    )
    assert template["extras"]["qemu_version"] == "4.1.0"
