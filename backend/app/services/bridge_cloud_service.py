# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Enumeration service for Bridge-Cloud host bridges (``br-eth*``).

Backed by ``/sys/class/net/<bridge>/{carrier,operstate}`` reads and an
``ip -br addr`` subprocess.  Results are cached in-process for 5 seconds
to dampen the cost of repeated UI polls.

See ``.omc/plans/bridge-cloud-feature.md`` §3 AC10a / §4.8 for the
contract: the response feeds the Bridge-Cloud network-config modal's
host-bridge dropdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional


logger = logging.getLogger("nova-ve")


_HOST_BRIDGE_RE = re.compile(r"\Abr-eth([0-9]+)\Z")
_CACHE_TTL_SECONDS = 5.0


class BridgeCloudService:
    """Read-only enumerator for host-owned ``br-eth*`` bridges.

    Uses an instance-level cache so callers from different requests share
    a hit; the global instance is constructed once by the router module.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_ts: float = 0.0

    async def list(self) -> List[Dict[str, Any]]:
        async with self._lock:
            now = time.monotonic()
            if (
                self._cache is not None
                and (now - self._cache_ts) < _CACHE_TTL_SECONDS
            ):
                return list(self._cache)
            payload = await asyncio.to_thread(self._collect_sync)
            self._cache = payload
            self._cache_ts = now
            return list(payload)

    # ------------------------------------------------------------------
    # Internals — synchronous, runs inside ``asyncio.to_thread`` so the
    # event loop isn't blocked by ``subprocess.run``.
    # ------------------------------------------------------------------

    def _collect_sync(self) -> List[Dict[str, Any]]:
        bridges = self._enumerate_bridges()
        addrs_by_iface = self._collect_addrs()
        result: List[Dict[str, Any]] = []
        for bridge in bridges:
            match = _HOST_BRIDGE_RE.match(bridge)
            if not match:
                continue
            iface = f"eth{match.group(1)}"
            result.append({
                "id": f"bridge_cloud_{iface}",
                "label": f"Bridge-Cloud-{iface}",
                "host_bridge": bridge,
                "iface": iface,
                "carrier": self._read_carrier(bridge),
                "addrs": addrs_by_iface.get(bridge, []),
            })
        result.sort(key=lambda r: r["host_bridge"])
        return result

    @staticmethod
    def _enumerate_bridges() -> List[str]:
        try:
            entries = os.listdir("/sys/class/net")
        except OSError:
            return []
        return [name for name in entries if _HOST_BRIDGE_RE.match(name)]

    @staticmethod
    def _read_carrier(bridge: str) -> bool:
        path = f"/sys/class/net/{bridge}/carrier"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip() == "1"
        except OSError:
            return False

    @staticmethod
    def _collect_addrs() -> Dict[str, List[str]]:
        """Parse ``ip -br addr`` once and return {iface: [cidr, ...]}.

        Returns an empty dict if ``ip`` is missing or the call fails.
        """
        ip_bin = shutil.which("ip") or "/usr/sbin/ip"
        if not os.path.exists(ip_bin):
            return {}
        try:
            proc = subprocess.run(
                [ip_bin, "-br", "addr"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("BridgeCloudService: ip -br addr failed (%s)", exc)
            return {}
        out: Dict[str, List[str]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            if not _HOST_BRIDGE_RE.match(name):
                continue
            # parts[1] is operstate; addresses start at parts[2:]
            out[name] = [p for p in parts[2:] if "/" in p]
        return out

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------

    def _reset_cache_for_test(self) -> None:
        self._cache = None
        self._cache_ts = 0.0


# Module-level singleton so the cache survives across requests.
_service = BridgeCloudService()


async def list_bridge_clouds() -> List[Dict[str, Any]]:
    return await _service.list()


def _service_for_test() -> BridgeCloudService:
    return _service
