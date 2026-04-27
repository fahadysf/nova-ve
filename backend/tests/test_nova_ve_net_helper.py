"""Unit tests for ``deploy/nova-ve-net.py`` — the US-201 privileged helper.

These tests cover the helper in isolation:
  * each verb's argparse + regex validation;
  * regex rejection of malformed argv;
  * unknown verb -> argparse exit (non-zero);
  * pid-ownership check rejects pids missing from the registry.

The tests NEVER actually invoke ``ip`` / ``nsenter`` — the helper module
exposes a ``_run`` shim that we monkey-patch so each test records which
argv WOULD have been spawned.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPO_ROOT / "deploy" / "nova-ve-net.py"


@pytest.fixture
def helper(monkeypatch, tmp_path):
    """Load the helper as a module without executing it as __main__."""
    # Load the file under a stable module name; importlib does the rest.
    spec = importlib.util.spec_from_file_location("nova_ve_net_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nova_ve_net_helper"] = mod
    spec.loader.exec_module(mod)

    # Redirect the registry + /proc to a writable tmp tree.
    pids_path = tmp_path / "pids.json"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(mod, "PIDS_JSON_PATH", pids_path, raising=True)
    monkeypatch.setattr(mod, "PROC_ROOT", proc_root, raising=True)

    # Capture the argv that WOULD have been run — never actually fork.
    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(mod, "_run", fake_run, raising=True)

    mod._test_calls = calls  # type: ignore[attr-defined]
    mod._test_pids_path = pids_path  # type: ignore[attr-defined]
    mod._test_proc_root = proc_root  # type: ignore[attr-defined]
    return mod


def _seed_pid(mod, pid: int, *, kind: str = "docker", cgroup: str | None = None) -> None:
    """Register ``pid`` in the registry and seed /proc cgroup data."""
    pids_path: Path = mod._test_pids_path
    proc_root: Path = mod._test_proc_root
    existing = []
    if pids_path.exists():
        existing = json.loads(pids_path.read_text())
    existing.append(
        {
            "pid": pid,
            "kind": kind,
            "lab_id": "lab-test",
            "node_id": "node-test",
            "started_at": 0,
            "generation": 1,
        }
    )
    pids_path.write_text(json.dumps(existing))

    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    if cgroup is None:
        if kind == "docker":
            cgroup = "0::/system.slice/docker-abcdef0123456789.scope\n"
        else:
            cgroup = "0::/system.slice/user.slice\n"
    (pid_dir / "cgroup").write_text(cgroup)
    if kind == "qemu":
        (pid_dir / "comm").write_text("qemu-system-x86_64\n")
    else:
        (pid_dir / "comm").write_text("dockerd-shim\n")


# ---------------------------------------------------------------------------
# Verb parsing + regex validation: positive paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_argv_tail"),
    [
        (
            ["bridge-add", "novec0den1"],
            ["link", "add", "novec0den1", "type", "bridge"],
        ),
        (
            ["bridge-del", "novec0den1"],
            ["link", "del", "novec0den1"],
        ),
        (
            ["tap-add", "nvec0ded1i2"],
            ["tuntap", "add", "dev", "nvec0ded1i2", "mode", "tap"],
        ),
        (
            ["tap-del", "nvec0ded1i2"],
            ["link", "del", "nvec0ded1i2"],
        ),
        (
            ["veth-pair-add", "nvec0ded1i2h", "nvec0ded1i2p"],
            [
                "link",
                "add",
                "nvec0ded1i2h",
                "type",
                "veth",
                "peer",
                "name",
                "nvec0ded1i2p",
            ],
        ),
        (
            ["link-master", "nvec0ded1i2h", "novec0den1"],
            ["link", "set", "nvec0ded1i2h", "master", "novec0den1"],
        ),
        (
            ["link-set-nomaster", "nvec0ded1i2h"],
            ["link", "set", "nvec0ded1i2h", "nomaster"],
        ),
        (
            ["link-up", "nvec0ded1i2h"],
            ["link", "set", "nvec0ded1i2h", "up"],
        ),
    ],
)
def test_non_pid_verbs_emit_correct_argv(helper, argv, expected_argv_tail):
    rc = helper.main(argv)
    assert rc == 0, f"verb {argv[0]} returned {rc}"
    assert helper._test_calls, "helper did not invoke _run"
    spawned = helper._test_calls[-1]
    # First element is the ``ip`` binary path; tail must match exactly.
    assert spawned[1:] == expected_argv_tail


def test_link_netns_with_authorized_pid(helper):
    _seed_pid(helper, 4242)
    rc = helper.main(["link-netns", "nvec0ded1i2p", "4242"])
    assert rc == 0
    spawned = helper._test_calls[-1]
    assert spawned[1:] == ["link", "set", "nvec0ded1i2p", "netns", "4242"]


def test_link_set_name_in_netns_with_authorized_pid(helper):
    _seed_pid(helper, 4242)
    rc = helper.main(["link-set-name-in-netns", "4242", "nvec0ded1i2p", "eth1"])
    assert rc == 0
    spawned = helper._test_calls[-1]
    # Expect ``nsenter -t 4242 -n ip link set <old> name <new>``
    assert spawned[0].endswith("nsenter")
    assert spawned[1:5] == ["-t", "4242", "-n", helper.IP_BIN]
    assert spawned[5:] == ["link", "set", "nvec0ded1i2p", "name", "eth1"]


def test_addr_add_in_netns_with_authorized_pid(helper):
    _seed_pid(helper, 4242)
    rc = helper.main(["addr-add-in-netns", "4242", "eth1", "10.99.1.5/24"])
    assert rc == 0
    spawned = helper._test_calls[-1]
    assert spawned[5:] == ["addr", "add", "10.99.1.5/24", "dev", "eth1"]


def test_addr_up_in_netns_with_authorized_pid(helper):
    _seed_pid(helper, 4242)
    rc = helper.main(["addr-up-in-netns", "4242", "eth1"])
    assert rc == 0
    spawned = helper._test_calls[-1]
    assert spawned[5:] == ["link", "set", "eth1", "up"]


def test_read_iface_mac_with_authorized_pid(helper):
    _seed_pid(helper, 4242)
    rc = helper.main(["read-iface-mac", "4242", "eth0"])
    assert rc == 0
    spawned = helper._test_calls[-1]
    # nsenter -t <pid> -n cat /sys/class/net/<iface>/address
    assert spawned[0].endswith("nsenter")
    assert spawned[1:4] == ["-t", "4242", "-n"]
    assert spawned[4] == "cat"
    assert spawned[5] == "/sys/class/net/eth0/address"


def test_read_iface_mac_for_qemu_pid(helper):
    _seed_pid(helper, 5555, kind="qemu")
    rc = helper.main(["read-iface-mac", "5555", "eth0"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Regex rejection: each verb refuses bad arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "",                          # empty
        "br0",                       # missing nove prefix
        "noveZZZZn1",                # non-hex lab hash
        "novedeadbeef",              # missing n<id>
        "novedeadn",                 # missing id digits
        "novedeadn123456",           # too many digits (>5)
        "novedeadn1; rm -rf /",      # injection
        "novedeadn1\nfoo",           # newline
        "novedeadn1 ",               # trailing space
        "noveDEADn1",                # uppercase hex
    ],
)
def test_bridge_add_rejects_bad_name(helper, capsys, name):
    rc = helper.main(["bridge-add", name])
    assert rc == 2
    assert not helper._test_calls
    assert "argument failed validation" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name",
    [
        "tap0",
        "nve0000d1",                 # missing iface segment
        "nve0000d1i",                # missing iface digits
        "nve0000d1234i1",            # node too many digits
        "nve0000d1i123",             # iface too many digits
        "nve0000d1i1; reboot",       # injection
    ],
)
def test_tap_add_rejects_bad_name(helper, name):
    rc = helper.main(["tap-add", name])
    assert rc == 2
    assert not helper._test_calls


def test_veth_pair_rejects_identical_names(helper):
    rc = helper.main(["veth-pair-add", "nve0000d1i1h", "nve0000d1i1h"])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize(
    ("hostend", "peerend"),
    [
        ("nve0000d1i1h", "eth0"),                # peer not nova-ve shape
        ("nve0000d1i1h", "nve0000d1i1x"),         # x suffix not h/p
        ("nve0000d1i1H", "nve0000d1i1p"),         # uppercase
    ],
)
def test_veth_pair_rejects_bad_arg(helper, hostend, peerend):
    rc = helper.main(["veth-pair-add", hostend, peerend])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize(
    "pid_str",
    [
        "0",                # too low
        "1",                # init
        "9",                # below floor
        "12345678",         # 8 digits, too many
        "abc",              # non-numeric
        "-1",               # negative
        "12 34",            # whitespace
        "12;rm",            # injection
    ],
)
def test_pid_taking_verbs_reject_bad_pid_shape(helper, pid_str):
    # Even before registry lookup, the regex rejects.
    rc = helper.main(["link-netns", "nve0000d1i1p", pid_str])
    assert rc == 2
    assert not helper._test_calls


def test_pid_taking_verb_rejects_unregistered_pid(helper, capsys):
    # No _seed_pid call → registry is empty → reject.
    rc = helper.main(["link-netns", "nve0000d1i1p", "4242"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pid ownership check FAILED" in err
    assert not helper._test_calls


def test_addr_add_rejects_bad_cidr(helper):
    _seed_pid(helper, 4242)
    for bad in ["10.99.1.5", "10.99.1.5/33", "not-an-ip", "10.99.1.5/24; reboot"]:
        helper._test_calls.clear()
        rc = helper.main(["addr-add-in-netns", "4242", "eth1", bad])
        assert rc == 2, f"cidr {bad!r} should have been rejected"
        assert not helper._test_calls


def test_addr_add_rejects_ipv6_cidr(helper):
    _seed_pid(helper, 4242)
    rc = helper.main(["addr-add-in-netns", "4242", "eth1", "fd00::1/64"])
    assert rc == 2
    assert not helper._test_calls


def test_netns_iface_rejects_non_eth(helper):
    _seed_pid(helper, 4242)
    rc = helper.main(["addr-up-in-netns", "4242", "lo"])
    assert rc == 2
    assert not helper._test_calls


# ---------------------------------------------------------------------------
# Unknown verb -> argparse exits non-zero
# ---------------------------------------------------------------------------


def test_unknown_verb_returns_nonzero(helper):
    rc = helper.main(["wormhole-please", "anything"])
    assert rc != 0
    assert not helper._test_calls


def test_no_verb_returns_nonzero(helper):
    rc = helper.main([])
    assert rc != 0
    assert not helper._test_calls


# ---------------------------------------------------------------------------
# Iface-name validator covers both TAP and veth shapes
# ---------------------------------------------------------------------------


def test_link_master_accepts_tap_iface(helper):
    rc = helper.main(["link-master", "nve0000d1i1", "novec0den1"])
    assert rc == 0


def test_link_master_accepts_veth_iface(helper):
    rc = helper.main(["link-master", "nve0000d1i1h", "novec0den1"])
    assert rc == 0


def test_link_master_rejects_random_iface(helper):
    rc = helper.main(["link-master", "eth0", "novec0den1"])
    assert rc == 2
    assert not helper._test_calls
