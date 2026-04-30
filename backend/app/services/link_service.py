# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource link service for v2 lab.json mutations.

Implements US-063 (per-resource link CRUD with idempotent POST + bulk-PUT
layout) and US-064 (implicit-bridge state machine). All mutations acquire the
per-lab flock, persist via :class:`LabService`, recompute the MAC registry,
and publish WebSocket events.
"""

from __future__ import annotations

import copy
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from contextlib import AsyncExitStack

from app.config import get_settings
from app.services import host_net
from app.services.lab_lock import lab_lock
from app.services.lab_service import LabService, _normalize_relative_lab_path
from app.services.link_utils import _endpoint_key, _link_pair_key  # noqa: F401 — re-exported
from app.services.runtime_mutex import RuntimeMutexContention, runtime_mutex
from app.services.ws_hub import ws_hub


_logger = logging.getLogger("nova-ve.link_service")


_IDEMPOTENCY_CACHE_MAX = 1024


class DuplicateLinkError(Exception):
    """Raised when a link with the same canonical port pair already exists."""

    def __init__(self, existing_link: dict) -> None:
        super().__init__("link already exists")
        self.existing_link = existing_link


class LinkContentionError(Exception):
    """US-303 codex iter1 MEDIUM: raised when the per-(lab, node, iface)
    runtime mutex cannot be acquired within the bounded contention
    window (default 2.0s). The router translates this to HTTP 409.
    """

    def __init__(self, contention: RuntimeMutexContention) -> None:
        self.contention = contention
        super().__init__(str(contention))


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
        # US-204b: generation token stamped at hot-attach time.
        "runtime": link.get("runtime", {"attach_generation": 0}),
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


def _runtime_mutex_lab_id(lab_data: Optional[dict], normalized: str) -> str:
    """Return the canonical mutex key for ``(lab, node, iface)`` ordering.

    Codex critic v2 HIGH #1: ``create_link`` and ``delete_link`` MUST agree on
    the same mutex key for any given lab — otherwise concurrent attach +
    detach on the same interface would acquire DIFFERENT mutex instances and
    race. We resolve to ``str(lab_data["id"] or normalized)`` to mirror the
    runtime service's own keying (``NodeRuntimeService._runtime_record`` keys
    by ``lab_id`` which is the JSON ``id`` field — see
    ``_attach_docker_interface_locked``'s
    ``runtime_mutex.is_held(lab_id, ...)`` defensive assert). When ``lab_data``
    is None (e.g. before the lab.json has been read) we fall back to the
    path-derived ``normalized`` key — concurrent callers in this process all
    see the same path so the mutex still serializes correctly.
    """
    if isinstance(lab_data, dict):
        lab_id = lab_data.get("id")
        if lab_id is not None and lab_id != "":
            return str(lab_id)
    return str(normalized)


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

        # US-204b: acquire the per-(lab, node, iface) runtime mutex BEFORE
        # entering lab_lock so the hot-attach kernel sequence is serialized
        # against any concurrent create_link / delete_link on the same
        # interface. Unrelated (node, iface) pairs acquire distinct mutexes
        # and never block each other. We acquire one mutex per node-side
        # endpoint (network endpoints don't have a per-iface mutex) and
        # rely on a deterministic order to avoid deadlocks when both
        # endpoints are nodes.
        node_keys: List[Tuple[int, int]] = []
        for endpoint in (endpoint_a, endpoint_b):
            if "node_id" in endpoint:
                node_keys.append(
                    (int(endpoint["node_id"]), int(endpoint.get("interface_index", 0)))
                )
        # Sort so concurrent calls always acquire the locks in the same
        # global order (deadlock prevention).
        node_keys.sort()

        # Codex critic v2 HIGH #1: resolve the runtime mutex key the SAME
        # way ``delete_link`` does so concurrent attach + detach on the same
        # ``(node, iface)`` always serialize on the same mutex instance.
        # Reading lab.json outside lab_lock is safe because the mutex key
        # is process-local (``RuntimeMutexRegistry``) and the JSON ``id``
        # field is immutable for the lifetime of a lab. ``probe`` is None
        # when the lab does not yet exist on disk; the helper falls back to
        # ``normalized``.
        try:
            probe = LabService.read_lab_json_static(normalized)
        except Exception:  # pragma: no cover — lab.json may not exist yet
            probe = None
        mutex_lab_id = _runtime_mutex_lab_id(probe, normalized)

        try:
            async with AsyncExitStack() as stack:
                for node_id, interface_index in node_keys:
                    await stack.enter_async_context(
                        runtime_mutex.acquire(mutex_lab_id, node_id, interface_index)
                    )
                return await self._create_link_locked(
                    normalized=normalized,
                    labs_dir=labs_dir,
                    endpoint_a=endpoint_a,
                    endpoint_b=endpoint_b,
                    style_override=style_override,
                    idempotency_key=idempotency_key,
                    ws_events=ws_events,
                    mutex_lab_id=mutex_lab_id,
                )
        except RuntimeMutexContention as exc:
            # US-303 codex iter1 MEDIUM: bounded-wait timeout → 409.
            raise LinkContentionError(exc) from exc

    async def _create_link_locked(
        self,
        *,
        normalized: str,
        labs_dir,
        endpoint_a: dict,
        endpoint_b: dict,
        style_override: Optional[str],
        idempotency_key: Optional[str],
        ws_events: List[Tuple[str, dict]],
        mutex_lab_id: str,
    ) -> Tuple[dict, Optional[dict], bool]:
        """Per-iface mutex(es) ARE held by the caller (US-204b). This
        helper does the lab_lock-bounded mutation + hot-attach work.
        """
        link_payload: dict
        network_payload: Optional[dict] = None

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

            # Snapshot pre-write state so we can roll back lab.json if
            # post-write hot-attach (US-204) fails. ``read_lab_json_static``
            # synthesises ``topology`` from links[]; we strip that derived
            # field before deep-copying so the rollback write does not
            # regenerate links[] from a stale shim.
            original_snapshot = copy.deepcopy(data)
            original_snapshot.pop("topology", None)
            # ``implicit_bridge_to_provision`` is the (network_id, bridge_name|None)
            # for an implicit network synthesised by this call. ``bridge_name``
            # is None at this stage; the helper resolves it lazily so labs
            # without a per-host instance_id (typical for unit tests that
            # never start a node) do not pre-emptively blow up here.
            implicit_bridge_to_provision: Optional[Tuple[int, Optional[str]]] = None
            concrete_links_to_attach: List[dict] = []

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

                # US-204: each concrete node↔network link is processed
                # independently for hot-attach. The implicit network's
                # bridge must be provisioned exactly once before either
                # attach call (Codex critic v4 new defect #3). The bridge
                # name is resolved lazily in ``_hot_attach_running_endpoints``
                # so labs without a per-host ``instance_id`` (e.g. unit
                # tests that never start a node) do not blow up on the
                # ``host_net.bridge_name`` call.
                implicit_bridge_to_provision = (int(net_id), None)
                concrete_links_to_attach.append(first_link)
                concrete_links_to_attach.append(second_link)

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

                # US-204: hot-attach single concrete link (only when the
                # node-ish endpoint is running). bridge_name resolution
                # uses the existing network's runtime.bridge_name when
                # present (US-202 / US-202b), falling back to the canonical
                # derived name.
                concrete_links_to_attach.append(new_link)

            # ----------------------------------------------------------
            # US-204 hot-attach pass — runs INSIDE the lab_lock so we can
            # roll back lab.json atomically on host-side failure.
            # ----------------------------------------------------------
            try:
                stamped = self._hot_attach_running_endpoints(
                    lab_path=normalized,
                    lab_data=data,
                    implicit_bridge=implicit_bridge_to_provision,
                    concrete_links=concrete_links_to_attach,
                )
            except Exception as exc:
                # Roll back lab.json — JSON file leads kernel state contract.
                LabService.write_lab_json_static(normalized, original_snapshot)
                _recompute_mac_registry(normalized, original_snapshot)
                _logger.error(
                    "create_link: hot-attach failed (%s); rolled back lab.json",
                    exc,
                )
                raise

            # US-204b: persist the generation stamps written into ``data``
            # by the hot-attach pass. We re-write lab.json inside the same
            # lab_lock so the link's ``runtime.attach_generation`` and the
            # node interface's ``runtime.current_attach_generation`` land
            # atomically with the rest of this create_link mutation.
            if stamped:
                data.pop("topology", None)
                LabService.write_lab_json_static(normalized, data)
                # ``link_payload`` was built before the generation was
                # known; refresh it from the now-stamped link record so
                # the API response carries the canonical value.
                link_id = str(link_payload.get("id", "") or "")
                for link in data.get("links", []) or []:
                    if str(link.get("id", "")) == link_id:
                        link_payload = _link_with_state(link)
                        break

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

    def _hot_attach_running_endpoints(
        self,
        *,
        lab_path: str,
        lab_data: dict,
        implicit_bridge: Optional[Tuple[int, Optional[str]]],
        concrete_links: List[dict],
    ) -> bool:
        """US-204: hot-attach Docker for every concrete node↔network link
        whose node endpoint is currently running.

        Symmetric with US-203's initial-attach path: both invoke the same
        per-iface attach helper (``_attach_docker_interface_initial``) so
        host-side iface naming is identical between first-NIC and Nth-NIC
        attachments — no special-case for the first NIC.

        ``implicit_bridge`` is set when ``create_link`` synthesises an
        implicit network from a node↔node request. The bridge for that
        network is provisioned exactly once before any hot-attach call (the
        plan's "implicit network's bridge is created via ``host_net.bridge_add``
        exactly once, before either attach call" requirement).

        Returns ``True`` if at least one link was hot-attached + stamped
        with a generation token, ``False`` otherwise (no running endpoints
        to mutate). The caller uses this to decide whether to re-persist
        lab.json with the generation stamps.

        On any failure, raises and the caller rolls back lab.json.
        """
        # Local import to avoid a module-load cycle (node_runtime_service →
        # link_service is fine, but link_service → node_runtime_service at
        # import time would create one if node_runtime_service ever imports
        # link_service back).
        from app.services.node_runtime_service import (  # noqa: WPS433 — local import
            NodeRuntimeError,
            NodeRuntimeQMPTimeout,
            NodeRuntimeService,
        )

        # Filter to links that need a hot-attach: node↔network endpoint pairs
        # where the node is currently running on this host. Network↔network
        # links and stopped nodes are no-ops at the runtime layer.
        runtime_service = NodeRuntimeService()
        lab_id = str(lab_data.get("id") or lab_path)
        nodes = lab_data.get("nodes") or {}

        # Pre-pass: identify candidate (node_id, interface_index, network_id, kind)
        # tuples whose node is currently running. We defer bridge_name
        # resolution (which requires a per-host instance_id) until we know
        # there is at least one candidate — labs that never start nodes
        # never trigger the instance_id read.
        pending: List[Tuple[int, int, int, str]] = []
        for link in concrete_links:
            node_endpoint = None
            network_endpoint = None
            for endpoint in (link.get("from"), link.get("to")):
                if not isinstance(endpoint, dict):
                    continue
                if "network_id" in endpoint:
                    network_endpoint = endpoint
                elif "node_id" in endpoint:
                    node_endpoint = endpoint
            if node_endpoint is None or network_endpoint is None:
                continue

            try:
                node_id = int(node_endpoint["node_id"])
                interface_index = int(node_endpoint.get("interface_index", 0))
                network_id = int(network_endpoint["network_id"])
            except (TypeError, ValueError):
                continue

            # Hot-attach is opt-in by liveness: skip nodes that are not
            # running (their initial attach will run when the user starts
            # the node). The runtime registry is the single source of truth.
            runtime = runtime_service._runtime_record(lab_id, node_id)
            if runtime is None:
                continue
            kind = str(runtime.get("kind") or "")
            if kind not in ("docker", "qemu"):
                # Other runtime kinds (iol, dynamips, vpcs) have no hot-add
                # path yet — initial-attach at start time covers them.
                continue

            pending.append((node_id, interface_index, network_id, kind))

        if not pending:
            return False

        # Resolve the implicit network's bridge name lazily — only now do we
        # know there is at least one running endpoint that needs the kernel
        # object.
        resolved_implicit_bridge: Optional[Tuple[int, str]] = None
        if implicit_bridge is not None:
            implicit_net_id, implicit_name = implicit_bridge
            if implicit_name is None:
                implicit_name = host_net.bridge_name(lab_id, int(implicit_net_id))
            resolved_implicit_bridge = (int(implicit_net_id), implicit_name)

        # Now resolve a bridge name for every pending target.
        attach_targets: List[Tuple[int, int, int, str, str]] = []
        for node_id, interface_index, network_id, kind in pending:
            bridge = self._resolve_bridge_name(
                lab_id=lab_id,
                network_id=network_id,
                networks=lab_data.get("networks") or {},
                implicit_bridge=resolved_implicit_bridge,
            )
            attach_targets.append(
                (node_id, interface_index, network_id, bridge, kind)
            )

        # Provision missing host bridges before any attach call.
        # 1. Implicit network's bridge (Codex critic v4 new defect #3) — created
        #    exactly once per ``create_link`` call.
        # 2. Self-heal for explicit networks: labs loaded from static JSON or
        #    labs whose bridges were lost (host reboot, manual cleanup) never
        #    went through ``create_network`` so their bridges are absent. Mirror
        #    the implicit-bridge auto-provision so a link create succeeds
        #    end-to-end. The lab-open ``ensure_lab_bridges`` reconciliation is
        #    the primary path; this is the belt-and-suspenders for any lab
        #    where reconciliation hasn't run yet (e.g. direct API call).
        # Idempotent: pre-existing bridges are no-ops. We track every bridge
        # *we* provisioned so a downstream attach failure rolls them back.
        provisioned_bridges: List[str] = []
        implicit_net_id_value: Optional[int] = None
        if resolved_implicit_bridge is not None:
            implicit_net_id_value, bridge_name = resolved_implicit_bridge
            if any(target[2] == implicit_net_id_value for target in attach_targets):
                if not host_net.bridge_exists(bridge_name):
                    host_net.bridge_add(bridge_name)
                    provisioned_bridges.append(bridge_name)
        seen_explicit_bridges: set = set()
        for _node_id, _iface, network_id, bridge, _kind in attach_targets:
            if network_id == implicit_net_id_value:
                continue  # already handled above
            if bridge in seen_explicit_bridges:
                continue
            seen_explicit_bridges.add(bridge)
            if not host_net.bridge_exists(bridge):
                host_net.bridge_add(bridge)
                provisioned_bridges.append(bridge)
                # Stamp ownership so future reconciliations recognise it as ours.
                try:
                    host_net.bridge_fingerprint_write(bridge, lab_id, network_id)
                except host_net.HostNetError:
                    pass

        # Attach each target. On the FIRST failure, sweep the bridges + every
        # already-attached interface so we leave no host-side leftover.
        # ``attached`` carries the kind so rollback can pick the matching
        # cleanup path (veth host-end for docker, TAP for qemu).
        attached: List[Tuple[int, int, str]] = []
        bridges_to_raise: List[str] = []
        try:
            for node_id, interface_index, network_id, bridge, kind in attach_targets:
                # US-204b: the per-(lab, node, iface) mutex is held by the
                # caller (``create_link``). Call the PRIVATE locked helper
                # directly to avoid double-acquiring the same mutex.
                if kind == "docker":
                    attachment = runtime_service._attach_docker_interface_locked(
                        lab_id,
                        node_id,
                        network_id,
                        interface_index,
                        bridge_name=bridge,
                    )
                else:
                    # US-303: QEMU hot-add via QMP.
                    attachment = runtime_service._attach_qemu_interface_locked(
                        lab_id,
                        node_id,
                        network_id,
                        interface_index,
                        bridge_name=bridge,
                    )
                attached.append((node_id, interface_index, kind))
                if bridge not in bridges_to_raise:
                    bridges_to_raise.append(bridge)

                # US-204b: stamp the link's ``runtime.attach_generation``
                # under lab_lock — atomic with the lab.json write so the
                # link record's generation is never out of sync with the
                # node interface's ``current_attach_generation`` written
                # by the runtime service.
                attach_generation = int(attachment.get("attach_generation", 0))
                self._stamp_link_attach_generation(
                    lab_data=lab_data,
                    node_id=node_id,
                    interface_index=interface_index,
                    network_id=network_id,
                    attach_generation=attach_generation,
                )

                if kind == "qemu":
                    # Best-effort: a hot-added QEMU NIC starts at link=on
                    # by default, but if the boot-time NIC at this index
                    # was created with ``link=off`` (the unconnected
                    # branch in the boot path), set_link forces the
                    # guest-visible carrier to match the lab JSON. QMP
                    # errors here are non-fatal — the attach itself
                    # already succeeded and the next reconcile will
                    # correct any drift.
                    runtime_service.set_qemu_nic_link(
                        lab_id, node_id, interface_index, up=True
                    )
        except (NodeRuntimeError, NodeRuntimeQMPTimeout, host_net.HostNetError):
            # US-303 codex iter1 HIGH-1: NodeRuntimeQMPTimeout is a
            # subclass of NodeRuntimeError so the listing is technically
            # redundant, but we list it explicitly so multi-endpoint
            # rollback intent is grep-able. A raw transport timeout on
            # endpoint B MUST roll back endpoint A's successful attach.
            for node_id, interface_index, kind in attached:
                if kind == "docker":
                    host_end = host_net.veth_host_name(lab_id, node_id, interface_index)
                    host_net.try_link_del(host_end)
                    # Best-effort: drop the rolled-back attachment from the
                    # runtime record so subsequent reads stay consistent.
                    runtime = runtime_service._runtime_record(lab_id, node_id)
                    if runtime is not None:
                        attachments = [
                            a for a in (runtime.get("interface_attachments") or [])
                            if int(a.get("interface_index", -1)) != interface_index
                        ]
                        runtime["interface_attachments"] = attachments
                        host_ends = [
                            h for h in (runtime.get("veth_host_ends") or [])
                            if h != host_end
                        ]
                        runtime["veth_host_ends"] = host_ends
                        runtime_service._persist_runtime(runtime)
                elif kind == "qemu":
                    # US-303 inverse: attach_qemu_interface ran the full
                    # 6-step rollback for the FAILING target itself; for
                    # earlier successful targets we tear down the QMP
                    # device + netdev + TAP here. Best-effort throughout.
                    runtime = runtime_service._runtime_record(lab_id, node_id)
                    if runtime is None:
                        continue
                    socket_path = runtime.get("qmp_socket") or ""
                    netdev_id = f"net{interface_index}"
                    device_id = f"dev{interface_index}"
                    tap = host_net.tap_name(lab_id, node_id, interface_index)
                    if socket_path:
                        try:
                            runtime_service._qmp_command(
                                socket_path, "device_del", {"id": device_id}
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            runtime_service._qmp_command(
                                socket_path, "netdev_del", {"id": netdev_id}
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        host_net.link_set_nomaster(tap)
                    except Exception:  # noqa: BLE001
                        pass
                    host_net.try_link_del(tap)
                    attachments = [
                        a for a in (runtime.get("interface_attachments") or [])
                        if int(a.get("interface_index", -1)) != interface_index
                    ]
                    runtime["interface_attachments"] = attachments
                    tap_names = [
                        t for t in (runtime.get("tap_names") or []) if t != tap
                    ]
                    runtime["tap_names"] = tap_names
                    runtime_service._persist_runtime(runtime)
            for bridge_name in provisioned_bridges:
                try:
                    host_net.bridge_del(bridge_name)
                except host_net.HostNetError:
                    pass
            raise

        # Force every bridge that just had a port attached to UP. Linux
        # bridges with admin-down state hold their slave ports in
        # ``state disabled`` so packets do not forward — even though the
        # slaves themselves are up. Best-effort: failures here only mean
        # we will try again on the next link create or reconcile pass.
        for bridge_name in bridges_to_raise:
            try:
                host_net.link_up(bridge_name)
            except host_net.HostNetError as exc:
                _logger.warning(
                    "create_link: link_up(%s) failed (%s); will retry "
                    "on next attach or reconcile",
                    bridge_name,
                    exc,
                )

        return bool(attached)

    @staticmethod
    def _stamp_link_attach_generation(
        *,
        lab_data: dict,
        node_id: int,
        interface_index: int,
        network_id: int,
        attach_generation: int,
    ) -> None:
        """US-204b: stamp ``Link.runtime.attach_generation`` on the link
        whose endpoints match ``(node_id, interface_index, network_id)``
        and bump
        ``node.interfaces[interface_index].runtime.current_attach_generation``
        on the matching node. Both writes happen under ``lab_lock`` and
        the lab.json is re-persisted by the caller.
        """
        for link in lab_data.get("links", []) or []:
            endpoints = (link.get("from"), link.get("to"))
            node_match = False
            network_match = False
            for endpoint in endpoints:
                if not isinstance(endpoint, dict):
                    continue
                if (
                    "node_id" in endpoint
                    and int(endpoint.get("node_id", -1)) == int(node_id)
                    and int(endpoint.get("interface_index", -1)) == int(interface_index)
                ):
                    node_match = True
                if (
                    "network_id" in endpoint
                    and int(endpoint.get("network_id", -1)) == int(network_id)
                ):
                    network_match = True
            if node_match and network_match:
                runtime_record = link.setdefault("runtime", {})
                runtime_record["attach_generation"] = int(attach_generation)
                break

        nodes = lab_data.get("nodes") or {}
        node_record = nodes.get(str(node_id))
        if isinstance(node_record, dict):
            interfaces = node_record.get("interfaces") or []
            for iface in interfaces:
                if not isinstance(iface, dict):
                    continue
                if int(iface.get("index", -1)) == int(interface_index):
                    iface_runtime = iface.setdefault("runtime", {})
                    iface_runtime["current_attach_generation"] = int(attach_generation)
                    break

    @staticmethod
    def _resolve_bridge_name(
        *,
        lab_id: str,
        network_id: int,
        networks: dict,
        implicit_bridge: Optional[Tuple[int, str]],
    ) -> str:
        """Return the host bridge name for a network referenced by a link.

        Resolution order:
          1. Implicit bridge tuple if it matches this network_id (newly
             synthesised network from a node↔node create_link call).
          2. ``networks[str(network_id)].runtime.bridge_name`` if present
             (US-202 / US-202b populated this on create / migrate).
          3. Canonical derived name via ``host_net.bridge_name``.
        """
        if implicit_bridge is not None and implicit_bridge[0] == network_id:
            return implicit_bridge[1]
        record = networks.get(str(network_id))
        if isinstance(record, dict):
            runtime_record = record.get("runtime") or {}
            bridge = runtime_record.get("bridge_name")
            if isinstance(bridge, str) and bridge:
                return bridge
        return host_net.bridge_name(lab_id, network_id)

    async def delete_link(self, lab_path: str, link_id: str) -> Tuple[bool, Optional[dict]]:
        """Delete a link. Idempotent: missing link returns ``(True, None)``.

        Returns ``(already_deleted_or_succeeded, deleted_implicit_network)``.
        ``deleted_implicit_network`` is None unless an implicit network's
        refcount dropped to 0 and it was GC'd in the same step.

        US-205 / US-204b: mirror of ``create_link`` for hot-detach. We
        acquire the per-``(lab, node, iface)`` runtime mutex BEFORE entering
        ``lab_lock`` for every node-side endpoint of the link being deleted,
        then call :meth:`NodeRuntimeService._detach_docker_interface_locked`
        with the link's ``runtime.attach_generation`` as
        ``expected_generation`` so a stale rollback never tears down a fresh
        re-attach. The kernel-side veth removal runs OUTSIDE the lab_lock.
        """
        normalized = _normalize_relative_lab_path(lab_path)
        labs_dir = get_settings().LABS_DIR

        # US-205 / US-204b: probe for the link's node-side endpoints before
        # we take any lock so we know which mutex keys to acquire.  Reading
        # outside lab_lock is safe — the actual mutation happens under
        # lab_lock below.  Worst case: we acquire one extra mutex if the
        # link disappears between probe and lock.
        probe = LabService.read_lab_json_static(normalized)
        target_link_probe: Optional[dict] = None
        for lnk in (probe.get("links", []) or []):
            if str(lnk.get("id")) == str(link_id):
                target_link_probe = lnk
                break
        if target_link_probe is None:
            return True, None

        node_keys: List[Tuple[int, int]] = []
        for ep in (target_link_probe.get("from"), target_link_probe.get("to")):
            if isinstance(ep, dict) and "node_id" in ep:
                try:
                    node_keys.append(
                        (int(ep["node_id"]), int(ep.get("interface_index", 0)))
                    )
                except (TypeError, ValueError):
                    continue
        # Deterministic acquisition order prevents deadlocks (symmetric
        # with create_link).
        node_keys.sort()

        # Codex critic v2 HIGH #1: unified mutex key — must match the helper
        # used by ``create_link`` so concurrent attach + detach on the same
        # ``(node, iface)`` always observe the same mutex instance.
        mutex_lab_id = _runtime_mutex_lab_id(probe, normalized)

        try:
            async with AsyncExitStack() as stack:
                for node_id, interface_index in node_keys:
                    await stack.enter_async_context(
                        runtime_mutex.acquire(mutex_lab_id, node_id, interface_index)
                    )
                return await self._delete_link_locked(
                    normalized=normalized,
                    labs_dir=labs_dir,
                    link_id=link_id,
                    mutex_lab_id=mutex_lab_id,
                )
        except RuntimeMutexContention as exc:
            # US-303 codex iter1 MEDIUM: bounded-wait timeout → 409.
            raise LinkContentionError(exc) from exc

    async def _delete_link_locked(
        self,
        *,
        normalized: str,
        labs_dir,
        link_id: str,
        mutex_lab_id: str,
    ) -> Tuple[bool, Optional[dict]]:
        """Per-iface mutex(es) ARE held by the caller (US-205 / US-204b).

        Codex critic v2 HIGH #2 ordering: kernel-side detach runs FIRST
        (outside ``lab_lock``, inside the per-iface mutex). Only on
        successful detach do we (a) release the IP via
        ``network_service._release_ip``, (b) remove the link from
        ``lab.json``, (c) clear the runtime attachment record (the latter
        is performed inside ``_detach_docker_interface_locked`` itself on
        success). On detach failure we re-raise: lab.json + IPAM
        free-list + runtime attachments all stay intact so a retry is
        meaningful.

        Idempotency:
          * Missing link in lab.json -> ``(True, None)`` (no-op).
          * ``HostNetEINVAL`` from the helper is treated as success inside
            ``_detach_docker_interface_locked`` (host-end already gone,
            cleanup still proceeds).
          * Stopped node (no runtime record) -> skip detach, proceed with
            JSON cleanup.
        """
        ws_events: List[Tuple[str, dict]] = []
        deleted_implicit: Optional[dict] = None

        # ------------------------------------------------------------------
        # Phase 1 — probe lab.json (no mutation) to learn what to detach.
        # ------------------------------------------------------------------
        probe = LabService.read_lab_json_static(normalized)
        probe_links = probe.get("links", []) or []
        target_link: Optional[dict] = None
        for link in probe_links:
            if str(link.get("id")) == str(link_id):
                target_link = link
                break
        if target_link is None:
            # Idempotent: link already removed (or never existed).
            return True, None

        removed_runtime = target_link.get("runtime") or {}
        attach_generation = int(removed_runtime.get("attach_generation", 0))
        removed_ip: Optional[str] = removed_runtime.get("ip")  # type: ignore[assignment]

        node_eps: List[Tuple[int, int]] = []
        network_ep_ids: List[int] = []
        for ep in (target_link.get("from"), target_link.get("to")):
            if not isinstance(ep, dict):
                continue
            if "node_id" in ep:
                try:
                    node_eps.append(
                        (int(ep["node_id"]), int(ep.get("interface_index", 0)))
                    )
                except (TypeError, ValueError):
                    continue
            elif "network_id" in ep:
                try:
                    network_ep_ids.append(int(ep["network_id"]))
                except (TypeError, ValueError):
                    continue

        paired_net_id: Optional[int] = (
            network_ep_ids[0] if (node_eps and network_ep_ids) else None
        )

        # ------------------------------------------------------------------
        # Phase 2 — kernel-side hot-detach FIRST (Codex critic v2 HIGH #2).
        # If this raises, lab.json + IPAM + runtime attachments stay intact.
        # ``_detach_docker_interface_locked`` itself absorbs ``HostNetEINVAL``
        # (host-end already gone) and on success removes the attachment row.
        # ------------------------------------------------------------------
        if node_eps:
            from app.services.node_runtime_service import (  # noqa: WPS433
                NodeRuntimeError,
                NodeRuntimeQMPTimeout,
                NodeRuntimeService,
            )

            runtime_service = NodeRuntimeService()
            # US-304: collect (node_id, iface_idx, kind) so we can
            # tear down qemu + docker endpoints on the same link in
            # the right order. For multi-endpoint mixed-kind links,
            # surface the FIRST error and best-effort log the
            # secondary cleanup attempt — partial-failure detach is
            # better than no detach.
            primary_error: Exception | None = None
            for node_id, iface_idx in node_eps:
                runtime = runtime_service._runtime_record(
                    mutex_lab_id, node_id, include_stopped=True
                )
                if runtime is None:
                    # Stopped node — no kernel-side work; proceed to JSON
                    # cleanup so the link record disappears.
                    continue
                kind = str(runtime.get("kind") or "")
                if kind == "docker":
                    try:
                        runtime_service._detach_docker_interface_locked(
                            mutex_lab_id,
                            int(node_id),
                            int(iface_idx),
                            expected_generation=(
                                int(attach_generation) if attach_generation else None
                            ),
                        )
                    except RuntimeMutexContention as exc:
                        # Surface as 409 to the router (mirrors create_link).
                        raise LinkContentionError(exc) from exc
                    except (
                        NodeRuntimeError,
                        NodeRuntimeQMPTimeout,
                        host_net.HostNetError,
                    ) as exc:
                        if primary_error is None:
                            primary_error = exc
                        else:
                            _logger.exception(
                                "delete_link: secondary docker detach failed "
                                "(lab=%s node=%s iface=%s) — primary error already "
                                "raised: %s",
                                mutex_lab_id,
                                node_id,
                                iface_idx,
                                primary_error,
                            )
                elif kind == "qemu":
                    # Pin guest-visible carrier OFF before tearing the
                    # device down. The hot-detach path will remove the
                    # device entirely on the happy path, so this is
                    # mostly belt-and-suspenders for the forced-fallback
                    # branch (where ``netdev_del`` is skipped and the
                    # device lingers until guest reboot). Best-effort.
                    runtime_service.set_qemu_nic_link(
                        mutex_lab_id, int(node_id), int(iface_idx), up=False
                    )

                    # US-304: hot-detach via QMP. Mutex is held by the
                    # caller (delete_link) so we use the PRIVATE locked
                    # helper directly to avoid re-acquiring on the same
                    # thread.
                    #
                    # US-204b freshness contract (codex hotfix HIGH-1):
                    # pass ``expected_generation`` so a stale rollback
                    # cannot tear down a NEWER QEMU NIC on the same iface
                    # — QMP IDs reuse ``dev{iface}`` / ``net{iface}`` and
                    # without the gen check the docker-path safety net
                    # was silently bypassed on the qemu side.
                    try:
                        runtime_service._detach_qemu_interface_locked(
                            mutex_lab_id,
                            int(node_id),
                            int(iface_idx),
                            lab_path=normalized,
                            expected_generation=(
                                int(attach_generation) if attach_generation else None
                            ),
                        )
                    except RuntimeMutexContention as exc:
                        raise LinkContentionError(exc) from exc
                    except (
                        NodeRuntimeError,
                        NodeRuntimeQMPTimeout,
                        host_net.HostNetError,
                    ) as exc:
                        if primary_error is None:
                            primary_error = exc
                        else:
                            _logger.exception(
                                "delete_link: secondary qemu detach failed "
                                "(lab=%s node=%s iface=%s) — primary error already "
                                "raised: %s",
                                mutex_lab_id,
                                node_id,
                                iface_idx,
                                primary_error,
                            )
                # Any other runtime kind (iol, dynamips, vpcs) — no
                # hot-detach path yet; fall through to JSON cleanup.
            if primary_error is not None:
                raise primary_error

        # ------------------------------------------------------------------
        # Phase 3 — release the IP via ``network_service._release_ip`` (its
        # own ``lab_lock``). Codex critic v2 MEDIUM: the canonical helper
        # owns the sort + ipv4 validation; the inlined ``used.remove`` it
        # used to bypass is gone.
        # ------------------------------------------------------------------
        if (
            paired_net_id is not None
            and isinstance(removed_ip, str)
            and removed_ip
        ):
            from app.services.network_service import NetworkService  # noqa: WPS433

            NetworkService()._release_ip(normalized, int(paired_net_id), removed_ip)

        # ------------------------------------------------------------------
        # Phase 4 — under ``lab_lock``: remove the link from lab.json + GC
        # implicit networks at refcount 0. We re-read the JSON inside the
        # lock because Phase 3 mutated it; the link record itself was not
        # changed by ``_release_ip`` so the original ``target_link`` is
        # still findable by id.
        # ------------------------------------------------------------------
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
                # Another caller deleted the link between Phase 1 and now.
                # Treat as idempotent success — kernel + IPAM cleanup
                # already ran for our copy of the link record.
                return True, None

            removed_link = links.pop(target_index)
            data["links"] = links

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

        return False, deleted_implicit

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
