# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Built-in platform image, console, and lab management defaults."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from app.services.template_service import TemplateService

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture()
def template_settings(tmp_path):
    """Local copy of the fixture from ``test_template_management.py`` —
    minimal settings shape TemplateService needs (labs/images/tmp dirs)."""
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
        SESSION_MAX_AGE=14400,
    )


@pytest.fixture()
def real_templates_service(template_settings):
    template_settings.TEMPLATES_DIR = REPO_ROOT / "backend" / "templates"
    import app.services.template_service as ts_module

    original_get_settings = ts_module.get_settings
    ts_module.get_settings = lambda: template_settings
    try:
        yield TemplateService()
    finally:
        ts_module.get_settings = original_get_settings


class TestPlatformTemplateContract:
    def test_mgmt_server_template_defaults(self, real_templates_service):
        templates = real_templates_service.list_templates("docker")
        assert "mgmt-server" in templates
        mgmt = templates["mgmt-server"]

        assert mgmt["name"] == "Mgmt Server"
        assert mgmt["console_type"] == "telnet"
        assert mgmt["second_console"] is None
        assert mgmt["cpu"] == 1
        assert mgmt["ram"] == 1024
        assert mgmt["ethernet"] == 1

    def test_browser_template_defaults(self, real_templates_service):
        templates = real_templates_service.list_templates("docker")
        assert "browser" in templates
        browser = templates["browser"]

        assert browser["name"] == "Browser"
        assert browser["console_type"] == "vnc"
        assert browser["second_console"] == "telnet"
        assert browser["cpu"] == 1
        assert browser["ram"] == 1024

    def test_platform_templates_opt_into_mgmt_access(self, real_templates_service):
        for key in ("mgmt-server", "browser", "desktop"):
            extras = real_templates_service.template_extras("docker", key)
            assert extras["mgmt_access"] == "true", key

    def test_browser_template_vnc_port_default(self, real_templates_service):
        extras = real_templates_service.template_extras("docker", "browser")
        assert extras["vnc_port"] == 5900

    def test_declared_image_short_circuits_the_catalog(
        self, real_templates_service
    ):
        """Platform templates pin their image: list_images returns exactly the
        declared image (no docker-daemon walk), so ``defaults.image`` and
        request validation resolve to the platform image."""
        resolved = real_templates_service.list_images("docker", "mgmt-server")
        assert resolved == {
            "nova-ve/mgmt-server:latest": {
                "image": "nova-ve/mgmt-server:latest",
                "source": "template-declared",
            }
        }

        resolved = real_templates_service.list_images("docker", "browser")
        assert resolved == {
            "nova-ve/browser:latest": {
                "image": "nova-ve/browser:latest",
                "source": "template-declared",
            }
        }

    def test_generic_docker_template_has_no_declared_image(
        self, real_templates_service, monkeypatch
    ):
        """The generic docker template keeps the whole marked-catalog picker;
        verify the declared short-circuit did not change it (sandbox-mode
        synthetic catalog so no docker daemon is needed)."""
        import app.services.host_net as host_net_module

        monkeypatch.setattr(host_net_module, "sandbox_mode", lambda: True)
        images = real_templates_service.list_images("docker", "docker")
        assert "nova-ve/mgmt-server:latest" in images
        assert "nova-ve/browser:latest" in images
        assert "nova-ve/desktop:latest" in images

    def test_node_catalog_prefills_platform_image(
        self, real_templates_service
    ):
        catalog = real_templates_service.build_node_catalog()
        by_key = {t["key"]: t for t in catalog["templates"]}

        assert by_key["mgmt-server"]["defaults"]["image"] == (
            "nova-ve/mgmt-server:latest"
        )
        assert by_key["mgmt-server"]["defaults"]["extras"]["mgmt_access"] == "true"

        assert by_key["browser"]["defaults"]["image"] == "nova-ve/browser:latest"
        assert by_key["browser"]["defaults"]["extras"]["mgmt_access"] == "true"
        assert by_key["browser"]["defaults"]["second_console"] == "telnet"

        desktop = by_key["desktop"]["defaults"]
        assert desktop["image"] == "nova-ve/desktop:latest"
        assert desktop["console_type"] == "rdp"
        assert desktop["second_console"] == "telnet"

    def test_validate_node_request_accepts_declared_image(
        self, real_templates_service
    ):
        template = real_templates_service.validate_node_request(
            "docker", "mgmt-server", "nova-ve/mgmt-server:latest"
        )
        assert template.key == "mgmt-server"

        with pytest.raises(Exception, match="not available for template"):
            real_templates_service.validate_node_request(
                "docker", "mgmt-server", "nginx:alpine"
            )


class TestSandboxMarkedImages:
    def test_platform_images_in_sandbox_catalog(self):
        from app.services.docker_image_service import DockerImageService

        assert "nova-ve/mgmt-server:latest" in DockerImageService._SANDBOX_MARKED_IMAGES
        assert "nova-ve/browser:latest" in DockerImageService._SANDBOX_MARKED_IMAGES
        assert "nova-ve/desktop:latest" in DockerImageService._SANDBOX_MARKED_IMAGES
