#!/usr/bin/env python3
"""nova-ve privileged network helper.

Single privileged binary invoked via ``sudo`` from the un-privileged
``nova-ve`` backend.  Exposes a *fixed* set of verbs — there is no generic
``in-netns`` escape hatch.  Each verb maps to one ``ip`` (or ``nsenter ip``)
invocation with all arguments validated against tight regular expressions
before any subprocess is spawned.

Authoritative spec: ``.omc/plans/network-runtime-wiring.md`` § US-201.

The helper:

* uses ``subprocess.run([...], shell=False)`` exclusively — argv is never
  spliced into a shell string;
* validates every argv argument against a tight regex (kernel
  ``IFNAMSIZ=16`` → max 15 usable chars, plus argument-specific shape);
* for verbs that take a *pid*, verifies pid ownership against the
  nova-ve runtime registry at ``/var/lib/nova-ve/runtime/pids.json``;
* exits ``2`` on any validation / ownership failure with a structured
  diagnostic on stderr.

Exit codes
----------
0   success
2   argument failed validation OR pid ownership check failed
3   unknown verb (argparse rejection)
1   underlying ``ip`` / ``nsenter`` invocation failed
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path to the nova-ve runtime pid registry.  Settable via env var so tests
# can point at a tmp_path fixture.
PIDS_JSON_PATH = Path(
    os.environ.get("NOVA_VE_PIDS_JSON", "/var/lib/nova-ve/runtime/pids.json")
)

# Allow tests to override the path the helper uses to inspect /proc.
PROC_ROOT = Path(os.environ.get("NOVA_VE_PROC_ROOT", "/proc"))

# Allow tests to inject a fake ``ip`` / ``nsenter`` binary.  Production
# leaves these as None so we use $PATH.
IP_BIN = os.environ.get("NOVA_VE_IP_BIN") or "ip"
NSENTER_BIN = os.environ.get("NOVA_VE_NSENTER_BIN") or "nsenter"
NFT_BIN = os.environ.get("NOVA_VE_NFT_BIN") or "nft"
DNSMASQ_BIN = os.environ.get("NOVA_VE_DNSMASQ_BIN") or "dnsmasq"
RUNTIME_ROOT = Path(os.environ.get("NOVA_VE_RUNTIME_ROOT", "/var/lib/nova-ve"))
IP_FORWARD_PATH = Path(os.environ.get("NOVA_VE_IP_FORWARD_PATH", "/proc/sys/net/ipv4/ip_forward"))


# ---------------------------------------------------------------------------
# Regex catalogue (single source of truth for argv shape)
# ---------------------------------------------------------------------------

# kernel IFNAMSIZ = 16 (incl NUL) → 15 usable.  Each format below caps to the
# advertised length so overflow is impossible even if the regex is reused.

# Bridge: nove<lab_hash:04x>n<network_id 1-5 digits>     max 14
RE_BRIDGE_NAME = re.compile(r"^nove[a-f0-9]{4}n[0-9]{1,5}$")

# TAP (QEMU): nve<lab_hash:04x>d<node 1-3>i<iface 1-2>     max 13
RE_TAP_NAME = re.compile(r"^nve[a-f0-9]{4}d[0-9]{1,3}i[0-9]{1,2}$")

# Veth host/peer: same as TAP plus h/p suffix                max 14
RE_VETH_NAME = re.compile(r"^nve[a-f0-9]{4}d[0-9]{1,3}i[0-9]{1,2}[hp]$")

# Container netns interface name: ethN where N is 0-99
RE_NETNS_IFACE = re.compile(r"^eth[0-9]{1,2}$")

# Pid: 1-7 digits.  Additional runtime checks reject pid<10 and pid==1.
RE_PID = re.compile(r"^[0-9]{1,7}$")

# TCP port (numeric, 1-65535).
RE_PORT = re.compile(r"^[0-9]{1,5}$")
RE_HOST_IFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")

# Path to the bundled console TCP forwarder.  Test override via env var.
CONSOLE_PROXY_BIN = os.environ.get(
    "NOVA_VE_CONSOLE_PROXY_BIN", "/opt/nova-ve/bin/nova-ve-console-proxy.py"
)


# Sentinel returned by validate_pid for pids in the kernel/init reserved
# range or non-matching the registry.
class _ValidationError(Exception):
    """Raised by argument validators on regex / ownership failure."""


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _check_regex(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise _ValidationError(label)
    return value


def validate_bridge_name(value: str) -> str:
    return _check_regex(value, RE_BRIDGE_NAME, "bridge_name")


def validate_tap_name(value: str) -> str:
    return _check_regex(value, RE_TAP_NAME, "tap_name")


def validate_veth_name(value: str, *, label: str = "veth_name") -> str:
    return _check_regex(value, RE_VETH_NAME, label)


def validate_iface_name(value: str, *, label: str = "iface_name") -> str:
    """Accept any nova-ve-owned interface name (TAP, veth, or bridge).

    Bridges are accepted so generic verbs like ``link-up`` / ``link-del``
    can target a bridge without needing a bridge-specific verb.
    """
    if (
        RE_TAP_NAME.match(value)
        or RE_VETH_NAME.match(value)
        or RE_BRIDGE_NAME.match(value)
    ):
        return value
    raise _ValidationError(label)


def validate_netns_iface(value: str) -> str:
    return _check_regex(value, RE_NETNS_IFACE, "netns_iface")


def validate_pid_shape(value: str) -> int:
    """Validate pid *shape* only (regex + reserved-range)."""
    if not RE_PID.match(value or ""):
        raise _ValidationError("pid")
    pid = int(value)
    # pid==1 is init/systemd, pid<10 covers kernel threads and the early
    # systemd init range.  Refuse to operate on these even before
    # consulting the registry.
    if pid < 10 or pid == 1:
        raise _ValidationError("pid")
    return pid


def validate_cidr(value: str) -> str:
    """Accept a single IPv4 CIDR (e.g. 10.99.1.42/24).  IPv6 deferred."""
    try:
        iface = ipaddress.IPv4Interface(value)
    except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        raise _ValidationError("cidr")
    # ipaddress accepts ``10.0.0.1`` (no /N) — require explicit prefix.
    if "/" not in value:
        raise _ValidationError("cidr")
    # Reject prefix lengths that would be useless for container IPs.
    if iface.network.prefixlen < 0 or iface.network.prefixlen > 32:
        raise _ValidationError("cidr")
    return f"{iface.ip}/{iface.network.prefixlen}"


def validate_network_cidr(value: str) -> str:
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        raise _ValidationError("network_cidr")
    return str(network)


def validate_ipv4(value: str, *, label: str = "ipv4") -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except (ValueError, ipaddress.AddressValueError):
        raise _ValidationError(label)


def validate_host_iface(value: str) -> str:
    return _check_regex(value, RE_HOST_IFACE, "host_iface")


# ---------------------------------------------------------------------------
# Pid ownership: registry + cgroup/comm defense-in-depth
# ---------------------------------------------------------------------------


def _load_pid_registry() -> list[dict]:
    """Load the runtime pid registry.

    Missing file or corrupted JSON → return ``[]`` (deny all).  A diagnostic
    is emitted to stderr in the corrupted case so operators can spot it.
    """
    try:
        raw = PIDS_JSON_PATH.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(
            f"pids.json read failed: {exc}; denying all pid-taking verbs",
            file=sys.stderr,
        )
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"pids.json corrupted ({exc}); denying all pid-taking verbs",
            file=sys.stderr,
        )
        return []
    if not isinstance(data, list):
        print(
            "pids.json malformed (expected list); denying all pid-taking verbs",
            file=sys.stderr,
        )
        return []
    out: list[dict] = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("pid"), int):
            out.append(entry)
    return out


def _registry_lookup(pid: int) -> dict | None:
    for entry in _load_pid_registry():
        if entry.get("pid") == pid:
            return entry
    return None


# Cgroup fingerprint patterns (defense-in-depth secondary check).
RE_CGROUP_DOCKER_V1 = re.compile(r"docker[/\-][0-9a-f]{12,}")
RE_CGROUP_CRIO_V1 = re.compile(r"crio[/\-][0-9a-f]{12,}")
RE_CGROUP_DOCKER_V2_SCOPE = re.compile(r"docker-[0-9a-f]{12,}\.scope")
RE_CGROUP_CONTAINERD_V2_SCOPE = re.compile(r"containerd-[0-9a-f]{12,}\.scope")
RE_COMM_QEMU = re.compile(r"^qemu-system-.+$")


def _read_proc_text(pid: int, name: str) -> str:
    path = PROC_ROOT / str(pid) / name
    try:
        return path.read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return ""


def _cgroup_or_comm_matches(pid: int) -> bool:
    cgroup = _read_proc_text(pid, "cgroup")
    if (
        RE_CGROUP_DOCKER_V1.search(cgroup)
        or RE_CGROUP_CRIO_V1.search(cgroup)
        or RE_CGROUP_DOCKER_V2_SCOPE.search(cgroup)
        or RE_CGROUP_CONTAINERD_V2_SCOPE.search(cgroup)
    ):
        return True
    comm = _read_proc_text(pid, "comm").strip()
    if comm and RE_COMM_QEMU.match(comm):
        return True
    return False


def _emit_ownership_diagnostic(pid: int) -> None:
    comm = _read_proc_text(pid, "comm").rstrip("\n")[:80]
    cgroup_lines = _read_proc_text(pid, "cgroup").splitlines()[:5]
    print(f"pid ownership check FAILED for pid={pid}", file=sys.stderr)
    print(f"  /proc/{pid}/comm: {comm}", file=sys.stderr)
    print(f"  /proc/{pid}/cgroup (first 5 lines):", file=sys.stderr)
    for line in cgroup_lines:
        print(f"    {line}", file=sys.stderr)
    print(
        "  expected one of: docker[/-]<hex>, docker-<hex>.scope, "
        "containerd-<hex>.scope, crio[/-]<hex>, or comm matching qemu-system-*",
        file=sys.stderr,
    )


def authorize_pid(value: str) -> int:
    """Validate pid argv shape AND verify ownership.

    Order: regex shape → reserved range → registry lookup → cgroup/comm
    defense-in-depth.  On any failure raises ``_ValidationError`` after
    emitting the structured diagnostic specified in US-201.
    """
    pid = validate_pid_shape(value)
    if _registry_lookup(pid) is None:
        _emit_ownership_diagnostic(pid)
        raise _ValidationError("pid_not_in_registry")
    if not _cgroup_or_comm_matches(pid):
        _emit_ownership_diagnostic(pid)
        raise _ValidationError("pid_cgroup_mismatch")
    return pid


# ---------------------------------------------------------------------------
# Subprocess invocation (shell=False, fixed argv)
# ---------------------------------------------------------------------------


def _run(argv: Sequence[str]) -> int:
    """Run ``argv`` with ``shell=False``; return the child exit code.

    stdout/stderr are streamed through to the caller so the wrapping
    backend service can capture diagnostics.
    """
    # Defensive: every element of argv must already be a str.  A non-str
    # element here would be a programming error in this file (not user
    # input — argv values arrived as strings via argparse).  Catch it
    # eagerly so a typo can never bypass validation.
    for piece in argv:
        if not isinstance(piece, str):
            raise TypeError(f"argv element not a str: {piece!r}")
    try:
        proc = subprocess.run(list(argv), shell=False, check=False)
    except FileNotFoundError as exc:
        print(f"required binary not found: {exc}", file=sys.stderr)
        return 1
    return proc.returncode


def _run_capture(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    for piece in argv:
        if not isinstance(piece, str):
            raise TypeError(f"argv element not a str: {piece!r}")
    return subprocess.run(
        list(argv),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )


def _ip(*args: str) -> int:
    return _run([IP_BIN, *args])


def _nft(*args: str) -> int:
    return _run([NFT_BIN, *args])


def _nft_quiet(*args: str) -> int:
    return _run_capture([NFT_BIN, *args]).returncode


def _nsenter_ip_netns(pid: int, *args: str) -> int:
    """``nsenter -t <pid> -n ip <args>`` with shell=False."""
    return _run([NSENTER_BIN, "-t", str(pid), "-n", IP_BIN, *args])


def _nsenter_cat_address(pid: int, iface: str) -> int:
    """Read MAC of ``iface`` inside ``pid``'s netns by reading sysfs."""
    return _run(
        [
            NSENTER_BIN,
            "-t",
            str(pid),
            "-n",
            "cat",
            f"/sys/class/net/{iface}/address",
        ]
    )


# ---------------------------------------------------------------------------
# Verb implementations (each verb = one fixed ip/nsenter invocation)
# ---------------------------------------------------------------------------


def cmd_bridge_add(args: argparse.Namespace) -> int:
    name = validate_bridge_name(args.name)
    return _ip("link", "add", name, "type", "bridge")


def cmd_bridge_del(args: argparse.Namespace) -> int:
    name = validate_bridge_name(args.name)
    return _ip("link", "del", name)


def cmd_tap_add(args: argparse.Namespace) -> int:
    name = validate_tap_name(args.name)
    return _ip("tuntap", "add", "dev", name, "mode", "tap")


def cmd_tap_del(args: argparse.Namespace) -> int:
    name = validate_tap_name(args.name)
    return _ip("link", "del", name)


def cmd_link_del(args: argparse.Namespace) -> int:
    name = validate_iface_name(args.name)
    return _ip("link", "del", name)


def cmd_veth_pair_add(args: argparse.Namespace) -> int:
    host = validate_veth_name(args.hostend, label="hostend")
    peer = validate_veth_name(args.peerend, label="peerend")
    if host == peer:
        raise _ValidationError("hostend==peerend")
    return _ip("link", "add", host, "type", "veth", "peer", "name", peer)


def cmd_link_master(args: argparse.Namespace) -> int:
    iface = validate_iface_name(args.iface)
    bridge = validate_bridge_name(args.bridge)
    return _ip("link", "set", iface, "master", bridge)


def cmd_link_set_nomaster(args: argparse.Namespace) -> int:
    iface = validate_iface_name(args.iface)
    return _ip("link", "set", iface, "nomaster")


def cmd_link_netns(args: argparse.Namespace) -> int:
    iface = validate_iface_name(args.iface)
    pid = authorize_pid(args.pid)
    return _ip("link", "set", iface, "netns", str(pid))


def cmd_link_up(args: argparse.Namespace) -> int:
    iface = validate_iface_name(args.iface)
    return _ip("link", "set", iface, "up")


def cmd_bridge_addr_add(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    cidr = validate_cidr(args.cidr)
    return _ip("addr", "replace", cidr, "dev", bridge)


def cmd_default_egress(args: argparse.Namespace) -> int:
    proc = _run_capture([IP_BIN, "route", "show", "default"])
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] != "default":
            continue
        if "dev" not in parts:
            continue
        iface = parts[parts.index("dev") + 1]
        validate_host_iface(iface)
        print(iface)
        return 0
    print("default route has no dev", file=sys.stderr)
    return 1


def cmd_ipv4_forward_enable(args: argparse.Namespace) -> int:
    try:
        IP_FORWARD_PATH.write_text("1\n")
    except OSError as exc:
        print(f"failed to enable IPv4 forwarding: {exc}", file=sys.stderr)
        return 1
    return 0


def _nat_chain_name(bridge: str) -> str:
    return f"nvc_{bridge}"


def _forward_chain_name(bridge: str) -> str:
    return f"nvf_{bridge}"


def _docker_user_chain_exists() -> bool:
    return _nft_quiet("list", "chain", "ip", "filter", "DOCKER-USER") == 0


def _forward_rule_specs(bridge: str, cidr: str, egress: str) -> list[list[str]]:
    return [
        [
            "iifname",
            bridge,
            "oifname",
            egress,
            "ip",
            "saddr",
            cidr,
            "accept",
            "comment",
            f'"nova-ve {bridge} forward-out"',
        ],
        [
            "iifname",
            egress,
            "oifname",
            bridge,
            "ip",
            "daddr",
            cidr,
            "ct",
            "state",
            "established,related",
            "accept",
            "comment",
            f'"nova-ve {bridge} forward-in"',
        ],
    ]


def cmd_nat_apply(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    cidr = validate_network_cidr(args.cidr)
    egress = validate_host_iface(args.egress_iface)
    chain = _nat_chain_name(bridge)
    _nft_quiet("add", "table", "ip", "nova_ve")
    _nft_quiet("flush", "chain", "ip", "nova_ve", chain)
    _nft_quiet("delete", "chain", "ip", "nova_ve", chain)
    rc = _nft(
        "add",
        "chain",
        "ip",
        "nova_ve",
        chain,
        "{ type nat hook postrouting priority srcnat; policy accept; }",
    )
    if rc != 0:
        return rc
    return _nft(
        "add",
        "rule",
        "ip",
        "nova_ve",
        chain,
        "ip",
        "saddr",
        cidr,
        "oifname",
        egress,
        "masquerade",
        "comment",
        f'"nova-ve {bridge}"',
    )


def cmd_nat_remove(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    chain = _nat_chain_name(bridge)
    _nft_quiet("flush", "chain", "ip", "nova_ve", chain)
    _nft_quiet("delete", "chain", "ip", "nova_ve", chain)
    return 0


def cmd_forward_apply(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    cidr = validate_network_cidr(args.cidr)
    egress = validate_host_iface(args.egress_iface)
    specs = _forward_rule_specs(bridge, cidr, egress)

    if _docker_user_chain_exists():
        for spec in specs:
            _nft_quiet("delete", "rule", "ip", "filter", "DOCKER-USER", *spec)
        for spec in reversed(specs):
            rc = _nft("insert", "rule", "ip", "filter", "DOCKER-USER", *spec)
            if rc != 0:
                return rc
        return 0

    chain = _forward_chain_name(bridge)
    _nft_quiet("add", "table", "ip", "nova_ve_filter")
    _nft_quiet("flush", "chain", "ip", "nova_ve_filter", chain)
    _nft_quiet("delete", "chain", "ip", "nova_ve_filter", chain)
    rc = _nft(
        "add",
        "chain",
        "ip",
        "nova_ve_filter",
        chain,
        "{ type filter hook forward priority filter; policy accept; }",
    )
    if rc != 0:
        return rc
    for spec in specs:
        rc = _nft("add", "rule", "ip", "nova_ve_filter", chain, *spec)
        if rc != 0:
            return rc
    return 0


def cmd_forward_remove(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    cidr = getattr(args, "cidr", None)
    egress = getattr(args, "egress_iface", None)
    if cidr and egress:
        cidr = validate_network_cidr(cidr)
        egress = validate_host_iface(egress)
        for spec in _forward_rule_specs(bridge, cidr, egress):
            _nft_quiet("delete", "rule", "ip", "filter", "DOCKER-USER", *spec)

    chain = _forward_chain_name(bridge)
    _nft_quiet("flush", "chain", "ip", "nova_ve_filter", chain)
    _nft_quiet("delete", "chain", "ip", "nova_ve_filter", chain)
    return 0


def _dnsmasq_paths(bridge: str) -> tuple[Path, Path, Path]:
    root = RUNTIME_ROOT / "nat-cloud" / bridge
    return root / "dnsmasq.conf", root / "dnsmasq.pid", root / "leases"


def _dnsmasq_stop_bridge(bridge: str) -> None:
    _conf, pidfile, _leases = _dnsmasq_paths(bridge)
    try:
        raw = pidfile.read_text().strip()
        pid = int(raw)
    except (FileNotFoundError, ValueError, OSError):
        return
    try:
        cmdline = (PROC_ROOT / str(pid) / "cmdline").read_bytes().split(b"\x00")
    except OSError:
        cmdline = []
    if cmdline and not any(b"dnsmasq" in arg for arg in cmdline):
        print(f"pid {pid} does not look like dnsmasq; refusing to kill", file=sys.stderr)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    pidfile.unlink(missing_ok=True)


def cmd_dnsmasq_start(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    gateway = validate_ipv4(args.gateway, label="gateway")
    dhcp_start = validate_ipv4(args.dhcp_start, label="dhcp_start")
    dhcp_end = validate_ipv4(args.dhcp_end, label="dhcp_end")
    if int(ipaddress.IPv4Address(dhcp_start)) > int(ipaddress.IPv4Address(dhcp_end)):
        raise _ValidationError("dhcp_start>dhcp_end")

    _dnsmasq_stop_bridge(bridge)
    conf, pidfile, leases = _dnsmasq_paths(bridge)
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(
        "\n".join(
            [
                "bind-interfaces",
                f"interface={bridge}",
                f"listen-address={gateway}",
                "port=0",
                "dhcp-authoritative",
                f"dhcp-range={dhcp_start},{dhcp_end},12h",
                f"dhcp-option=option:router,{gateway}",
                f"dhcp-leasefile={leases}",
                f"pid-file={pidfile}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    rc = _run([DNSMASQ_BIN, f"--conf-file={conf}"])
    if rc != 0:
        return rc
    try:
        pid_text = pidfile.read_text().strip()
    except OSError:
        pid_text = ""
    if pid_text:
        print(pid_text)
    return 0


def cmd_dnsmasq_stop(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    _dnsmasq_stop_bridge(bridge)
    return 0


def cmd_link_set_name_in_netns(args: argparse.Namespace) -> int:
    pid = authorize_pid(args.pid)
    # Inside the netns the *current* name is whatever ``link-netns`` left
    # behind — a veth peer name (``...p``) — and the new name is an
    # ``ethN`` slot.  Both shapes validated.
    oldname = validate_iface_name(args.oldname, label="oldname")
    newname = validate_netns_iface(args.newname)
    # ``ip link set <old> name <new>`` requires the link be DOWN; the
    # caller (host_net) must arrange that.  We expose only the rename.
    return _nsenter_ip_netns(pid, "link", "set", oldname, "name", newname)


def cmd_addr_add_in_netns(args: argparse.Namespace) -> int:
    pid = authorize_pid(args.pid)
    iface = validate_netns_iface(args.iface)
    cidr = validate_cidr(args.cidr)
    return _nsenter_ip_netns(pid, "addr", "add", cidr, "dev", iface)


def cmd_addr_up_in_netns(args: argparse.Namespace) -> int:
    pid = authorize_pid(args.pid)
    iface = validate_netns_iface(args.iface)
    return _nsenter_ip_netns(pid, "link", "set", iface, "up")


def cmd_read_iface_mac(args: argparse.Namespace) -> int:
    pid = authorize_pid(args.pid)
    iface = validate_netns_iface(args.iface)
    return _nsenter_cat_address(pid, iface)


def _validate_port(value: str, *, label: str) -> int:
    """Reject anything that isn't a 1-5 digit base-10 TCP port."""
    if not RE_PORT.fullmatch(value):
        raise _ValidationError(f"{label} must match {RE_PORT.pattern}")
    port = int(value)
    if not (1 <= port <= 65535):
        raise _ValidationError(f"{label} out of range: {port}")
    return port


def cmd_console_proxy_start(args: argparse.Namespace) -> int:
    """Spawn the console TCP forwarder for a manual-veth Docker container
    or a dynamips loopback-only console.

    Two pid shapes:

      * **pid > 1** — netns-confined target (Docker --network=none). The
        pid is authorized against the runtime registry + cgroup before
        the proxy is allowed to setns into it.
      * **pid == 0** — default-netns target (dynamips). No setns; the
        proxy just republishes a host loopback port on ``0.0.0.0``. The
        registry lookup is skipped because there's no container pid to
        bind the proxy to. This is safe: the proxy only forwards to
        ``127.0.0.1:<target_port>``, and the listen port allocation
        still flows through the backend's port pool.

    Double-forks the proxy script with ``setsid`` so it survives the helper
    exiting. Prints the daemonized PID to stdout (caller persists it for
    later kill).
    """
    if str(args.pid).strip() == "0":
        target_pid = 0
    else:
        target_pid = authorize_pid(args.pid)
    listen_port = _validate_port(args.listen_port, label="listen_port")
    target_port = _validate_port(args.target_port, label="target_port")
    if listen_port < 1024:
        raise _ValidationError(
            "listen_port must be a non-privileged port (>=1024)"
        )

    if not os.access(CONSOLE_PROXY_BIN, os.X_OK):
        print(
            f"console proxy binary missing or not executable: {CONSOLE_PROXY_BIN}",
            file=sys.stderr,
        )
        return 1

    pipe_r, pipe_w = os.pipe()

    # Detach via double-fork + setsid so the proxy survives the helper exit
    # and is reparented to PID 1 (no zombie / no stdio held open).
    pid = os.fork()
    if pid == 0:
        try:
            os.close(pipe_r)
            os.setsid()
            second = os.fork()
            if second == 0:
                # Grandchild: replace with the proxy.
                os.close(pipe_w)
                devnull = os.open(os.devnull, os.O_RDWR)
                os.dup2(devnull, 0)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                if devnull > 2:
                    os.close(devnull)
                os.execv(
                    CONSOLE_PROXY_BIN,
                    [
                        CONSOLE_PROXY_BIN,
                        str(target_pid),
                        str(listen_port),
                        str(target_port),
                    ],
                )
            else:
                os.write(pipe_w, f"{second}\n".encode("ascii"))
                os._exit(0)
        except Exception as exc:
            try:
                os.write(pipe_w, f"err:{exc}\n".encode("ascii"))
            except OSError:
                pass
            os._exit(1)
    # Parent: read the grandchild PID, reap the first child, then return.
    os.close(pipe_w)
    with os.fdopen(pipe_r, "r") as r:
        line = r.readline().strip()
    os.waitpid(pid, 0)
    if line.startswith("err:") or not line.isdigit():
        print(f"console proxy spawn failed: {line}", file=sys.stderr)
        return 1
    print(line)
    return 0


def cmd_console_proxy_stop(args: argparse.Namespace) -> int:
    """Terminate a previously-spawned console proxy by PID.

    Validates the PID shape, confirms ``/proc/<pid>/comm`` looks like a
    Python interpreter running the bundled proxy script (so we never SIGTERM
    an unrelated process if the registry is stale), then sends SIGTERM
    followed by SIGKILL on a short grace period.
    """
    if not RE_PID.fullmatch(args.pid):
        raise _ValidationError("pid argument failed validation")
    pid = int(args.pid)
    if pid <= 1:
        raise _ValidationError("pid out of range")

    cmdline_path = PROC_ROOT / str(pid) / "cmdline"
    try:
        cmdline = cmdline_path.read_bytes().split(b"\x00")
    except FileNotFoundError:
        return 0  # already gone — idempotent
    except PermissionError:
        cmdline = []
    if not any(b"nova-ve-console-proxy" in arg for arg in cmdline):
        # Refuse to kill an unrelated process (PID recycled or stale registry).
        print(
            f"pid {pid} does not look like nova-ve-console-proxy; refusing to kill",
            file=sys.stderr,
        )
        return 1

    import time
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return 0
    for _ in range(20):
        time.sleep(0.05)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 0
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


VERB_TABLE: Mapping[str, Callable[[argparse.Namespace], int]] = {
    "bridge-add": cmd_bridge_add,
    "bridge-del": cmd_bridge_del,
    "tap-add": cmd_tap_add,
    "tap-del": cmd_tap_del,
    "link-del": cmd_link_del,
    "veth-pair-add": cmd_veth_pair_add,
    "link-master": cmd_link_master,
    "link-set-nomaster": cmd_link_set_nomaster,
    "link-netns": cmd_link_netns,
    "link-up": cmd_link_up,
    "bridge-addr-add": cmd_bridge_addr_add,
    "default-egress": cmd_default_egress,
    "ipv4-forward-enable": cmd_ipv4_forward_enable,
    "nat-apply": cmd_nat_apply,
    "nat-remove": cmd_nat_remove,
    "forward-apply": cmd_forward_apply,
    "forward-remove": cmd_forward_remove,
    "dnsmasq-start": cmd_dnsmasq_start,
    "dnsmasq-stop": cmd_dnsmasq_stop,
    "link-set-name-in-netns": cmd_link_set_name_in_netns,
    "addr-add-in-netns": cmd_addr_add_in_netns,
    "addr-up-in-netns": cmd_addr_up_in_netns,
    "read-iface-mac": cmd_read_iface_mac,
    "console-proxy-start": cmd_console_proxy_start,
    "console-proxy-stop": cmd_console_proxy_stop,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova-ve-net.py",
        description=(
            "Privileged network helper for nova-ve.  Each verb is a "
            "fixed ip/nsenter invocation with regex-validated arguments."
        ),
    )
    sub = parser.add_subparsers(dest="verb", required=True, metavar="VERB")

    p = sub.add_parser("bridge-add", help="ip link add <name> type bridge")
    p.add_argument("name")

    p = sub.add_parser("bridge-del", help="ip link del <name>")
    p.add_argument("name")

    p = sub.add_parser("tap-add", help="ip tuntap add dev <name> mode tap")
    p.add_argument("name")

    p = sub.add_parser("tap-del", help="ip link del <name>")
    p.add_argument("name")

    p = sub.add_parser(
        "link-del",
        help="ip link del <name> — accepts both TAP and veth host-end names",
    )
    p.add_argument("name")

    p = sub.add_parser("veth-pair-add", help="ip link add <h> type veth peer name <p>")
    p.add_argument("hostend")
    p.add_argument("peerend")

    p = sub.add_parser("link-master", help="ip link set <iface> master <bridge>")
    p.add_argument("iface")
    p.add_argument("bridge")

    p = sub.add_parser("link-set-nomaster", help="ip link set <iface> nomaster")
    p.add_argument("iface")

    p = sub.add_parser("link-netns", help="ip link set <iface> netns <pid>")
    p.add_argument("iface")
    p.add_argument("pid")

    p = sub.add_parser("link-up", help="ip link set <iface> up")
    p.add_argument("iface")

    p = sub.add_parser("bridge-addr-add", help="ip addr replace <cidr> dev <bridge>")
    p.add_argument("bridge")
    p.add_argument("cidr")

    sub.add_parser("default-egress", help="print the host default-route interface")

    sub.add_parser("ipv4-forward-enable", help="enable /proc/sys/net/ipv4/ip_forward")

    p = sub.add_parser("nat-apply", help="install nova-ve nftables masquerade")
    p.add_argument("bridge")
    p.add_argument("cidr")
    p.add_argument("egress_iface")

    p = sub.add_parser("nat-remove", help="remove nova-ve nftables masquerade")
    p.add_argument("bridge")

    p = sub.add_parser("forward-apply", help="install nova-ve NAT-Cloud forwarding rules")
    p.add_argument("bridge")
    p.add_argument("cidr")
    p.add_argument("egress_iface")

    p = sub.add_parser("forward-remove", help="remove nova-ve NAT-Cloud forwarding rules")
    p.add_argument("bridge")
    p.add_argument("cidr", nargs="?")
    p.add_argument("egress_iface", nargs="?")

    p = sub.add_parser("dnsmasq-start", help="start per-bridge dnsmasq DHCP/DNS")
    p.add_argument("bridge")
    p.add_argument("gateway")
    p.add_argument("dhcp_start")
    p.add_argument("dhcp_end")

    p = sub.add_parser("dnsmasq-stop", help="stop per-bridge dnsmasq DHCP/DNS")
    p.add_argument("bridge")

    p = sub.add_parser(
        "link-set-name-in-netns",
        help="nsenter -t <pid> -n ip link set <oldname> name <newname>",
    )
    p.add_argument("pid")
    p.add_argument("oldname")
    p.add_argument("newname")

    p = sub.add_parser(
        "addr-add-in-netns",
        help="nsenter -t <pid> -n ip addr add <cidr> dev <iface>",
    )
    p.add_argument("pid")
    p.add_argument("iface")
    p.add_argument("cidr")

    p = sub.add_parser(
        "addr-up-in-netns",
        help="nsenter -t <pid> -n ip link set <iface> up",
    )
    p.add_argument("pid")
    p.add_argument("iface")

    p = sub.add_parser(
        "read-iface-mac",
        help="nsenter -t <pid> -n cat /sys/class/net/<iface>/address",
    )
    p.add_argument("pid")
    p.add_argument("iface")

    p = sub.add_parser(
        "console-proxy-start",
        help=(
            "spawn nova-ve-console-proxy.py to forward 127.0.0.1:<listen_port> "
            "into the netns of <pid> at 127.0.0.1:<target_port>; prints the "
            "spawned proxy PID"
        ),
    )
    p.add_argument("pid")
    p.add_argument("listen_port")
    p.add_argument("target_port")

    p = sub.add_parser(
        "console-proxy-stop",
        help="kill a previously-spawned console proxy by pid (idempotent)",
    )
    p.add_argument("pid")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    # argparse exits 2 on parse failure (unknown verb, missing args); we
    # let that propagate.  We use exit code 3 only if an unknown verb
    # somehow makes it past argparse — defensive.
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already wrote a message; re-raise to honour its code.
        return int(exc.code) if isinstance(exc.code, int) else 2
    handler = VERB_TABLE.get(args.verb)
    if handler is None:
        print(f"unknown verb: {args.verb}", file=sys.stderr)
        return 3
    try:
        return handler(args)
    except _ValidationError as exc:
        print(f"argument failed validation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
