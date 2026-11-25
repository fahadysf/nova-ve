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
import shutil
import signal
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
    monkeypatch.setattr(mod, "RUNTIME_ROOT", tmp_path / "runtime", raising=True)
    monkeypatch.setattr(mod, "IP_FORWARD_PATH", tmp_path / "ip_forward", raising=True)

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
    conf = helper.RUNTIME_ROOT / "nat-cloud" / bridge / "dnsmasq.conf"
    entry = helper._test_proc_root / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(f"/usr/sbin/dnsmasq\0--conf-file={conf}\0".encode())


def _fake_kill(helper, monkeypatch, *, ignores=()):
    """``os.kill`` that removes the fake process unless it ignores the signal."""
    killed: list[tuple[int, int]] = []

    def kill(pid, sig):
        killed.append((pid, sig))
        if sig not in ignores:
            shutil.rmtree(helper._test_proc_root / str(pid))

    monkeypatch.setattr(helper.os, "kill", kill)
    monkeypatch.setattr(helper.time, "sleep", lambda _seconds: None)
    return killed


def test_dnsmasq_stop_finds_server_without_pid_file(helper, monkeypatch):
    _seed_dnsmasq(helper, 501, "novec0den1")
    killed = _fake_kill(helper, monkeypatch)
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert killed == [(501, helper.signal.SIGTERM)]


def test_dnsmasq_stop_ignores_stale_pid_file_of_another_network(helper, monkeypatch):
    # n1 vs n10: names that share a prefix must not match either.
    _seed_dnsmasq(helper, 502, "novec0den10")
    root = helper.RUNTIME_ROOT / "nat-cloud" / "novec0den1"
    root.mkdir(parents=True)
    (root / "dnsmasq.pid").write_text("502\n")
    killed = _fake_kill(helper, monkeypatch)
    assert helper.main(["dnsmasq-stop", "novec0den1"]) == 0
    assert killed == []
    assert not (root / "dnsmasq.pid").exists()


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
    conf = helper.RUNTIME_ROOT / "nat-cloud" / "novec0den1" / "dnsmasq.conf"
    text = conf.read_text()
    assert "interface=novec0den1" in text
    assert "port=0" in text
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
    lines = (helper.RUNTIME_ROOT / "nat-cloud" / "novec0den2" / "dnsmasq.conf").read_text().splitlines()
    assert "listen-address=10.40.1.254" in lines
    assert "port=0" in lines
    assert "dhcp-range=10.40.1.128,10.40.1.253,12h" in lines
    # Empty options: dnsmasq then sends neither a router nor a DNS server.
    assert "dhcp-option=3" in lines and "dhcp-option=6" in lines
    assert not any("router" in line for line in lines)
    # A lab router may serve the same segment; do not NAK its clients.
    assert "dhcp-authoritative" not in lines


def test_dnsmasq_start_keeps_router_for_nat_cloud(helper, monkeypatch):
    rc = _start_dnsmasq(
        helper, monkeypatch, ["dnsmasq-start", "novec0den1", "10.255.0.1", "10.255.0.100", "10.255.0.254"]
    )
    assert rc == 0
    lines = (helper.RUNTIME_ROOT / "nat-cloud" / "novec0den1" / "dnsmasq.conf").read_text().splitlines()
    assert "dhcp-option=option:router,10.255.0.1" in lines
    assert "dhcp-authoritative" in lines
    assert "dhcp-option=3" not in lines


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


def test_bridge_guard_apply_installs_one_transaction(helper):
    assert helper.main(["bridge-guard-apply", "novec0den2"]) == 0
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


def test_bridge_guard_remove_deletes_its_chains_and_the_empty_table(helper, monkeypatch):
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
