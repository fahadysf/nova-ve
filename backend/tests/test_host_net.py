# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Regression coverage for host_net wrapper drift fixes (HF7) and bridge
ownership fingerprint helpers (HF3)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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
        ("qemu_process_signal", (4242, "term"), ("qemu-process-signal", "4242", "term")),
        ("link_del", ("nvec0de1d2i3h",), ("link-del", "nvec0de1d2i3h")),
        ("try_link_del", ("nvec0de1d2i3h",), ("link-del", "nvec0de1d2i3h")),
        (
            "bridge_addr_del",
            ("novec0de1n7", "10.40.1.254/24"),
            ("bridge-addr-del", "novec0de1n7", "10.40.1.254/24"),
        ),
        ("bridge_guard_apply", ("novec0de1n7",), ("bridge-guard-apply", "novec0de1n7")),
        ("bridge_guard_remove", ("novec0de1n7",), ("bridge-guard-remove", "novec0de1n7")),
        (
            "dnsmasq_start_isolated",
            ("novec0de1n7", "10.40.1.254", "10.40.1.128", "10.40.1.253"),
            (
                "dnsmasq-start",
                "novec0de1n7",
                "10.40.1.254",
                "10.40.1.128",
                "10.40.1.253",
                "--no-router",
            ),
        ),
    ],
)
def test_public_wrapper_verbs_shape_expected_helper_argv(
    monkeypatch,
    wrapper_name: str,
    args: tuple[object, ...],
    expected_argv: tuple[str, ...],
) -> None:
    recorded: list[tuple[str, ...]] = []

    def fake_invoke_helper(verb: str, *invoke_args: str) -> SimpleNamespace:
        recorded.append((verb, *invoke_args))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(host_net, "_invoke_helper", fake_invoke_helper)

    getattr(host_net, wrapper_name)(*args)

    assert recorded == [expected_argv]


def test_qemu_process_signal_rejects_unknown_signal() -> None:
    with pytest.raises(ValueError):
        host_net.qemu_process_signal(4242, "hup")


def test_name_helpers_stay_within_linux_interface_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(host_net, "get_instance_id", lambda: "instance-for-hf7")

    assert len(host_net.bridge_name("lab-us-201", 99999)) <= 14
    assert len(host_net.tap_name("lab-us-201", 999, 99)) <= 14
    assert len(host_net.veth_host_name("lab-us-201", 99, 9)) <= 14
    assert len(host_net.veth_peer_name("lab-us-201", 99, 9)) <= 14


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


# ---------------------------------------------------------------------------
# Bridge-Cloud: driver-aware dispatch + host-bridge refusals
# ---------------------------------------------------------------------------


def test_link_master_host_invokes_helper_verb(monkeypatch) -> None:
    recorded: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        host_net,
        "_invoke_helper",
        lambda verb, *a: recorded.append((verb, *a)),
    )

    host_net.link_master_host("nvec0de1d2i3h", "br-eth0")

    assert recorded == [("link-master-host", "nvec0de1d2i3h", "br-eth0")]


def test_link_master_any_routes_nove_to_lab_helper(monkeypatch) -> None:
    lab_calls: list[tuple[str, str]] = []
    host_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        host_net, "link_master", lambda i, b: lab_calls.append((i, b))
    )
    monkeypatch.setattr(
        host_net, "link_master_host", lambda i, b: host_calls.append((i, b))
    )

    # Driver value irrelevant when bridge matches the lab regex.
    host_net.link_master_any("nvec0de1d2i3h", "nove1234n7", driver=None)
    host_net.link_master_any("nvec0de1d2i3h", "nove1234n7", driver="bridge_cloud")

    assert lab_calls == [("nvec0de1d2i3h", "nove1234n7")] * 2
    assert host_calls == []


def test_link_master_any_routes_br_eth_to_host_helper_with_correct_driver(monkeypatch) -> None:
    host_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(host_net, "link_master", lambda *_a: pytest.fail("lab helper not allowed"))
    monkeypatch.setattr(
        host_net, "link_master_host", lambda i, b: host_calls.append((i, b))
    )

    host_net.link_master_any("nvec0de1d2i3h", "br-eth0", driver="bridge_cloud")

    assert host_calls == [("nvec0de1d2i3h", "br-eth0")]


@pytest.mark.parametrize("wrong_driver", [None, "", "nat_cloud", "linux_bridge", "bogus"])
def test_link_master_any_refuses_br_eth_with_wrong_driver(monkeypatch, wrong_driver) -> None:
    monkeypatch.setattr(host_net, "link_master", lambda *_a: pytest.fail("must not call"))
    monkeypatch.setattr(host_net, "link_master_host", lambda *_a: pytest.fail("must not call"))

    with pytest.raises(host_net.HostNetError) as ei:
        host_net.link_master_any("nvec0de1d2i3h", "br-eth0", driver=wrong_driver)

    assert "without driver=bridge_cloud" in str(ei.value)


def test_link_master_any_defaults_unknown_to_lab_helper(monkeypatch) -> None:
    """For names that match neither the host-bridge nor lab-bridge
    regex, dispatch falls through to ``link_master``.  The privileged
    helper itself validates against the lab regex and rejects unknown
    names there.  Defense-in-depth: keep this dispatcher narrowly
    scoped to the Bridge-Cloud cross-domain rule.
    """
    lab_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        host_net, "link_master", lambda i, b: lab_calls.append((i, b))
    )
    monkeypatch.setattr(
        host_net, "link_master_host", lambda *_a: pytest.fail("must not call"),
    )

    host_net.link_master_any("nvec0de1d2i3h", "docker0", driver="bridge_cloud")

    assert lab_calls == [("nvec0de1d2i3h", "docker0")]


def test_bridge_del_refuses_br_eth(monkeypatch) -> None:
    monkeypatch.setattr(
        host_net,
        "_invoke_helper",
        lambda *_a: pytest.fail("helper must not be invoked for host bridges"),
    )
    monkeypatch.setattr(
        host_net,
        "bridge_fingerprint_remove",
        lambda *_a: pytest.fail("fingerprint must not be touched for host bridges"),
    )

    with pytest.raises(host_net.HostNetError) as ei:
        host_net.bridge_del("br-eth0")

    assert "host-owned" in str(ei.value)


def test_host_bridge_name_regex_rejects_injection() -> None:
    # Accepted shapes
    assert host_net.RE_HOST_BRIDGE_NAME.match("br-eth0")
    assert host_net.RE_HOST_BRIDGE_NAME.match("br-eth10")
    # Rejected shapes
    for bad in (
        "nove1234n1",
        "br-foo",
        "br-eth-0",
        "br-eth0; rm -rf /",
        "BR-ETH0",
        " br-eth0",
        "br-eth0 ",
        "br-eth0\n",
        "",
    ):
        assert not host_net.RE_HOST_BRIDGE_NAME.match(bad), bad


# ---------------------------------------------------------------------------
# link_state (#277): zero-subprocess sysfs read backing the reconcile
# check-before-write guards.
# ---------------------------------------------------------------------------
def _make_sysfs_net(tmp_path: Path, links: dict[str, tuple[str | None, str]]) -> Path:
    """Build a synthetic /sys/class/net tree.

    ``links`` maps interface name -> (master bridge name or None, flags hex).
    A master is represented as a real symlink (the production code reads it
    with os.readlink), like the kernel does.
    """
    base = tmp_path / "sysfs" / "class" / "net"
    for name, (master, flags) in links.items():
        dev = base / name
        dev.mkdir(parents=True)
        (dev / "flags").write_text(flags)
        if master is not None:
            (base / master).mkdir(parents=True, exist_ok=True)
            (dev / "master").symlink_to(base / master)
    return base


def test_link_state_reports_mastered_up_interface(tmp_path, monkeypatch) -> None:
    base = _make_sysfs_net(
        tmp_path, {"nve1234n1i0": ("nve1lab-net1", "0x1103")}  # IFF_UP set
    )
    monkeypatch.setattr(host_net, "_SYSFS_NET", base)
    assert host_net.link_state("nve1234n1i0") == (True, "nve1lab-net1", True)


def test_link_state_reports_masterless_down_interface(tmp_path, monkeypatch) -> None:
    base = _make_sysfs_net(tmp_path, {"nve1234n1i0": (None, "0x1102")})  # IFF_UP clear
    monkeypatch.setattr(host_net, "_SYSFS_NET", base)
    assert host_net.link_state("nve1234n1i0") == (True, None, False)


def test_link_state_missing_interface(tmp_path, monkeypatch) -> None:
    base = _make_sysfs_net(tmp_path, {})
    monkeypatch.setattr(host_net, "_SYSFS_NET", base)
    assert host_net.link_state("nve1234n1i9") == (False, None, False)


def test_link_state_tolerates_missing_flags(tmp_path, monkeypatch) -> None:
    base = _make_sysfs_net(tmp_path, {"nve1234n1i0": ("nve1lab-net1", "0x1003")})
    (base / "nve1234n1i0" / "flags").unlink()
    monkeypatch.setattr(host_net, "_SYSFS_NET", base)
    # exists + master still reported; admin-up degrades to False.
    assert host_net.link_state("nve1234n1i0") == (True, "nve1lab-net1", False)
