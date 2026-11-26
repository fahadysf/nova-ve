# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Read-only details for one network, for the canvas info panel (#318)."""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.services import host_net
from app.services.cloud_inventory_service import (
    LINUX_BRIDGE_TYPE,
    _base_data_dir,
    bridge_dhcp_config_on,
)
from app.services.lab_service import LabService, _normalize_relative_lab_path
from app.services.network_service import (
    BRIDGE_CLOUD_TYPE,
    NAT_CLOUD_STATIC_LAST_OFFSET,
    NAT_CLOUD_TYPE,
    NetworkServiceError,
)
from app.services.node_runtime_service import NodeRuntimeService
from app.services.shared_network_service import (
    LabNetwork,
    find_owner,
    is_member,
    member_key,
    segment,
    unshareable_reason,
)

SYS_CLASS_NET = Path("/sys/class/net")
PROC_ROOT = Path("/proc")
MAX_LEASES = 100
# #334: the privileged helper keeps dnsmasq's config, pid and leases under a
# root-owned /run tree; the backend only reads them. The pre-#334 location
# under the data dir is still read as a fallback for servers started before
# the upgrade.
DNSMASQ_ROOT = Path(os.environ.get("NOVA_VE_DNSMASQ_ROOT", "/run/nova-ve/dnsmasq"))


def _dnsmasq_dir(bridge: str) -> Path:
    """Directory holding the bridge's live dnsmasq state (new, else legacy)."""
    new = DNSMASQ_ROOT / bridge
    if (new / "dnsmasq.conf").exists():
        return new
    legacy = _base_data_dir() / "nat-cloud" / bridge
    if (legacy / "dnsmasq.conf").exists():
        return legacy
    return new

TYPE_LABELS = {
    "linux_bridge": "Isolated bridge",
    "nat_cloud": "NAT-Cloud",
    "bridge_cloud": "Bridge-Cloud",
    "ovs_bridge": "OVS bridge",
}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


# --------------------------------------------------------------------------
# Live host state
# --------------------------------------------------------------------------

def _bridge_state(bridge: str | None, driver: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "name": bridge,
        "driver": driver,
        "exists": False,
        "admin_up": None,
        "oper_state": None,
        "mac": None,
        "mtu": None,
        "ports": None,
    }
    # Records are plain JSON: never let a bridge name become a path.
    if not bridge or "/" in bridge or bridge in {".", ".."}:
        return state
    root = SYS_CLASS_NET / bridge
    if not root.is_dir():
        return state
    state["exists"] = True
    state["oper_state"] = _read_text(root / "operstate")
    state["mac"] = _read_text(root / "address")
    try:
        state["mtu"] = int(_read_text(root / "mtu") or "")
    except ValueError:
        pass
    try:
        state["admin_up"] = bool(int(_read_text(root / "flags") or "", 16) & 0x1)
    except ValueError:
        pass
    try:
        state["ports"] = len(os.listdir(root / "brif"))
    except OSError:
        pass
    return state


def _learned_macs(bridge: str | None) -> dict[str, str]:
    """``{mac: port}`` the bridge has learned from traffic (unprivileged read)."""
    if not bridge or not host_net.RE_BRIDGE_NAME.match(bridge):
        return {}
    try:
        proc = subprocess.run(
            [shutil.which("bridge") or "/usr/sbin/bridge", "fdb", "show", "br", bridge],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    learned: dict[str, str] = {}
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        # "<mac> dev <port> master <bridge> [permanent]"; permanent = host side
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "dev" and "permanent" not in parts:
            learned[parts[0].lower()] = parts[2]
    return learned


def _dnsmasq_pid(root: Path) -> int | None:
    """Pid of the bridge's dnsmasq if that process is really running.

    Same rule as the privileged helper: the process is identified by its exact
    ``--conf-file`` argument. The pid file is only a shortcut (#320).
    """
    # The helper does not resolve symlinks in the data directory; we do.
    wanted = os.path.realpath(root / "dnsmasq.conf")
    prefix = b"--conf-file="

    def serves(pid: str) -> bool:
        try:
            argv = (PROC_ROOT / pid / "cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
        return os.path.basename(argv[0]) == b"dnsmasq" and any(
            arg.startswith(prefix) and os.path.realpath(os.fsdecode(arg[len(prefix):])) == wanted
            for arg in argv
        )

    recorded = (_read_text(root / "dnsmasq.pid") or "").strip()
    if recorded.isdigit() and serves(recorded):
        return int(recorded)
    try:
        candidates = [name for name in os.listdir(PROC_ROOT) if name.isdigit()]
    except OSError:
        return None
    return next((int(pid) for pid in candidates if serves(pid)), None)


def dhcp_server_pid(bridge: str | None) -> int | None:
    """Pid of the DHCP server really running for a lab bridge."""
    # The directory name comes from a JSON record; only lab bridges have one.
    if not bridge or not host_net.RE_BRIDGE_NAME.match(bridge):
        return None
    # Try the new root-owned location first, then the pre-#334 one: a server
    # started before the upgrade still serves from the legacy config.
    return _dnsmasq_pid(DNSMASQ_ROOT / bridge) or _dnsmasq_pid(
        _base_data_dir() / "nat-cloud" / bridge
    )


def _lease_time(root: Path) -> str | None:
    for line in (_read_text(root / "dnsmasq.conf") or "").splitlines():
        if line.startswith("dhcp-range="):
            parts = line.partition("=")[2].split(",")
            return parts[2].strip() if len(parts) > 2 else None
    return None


def _leases(root: Path, now: float) -> list[dict[str, Any]]:
    leases: list[dict[str, Any]] = []
    for line in (_read_text(root / "leases") or "").splitlines():
        # "<expiry> <mac> <ip> <hostname|*> <client-id|*>"; expiry 0 = infinite
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            expiry = int(parts[0])
            address = ipaddress.IPv4Address(parts[2])
        except ValueError:
            continue
        if expiry and expiry < now:
            continue
        leases.append(
            {
                "ip": str(address),
                "mac": parts[1].lower(),
                "hostname": None if parts[3] == "*" else parts[3],
                "expires_at": (
                    datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat() if expiry else None
                ),
            }
        )
    return sorted(leases, key=lambda lease: int(ipaddress.IPv4Address(lease["ip"])))


# --------------------------------------------------------------------------
# Lab records
# --------------------------------------------------------------------------

def _endpoint_mac(node: dict, interface: dict, index: int) -> str | None:
    """MAC the interface uses, as far as the lab file determines it.

    QEMU boots its NICs from ``firstmac`` and ignores ``planned_mac``, which
    applies only to NICs added while the node runs.
    """
    planned = interface.get("planned_mac")
    if str(node.get("type") or "").lower() != "qemu":
        return str(planned).lower() if planned else None
    if planned and index >= int(node.get("ethernet") or 0):
        return str(planned).lower()
    first = node.get("firstmac") or _dict(node.get("extras")).get("firstmac")
    try:
        return NodeRuntimeService._mac_for_index(first, index).lower()
    except (ValueError, AttributeError):
        return None


def _endpoints(item: LabNetwork) -> list[dict[str, Any]]:
    nodes = _dict(item.data.get("nodes"))
    out: list[dict[str, Any]] = []
    for link in item.data.get("links") or []:
        ends = [link.get("from"), link.get("to")] if isinstance(link, dict) else []
        node_end: dict | None = None
        matches = False
        for end in ends:
            if not isinstance(end, dict):
                continue
            if "node_id" in end:
                node_end = end
            else:
                matches = matches or str(end.get("network_id")) == str(item.network_id)
        if not matches or node_end is None:
            continue
        node = _dict(nodes.get(str(node_end.get("node_id"))))
        try:
            index = int(node_end.get("interface_index", 0))
        except (TypeError, ValueError):
            continue
        interfaces = node.get("interfaces") or []
        interface = next(
            (i for i in interfaces if isinstance(i, dict) and i.get("index") == index), {}
        )
        out.append(
            {
                "lab_name": item.lab_name,
                "current_lab": False,
                "node_id": node_end.get("node_id"),
                "node_name": str(node.get("name") or f"node {node_end.get('node_id')}"),
                "interface": str(interface.get("name") or f"eth{index}"),
                "mac": _endpoint_mac(node, interface, index),
                "_ports": _host_ports(item.lab_id, node_end.get("node_id"), index),
            }
        )
    return out


def _host_ports(lab_id: str, node_id: Any, index: int) -> set[str]:
    """Host interface names this endpoint can have on its bridge."""
    try:
        return {
            host_net.tap_name(lab_id, int(node_id), index),
            host_net.veth_host_name(lab_id, int(node_id), index),
        }
    except Exception:  # noqa: BLE001 - no instance id, or an id out of range
        return set()


def _apply_learned_macs(endpoints: list[dict[str, Any]], learned: dict[str, str]) -> dict[str, str]:
    """Fill in MACs only the running node knows; returns ``{mac: node name}``."""
    names = {end["mac"]: end["node_name"] for end in endpoints if end["mac"]}
    for end in endpoints:
        ports = end.pop("_ports")
        macs = [mac for mac, port in learned.items() if port in ports]
        # Several MACs mean a switch or a router behind the port.
        if end["mac"] is None and len(macs) == 1:
            end["mac"] = macs[0]
        for mac in macs:
            names.setdefault(mac, end["node_name"])
    return names


def _mac_conflicts(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_mac: dict[str, list[str]] = {}
    for end in endpoints:
        if end["mac"]:
            by_mac.setdefault(end["mac"], []).append(
                f"{end['lab_name']} / {end['node_name']} {end['interface']}"
            )
    return [
        {"mac": mac, "endpoints": names} for mac, names in sorted(by_mac.items()) if len(names) > 1
    ]


def _ip_settings(network: dict, bridge: str | None, node_by_mac: dict[str, str]) -> dict | None:
    """Subnet, gateway and address use; ``None`` for a pure layer 2 network."""
    config = _dict(network.get("config"))
    runtime = _dict(network.get("runtime"))
    cidr = config.get("cidr")
    if not cidr:
        return None
    is_nat = str(network.get("type")) == NAT_CLOUD_TYPE
    out: dict[str, Any] = {
        "cidr": str(cidr),
        "gateway": config.get("gateway") or runtime.get("gateway"),
        "egress_interface": (
            runtime.get("egress_interface") or config.get("egress_interface") if is_nat else None
        ),
        "nat": is_nat,
        "static_start": None,
        "static_end": None,
        "used_ips": [str(ip) for ip in runtime.get("used_ips") or []],
        "dhcp": None,
    }
    if not is_nat:
        # #325: an isolated bridge has DHCP settings once its CIDR allows them.
        if str(network.get("type") or LINUX_BRIDGE_TYPE) == LINUX_BRIDGE_TYPE and config.get(
            "dhcp_server"
        ):
            out["dhcp"] = _dhcp_block(
                bridge_dhcp_config_on(config),
                config.get("dhcp_server"),
                config.get("dhcp_start"),
                config.get("dhcp_end"),
                bridge,
                node_by_mac,
            )
        return out

    try:
        base = int(ipaddress.ip_network(str(cidr)).network_address)
        first = int(runtime.get("first_offset") or 2)
        out["static_start"] = str(ipaddress.IPv4Address(base + first))
        out["static_end"] = str(ipaddress.IPv4Address(base + NAT_CLOUD_STATIC_LAST_OFFSET))
    except (ValueError, TypeError):
        pass

    out["dhcp"] = _dhcp_block(
        config.get("dhcp") is not False,
        out["gateway"],
        config.get("dhcp_start") or runtime.get("dhcp_start"),
        config.get("dhcp_end") or runtime.get("dhcp_end"),
        bridge,
        node_by_mac,
    )
    return out


def _dhcp_block(
    enabled: bool,
    server: Any,
    start: Any,
    end: Any,
    bridge: str | None,
    node_by_mac: dict[str, str],
) -> dict[str, Any]:
    """Saved DHCP settings next to the live server state and its leases."""
    try:
        size = int(ipaddress.IPv4Address(str(end))) - int(ipaddress.IPv4Address(str(start))) + 1
    except ValueError:
        size = None
    dhcp: dict[str, Any] = {
        "enabled": enabled,
        "server": server,
        "start": start,
        "end": end,
        "size": size,
        "lease_time": None,
        "running": False,
        "pid": None,
        "leases": [],
        "lease_count": 0,
    }
    # The directory name comes from a JSON record; only lab bridges have one.
    if bridge and host_net.RE_BRIDGE_NAME.match(bridge):
        root = _dnsmasq_dir(bridge)
        leases = _leases(root, time.time())
        for lease in leases:
            lease["node_name"] = node_by_mac.get(lease["mac"])
        dhcp["lease_time"] = _lease_time(root)
        dhcp["pid"] = dhcp_server_pid(bridge)
        dhcp["running"] = dhcp["pid"] is not None
        dhcp["leases"] = leases[:MAX_LEASES]
        dhcp["lease_count"] = len(leases)
    return dhcp


def build_network_info(
    lab_path: str, network_id: int, can_open: Callable[[str], bool]
) -> dict[str, Any]:
    """Details for one network.

    ``can_open`` decides whether the viewer may be told another lab's path.
    The labs on a shared segment are always named: they opted in to it.
    """
    normalized = _normalize_relative_lab_path(lab_path)
    data = LabService.read_lab_json_static(normalized)
    network = _dict(data.get("networks")).get(str(network_id))
    if not isinstance(network, dict):
        raise NetworkServiceError(404, "Network does not exist.")

    lab_id = str(data.get("id") or normalized)
    current = LabNetwork(
        lab_path=normalized,
        lab_id=lab_id,
        lab_name=str(_dict(data.get("meta")).get("name") or Path(normalized).stem),
        network_id=int(network_id),
        network=network,
        data=data,
    )
    joined = is_member(network)
    key = member_key(network)
    owner = find_owner(key) if joined else current
    source = owner or current

    labs = segment(key or current.key)
    if not any(i.lab_path == normalized and i.network_id == current.network_id for i in labs):
        labs.append(current)

    endpoints: list[dict[str, Any]] = []
    lab_rows: list[dict[str, Any]] = []
    for item in labs:
        own = item.lab_path == normalized and item.network_id == current.network_id
        rows = _endpoints(item)
        for row in rows:
            row["current_lab"] = own
        endpoints.extend(rows)
        lab_rows.append(
            {
                "lab_name": item.lab_name,
                "lab_path": item.lab_path if own or can_open(item.lab_path) else None,
                "network_name": item.name,
                "role": "member" if is_member(item.network) else "owner",
                "current_lab": own,
                "connections": len(rows),
            }
        )
    endpoints.sort(key=lambda e: (not e["current_lab"], e["lab_name"], e["node_name"], e["interface"]))

    runtime = _dict(network.get("runtime"))
    bridge = source.bridge_name or current.bridge_name
    mac_conflicts = _mac_conflicts(endpoints)
    node_by_mac = _apply_learned_macs(endpoints, _learned_macs(bridge))
    network_type = str(network.get("type") or "linux_bridge")
    reason = unshareable_reason(network)
    return {
        "id": current.network_id,
        "name": current.name,
        "type": network_type,
        "type_label": TYPE_LABELS.get(network_type, network_type),
        "visible": network.get("visibility") is not False,
        "created_at": runtime.get("created_at"),
        "bridge": _bridge_state(bridge, runtime.get("driver")),
        "host_bridge": (
            _dict(network.get("config")).get("host_bridge")
            if network_type == BRIDGE_CLOUD_TYPE
            else None
        ),
        "ip": _ip_settings(source.network, bridge, node_by_mac),
        "endpoints": endpoints,
        "mac_conflicts": mac_conflicts,
        "sharing": {
            "role": "member" if joined else "owner" if current.shared or len(lab_rows) > 1 else "none",
            "shareable": reason is None,
            "reason": reason,
            "owner_lab": owner.lab_name if joined and owner else None,
            "owner_missing": joined and owner is None,
            "labs": lab_rows if len(lab_rows) > 1 or joined or current.shared else [],
        },
    }
