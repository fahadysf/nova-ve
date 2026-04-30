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

import ipaddress
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.services import host_net
from app.services.lab_lock import lab_lock
from app.services.lab_service import LabService, _normalize_relative_lab_path
from app.services.link_service import _refcount, _recompute_mac_registry
from app.services.ws_hub import ws_hub

logger = logging.getLogger("nova-ve")


# US-204c: free-list IPAM defaults. ``first_offset=2`` skips .0 (network)
# and .1 (conventional gateway reservation). The broadcast address is
# excluded explicitly inside ``_allocate_ip``.
DEFAULT_FIRST_OFFSET = 2


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
        nic_link_state: List[Dict[str, Any]] = []

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

                try:
                    if host_net.bridge_exists(bridge):
                        status = host_net.bridge_fingerprint_check(
                            bridge, lab_id, network_id
                        )
                        if status == "mismatch":
                            skipped.append({
                                "bridge": bridge,
                                "network_id": network_id,
                                "reason": _bridge_ownership_message(
                                    bridge, lab_id, network_id
                                ),
                            })
                            continue
                        if status == "absent":
                            try:
                                host_net.bridge_fingerprint_write(
                                    bridge, lab_id, network_id
                                )
                            except host_net.HostNetError:
                                pass
                        ensured.append(bridge)
                    else:
                        host_net.bridge_add(bridge)
                        try:
                            host_net.bridge_fingerprint_write(
                                bridge, lab_id, network_id
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

            if dirty:
                LabService.write_lab_json_static(normalized, data)

            nic_link_state = _reconcile_qemu_nic_link_state(lab_id, data)

        return {
            "ensured": ensured,
            "created": created,
            "skipped": skipped,
            "raised": raised,
            "nic_link_state": nic_link_state,
        }

    async def create_network(self, lab_path: str, request: dict) -> dict:
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        # US-204c: validate CIDR (if any) before touching the lab lock so
        # invalid input cannot create partial state. ``config`` is the
        # user-supplied shape from ``NetworkCreate``/``NetworkConfig``.
        raw_config = request.get("config") or {}
        if not isinstance(raw_config, dict):
            raw_config = {}
        cidr_value = raw_config.get("cidr")
        if cidr_value:
            _validate_cidr(cidr_value)

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
            lab_id = str(data.get("id") or normalized)
            networks = data.setdefault("networks", {})
            next_id = max(
                (int(key) for key in networks.keys() if str(key).isdigit()),
                default=0,
            ) + 1
            bridge = host_net.bridge_name(lab_id, next_id)
            network = {
                "id": next_id,
                "name": request.get("name", "Net"),
                "type": request.get("type", "linux_bridge"),
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
                "config": dict(raw_config),
                "runtime": {
                    "bridge_name": bridge,
                    # US-401: provisioning-backend metadata for the
                    # reconciliation loop (US-402). ``driver`` mirrors
                    # the network's ``type`` vocabulary; ``created_at``
                    # is an ISO-8601 UTC timestamp.
                    "driver": "linux_bridge",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    # US-204c: seed an empty free-list. Counter-based
                    # allocation would exhaust a /24 in 250 cycles even
                    # with one container; the free-list does not.
                    "used_ips": [],
                    "first_offset": DEFAULT_FIRST_OFFSET,
                },
            }
            networks[str(next_id)] = network
            data.pop("topology", None)
            # Persist BEFORE provisioning so on-disk state never leads the
            # kernel state. Any provisioning or ownership failure rolls it back.
            LabService.write_lab_json_static(normalized, data)
            try:
                if host_net.bridge_exists(bridge):
                    status = host_net.bridge_fingerprint_check(bridge, lab_id, next_id)
                    if status != "match":
                        raise host_net.HostNetBridgeOwnershipError(
                            _bridge_ownership_message(bridge, lab_id, next_id)
                        )
                else:
                    host_net.bridge_add(bridge)
                    try:
                        host_net.bridge_fingerprint_write(bridge, lab_id, next_id)
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
            except host_net.HostNetBridgeOwnershipError:
                networks.pop(str(next_id), None)
                data["networks"] = networks
                LabService.write_lab_json_static(normalized, data)
                raise
            except (host_net.HostNetError, OSError) as exc:
                # Roll back the JSON write — never leave inconsistent state.
                networks.pop(str(next_id), None)
                data["networks"] = networks
                LabService.write_lab_json_static(normalized, data)
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
            _recompute_mac_registry(normalized, data)
            payload = _serialize_network(network, 0)

        await ws_hub.publish(normalized, "network_created", {"network": dict(payload)})
        return payload

    async def delete_network(self, lab_path: str, network_id: int) -> dict:
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

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
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
            networks = data.get("networks", {}) or {}
            network = networks.get(str(network_id))
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

            chosen: Optional[ipaddress.IPv4Address] = None
            for candidate_int in range(start_int, end_int + 1):
                candidate = ipaddress.IPv4Address(candidate_int)
                if candidate in used:
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
            networks[str(network_id)] = network
            data["networks"] = networks
            data.pop("topology", None)
            LabService.write_lab_json_static(normalized, data)

        return str(chosen)

    def _release_ip(self, lab_path: str, network_id: int, ip: str) -> bool:
        """Remove ``ip`` from ``runtime.used_ips``; return True if removed.

        No-op (returns False) when the network or IP is absent —
        idempotent so detach paths can call this without a pre-check.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
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
            LabService.write_lab_json_static(normalized, data)
            return True

    async def patch_network(
        self,
        lab_path: str,
        network_id: int,
        patch: Dict[str, Any],
    ) -> Tuple[dict, str]:
        """Patch a network record. Returns (payload, event_type).

        ``event_type`` is ``network_promoted`` when the patch turned an
        implicit network into an explicit one, otherwise ``network_updated``.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
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
                    new_name = str(new_name)
                    if new_name and was_implicit:
                        network["name"] = new_name
                        network["implicit"] = False
                        network["visibility"] = True
                        promoted = True
                    else:
                        network["name"] = new_name

            for field in ("type", "left", "top", "icon", "visibility", "config"):
                if field in patch and patch[field] is not None:
                    network[field] = patch[field]

            networks[str(network_id)] = network
            data.pop("topology", None)
            LabService.write_lab_json_static(normalized, data)
            _recompute_mac_registry(normalized, data)
            count = _refcount(data, int(network_id))
            payload = _serialize_network(network, count)
            event_type = "network_promoted" if promoted else "network_updated"

        await ws_hub.publish(normalized, event_type, {"network": dict(payload)})
        return payload, event_type


network_service = NetworkService()
