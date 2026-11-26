#!/usr/bin/env python3
"""nova-ve privileged network helper.

Single privileged binary invoked via ``sudo`` from the un-privileged
``nova-ve`` backend.  Exposes a *fixed* set of verbs — there is no generic
``in-netns`` escape hatch.  Most verbs map to one ``ip`` (or ``nsenter ip``)
invocation; the process-signal verb is limited to registry-authorized QEMU
process groups.  All arguments are validated before any subprocess or signal is
issued.

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
1   underlying ``ip`` / ``nsenter`` / process operation failed
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
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
# #334: root dnsmasq must not read its config — or write its lease file —
# from a directory the service account owns (it could swap in a
# ``dhcp-script`` or point the lease file at a root-owned target through a
# symlink). Config, pid and leases live under /run instead: tmpfs, so the
# helper recreates the tree on every start, and every ancestor is
# root-owned. Not derived from RUNTIME_ROOT, and the backend cannot move it
# through sudo (sudo resets the environment; this is root's own setting).
# ``_check_dnsmasq_root`` refuses the tree unless every directory in it is
# root's alone, whatever this is set to.
DNSMASQ_ROOT = Path(os.environ.get("NOVA_VE_DNSMASQ_ROOT", "/run/nova-ve/dnsmasq"))
IP_FORWARD_PATH = Path(os.environ.get("NOVA_VE_IP_FORWARD_PATH", "/proc/sys/net/ipv4/ip_forward"))
IPV4_CONF_ROOT = Path(os.environ.get("NOVA_VE_IPV4_CONF_ROOT", "/proc/sys/net/ipv4/conf"))
# Root-owned, unlike the service account's state directory.
GUARD_LOCK_PATH = Path(os.environ.get("NOVA_VE_GUARD_LOCK", "/run/nova-ve-bridge-guard.lock"))


# ---------------------------------------------------------------------------
# Regex catalogue (single source of truth for argv shape)
# ---------------------------------------------------------------------------

# kernel IFNAMSIZ = 16 (incl NUL) → 15 usable.  Each format below caps to the
# advertised length so overflow is impossible even if the regex is reused.

# Bridge: nove<lab_hash:04x>n<network_id 1-5 digits>     max 14
# Lab bridges: nove<lab_hash:04x>n<net 1-5 digits>, plus the per-lab
# management bridge (#287): nove<lab_hash:04x>mg
RE_BRIDGE_NAME = re.compile(r"^nove[a-f0-9]{4}(n[0-9]{1,5}|mg)\Z")

# Lab network bridges only (no management bridge): the isolated-bridge DHCP
# verbs (#325) never apply to anything else.
RE_NETWORK_BRIDGE_NAME = re.compile(r"^nove[a-f0-9]{4}n[0-9]{1,5}\Z")

# Host-owned bridges created by the provision script for Bridge-Cloud
# networks.  Lab-side verbs MUST NOT accept these; only the dedicated
# link-master-host verb does.
RE_HOST_BRIDGE_NAME = re.compile(r"^br-eth[0-9]+$")

# TAP (QEMU): nve<lab_hash:04x>d<node 1-3>i<iface 1-2>     max 13
RE_TAP_NAME = re.compile(r"^nve[a-f0-9]{4}d[0-9]{1,3}i[0-9]{1,2}$")

# Veth host/peer: same as TAP plus h/p suffix                max 14
# (#287) mgmt veths use the `mt` marker instead of `i<iface>`: mth / mtp.
RE_VETH_NAME = re.compile(r"^nve[a-f0-9]{4}d[0-9]{1,3}(i[0-9]{1,2}|mt)[hp]$")

# Container netns interface name: ethN where N is 0-99
# ``ethN`` slots plus the #287 management interface (``mgmt0``).
RE_NETNS_IFACE = re.compile(r"^(eth[0-9]{1,2}|mgmt0)$")

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


def validate_network_bridge_name(value: str) -> str:
    return _check_regex(value, RE_NETWORK_BRIDGE_NAME, "bridge_name")


def validate_host_bridge_name(value: str) -> str:
    """Validate names for host-owned ``br-eth*`` bridges.

    Accepted by exactly one verb (``link-master-host``).  Lab-side verbs
    continue to use ``validate_bridge_name`` / ``validate_iface_name`` and
    refuse anything matching ``RE_HOST_BRIDGE_NAME``.
    """
    return _check_regex(value, RE_HOST_BRIDGE_NAME, "host_bridge_name")


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
RE_SAFE_LAB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _read_proc_text(pid: int, name: str) -> str:
    path = PROC_ROOT / str(pid) / name
    try:
        return path.read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return ""


def _read_proc_cmdline(pid: int) -> list[str]:
    path = PROC_ROOT / str(pid) / "cmdline"
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return []
    return [
        piece.decode("utf-8", errors="surrogateescape")
        for piece in raw.split(b"\0")
        if piece
    ]


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


def _qemu_qmp_path_for_entry(entry: Mapping[str, object]) -> str | None:
    lab_id = str(entry.get("lab_id") or "").strip()
    if not RE_SAFE_LAB_ID.match(lab_id):
        return None
    try:
        node_id = int(entry.get("node_id"))
    except (TypeError, ValueError):
        return None
    if node_id <= 0:
        return None
    return str(RUNTIME_ROOT / "tmp" / lab_id / str(node_id) / "qmp.sock")


def _qemu_cmdline_matches_registry_entry(pid: int, entry: Mapping[str, object]) -> bool:
    """Bind QEMU signaling to an immutable /proc cmdline fingerprint.

    ``pids.json`` is writable by the service account so it is not sufficient
    authorization for root-mediated process signals.  The QEMU process argv is
    controlled by the kernel; require the live process to carry the canonical
    nova-ve QMP socket path for the registered lab/node before signaling it.
    """
    argv = _read_proc_cmdline(pid)
    if not argv:
        return False
    if not Path(argv[0]).name.startswith("qemu-system-"):
        return False

    qmp_path = _qemu_qmp_path_for_entry(entry)
    if not qmp_path:
        return False
    qmp_prefix = f"unix:{qmp_path},"
    for idx, arg in enumerate(argv[:-1]):
        if arg == "-qmp" and argv[idx + 1].startswith(qmp_prefix):
            return True
    return False


def _emit_qemu_signal_diagnostic(pid: int, entry: Mapping[str, object]) -> None:
    qmp_path = _qemu_qmp_path_for_entry(entry) or "<invalid registry lab/node>"
    argv = _read_proc_cmdline(pid)
    print(f"qemu process-signal check FAILED for pid={pid}", file=sys.stderr)
    print(f"  expected qmp path: {qmp_path}", file=sys.stderr)
    print(f"  /proc/{pid}/cmdline: {argv[:20]!r}", file=sys.stderr)


def authorize_pid_entry(value: str) -> tuple[int, dict]:
    """Validate pid argv shape and return the matching registry entry.

    Order: regex shape → reserved range → registry lookup → cgroup/comm
    defense-in-depth.  On any failure raises ``_ValidationError`` after
    emitting the structured diagnostic specified in US-201.
    """
    pid = validate_pid_shape(value)
    entry = _registry_lookup(pid)
    if entry is None:
        _emit_ownership_diagnostic(pid)
        raise _ValidationError("pid_not_in_registry")
    if not _cgroup_or_comm_matches(pid):
        _emit_ownership_diagnostic(pid)
        raise _ValidationError("pid_cgroup_mismatch")
    return pid, entry


def authorize_pid(value: str) -> int:
    """Validate pid argv shape AND verify ownership."""
    pid, _entry = authorize_pid_entry(value)
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


def _run_input(argv: Sequence[str], text: str) -> int:
    """Run ``argv`` with ``text`` on stdin (``nft -f -``); return the exit code."""
    for piece in argv:
        if not isinstance(piece, str):
            raise TypeError(f"argv element not a str: {piece!r}")
    try:
        proc = subprocess.run(list(argv), shell=False, check=False, input=text, text=True)
    except FileNotFoundError as exc:
        print(f"required binary not found: {exc}", file=sys.stderr)
        return 1
    return proc.returncode


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


def cmd_link_master_host(args: argparse.Namespace) -> int:
    """Attach a nova-ve iface (TAP/veth/lab bridge) to a host-owned
    ``br-eth*`` bridge.

    Distinct from ``cmd_link_master`` so the lab-side ``RE_BRIDGE_NAME``
    regex stays exclusive for lab bridges.  Scope creep into the existing
    verb would let lab paths target host bridges; instead this new verb
    has its own ``RE_HOST_BRIDGE_NAME`` regex and is invoked only by
    Bridge-Cloud paths in the backend.
    """
    iface = validate_iface_name(args.iface)
    bridge = validate_host_bridge_name(args.bridge)
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


def cmd_bridge_addr_del(args: argparse.Namespace) -> int:
    """Remove one address from a lab bridge; succeed when it is already gone."""
    bridge = validate_network_bridge_name(args.bridge)
    cidr = validate_cidr(args.cidr)
    proc = _run_capture([IP_BIN, "-o", "-4", "addr", "show", "dev", bridge])
    if proc.returncode != 0:
        if "does not exist" in (proc.stderr or ""):
            return 0  # the bridge went, and its addresses with it
        print(proc.stderr or f"ip addr show dev {bridge} failed", file=sys.stderr, end="")
        return 1
    present = any(
        len(parts) >= 4 and parts[2] == "inet" and parts[3] == cidr
        for parts in (line.split() for line in (proc.stdout or "").splitlines())
    )
    if not present:
        return 0
    return _ip("addr", "del", cidr, "dev", bridge)


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


def _forward_rule_comments(bridge: str) -> list[str]:
    return [
        f"nova-ve {bridge} forward-out",
        f"nova-ve {bridge} forward-in",
    ]


def _nft_delete_rules_by_comment(
    family: str, table: str, chain: str, comments: Sequence[str]
) -> int:
    proc = _run_capture([NFT_BIN, "-a", "list", "chain", family, table, chain])
    if proc.returncode != 0:
        return proc.returncode

    handles: list[str] = []
    for line in proc.stdout.splitlines():
        if not any(f'comment "{comment}"' in line for comment in comments):
            continue
        match = re.search(r"# handle ([0-9]+)\s*$", line)
        if match:
            handles.append(match.group(1))
    for handle in handles:
        rc = _nft("delete", "rule", family, table, chain, "handle", handle)
        if rc != 0:
            return rc
    return 0


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
        rc = _nft_delete_rules_by_comment(
            "ip", "filter", "DOCKER-USER", _forward_rule_comments(bridge)
        )
        if rc != 0:
            return rc
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
        validate_host_iface(egress)
    if _docker_user_chain_exists():
        rc = _nft_delete_rules_by_comment(
            "ip", "filter", "DOCKER-USER", _forward_rule_comments(bridge)
        )
        if rc != 0:
            return rc

    chain = _forward_chain_name(bridge)
    _nft_quiet("flush", "chain", "ip", "nova_ve_filter", chain)
    _nft_quiet("delete", "chain", "ip", "nova_ve_filter", chain)
    return 0


# Isolated-bridge guard (#325). A bridge that runs DHCP carries a host
# address, so the host would answer anything that reaches it through the
# bridge and route between the bridge and its other interfaces. These chains
# accept only DHCP from the bridge and drop routed traffic both ways. They sit
# in their own table, one input and one forward chain per bridge, so removal
# is exact and cannot touch Docker's or NAT-Cloud's rules. A drop in any base
# chain is final, so running before Docker's chains (priority 0) keeps a
# later accept elsewhere from letting traffic through.
GUARD_TABLE = "nova_ve_bridge_guard"
GUARD_PRIORITY = "-10"


def _guard_chains(bridge: str) -> tuple[str, str]:
    return f"in_{bridge}", f"fwd_{bridge}"


def _guard_locked(fn: Callable[[str], int], bridge: str) -> int:
    """Serialize guard edits: removal deletes the table once it is empty."""
    # O_NOFOLLOW: never open (and create) a file through a planted symlink.
    try:
        fd = os.open(GUARD_LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        print(f"cannot open {GUARD_LOCK_PATH}: {exc}", file=sys.stderr)
        return 1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn(bridge)
    finally:
        os.close(fd)


def _set_arp_ignore(bridge: str, value: str, *, missing_ok: bool) -> int:
    """Answer ARP on the bridge only for the bridge's own address.

    Without it the host answers ARP for every local address, telling lab
    nodes about the host's other networks.
    """
    try:
        (IPV4_CONF_ROOT / bridge / "arp_ignore").write_text(f"{value}\n")
    except FileNotFoundError:
        if missing_ok:
            return 0
        print(f"bridge {bridge} does not exist", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"failed to set arp_ignore on {bridge}: {exc}", file=sys.stderr)
        return 1
    return 0


def _guard_script(bridge: str) -> str:
    chain_in, chain_fwd = _guard_chains(bridge)
    table = f"inet {GUARD_TABLE}"
    quoted = f'"{bridge}"'
    return "\n".join(
        [
            f"add table {table}",
            f"add chain {table} {chain_in} {{ type filter hook input priority {GUARD_PRIORITY}; policy accept; }}",
            f"add chain {table} {chain_fwd} {{ type filter hook forward priority {GUARD_PRIORITY}; policy accept; }}",
            f"flush chain {table} {chain_in}",
            f"flush chain {table} {chain_fwd}",
            # DHCP from clients is the only host input the bridge may carry,
            # plus echo replies: dnsmasq pings an address before offering
            # it, and the host can check that a node is up.
            f"add rule {table} {chain_in} iifname {quoted} meta nfproto ipv4 udp sport 68 udp dport 67 accept",
            f"add rule {table} {chain_in} iifname {quoted} icmp type echo-reply accept",
            # Everything else is dropped: IPv6 (link-local included), and
            # traffic for any host address, not just the bridge's own.
            f"add rule {table} {chain_in} iifname {quoted} drop",
            # Frames bridged between lab nodes reach this hook only when
            # br_netfilter is loaded, with the bridge as both devices: let
            # them pass to the remaining chains unchanged.
            f"add rule {table} {chain_fwd} iifname {quoted} oifname {quoted} accept",
            f"add rule {table} {chain_fwd} iifname {quoted} drop",
            f"add rule {table} {chain_fwd} oifname {quoted} drop",
            "",
        ]
    )


def _guard_apply(bridge: str) -> int:
    # One transaction: the chains are never half-filled.
    rc = _run_input([NFT_BIN, "-f", "-"], _guard_script(bridge))
    return rc if rc != 0 else _set_arp_ignore(bridge, "1", missing_ok=False)


def _guard_remove(bridge: str) -> int:
    table = f"inet {GUARD_TABLE}"
    rc = _set_arp_ignore(bridge, "0", missing_ok=True)
    if rc != 0:
        return rc
    for chain in _guard_chains(bridge):
        if _run_capture([NFT_BIN, "list", "chain", "inet", GUARD_TABLE, chain]).returncode != 0:
            continue  # absent already
        rc = _run_input(
            [NFT_BIN, "-f", "-"],
            f"flush chain {table} {chain}\ndelete chain {table} {chain}\n",
        )
        if rc != 0:
            return rc
    listing = _run_capture([NFT_BIN, "list", "table", "inet", GUARD_TABLE])
    if listing.returncode == 0 and not re.search(r"^\s*chain ", listing.stdout or "", re.M):
        return _nft("delete", "table", "inet", GUARD_TABLE)
    return 0


def cmd_bridge_guard_apply(args: argparse.Namespace) -> int:
    bridge = validate_network_bridge_name(args.bridge)
    return _guard_locked(_guard_apply, bridge)


def cmd_bridge_guard_remove(args: argparse.Namespace) -> int:
    bridge = validate_network_bridge_name(args.bridge)
    return _guard_locked(_guard_remove, bridge)


def _write_private_file(
    path: Path,
    text: str,
    root: Path | None = None,
    *,
    check_dir: Callable[[Path], None] | None = None,
) -> None:
    """Write ``path`` as root without following links the service account made.

    The state directory belongs to the unprivileged service account, so a
    symlink planted there must never redirect a root write. Directories are
    created and checked one by one below ``root``; the file is
    written to a fresh temporary name and renamed into place. ``check_dir``
    sees every one of those directories once it exists, for a tree whose
    trust rules are stricter than "not a link".
    """
    if root is None:
        root = RUNTIME_ROOT
    # The root itself is the operator's choice and may be a link.
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in path.parent.relative_to(root).parts:
        current = current / part
        try:
            os.mkdir(current, 0o755)
        except FileExistsError:
            pass
        if current.is_symlink() or not current.is_dir():
            raise OSError(f"refusing to write through {current}: not a plain directory")
        if check_dir is not None:
            check_dir(current)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# Owner every directory of the dnsmasq tree must have. Root in production;
# the tests run unprivileged and point this at their own uid, so the
# ownership refusal is exercised without weakening the default.
_TRUSTED_UID = 0


def _check_trusted_dir(path: Path) -> None:
    """Refuse ``path`` unless it is a directory that only root may change.

    Only then can nobody replace what the helper writes below it. The stat
    does not follow links: a link or a plain file where a directory belongs
    is refused rather than used.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise OSError(f"refusing to use {path}: {exc.strerror}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"refusing to use {path}: not a plain directory")
    if info.st_uid != _TRUSTED_UID:
        raise OSError(
            f"refusing to use {path}: owned by uid {info.st_uid}, expected {_TRUSTED_UID}"
        )
    if info.st_mode & 0o022:
        raise OSError(f"refusing to use {path}: writable by group or others")


def _ensure_dnsmasq_root() -> None:
    """Create the tree's fixed ancestors when a reboot emptied ``/run``.

    Each level is made only once the level above it passed the trust check,
    so nothing is ever created below a planted link.
    """
    for candidate in (DNSMASQ_ROOT.parent, DNSMASQ_ROOT):
        if not os.path.lexists(candidate):
            try:
                os.mkdir(candidate, 0o755)
            except OSError as exc:
                raise OSError(
                    f"refusing to use {candidate}: cannot create it ({exc.strerror})"
                ) from exc
        _check_trusted_dir(candidate)


def _check_dnsmasq_root(bridge: str | None = None) -> None:
    """Refuse a dnsmasq tree that is not root's alone (#334).

    Covers both fixed ancestors and, when it already exists, the bridge's
    own directory. A missing directory is fine here: ``_ensure_dnsmasq_root``
    creates and checks the ancestors before anything is written, and
    ``_write_private_file`` checks the bridge's directory as it makes it.
    """
    for candidate in (DNSMASQ_ROOT.parent, DNSMASQ_ROOT):
        if os.path.lexists(candidate):
            _check_trusted_dir(candidate)
    if bridge is not None and os.path.lexists(DNSMASQ_ROOT / bridge):
        _check_trusted_dir(DNSMASQ_ROOT / bridge)


def _dnsmasq_paths(bridge: str) -> tuple[Path, Path, Path]:
    root = DNSMASQ_ROOT / bridge
    return root / "dnsmasq.conf", root / "dnsmasq.pid", root / "leases"


def _legacy_dnsmasq_paths(bridge: str) -> tuple[Path, Path, Path]:
    """Pre-#334 locations under the service-owned data directory.

    Only used to find servers started before the upgrade and to carry their
    leases over; nothing new is written there.
    """
    root = RUNTIME_ROOT / "nat-cloud" / bridge
    return root / "dnsmasq.conf", root / "dnsmasq.pid", root / "leases"


def _dnsmasq_pids(bridge: str) -> list[int]:
    """Pids of the dnsmasq processes serving ``bridge``.

    Matched on the exact ``--conf-file`` argument rather than the pid file: a
    lost pid file must not leave a server running, and a stale one must not
    get another network's server killed (#320).
    """
    # #334: also match the legacy conf path, or an upgrade would leave the
    # old server running next to the new one for the same bridge.
    wanted = {
        f"--conf-file={_dnsmasq_paths(bridge)[0]}".encode(),
        f"--conf-file={_legacy_dnsmasq_paths(bridge)[0]}".encode(),
    }
    pids: list[int] = []
    for entry in PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\x00")
        except OSError:
            continue
        if os.path.basename(argv[0]) == b"dnsmasq" and wanted.intersection(argv):
            pids.append(int(entry.name))
    return sorted(pids)


def _dnsmasq_stop_bridge(bridge: str) -> bool:
    """Stop the bridge's dnsmasq and confirm that it is gone."""
    for sig, attempts in ((signal.SIGTERM, 40), (signal.SIGKILL, 20)):
        for pid in _dnsmasq_pids(bridge):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        for _attempt in range(attempts):
            if not _dnsmasq_pids(bridge):
                return True
            time.sleep(0.05)
    print(f"dnsmasq for {bridge} did not stop", file=sys.stderr)
    return False


def _remove_pidfiles(bridge: str) -> bool:
    """Unlink both pid files once the server is gone; False when the tree is not trusted.

    Stopping a server only reads ``/proc``, so a tree the helper refuses to
    write must not keep one alive: only the unlink below it is skipped.
    """
    _unlink_legacy_pidfile(bridge)
    try:
        _check_dnsmasq_root(bridge)
    except OSError as exc:
        print(f"{exc}; leaving the pid file of {bridge} in place", file=sys.stderr)
        return False
    _dnsmasq_paths(bridge)[1].unlink(missing_ok=True)
    return True


def _dnsmasq_options(gateway: str, dhcp_start: str, dhcp_end: str, *, no_router: bool) -> list[str]:
    if no_router:
        # Isolated bridge (#325): the host is no gateway and serves no DNS.
        # An option with no value tells dnsmasq not to send it at all. Not
        # authoritative: a lab router may legitimately serve this segment too.
        return [
            f"dhcp-range={dhcp_start},{dhcp_end},12h",
            "dhcp-option=3",
            "dhcp-option=6",
        ]
    return [
        "dhcp-authoritative",
        f"dhcp-range={dhcp_start},{dhcp_end},12h",
        f"dhcp-option=option:router,{gateway}",
    ]


def _write_dnsmasq_conf(
    conf: Path,
    pidfile: Path,
    leases: Path,
    bridge: str,
    gateway: str,
    dhcp_start: str,
    dhcp_end: str,
    no_router: bool,
) -> None:
    # The tree itself was checked before anything was stopped or read; this
    # re-checks the bridge's directory at the moment it is made or found.
    _write_private_file(
        conf,
        "\n".join(
            [
                "bind-interfaces",
                f"interface={bridge}",
                f"listen-address={gateway}",
                "port=0",
                *_dnsmasq_options(gateway, dhcp_start, dhcp_end, no_router=no_router),
                f"dhcp-leasefile={leases}",
                f"pid-file={pidfile}",
                "",
            ]
        ),
        root=DNSMASQ_ROOT,
        check_dir=_check_trusted_dir,
    )


def _open_legacy_dir(bridge: str) -> int | None:
    """fd for the legacy per-bridge dir, refusing every planted link.

    Each component below ``RUNTIME_ROOT`` is opened relative to the previous
    fd with ``O_NOFOLLOW``, so the service account cannot swap a component
    for a symlink between a check and the use that follows it.
    """
    try:
        fd = os.open(RUNTIME_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for part in ("nat-cloud", bridge):
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlink_legacy_pidfile(bridge: str) -> None:
    """Remove the pre-#334 pid file without resolving planted links."""
    fd = _open_legacy_dir(bridge)
    if fd is None:
        return
    try:
        os.unlink("dnsmasq.pid", dir_fd=fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _migrate_legacy_leases(bridge: str, leases: Path) -> None:
    """Carry pre-#334 leases into the new root-owned location.

    The legacy file lives in a service-owned directory: read it through the
    ``O_NOFOLLOW`` dir-fd chain (never a checked-then-opened path), confirm
    it is a regular file, and cap its size before copying the content
    through the checked writer.
    """
    if leases.exists():
        return
    dir_fd = _open_legacy_dir(bridge)
    if dir_fd is None:
        return
    try:
        fd = os.open(
            "leases",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError:
        os.close(dir_fd)
        return
    os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576:
            return
        with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
            fd = -1
            text = fh.read(1_048_577)
            if len(text) > 1_048_576:
                return
    except OSError:
        return
    finally:
        if fd >= 0:
            os.close(fd)
    # The legacy file is consumed only once a server runs on this copy, which
    # may be a later attempt: the note says which starts still owe that.
    _write_private_file(_legacy_lease_note(bridge), "", root=DNSMASQ_ROOT, check_dir=_check_trusted_dir)
    _write_private_file(leases, text, root=DNSMASQ_ROOT, check_dir=_check_trusted_dir)
    try:
        os.chmod(leases, 0o644)
    except OSError:
        pass


def _legacy_lease_note(bridge: str) -> Path:
    """Marks leases that were copied from the legacy file, which is still to be consumed."""
    return DNSMASQ_ROOT / bridge / "leases.imported"


def _finish_legacy_leases(bridge: str) -> None:
    """Consume the legacy lease file now that a server runs on its copy.

    /run is tmpfs, so a legacy file that stayed would be imported again on
    every boot. Only a running server makes the move final: a start that
    failed must leave the file, because the copy may be gone with the next
    boot. Only a file the helper copied is ever consumed, so one it refused
    (too large, or not a regular file) is left as it is.
    """
    note = _legacy_lease_note(bridge)
    if not os.path.lexists(note):
        return
    dir_fd = _open_legacy_dir(bridge)
    if dir_fd is None:
        return
    try:
        os.rename("leases", "leases.pre-334", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except FileNotFoundError:
        pass  # consumed already
    except OSError:
        return  # left for the next start
    finally:
        os.close(dir_fd)
    note.unlink(missing_ok=True)


def cmd_dnsmasq_start(args: argparse.Namespace) -> int:
    no_router = bool(getattr(args, "no_router", False))
    bridge = (validate_network_bridge_name if no_router else validate_bridge_name)(args.bridge)
    gateway = validate_ipv4(args.gateway, label="gateway")
    dhcp_start = validate_ipv4(args.dhcp_start, label="dhcp_start")
    dhcp_end = validate_ipv4(args.dhcp_end, label="dhcp_end")
    if int(ipaddress.IPv4Address(dhcp_start)) > int(ipaddress.IPv4Address(dhcp_end)):
        raise _ValidationError("dhcp_start>dhcp_end")
    if no_router and (
        int(ipaddress.IPv4Address(dhcp_start))
        <= int(ipaddress.IPv4Address(gateway))
        <= int(ipaddress.IPv4Address(dhcp_end))
    ):
        raise _ValidationError("server inside dhcp range")

    # #334: fail closed on a tree that is not root's alone, before anything
    # below it is read, written, unlinked or any server is stopped.
    try:
        _ensure_dnsmasq_root()
        _check_dnsmasq_root(bridge)
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not _dnsmasq_stop_bridge(bridge):
        return 1
    _remove_pidfiles(bridge)
    conf, pidfile, leases = _dnsmasq_paths(bridge)
    try:
        _migrate_legacy_leases(bridge, leases)
        _write_dnsmasq_conf(conf, pidfile, leases, bridge, gateway, dhcp_start, dhcp_end, no_router)
    except OSError as exc:
        print(f"failed to write {conf}: {exc}", file=sys.stderr)
        return 1
    rc = _run([DNSMASQ_BIN, f"--conf-file={conf}"])
    if rc != 0:
        return rc
    # dnsmasq daemonises after binding; confirm the daemon stayed up.
    pids = _dnsmasq_pids(bridge)
    if not pids:
        print(f"dnsmasq for {bridge} exited right after starting", file=sys.stderr)
        return 1
    _finish_legacy_leases(bridge)
    print(pids[0])
    return 0


def cmd_dnsmasq_stop(args: argparse.Namespace) -> int:
    bridge = validate_bridge_name(args.bridge)
    if not _dnsmasq_stop_bridge(bridge):
        return 1
    # The server is gone either way; a tree the helper refuses still needs
    # an operator, so the verb must not look like a clean stop.
    return 0 if _remove_pidfiles(bridge) else 1


def cmd_dnsmasq_layout_version(args: argparse.Namespace) -> int:
    """Report the protected DHCP layout without touching host state."""
    print("root-owned-dnsmasq-v1")
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


def cmd_list_ipv4_addrs(args: argparse.Namespace) -> int:
    """Print every host IPv4 address as ``<iface> <cidr>`` lines (#287).

    Used by the docker mgmt attachment to skip /24 subnets already owned
    by other host interfaces and to reuse an existing bridge's gateway.
    """
    out = _run_capture(["ip", "-o", "-4", "addr", "show"])
    if out.returncode != 0:
        sys.stderr.write(out.stderr or "ip -o -4 addr show failed\n")
        return 1
    for line in (out.stdout or "").splitlines():
        # ``ip -o -4 addr show`` rows look like:
        #   2: eth0    inet 10.0.0.5/24 scope global eth0\ ...
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            print(f"{parts[1]} {parts[3]}")
    return 0


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


def cmd_qemu_process_signal(args: argparse.Namespace) -> int:
    """Signal a registry-authorized QEMU process group.

    This is the migration-safe fallback for hosts moving the backend from the
    installer user to the dedicated service user: old QEMU processes may still
    be owned by the old account, but the root helper can terminate only pids
    that are both in nova-ve's registry and still look like qemu-system-*.
    """
    pid, entry = authorize_pid_entry(args.pid)
    if entry.get("kind") != "qemu":
        raise _ValidationError("pid_not_qemu")
    if not _qemu_cmdline_matches_registry_entry(pid, entry):
        _emit_qemu_signal_diagnostic(pid, entry)
        raise _ValidationError("qemu_cmdline_mismatch")
    if args.signal == "term":
        sig = signal.SIGTERM
    elif args.signal == "kill":
        sig = signal.SIGKILL
    else:
        raise _ValidationError("signal")
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return 0
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
    "link-master-host": cmd_link_master_host,
    "link-set-nomaster": cmd_link_set_nomaster,
    "link-netns": cmd_link_netns,
    "link-up": cmd_link_up,
    "bridge-addr-add": cmd_bridge_addr_add,
    "bridge-addr-del": cmd_bridge_addr_del,
    "bridge-guard-apply": cmd_bridge_guard_apply,
    "bridge-guard-remove": cmd_bridge_guard_remove,
    "default-egress": cmd_default_egress,
    "ipv4-forward-enable": cmd_ipv4_forward_enable,
    "nat-apply": cmd_nat_apply,
    "nat-remove": cmd_nat_remove,
    "forward-apply": cmd_forward_apply,
    "forward-remove": cmd_forward_remove,
    "dnsmasq-start": cmd_dnsmasq_start,
    "dnsmasq-stop": cmd_dnsmasq_stop,
    "dnsmasq-layout-version": cmd_dnsmasq_layout_version,
    "link-set-name-in-netns": cmd_link_set_name_in_netns,
    "addr-add-in-netns": cmd_addr_add_in_netns,
    "addr-up-in-netns": cmd_addr_up_in_netns,
    "list-ipv4-addrs": cmd_list_ipv4_addrs,
    "read-iface-mac": cmd_read_iface_mac,
    "console-proxy-start": cmd_console_proxy_start,
    "console-proxy-stop": cmd_console_proxy_stop,
    "qemu-process-signal": cmd_qemu_process_signal,
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

    p = sub.add_parser(
        "link-master-host",
        help="ip link set <iface> master <br-eth*> — Bridge-Cloud only",
    )
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

    p = sub.add_parser(
        "bridge-addr-del", help="ip addr del <cidr> dev <bridge> (no-op when absent)"
    )
    p.add_argument("bridge")
    p.add_argument("cidr")

    p = sub.add_parser(
        "bridge-guard-apply",
        help="limit host input from <bridge> to DHCP and drop forwarding to/from it",
    )
    p.add_argument("bridge")

    p = sub.add_parser("bridge-guard-remove", help="remove the guard rules for <bridge>")
    p.add_argument("bridge")

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
    p.add_argument(
        "--no-router",
        action="store_true",
        help="isolated bridge: send no router and no DNS option (lab bridges only)",
    )

    p = sub.add_parser("dnsmasq-stop", help="stop per-bridge dnsmasq DHCP/DNS")
    p.add_argument("bridge")

    sub.add_parser("dnsmasq-layout-version", help="report the protected DHCP layout version")

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
        "list-ipv4-addrs",
        help="print every host IPv4 address as '<iface> <cidr>' lines",
    )

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

    p = sub.add_parser(
        "qemu-process-signal",
        help="signal a registry-authorized QEMU process group",
    )
    p.add_argument("pid")
    p.add_argument("signal", choices=("term", "kill"))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # #334: what the helper creates must not depend on who invoked it. A
    # tight umask would lock the backend out of the dnsmasq leases it only
    # reads; a loose one would open the dnsmasq tree to everyone.
    os.umask(0o022)
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
