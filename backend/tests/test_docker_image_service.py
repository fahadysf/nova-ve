# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the docker image curation service."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Iterable

import pytest

from app.services import docker_image_service as svc


def _row(image_id: str, repo: str, tag: str, size: str = "5MB", created: str = "t") -> str:
    return (
        '{"ID":"' + image_id + '",'
        '"Repository":"' + repo + '",'
        '"Tag":"' + tag + '",'
        '"Size":"' + size + '",'
        '"CreatedAt":"' + created + '"}'
    )


def _stub_docker(monkeypatch, ls_stdout: str, recorder: list[list[str]] | None = None) -> None:
    """Make ``shutil.which('docker')`` succeed and replace subprocess.run."""
    monkeypatch.setattr(svc.shutil, "which", lambda b: "/usr/bin/docker" if b == "docker" else None)

    def fake_run(cmd, capture_output=False, text=False, check=False, env=None):
        if recorder is not None:
            recorder.append(list(cmd))
        # docker image ls
        if cmd[1:3] == ["image", "ls"]:
            return SimpleNamespace(returncode=0, stdout=ls_stdout, stderr="")
        # docker image inspect (presence check)
        if cmd[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="[{}]", stderr="")
        # docker tag / rmi / pull -- success
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)


# -------------------------------------------------------------- list/marker


def test_sanitize_repo_collapses_paths_and_lowercases():
    assert svc._sanitize_repo_for_marker("Alpine") == "alpine"
    assert svc._sanitize_repo_for_marker("library/alpine") == "library_alpine"
    assert svc._sanitize_repo_for_marker("gcr.io/foo/bar") == "gcr.io_foo_bar"
    assert svc._sanitize_repo_for_marker("registry:5000/x/y") == "registry_5000_x_y"
    assert svc._sanitize_repo_for_marker("") == "untitled"


def test_marker_for_defaults_to_latest():
    assert svc.marker_for("alpine") == "nova-ve-lab/alpine:latest"
    assert svc.marker_for("alpine:3.22") == "nova-ve-lab/alpine:3.22"
    assert svc.marker_for("gcr.io/foo:dev") == "nova-ve-lab/gcr.io_foo:dev"


def test_list_marked_image_names_returns_only_marked(monkeypatch):
    stdout = "\n".join(
        [
            _row("sha256:aa", "alpine", "3.22"),
            _row("sha256:aa", "nova-ve-lab/alpine", "3.22"),
            _row("sha256:bb", "postgres", "16-alpine"),
            _row("sha256:cc", "nova-ve/alpine-telnet", "latest"),
            # The real marker format collapses ``/`` -> ``_`` for the
            # repository segment (see _sanitize_repo_for_marker).
            _row("sha256:cc", "nova-ve-lab/nova-ve_alpine-telnet", "latest"),
        ]
    )
    _stub_docker(monkeypatch, stdout)

    names = svc.DockerImageService().list_marked_image_names()
    assert "alpine:3.22" in names
    assert "nova-ve/alpine-telnet:latest" in names
    assert "postgres:16-alpine" not in names


def test_list_marked_image_names_returns_marker_when_no_repo_tag(monkeypatch):
    """An orphan marker (original tag deleted) still surfaces.

    Without this, an operator who removed the upstream tag would lose
    visibility into the marker that's still consuming an image ID.
    """
    stdout = _row("sha256:dd", "nova-ve-lab/alpine", "3.22")
    _stub_docker(monkeypatch, stdout)
    names = svc.DockerImageService().list_marked_image_names()
    assert names == ["nova-ve-lab/alpine:3.22"]


def test_list_all_images_groups_tags_by_image_id(monkeypatch):
    stdout = "\n".join(
        [
            _row("sha256:aa", "alpine", "3.22"),
            _row("sha256:aa", "nova-ve-lab/alpine", "3.22"),
            _row("sha256:aa", "alpine", "latest"),
            _row("sha256:bb", "postgres", "16-alpine"),
        ]
    )
    _stub_docker(monkeypatch, stdout)

    records = {r.image_id: r for r in svc.DockerImageService().list_all_images()}
    assert set(records) == {"sha256:aa", "sha256:bb"}
    assert sorted(records["sha256:aa"].repo_tags) == ["alpine:3.22", "alpine:latest"]
    assert records["sha256:aa"].marker_tags == ["nova-ve-lab/alpine:3.22"]
    assert records["sha256:aa"].marked is True
    assert records["sha256:bb"].marked is False


def test_list_all_images_returns_empty_when_docker_missing(monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda _b: None)
    assert svc.DockerImageService().list_all_images() == []
    assert svc.DockerImageService().list_marked_image_names() == []


def test_console_hints_prefers_vnc_port_env(monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda b: "/usr/bin/docker" if b == "docker" else None)

    def fake_run(cmd, capture_output=False, text=False, check=False, env=None):
        if cmd[1:3] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout='[{"Config":{"Env":["VNC_PORT=5901"],"ExposedPorts":{"6901/tcp":{}}}}]',
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    assert svc.DockerImageService().console_hints("accetto/ubuntu-vnc:latest").vnc_port == 5901


def test_console_hints_uses_exposed_vnc_port(monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda b: "/usr/bin/docker" if b == "docker" else None)

    def fake_run(cmd, capture_output=False, text=False, check=False, env=None):
        if cmd[1:3] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout='[{"Config":{"Env":[],"ExposedPorts":{"5900/tcp":{},"6901/tcp":{}}}}]',
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    assert svc.DockerImageService().console_hints("nova-ve/alpine-vnc:latest").vnc_port == 5900


# ------------------------------------------------------------------ mutate


def test_mark_invokes_docker_tag_with_canonical_marker(monkeypatch):
    seen: list[list[str]] = []
    _stub_docker(monkeypatch, "", recorder=seen)

    marker = svc.DockerImageService().mark("alpine:3.22")
    assert marker == "nova-ve-lab/alpine:3.22"
    tag_cmds = [c for c in seen if c[1] == "tag"]
    assert tag_cmds == [["/usr/bin/docker", "tag", "alpine:3.22", "nova-ve-lab/alpine:3.22"]]


def test_mark_rejects_image_not_present_locally(monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda b: "/usr/bin/docker")

    def fake_run(cmd, capture_output=False, text=False, check=False, env=None):
        if cmd[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    with pytest.raises(svc.DockerImageError):
        svc.DockerImageService().mark("nope:latest")


def test_unmark_removes_only_the_marker_tag(monkeypatch):
    seen: list[list[str]] = []
    _stub_docker(monkeypatch, "", recorder=seen)

    svc.DockerImageService().unmark("alpine:3.22")
    rmi_cmds = [c for c in seen if c[1] == "rmi"]
    assert rmi_cmds == [["/usr/bin/docker", "rmi", "nova-ve-lab/alpine:3.22"]]


def test_unmark_is_idempotent_when_marker_missing(monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda b: "/usr/bin/docker")

    def fake_run(cmd, capture_output=False, text=False, check=False, env=None):
        return SimpleNamespace(returncode=1, stdout="", stderr="Error: No such image: nova-ve-lab/alpine:3.22")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    # Must NOT raise -- already removed.
    assert svc.DockerImageService().unmark("alpine:3.22") == "nova-ve-lab/alpine:3.22"


def test_unmark_accepts_marker_tag_directly(monkeypatch):
    seen: list[list[str]] = []
    _stub_docker(monkeypatch, "", recorder=seen)
    svc.DockerImageService().unmark("nova-ve-lab/alpine:3.22")
    rmi_cmds = [c for c in seen if c[1] == "rmi"]
    assert rmi_cmds == [["/usr/bin/docker", "rmi", "nova-ve-lab/alpine:3.22"]]


def test_pull_invokes_docker_pull_then_marks(monkeypatch):
    seen: list[list[str]] = []
    _stub_docker(monkeypatch, "", recorder=seen)

    result = svc.DockerImageService().pull("alpine:3.22", mark_after=True)
    assert result["marker"] == "nova-ve-lab/alpine:3.22"
    assert any(c[1] == "pull" and c[-1] == "alpine:3.22" for c in seen)
    assert any(c[1] == "tag" for c in seen)


def test_pull_can_skip_marking(monkeypatch):
    seen: list[list[str]] = []
    _stub_docker(monkeypatch, "", recorder=seen)
    result = svc.DockerImageService().pull("alpine:3.22", mark_after=False)
    assert result["marker"] is None
    assert not any(c[1] == "tag" for c in seen)


def test_parse_size_handles_units():
    assert svc._parse_size("0B") == 0
    assert svc._parse_size("5MB") == 5 * 1024 * 1024
    assert svc._parse_size("garbage") == 0
