# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource network service for v2 lab.json mutations.

US-063 + US-064. All mutations acquire the per-lab flock, persist via
:class:`LabService`, recompute the MAC registry, and publish WS events.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.services.lab_lock import lab_lock
from app.services.lab_service import LabService, _normalize_relative_lab_path
from app.services.link_service import _refcount, _recompute_mac_registry
from app.services.ws_hub import ws_hub


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
            networks = data.setdefault("networks", {})
            next_id = max(
                (int(key) for key in networks.keys() if str(key).isdigit()),
                default=0,
            ) + 1
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
            }
            networks[str(next_id)] = network
            data.pop("topology", None)
            LabService.write_lab_json_static(normalized, data)
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
