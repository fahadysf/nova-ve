#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Generate the Bridge-Cloud netplan YAML for a list of physical
interfaces.

Usage::

    nova-ve-netplan-gen.py eth0 [eth1 ...] [options]

Without ``--migrate-from`` the bridge defaults to ``dhcp4: true`` /
``dhcp6: false`` — the original behaviour for fresh hosts where the
parent NIC was DHCP.

With ``--migrate-from <dir>`` + one ``--iface-mac eth0:<MAC>`` per arg,
the generator inspects the source netplan(s) under ``<dir>``, finds the
ethernet stanza whose ``match.macaddress`` matches each MAC, and copies
the L3 fields (``addresses``, ``routes``, ``nameservers``, ``gateway4``,
``gateway6``, ``mtu``, ``dhcp4``, ``dhcp6``) onto the corresponding
``br-ethN`` bridge stanza.  The parent ``ethernets.ethN`` stanza always
stays ``dhcp4: false`` / ``dhcp6: false`` so the link role is just to
slave into the bridge.

Round-trips through ``yaml.safe_load`` / ``yaml.safe_dump``
byte-identical when ``--migrate-from`` is not used (per AC3-ci in
.omc/plans/bridge-cloud-feature.md §3).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Any, Dict, Iterable, List, Optional


_IFACE_RE = re.compile(r"\Aeth[0-9]+\Z")
_MAC_RE = re.compile(r"\A[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}\Z")

# Fields to copy from the source ethernet stanza onto the bridge stanza
# when ``--migrate-from`` is used.  Listed fields are netplan v2's
# canonical names for L3 / link properties.
_L3_FIELDS = (
    "dhcp4",
    "dhcp6",
    "addresses",
    "routes",
    "routing-policy",
    "nameservers",
    "gateway4",
    "gateway6",
    "mtu",
    "accept-ra",
    "dhcp4-overrides",
    "dhcp6-overrides",
)


def _load_yaml(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("PyYAML missing — install python3-yaml") from exc
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _normalise_mac(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _find_source_ethernet_by_mac(
    source_files: Iterable[pathlib.Path], mac: str
) -> Optional[Dict[str, Any]]:
    """Search the source netplan files for an ethernet whose
    ``match.macaddress`` matches ``mac``.  Returns the stanza dict or
    None.
    """
    mac_norm = _normalise_mac(mac)
    if not mac_norm:
        return None
    for path in source_files:
        data = _load_yaml(path)
        if not data:
            continue
        ethernets = (data.get("network") or {}).get("ethernets") or {}
        if not isinstance(ethernets, dict):
            continue
        for _name, stanza in ethernets.items():
            if not isinstance(stanza, dict):
                continue
            match_block = stanza.get("match") or {}
            stanza_mac = _normalise_mac(match_block.get("macaddress"))
            if stanza_mac == mac_norm:
                return stanza
    return None


def build_transitional_config(
    ifaces: List[str],
    *,
    mac_map: Dict[str, str],
    migrate_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    """Phase B transitional netplan: NO bridges yet.

    Each ``ethN`` gets the L3 config from the MAC-matched source
    ethernet (so static IPs survive the rename), with a ``match`` block
    pinning by MAC.  Used between Phase B reboot and Phase C apply.
    """
    source_files: List[pathlib.Path] = []
    if migrate_from is not None and migrate_from.exists():
        if migrate_from.is_dir():
            source_files = sorted(migrate_from.glob("*.yaml"))
        elif migrate_from.is_file():
            source_files = [migrate_from]

    ethernets: Dict[str, Dict[str, Any]] = {}
    for iface in ifaces:
        mac = mac_map.get(iface, "")
        stanza: Dict[str, Any] = {"match": {"macaddress": mac}} if mac else {}
        copied_from_source = False
        if source_files and mac:
            src = _find_source_ethernet_by_mac(source_files, mac)
            if src:
                for field in _L3_FIELDS:
                    if field in src:
                        stanza[field] = src[field]
                copied_from_source = True
        if not copied_from_source:
            stanza.setdefault("dhcp4", True)
            stanza.setdefault("dhcp6", False)
        has_addr = (
            stanza.get("dhcp4")
            or stanza.get("dhcp6")
            or stanza.get("addresses")
        )
        if not has_addr:
            stanza["dhcp4"] = True
            stanza["dhcp6"] = False
        ethernets[iface] = stanza

    return {
        "network": {
            "version": 2,
            "renderer": "networkd",
            "ethernets": ethernets,
        }
    }


def build_config(
    ifaces: List[str],
    *,
    mac_map: Optional[Dict[str, str]] = None,
    migrate_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    """Build the netplan v2 structure for ``ifaces``.

    When ``mac_map`` and ``migrate_from`` are provided, each bridge
    stanza inherits L3 fields from the source ethernet stanza identified
    by MAC.  When source data is missing or doesn't match, the bridge
    falls back to ``dhcp4: true``.
    """
    source_files: List[pathlib.Path] = []
    if migrate_from is not None and migrate_from.exists():
        if migrate_from.is_dir():
            source_files = sorted(migrate_from.glob("*.yaml"))
        elif migrate_from.is_file():
            source_files = [migrate_from]

    bridges: Dict[str, Dict[str, Any]] = {}
    for iface in ifaces:
        bridge_name = f"br-{iface}"
        bridge_cfg: Dict[str, Any] = {
            "interfaces": [iface],
            "parameters": {"stp": False, "forward-delay": 0},
        }
        # Pin the bridge MAC to the parent NIC's MAC.  Without this Linux
        # generates a random locally-administered MAC for the bridge, which
        # (a) varies as veth slaves are added/removed and (b) breaks
        # VMware VDS MAC-Learning + upstream firewall IP-MAC bindings that
        # were established against the vNIC's burnt-in MAC.  See
        # .omc/plans/bridge-cloud-feature.md "VDS MAC pinning".
        if mac_map and iface in mac_map:
            bridge_cfg["macaddress"] = mac_map[iface]
        applied_from_source = False
        if source_files and mac_map and iface in mac_map:
            stanza = _find_source_ethernet_by_mac(source_files, mac_map[iface])
            if stanza:
                for field in _L3_FIELDS:
                    if field in stanza:
                        bridge_cfg[field] = stanza[field]
                applied_from_source = True
        if not applied_from_source:
            bridge_cfg.setdefault("dhcp4", True)
            bridge_cfg.setdefault("dhcp6", False)
        else:
            # Make sure the bridge has at least one address source.  If
            # the source stanza had neither dhcp nor addresses, fall
            # back to dhcp4 so the host isn't left orphaned.
            has_addr = (
                bridge_cfg.get("dhcp4")
                or bridge_cfg.get("dhcp6")
                or bridge_cfg.get("addresses")
            )
            if not has_addr:
                bridge_cfg["dhcp4"] = True
                bridge_cfg["dhcp6"] = False
        bridges[bridge_name] = bridge_cfg

    return {
        "network": {
            "version": 2,
            "renderer": "networkd",
            "ethernets": {
                iface: {"dhcp4": False, "dhcp6": False} for iface in ifaces
            },
            "bridges": bridges,
        }
    }


def render_yaml(config: Dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "PyYAML missing — install python3-yaml before running this script"
        ) from exc
    return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def _parse_iface_mac_pairs(values: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in values or []:
        if ":" not in raw:
            raise SystemExit(f"--iface-mac expects 'ethN:<MAC>': {raw!r}")
        iface, mac = raw.split(":", 1)
        iface = iface.strip()
        mac = mac.strip()
        if not _IFACE_RE.match(iface):
            raise SystemExit(f"--iface-mac: invalid iface name {iface!r}")
        if not _MAC_RE.match(mac):
            raise SystemExit(f"--iface-mac: invalid MAC {mac!r}")
        out[iface] = mac
    return out


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="nova-ve-netplan-gen.py")
    parser.add_argument("ifaces", nargs="+", help="physical iface names (eth0, eth1, ...)")
    parser.add_argument(
        "--out",
        default="/etc/netplan/60-nova-ve-bridge-cloud.yaml",
        help="output path (set to '-' to write to stdout)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="print the YAML to stdout in addition to writing",
    )
    parser.add_argument(
        "--migrate-from",
        type=pathlib.Path,
        default=None,
        help="copy L3 config (addresses/routes/nameservers/dhcp4/etc.) "
        "from each MAC-matched ethernet in this netplan dir/file onto "
        "the corresponding br-ethN bridge",
    )
    parser.add_argument(
        "--iface-mac",
        action="append",
        default=[],
        help="ethN:<MAC> pair for --migrate-from lookups (repeatable)",
    )
    parser.add_argument(
        "--transitional",
        action="store_true",
        help="emit ethernet-only netplan (no bridges) — used by Phase B "
        "to keep the host reachable across the rename reboot",
    )
    args = parser.parse_args(argv)

    bad = [i for i in args.ifaces if not _IFACE_RE.match(i)]
    if bad:
        sys.stderr.write(f"invalid iface name(s): {bad!r} (expected ^eth[0-9]+$)\n")
        return 2

    mac_map = _parse_iface_mac_pairs(args.iface_mac)
    if args.transitional:
        if not mac_map:
            sys.stderr.write("--transitional requires at least one --iface-mac pair\n")
            return 2
        config = build_transitional_config(
            args.ifaces, mac_map=mac_map, migrate_from=args.migrate_from
        )
    else:
        config = build_config(
            args.ifaces, mac_map=mac_map, migrate_from=args.migrate_from
        )
    body = render_yaml(config)

    if args.out == "-":
        sys.stdout.write(body)
        return 0
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    try:
        out.chmod(0o600)
    except OSError as exc:  # pragma: no cover
        sys.stderr.write(f"chmod 0600 {out} failed: {exc}\n")
        return 1
    if args.print:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
