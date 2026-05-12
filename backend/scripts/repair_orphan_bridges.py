#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""One-shot repair for orphan implicit-bridge corruption in lab.json.

Two failure modes have been observed:

1. Duplicate iface use — a single ``(node_id, interface_index)`` appears in
   more than one declared link. The kernel can only attach one TAP/veth to
   one bridge at a time, so only one of these can match real state; the
   others are zombies. ``link_service.create_link`` now rejects this at
   creation time (``InterfaceAlreadyAttachedError``), but earlier lab.json
   files may already contain the corruption.

2. Orphan implicit-bridge halves — an implicit network has refcount != 2.
   Implicit bridges are created in paired-link mode (refcount == 2) and
   promoted to ordinary networks at refcount == 3. Any other refcount on
   an implicit network is a broken state. ``link_service.delete_link`` now
   cascades the orphan half + drops the bridge on the refcount==1 path,
   but pre-existing corruption needs this script.

Usage::

    sudo python3 -m backend.scripts.repair_orphan_bridges \\
        /var/lib/nova-ve/labs/alpine-docker-demo.json [--dry-run]

The script writes a ``.bak`` of the original next to the lab.json before
making changes.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


def _iface_key_for_link(link: dict) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for ep in (link.get("from"), link.get("to")):
        if not isinstance(ep, dict) or "node_id" not in ep:
            continue
        try:
            out.append((int(ep["node_id"]), int(ep.get("interface_index", 0))))
        except (TypeError, ValueError):
            continue
    return out


def _refcount(links: List[dict], network_id: int) -> int:
    n = 0
    for link in links:
        for ep in (link.get("from"), link.get("to")):
            if isinstance(ep, dict):
                try:
                    if int(ep.get("network_id", 0) or 0) == int(network_id):
                        n += 1
                except (TypeError, ValueError):
                    continue
    return n


def _link_id_sort_key(link_id: str) -> Tuple[int, str]:
    """Sort key so ``lnk_001 < lnk_002 < lnk_010``."""
    if isinstance(link_id, str) and link_id.startswith("lnk_"):
        tail = link_id[len("lnk_"):]
        if tail.isdigit():
            return (int(tail), link_id)
    return (10**9, str(link_id))


def repair(lab_data: dict) -> Dict[str, list]:
    """Return a diff report and mutate ``lab_data`` in place."""
    links: List[dict] = list(lab_data.get("links", []) or [])
    networks: Dict[str, dict] = dict(lab_data.get("networks", {}) or {})

    removed_links: List[dict] = []
    removed_networks: List[dict] = []

    # Pass 1 — drop duplicate iface use. Keep the link with the highest
    # lnk_NNN suffix (presumed newest = matches the user's latest intent).
    by_iface: Dict[Tuple[int, int], List[dict]] = {}
    for link in links:
        for iface_key in _iface_key_for_link(link):
            by_iface.setdefault(iface_key, []).append(link)
    dup_drop: Set[str] = set()
    for iface_key, link_group in by_iface.items():
        if len(link_group) <= 1:
            continue
        link_group_sorted = sorted(
            link_group, key=lambda lk: _link_id_sort_key(str(lk.get("id"))), reverse=True
        )
        # link_group_sorted[0] is the newest; drop the rest.
        for stale in link_group_sorted[1:]:
            dup_drop.add(str(stale.get("id")))
    if dup_drop:
        kept: List[dict] = []
        for link in links:
            if str(link.get("id")) in dup_drop:
                removed_links.append(link)
            else:
                kept.append(link)
        links = kept

    # Pass 2 — drop orphan implicit-bridge halves and their bridge. An
    # implicit network with refcount != 2 is broken (post-promotion it
    # should have implicit=false; pre-paired it has refcount==2).
    changed = True
    while changed:
        changed = False
        for net_id_str, network_record in list(networks.items()):
            if not isinstance(network_record, dict):
                continue
            if network_record.get("implicit") is not True:
                continue
            try:
                net_id = int(net_id_str)
            except (TypeError, ValueError):
                continue
            count = _refcount(links, net_id)
            if count == 2:
                continue
            # Orphan: drop every link referencing this network and the
            # network itself. Looping again will catch any cascades.
            survivors: List[dict] = []
            for link in links:
                touches = False
                for ep in (link.get("from"), link.get("to")):
                    if isinstance(ep, dict) and "network_id" in ep:
                        try:
                            if int(ep["network_id"]) == net_id:
                                touches = True
                                break
                        except (TypeError, ValueError):
                            continue
                if touches:
                    removed_links.append(link)
                    changed = True
                else:
                    survivors.append(link)
            links = survivors
            removed_networks.append(network_record)
            networks.pop(net_id_str, None)
            changed = True

    lab_data["links"] = links
    lab_data["networks"] = networks
    return {
        "removed_links": removed_links,
        "removed_networks": removed_networks,
    }


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lab_path", help="absolute path to lab.json")
    parser.add_argument("--dry-run", action="store_true", help="report only, do not write")
    args = parser.parse_args(argv)

    path = Path(args.lab_path)
    if not path.is_file():
        print(f"error: {path} is not a regular file", file=sys.stderr)
        return 2

    with path.open("r", encoding="utf-8") as fh:
        lab_data = json.load(fh)

    report = repair(lab_data)

    print(f"Repair report for {path}:")
    print(f"  removed links:    {len(report['removed_links'])}")
    for link in report["removed_links"]:
        endpoints = []
        for ep in (link.get("from"), link.get("to")):
            if isinstance(ep, dict):
                endpoints.append(json.dumps(ep, sort_keys=True))
        print(f"    - {link.get('id')!r:>10s}  {' <-> '.join(endpoints)}")
    print(f"  removed networks: {len(report['removed_networks'])}")
    for network in report["removed_networks"]:
        print(f"    - id={network.get('id')!r}  name={network.get('name')!r}  bridge={(network.get('runtime') or {}).get('bridge_name')!r}")

    if args.dry_run:
        print("dry-run: no changes written")
        return 0
    if not report["removed_links"] and not report["removed_networks"]:
        print("no corruption found; lab.json untouched")
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(lab_data, fh, indent=2)
        fh.write("\n")
    print(f"wrote repaired lab.json (backup at {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
