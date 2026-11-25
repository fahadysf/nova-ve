# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource network service for v2 lab.json mutations.

US-063 + US-064 — JSON-side mutations under the per-lab flock.
US-202 — ``create_network`` / ``delete_network`` provision and tear down
the matching Linux bridge via the privileged helper, persisting
``runtime.bridge_name`` on the network record.
US-204c — Container interface IP bringup. ``create_network`` validates
the optional ``config.cidr`` (IPv4 only) and seeds
``runtime.used_ips: list[str]`` and ``runtime.first_offset: int``.
``_allocate_ip``/``_release_ip`` mutate the free-list under ``lab_lock``;
nsenter-based IP application happens OUTSIDE the lock by callers per the
plan §US-204c "Lock-hold-time discipline".
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import get_settings
from app.services import host_net
from app.services.cloud_inventory_service import (
    LINUX_BRIDGE_TYPE,
    bridge_dhcp_config_on,
    bridge_dhcp_enabled,
    find_nat_cloud_owner,
    list_dhcp_bridges,
    list_nat_clouds,
    nat_cloud_id,
    nat_cloud_ipam_lock,
)
from app.services.lab_lock import LabLockTimeout, lab_lock
from app.services.lab_service import (
    LabService,
    _bridge_server_cidr,
    _cleanup_bridge_dhcp_runtime,
    _normalize_relative_lab_path,
    keep_written_revision,
    locked_lab,
)
from app.services.link_service import _refcount, _recompute_mac_registry
from app.services.shared_network_service import (
    MEMBERSHIP_CONFIG_KEYS,
    LabNetwork,
    find_owner,
    find_shared_owner,
    is_member,
    lab_names,
    member_key,
    members_of,
    patch_violation,
    reclaim_fingerprint,
    unshareable_reason,
)
from app.services.ws_hub import ws_hub

logger = logging.getLogger("nova-ve")


# US-204c: free-list IPAM defaults. ``first_offset=2`` skips .0 (network)
# and .1 (conventional gateway reservation). The broadcast address is
# excluded explicitly inside ``_allocate_ip``.
DEFAULT_FIRST_OFFSET = 2
NAT_CLOUD_TYPE = "nat_cloud"
NAT_CLOUD_STATIC_FIRST_OFFSET = 2
NAT_CLOUD_STATIC_LAST_OFFSET = 99
NAT_CLOUD_DHCP_FIRST_OFFSET = 100

# Bridge-Cloud network type — transparent host-bridge backed by
# provision-ubuntu-2604.sh.  Lab side never adds/removes the bridge, never
# writes a fingerprint, never runs NAT or dnsmasq.  See
# .omc/plans/bridge-cloud-feature.md §4.4 for the full ownership model.
BRIDGE_CLOUD_TYPE = "bridge_cloud"

# #325: an isolated bridge may run DHCP when its CIDR is /22 to /29. Larger
# subnets would hand one lab a huge share of the host's address plan; smaller
# ones leave no room for a server address and a scope.
BRIDGE_DHCP_MIN_PREFIX = 22
BRIDGE_DHCP_MAX_PREFIX = 29
BRIDGE_DHCP_FIELDS = ("dhcp_server", "dhcp_start", "dhcp_end")


class NetworkServiceError(Exception):
    """Generic exception for network-service contract violations."""

    def __init__(self, code: int, message: str, *, extra: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except (ValueError, TypeError):
        return False


def _validate_cidr(cidr: Any) -> ipaddress.IPv4Network:
    """Validate a user-supplied CIDR string.

    IPv4 only — IPv6 raises 422 with explicit pointer to plan §5
    "deferred-IPv6". Empty/None inputs raise; callers must gate on the
    presence of ``config.cidr`` before calling.
    """
    if not isinstance(cidr, str) or not cidr.strip():
        raise NetworkServiceError(422, "config.cidr must be a non-empty string")
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=True)
    except (ValueError, TypeError) as exc:
        raise NetworkServiceError(
            422, f"config.cidr is not a valid CIDR: {exc}"
        ) from exc
    if isinstance(net, ipaddress.IPv6Network):
        raise NetworkServiceError(
            422,
            "IPv6 CIDR not yet supported (see plan §5 deferred-IPv6)",
        )
    return net


def _validate_ipv4(value: Any, field: str) -> ipaddress.IPv4Address:
    if not isinstance(value, str) or not value.strip():
        raise NetworkServiceError(422, f"{field} must be a non-empty IPv4 address")
    try:
        return ipaddress.IPv4Address(value.strip())
    except ValueError as exc:
        raise NetworkServiceError(422, f"{field} is not a valid IPv4 address: {exc}") from exc


def nat_cloud_dhcp_enabled(config: dict) -> bool:
    """Only an explicit ``false`` disables DHCP, as in the UI and provisioner."""
    return config.get("dhcp") is not False


def _reconcile_nat_cloud_dhcp(bridge: str, config: dict) -> int:
    if nat_cloud_dhcp_enabled(config):
        return host_net.dnsmasq_start(
            bridge,
            str(config["gateway"]),
            str(config["dhcp_start"]),
            str(config["dhcp_end"]),
        )
    host_net.dnsmasq_stop(bridge)
    return 0


def _field_error(field: str, message: str, code: int = 422) -> "NetworkServiceError":
    return NetworkServiceError(code, message, extra={"field": field})


def _nat_address(value: Any, field: str) -> ipaddress.IPv4Address:
    try:
        return _validate_ipv4(value, field)
    except NetworkServiceError as exc:
        raise _field_error(field, exc.message) from exc


def _restore_nat_cloud_dhcp(bridge: str, config: dict) -> None:
    """Best-effort return to the previous DHCP state after a failed edit."""
    try:
        _reconcile_nat_cloud_dhcp(bridge, config)
    except Exception:
        logger.exception("patch_network: failed to restore DHCP on %s", bridge)


def _settings_nat_cloud_pool() -> ipaddress.IPv4Network:
    raw = getattr(get_settings(), "NAT_CLOUD_POOL", "10.255.0.0/16")
    try:
        pool = ipaddress.ip_network(str(raw).strip(), strict=True)
    except ValueError as exc:
        raise NetworkServiceError(500, f"NAT-Cloud pool is invalid: {exc}") from exc
    if isinstance(pool, ipaddress.IPv6Network):
        raise NetworkServiceError(500, "NAT-Cloud pool must be IPv4")
    if pool.prefixlen > 24:
        raise NetworkServiceError(
            500,
            "NAT-Cloud pool must be at least large enough to allocate /24 networks",
        )
    return pool


def _existing_network_cidrs(networks: dict) -> list[ipaddress.IPv4Network]:
    out: list[ipaddress.IPv4Network] = []
    for network in networks.values():
        if not isinstance(network, dict):
            continue
        config = network.get("config") or {}
        if not isinstance(config, dict) or not config.get("cidr"):
            continue
        try:
            out.append(_validate_cidr(config["cidr"]))
        except NetworkServiceError:
            continue
    return out


def _host_ipv4_networks(
    *, exclude_bridge: str | None = None
) -> list[tuple[str, ipaddress.IPv4Network]]:
    occupied: list[tuple[str, ipaddress.IPv4Network]] = []
    try:
        host_addrs = host_net.list_ipv4_addrs()
    except host_net.HostNetError as exc:
        raise NetworkServiceError(
            503, f"Could not read host interfaces to check for subnet conflicts: {exc}"
        ) from exc
    for iface, raw_cidr in host_addrs:
        if iface == exclude_bridge:
            continue
        try:
            network = ipaddress.ip_network(raw_cidr, strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network):
            occupied.append((f"host interface {iface}", network))
    return occupied


def _nat_cloud_owner_networks(
    can_open: Callable[[str], bool] | None = None,
) -> list[tuple[str, ipaddress.IPv4Network]]:
    """Owner subnets in every lab; labs the caller cannot open stay unnamed."""
    occupied: list[tuple[str, ipaddress.IPv4Network]] = []
    for item in list_nat_clouds():
        if item.get("is_reference"):
            continue
        try:
            network = ipaddress.ip_network(str(item.get("cidr") or ""), strict=True)
        except ValueError:
            continue
        if not isinstance(network, ipaddress.IPv4Network):
            continue
        source = (
            f"NAT-Cloud {item.get('network_name')!r} in lab "
            f"{item.get('lab_name')!r} ({item.get('lab_path')})"
        )
        if can_open is not None and not can_open(str(item.get("lab_path") or "")):
            source = "a NAT-Cloud in another lab"
        occupied.append((source, network))
    return occupied


def _allocate_nat_cloud_cidr(networks: dict, extra_used: list[ipaddress.IPv4Network] | None = None) -> ipaddress.IPv4Network:
    used = _existing_network_cidrs(networks)
    if extra_used:
        used.extend(extra_used)
    for candidate in _settings_nat_cloud_pool().subnets(new_prefix=24):
        if any(candidate.overlaps(existing) for existing in used):
            continue
        return candidate
    raise NetworkServiceError(
        409,
        "NAT-Cloud subnet pool exhausted; configure a larger NOVA_VE_NAT_CLOUD_POOL.",
    )


def _next_network_id(networks: dict) -> int:
    return max((int(key) for key in networks if str(key).isdigit()), default=0) + 1


def _normalize_new_nat_cloud(
    raw_config: dict,
    lab_id: str,
    networks: dict,
    can_open: Callable[[str], bool] | None,
) -> dict:
    """Full checks for a NAT-Cloud that does not exist yet."""
    # A bridge left over from a failed delete is reused, so it is no conflict.
    own_bridge = host_net.bridge_name(lab_id, _next_network_id(networks))
    config = _normalize_nat_cloud_config(
        raw_config,
        networks,
        occupied=[
            *_nat_cloud_owner_networks(can_open),
            # #325: a DHCP bridge's subnet is as taken as a NAT-Cloud's.
            *_dhcp_bridge_networks(can_open),
            *_host_ipv4_networks(exclude_bridge=own_bridge),
        ],
    )
    egress = config.get("egress_interface")
    if egress and not host_net.host_iface_exists(egress):
        raise _field_error(
            "config.egress_interface", f"Host has no network interface named {egress!r}."
        )
    return config


def _normalize_nat_cloud_config(
    raw_config: dict,
    networks: dict,
    *,
    host_used: list[ipaddress.IPv4Network] | None = None,
    occupied: list[tuple[str, ipaddress.IPv4Network]] | None = None,
    check_overlap: bool = True,
) -> dict:
    dhcp = raw_config.get("dhcp", True)
    if not isinstance(dhcp, bool):
        raise _field_error("config.dhcp", "config.dhcp must be true or false")
    occupied = occupied or []
    occupied_networks = [network for _source, network in occupied]
    if raw_config.get("cidr"):
        try:
            _validate_cidr(raw_config["cidr"])
        except NetworkServiceError as exc:
            raise _field_error("config.cidr", exc.message) from exc
    cidr = (
        _validate_cidr(raw_config["cidr"])
        if raw_config.get("cidr")
        else _allocate_nat_cloud_cidr(
            networks, [*(host_used or []), *occupied_networks]
        )
    )
    if cidr.prefixlen > 24:
        raise _field_error("config.cidr", "NAT-Cloud CIDR must be /24 or larger")
    for source, existing in occupied if check_overlap else []:
        if cidr.overlaps(existing):
            raise NetworkServiceError(
                409,
                f"NAT-Cloud CIDR {cidr} overlaps {source} CIDR {existing}.",
                extra={
                    "field": "config.cidr",
                    "conflicting_cidr": str(existing),
                    "source": source,
                },
            )
    in_use = [*_existing_network_cidrs(networks), *(host_used or [])]
    for existing in in_use if check_overlap else []:
        if cidr.overlaps(existing):
            raise _field_error(
                "config.cidr",
                f"NAT-Cloud CIDR {cidr} overlaps existing network CIDR {existing}.",
                409,
            )

    gateway = (
        _nat_address(raw_config["gateway"], "config.gateway")
        if raw_config.get("gateway")
        else ipaddress.IPv4Address(int(cidr.network_address) + 1)
    )
    if gateway not in cidr:
        raise _field_error("config.gateway", "config.gateway must be inside config.cidr")
    if gateway in (cidr.network_address, cidr.broadcast_address):
        raise _field_error(
            "config.gateway", "config.gateway cannot be the network or broadcast address"
        )

    default_start = ipaddress.IPv4Address(int(cidr.network_address) + NAT_CLOUD_DHCP_FIRST_OFFSET)
    default_end = ipaddress.IPv4Address(int(cidr.broadcast_address) - 1)
    dhcp_start = (
        _nat_address(raw_config["dhcp_start"], "config.dhcp_start")
        if raw_config.get("dhcp_start")
        else default_start
    )
    dhcp_end = (
        _nat_address(raw_config["dhcp_end"], "config.dhcp_end")
        if raw_config.get("dhcp_end")
        else default_end
    )
    for field, address in (("config.dhcp_start", dhcp_start), ("config.dhcp_end", dhcp_end)):
        if address not in cidr:
            raise _field_error(field, "DHCP range must be inside config.cidr")
    if int(dhcp_start) > int(dhcp_end):
        raise _field_error("config.dhcp_end", "config.dhcp_start must be <= config.dhcp_end")
    static_last = ipaddress.IPv4Address(int(cidr.network_address) + NAT_CLOUD_STATIC_LAST_OFFSET)
    if int(dhcp_start) <= int(static_last):
        raise _field_error(
            "config.dhcp_start",
            "NAT-Cloud DHCP range must start after the reserved static range ending at offset .99",
        )

    config = dict(raw_config)
    config.update(
        {
            "cidr": str(cidr),
            "gateway": str(gateway),
            "dhcp": dhcp,
            "dhcp_start": str(dhcp_start),
            "dhcp_end": str(dhcp_end),
        }
    )
    if raw_config.get("egress_interface"):
        config["egress_interface"] = str(raw_config["egress_interface"]).strip()
    return config


def _normalize_nat_cloud_edit(current: dict, changes: dict) -> dict:
    """Merge a partial edit into an existing NAT-Cloud config.

    Only DHCP settings are editable. Overlap checks are skipped on purpose: the
    CIDR cannot change, and a conflict that appeared on the host later must not
    stop the owner from, say, disabling DHCP.
    """
    for key in ("cidr", "gateway", "egress_interface", *MEMBERSHIP_CONFIG_KEYS):
        if key in changes and (changes[key] or None) != (current.get(key) or None):
            raise _field_error(
                f"config.{key}",
                f"Cannot change a NAT-Cloud {'CIDR' if key == 'cidr' else key} after creation; "
                "delete and recreate the NAT-Cloud with the correct settings.",
            )
    merged = {**current, "dhcp": nat_cloud_dhcp_enabled(current)}
    merged.update({k: v for k, v in changes.items() if k in ("dhcp", "dhcp_start", "dhcp_end")})
    return _normalize_nat_cloud_config(merged, {}, check_overlap=False)


def _bridge_cidr(value: Any) -> ipaddress.IPv4Network:
    try:
        return _validate_cidr(value)
    except NetworkServiceError as exc:
        raise _field_error("config.cidr", exc.message) from exc


def bridge_dhcp_capable(cidr: ipaddress.IPv4Network) -> bool:
    return BRIDGE_DHCP_MIN_PREFIX <= cidr.prefixlen <= BRIDGE_DHCP_MAX_PREFIX


def bridge_dhcp_defaults(cidr: ipaddress.IPv4Network) -> dict[str, str]:
    """Server at the last usable address, scope over the upper half before it.

    Fits every size from /22 to /29: a /29 gets server .6 and scope .4-.5,
    leaving .1-.3 for static addresses.
    """
    base = int(cidr.network_address)
    last = int(cidr.broadcast_address)
    return {
        "dhcp_server": str(ipaddress.IPv4Address(last - 1)),
        "dhcp_start": str(ipaddress.IPv4Address(base + cidr.num_addresses // 2)),
        "dhcp_end": str(ipaddress.IPv4Address(last - 2)),
    }


def _normalize_bridge_config(raw_config: dict) -> dict:
    """Check an isolated bridge's CIDR and DHCP settings and fill defaults (#325).

    A CIDR alone is informational. DHCP settings are kept only for a CIDR that
    can have DHCP; a blank server or scope field means the default.
    """
    config = dict(raw_config)
    dhcp = config.get("dhcp", False)
    if not isinstance(dhcp, bool):
        raise _field_error("config.dhcp", "config.dhcp must be true or false")
    needs_cidr = (
        f"DHCP needs a CIDR between /{BRIDGE_DHCP_MIN_PREFIX} and /{BRIDGE_DHCP_MAX_PREFIX}."
    )
    if config.get("cidr") in (None, ""):
        if dhcp:
            raise _field_error("config.dhcp", needs_cidr)
        for key in BRIDGE_DHCP_FIELDS:
            config.pop(key, None)
        if "cidr" in config:
            config["cidr"] = None
        return config

    cidr = _bridge_cidr(config["cidr"])
    config["cidr"] = str(cidr)
    if not bridge_dhcp_capable(cidr):
        if dhcp:
            raise _field_error("config.dhcp", f"{needs_cidr} {cidr} is /{cidr.prefixlen}.")
        for key in BRIDGE_DHCP_FIELDS:
            config.pop(key, None)
        return config

    defaults = bridge_dhcp_defaults(cidr)
    values: dict[str, ipaddress.IPv4Address] = {}
    for key in BRIDGE_DHCP_FIELDS:
        raw = config.get(key)
        field = f"config.{key}"
        address = (
            ipaddress.IPv4Address(defaults[key])
            if raw is None or (isinstance(raw, str) and not raw.strip())
            else _nat_address(raw, field)
        )
        if address not in cidr or address in (cidr.network_address, cidr.broadcast_address):
            raise _field_error(field, f"{address} is not a usable host address in {cidr}.")
        values[key] = address
    server, start, end = (values[key] for key in BRIDGE_DHCP_FIELDS)
    if int(start) > int(end):
        raise _field_error("config.dhcp_end", "The DHCP range must end at or after its start.")
    if int(start) <= int(server) <= int(end):
        raise _field_error(
            "config.dhcp_server", "The server address must be outside the DHCP range."
        )
    config.update({"dhcp": dhcp, **{key: str(values[key]) for key in BRIDGE_DHCP_FIELDS}})
    return config


def _cidr_text(value: Any) -> str:
    try:
        return str(ipaddress.ip_network(str(value).strip(), strict=True))
    except ValueError:
        return str(value)


def _normalize_bridge_edit(current: dict, changes: dict) -> dict:
    """Merge a partial edit into an isolated bridge's settings.

    A CIDR can be added later but never changed or removed: addresses already
    given out and the overlap checks depend on it.
    """
    for key in MEMBERSHIP_CONFIG_KEYS:
        if changes.get(key):
            raise _field_error(
                f"config.{key}",
                "Joining a shared network is only possible when the network is created.",
            )
    current_cidr = current.get("cidr")
    if current_cidr and "cidr" in changes and (
        not changes["cidr"] or _cidr_text(changes["cidr"]) != _cidr_text(current_cidr)
    ):
        raise _field_error(
            "config.cidr", "CIDR cannot be changed; delete and recreate the network."
        )
    merged = {**current, "dhcp": current.get("dhcp") is True}
    merged.update(changes)
    return _normalize_bridge_config(merged)


def _dhcp_bridge_networks(
    can_open: Callable[[str], bool] | None = None,
    *,
    exclude: tuple[str, int] | None = None,
) -> list[tuple[str, ipaddress.IPv4Network]]:
    """Subnets of bridges that run DHCP, in every lab; ``exclude`` is self."""
    occupied: list[tuple[str, ipaddress.IPv4Network]] = []
    for item in list_dhcp_bridges():
        if exclude is not None and (item["lab_path"], item["network_id"]) == exclude:
            continue
        try:
            network = ipaddress.ip_network(str(item["cidr"]), strict=True)
        except ValueError:
            continue
        if not isinstance(network, ipaddress.IPv4Network):
            continue
        source = (
            f"DHCP bridge {item['network_name']!r} in lab "
            f"{item['lab_name']!r} ({item['lab_path']})"
        )
        if can_open is not None and not can_open(str(item["lab_path"])):
            source = "a DHCP bridge in another lab"
        occupied.append((source, network))
    return occupied


def _check_bridge_dhcp_overlaps(
    cidr: str,
    *,
    own_bridge: str,
    can_open: Callable[[str], bool] | None,
    exclude: tuple[str, int] | None = None,
) -> None:
    """A bridge that serves DHCP needs a subnet nothing else on the host uses."""
    subnet = ipaddress.ip_network(cidr)
    occupied = [
        *_nat_cloud_owner_networks(can_open),
        *_dhcp_bridge_networks(can_open, exclude=exclude),
        *_host_ipv4_networks(exclude_bridge=own_bridge),
    ]
    for source, existing in occupied:
        if subnet.overlaps(existing):
            raise NetworkServiceError(
                409,
                f"CIDR {subnet} overlaps {source} CIDR {existing}; "
                "a bridge that runs DHCP needs a subnet of its own.",
                extra={
                    "field": "config.cidr",
                    "conflicting_cidr": str(existing),
                    "source": source,
                },
            )


def _check_ipam_outside_dhcp(network: dict, config: dict) -> None:
    """Addresses already given to nodes must stay clear of the DHCP server."""
    server = ipaddress.IPv4Address(config["dhcp_server"])
    start = int(ipaddress.IPv4Address(config["dhcp_start"]))
    end = int(ipaddress.IPv4Address(config["dhcp_end"]))
    runtime = network.get("runtime")
    used = runtime.get("used_ips") if isinstance(runtime, dict) else None
    for raw in used or []:
        try:
            address = ipaddress.IPv4Address(str(raw))
        except ValueError:
            continue
        if address == server:
            raise _field_error(
                "config.dhcp_server",
                f"{address} is already assigned to a node on this network.",
            )
        if start <= int(address) <= end:
            raise _field_error(
                "config.dhcp_start",
                f"Node address {address} is inside the DHCP range; "
                "choose a range that leaves it out.",
            )


def _check_bridge_dhcp_change(
    key: tuple[str, int],
    network: dict,
    current: dict,
    config: dict,
    *,
    own_bridge: str,
    can_open: Callable[[str], bool] | None,
) -> None:
    """Host-wide checks for an edit of bridge ``key`` that leaves DHCP on."""
    if not bridge_dhcp_config_on(config):
        return
    if network.get("implicit") is True:
        raise _field_error("config.dhcp", "DHCP is not available on point-to-point links.")
    if not bridge_dhcp_config_on(current):
        _check_bridge_dhcp_overlaps(
            config["cidr"], own_bridge=own_bridge, can_open=can_open, exclude=key
        )
    _check_ipam_outside_dhcp(network, config)


def _apply_bridge_dhcp(bridge: str, old: dict, new: dict) -> int:
    """Move a bridge's host DHCP state from ``old`` settings to ``new``.

    The guard goes on before the address and comes off after it, so the
    address is never reachable unguarded. Returns the server pid (0 when off).
    """
    old_addr = _bridge_server_cidr(old) if bridge_dhcp_config_on(old) else None
    if bridge_dhcp_config_on(new):
        new_addr = _bridge_server_cidr(new)
        host_net.bridge_guard_apply(bridge)
        host_net.bridge_addr_add(bridge, new_addr)
        host_net.link_up(bridge)
        pid = host_net.dnsmasq_start_isolated(
            bridge, str(new["dhcp_server"]), str(new["dhcp_start"]), str(new["dhcp_end"])
        )
        if old_addr and old_addr != new_addr:
            host_net.bridge_addr_del(bridge, old_addr)
        return pid
    host_net.dnsmasq_stop(bridge)
    if old_addr:
        host_net.bridge_addr_del(bridge, old_addr)
    host_net.bridge_guard_remove(bridge)
    return 0


def _switch_bridge_dhcp(bridge: str, old: dict, new: dict) -> int:
    """``_apply_bridge_dhcp`` that puts the old state back when a step fails."""
    try:
        return _apply_bridge_dhcp(bridge, old, new)
    except host_net.HostNetError as exc:
        try:
            _apply_bridge_dhcp(bridge, new, old)
        except host_net.HostNetError:
            logger.exception("bridge DHCP: failed to restore the previous state on %s", bridge)
        raise NetworkServiceError(500, f"Could not apply the DHCP settings: {exc}") from exc


def _drop_new_bridge(bridge: str) -> None:
    """Remove a bridge this request created, after its create failed."""
    try:
        host_net.bridge_del(bridge)
    except host_net.HostNetError as exc:
        logger.error("create_network: could not remove new bridge %s (%s)", bridge, exc)


def _restore_bridge_dhcp(bridge: str, applied: dict, previous: dict) -> None:
    """Best-effort return to the previous DHCP state after a failed edit."""
    try:
        _apply_bridge_dhcp(bridge, applied, previous)
    except host_net.HostNetError:
        logger.exception("patch_network: failed to restore DHCP on %s", bridge)


def _record_bridge_dhcp(runtime: dict, config: dict, pid: int) -> bool:
    """Store the applied DHCP state on ``runtime``; True when it changed."""
    on = bridge_dhcp_config_on(config)
    desired: dict[str, Any] = {"dhcp": on, "dhcp_pid": pid}
    if on:
        desired.update({key: config[key] for key in BRIDGE_DHCP_FIELDS})
    changed = False
    for key in BRIDGE_DHCP_FIELDS:
        if not on and key in runtime:
            runtime.pop(key)
            changed = True
    for key, value in desired.items():
        if runtime.get(key) != value:
            runtime[key] = value
            changed = True
    return changed


def _normalize_shared_nat_cloud_config(shared_cloud_id: str) -> tuple[dict, dict]:
    owner = find_nat_cloud_owner(shared_cloud_id)
    if owner is None:
        raise NetworkServiceError(
            404,
            "NAT-Cloud shared_cloud_id does not reference an existing owner.",
            extra={"shared_cloud_id": shared_cloud_id},
        )
    for item in list_nat_clouds():
        if item.get("id") == shared_cloud_id and not item.get("safe_for_reuse", True):
            raise NetworkServiceError(
                409,
                "NAT-Cloud owner is not safe to reuse because its CIDR collides with another owner.",
                extra={"shared_cloud_id": shared_cloud_id, "warning": item.get("warning")},
            )
    owner_config = owner.network.get("config") or {}
    if not isinstance(owner_config, dict) or not owner_config.get("cidr"):
        raise NetworkServiceError(
            409,
            "NAT-Cloud owner is missing reusable configuration.",
            extra={"shared_cloud_id": shared_cloud_id},
        )
    config = dict(owner_config)
    config["shared_cloud_id"] = shared_cloud_id
    runtime = dict(owner.network.get("runtime") or {})
    return config, runtime


def _joinable_owner(shared_id: str, lab_id: str, networks: dict) -> LabNetwork:
    """Owner behind a ``shared_network_id`` that this lab may join (#319)."""
    owner = find_shared_owner(shared_id)
    if owner is None or unshareable_reason(owner.network):
        raise NetworkServiceError(
            404,
            "That shared network does not exist or is no longer shared.",
            extra={"shared_network_id": shared_id},
        )
    if owner.lab_id == lab_id:
        raise NetworkServiceError(409, "That shared network already belongs to this lab.")
    for network in networks.values():
        if member_key(network) == owner.key:
            raise NetworkServiceError(
                409,
                f"This lab already uses that shared network as {network.get('name')!r}.",
            )
    return owner


def _member_summaries(members: List[LabNetwork]) -> List[dict]:
    return [
        {
            "lab_path": member.lab_path,
            "network_id": member.network_id,
            "network_name": member.name,
        }
        for member in members
    ]


def _ensure_shared_bridge(owner: LabNetwork, bridge: str) -> None:
    """Provision a shared bridge under its owner's identity if it is missing."""
    if not host_net.bridge_exists(bridge):
        try:
            host_net.bridge_add(bridge)
        except host_net.HostNetEEXIST:
            pass  # the other lab was opened at the same moment
        host_net.bridge_fingerprint_write(bridge, owner.lab_id, owner.network_id)
        return
    if host_net.bridge_fingerprint_check(
        bridge, owner.lab_id, owner.network_id
    ) == "mismatch" and not reclaim_fingerprint(bridge, owner.lab_id, owner.network_id):
        raise host_net.HostNetBridgeOwnershipError(
            _bridge_ownership_message(bridge, owner.lab_id, owner.network_id)
        )


def _bridge_gateway_cidr(config: dict) -> str:
    cidr = _validate_cidr(config.get("cidr"))
    gateway = _validate_ipv4(config.get("gateway"), "config.gateway")
    return f"{gateway}/{cidr.prefixlen}"


def _serialize_network(network: dict, count: int) -> dict:
    out = dict(network)
    out["count"] = count
    return out


def _bridge_ownership_message(bridge: str, lab_id: str, network_id: int) -> str:
    actual = host_net.bridge_fingerprint_read(bridge)
    actual_desc = "absent" if actual is None else json.dumps(actual, sort_keys=True)
    return (
        f"Bridge {bridge} ownership check failed: expected lab_id={lab_id!r}, "
        f"network_id={int(network_id)}; actual fingerprint={actual_desc}"
    )


def _network_name_in_use(
    networks: dict, name: str, *, exclude_id: Optional[int] = None
) -> bool:
    """True when a non-implicit network already carries this display name.

    Comparison is case-insensitive after whitespace strip. Implicit
    networks are excluded — their names are synthesized for node-to-node
    link bridges and operators don't see or deconflict against them.
    """
    if not isinstance(name, str):
        return False
    target = name.strip().lower()
    if not target:
        return False
    for key, network in networks.items():
        if not isinstance(network, dict):
            continue
        if network.get("implicit") is True:
            continue
        if exclude_id is not None:
            try:
                if int(network.get("id", key)) == int(exclude_id):
                    continue
            except (TypeError, ValueError):
                pass
        existing = network.get("name")
        if isinstance(existing, str) and existing.strip().lower() == target:
            return True
    return False


def _network_ids_with_links(lab_data: dict) -> set:
    """Return the set of ``network_id`` values referenced by any link."""
    ids: set = set()
    for link in lab_data.get("links", []) or []:
        for endpoint in (link.get("from"), link.get("to")):
            if not isinstance(endpoint, dict):
                continue
            if "network_id" in endpoint:
                try:
                    ids.add(int(endpoint["network_id"]))
                except (TypeError, ValueError):
                    continue
    return ids


def _connected_iface_indices_by_node(lab_data: dict) -> Dict[int, set]:
    """Return ``{node_id: set(interface_index)}`` for indices that appear in any link."""
    out: Dict[int, set] = {}
    for link in lab_data.get("links", []) or []:
        for endpoint in (link.get("from"), link.get("to")):
            if not isinstance(endpoint, dict):
                continue
            if "node_id" not in endpoint:
                continue
            try:
                node_id = int(endpoint["node_id"])
                iface = int(endpoint.get("interface_index", 0))
            except (TypeError, ValueError):
                continue
            out.setdefault(node_id, set()).add(iface)
    return out


def _reconcile_qemu_nic_link_state(
    lab_id: str, lab_data: dict
) -> List[Dict[str, Any]]:
    """Walk running QEMU nodes and force per-NIC carrier state via QMP set_link.

    Returns one record per NIC index touched, shape ``{"node_id", "interface_index",
    "up", "ok", "reason"?}``. Best-effort: any per-node failure is captured in the
    return value and does not raise.
    """
    # Local import — node_runtime_service imports network_service at runtime
    # (e.g. via ``LabService``), so a top-level import would cycle.
    from app.services.node_runtime_service import NodeRuntimeService

    out: List[Dict[str, Any]] = []
    nodes = lab_data.get("nodes") or {}
    if not nodes:
        return out

    connected = _connected_iface_indices_by_node(lab_data)
    runtime_service = NodeRuntimeService()

    for raw_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if str(node.get("type") or "").lower() != "qemu":
            continue
        try:
            node_id = int(node.get("id", raw_id))
        except (TypeError, ValueError):
            continue
        ethernet = int(node.get("ethernet", 0) or 0)
        if ethernet <= 0:
            continue
        runtime = runtime_service._runtime_record(lab_id, node_id)
        if runtime is None:
            continue

        # Issue #174: full per-node reconcile of boot ifaces (TAP master,
        # UP, runtime.interface_attachments) to lab.json links[]. Bring the
        # host side into agreement with the JSON before we ask QEMU to
        # flip carrier — otherwise QMP set_link runs on stale TAP state.
        try:
            recon = runtime_service.reconcile_qemu_node_links(lab_id, lab_data, node)
            for w in recon.get("warnings", []):
                logger.warning(
                    "reconcile_qemu_node_links: lab=%s node=%s iface=%s reason=%s",
                    lab_id,
                    node_id,
                    w.get("interface_index"),
                    w.get("reason"),
                )
        except Exception:  # noqa: BLE001 — best-effort observability
            logger.exception(
                "reconcile_qemu_node_links failed for lab=%s node=%s",
                lab_id,
                node_id,
            )

        node_connected = connected.get(node_id, set())
        for index in range(ethernet):
            up = index in node_connected
            ok, reason = runtime_service.set_qemu_nic_link(
                lab_id, node_id, index, up=up
            )
            entry: Dict[str, Any] = {
                "node_id": node_id,
                "interface_index": index,
                "up": up,
                "ok": ok,
            }
            if not ok and reason:
                entry["reason"] = reason
            out.append(entry)

    return out


class NetworkService:
    def _ensure_nat_cloud_runtime(self, network: dict, bridge: str) -> bool:
        """Ensure host L3/NAT/DHCP state for a ``nat_cloud`` network.

        Returns True when persisted runtime fields changed.
        """
        config = network.get("config") or {}
        if not isinstance(config, dict):
            raise NetworkServiceError(422, "NAT-Cloud config must be an object.")

        gateway_cidr = _bridge_gateway_cidr(config)
        runtime = network.setdefault("runtime", {})
        egress = str(config.get("egress_interface") or "").strip()
        if not egress:
            egress = host_net.default_egress_iface()
        if not egress:
            raise NetworkServiceError(409, "Host has no default-route interface for NAT-Cloud egress.")
        runtime["egress_interface"] = egress

        host_net.bridge_addr_add(bridge, gateway_cidr)
        host_net.link_up(bridge)
        host_net.ipv4_forward_enable()
        host_net.nat_apply(bridge, str(config["cidr"]), egress)
        host_net.forward_apply(bridge, str(config["cidr"]), egress)

        dhcp_pid = 0
        dhcp_enabled = nat_cloud_dhcp_enabled(config)
        if dhcp_enabled:
            dhcp_pid = host_net.dnsmasq_start(
                bridge,
                str(config["gateway"]),
                str(config["dhcp_start"]),
                str(config["dhcp_end"]),
            )
        else:
            host_net.dnsmasq_stop(bridge)

        desired = {
            "bridge_name": bridge,
            "driver": NAT_CLOUD_TYPE,
            "gateway": str(config["gateway"]),
            "egress_interface": egress,
            "dhcp": dhcp_enabled,
            "dhcp_start": str(config["dhcp_start"]),
            "dhcp_end": str(config["dhcp_end"]),
            "dhcp_pid": dhcp_pid,
            "nat": "nftables",
        }
        changed = False
        for key, value in desired.items():
            if runtime.get(key) != value:
                runtime[key] = value
                changed = True
        return changed

    def _cleanup_nat_cloud_runtime(
        self,
        bridge: Optional[str],
        cidr: Optional[str] = None,
        egress: Optional[str] = None,
    ) -> None:
        if not bridge:
            return
        try:
            host_net.dnsmasq_stop(bridge)
        except host_net.HostNetError:
            logger.warning("cleanup_nat_cloud: dnsmasq_stop(%s) failed", bridge)
        try:
            host_net.forward_remove(bridge, cidr, egress)
        except host_net.HostNetError:
            logger.warning("cleanup_nat_cloud: forward_remove(%s) failed", bridge)
        try:
            host_net.nat_remove(bridge)
        except host_net.HostNetError:
            logger.warning("cleanup_nat_cloud: nat_remove(%s) failed", bridge)

    def _ensure_bridge_dhcp_runtime(self, network: dict, bridge: str) -> bool:
        """Bring an isolated bridge's host DHCP state in line with its settings.

        Restarts the server only when it is not running or its settings
        changed. A bridge with DHCP off is touched only when something is
        left to remove. Returns True when persisted runtime fields changed.
        """
        # Local import: network_info_service imports this module.
        from app.services.network_info_service import dhcp_server_pid

        config = network.get("config") or {}
        runtime = network.setdefault("runtime", {})
        applied = {
            "cidr": config.get("cidr"),
            "dhcp": runtime.get("dhcp") is True,
            **{key: runtime.get(key) for key in BRIDGE_DHCP_FIELDS},
        }
        if bridge_dhcp_enabled(network):
            host_net.bridge_guard_apply(bridge)
            host_net.bridge_addr_add(bridge, _bridge_server_cidr(config))
            host_net.link_up(bridge)
            pid = dhcp_server_pid(bridge)
            if pid is None or any(applied[key] != config[key] for key in BRIDGE_DHCP_FIELDS):
                pid = host_net.dnsmasq_start_isolated(
                    bridge, config["dhcp_server"], config["dhcp_start"], config["dhcp_end"]
                )
            if bridge_dhcp_config_on(applied) and applied["dhcp_server"] != config["dhcp_server"]:
                host_net.bridge_addr_del(bridge, _bridge_server_cidr(applied))
            return _record_bridge_dhcp(runtime, config, pid or 0)
        if applied["dhcp"] or (config.get("cidr") and dhcp_server_pid(bridge) is not None):
            _apply_bridge_dhcp(bridge, {**applied, "dhcp": True}, {})
            return _record_bridge_dhcp(runtime, {}, 0)
        return False

    def validate_network_config(
        self,
        lab_path: str,
        network_type: str,
        raw_config: dict,
        *,
        network_id: int | None = None,
        can_open: Callable[[str], bool] | None = None,
    ) -> dict:
        """Settings a create or edit would store, without changing anything."""
        if network_type == NAT_CLOUD_TYPE:
            return self.validate_nat_cloud_config(
                lab_path, raw_config, network_id=network_id, can_open=can_open
            )
        if network_type != LINUX_BRIDGE_TYPE:
            raise _field_error(
                "type", "Only Linux bridge and NAT-Cloud settings can be validated."
            )
        normalized = _normalize_relative_lab_path(lab_path)
        data = LabService.read_lab_json_static(normalized)
        networks = data.get("networks") or {}
        lab_id = str(data.get("id") or normalized)
        if network_id is None:
            config = _normalize_bridge_config(
                {k: v for k, v in raw_config.items() if k not in MEMBERSHIP_CONFIG_KEYS}
            )
            if config["dhcp"]:
                _check_bridge_dhcp_overlaps(
                    config["cidr"],
                    own_bridge=host_net.bridge_name(lab_id, _next_network_id(networks)),
                    can_open=can_open,
                )
            return config

        network = networks.get(str(network_id))
        if (
            not isinstance(network, dict)
            or str(network.get("type") or LINUX_BRIDGE_TYPE) != LINUX_BRIDGE_TYPE
        ):
            raise NetworkServiceError(404, "Network does not exist.")
        if is_member(network):
            raise NetworkServiceError(
                422, "Shared network settings are managed by the owning lab."
            )
        current = dict(network.get("config") or {})
        config = _normalize_bridge_edit(current, raw_config)
        runtime = network.get("runtime") or {}
        _check_bridge_dhcp_change(
            (normalized, int(network_id)),
            network,
            current,
            config,
            own_bridge=runtime.get("bridge_name") or host_net.bridge_name(lab_id, int(network_id)),
            can_open=can_open,
        )
        return config

    def validate_nat_cloud_config(
        self,
        lab_path: str,
        raw_config: dict,
        *,
        network_id: int | None = None,
        can_open: Callable[[str], bool] | None = None,
    ) -> dict:
        normalized = _normalize_relative_lab_path(lab_path)
        data = LabService.read_lab_json_static(normalized)
        networks = data.get("networks") or {}
        if network_id is None:
            lab_id = str(data.get("id") or normalized)
            return _normalize_new_nat_cloud(raw_config, lab_id, networks, can_open)

        network = networks.get(str(network_id))
        if not isinstance(network, dict) or network.get("type") != NAT_CLOUD_TYPE:
            raise NetworkServiceError(404, "NAT-Cloud does not exist.")
        if is_member(network):
            raise NetworkServiceError(
                422, "Shared NAT-Cloud settings are managed by the owning lab."
            )
        return _normalize_nat_cloud_edit(dict(network.get("config") or {}), raw_config)

    def list_networks(self, lab_path: str, *, include_hidden: bool = False) -> Dict[str, dict]:
        """Return networks keyed by string id, mirroring legacy router shape.

        Implicit and ``visibility=False`` networks are filtered out unless
        ``include_hidden`` is True. Each entry includes a derived ``count``.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        data = LabService.read_lab_json_static(normalized)
        networks = data.get("networks", {}) or {}
        out: Dict[str, dict] = {}
        for key, network in networks.items():
            if not isinstance(network, dict):
                continue
            if not include_hidden:
                if network.get("implicit") is True:
                    continue
                if network.get("visibility") is False:
                    continue
            try:
                net_id = int(network.get("id", key))
            except (TypeError, ValueError):
                continue
            out[str(net_id)] = _serialize_network(network, _refcount(data, net_id))
        return out

    def ensure_lab_bridges(self, lab_path: str) -> dict:
        """Idempotently provision the host bridge for every network in the lab.

        Walks ``data["networks"]``, resolves each network's expected bridge
        name (``runtime.bridge_name`` if stamped, else
        ``host_net.bridge_name``), and creates the bridge on the host if
        absent. Stamps the resolved name back onto the network record so
        future resolves don't rely on the canonical-derive path.

        Called on lab open to recover host state after a reboot or manual
        bridge removal — labs restored from static JSON never went through
        ``create_network`` so their bridges were never provisioned.

        Bridges that have at least one link endpoint pointing at them are
        forced ``UP`` after the existence/fingerprint check so a bridge
        with active ports never sits in admin-down (slave ports go to
        ``state disabled`` and forwarding stops, even though the slaves
        themselves are up).

        For every running QEMU node, the matching netdev's QMP link state
        is set to ``on`` for connected NIC indices and ``off`` for
        unconnected ones. This is independent of TAP-vs-hubport backing,
        so it works on already-running VMs whose unconnected NICs were
        booted before the ``link=off`` device-line fix landed.

        Returns ``{"ensured": [...], "created": [...], "skipped": [...],
        "raised": [...], "nic_link_state": [...]}``.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        ensured: List[str] = []
        created: List[str] = []
        skipped: List[Dict[str, Any]] = []
        raised: List[str] = []
        nat_cloud: List[Dict[str, Any]] = []
        nic_link_state: List[Dict[str, Any]] = []
        # Reconciled after this lab's lock is released: two labs that join
        # each other's NAT-Clouds must not wait on each other's locks.
        foreign_nat_owners: List[Tuple[int, str, Any, str]] = []
        # #325: likewise a joined bridge whose owner runs DHCP on it.
        foreign_dhcp_owners: List[Tuple[int, LabNetwork, str]] = []
        dhcp_bridges: List[Dict[str, Any]] = []

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
            lab_id = str(data.get("id") or normalized)
            networks = data.get("networks") or {}
            networks_with_links = _network_ids_with_links(data)
            dirty = False
            for key, network in networks.items():
                if not isinstance(network, dict):
                    skipped.append({"key": str(key), "reason": "non-dict network record"})
                    continue
                try:
                    network_id = int(network.get("id", key))
                except (TypeError, ValueError):
                    skipped.append({"key": str(key), "reason": "non-integer id"})
                    continue

                runtime_record = network.get("runtime") or {}
                bridge = runtime_record.get("bridge_name")
                if not isinstance(bridge, str) or not bridge:
                    bridge = host_net.bridge_name(lab_id, network_id)

                config = network.get("config") or {}
                shared_cloud_id = (
                    str(config.get("shared_cloud_id") or "").strip()
                    if isinstance(config, dict)
                    else ""
                )
                if str(network.get("type", "")) == NAT_CLOUD_TYPE and shared_cloud_id:
                    owner = find_nat_cloud_owner(shared_cloud_id)
                    if owner is None:
                        skipped.append(
                            {
                                "bridge": bridge,
                                "network_id": network_id,
                                "reason": "shared NAT-Cloud owner is missing",
                            }
                        )
                        continue
                    owner_bridge = owner.bridge_name or host_net.bridge_name(
                        owner.lab_id, owner.network_id
                    )
                    if owner.lab_path != normalized:
                        foreign_nat_owners.append(
                            (network_id, shared_cloud_id, owner, owner_bridge)
                        )
                        continue
                    try:
                        owner_network = networks.get(str(owner.network_id))
                        if isinstance(owner_network, dict):
                            if self._ensure_nat_cloud_runtime(owner_network, owner_bridge):
                                dirty = True
                        nat_cloud.append(
                            {
                                "network_id": network_id,
                                "bridge": owner_bridge,
                                "shared_cloud_id": shared_cloud_id,
                            }
                        )
                        ensured.append(owner_bridge)
                    except (NetworkServiceError, host_net.HostNetError, OSError) as exc:
                        skipped.append(
                            {
                                "bridge": owner_bridge,
                                "network_id": network_id,
                                "reason": str(exc),
                            }
                        )
                    continue

                # #319: a joined bridge is provisioned under its owner's
                # identity, so either lab can restore it after a reboot.
                bridge_lab_id, bridge_network_id = lab_id, network_id
                member_owner: Optional[LabNetwork] = None
                if is_member(network):
                    shared_owner = find_owner(member_key(network))
                    if shared_owner is None or not shared_owner.shared:
                        skipped.append(
                            {
                                "bridge": bridge,
                                "network_id": network_id,
                                "reason": "shared network owner is missing or no longer shares it",
                            }
                        )
                        continue
                    bridge_lab_id = shared_owner.lab_id
                    bridge_network_id = shared_owner.network_id
                    member_owner = shared_owner
                    bridge = shared_owner.bridge_name or host_net.bridge_name(
                        bridge_lab_id, bridge_network_id
                    )

                # Bridge-Cloud: host-owned bridge, NEVER fingerprint or add.
                # Verify-only.  Distinguish via runtime.driver (set at create
                # time) AND network.type (defensive, covers migrations).
                if (
                    runtime_record.get("driver") == BRIDGE_CLOUD_TYPE
                    or str(network.get("type", "")) == BRIDGE_CLOUD_TYPE
                ):
                    try:
                        if host_net.bridge_exists(bridge):
                            ensured.append(bridge)
                        else:
                            skipped.append({
                                "bridge": bridge,
                                "network_id": network_id,
                                "reason": (
                                    "host-bridge-missing — "
                                    "provision-ubuntu-2604.sh has not run"
                                ),
                            })
                    except host_net.HostNetError as exc:
                        skipped.append({
                            "bridge": bridge,
                            "network_id": network_id,
                            "reason": str(exc),
                        })
                    continue   # never fingerprint or add a host bridge

                try:
                    if host_net.bridge_exists(bridge):
                        status = host_net.bridge_fingerprint_check(
                            bridge, bridge_lab_id, bridge_network_id
                        )
                        if status == "mismatch" and not reclaim_fingerprint(
                            bridge, bridge_lab_id, bridge_network_id
                        ):
                            skipped.append({
                                "bridge": bridge,
                                "network_id": network_id,
                                "reason": _bridge_ownership_message(
                                    bridge, bridge_lab_id, bridge_network_id
                                ),
                            })
                            continue
                        if status == "absent":
                            try:
                                host_net.bridge_fingerprint_write(
                                    bridge, bridge_lab_id, bridge_network_id
                                )
                            except host_net.HostNetError:
                                pass
                        ensured.append(bridge)
                    else:
                        try:
                            host_net.bridge_add(bridge)
                        except host_net.HostNetEEXIST:
                            pass  # another lab on this bridge got there first
                        try:
                            host_net.bridge_fingerprint_write(
                                bridge, bridge_lab_id, bridge_network_id
                            )
                        except host_net.HostNetError:
                            pass
                        created.append(bridge)
                except host_net.HostNetError as exc:
                    skipped.append({
                        "bridge": bridge,
                        "network_id": network_id,
                        "reason": str(exc),
                    })
                    continue

                if network_id in networks_with_links:
                    try:
                        # #277: skip the privileged link-up spawn when the
                        # bridge is already admin-up (the common case on
                        # every lab open after the first).
                        _exists, _master, admin_up = host_net.link_state(bridge)
                        if not admin_up:
                            host_net.link_up(bridge)
                        raised.append(bridge)
                    except host_net.HostNetError as exc:
                        logger.warning(
                            "ensure_lab_bridges: link_up(%s) failed (%s); "
                            "bridge will stay admin-down until next reconcile",
                            bridge,
                            exc,
                        )

                existing_runtime = network.setdefault("runtime", {})
                if existing_runtime.get("bridge_name") != bridge:
                    existing_runtime["bridge_name"] = bridge
                    dirty = True
                if str(network.get("type", "")) == NAT_CLOUD_TYPE:
                    try:
                        if self._ensure_nat_cloud_runtime(network, bridge):
                            dirty = True
                        nat_cloud.append(
                            {
                                "network_id": network_id,
                                "bridge": bridge,
                                "egress_interface": network.get("runtime", {}).get("egress_interface"),
                            }
                        )
                    except (NetworkServiceError, host_net.HostNetError, OSError) as exc:
                        skipped.append(
                            {
                                "bridge": bridge,
                                "network_id": network_id,
                                "reason": str(exc),
                            }
                        )
                elif str(network.get("type") or LINUX_BRIDGE_TYPE) == LINUX_BRIDGE_TYPE:
                    if member_owner is not None:
                        if bridge_dhcp_enabled(member_owner.network):
                            foreign_dhcp_owners.append((network_id, member_owner, bridge))
                    else:
                        try:
                            if self._ensure_bridge_dhcp_runtime(network, bridge):
                                dirty = True
                            if bridge_dhcp_enabled(network):
                                dhcp_bridges.append({"network_id": network_id, "bridge": bridge})
                        except (
                            NetworkServiceError,
                            host_net.HostNetError,
                            OSError,
                            ValueError,
                        ) as exc:
                            skipped.append(
                                {"bridge": bridge, "network_id": network_id, "reason": str(exc)}
                            )

            if dirty:
                # #248: bridge reconcile is bookkeeping, not an edit.
                LabService.write_lab_json_static(normalized, data, bump_revision=False)

        for network_id, shared_cloud_id, owner, owner_bridge in foreign_nat_owners:
            try:
                _ensure_shared_bridge(owner, owner_bridge)
                with lab_lock(owner.lab_path, labs_dir):
                    owner_data = LabService.read_lab_json_static(owner.lab_path)
                    owner_network = (owner_data.get("networks") or {}).get(str(owner.network_id))
                    if isinstance(owner_network, dict) and self._ensure_nat_cloud_runtime(
                        owner_network, owner_bridge
                    ):
                        # #248: reconcile bookkeeping, not an edit.
                        LabService.write_lab_json_static(
                            owner.lab_path, owner_data, bump_revision=False
                        )
                nat_cloud.append(
                    {
                        "network_id": network_id,
                        "bridge": owner_bridge,
                        "shared_cloud_id": shared_cloud_id,
                    }
                )
                ensured.append(owner_bridge)
            except (
                NetworkServiceError,
                host_net.HostNetError,
                LabLockTimeout,
                OSError,
                ValueError,
            ) as exc:
                skipped.append(
                    {"bridge": owner_bridge, "network_id": network_id, "reason": str(exc)}
                )

        for network_id, owner, owner_bridge in foreign_dhcp_owners:
            try:
                with lab_lock(owner.lab_path, labs_dir):
                    owner_data = LabService.read_lab_json_static(owner.lab_path)
                    owner_network = (owner_data.get("networks") or {}).get(str(owner.network_id))
                    if bridge_dhcp_enabled(owner_network) and self._ensure_bridge_dhcp_runtime(
                        owner_network, owner_bridge
                    ):
                        # #248: reconcile bookkeeping, not an edit.
                        LabService.write_lab_json_static(
                            owner.lab_path, owner_data, bump_revision=False
                        )
                dhcp_bridges.append(
                    {"network_id": network_id, "bridge": owner_bridge, "owner_lab": owner.lab_name}
                )
            except (
                NetworkServiceError,
                host_net.HostNetError,
                LabLockTimeout,
                OSError,
                ValueError,
            ) as exc:
                skipped.append(
                    {"bridge": owner_bridge, "network_id": network_id, "reason": str(exc)}
                )

        # QMP set_link calls are idempotent and read-only w.r.t. lab.json —
        # run outside the lock to avoid holding it for O(nodes × NICs × 2s).
        nic_link_state = _reconcile_qemu_nic_link_state(lab_id, data)

        return {
            "ensured": ensured,
            "created": created,
            "skipped": skipped,
            "raised": raised,
            "nat_cloud": nat_cloud,
            "dhcp_bridges": dhcp_bridges,
            "nic_link_state": nic_link_state,
        }

    async def create_network(
        self,
        lab_path: str,
        request: dict,
        *,
        allow_unshared_clouds: bool = False,
        can_open: Callable[[str], bool] | None = None,
    ) -> dict:
        """Create a network in a lab.

        ``allow_unshared_clouds`` lets an administrator reuse any NAT-Cloud by
        ``shared_cloud_id``; everyone else can join only what is shared (#319).
        ``can_open`` keeps labs the caller cannot open out of conflict messages.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        # US-204c: validate CIDR (if any) before touching the lab lock so
        # invalid input cannot create partial state. ``config`` is the
        # user-supplied shape from ``NetworkCreate``/``NetworkConfig``.
        raw_config = request.get("config") or {}
        if not isinstance(raw_config, dict):
            raw_config = {}
        network_type = str(request.get("type", "linux_bridge") or "linux_bridge")
        cidr_value = raw_config.get("cidr")
        if cidr_value and network_type not in (NAT_CLOUD_TYPE, LINUX_BRIDGE_TYPE):
            _validate_cidr(cidr_value)

        raw_name = str(request.get("name") or "").strip()
        if not raw_name:
            raise NetworkServiceError(422, "Network name must be non-empty.")

        shared_network_id = str(raw_config.get("shared_network_id") or "").strip()
        requested_cloud_id = str(raw_config.get("shared_cloud_id") or "").strip()
        # #319: the record becomes a member only of what is resolved below.
        raw_config = {k: v for k, v in raw_config.items() if k not in MEMBERSHIP_CONFIG_KEYS}
        if requested_cloud_id and not shared_network_id and network_type != NAT_CLOUD_TYPE:
            raise NetworkServiceError(
                422, "config.shared_cloud_id is only valid on nat_cloud networks."
            )
        # #325: a joined bridge takes its owner's settings, checked there.
        bridge_config: dict | None = None
        if not shared_network_id:
            if network_type == LINUX_BRIDGE_TYPE:
                bridge_config = _normalize_bridge_config(raw_config)
            elif network_type != NAT_CLOUD_TYPE and raw_config.get("dhcp") is True:
                raise _field_error(
                    "config.dhcp", "DHCP is only available on Linux bridge and NAT-Cloud networks."
                )
        bridge_dhcp = bridge_dhcp_config_on(bridge_config)
        create_lock = (
            nat_cloud_ipam_lock()
            if network_type == NAT_CLOUD_TYPE or shared_network_id or bridge_dhcp
            else nullcontext()
        )
        with create_lock:
            with lab_lock(normalized, labs_dir):
                data = LabService.read_lab_json_static(normalized)
                lab_id = str(data.get("id") or normalized)
                networks = data.setdefault("networks", {})
                if _network_name_in_use(networks, raw_name):
                    raise NetworkServiceError(
                        409,
                        f"A network named {raw_name!r} already exists in this lab.",
                        extra={"name": raw_name},
                    )
                shared_owner: Optional[LabNetwork] = None
                if shared_network_id:
                    # #319: the owner decides type and settings. A NAT-Cloud
                    # is joined through its existing reference path.
                    shared_owner = _joinable_owner(shared_network_id, lab_id, networks)
                    network_type = shared_owner.type
                    if network_type == NAT_CLOUD_TYPE:
                        requested_cloud_id = nat_cloud_id(
                            shared_owner.lab_id, shared_owner.network_id
                        )
                    else:
                        requested_cloud_id = ""
                        raw_config = {
                            **(shared_owner.network.get("config") or {}),
                            "shared_network_id": shared_network_id,
                        }
                elif requested_cloud_id and not allow_unshared_clouds:
                    cloud = find_nat_cloud_owner(requested_cloud_id)
                    if cloud is None or cloud.network.get("shared") is not True:
                        raise NetworkServiceError(
                            404,
                            "NAT-Cloud shared_cloud_id does not reference a shared owner.",
                            extra={"shared_cloud_id": requested_cloud_id},
                        )
                shared_cloud_id = ""
                if network_type == NAT_CLOUD_TYPE:
                    shared_cloud_id = requested_cloud_id
                    if shared_cloud_id:
                        normalized_config, owner_runtime = _normalize_shared_nat_cloud_config(shared_cloud_id)
                    else:
                        normalized_config = _normalize_new_nat_cloud(
                            raw_config, lab_id, networks, can_open
                        )
                        owner_runtime = {}
                else:
                    normalized_config = (
                        bridge_config if bridge_config is not None else dict(raw_config)
                    )
                    owner_runtime = {}
                if bridge_dhcp:
                    _check_bridge_dhcp_overlaps(
                        normalized_config["cidr"],
                        own_bridge=host_net.bridge_name(lab_id, _next_network_id(networks)),
                        can_open=can_open,
                    )
                # Bridge-Cloud: host-owned bridge.  Validate config.host_bridge
                # name + existence BEFORE allocating an id or persisting state.
                if network_type == BRIDGE_CLOUD_TYPE:
                    host_bridge = normalized_config.get("host_bridge")
                    if not isinstance(host_bridge, str) or not host_net.RE_HOST_BRIDGE_NAME.fullmatch(host_bridge):
                        raise NetworkServiceError(
                            400,
                            "config.host_bridge must match ^br-eth[0-9]+$",
                            extra={"host_bridge": host_bridge},
                        )
                    if not host_net.bridge_exists(host_bridge):
                        raise NetworkServiceError(
                            404,
                            f"Host bridge {host_bridge!r} not found.  Run "
                            "provision-ubuntu-2604.sh on the host first.",
                            extra={"host_bridge": host_bridge},
                        )
                next_id = _next_network_id(networks)
                bridge = (
                    normalized_config["host_bridge"]
                    if network_type == BRIDGE_CLOUD_TYPE
                    else owner_runtime.get("bridge_name")
                    if network_type == NAT_CLOUD_TYPE and shared_cloud_id and owner_runtime.get("bridge_name")
                    else shared_owner.bridge_name
                    or host_net.bridge_name(shared_owner.lab_id, shared_owner.network_id)
                    if shared_owner is not None
                    else host_net.bridge_name(lab_id, next_id)
                )
                network = {
                    "id": next_id,
                    "name": raw_name,
                    "type": network_type,
                    "left": int(request.get("left", 0)),
                    "top": int(request.get("top", 0)),
                    "icon": "01-Cloud-Default.svg",
                    "width": 0,
                    "style": "Solid",
                    "linkstyle": "Straight",
                    "color": "",
                    "label": "",
                    "visibility": True,
                    "implicit": False,
                    "smart": -1,
                    "config": normalized_config,
                    "runtime": {
                        "bridge_name": bridge,
                        # US-401: provisioning-backend metadata for the
                        # reconciliation loop (US-402). ``driver`` mirrors
                        # the network's ``type`` vocabulary; ``created_at``
                        # is an ISO-8601 UTC timestamp.  For bridge_cloud
                        # ``driver`` is the discriminator the dispatcher in
                        # ``host_net.link_master_any`` keys off.
                        "driver": (
                            network_type
                            if network_type in (NAT_CLOUD_TYPE, BRIDGE_CLOUD_TYPE)
                            else "linux_bridge"
                        ),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        # US-204c: seed an empty free-list. Counter-based
                        # allocation would exhaust a /24 in 250 cycles even
                        # with one container; the free-list does not.
                        "used_ips": [],
                        "first_offset": (
                            NAT_CLOUD_STATIC_FIRST_OFFSET
                            if network_type == NAT_CLOUD_TYPE
                            else DEFAULT_FIRST_OFFSET
                        ),
                    },
                }
                if network_type == NAT_CLOUD_TYPE:
                    network["runtime"].update(
                        {
                            "gateway": normalized_config["gateway"],
                            "dhcp": normalized_config.get("dhcp", True),
                            "dhcp_start": normalized_config["dhcp_start"],
                            "dhcp_end": normalized_config["dhcp_end"],
                            "shared_owner": not bool(shared_cloud_id),
                            "shared_reference": bool(shared_cloud_id),
                        }
                    )
                if shared_owner is not None:
                    network["runtime"]["shared_reference"] = True
                networks[str(next_id)] = network
                data.pop("topology", None)
                # Persist BEFORE provisioning so on-disk state never leads the
                # kernel state. Any provisioning or ownership failure rolls it back.
                created_revision = LabService.write_lab_json_static(normalized, data)
                created_bridge = False
                try:
                    if network_type == BRIDGE_CLOUD_TYPE:
                        # Bridge-Cloud: host-owned bridge, existence validated
                        # above.  No bridge_add, no fingerprint, no NAT, no
                        # dnsmasq — the post-JSON-write step is a no-op.
                        pass
                    elif network_type == NAT_CLOUD_TYPE and shared_cloud_id:
                        # Reference records attach to the owner bridge and
                        # mutate owner IPAM only. Runtime exists on owner.
                        pass
                    elif shared_owner is not None:
                        _ensure_shared_bridge(shared_owner, bridge)
                    elif host_net.bridge_exists(bridge):
                        status = host_net.bridge_fingerprint_check(bridge, lab_id, next_id)
                        if status != "match":
                            raise host_net.HostNetBridgeOwnershipError(
                                _bridge_ownership_message(bridge, lab_id, next_id)
                            )
                    else:
                        host_net.bridge_add(bridge)
                        created_bridge = True
                        try:
                            host_net.bridge_fingerprint_write(
                                bridge, lab_id, next_id
                            )
                        except Exception:
                            try:
                                host_net.bridge_del(bridge)
                            except host_net.HostNetError as rollback_exc:
                                logger.error(
                                    "create_network: bridge fingerprint rollback failed for %s (%s)",
                                    bridge,
                                    rollback_exc,
                                )
                            raise
                    if network_type == NAT_CLOUD_TYPE and not shared_cloud_id:
                        self._ensure_nat_cloud_runtime(network, bridge)
                    if bridge_dhcp:
                        # Rolls its own host steps back when one fails.
                        pid = _switch_bridge_dhcp(bridge, {}, normalized_config)
                        _record_bridge_dhcp(network["runtime"], normalized_config, pid)
                except host_net.HostNetBridgeOwnershipError:
                    networks.pop(str(next_id), None)
                    data["networks"] = networks
                    # #248: rollback restores the pre-operation snapshot
                    # (revision included) — no phantom bump on a failed create.
                    LabService.write_lab_json_static(normalized, data, bump_revision=False)
                    raise
                except NetworkServiceError:
                    networks.pop(str(next_id), None)
                    data["networks"] = networks
                    LabService.write_lab_json_static(normalized, data, bump_revision=False)
                    if bridge_dhcp and created_bridge:
                        _drop_new_bridge(bridge)
                    if network_type == NAT_CLOUD_TYPE and not shared_cloud_id:
                        config = network.get("config") or {}
                        runtime = network.get("runtime") or {}
                        self._cleanup_nat_cloud_runtime(
                            bridge,
                            str(config.get("cidr") or ""),
                            str(runtime.get("egress_interface") or config.get("egress_interface") or ""),
                        )
                    raise
                except (host_net.HostNetError, OSError) as exc:
                    # Roll back the JSON write — never leave inconsistent state.
                    networks.pop(str(next_id), None)
                    data["networks"] = networks
                    LabService.write_lab_json_static(normalized, data, bump_revision=False)
                    if network_type == NAT_CLOUD_TYPE and not shared_cloud_id:
                        config = network.get("config") or {}
                        runtime = network.get("runtime") or {}
                        self._cleanup_nat_cloud_runtime(
                            bridge,
                            str(config.get("cidr") or ""),
                            str(runtime.get("egress_interface") or config.get("egress_interface") or ""),
                        )
                    logger.error(
                        "create_network: bridge provisioning failed for %s (%s); rolled back lab.json",
                        bridge,
                        exc,
                    )
                    raise NetworkServiceError(
                        409,
                        f"Failed to provision bridge {bridge}: {exc}",
                        extra={"bridge": bridge},
                    ) from exc
                if (network_type == NAT_CLOUD_TYPE and not shared_cloud_id) or bridge_dhcp:
                    # #248: runtime-info refresh after the create's real write
                    # — bookkeeping, don't bump again, nor undo the create's bump.
                    keep_written_revision(data, created_revision)
                    LabService.write_lab_json_static(normalized, data, bump_revision=False)
                _recompute_mac_registry(normalized, data)
                payload = _serialize_network(network, 0)

        await ws_hub.publish(normalized, "network_created", {"network": dict(payload)})
        return payload

    async def delete_network(self, lab_path: str, network_id: int) -> dict:
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        with nat_cloud_ipam_lock():
            with lab_lock(normalized, labs_dir):
                data = LabService.read_lab_json_static(normalized)
                networks = data.get("networks", {}) or {}
                network = networks.get(str(network_id))
                if not isinstance(network, dict):
                    raise NetworkServiceError(404, "Network does not exist.")
                if network.get("implicit") is True:
                    raise NetworkServiceError(404, "Network does not exist.")
                count = _refcount(data, int(network_id))
                if count > 0:
                    raise NetworkServiceError(
                        409,
                        "Cannot delete network with active attachments.",
                        extra={"count": count},
                    )
                config = network.get("config") or {}
                is_nat_cloud = str(network.get("type", "")) == NAT_CLOUD_TYPE
                joined = is_member(network)
                if not joined:
                    members = members_of(f"{data.get('id') or normalized}:{int(network_id)}")
                    if members:
                        raise NetworkServiceError(
                            409,
                            f"Cannot delete {network.get('name')!r} while it is used by "
                            f"another lab ({lab_names(members)}).",
                            extra={"references": _member_summaries(members)},
                        )
                removed = dict(network)
                networks.pop(str(network_id), None)
                data["networks"] = networks
                data.pop("topology", None)
                LabService.write_lab_json_static(normalized, data)
                _recompute_mac_registry(normalized, data)
                # Tear down the bridge AFTER the JSON write commits. Best-effort:
                # a bridge that has already been removed (e.g. by the orphan
                # sweeper from US-206) is logged but not propagated as a 5xx —
                # the network record is gone either way.
                runtime = network.get("runtime") or {}
                bridge = runtime.get("bridge_name")
                if not bridge:
                    # Pre-Wave-6 record — recompute the name to clean up.
                    lab_id = str(data.get("id") or normalized)
                    try:
                        bridge = host_net.bridge_name(lab_id, int(network_id))
                    except Exception:  # noqa: BLE001 — defensive
                        bridge = None
                if bridge:
                    network_driver = runtime.get("driver")
                    if (
                        str(network.get("type", "")) == BRIDGE_CLOUD_TYPE
                        or network_driver == BRIDGE_CLOUD_TYPE
                    ):
                        # Bridge-Cloud: host-owned bridge.  Lab cleanup MUST
                        # NOT call bridge_del or remove the fingerprint.  The
                        # 409 refcount guard above already prevents premature
                        # delete with active attachments.
                        logger.info(
                            "delete_network: host-owned bridge %s preserved (driver=bridge_cloud)",
                            bridge,
                        )
                    elif joined:
                        logger.info(
                            "delete_network: joined network removed; owner bridge %s preserved",
                            bridge,
                        )
                    else:
                        if is_nat_cloud:
                            self._cleanup_nat_cloud_runtime(
                                bridge,
                                str(config.get("cidr") or ""),
                                str(runtime.get("egress_interface") or config.get("egress_interface") or ""),
                            )
                        _cleanup_bridge_dhcp_runtime(bridge, network, "delete_network")
                        try:
                            host_net.bridge_del(bridge)
                        except host_net.HostNetEINVAL:
                            logger.info(
                                "delete_network: bridge %s already absent; nothing to do",
                                bridge,
                            )
                        except host_net.HostNetError as exc:
                            logger.warning(
                                "delete_network: bridge_del(%s) failed (%s); "
                                "JSON record removed regardless",
                                bridge,
                                exc,
                            )

        await ws_hub.publish(normalized, "network_deleted", {"network": removed})
        return removed

    # ------------------------------------------------------------------
    # US-204c — IPAM free-list allocator
    # ------------------------------------------------------------------

    def _ipam_target(self, lab_path: str, network_id: int) -> tuple[str, int]:
        """Return the lab/network record that owns IPAM for this network."""
        normalized = _normalize_relative_lab_path(lab_path)
        data = LabService.read_lab_json_static(normalized)
        networks = data.get("networks", {}) or {}
        network = networks.get(str(network_id))
        if not isinstance(network, dict):
            raise NetworkServiceError(404, "Network does not exist.")
        key = member_key(network)
        if key is None:
            return normalized, int(network_id)
        owner = find_owner(key)
        if owner is None:
            raise NetworkServiceError(
                409,
                "The owner of this shared network is missing; cannot allocate an address.",
            )
        return owner.lab_path, owner.network_id

    def _allocate_ip(self, lab_path: str, network_id: int) -> str:
        """Reserve and return the lowest free IP from ``network.config.cidr``.

        Walks the CIDR's host range starting at ``runtime.first_offset``,
        skipping the network/broadcast addresses and any IP already in
        ``runtime.used_ips``. The chosen address is appended to
        ``used_ips`` (kept sorted by integer value) and persisted under
        the per-lab flock. Callers MUST execute ``nsenter ip addr add``
        OUTSIDE the lock and call ``_release_ip`` on failure (plan
        §US-204c "Lock-hold-time discipline" / "Rollback semantics").

        Raises:
            NetworkServiceError(404): the network does not exist.
            NetworkServiceError(422): the network has no ``config.cidr``,
                or the CIDR cannot be parsed.
            NetworkServiceError(409): the subnet has no free addresses.
        """
        labs_dir = get_settings().LABS_DIR

        with nat_cloud_ipam_lock():
            target_lab_path = _normalize_relative_lab_path(lab_path)
            with lab_lock(target_lab_path, labs_dir):
                owner_lab_path, owner_network_id = self._ipam_target(target_lab_path, network_id)
            with lab_lock(owner_lab_path, labs_dir):
                data = LabService.read_lab_json_static(owner_lab_path)
                networks = data.get("networks", {}) or {}
                network = networks.get(str(owner_network_id))
                if not isinstance(network, dict):
                    raise NetworkServiceError(404, "Network does not exist.")
                config = network.get("config") or {}
                cidr_value = config.get("cidr") if isinstance(config, dict) else None
                if not cidr_value:
                    raise NetworkServiceError(
                        422,
                        "Network has no config.cidr; allocate_ip not applicable.",
                        extra={"network_id": int(network_id)},
                    )
                net = _validate_cidr(cidr_value)

                runtime = network.setdefault("runtime", {})
                used_raw = runtime.get("used_ips") or []
                # Defensive: tolerate persisted garbage (None, non-string)
                # and keep only addresses that parse and live in the CIDR.
                used: set = set()
                for entry in used_raw:
                    if not isinstance(entry, str):
                        continue
                    try:
                        addr = ipaddress.IPv4Address(entry)
                    except ValueError:
                        continue
                    if addr in net:
                        used.add(addr)

                first_offset = int(runtime.get("first_offset") or DEFAULT_FIRST_OFFSET)
                if first_offset < 1:
                    first_offset = 1
                network_addr = net.network_address
                broadcast_addr = net.broadcast_address
                start_int = int(network_addr) + first_offset
                end_int = int(broadcast_addr) - 1  # exclusive of broadcast
                if str(network.get("type", "")) == NAT_CLOUD_TYPE:
                    end_int = min(end_int, int(network_addr) + NAT_CLOUD_STATIC_LAST_OFFSET)
                # #325: the DHCP server and scope are not for static use.
                dhcp_ints: set[int] = set()
                if bridge_dhcp_enabled(network):
                    dhcp_ints = {int(ipaddress.IPv4Address(config["dhcp_server"]))}
                    dhcp_ints.update(
                        range(
                            int(ipaddress.IPv4Address(config["dhcp_start"])),
                            int(ipaddress.IPv4Address(config["dhcp_end"])) + 1,
                        )
                    )

                chosen: Optional[ipaddress.IPv4Address] = None
                for candidate_int in range(start_int, end_int + 1):
                    candidate = ipaddress.IPv4Address(candidate_int)
                    if candidate in used or candidate_int in dhcp_ints:
                        continue
                    chosen = candidate
                    break

                if chosen is None:
                    raise NetworkServiceError(
                        409,
                        "subnet exhausted, please widen CIDR",
                        extra={"network_id": int(network_id), "cidr": str(net)},
                    )

                used.add(chosen)
                runtime["used_ips"] = sorted(
                    (str(addr) for addr in used),
                    key=lambda s: int(ipaddress.IPv4Address(s)),
                )
                runtime.setdefault("first_offset", first_offset)
                networks[str(owner_network_id)] = network
                data["networks"] = networks
                data.pop("topology", None)
                # #248: DHCP allocation bookkeeping, not an edit.
                LabService.write_lab_json_static(owner_lab_path, data, bump_revision=False)

        return str(chosen)

    def _release_ip(
        self,
        lab_path: str,
        network_id: int,
        ip: str,
        *,
        _lab_lock_held: bool = False,
    ) -> bool:
        """Remove ``ip`` from ``runtime.used_ips``; return True if removed.

        No-op (returns False) when the network or IP is absent —
        idempotent so detach paths can call this without a pre-check.

        ``_lab_lock_held`` (#208a/b architect-iter1): set True when the
        caller already holds ``lab_lock(normalized, labs_dir)`` so the
        inner acquisition is skipped. Used by paired-create's compensation
        path (delete_link from inside the outer paired lab_lock); fcntl
        flock LOCK_EX is not reentrant within the same Linux process.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        if _lab_lock_held:
            data = LabService.read_lab_json_static(normalized)
            networks = data.get("networks", {}) or {}
            network = networks.get(str(network_id))
            if not isinstance(network, dict):
                return False
            key = member_key(network)
            if key is not None:
                owner = find_owner(key)
                if owner is None:
                    return False
                target_lab_path = owner.lab_path
                target_network_id = owner.network_id
                target_lock_cm = (
                    nullcontext()
                    if target_lab_path == normalized
                    else lab_lock(target_lab_path, labs_dir)
                )
            else:
                target_lab_path = normalized
                target_network_id = int(network_id)
                target_lock_cm = nullcontext()
            with target_lock_cm:
                return self._release_ip_from_owner(target_lab_path, target_network_id, ip)

        with nat_cloud_ipam_lock():
            with lab_lock(normalized, labs_dir):
                target_lab_path, target_network_id = self._ipam_target(normalized, network_id)
            with lab_lock(target_lab_path, labs_dir):
                return self._release_ip_from_owner(target_lab_path, target_network_id, ip)

    def _release_ip_from_owner(self, lab_path: str, network_id: int, ip: str) -> bool:
        data = LabService.read_lab_json_static(lab_path)
        networks = data.get("networks", {}) or {}
        network = networks.get(str(network_id))
        if not isinstance(network, dict):
            return False
        runtime = network.get("runtime") or {}
        used = list(runtime.get("used_ips") or [])
        if ip not in used:
            return False
        used.remove(ip)
        runtime["used_ips"] = sorted(
            used,
            key=lambda s: int(ipaddress.IPv4Address(s))
            if _is_ipv4(s)
            else 0,
        )
        network["runtime"] = runtime
        networks[str(network_id)] = network
        data["networks"] = networks
        data.pop("topology", None)
        # #248: DHCP release bookkeeping, not an edit.
        LabService.write_lab_json_static(lab_path, data, bump_revision=False)
        return True

    async def patch_network(
        self,
        lab_path: str,
        network_id: int,
        patch: Dict[str, Any],
        *,
        can_open: Callable[[str], bool] | None = None,
    ) -> Tuple[dict, str]:
        """Patch a network record. Returns (payload, event_type).

        ``event_type`` is ``network_promoted`` when the patch turned an
        implicit network into an explicit one, otherwise ``network_updated``.
        ``can_open`` keeps labs the caller cannot open out of conflict messages.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        # A worker thread, so a slow dnsmasq stop does not stall the event
        # loop. Safe because every lab-file writer takes ``lab_lock`` and
        # re-reads under it (#328): edits made meanwhile wait, then land.
        payload, event_type = await asyncio.to_thread(
            self._patch_network_locked, normalized, network_id, patch, can_open=can_open
        )
        await ws_hub.publish(normalized, event_type, {"network": dict(payload)})
        return payload, event_type

    def _patch_network_locked(
        self,
        normalized: str,
        network_id: int,
        patch: Dict[str, Any],
        *,
        can_open: Callable[[str], bool] | None = None,
    ) -> Tuple[dict, str]:
        # Local import: network_info_service imports this module.
        from app.services.network_info_service import dhcp_server_pid

        # #325: turning DHCP on for a bridge claims its subnet host-wide.
        subnet_lock = (
            nat_cloud_ipam_lock() if isinstance(patch.get("config"), dict) else nullcontext()
        )

        # Runs in a worker thread (see patch_network).
        with subnet_lock, locked_lab(normalized) as lab:
            data = lab.data
            networks = data.get("networks", {}) or {}
            network = networks.get(str(network_id))
            if not isinstance(network, dict):
                raise NetworkServiceError(404, "Network does not exist.")

            was_implicit = network.get("implicit") is True
            promoted = False

            if "name" in patch:
                new_name = patch["name"]
                if new_name is None:
                    if not was_implicit:
                        raise NetworkServiceError(
                            422,
                            "cannot un-name a promoted bridge",
                        )
                else:
                    new_name = str(new_name).strip()
                    if new_name and _network_name_in_use(
                        networks, new_name, exclude_id=int(network_id)
                    ):
                        raise NetworkServiceError(
                            409,
                            f"A network named {new_name!r} already exists in this lab.",
                            extra={"name": new_name},
                        )
                    if new_name and was_implicit:
                        network["name"] = new_name
                        network["implicit"] = False
                        network["visibility"] = True
                        promoted = True
                    else:
                        network["name"] = new_name

            # Bridge-Cloud security: PATCH is NOT the validated path for
            # bridge_cloud creation.  Refuse type↔bridge_cloud transitions
            # and refuse setting ``config.host_bridge`` on a
            # non-bridge_cloud record.  Without this, an authenticated
            # editor could mutate ``type="bridge_cloud"`` on an existing
            # linux_bridge record (leaving stale runtime.driver), and
            # subsequent lifecycle code that branches on ``type`` (e.g.
            # ``_cleanup_lab_network_runtime``) would corrupt cleanup.
            current_type = str(network.get("type", "linux_bridge") or "linux_bridge")
            new_type = patch.get("type")
            if new_type is not None and new_type != current_type:
                if new_type == BRIDGE_CLOUD_TYPE or current_type == BRIDGE_CLOUD_TYPE:
                    raise NetworkServiceError(
                        422,
                        "Cannot mutate network type to/from 'bridge_cloud' via "
                        "PATCH; delete and recreate the network instead.",
                    )
            new_config = patch.get("config")
            if isinstance(new_config, dict) and "host_bridge" in new_config:
                if current_type != BRIDGE_CLOUD_TYPE:
                    raise NetworkServiceError(
                        422,
                        "config.host_bridge is only valid on bridge_cloud "
                        "networks; cannot set it on a non-bridge_cloud record.",
                    )
            # Bridge-Cloud is create-only — ``config`` (specifically
            # ``host_bridge``) MUST NOT be re-targeted after create.
            # Re-pointing a bridge_cloud network at a different host
            # bridge requires delete + recreate so the runtime metadata,
            # bridge ownership, and any attached links stay consistent.
            if current_type == BRIDGE_CLOUD_TYPE and "config" in patch:
                raise NetworkServiceError(
                    422,
                    "Cannot mutate config on a bridge_cloud network; "
                    "delete and recreate to retarget the host bridge.",
                )
            # #319: same for joined networks, whose settings live in another lab.
            violation = patch_violation(network, patch)
            if violation:
                raise NetworkServiceError(422, violation)
            # Service-managed fields MUST NOT be mutated via PATCH.
            for forbidden in ("runtime", "id", "implicit"):
                if forbidden in patch:
                    raise NetworkServiceError(
                        422,
                        f"PATCH cannot mutate {forbidden!r} (service-managed).",
                    )

            if new_type is not None and new_type != current_type and NAT_CLOUD_TYPE in (new_type, current_type):
                raise NetworkServiceError(
                    422,
                    "Cannot change a network type to or from NAT-Cloud; delete and recreate the network.",
                )
            normalized_nat_config: dict | None = None
            current_nat_config: dict = {}
            if current_type == NAT_CLOUD_TYPE and isinstance(new_config, dict):
                current_nat_config = dict(network.get("config") or {})
                normalized_nat_config = _normalize_nat_cloud_edit(current_nat_config, new_config)
            if new_type is not None and new_type != current_type and bridge_dhcp_enabled(network):
                raise _field_error("type", "Turn DHCP off before changing the network type.")
            normalized_bridge_config: dict | None = None
            current_bridge_config: dict = {}
            bridge = ""
            resulting_type = new_type or current_type
            if isinstance(new_config, dict) and resulting_type == LINUX_BRIDGE_TYPE:
                if current_type == LINUX_BRIDGE_TYPE:
                    current_bridge_config = dict(network.get("config") or {})
                normalized_bridge_config = _normalize_bridge_edit(current_bridge_config, new_config)
                if normalized_bridge_config.get("cidr"):
                    bridge = (network.get("runtime") or {}).get(
                        "bridge_name"
                    ) or host_net.bridge_name(str(data.get("id") or normalized), int(network_id))
                _check_bridge_dhcp_change(
                    (normalized, int(network_id)),
                    network,
                    current_bridge_config,
                    normalized_bridge_config,
                    own_bridge=bridge,
                    can_open=can_open,
                )
            elif (
                isinstance(new_config, dict)
                and resulting_type != NAT_CLOUD_TYPE
                and new_config.get("dhcp") is True
            ):
                raise _field_error(
                    "config.dhcp", "DHCP is only available on Linux bridge and NAT-Cloud networks."
                )

            for field in ("type", "left", "top", "icon", "visibility", "config"):
                if field in patch and patch[field] is not None:
                    if field == "config" and normalized_nat_config is not None:
                        network[field] = normalized_nat_config
                    elif field == "config" and normalized_bridge_config is not None:
                        network[field] = normalized_bridge_config
                    else:
                        network[field] = patch[field]

            dhcp_rollback: tuple[str, dict] | None = None
            if normalized_nat_config is not None:
                runtime = network.setdefault("runtime", {})
                bridge = runtime.get("bridge_name") or host_net.bridge_name(
                    str(data.get("id") or normalized), int(network_id)
                )
                dhcp_changed = any(
                    current_nat_config.get(key) != normalized_nat_config.get(key)
                    for key in ("dhcp", "dhcp_start", "dhcp_end")
                )
                # An explicit ``dhcp`` also repairs a server whose real state has
                # drifted from the setting, for example after it crashed.
                if not dhcp_changed and "dhcp" in new_config:
                    dhcp_changed = (
                        dhcp_server_pid(bridge) is not None
                    ) != normalized_nat_config["dhcp"]
                if dhcp_changed:
                    # Fill keys that older records may not carry, so a
                    # rollback always has a complete scope to restore.
                    previous = {**normalized_nat_config, **current_nat_config}
                    try:
                        dhcp_pid = _reconcile_nat_cloud_dhcp(bridge, normalized_nat_config)
                    except host_net.HostNetError as exc:
                        # dnsmasq-start stops the old server first: put it back.
                        _restore_nat_cloud_dhcp(bridge, previous)
                        raise NetworkServiceError(
                            500, f"Could not apply the DHCP settings: {exc}"
                        ) from exc
                    dhcp_rollback = (bridge, previous)
                    runtime.update(
                        {
                            "dhcp": normalized_nat_config["dhcp"],
                            "dhcp_start": normalized_nat_config["dhcp_start"],
                            "dhcp_end": normalized_nat_config["dhcp_end"],
                            "dhcp_pid": dhcp_pid,
                        }
                    )

            bridge_rollback: tuple[str, dict, dict] | None = None
            if normalized_bridge_config is not None:
                old_on = bridge_dhcp_config_on(current_bridge_config)
                new_on = bridge_dhcp_config_on(normalized_bridge_config)
                old_state = current_bridge_config if old_on else {}
                switch = old_on != new_on or (
                    new_on
                    and any(
                        current_bridge_config.get(key) != normalized_bridge_config[key]
                        for key in BRIDGE_DHCP_FIELDS
                    )
                )
                # As for NAT-Clouds, an explicit ``dhcp`` repairs a server
                # whose real state has drifted from the setting.
                if not switch and "dhcp" in new_config and normalized_bridge_config.get("cidr"):
                    switch = (dhcp_server_pid(bridge) is not None) != new_on
                    if switch and not new_on:
                        old_state = {**current_bridge_config, "dhcp": True}
                if switch:
                    pid = _switch_bridge_dhcp(bridge, old_state, normalized_bridge_config)
                    _record_bridge_dhcp(network.setdefault("runtime", {}), normalized_bridge_config, pid)
                    bridge_rollback = (bridge, normalized_bridge_config, old_state)

            networks[str(network_id)] = network
            data.pop("topology", None)
            try:
                lab.save()
            except Exception:
                if dhcp_rollback is not None:
                    _restore_nat_cloud_dhcp(*dhcp_rollback)
                if bridge_rollback is not None:
                    _restore_bridge_dhcp(*bridge_rollback)
                raise
            _recompute_mac_registry(normalized, data)
            count = _refcount(data, int(network_id))
            payload = _serialize_network(network, count)
            event_type = "network_promoted" if promoted else "network_updated"

        return payload, event_type

    async def set_sharing(self, lab_path: str, network_id: int, shared: bool) -> Tuple[dict, bool]:
        """Offer a network to other labs, or stop (#319).

        Returns ``(payload, changed)``. Stopping is refused while another lab
        still uses the network.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        with nat_cloud_ipam_lock():
            with lab_lock(normalized, labs_dir):
                data = LabService.read_lab_json_static(normalized)
                network = (data.get("networks") or {}).get(str(network_id))
                if not isinstance(network, dict) or network.get("implicit") is True:
                    raise NetworkServiceError(404, "Network does not exist.")
                reason = unshareable_reason(network)
                if reason:
                    raise NetworkServiceError(422, reason)
                if not shared:
                    members = members_of(f"{data.get('id') or normalized}:{int(network_id)}")
                    if members:
                        raise NetworkServiceError(
                            409,
                            f"Cannot stop sharing {network.get('name')!r} while it is used by "
                            f"another lab ({lab_names(members)}).",
                            extra={"references": _member_summaries(members)},
                        )
                changed = (network.get("shared") is True) != shared
                if changed:
                    network["shared"] = shared
                    data.pop("topology", None)
                    LabService.write_lab_json_static(normalized, data)
                payload = _serialize_network(network, _refcount(data, int(network_id)))

        if changed:
            await ws_hub.publish(normalized, "network_updated", {"network": dict(payload)})
        return payload, changed


network_service = NetworkService()
