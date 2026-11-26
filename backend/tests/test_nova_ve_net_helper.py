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
import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
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
    namespaces = proc_root / "self" / "ns"
    namespaces.mkdir(parents=True)
    for name in ("user", "net", "mnt"):
        (namespaces / name).write_text(f"host {name} namespace fixture")
    monkeypatch.setattr(mod, "PIDS_JSON_PATH", pids_path, raising=True)
    monkeypatch.setattr(mod, "PROC_ROOT", proc_root, raising=True)
    monkeypatch.setattr(mod, "RUNTIME_ROOT", tmp_path / "runtime", raising=True)
    monkeypatch.setattr(mod, "DNSMASQ_ROOT", tmp_path / "run" / "dnsmasq", raising=True)
    # This is a metadata fixture only, and not a runnable daemon.
    dnsmasq = tmp_path / "dnsmasq-fixture"
    dnsmasq.write_bytes(b"dnsmasq executable fixture")
    dnsmasq.chmod(0o755)
    monkeypatch.setattr(mod, "DNSMASQ_BIN", str(dnsmasq), raising=True)
    monkeypatch.setattr(mod, "IP_FORWARD_PATH", tmp_path / "ip_forward", raising=True)
    monkeypatch.setattr(mod, "IPV4_CONF_ROOT", tmp_path / "ipv4-conf", raising=True)
    monkeypatch.setattr(mod, "GUARD_LOCK_PATH", tmp_path / "guard.lock", raising=True)
    # The tree is this test user's own; production keeps the default of root.
    monkeypatch.setattr(mod, "_TRUSTED_UID", os.geteuid(), raising=True)

    # Numeric signals are forbidden even when a test simulates pidfd signals.
    def no_kill(pid, sig):
        raise AssertionError(f"os.kill({pid}, {sig}) called without a stub")

    monkeypatch.setattr(mod.os, "kill", no_kill)

    # Use real scratch file descriptors as opaque fake handles. No kernel
    # process is opened, inspected, or signalled by these stubs.
    handles = {}

    def fake_pidfd_open(pid):
        entry = proc_root / str(pid)
        try:
            inode = entry.stat().st_ino
            fd = os.open(entry / "cmdline", os.O_RDONLY)
        except FileNotFoundError as exc:
            raise ProcessLookupError(pid) from exc
        handles[fd] = (entry, inode)
        return fd

    def fake_exited(fd):
        entry, inode = handles[fd]
        try:
            return entry.stat().st_ino != inode
        except FileNotFoundError:
            return True

    monkeypatch.setattr(mod.os, "pidfd_open", fake_pidfd_open, raising=False)
    monkeypatch.setattr(mod.signal, "pidfd_send_signal", no_kill, raising=False)
    monkeypatch.setattr(mod, "_dnsmasq_pidfd_exited", fake_exited)
    monkeypatch.setattr(mod, "_dnsmasq_uids", lambda: {65534})
    mod._test_dnsmasq_handles = handles

    # The helper fixes its own umask and some tests vary it, so every test
    # starts from a known one and the runner's is put back afterwards.
    runner_umask = os.umask(0o022)

    # Capture the argv that WOULD have been run — never actually fork.
    calls: list[list[str]] = []
    capture_calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0

    def fake_run_capture(argv):
        capture_calls.append(list(argv))
        if argv[1:3] == ["delete", "rule"]:
            return mod.subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return mod.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    input_calls: list[tuple[list[str], str]] = []

    def fake_run_input(argv, text):
        input_calls.append((list(argv), text))
        return 0

    monkeypatch.setattr(mod, "_run", fake_run, raising=True)
    monkeypatch.setattr(mod, "_run_capture", fake_run_capture, raising=True)
    monkeypatch.setattr(mod, "_run_input", fake_run_input, raising=True)

    mod._test_calls = calls  # type: ignore[attr-defined]
    mod._test_input_calls = input_calls  # type: ignore[attr-defined]
    mod._test_capture_calls = capture_calls  # type: ignore[attr-defined]
    mod._test_pids_path = pids_path  # type: ignore[attr-defined]
    mod._test_proc_root = proc_root  # type: ignore[attr-defined]
    yield mod
    os.umask(runner_umask)


def test_tap_batch_validates_all_entries_before_applying(helper, monkeypatch, capsys):
    changes = {
        "nveabcdd1i0": {"up": True},
        "nveabcdd1i1": {"up": 1},
    }
    monkeypatch.setattr(helper.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(changes).encode())))

    assert helper.main(["reconcile-taps"]) == 2
    assert "invalid TAP change" in capsys.readouterr().err
    assert not helper._test_capture_calls
    assert not helper._test_calls


def test_tap_batch_retains_host_bridge_validation(helper, monkeypatch):
    changes = {"nveabcdd1i0": {"master": "br-eth0"}}
    monkeypatch.setattr(helper.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(changes).encode())))

    assert helper.main(["reconcile-taps"]) == 2
    assert not helper._test_capture_calls


def test_tap_batch_reports_failures_without_running_later_steps_for_that_tap(helper, monkeypatch, capsys):
    attached = "nveabcdd1i0"
    detached = "nveabcdd1i1"
    changes = {
        attached: {"create": True, "master": "noveabcdn1", "up": True},
        detached: {"detach": True},
    }
    monkeypatch.setattr(helper.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(changes).encode())))
    commands = []

    def capture(argv):
        commands.append(argv[1:])
        if "master" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "fixture attach failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(helper, "_run_capture", capture)

    assert helper.main(["reconcile-taps"]) == 0
    assert json.loads(capsys.readouterr().out) == {attached: "fixture attach failed"}
    assert commands == [
        ["tuntap", "add", "dev", attached, "mode", "tap"],
        ["link", "set", attached, "master", changes[attached]["master"]],
        ["link", "set", detached, "nomaster"],
    ]


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
            "node_id": 1,
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
        qmp_path = mod.RUNTIME_ROOT / "tmp" / "lab-test" / "1" / "qmp.sock"
        cmdline = [
            "/usr/bin/qemu-system-x86_64",
            "-qmp",
            f"unix:{qmp_path},server,nowait",
        ]
        (pid_dir / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in cmdline) + b"\0")
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
        (
            ["bridge-addr-add", "novec0den1", "10.255.0.1/24"],
            ["addr", "replace", "10.255.0.1/24", "dev", "novec0den1"],
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


def test_default_egress_prints_default_route_iface(helper, monkeypatch, capsys):
    def fake_capture(argv):
        return helper.subprocess.CompletedProcess(
            argv,
            0,
            stdout="default via 192.0.2.1 dev ens18 proto dhcp\n",
            stderr="",
        )

    monkeypatch.setattr(helper, "_run_capture", fake_capture, raising=True)
    rc = helper.main(["default-egress"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "ens18"


def test_ipv4_forward_enable_writes_one(helper):
    rc = helper.main(["ipv4-forward-enable"])
    assert rc == 0
    assert helper.IP_FORWARD_PATH.read_text() == "1\n"


def test_nat_apply_uses_nftables_chain(helper):
    rc = helper.main(["nat-apply", "novec0den1", "10.255.0.0/24", "ens18"])
    assert rc == 0
    assert [call[0] for call in helper._test_capture_calls] == [helper.NFT_BIN] * 3
    assert [call[0] for call in helper._test_calls] == [helper.NFT_BIN] * 2
    assert helper._test_calls[-1][1:] == [
        "add",
        "rule",
        "ip",
        "nova_ve",
        "nvc_novec0den1",
        "ip",
        "saddr",
        "10.255.0.0/24",
        "oifname",
        "ens18",
        "masquerade",
        "comment",
        '"nova-ve novec0den1"',
    ]


def test_forward_apply_uses_docker_user_when_available(helper):
    rc = helper.main(["forward-apply", "novec0den1", "10.255.0.0/24", "ens18"])
    assert rc == 0
    assert helper._test_capture_calls[0][1:] == [
        "list",
        "chain",
        "ip",
        "filter",
        "DOCKER-USER",
    ]
    assert [call[1:6] for call in helper._test_capture_calls[1:]] == [
        ["-a", "list", "chain", "ip", "filter"],
    ]
    assert [call[1:5] for call in helper._test_calls] == [
        ["insert", "rule", "ip", "filter"],
        ["insert", "rule", "ip", "filter"],
    ]
    assert helper._test_calls[-1][1:] == [
        "insert",
        "rule",
        "ip",
        "filter",
        "DOCKER-USER",
        "iifname",
        "novec0den1",
        "oifname",
        "ens18",
        "ip",
        "saddr",
        "10.255.0.0/24",
        "accept",
        "comment",
        '"nova-ve novec0den1 forward-out"',
    ]


def test_forward_apply_removes_duplicate_docker_user_rules_before_insert(
    helper, monkeypatch
):
    rules = "\n".join(
        [
            'iifname "novec0den1" oifname "ens18" ip saddr 10.255.0.0/24 accept comment "nova-ve novec0den1 forward-out" # handle 31',
            'iifname "ens18" oifname "novec0den1" ip daddr 10.255.0.0/24 ct state established,related accept comment "nova-ve novec0den1 forward-in" # handle 32',
            'iifname "novec0den1" oifname "ens18" ip saddr 10.255.0.0/24 accept comment "nova-ve novec0den1 forward-out" # handle 41',
            'iifname "ens18" oifname "novec0den1" ip daddr 10.255.0.0/24 ct state established,related accept comment "nova-ve novec0den1 forward-in" # handle 42',
        ]
    )

    def fake_run_capture(argv):
        helper._test_capture_calls.append(list(argv))
        if argv[1:6] == ["list", "chain", "ip", "filter", "DOCKER-USER"]:
            return helper.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1:7] == ["-a", "list", "chain", "ip", "filter", "DOCKER-USER"]:
            return helper.subprocess.CompletedProcess(argv, 0, stdout=rules, stderr="")
        return helper.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(helper, "_run_capture", fake_run_capture, raising=True)

    rc = helper.main(["forward-apply", "novec0den1", "10.255.0.0/24", "ens18"])

    assert rc == 0
    assert [call[1:5] for call in helper._test_calls] == [
        ["delete", "rule", "ip", "filter"],
        ["delete", "rule", "ip", "filter"],
        ["delete", "rule", "ip", "filter"],
        ["delete", "rule", "ip", "filter"],
        ["insert", "rule", "ip", "filter"],
        ["insert", "rule", "ip", "filter"],
    ]
    assert [call[-1] for call in helper._test_calls[:4]] == ["31", "32", "41", "42"]


def test_forward_remove_deletes_docker_user_rules_when_rule_shape_available(helper):
    rc = helper.main(["forward-remove", "novec0den1", "10.255.0.0/24", "ens18"])
    assert rc == 0
    assert [call[1:6] for call in helper._test_capture_calls[:2]] == [
        ["list", "chain", "ip", "filter", "DOCKER-USER"],
        ["-a", "list", "chain", "ip", "filter"],
    ]


def _seed_dnsmasq(helper, pid: int, bridge: str) -> None:
    _seed_dnsmasq_process(helper, pid, helper._dnsmasq_paths(bridge)[0])


def _seed_dnsmasq_process(helper, pid: int, conf: Path) -> None:
    entry = helper._test_proc_root / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(f"/usr/sbin/dnsmasq\0--conf-file={conf}\0".encode())
    (entry / "exe").symlink_to(Path(helper.DNSMASQ_BIN).resolve())
    (entry / "status").write_text("Uid:\t65534\t65534\t65534\t65534\nCapEff:\t0000000000002000\n")
    (entry / "ns").mkdir()
    for name in ("user", "net", "mnt"):
        (entry / "ns" / name).symlink_to(helper.PROC_ROOT / "self" / "ns" / name)


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("managed", True),
        ("different-executable", False),
        ("different-uid", False),
        ("different-net-namespace", False),
        ("different-mnt-namespace", False),
        ("missing-status", None),
        ("malformed-uid", None),
        ("writable-executable", None),
        ("missing-dhcp-capability", None),
    ],
)
def test_dnsmasq_process_identity_uses_executable_and_credentials(helper, scenario, expected):
    _seed_dnsmasq(helper, 510, "novec0den1")
    entry = helper.PROC_ROOT / "510"
    executable = Path(helper.DNSMASQ_BIN)
    executable.write_bytes(b"executable fixture")
    executable.chmod(0o775 if scenario == "writable-executable" else 0o755)
    if scenario == "different-executable":
        other = executable.with_name("unrelated-fixture")
        other.write_bytes(b"different executable fixture")
        other.chmod(0o755)
        (entry / "exe").unlink()
        (entry / "exe").symlink_to(other)
    if scenario in {"different-net-namespace", "different-mnt-namespace"}:
        kind = scenario.split("-")[1]
        foreign = helper.PROC_ROOT / f"foreign-{kind}"
        foreign.write_text("foreign namespace fixture")
        (entry / "ns" / kind).unlink()
        (entry / "ns" / kind).symlink_to(foreign)

    daemon_uid = 65534
    if scenario == "missing-status":
        (entry / "status").unlink()
    else:
        uid = 65533 if scenario == "different-uid" else daemon_uid
        values = f"{uid}\t{uid}" if scenario == "malformed-uid" else "\t".join([str(uid)] * 4)
        cap = "0" if scenario == "missing-dhcp-capability" else "2000"
        (entry / "status").write_text(f"Uid:\t{values}\nCapEff:\t{cap}\n")

    if expected is None:
        with pytest.raises(OSError):
            helper._dnsmasq_identity_matches(entry, executable, {daemon_uid})
    else:
        assert helper._dnsmasq_identity_matches(entry, executable, {daemon_uid}) is expected


def _fake_kill(helper, monkeypatch, *, ignores=()):
    """Remove the fake process when its pinned synthetic handle is signalled."""
    killed: list[tuple[int, int]] = []

    def kill(handle, sig):
        if helper._dnsmasq_pidfd_exited(handle):
            raise ProcessLookupError(handle)
        entry, _inode = helper._test_dnsmasq_handles[handle]
        killed.append((int(entry.name), sig))
        if sig not in ignores:
            shutil.rmtree(entry)

    monkeypatch.setattr(helper.signal, "pidfd_send_signal", kill)
    monkeypatch.setattr(helper.time, "sleep", lambda _seconds: None)
    return killed


def test_dnsmasq_layout_version_is_read_only(helper, capsys):
    assert helper.main(["dnsmasq-layout-version"]) == 0
    assert capsys.readouterr().out.strip() == "root-owned-dnsmasq-v1"
    assert helper._test_calls == []


def test_dnsmasq_stop_finds_server_without_pid_file(helper, monkeypatch):
    _seed_dnsmasq(helper, 501, "novec0den1")
    killed = _fake_kill(helper, monkeypatch)
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert killed == [(501, helper.signal.SIGTERM)]


def test_dnsmasq_stop_ignores_stale_pid_file_of_another_network(helper, monkeypatch):
    # n1 vs n10: names that share a prefix must not match either.
    _seed_dnsmasq(helper, 502, "novec0den10")
    root = helper._dnsmasq_paths("novec0den1")[0].parent
    root.mkdir(parents=True)
    (root / "dnsmasq.pid").write_text("502\n")
    killed = _fake_kill(helper, monkeypatch)
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert killed == []
    assert not (root / "dnsmasq.pid").exists()


def test_dnsmasq_stop_ignores_a_candidate_with_foreign_credentials(helper, monkeypatch):
    _seed_dnsmasq(helper, 507, "novec0den1")
    entry = helper.PROC_ROOT / "507"
    (entry / "status").write_text("Uid:\t65533\t65533\t65533\t65533\n")
    signals = _fake_kill(helper, monkeypatch)

    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert signals == [] and entry.is_dir()


def test_dnsmasq_start_leaves_an_unverified_candidate_in_place(helper, monkeypatch, capsys):
    _seed_dnsmasq(helper, 508, "novec0den1")
    entry = helper.PROC_ROOT / "508"
    (entry / "status").unlink()
    signals = _fake_kill(helper, monkeypatch)

    assert helper.main(_NAT_START) == 1
    assert "Could not stop dnsmasq" in capsys.readouterr().err
    assert signals == [] and entry.is_dir()
    assert helper._test_calls == []
    assert not helper._dnsmasq_paths("novec0den1")[0].exists()
    assert helper._test_dnsmasq_handles
    for fd in helper._test_dnsmasq_handles:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_dnsmasq_start_refuses_an_incomplete_process_scan(helper, monkeypatch):
    _seed_dnsmasq(helper, 512, "novec0den1")
    entry = helper.PROC_ROOT / "512"
    original_open = Path.open

    def open_fixture(path, *args, **kwargs):
        if path == entry / "cmdline":
            raise PermissionError("unreadable process fixture")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_fixture)
    signals = _fake_kill(helper, monkeypatch)
    assert helper.main(_NAT_START) == 1
    assert signals == [] and helper._test_calls == [] and entry.is_dir()


def test_dnsmasq_start_refuses_an_ambiguous_invocation(helper, monkeypatch, capsys):
    _seed_dnsmasq(helper, 513, "novec0den1")
    entry = helper.PROC_ROOT / "513"
    with (entry / "cmdline").open("ab") as argv:
        argv.write(b"unexpected-fixture-argument\0")
    signals = _fake_kill(helper, monkeypatch)

    assert helper.main(_NAT_START) == 1
    assert "ambiguous configuration invocation" in capsys.readouterr().err
    assert signals == [] and helper._test_calls == [] and entry.is_dir()


def test_dnsmasq_pinning_tolerates_esrch_only_for_an_exited_process(helper, monkeypatch):
    _seed_dnsmasq(helper, 514, "novec0den1")

    def exited(entry, *_args):
        entry.rename(helper.PROC_ROOT / "exited-fixture")
        raise ProcessLookupError("process fixture exited during verification")

    monkeypatch.setattr(helper, "_dnsmasq_identity_matches", exited)
    signals = _fake_kill(helper, monkeypatch)

    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert signals == []
    for fd in helper._test_dnsmasq_handles:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_dnsmasq_escalation_does_not_signal_a_replacement_process(helper, monkeypatch):
    pid = 509
    bridge = "novec0den1"
    _seed_dnsmasq(helper, pid, bridge)
    sent = []

    def send(handle, sig):
        assert not helper._dnsmasq_pidfd_exited(handle)
        sent.append(sig)
        entry, _inode = helper._test_dnsmasq_handles[handle]
        entry.rename(helper.PROC_ROOT / "exited-fixture")
        _seed_dnsmasq(helper, pid, bridge)

    monkeypatch.setattr(helper.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)

    assert helper.main(["dnsmasq-stop", bridge]) == 1
    assert sent == [signal.SIGTERM]
    assert (helper.PROC_ROOT / str(pid)).is_dir()


def test_dnsmasq_identity_allows_the_trusted_inode_during_package_replacement(helper, monkeypatch):
    _seed_dnsmasq(helper, 511, "novec0den1")
    entry = helper.PROC_ROOT / "511"
    installed = Path(helper.DNSMASQ_BIN).resolve()
    previous = installed.with_name("old-executable-fixture")
    previous.write_bytes(b"previous executable")
    previous.chmod(0o755)
    (entry / "exe").unlink()
    (entry / "exe").symlink_to(previous)
    readlink = os.readlink

    def kernel_link(path, *args, **kwargs):
        if path == entry / "exe":
            return f"{installed} (deleted)"
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(helper.os, "readlink", kernel_link)
    assert not installed.samefile(previous)
    assert helper._dnsmasq_identity_matches(entry, installed, {65534})


def test_dnsmasq_stop_escalates_to_sigkill(helper, monkeypatch):
    _seed_dnsmasq(helper, 503, "novec0den1")
    killed = _fake_kill(helper, monkeypatch, ignores=(signal.SIGTERM,))
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert [sig for _pid, sig in killed] == [signal.SIGTERM, signal.SIGKILL]


def test_dnsmasq_stop_and_start_fail_when_the_server_survives(helper, monkeypatch, capsys):
    _seed_dnsmasq(helper, 504, "novec0den1")
    _fake_kill(helper, monkeypatch, ignores=(signal.SIGTERM, signal.SIGKILL))
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 1
    assert "did not stop" in capsys.readouterr().err
    start = ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"]
    assert helper.main(start) == 1
    assert helper._test_calls == []


def test_dnsmasq_start_fails_when_the_daemon_exits(helper, capsys):
    start = ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"]
    assert helper.main(start) == 1
    assert "exited right after starting" in capsys.readouterr().err


def test_dnsmasq_start_writes_bridge_config(helper, monkeypatch):
    record = helper._run

    def run(argv):
        rc = record(argv)
        _seed_dnsmasq(helper, 505, "novec0den1")
        return rc

    monkeypatch.setattr(helper, "_run", run)
    rc = helper.main(
        ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"]
    )
    assert rc == 0
    conf = helper._dnsmasq_paths("novec0den1")[0]
    text = conf.read_text()
    assert "interface=novec0den1" in text
    assert "port=0" in text
    assert "user=nobody" in text
    assert "dhcp-range=10.255.0.100,10.255.0.254,12h" in text
    assert helper._test_calls[-1][0] == helper.DNSMASQ_BIN
    assert helper._test_calls[-1][1] == f"--conf-file={conf}"


def _start_dnsmasq(helper, monkeypatch, argv):
    record = helper._run

    def run(cmd):
        rc = record(cmd)
        _seed_dnsmasq(helper, 506, argv[1])
        return rc

    monkeypatch.setattr(helper, "_run", run)
    return helper.main(argv)


def test_dnsmasq_start_isolated_sends_no_router_and_no_dns(helper, monkeypatch):
    """#325: an isolated bridge's server offers addresses only."""
    rc = _start_dnsmasq(
        helper,
        monkeypatch,
        ["dnsmasq-start", "novec0den2", "10.40.1.254", "10.40.1.128", "10.40.1.253", "--no-router"],
    )
    assert rc == 0
    lines = (helper._dnsmasq_paths("novec0den2")[0]).read_text().splitlines()
    assert "listen-address=10.40.1.254" in lines
    assert "port=0" in lines
    assert "dhcp-range=10.40.1.128,10.40.1.253,12h" in lines
    # Empty options: dnsmasq then sends neither a router nor a DNS server.
    assert "dhcp-option=3" in lines and "dhcp-option=6" in lines
    assert not any("router" in line for line in lines)
    # A lab router may serve the same segment; do not NAK its clients.
    assert "dhcp-authoritative" not in lines


def test_dnsmasq_config_never_follows_planted_links(helper, monkeypatch, tmp_path):
    """The state directory is the service account's: root writes stay inside it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("keep\n")
    root = helper._dnsmasq_paths("novec0den2")[0].parent
    root.mkdir(parents=True)
    (root / "dnsmasq.conf").symlink_to(victim)
    argv = ["dnsmasq-start", "novec0den2", "10.40.1.254", "10.40.1.128", "10.40.1.253", "--no-router"]
    assert _start_dnsmasq(helper, monkeypatch, argv) == 0
    assert victim.read_text() == "keep\n"
    assert not (root / "dnsmasq.conf").is_symlink()
    assert "dhcp-option=3" in (root / "dnsmasq.conf").read_text()

    # A symlinked directory is refused outright.
    (helper._dnsmasq_paths("novec0den3")[0].parent).symlink_to(outside)
    argv[1] = "novec0den3"
    assert helper.main(argv) == 1
    assert sorted(p.name for p in outside.iterdir()) == ["victim"]


def test_dnsmasq_paths_live_under_root_owned_dnsmasq_root(helper, monkeypatch, tmp_path):
    """#334: conf, pid and leases must not live in the service-owned data dir.

    Root dnsmasq follows the lease file too, so leaving leases behind in a
    directory the service account controls would keep a symlink-based root
    write open.
    """
    dnsmasq_root = tmp_path / "run" / "nova-ve" / "dnsmasq"
    monkeypatch.setattr(helper, "DNSMASQ_ROOT", dnsmasq_root, raising=True)
    conf, pidfile, leases = helper._dnsmasq_paths("novec0den1")
    assert conf == dnsmasq_root / "novec0den1" / "dnsmasq.conf"
    assert pidfile == dnsmasq_root / "novec0den1" / "dnsmasq.pid"
    assert leases == dnsmasq_root / "novec0den1" / "leases"
    assert helper.RUNTIME_ROOT not in conf.parents


def test_dnsmasq_stop_finds_legacy_server_from_before_upgrade(helper, monkeypatch, tmp_path):
    """#334 upgrade trap: servers started with the old conf path stay stoppable."""
    monkeypatch.setattr(helper, "DNSMASQ_ROOT", tmp_path / "run-dnsmasq", raising=True)
    legacy_conf = helper._legacy_dnsmasq_paths("novec0den1")[0]
    _seed_dnsmasq_process(helper, 601, legacy_conf)
    killed = _fake_kill(helper, monkeypatch)
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert killed == [(601, helper.signal.SIGTERM)]


def test_dnsmasq_start_migrates_existing_leases(helper, monkeypatch, tmp_path):
    """#334: leases from the old location must survive the move."""
    monkeypatch.setattr(helper, "DNSMASQ_ROOT", tmp_path / "run-dnsmasq", raising=True)
    legacy = helper.RUNTIME_ROOT / "nat-cloud" / "novec0den1"
    legacy.mkdir(parents=True)
    (legacy / "leases").write_text("0 aa:bb:cc:dd:ee:ff 10.255.0.100 host *\n")
    assert _start_dnsmasq(
        helper, monkeypatch,
        ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"],
    ) == 0
    _conf, _pidfile, leases = helper._dnsmasq_paths("novec0den1")
    assert "10.255.0.100" in leases.read_text()
    assert not (legacy / "leases").exists()
    assert (legacy / "leases.pre-334").exists()


def test_dnsmasq_start_never_migrates_a_planted_lease_symlink(helper, monkeypatch, tmp_path):
    """#334: a symlinked legacy leases file must not become a root read."""
    monkeypatch.setattr(helper, "DNSMASQ_ROOT", tmp_path / "run-dnsmasq", raising=True)
    victim = tmp_path / "victim"
    victim.write_text("root-only-secret\n")
    legacy = helper.RUNTIME_ROOT / "nat-cloud" / "novec0den1"
    legacy.mkdir(parents=True)
    (legacy / "leases").symlink_to(victim)
    assert _start_dnsmasq(
        helper, monkeypatch,
        ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"],
    ) == 0
    _conf, _pidfile, leases = helper._dnsmasq_paths("novec0den1")
    assert not leases.exists() or "root-only-secret" not in leases.read_text()
    assert victim.read_text() == "root-only-secret\n"


def test_dnsmasq_stop_never_unlinks_through_a_planted_link(helper, monkeypatch, tmp_path):
    """#334: removing the legacy pid file must not follow a symlinked ancestor."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "dnsmasq.pid").write_text("999\n")
    _seed_dnsmasq(helper, 602, "novec0den1")
    _fake_kill(helper, monkeypatch)
    (helper.RUNTIME_ROOT / "nat-cloud").mkdir(parents=True, exist_ok=True)
    (helper.RUNTIME_ROOT / "nat-cloud" / "novec0den1").symlink_to(outside)
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert (outside / "dnsmasq.pid").read_text() == "999\n"


def test_dnsmasq_start_keeps_router_for_nat_cloud(helper, monkeypatch):
    rc = _start_dnsmasq(
        helper, monkeypatch, ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"]
    )
    assert rc == 0
    lines = (helper._dnsmasq_paths("novec0den1")[0]).read_text().splitlines()
    assert "dhcp-option=option:router,10.255.0.1" in lines
    assert "dhcp-authoritative" in lines
    assert "dhcp-option=3" not in lines


_NAT_START = ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"]
_LEASE = "0 aa:bb:cc:dd:ee:ff 10.255.0.100 host *\n"


def _legacy_dir(helper, bridge: str = "novec0den1") -> Path:
    """The bridge's pre-#334 state directory, which the service account owns."""
    legacy = helper.RUNTIME_ROOT / "nat-cloud" / bridge
    legacy.mkdir(parents=True, exist_ok=True)
    return legacy


def _seed_legacy_dnsmasq(helper, pid: int, bridge: str) -> None:
    """A server the old helper started: only the legacy conf path finds it."""
    _seed_dnsmasq_process(helper, pid, helper._legacy_dnsmasq_paths(bridge)[0])


def _written(helper) -> dict[str, str]:
    """Everything the test's scratch tree holds, as ``{path: kind}``.

    The fake ``/proc`` is left out because a stopped server leaves it. Links
    are listed, never followed.
    """
    root = helper._test_proc_root.parent
    out: dict[str, str] = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            relative = path.relative_to(root)
            if relative.parts[0] == "proc" or path == Path(helper.DNSMASQ_BIN):
                continue
            kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
            out[str(relative)] = kind
    return out


@pytest.mark.parametrize("umask", [0o077, 0o000])
def test_dnsmasq_tree_modes_do_not_depend_on_the_callers_umask(helper, monkeypatch, umask):
    """#334: the tree's modes do not follow the umask of whoever ran sudo.

    A tight one would lock the backend out of the leases it only reads; a
    loose one would leave the tree open to everyone.
    """
    (_legacy_dir(helper) / "leases").write_text(_LEASE)
    os.umask(umask)
    assert _start_dnsmasq(helper, monkeypatch, _NAT_START) == 0
    bridge_root = helper._dnsmasq_paths("novec0den1")[0].parent
    for directory in (helper.DNSMASQ_ROOT.parent, helper.DNSMASQ_ROOT, bridge_root):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    for name in ("dnsmasq.conf", "leases"):
        assert stat.S_IMODE((bridge_root / name).stat().st_mode) == 0o644
    assert sorted(path.name for path in bridge_root.iterdir()) == ["dnsmasq.conf", "leases"]


@pytest.mark.parametrize("level", ["parent", "root"])
@pytest.mark.parametrize("planted", ["symlink", "file"])
def test_dnsmasq_verbs_refuse_a_tree_that_is_not_a_plain_directory(
    helper, monkeypatch, capsys, level, planted
):
    """#334: a link or a file where a tree directory belongs is refused, never followed."""
    outside = helper._test_proc_root.parent / "outside"
    outside.mkdir()
    (outside / "victim").write_text("keep\n")
    target = helper.DNSMASQ_ROOT.parent if level == "parent" else helper.DNSMASQ_ROOT
    target.parent.mkdir(parents=True, exist_ok=True)
    if planted == "symlink":
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.write_text("not a directory\n")
    legacy = _legacy_dir(helper)
    (legacy / "leases").write_text(_LEASE)
    (legacy / "dnsmasq.pid").write_text("701\n")
    _seed_legacy_dnsmasq(helper, 701, "novec0den1")
    killed = _fake_kill(helper, monkeypatch)

    # Start fails closed: nothing is written or spawned, no server is stopped
    # and the legacy leases are left for a later attempt.
    before = _written(helper)
    assert helper.main(_NAT_START) == 1
    assert "refusing to use" in capsys.readouterr().err
    assert killed == [] and helper._test_calls == []
    assert (legacy / "leases").read_text() == _LEASE
    assert _written(helper) == before

    # Stop still removes the server, which is found through /proc alone. Only
    # the work below the tree is skipped, and the verb says so.
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 1
    assert killed == [(701, helper.signal.SIGTERM)]
    assert "refusing to use" in capsys.readouterr().err
    assert sorted(path.name for path in outside.iterdir()) == ["victim"]
    assert not (legacy / "dnsmasq.pid").exists()


def _tree_dir(helper, level: str) -> Path:
    """One of the three directories a bridge's state lives under."""
    return {
        "parent": helper.DNSMASQ_ROOT.parent,
        "root": helper.DNSMASQ_ROOT,
        "bridge": helper._dnsmasq_paths("novec0den1")[0].parent,
    }[level]


@pytest.mark.parametrize("level", ["parent", "root", "bridge"])
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(0o777, id="world-and-group-writable"),
        pytest.param(0o720, id="group-writable"),
        pytest.param(0o702, id="world-writable"),
    ],
)
def test_dnsmasq_start_refuses_a_tree_directory_others_may_write(
    helper, monkeypatch, capsys, level, mode
):
    """#334: only a directory that nobody but root may write is trusted."""
    tree_dir = _tree_dir(helper, level)
    tree_dir.mkdir(parents=True)
    tree_dir.chmod(mode)
    killed = _fake_kill(helper, monkeypatch)

    assert helper.main(_NAT_START) == 1
    expected = f"refusing to use {tree_dir}: writable by group or others\n"
    assert capsys.readouterr().err == expected
    assert killed == [] and helper._test_calls == []
    assert list(tree_dir.iterdir()) == []


def test_dnsmasq_start_refuses_a_tree_owned_by_someone_else(helper, monkeypatch, capsys):
    """#334: the owner must be root, not merely "not writable by others"."""
    helper.DNSMASQ_ROOT.mkdir(parents=True)
    # Unprivileged, the test cannot make another user's directory, so it
    # moves the trusted owner one uid away instead.
    monkeypatch.setattr(helper, "_TRUSTED_UID", os.geteuid() + 1, raising=True)
    killed = _fake_kill(helper, monkeypatch)

    assert helper.main(_NAT_START) == 1
    owner = f"owned by uid {os.geteuid()}, expected {os.geteuid() + 1}"
    assert capsys.readouterr().err == f"refusing to use {helper.DNSMASQ_ROOT.parent}: {owner}\n"
    assert killed == [] and helper._test_calls == []


def test_dnsmasq_stop_skips_only_the_pid_file_below_an_untrusted_tree(helper, monkeypatch, capsys):
    """#334: a tree the helper refuses to write must not keep a server alive."""
    bridge_root = helper._dnsmasq_paths("novec0den1")[0].parent
    bridge_root.mkdir(parents=True)
    (bridge_root / "dnsmasq.pid").write_text("703\n")
    bridge_root.chmod(0o777)
    _seed_dnsmasq(helper, 703, "novec0den1")
    killed = _fake_kill(helper, monkeypatch)

    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 1
    assert killed == [(703, helper.signal.SIGTERM)]
    assert "refusing to use" in capsys.readouterr().err
    assert (bridge_root / "dnsmasq.pid").read_text() == "703\n"


def test_dnsmasq_start_keeps_legacy_leases_until_the_server_is_up(helper, monkeypatch):
    """#334: a start that fails must not eat the leases, and a retry must not import twice."""
    legacy = _legacy_dir(helper)
    (legacy / "leases").write_text(_LEASE)
    note = helper._legacy_lease_note("novec0den1")

    # The daemon exits right away: the copy is kept, the legacy file is not consumed.
    assert helper.main(_NAT_START) == 1
    leases = helper._dnsmasq_paths("novec0den1")[2]
    assert leases.read_text() == _LEASE and note.exists()
    assert (legacy / "leases").read_text() == _LEASE
    assert not (legacy / "leases.pre-334").exists()

    # The retry keeps that copy instead of importing the file again, so a
    # legacy file changed in between never yields a second, different lease
    # file. It is the retry, with its server up, that consumes the legacy file.
    (legacy / "leases").write_text("0 aa:bb:cc:dd:ee:ff 10.255.0.100 swapped *\n")
    assert _start_dnsmasq(helper, monkeypatch, _NAT_START) == 0
    assert leases.read_text() == _LEASE
    assert not note.exists()
    assert not (legacy / "leases").exists()
    assert "swapped" in (legacy / "leases.pre-334").read_text()


def test_dnsmasq_stop_removes_the_pid_file_of_the_old_layout(helper, monkeypatch):
    """#334: a pid file in the service-owned directory must not outlive its server."""
    legacy = _legacy_dir(helper)
    (legacy / "dnsmasq.pid").write_text("704\n")
    _seed_legacy_dnsmasq(helper, 704, "novec0den1")
    killed = _fake_kill(helper, monkeypatch)

    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert killed == [(704, helper.signal.SIGTERM)]
    assert not (legacy / "dnsmasq.pid").exists()


def test_migrated_leases_stay_readable_by_the_backend(helper):
    """#334: root writes the copy; the unprivileged backend only reads it."""
    (_legacy_dir(helper) / "leases").write_text(_LEASE)
    os.umask(0o077)
    leases = helper._dnsmasq_paths("novec0den1")[2]
    helper._migrate_legacy_leases("novec0den1", leases)
    assert leases.read_text() == _LEASE
    assert stat.S_IMODE(leases.stat().st_mode) == 0o644


def test_private_state_writer_uses_distinct_temporary_names(helper, tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    staged = []
    replace = helper.os.replace

    def record_replace(source, destination):
        staged.append(source.name)
        replace(source, destination)

    monkeypatch.setattr(helper.os, "replace", record_replace)
    helper._write_private_file(target, "first", root=tmp_path)
    helper._write_private_file(target, "second", root=tmp_path)

    assert len(set(staged)) == 2
    assert target.read_text() == "second"


def test_private_state_writer_cleans_failed_attempt_and_retries(helper, tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("previous")
    leftover = tmp_path / f".state.json.{os.getpid()}.tmp"
    leftover.write_text("leftover from an older helper")

    def fail_replace(_source, _destination):
        raise OSError("Replace failed")

    with monkeypatch.context() as patch:
        patch.setattr(helper.os, "replace", fail_replace)
        with pytest.raises(OSError, match="Replace failed"):
            helper._write_private_file(target, "updated", root=tmp_path)

    assert target.read_text() == "previous"
    assert set(tmp_path.glob(".state.json.*.tmp")) == {leftover}
    helper._write_private_file(target, "updated", root=tmp_path)

    assert target.read_text() == "updated"
    assert leftover.read_text() == "leftover from an older helper"
    assert set(tmp_path.glob(".state.json.*.tmp")) == {leftover}


def _load_helper(monkeypatch, **env):
    """The helper as the service account's sudo runs it: no ``NOVA_VE_*`` in the environment."""
    for name in list(os.environ):
        if name.startswith("NOVA_VE_"):
            monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("nova_ve_net_helper_defaults", HELPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("source", ["SUDO_UID", "NOVA_VE_SERVICE_USER"])
def test_dnsmasq_runtime_account_must_differ_from_the_sudo_caller(monkeypatch, source):
    mod = _load_helper(monkeypatch)
    uid = mod.pwd.getpwnam(mod.DNSMASQ_USER).pw_uid
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.setenv(source, str(uid) if source == "SUDO_UID" else mod.DNSMASQ_USER)
    with pytest.raises(OSError, match="must use different accounts"):
        mod._dnsmasq_uids()


def test_dnsmasq_root_defaults_to_the_run_tree_and_not_to_the_data_dir(monkeypatch):
    """#334: only root's own setting moves the tree, never the data directory.

    No verb runs here. The module is loaded for its constants and the paths
    the verbs would use are only computed, so nothing is written anywhere.
    """
    mod = _load_helper(monkeypatch)
    assert mod.DNSMASQ_ROOT == Path("/run/nova-ve/dnsmasq")
    conf, pidfile, leases = mod._dnsmasq_paths("novec0den1")
    assert (conf, pidfile, leases) == tuple(
        Path("/run/nova-ve/dnsmasq/novec0den1") / name for name in ("dnsmasq.conf", "dnsmasq.pid", "leases")
    )
    assert mod._legacy_dnsmasq_paths("novec0den1") == (
        Path("/var/lib/nova-ve/nat-cloud/novec0den1/dnsmasq.conf"),
        Path("/var/lib/nova-ve/nat-cloud/novec0den1/dnsmasq.pid"),
        Path("/var/lib/nova-ve/nat-cloud/novec0den1/leases"),
    )

    # Moving the data directory moves the legacy paths, not the tree.
    moved = _load_helper(monkeypatch, NOVA_VE_RUNTIME_ROOT="/elsewhere/data")
    assert moved.RUNTIME_ROOT == Path("/elsewhere/data")
    assert moved.DNSMASQ_ROOT == Path("/run/nova-ve/dnsmasq")
    assert moved._dnsmasq_paths("novec0den1") == (conf, pidfile, leases)

    # The backend reads the same tree from a constant of its own.
    from app.services import network_info_service

    assert network_info_service.DNSMASQ_ROOT == mod.DNSMASQ_ROOT


_CAP = 1_048_576

# One helper verb in a fresh interpreter, with every side effect removed
# before ``main`` runs: ``os.kill`` refuses, the spawn point only records a
# server in a ``/proc`` of the test's own, and every path is under the test's
# scratch directory. A helper that blocks on a planted file is killed by the
# timeout instead of hanging the suite.
_ISOLATED_RUNNER = """
import importlib.util
import sys
from pathlib import Path

helper_file, scratch, *argv = sys.argv[1:]
spec = importlib.util.spec_from_file_location("nova_ve_net_helper_isolated", helper_file)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def refuse(*_args):
    raise AssertionError("the isolated helper must not signal a process")


def spawn(command):
    entry = mod.PROC_ROOT / "7001"
    entry.mkdir()
    (entry / "cmdline").write_bytes(f"dnsmasq\\0{command[1]}\\0".encode())
    (entry / "exe").symlink_to(Path(mod.DNSMASQ_BIN).resolve())
    (entry / "status").write_text("Uid:\\t65534\\t65534\\t65534\\t65534\\nCapEff:\\t2000\\n")
    (entry / "ns").mkdir()
    for name in ("user", "net", "mnt"):
        (entry / "ns" / name).symlink_to(mod.PROC_ROOT / "self" / "ns" / name)
    return 0


mod.os.kill = refuse
mod.signal.pidfd_send_signal = refuse
mod.os.pidfd_open = lambda pid: mod.os.open(mod.PROC_ROOT / str(pid) / "cmdline", mod.os.O_RDONLY)
mod._dnsmasq_pidfd_exited = lambda fd: False
mod._dnsmasq_uids = lambda: {65534}
mod._run = spawn
dnsmasq = Path(scratch) / "dnsmasq-fixture"
dnsmasq.write_bytes(b"dnsmasq executable fixture")
dnsmasq.chmod(0o755)
mod.DNSMASQ_BIN = str(dnsmasq)
mod._TRUSTED_UID = mod.os.geteuid()
mod.PROC_ROOT = Path(scratch) / "proc"
namespaces = mod.PROC_ROOT / "self" / "ns"
namespaces.mkdir(parents=True)
for name in ("user", "net", "mnt"):
    (namespaces / name).write_text(f"host {name} namespace fixture")
mod.RUNTIME_ROOT = Path(scratch) / "runtime"
mod.DNSMASQ_ROOT = Path(scratch) / "run" / "dnsmasq"
sys.exit(mod.main(argv))
"""


def _start_isolated(scratch: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` through the helper in its own process, bounded in time."""
    try:
        return subprocess.run(
            [sys.executable, "-B", "-c", _ISOLATED_RUNNER, str(HELPER_PATH), str(scratch), *argv],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the helper blocked on a planted legacy file")


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("fifo", id="fifo-without-writer"),
        pytest.param("fed-fifo", id="fifo-with-text-in-it"),
        pytest.param("socket", id="unix-socket"),
        pytest.param("directory", id="directory"),
        pytest.param("symlink", id="symlink"),
    ],
)
def test_dnsmasq_start_never_migrates_leases_that_are_not_a_regular_file(tmp_path, tmp_path_factory, kind):
    """#334: the legacy directory is the service account's, so ``leases`` can be anything."""
    legacy = tmp_path / "runtime" / "nat-cloud" / "novec0den1"
    legacy.mkdir(parents=True)
    (tmp_path / "proc").mkdir()
    leases = legacy / "leases"
    victim = tmp_path / "victim"
    victim.write_text("root-only\n")
    held: list[int] = []
    server = None
    if kind in ("fifo", "fed-fifo"):
        # Without a writer a plain open blocks forever: the helper must not
        # wait for one to turn up.
        os.mkfifo(leases)
        if kind == "fed-fifo":
            # A reader of our own lets a writer in, so text is already
            # waiting in the pipe when the helper looks at the file.
            held.append(os.open(leases, os.O_RDONLY | os.O_NONBLOCK))
            held.append(os.open(leases, os.O_WRONLY | os.O_NONBLOCK))
            os.write(held[-1], _LEASE.encode())
    elif kind == "socket":
        # A socket path may not be long, so it is bound in a short directory
        # of the test's own and then moved into place.
        bound = tmp_path_factory.mktemp("sock") / "leases"
        if len(str(bound)) > 100:
            pytest.skip("no path short enough for a unix socket on this host")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(bound))
        os.rename(bound, leases)
    elif kind == "directory":
        leases.mkdir()
        (leases / "leases").write_text("not a lease file\n")
    else:
        leases.symlink_to(victim)
    expected = stat.S_IFMT(os.lstat(leases).st_mode)

    try:
        proc = _start_isolated(tmp_path, _NAT_START)
    finally:
        for fd in held:
            os.close(fd)
        if server is not None:
            server.close()

    # The server came up: the start succeeded, only without those leases.
    assert proc.returncode == 0
    assert "Traceback" not in proc.stderr
    # Only the configuration every start writes: no lease file was made.
    new_root = tmp_path / "run" / "dnsmasq" / "novec0den1"
    assert sorted(path.name for path in new_root.iterdir()) == ["dnsmasq.conf"]
    # The planted entry is still exactly what it was, under its own name.
    assert stat.S_IFMT(os.lstat(leases).st_mode) == expected
    assert not (legacy / "leases.pre-334").exists()
    assert victim.read_text() == "root-only\n"


def test_dnsmasq_start_does_not_migrate_an_oversized_legacy_lease_file(helper, monkeypatch):
    """#334: the service account must not be able to fill /run through the helper."""
    legacy = _legacy_dir(helper)
    content = b"0 aa:bb:cc:dd:ee:ff 10.255.0.100 host *\n" * (_CAP // 40 + 2)
    assert len(content) > _CAP
    (legacy / "leases").write_bytes(content)
    assert _start_dnsmasq(helper, monkeypatch, _NAT_START) == 0
    bridge_root = helper._dnsmasq_paths("novec0den1")[0].parent
    assert sorted(path.name for path in bridge_root.iterdir()) == ["dnsmasq.conf"]
    # Refused, so never copied and never consumed: left as it was.
    assert (legacy / "leases").read_bytes() == content
    assert not (legacy / "leases.pre-334").exists()


def test_dnsmasq_start_migrates_a_legacy_lease_file_exactly_at_the_cap(helper, monkeypatch):
    legacy = _legacy_dir(helper)
    content = b"#" * (_CAP - len(_LEASE)) + _LEASE.encode()
    assert len(content) == _CAP
    (legacy / "leases").write_bytes(content)
    assert _start_dnsmasq(helper, monkeypatch, _NAT_START) == 0
    bridge_root = helper._dnsmasq_paths("novec0den1")[0].parent
    assert sorted(path.name for path in bridge_root.iterdir()) == ["dnsmasq.conf", "leases"]
    assert helper._dnsmasq_paths("novec0den1")[2].read_bytes() == content
    assert (legacy / "leases.pre-334").read_bytes() == content


@pytest.mark.parametrize("guard", ["fstat", "read"])
def test_each_size_guard_refuses_an_oversized_legacy_lease_file_on_its_own(helper, monkeypatch, guard):
    """#334: the two guards cover different moments, so each must hold without the other.

    ``fstat`` sees a small file and is told it is too large. ``read`` sees a
    file that really is too large while ``fstat`` is told it is small, which
    is what a writer racing the helper could arrange.
    """
    legacy = _legacy_dir(helper)
    content = b"#" * (_CAP + 1) if guard == "read" else _LEASE.encode()
    (legacy / "leases").write_bytes(content)
    real_fstat = os.fstat
    reported = 4 if guard == "read" else _CAP + 1

    def fstat(fd):
        info = real_fstat(fd)
        return os.stat_result(info[:6] + (reported,) + info[7:])

    monkeypatch.setattr(helper.os, "fstat", fstat)
    leases = helper._dnsmasq_paths("novec0den1")[2]
    helper._migrate_legacy_leases("novec0den1", leases)
    assert not leases.exists()
    assert (legacy / "leases").read_bytes() == content


@pytest.mark.parametrize(
    "argv",
    [
        # management bridges never run isolated DHCP
        ["dnsmasq-start", "novec0demg", "10.40.1.254", "10.40.1.128", "10.40.1.253", "--no-router"],
        # server inside its own scope
        ["dnsmasq-start", "novec0den2", "10.40.1.200", "10.40.1.128", "10.40.1.253", "--no-router"],
    ],
)
def test_dnsmasq_start_isolated_rejects_bad_arguments(helper, argv):
    assert helper.main(argv) == 2
    assert helper._test_calls == []


def _bridge_conf(helper, bridge="novec0den2"):
    root = helper.IPV4_CONF_ROOT / bridge
    root.mkdir(parents=True)
    (root / "arp_ignore").write_text("0\n")
    return root / "arp_ignore"


def test_bridge_guard_apply_installs_one_transaction(helper):
    arp_ignore = _bridge_conf(helper)
    assert helper.main(["bridge-guard-apply", "novec0den2"]) == 0
    assert arp_ignore.read_text() == "1\n"
    [(argv, script)] = helper._test_input_calls
    assert argv == [helper.NFT_BIN, "-f", "-"]
    lines = script.splitlines()
    table = "inet nova_ve_bridge_guard"
    assert lines[:5] == [
        f"add table {table}",
        f"add chain {table} in_novec0den2 {{ type filter hook input priority -10; policy accept; }}",
        f"add chain {table} fwd_novec0den2 {{ type filter hook forward priority -10; policy accept; }}",
        f"flush chain {table} in_novec0den2",
        f"flush chain {table} fwd_novec0den2",
    ]
    assert lines[5:] == [
        f'add rule {table} in_novec0den2 iifname "novec0den2" meta nfproto ipv4 udp sport 68 udp dport 67 accept',
        f'add rule {table} in_novec0den2 iifname "novec0den2" icmp type echo-reply accept',
        f'add rule {table} in_novec0den2 iifname "novec0den2" drop',
        f'add rule {table} fwd_novec0den2 iifname "novec0den2" oifname "novec0den2" accept',
        f'add rule {table} fwd_novec0den2 iifname "novec0den2" drop',
        f'add rule {table} fwd_novec0den2 oifname "novec0den2" drop',
    ]
    assert not any("nat" in line or "masquerade" in line for line in lines)


def test_bridge_guard_remove_is_a_no_op_when_absent(helper, monkeypatch):
    monkeypatch.setattr(
        helper,
        "_run_capture",
        lambda argv: helper.subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such file"),
    )
    assert helper.main(["bridge-guard-remove", "novec0den2"]) == 0
    assert helper._test_input_calls == []
    assert helper._test_calls == []


def test_bridge_guard_apply_fails_for_a_missing_bridge(helper, capsys):
    assert helper.main(["bridge-guard-apply", "novec0den2"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_bridge_guard_refuses_a_symlinked_lock(helper, tmp_path):
    _bridge_conf(helper)
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    helper.GUARD_LOCK_PATH.symlink_to(target)
    assert helper.main(["bridge-guard-apply", "novec0den2"]) == 1
    assert helper._test_input_calls == []
    assert target.read_text() == "keep\n"


def test_bridge_guard_remove_deletes_its_chains_and_the_empty_table(helper, monkeypatch):
    arp_ignore = _bridge_conf(helper)
    arp_ignore.write_text("1\n")
    listings: list[list[str]] = []

    def capture(argv):
        listings.append(list(argv))
        # Both chains exist; afterwards the table holds no chain.
        stdout = "table inet nova_ve_bridge_guard {\n}\n" if argv[2] == "table" else ""
        return helper.subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(helper, "_run_capture", capture)
    assert helper.main(["bridge-guard-remove", "novec0den2"]) == 0
    assert [text for _argv, text in helper._test_input_calls] == [
        "flush chain inet nova_ve_bridge_guard in_novec0den2\n"
        "delete chain inet nova_ve_bridge_guard in_novec0den2\n",
        "flush chain inet nova_ve_bridge_guard fwd_novec0den2\n"
        "delete chain inet nova_ve_bridge_guard fwd_novec0den2\n",
    ]
    assert helper._test_calls == [[helper.NFT_BIN, "delete", "table", "inet", "nova_ve_bridge_guard"]]
    assert arp_ignore.read_text() == "0\n"


def test_bridge_guard_remove_keeps_a_table_other_bridges_use(helper, monkeypatch):
    def capture(argv):
        stdout = (
            "table inet nova_ve_bridge_guard {\n\tchain in_novec0den3 {\n\t}\n}\n"
            if argv[2] == "table"
            else ""
        )
        return helper.subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(helper, "_run_capture", capture)
    assert helper.main(["bridge-guard-remove", "novec0den2"]) == 0
    assert helper._test_calls == []


def _addr_listing(helper, monkeypatch, *, rc=0, stdout="", stderr=""):
    monkeypatch.setattr(
        helper,
        "_run_capture",
        lambda argv: helper.subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr),
    )


def test_bridge_addr_del_removes_a_present_address(helper, monkeypatch):
    _addr_listing(
        helper,
        monkeypatch,
        stdout="7: novec0den2    inet 10.40.1.254/24 scope global novec0den2\\       valid_lft forever\n",
    )
    assert helper.main(["bridge-addr-del", "novec0den2", "10.40.1.254/24"]) == 0
    assert helper._test_calls == [[helper.IP_BIN, "addr", "del", "10.40.1.254/24", "dev", "novec0den2"]]


@pytest.mark.parametrize(
    "listing",
    [
        {"stdout": "7: novec0den2    inet 10.40.1.1/24 scope global novec0den2\n"},
        {"rc": 1, "stderr": 'Device "novec0den2" does not exist.\n'},
    ],
)
def test_bridge_addr_del_succeeds_when_already_gone(helper, monkeypatch, listing):
    _addr_listing(helper, monkeypatch, **listing)
    assert helper.main(["bridge-addr-del", "novec0den2", "10.40.1.254/24"]) == 0
    assert helper._test_calls == []


def test_bridge_addr_del_reports_other_listing_failures(helper, monkeypatch):
    _addr_listing(helper, monkeypatch, rc=1, stderr="permission denied\n")
    assert helper.main(["bridge-addr-del", "novec0den2", "10.40.1.254/24"]) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["bridge-guard-apply", "novec0demg"],
        ["bridge-guard-apply", "br-eth0"],
        ["bridge-guard-apply", "novec0den2; nft flush ruleset"],
        ["bridge-guard-remove", "eth0"],
        ["bridge-addr-del", "novec0demg", "10.40.1.254/24"],
        ["bridge-addr-del", "novec0den2", "10.40.1.254"],
        ["bridge-addr-del", "novec0den2", "fe80::1/64"],
    ],
)
def test_bridge_dhcp_verbs_reject_bad_arguments(helper, argv):
    assert helper.main(argv) == 2
    assert helper._test_calls == []
    assert helper._test_input_calls == []


# ---------------------------------------------------------------------------
# Regex rejection: each verb refuses bad arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "validator_name,bridge_name",
    [
        ("validate_bridge_name", "nove0123n1"),
        ("validate_bridge_name", "nove0123mg"),
        ("validate_network_bridge_name", "nove0123n99999"),
    ],
)
def test_bridge_name_rejects_terminal_newlines(helper, validator_name, bridge_name, terminal):
    validator = getattr(helper, validator_name)

    assert validator(bridge_name) == bridge_name

    with pytest.raises(helper._ValidationError):
        validator(bridge_name + terminal)


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


# ---------------------------------------------------------------------------
# #287 — per-lab management attachment name shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected_tail",
    [
        (
            ["bridge-add", "novec0demg"],
            ["link", "add", "novec0demg", "type", "bridge"],
        ),
        (
            ["bridge-addr-add", "novec0demg", "172.16.42.1/24"],
            ["addr", "replace", "172.16.42.1/24", "dev", "novec0demg"],
        ),
        (
            ["link-master", "nvec0ded1mth", "novec0demg"],
            ["link", "set", "nvec0ded1mth", "master", "novec0demg"],
        ),
        (
            ["veth-pair-add", "nvec0ded1mth", "nvec0ded1mtp"],
            ["link", "add", "nvec0ded1mth", "type", "veth", "peer", "name", "nvec0ded1mtp"],
        ),
        (
            ["link-up", "novec0demg"],
            ["link", "set", "novec0demg", "up"],
        ),
    ],
)
def test_mgmt_verbs_emit_correct_argv(helper, argv, expected_tail):
    """#287: the mgmt bridge (``nove{h}mg``) and mgmt veths (``mt[hp]``)
    pass helper validation and produce the expected argv."""
    rc = helper.main(argv)
    assert rc == 0, f"verb {argv[0]} returned {rc}"
    assert helper._test_calls, "helper did not invoke _run"
    spawned = helper._test_calls[-1]
    assert spawned[1:] == expected_tail


@pytest.mark.parametrize(
    "name",
    [
        "novec0demg2",   # mg followed by digits
        "novec0demG",    # uppercase suffix
        "novec0dem",     # missing g
    ],
)
def test_bridge_add_rejects_bad_mgmt_names(helper, capsys, name):
    rc = helper.main(["bridge-add", name])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize(
    "hostend, peerend",
    [
        ("nvec0ded1mthh", "nvec0ded1mtp"),   # doubled h
        ("nvec0ded1mth", "nvec0ded1mtpx"),   # bad peer suffix
        ("nvec0ded1mtx", "nvec0ded1mtp"),    # x is not h/p
    ],
)
def test_veth_pair_rejects_bad_mgmt_names(helper, hostend, peerend):
    rc = helper.main(["veth-pair-add", hostend, peerend])
    assert rc == 2
    assert not helper._test_calls


def test_mgmt_netns_verbs_accept_mgmt0(helper):
    """#287: the container-side ``mgmt0`` interface passes netns validation."""
    _seed_pid(helper, 4242)
    rc = helper.main(["link-netns", "nvec0ded1mtp", "4242"])
    assert rc == 0
    rc = helper.main(["link-set-name-in-netns", "4242", "nvec0ded1mtp", "mgmt0"])
    assert rc == 0
    assert helper._test_calls[-1][5:] == ["link", "set", "nvec0ded1mtp", "name", "mgmt0"]
    rc = helper.main(["addr-add-in-netns", "4242", "mgmt0", "172.16.42.2/24"])
    assert rc == 0
    assert helper._test_calls[-1][5:] == ["addr", "add", "172.16.42.2/24", "dev", "mgmt0"]
    rc = helper.main(["addr-up-in-netns", "4242", "mgmt0"])
    assert rc == 0
    assert helper._test_calls[-1][5:] == ["link", "set", "mgmt0", "up"]


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


def test_qemu_process_signal_authorizes_registry_qemu_pid(helper, monkeypatch):
    _seed_pid(helper, 5555, kind="qemu")
    calls: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr(helper.os, "killpg", fake_killpg)

    rc = helper.main(["qemu-process-signal", "5555", "term"])

    assert rc == 0
    assert calls == [(5555, helper.signal.SIGTERM)]
    assert not helper._test_calls


def test_qemu_process_signal_rejects_non_qemu_registry_pid(helper, monkeypatch):
    _seed_pid(helper, 4242, kind="docker")
    monkeypatch.setattr(helper.os, "killpg", lambda _pid, _sig: pytest.fail("no kill"))

    rc = helper.main(["qemu-process-signal", "4242", "term"])

    assert rc == 2


def test_qemu_process_signal_rejects_qemu_without_matching_qmp(helper, monkeypatch):
    _seed_pid(helper, 5555, kind="qemu")
    (helper._test_proc_root / "5555" / "cmdline").write_bytes(
        b"/usr/bin/qemu-system-x86_64\0-qmp\0unix:/tmp/other.sock,server,nowait\0"
    )
    monkeypatch.setattr(helper.os, "killpg", lambda _pid, _sig: pytest.fail("no kill"))

    rc = helper.main(["qemu-process-signal", "5555", "term"])

    assert rc == 2


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
