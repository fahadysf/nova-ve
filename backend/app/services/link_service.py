# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource link service for v2 lab.json mutations.

Implements US-063 (per-resource link CRUD with idempotent POST + bulk-PUT
layout) and US-064 (implicit-bridge state machine). All mutations acquire the
per-lab flock, persist via :class:`LabService`, recompute the MAC registry,
and publish WebSocket events.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.services.lab_lock import lab_lock
from app.services.lab_service import LabService, _normalize_relative_lab_path
from app.services.link_utils import _endpoint_key, _link_pair_key  # noqa: F401 — re-exported
from app.services.ws_hub import ws_hub


_IDEMPOTENCY_CACHE_MAX = 1024


class DuplicateLinkError(Exception):
    """Raised when a link with the same canonical port pair already exists."""

    def __init__(self, existing_link: dict) -> None:
        super().__init__("link already exists")
        self.existing_link = existing_link


def _refcount(lab_data: dict, network_id: int) -> int:
    """Count link endpoints referencing ``network_id``."""
    return sum(
        1
        for link in lab_data.get("links", []) or []
        for ep in (link.get("from"), link.get("to"))
        if isinstance(ep, dict) and int(ep.get("network_id", 0) or 0) == int(network_id)
    )


def _next_link_id(lab_data: dict) -> str:
    max_n = 0
    for link in lab_data.get("links", []) or []:
        link_id = str(link.get("id", "") or "")
        if link_id.startswith("lnk_"):
            tail = link_id[len("lnk_"):]
            if tail.isdigit():
                max_n = max(max_n, int(tail))
    return f"lnk_{max_n + 1:03d}"


def _next_network_id(lab_data: dict) -> int:
    networks = lab_data.get("networks", {}) or {}
    next_id = 0
    for key in networks.keys():
        try:
            next_id = max(next_id, int(key))
        except (TypeError, ValueError):
            continue
    return next_id + 1


def _normalize_endpoint(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Endpoint must be an object with node_id or network_id")
    if "network_id" in raw:
        return {"network_id": int(raw["network_id"])}
    if "node_id" in raw:
        return {
            "node_id": int(raw["node_id"]),
            "interface_index": int(raw.get("interface_index", 0)),
        }
    raise ValueError("Endpoint must contain either node_id or network_id")


def _build_link(
    *,
    link_id: str,
    endpoint_a: dict,
    endpoint_b: dict,
    style_override: Optional[str] = None,
    label: str = "",
    color: str = "",
    width: str = "1",
    metrics: Optional[dict] = None,
) -> dict:
    return {
        "id": link_id,
        "from": endpoint_a,
        "to": endpoint_b,
        "style_override": style_override,
        "label": label,
        "color": color,
        "width": width,
        "metrics": metrics or {
            "delay_ms": 0,
            "loss_pct": 0,
            "bandwidth_kbps": 0,
            "jitter_ms": 0,
        },
    }


def _build_implicit_network(network_id: int) -> dict:
    return {
        "id": network_id,
        "name": "",
        "type": "linux_bridge",
        "left": 0,
        "top": 0,
        "icon": "01-Cloud-Default.svg",
        "width": 0,
        "style": "Solid",
        "linkstyle": "Straight",
        "color": "",
        "label": "",
        "visibility": False,
        "implicit": True,
        "smart": -1,
        "config": {},
    }


def _serialize_link(link: dict) -> dict:
    return {
        "id": link.get("id"),
        "from": link.get("from"),
        "to": link.get("to"),
        "style_override": link.get("style_override"),
        "label": link.get("label", ""),
        "color": link.get("color", ""),
        "width": link.get("width", "1"),
        "metrics": link.get("metrics", {}),
    }


def _link_with_state(link: dict) -> dict:
    out = _serialize_link(link)
    out["state"] = "configured"
    return out


def _recompute_mac_registry(lab_id: str, lab_data: dict) -> None:
    try:
        from app.services.mac_registry import mac_registry  # noqa: WPS433

        mac_registry.recompute_for_lab(lab_id, lab_data)
    except ImportError:
        # MacRegistry not yet shipped — soft fail per cross-agent contract.
        return


class LinkService:
    """Stateful service handling links + implicit-bridge state machine."""

    def __init__(self) -> None:
        self._idempotency: "OrderedDict[Tuple[str, str], Tuple[str, dict, Optional[dict], List[Tuple[str, dict]]]]" = OrderedDict()

    # ------------------------------------------------------------------
    # Idempotency cache helpers
    # ------------------------------------------------------------------

    def _cache_get(self, lab_path: str, key: Optional[str]):
        if not key:
            return None
        cache_key = (lab_path, key)
        if cache_key in self._idempotency:
            self._idempotency.move_to_end(cache_key)
            return self._idempotency[cache_key]
        return None

    def _cache_set(
        self,
        lab_path: str,
        key: Optional[str],
        link_id: str,
        link_payload: dict,
        network_payload: Optional[dict],
        ws_events: List[Tuple[str, dict]],
    ) -> None:
        if not key:
            return
        cache_key = (lab_path, key)
        self._idempotency[cache_key] = (link_id, link_payload, network_payload, ws_events)
        self._idempotency.move_to_end(cache_key)
        while len(self._idempotency) > _IDEMPOTENCY_CACHE_MAX:
            self._idempotency.popitem(last=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_links(self, lab_path: str) -> List[dict]:
        normalized = _normalize_relative_lab_path(lab_path)
        data = LabService.read_lab_json_static(normalized)
        return [_link_with_state(link) for link in data.get("links", []) or []]

    def refcount_for(self, lab_path: str, network_id: int) -> int:
        normalized = _normalize_relative_lab_path(lab_path)
        data = LabService.read_lab_json_static(normalized)
        return _refcount(data, int(network_id))

    async def create_link(
        self,
        lab_path: str,
        from_endpoint: Any,
        to_endpoint: Any,
        *,
        style_override: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[dict, Optional[dict], bool]:
        """Create a link, applying the implicit-bridge state machine.

        Returns ``(link_payload, network_payload_or_none, replayed)``. When
        ``replayed`` is True the caller should respond 200 (not 201) and skip
        WebSocket publishing.
        """
        normalized = _normalize_relative_lab_path(lab_path)

        cached = self._cache_get(normalized, idempotency_key)
        if cached is not None:
            _link_id, link_payload, network_payload, _ws_events = cached
            return link_payload, network_payload, True

        endpoint_a = _normalize_endpoint(from_endpoint)
        endpoint_b = _normalize_endpoint(to_endpoint)

        labs_dir = get_settings().LABS_DIR
        ws_events: List[Tuple[str, dict]] = []

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
            networks = data.setdefault("networks", {})
            links = data.setdefault("links", [])

            # Duplicate detection (US-102): treat {a,b} and {b,a} as equivalent.
            target_key = _link_pair_key(endpoint_a, endpoint_b)
            for existing in links:
                ex_from = existing.get("from")
                ex_to = existing.get("to")
                if isinstance(ex_from, dict) and isinstance(ex_to, dict):
                    try:
                        ex_key = _link_pair_key(
                            _normalize_endpoint(ex_from),
                            _normalize_endpoint(ex_to),
                        )
                    except (ValueError, TypeError):
                        continue
                    if ex_key == target_key:
                        raise DuplicateLinkError(_link_with_state(existing))

            link_payload: dict
            network_payload: Optional[dict] = None
            promoted_network: Optional[dict] = None

            both_nodes = "node_id" in endpoint_a and "node_id" in endpoint_b

            if both_nodes:
                # Implicit-bridge create branch.
                net_id = _next_network_id(data)
                implicit_net = _build_implicit_network(net_id)
                networks[str(net_id)] = implicit_net

                first_id = _next_link_id(data)
                first_link = _build_link(
                    link_id=first_id,
                    endpoint_a=dict(endpoint_a),
                    endpoint_b={"network_id": net_id},
                    style_override=style_override,
                )
                links.append(first_link)

                second_id = _next_link_id(data)
                second_link = _build_link(
                    link_id=second_id,
                    endpoint_a=dict(endpoint_b),
                    endpoint_b={"network_id": net_id},
                    style_override=style_override,
                )
                links.append(second_link)

                # Strip transient legacy fields before persisting so that
                # write_lab_json_static does not regenerate links[] from the
                # synthesized topology[] shim added by read_lab_json_static.
                data.pop("topology", None)
                LabService.write_lab_json_static(normalized, data)
                _recompute_mac_registry(normalized, data)

                link_payload = _link_with_state(first_link)
                network_payload = dict(implicit_net)
                ws_events.append(("network_created", {"network": dict(implicit_net)}))
                ws_events.append(("link_created", {"link": _link_with_state(first_link)}))
                ws_events.append(("link_created", {"link": _link_with_state(second_link)}))

            else:
                # Mixed node/network or network/network attach.
                target_network_id: Optional[int] = None
                for endpoint in (endpoint_a, endpoint_b):
                    if "network_id" in endpoint:
                        target_network_id = int(endpoint["network_id"])
                        break

                if target_network_id is not None and str(target_network_id) not in networks:
                    raise KeyError(f"network {target_network_id} does not exist")

                new_id = _next_link_id(data)
                new_link = _build_link(
                    link_id=new_id,
                    endpoint_a=dict(endpoint_a),
                    endpoint_b=dict(endpoint_b),
                    style_override=style_override,
                )
                links.append(new_link)

                # Promotion check: implicit network now at refcount==3.
                if target_network_id is not None:
                    network_record = networks.get(str(target_network_id))
                    if (
                        isinstance(network_record, dict)
                        and network_record.get("implicit") is True
                        and _refcount(data, target_network_id) == 3
                    ):
                        network_record["implicit"] = False
                        network_record["visibility"] = True
                        if not network_record.get("name"):
                            network_record["name"] = f"bridge-{target_network_id}"
                        promoted_network = dict(network_record)

                # Strip the synthesized legacy topology shim so the writer
                # does not regenerate links[] from it.
                data.pop("topology", None)
                LabService.write_lab_json_static(normalized, data)
                _recompute_mac_registry(normalized, data)

                link_payload = _link_with_state(new_link)
                if promoted_network is not None:
                    network_payload = promoted_network
                    ws_events.append(("network_promoted", {"network": promoted_network}))
                ws_events.append(("link_created", {"link": _link_with_state(new_link)}))

        for event_type, payload in ws_events:
            await ws_hub.publish(normalized, event_type, payload)

        self._cache_set(
            normalized,
            idempotency_key,
            str(link_payload.get("id", "")),
            link_payload,
            network_payload,
            ws_events,
        )
        return link_payload, network_payload, False

    async def delete_link(self, lab_path: str, link_id: str) -> Tuple[bool, Optional[dict]]:
        """Delete a link. Idempotent: missing link returns ``(True, None)``.

        Returns ``(already_deleted_or_succeeded, deleted_implicit_network)``.
        ``deleted_implicit_network`` is None unless an implicit network's
        refcount dropped to 0 and it was GC'd in the same step.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        ws_events: List[Tuple[str, dict]] = []
        deleted_implicit: Optional[dict] = None
        already_deleted = False

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
            links = data.get("links", []) or []
            networks = data.get("networks", {}) or {}

            target_index = None
            for index, link in enumerate(links):
                if str(link.get("id")) == str(link_id):
                    target_index = index
                    break

            if target_index is None:
                return True, None

            removed_link = links.pop(target_index)
            data["links"] = links

            # GC implicit networks at refcount 0.
            referenced_network_ids: List[int] = []
            for endpoint in (removed_link.get("from"), removed_link.get("to")):
                if isinstance(endpoint, dict) and "network_id" in endpoint:
                    referenced_network_ids.append(int(endpoint["network_id"]))

            for net_id in referenced_network_ids:
                network_record = networks.get(str(net_id))
                if not isinstance(network_record, dict):
                    continue
                if network_record.get("implicit") is True and _refcount(data, net_id) == 0:
                    networks.pop(str(net_id), None)
                    deleted_implicit = dict(network_record)
                    ws_events.append(("network_deleted", {"network": dict(network_record)}))

            data["networks"] = networks
            data.pop("topology", None)
            LabService.write_lab_json_static(normalized, data)
            _recompute_mac_registry(normalized, data)

            ws_events.append(("link_deleted", {"link_id": str(link_id)}))

        for event_type, payload in ws_events:
            await ws_hub.publish(normalized, event_type, payload)

        return already_deleted, deleted_implicit

    async def patch_link(self, lab_path: str, link_id: str, patch: Dict[str, Any]) -> Optional[dict]:
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        with lab_lock(normalized, labs_dir):
            data = LabService.read_lab_json_static(normalized)
            links = data.get("links", []) or []

            target = None
            for link in links:
                if str(link.get("id")) == str(link_id):
                    target = link
                    break
            if target is None:
                return None

            allowed = {"style_override", "label", "color", "width", "metrics"}
            for field, value in patch.items():
                if field not in allowed:
                    continue
                target[field] = value

            data["links"] = links
            data.pop("topology", None)
            LabService.write_lab_json_static(normalized, data)
            _recompute_mac_registry(normalized, data)
            updated_payload = _link_with_state(target)

        await ws_hub.publish(normalized, "link_updated", {"link": updated_payload})
        return updated_payload


# Module-level singleton for routers and tests to share idempotency state.
link_service = LinkService()
