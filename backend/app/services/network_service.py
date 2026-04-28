# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource network service for v2 lab.json mutations.

US-063 + US-064 — JSON-side mutations under the per-lab flock.
US-202 — ``create_network`` / ``delete_network`` provision and tear down
the matching Linux bridge via the privileged helper, persisting
``runtime.bridge_name`` on the network record.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.services import host_net
from app.services.lab_lock import lab_lock
from app.services.lab_service import LabService, _normalize_relative_lab_path
from app.services.link_service import _refcount, _recompute_mac_registry
from app.services.ws_hub import ws_hub

logger = logging.getLogger("nova-ve")


class NetworkServiceError(Exception):
    """Generic exception for network-service contract violations."""

    def __init__(self, code: int, message: str, *, extra: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


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

    async def create_network(self, lab_path: str, request: dict) -> dict:
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

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
                "config": {},
                "runtime": {"bridge_name": bridge},
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
