"""Juniper vMX adapter (#188) — paired-node template (VCP + VFP).

vMX is **two VMs**: VCP (control plane, runs Junos) and VFP (forwarding
plane, runs Trio FPC packet-forwarding). The adapter emits a single
*paired-node template* with ``kind="paired"`` containing two ``nodes`` and
the internal ``fxp0`` ↔ ``em0`` link between them. The picker in #185 is
responsible for rejecting paired templates with 400 until the multi-node
rendering follow-up lands; the API guard cannot be bypassed.

Match heuristic: any image whose filename contains ``vmx-bundle`` (the
official Juniper download bundle name) OR whose name contains ``vmx``.

REQUIRED_FIELDS reflect the paired shape: ``image_vcp``, ``image_vfp``,
``ram_vcp``, ``ram_vfp`` are the per-node fields the parser is expected to
populate. A raw template with only a single ``image`` field will fall
through ``validate()`` to ``NeedsManualReview`` so the operator knows to
hand-augment.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(r"vmx-bundle|vmx-vcp|vmx-vfp", re.IGNORECASE)


class JuniperVMXAdapter(VendorAdapter):
    name: ClassVar[str] = "juniper_vmx"
    priority: ClassVar[int] = 80
    REQUIRED_FIELDS: ClassVar[set[str]] = {
        "image_vcp",
        "image_vfp",
        "ram_vcp",
        "ram_vfp",
    }

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        if _IMAGE_RE.search(image):
            return True
        # Some EVE-NG templates name vMX bundles as "vmx-22.4R1.S1" with a
        # vmx_paired flag — accept either signal.
        return bool(raw.get("vmx_paired"))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image_vcp = str(raw["image_vcp"])
        image_vfp = str(raw["image_vfp"])
        return {
            "schema": 1,
            "id": str(raw.get("name") or "juniper-vmx"),
            "name": str(raw.get("name") or "Juniper vMX"),
            "vendor": "juniper",
            "kind": "paired",
            "nodes": [
                {
                    "id": "vcp",
                    "name": "vMX VCP",
                    "kind": "qemu",
                    "image": image_vcp,
                    "cpu": int(raw.get("cpu_vcp", 1)),
                    "ram": int(raw["ram_vcp"]),
                    "ethernet": int(raw.get("ethernet_vcp", 1)),
                    "console": str(raw.get("console_type", "serial")),
                    "interface_naming": {"explicit": ["fxp0"]},
                    "extras": {
                        "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                        "_eveng_paired_role": "vcp",
                    },
                },
                {
                    "id": "vfp",
                    "name": "vMX VFP",
                    "kind": "qemu",
                    "image": image_vfp,
                    "cpu": int(raw.get("cpu_vfp", 3)),
                    "ram": int(raw["ram_vfp"]),
                    "ethernet": int(raw.get("ethernet_vfp", 4)),
                    "console": str(raw.get("console_type", "serial")),
                    "interface_naming": {"explicit": ["em0", "em1", "em2", "em3"]},
                    "extras": {
                        "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                        "_eveng_paired_role": "vfp",
                    },
                },
            ],
            "links": [
                {
                    "from_node": "vcp",
                    "from_iface": "fxp0",
                    "to_node": "vfp",
                    "to_iface": "em0",
                }
            ],
            "extras": {
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
