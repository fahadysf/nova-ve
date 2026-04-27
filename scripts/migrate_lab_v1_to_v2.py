#!/usr/bin/env python3
"""Migrate a legacy v1 lab.json file to the v2 schema.

Usage::

    python scripts/migrate_lab_v1_to_v2.py <input.json> <output.json>

The transformation:
  - sets ``schema = 2`` and adds ``viewport`` + ``defaults``
  - converts ``topology[]`` entries into ``links[]`` with auto-generated
    ``lnk_<idx>`` ids
  - rewrites ``network.type == "bridge"`` to ``"linux_bridge"`` (other
    "bridge" substrings inside names/labels/comments are left alone)
  - drops ``network.count`` (recomputed at read time from links)
  - rewrites each interface to v2 shape: adds ``index``, ``planned_mac``
    and ``port_position`` (both null) and removes ``network_id``
  - ensures every network has ``visibility``, ``implicit`` and ``config``

The script does not touch a v2 file; if the input already declares
``"schema": 2`` it is copied verbatim to the output path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _migrate_network(raw: dict[str, Any]) -> dict[str, Any]:
    network = dict(raw)
    if str(network.get("type", "")).strip() == "bridge":
        network["type"] = "linux_bridge"
    network.setdefault("visibility", True)
    network.setdefault("implicit", False)
    network.setdefault("config", {})
    network.pop("count", None)
    return network


def _migrate_interfaces(node: dict[str, Any]) -> list[dict[str, Any]]:
    raw_interfaces = node.get("interfaces") or []
    migrated: list[dict[str, Any]] = []
    for index, interface in enumerate(raw_interfaces):
        if not isinstance(interface, dict):
            continue
        migrated.append(
            {
                "index": index,
                "name": str(interface.get("name", f"eth{index}")),
                "planned_mac": None,
                "port_position": None,
            }
        )
    return migrated


def _topology_to_links(topology: list[Any]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for index, entry in enumerate(topology or []):
        if not isinstance(entry, dict):
            continue

        source = str(entry.get("source", "") or "")
        destination = str(entry.get("destination", "") or "")
        source_iface = entry.get("source_interfaceId", 0) or 0
        destination_iface = entry.get("destination_interfaceId", 0)
        network_id = int(entry.get("network_id", 0) or 0)

        def _node_id(value: str) -> int | None:
            if value.startswith("node"):
                tail = value[len("node"):]
                if tail.isdigit():
                    return int(tail)
            return None

        source_node_id = _node_id(source)
        destination_node_id = _node_id(destination)

        link: dict[str, Any] = {
            "id": f"lnk_{index + 1:03d}",
            "style_override": None,
            "label": str(entry.get("label", "") or ""),
            "color": str(entry.get("color", "") or ""),
            "width": str(entry.get("width", "1") or "1"),
            "metrics": {
                "delay_ms": int(entry.get("source_delay", 0) or 0),
                "loss_pct": int(entry.get("source_loss", 0) or 0),
                "bandwidth_kbps": int(entry.get("source_bandwidth", 0) or 0),
                "jitter_ms": int(entry.get("source_jitter", 0) or 0),
            },
        }

        if source_node_id is not None and destination_node_id is not None and not network_id:
            try:
                dst_index = int(destination_iface)
            except (TypeError, ValueError):
                dst_index = 0
            link["from"] = {"node_id": source_node_id, "interface_index": int(source_iface)}
            link["to"] = {"node_id": destination_node_id, "interface_index": dst_index}
            links.append(link)
            continue

        if source_node_id is not None and network_id:
            link["from"] = {"node_id": source_node_id, "interface_index": int(source_iface)}
            link["to"] = {"network_id": network_id}
            links.append(link)
            continue

        if destination_node_id is not None and network_id:
            try:
                dst_index = int(destination_iface)
            except (TypeError, ValueError):
                dst_index = 0
            link["from"] = {"node_id": destination_node_id, "interface_index": dst_index}
            link["to"] = {"network_id": network_id}
            links.append(link)
            continue

    return links


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("schema") == 2:
        return data

    out: dict[str, Any] = {
        "schema": 2,
        "id": data.get("id", ""),
        "meta": data.get("meta", {}) or {},
        "viewport": data.get("viewport", {"x": 0, "y": 0, "zoom": 1.0}),
        "nodes": {},
        "networks": {},
        "links": _topology_to_links(data.get("topology", [])),
        "defaults": data.get("defaults", {"link_style": "orthogonal"}),
        "textobjects": data.get("textobjects", []),
        "lineobjects": data.get("lineobjects", []),
        "pictures": data.get("pictures", []),
        "tasks": data.get("tasks", []),
        "configsets": data.get("configsets", {}),
    }

    for node_id, node in (data.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        new_node = {k: v for k, v in node.items() if k != "interfaces"}
        new_node["interfaces"] = _migrate_interfaces(node)
        out["nodes"][str(node_id)] = new_node

    for network_id, network in (data.get("networks") or {}).items():
        if isinstance(network, dict):
            out["networks"][str(network_id)] = _migrate_network(network)

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("input", type=Path, help="legacy v1 lab.json input")
    parser.add_argument("output", type=Path, help="v2 lab.json destination")
    args = parser.parse_args(argv)

    raw = json.loads(args.input.read_text())
    migrated = migrate(raw)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(migrated, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
