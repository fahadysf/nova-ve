"""Juniper vQFX adapter (#188) — paired-node template (RE + PFE).

vQFX is the lighter-weight cousin of vMX: same paired-VM shape (RE control
plane + PFE forwarding plane), shipped as ``vqfx*-re*.qcow2`` +
``vqfx*-pfe*.qcow2``. Emits the same paired-node template structure as
vMX with `kind="paired"`, two nodes, and the internal RE↔PFE link.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(r"vqfx", re.IGNORECASE)


class JuniperVQFXAdapter(VendorAdapter):
    name: ClassVar[str] = "juniper_vqfx"
    priority: ClassVar[int] = 80
    REQUIRED_FIELDS: ClassVar[set[str]] = {
        "image_re",
        "image_pfe",
        "ram_re",
        "ram_pfe",
    }

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        return bool(_IMAGE_RE.search(image))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        return {
            "schema": 1,
            "id": str(raw.get("name") or "juniper-vqfx"),
            "name": str(raw.get("name") or "Juniper vQFX"),
            "vendor": "juniper",
            "kind": "paired",
            "nodes": [
                {
                    "id": "re",
                    "name": "vQFX RE",
                    "kind": "qemu",
                    "image": str(raw["image_re"]),
                    "cpu": int(raw.get("cpu_re", 1)),
                    "ram": int(raw["ram_re"]),
                    # Default bumped 1 → 2 so the internal em1 link interface
                    # is in range; fxp0 occupies index 0 per Junos convention.
                    "ethernet": int(raw.get("ethernet_re", 2)),
                    "console": str(raw.get("console_type", "serial")),
                    "interface_naming": {"explicit": ["fxp0", "em1"]},
                    "extras": {
                        "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                        "_eveng_paired_role": "re",
                    },
                },
                {
                    "id": "pfe",
                    "name": "vQFX PFE",
                    "kind": "qemu",
                    "image": str(raw["image_pfe"]),
                    "cpu": int(raw.get("cpu_pfe", 1)),
                    "ram": int(raw["ram_pfe"]),
                    "ethernet": int(raw.get("ethernet_pfe", 4)),
                    "console": str(raw.get("console_type", "serial")),
                    "interface_naming": {"explicit": ["em0", "em1", "em2", "em3"]},
                    "extras": {
                        "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                        "_eveng_paired_role": "pfe",
                    },
                },
            ],
            "links": [
                {
                    "from_node": "re",
                    "from_iface": "em1",
                    "to_node": "pfe",
                    "to_iface": "em1",
                }
            ],
            "extras": {
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
