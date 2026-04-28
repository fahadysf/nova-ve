# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Regression coverage for host_net wrapper drift fixes (HF7) and bridge
ownership fingerprint helpers (HF3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.services.host_net as host_net


@pytest.mark.parametrize(
    ("stderr", "expected_type"),
    [
        ("RTNETLINK answers: File exists\n", host_net.HostNetEEXIST),
        ("RTNETLINK answers: Invalid argument\n", host_net.HostNetEINVAL),
        ("operation not permitted\n", host_net.HostNetUnknown),
    ],
)
def test_classify_helper_error_matches_contract(
    stderr: str, expected_type: type[host_net.HostNetError]
) -> None:
    exc = host_net._classify_helper_error(stderr, 1)

    assert isinstance(exc, expected_type)


@pytest.mark.parametrize(
    ("wrapper_name", "args", "expected_argv"),
    [
        ("bridge_add", ("novec0de1n7",), ("bridge-add", "novec0de1n7")),
        ("bridge_del", ("novec0de1n7",), ("bridge-del", "novec0de1n7")),
        ("tap_add", ("nvec0de1d2i3",), ("tap-add", "nvec0de1d2i3")),
        ("tap_del", ("nvec0de1d2i3",), ("tap-del", "nvec0de1d2i3")),
        (
            "veth_pair_add",
            ("nvec0de1d2i3h", "nvec0de1d2i3p"),
            ("veth-pair-add", "nvec0de1d2i3h", "nvec0de1d2i3p"),
        ),
        (
            "link_master",
            ("nvec0de1d2i3h", "novec0de1n7"),
            ("link-master", "nvec0de1d2i3h", "novec0de1n7"),
        ),
        (
            "link_set_nomaster",
            ("nvec0de1d2i3h",),
            ("link-set-nomaster", "nvec0de1d2i3h"),
        ),
        ("link_netns", ("nvec0de1d2i3p", 4242), ("link-netns", "nvec0de1d2i3p", "4242")),
        ("link_up", ("nvec0de1d2i3h",), ("link-up", "nvec0de1d2i3h")),
        (
            "link_set_name_in_netns",
            (4242, "nvec0de1d2i3p", "eth3"),
            ("link-set-name-in-netns", "4242", "nvec0de1d2i3p", "eth3"),
        ),
        ("addr_up_in_netns", (4242, "eth3"), ("addr-up-in-netns", "4242", "eth3")),
        ("link_del", ("nvec0de1d2i3h",), ("tap-del", "nvec0de1d2i3h")),
        ("try_link_del", ("nvec0de1d2i3h",), ("tap-del", "nvec0de1d2i3h")),
    ],
)
def test_public_wrapper_verbs_shape_expected_helper_argv(
    monkeypatch,
    wrapper_name: str,
    args: tuple[object, ...],
    expected_argv: tuple[str, ...],
) -> None:
    recorded: list[tuple[str, ...]] = []

    def fake_invoke_helper(verb: str, *invoke_args: str) -> None:
        recorded.append((verb, *invoke_args))

    monkeypatch.setattr(host_net, "_invoke_helper", fake_invoke_helper)

    getattr(host_net, wrapper_name)(*args)

    assert recorded == [expected_argv]


def test_name_helpers_stay_within_linux_interface_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(host_net, "get_instance_id", lambda: "instance-for-hf7")

    assert len(host_net.bridge_name("lab-us-201", 99999)) <= 14
    assert len(host_net.tap_name("lab-us-201", 999, 99)) <= 14
    assert len(host_net.veth_host_name("lab-us-201", 99, 9)) <= 14
    assert len(host_net.veth_peer_name("lab-us-201", 99, 9)) <= 14


def test_get_instance_id_prefers_file_override_over_dir(monkeypatch, tmp_path: Path) -> None:
    dir_root = tmp_path / "instance-dir"
    dir_root.mkdir()
    (dir_root / "instance_id").write_text("dir-instance-id\n", encoding="ascii")

    instance_file = tmp_path / "instance-id-file"
    instance_file.write_text("file-instance-id\n", encoding="ascii")

    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_FILE", str(instance_file))
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(dir_root))

    assert host_net.get_instance_id() == "file-instance-id"


def test_get_instance_id_uses_directory_override(monkeypatch, tmp_path: Path) -> None:
    dir_root = tmp_path / "instance-dir"
    dir_root.mkdir()
    (dir_root / "instance_id").write_text("dir-instance-id\n", encoding="ascii")

    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_FILE", raising=False)
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(dir_root))

    assert host_net.get_instance_id() == "dir-instance-id"


def test_get_instance_id_uses_default_path_when_env_unset(monkeypatch, tmp_path: Path) -> None:
    default_root = tmp_path / "etc-nova-ve"
    default_root.mkdir()
    (default_root / "instance_id").write_text("default-instance-id\n", encoding="ascii")

    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_FILE", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_DIR", raising=False)
    monkeypatch.setattr(host_net, "_INSTANCE_DIR_DEFAULT", str(default_root))

    assert host_net.get_instance_id() == "default-instance-id"


def test_get_instance_id_missing_file_raises_clear_error(monkeypatch, tmp_path: Path) -> None:
    missing_file = tmp_path / "missing-instance-id"

    monkeypatch.setenv("NOVA_VE_INSTANCE_ID_FILE", str(missing_file))
    monkeypatch.delenv("NOVA_VE_INSTANCE_DIR", raising=False)

    with pytest.raises(host_net.HostNetInstanceIdMissing, match=str(missing_file)):
        host_net.get_instance_id()


# ---------------------------------------------------------------------------
# Bridge ownership fingerprint helpers (HF3 — #126)
# ---------------------------------------------------------------------------


def test_bridge_fingerprint_write_check_remove(monkeypatch, tmp_path: Path):
    fingerprint_root = tmp_path / "fingerprints"
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(fingerprint_root))

    host_net.bridge_fingerprint_write("nove1234n1", "lab-a", 1)

    path = host_net.bridge_fingerprint_path("nove1234n1")
    assert path == fingerprint_root / "nove1234n1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["lab_id"] == "lab-a"
    assert payload["network_id"] == 1
    assert payload["created_at"]
    assert host_net.bridge_fingerprint_check("nove1234n1", "lab-a", 1) == "match"

    host_net.bridge_fingerprint_remove("nove1234n1")
    assert host_net.bridge_fingerprint_check("nove1234n1", "lab-a", 1) == "absent"


def test_bridge_fingerprint_path_uses_runtime_root_override(
    monkeypatch, tmp_path: Path
):
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.delenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", raising=False)
    monkeypatch.setenv("NOVA_VE_RUNTIME_ROOT", str(runtime_root))

    assert host_net.bridge_fingerprint_path("nove1234n2") == (
        runtime_root / "bridges" / "nove1234n2.json"
    )


def test_bridge_fingerprint_check_detects_mismatch(monkeypatch, tmp_path: Path):
    fingerprint_root = tmp_path / "fingerprints"
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(fingerprint_root))

    host_net.bridge_fingerprint_write("nove1234n3", "lab-a", 3)

    assert host_net.bridge_fingerprint_check("nove1234n3", "lab-b", 3) == "mismatch"
    assert host_net.bridge_fingerprint_check("nove1234n3", "lab-a", 4) == "mismatch"


def test_bridge_fingerprint_write_is_atomic(monkeypatch, tmp_path: Path):
    fingerprint_root = tmp_path / "fingerprints"
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(fingerprint_root))

    replace_calls: list[tuple[Path, Path]] = []
    original_replace = host_net.os.replace

    def fake_replace(src: str | Path, dst: str | Path) -> None:
        replace_calls.append((Path(src), Path(dst)))
        original_replace(src, dst)

    monkeypatch.setattr(host_net.os, "replace", fake_replace)

    host_net.bridge_fingerprint_write("nove1234n4", "lab-a", 4)

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert src.parent == fingerprint_root
    assert ".tmp." in src.name
    assert dst == fingerprint_root / "nove1234n4.json"
    assert not src.exists()
    assert dst.exists()


def test_bridge_del_removes_fingerprint(monkeypatch, tmp_path: Path):
    fingerprint_root = tmp_path / "fingerprints"
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(fingerprint_root))
    calls: list[tuple[str, str]] = []

    def fake_invoke_helper(verb: str, *args: str):
        calls.append((verb, *args))
        return object()

    monkeypatch.setattr(host_net, "_invoke_helper", fake_invoke_helper)
    host_net.bridge_fingerprint_write("nove1234n5", "lab-a", 5)

    host_net.bridge_del("nove1234n5")

    assert calls == [("bridge-del", "nove1234n5")]
    assert host_net.bridge_fingerprint_check("nove1234n5", "lab-a", 5) == "absent"
