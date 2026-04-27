"""Centralized planned-MAC registry scoped per ``(network_id, planned_mac)``.

This module is the single source of truth for collision detection of planned
MAC addresses across all labs persisted on this backend instance. The host's
NIC table is **never** probed; collision is purely a function of what the
labs themselves declare. Two interfaces sharing a MAC on the same L2
broadcast domain (same ``network_id``) collide; the same MAC on different
networks (or different labs that have isolated bridges with their own
``network_id`` namespace) does not.

The registry key is the 2-tuple ``(network_id, planned_mac_lower)`` and the
value is the owner triple ``(lab_id, node_id, interface_index)``. Peer-to-peer
links (both endpoints are nodes, no ``network_id``) are intentionally skipped
because they have no broadcast domain. Wave 1's implicit-bridge code turns
those into bridge networks before persistence, but defending against a stray
peer link here is correct.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional, Tuple

from app.services.lab_service import LabService


OwnerKey = Tuple[str, int, int]
RegistryKey = Tuple[int, str]


class MacRegistry:
    """Thread-safe in-memory map of ``(network_id, mac)`` -> owner triple."""

    def __init__(self) -> None:
        self._entries: dict[RegistryKey, OwnerKey] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        return mac.strip().lower()

    def check_collision(
        self,
        network_id: int,
        planned_mac: str,
        owner_key: Optional[OwnerKey] = None,
    ) -> Optional[OwnerKey]:
        """Return the conflicting owner triple, or ``None`` if free.

        ``owner_key`` (``(lab_id, node_id, interface_index)``) is excluded so
        re-registering an already-present interface is not a false positive.
        Comparison is case-insensitive on the MAC.
        """
        if planned_mac is None:
            return None
        mac = self._normalize_mac(planned_mac)
        if not mac:
            return None
        key = (int(network_id), mac)
        with self._lock:
            existing = self._entries.get(key)
        if existing is None:
            return None
        if owner_key is not None and existing == owner_key:
            return None
        return existing

    def recompute_for_lab(self, lab_id: str, lab_data: dict) -> None:
        """Single-pass replace of all entries owned by ``lab_id``.

        Evicts every prior registration owned by ``lab_id`` (regardless of
        which ``network_id`` it lived under) and re-derives the fresh set from
        ``lab_data['links']`` and ``lab_data['nodes']``.
        """
        nodes = lab_data.get("nodes", {}) or {}
        links = lab_data.get("links", []) or []

        fresh: dict[RegistryKey, OwnerKey] = {}
        for link in links:
            endpoint_a = link.get("from") or {}
            endpoint_b = link.get("to") or {}

            node_endpoint: Optional[dict] = None
            network_endpoint: Optional[dict] = None
            for endpoint in (endpoint_a, endpoint_b):
                if not isinstance(endpoint, dict):
                    continue
                if "network_id" in endpoint:
                    network_endpoint = endpoint
                elif "node_id" in endpoint:
                    node_endpoint = endpoint

            if node_endpoint is None or network_endpoint is None:
                # Peer-to-peer (no network_id) or malformed link: skip.
                continue

            try:
                network_id = int(network_endpoint.get("network_id"))
                node_id = int(node_endpoint.get("node_id"))
                interface_index = int(node_endpoint.get("interface_index", -1))
            except (TypeError, ValueError):
                continue
            if interface_index < 0:
                continue

            node = nodes.get(str(node_id)) or nodes.get(node_id) or {}
            interfaces = node.get("interfaces") or []
            if not (0 <= interface_index < len(interfaces)):
                continue
            interface = interfaces[interface_index]
            if not isinstance(interface, dict):
                continue
            planned_mac = interface.get("planned_mac")
            if not planned_mac:
                continue
            mac = self._normalize_mac(str(planned_mac))
            if not mac:
                continue

            owner: OwnerKey = (lab_id, node_id, interface_index)
            fresh[(network_id, mac)] = owner

        with self._lock:
            stale_keys = [k for k, v in self._entries.items() if v[0] == lab_id]
            for key in stale_keys:
                del self._entries[key]
            self._entries.update(fresh)

    def rebuild_all(self, labs_dir) -> None:
        """Walk every ``*.json`` in ``labs_dir`` and recompute its registry."""
        labs_path = Path(labs_dir)
        if not labs_path.exists():
            return
        for lab_file in sorted(labs_path.rglob("*.json")):
            try:
                relative = lab_file.relative_to(labs_path).as_posix()
            except ValueError:
                continue
            try:
                lab_data = LabService.read_lab_json_static(relative)
            except (ValueError, OSError):
                # Legacy schema or unreadable file: skip silently.
                continue
            lab_id = lab_file.stem
            self.recompute_for_lab(lab_id, lab_data)

    def suggest_mac(
        self,
        network_id: int,
        base_mac: Optional[str] = None,
    ) -> str:
        """Return a MAC ``50:00:00:NN:NN:NN`` that does not collide on the network.

        The 24-bit tail starts at the value derived from ``base_mac`` (if
        parseable) or 0, then increments until a free slot is found.
        """
        prefix = "50:00:00"
        start = 0
        if base_mac:
            tail = self._normalize_mac(base_mac).split(":")
            if len(tail) >= 6:
                try:
                    start = int(tail[3], 16) << 16 | int(tail[4], 16) << 8 | int(tail[5], 16)
                except ValueError:
                    start = 0
        net_id = int(network_id)
        candidate = start
        with self._lock:
            for _ in range(0x1000000):
                octet_4 = (candidate >> 16) & 0xFF
                octet_5 = (candidate >> 8) & 0xFF
                octet_6 = candidate & 0xFF
                mac = f"{prefix}:{octet_4:02x}:{octet_5:02x}:{octet_6:02x}"
                if (net_id, mac) not in self._entries:
                    return mac
                candidate = (candidate + 1) & 0xFFFFFF
        # Pathological: exhausted the whole 24-bit space; fall back.
        return f"{prefix}:00:00:00"


# Module-level singleton used by routers and the startup hook.
mac_registry = MacRegistry()
