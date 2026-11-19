# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Sandbox mode (#253): host-level operations become logged no-ops and
queries report "absent" so the UI sandbox can create networks/nodes/links
as pure data. Default (unset) behavior must be unchanged."""

from __future__ import annotations

import pytest

import app.services.host_net as host_net
from app.services.docker_image_service import DockerImageService


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_VE_SANDBOX_MODE", raising=False)


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_sandbox_mode_truthy(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_VE_SANDBOX_MODE", raw)
    assert host_net.sandbox_mode() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "garbage"])
def test_sandbox_mode_falsy(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_VE_SANDBOX_MODE", raw)
    assert host_net.sandbox_mode() is False


def test_sandbox_mode_default_off() -> None:
    assert host_net.sandbox_mode() is False


def test_invoke_helper_noop_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """The privileged helper must never be spawned in sandbox mode."""
    monkeypatch.setenv("NOVA_VE_SANDBOX_MODE", "1")

    def _explode(argv):  # any subprocess spawn is a sandbox-mode bug
        raise AssertionError(f"subprocess spawned in sandbox: {argv}")

    monkeypatch.setattr(host_net, "_run", _explode)
    proc = host_net._invoke_helper("bridge-add", "nove0001n1")
    assert proc.returncode == 0
    assert host_net.bridge_add("nove0001n1") is None


def test_bridge_add_raises_without_sandbox_when_helper_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode: unchanged behavior — missing sudo surfaces FileNotFoundError."""
    monkeypatch.setattr(
        host_net, "_sudo_bin", lambda: "/nonexistent/sandbox-test-sudo"
    )
    with pytest.raises(FileNotFoundError):
        host_net.bridge_add("nove0001n1")


def test_existence_queries_false_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_VE_SANDBOX_MODE", "1")
    assert host_net.bridge_exists("nove0001n1") is False
    assert host_net.tap_exists("nve0001d1i0") is False


def test_existence_queries_use_kernel_without_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        host_net, "_run", lambda argv: type("P", (), {"returncode": 0})()
    )
    assert host_net.bridge_exists("lo0ish") is True


def test_nat_cloud_sandbox_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """dnsmasq pid must be 0 (not an int-parse crash) and egress synthetic."""
    monkeypatch.setenv("NOVA_VE_SANDBOX_MODE", "1")
    assert host_net.default_egress_iface() == "eth0"
    assert host_net.dnsmasq_start("nove0001n2", "10.0.0.1/24", "10.0.0.10", "10.0.0.50") == 0


def test_console_proxy_start_zero_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_VE_SANDBOX_MODE", "1")
    assert host_net.console_proxy_start(1234, 2000, 3389) == 0


def test_docker_catalog_synthetic_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_VE_SANDBOX_MODE", "1")
    names = DockerImageService().list_marked_image_names()
    assert names and all(":" in name for name in names)


def test_docker_catalog_empty_without_sandbox_and_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode without a docker CLI: catalog degrades to empty (no raise)."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert DockerImageService().list_marked_image_names() == []
