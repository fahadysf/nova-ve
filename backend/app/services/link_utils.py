# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Pure link-endpoint helpers shared between link_service and standalone scripts.

These functions have zero heavy dependencies so they can be imported by
scripts/dedup_links.py without triggering the full app stack (database, etc.).
"""

from __future__ import annotations


def _endpoint_key(ep: dict) -> tuple:
    """Return a hashable key for a normalised endpoint dict."""
    if "network_id" in ep:
        return ("network", int(ep["network_id"]))
    return ("node", int(ep["node_id"]), int(ep.get("interface_index", 0)))


def _link_pair_key(ep_a: dict, ep_b: dict) -> tuple:
    """Return a canonical (order-independent) key for a {from, to} pair."""
    ka = _endpoint_key(ep_a)
    kb = _endpoint_key(ep_b)
    return (min(ka, kb), max(ka, kb))
