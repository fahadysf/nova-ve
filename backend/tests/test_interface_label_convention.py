# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Interface label convention for the built-in templates.

A port label carries at most four letters before its first digit, slash or
dot, and no space, so that every label stays readable on a node card. The
templates use the short form the platform itself documents where there is
one (``Gi``, ``Et``, ``Ma``, ``Mg``, ``GE``) and a chosen short form
otherwise (``vmn``, ``vtn``, ``e``).

These tests read the REAL built-in templates through the same validation the
runtime applies (mirroring ``test_platform_templates.py``).
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest

from app.routers.labs import _default_interfaces
from app.services.template_service import TemplateService, render_interface_name

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE_TYPES = ("qemu", "iol", "dynamips", "docker")


@pytest.fixture()
def template_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    for path in (labs_dir, images_dir, tmp_dir, templates_dir):
        path.mkdir()
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


def _schemes(service: TemplateService):
    """``(type, key, format)`` for every built-in template that names its ports."""
    for template_type in TEMPLATE_TYPES:
        for key in sorted(service.list_templates(template_type)):
            fmt = service.interface_naming(template_type, key).get("format")
            if fmt:
                yield template_type, key, fmt


def _labels(fmt: str) -> list[str]:
    """Every fixed name plus the first three names the trailing format yields."""
    fixed = len([item for item in str(fmt).split(",") if item.strip()]) - 1
    return [render_interface_name(fmt, index) for index in range(fixed + 3)]


def _offence(label: str) -> str | None:
    """Why ``label`` breaks the convention; ``None`` when it complies.

    Only the text before the first digit, slash or dot is measured, so a note
    that follows a name (``eth0 / mgmt``) does not count. A name with no
    numbering at all (``mgmt``) is not measured either.
    """
    head = re.split(r"[\d/.]", label, maxsplit=1)[0]
    if head == label:
        return None
    if re.search(r"\s", head):
        return f"space inside the name: {label!r}"
    if len(re.sub(r"[^A-Za-z]", "", head)) > 4:
        return f"more than four letters before the numbering: {label!r}"
    return None


def test_every_builtin_label_keeps_to_four_leading_letters(real_templates_service):
    offences = [
        f"{template_type}/{key}: {reason}"
        for template_type, key, fmt in _schemes(real_templates_service)
        for label in _labels(fmt)
        if (reason := _offence(label))
    ]

    assert offences == []


def test_templates_without_a_scheme_fall_back_to_short_names():
    for node_type in ("qemu", "iol", "dynamips", "docker"):
        names = [iface["name"] for iface in _default_interfaces(node_type, 6)]
        assert names == [name for name in names if _offence(name) is None], node_type


@pytest.mark.parametrize(
    ("key", "index", "expected"),
    [
        # Cisco IOS / IOS-XE
        ("csr", 0, "Gi0/0/0"),
        ("ise", 0, "Gi0"),
        ("viosl2", 7, "Gi1/3"),
        ("asav", 0, "Ma0/0"),
        ("ftd7", 1, "Diag0/0"),
        # Cisco NX-OS and IOS-XR print their own short forms
        ("nxosv9k", 1, "Eth1/1"),
        ("xrv", 0, "Mg0/0/CPU0/0"),
        ("xrv9k", 1, "int1"),
        # Arista
        ("veos", 0, "Ma1"),
        ("veos", 1, "Et1/1"),
        # Palo Alto and Dell
        ("paloalto", 1, "e1/1"),
        ("dellos10", 0, "eth1/1/1"),
        # Aruba and Huawei spell gigabit ports GE
        ("aruba", 1, "GE0/0/0"),
        ("huaweiusg6kv", 1, "GE1/0/0"),
        # No platform short form exists for these
        ("mikrotik", 0, "e1"),
        ("esxi", 0, "vmn0"),
        ("pfsense", 0, "vtn0"),
        ("win", 0, "Eth1"),
    ],
)
def test_short_forms_match_the_platform_convention(real_templates_service, key, index, expected):
    fmt = real_templates_service.interface_naming("qemu", key)["format"]

    assert render_interface_name(fmt, index) == expected
