"""Juniper vSRX adapter (#188).

Single VM, virtio-net NICs, serial console. Matches
``media-vsrx-vmdisk*.qcow2`` (the canonical vSRX bundle filename).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(r"media-vsrx-vmdisk.*\.qcow2$", re.IGNORECASE)


class JuniperVSRXAdapter(VendorAdapter):
    name: ClassVar[str] = "juniper_vsrx"
    priority: ClassVar[int] = 80
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image", "ram", "cpu"}

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        return bool(_IMAGE_RE.search(image))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image = str(raw["image"])
        return {
            "schema": 1,
            "id": str(raw.get("name") or image),
            "name": str(raw.get("name") or "Juniper vSRX"),
            "vendor": "juniper",
            "kind": "qemu",
            "image": image,
            "cpu": int(raw["cpu"]),
            "ram": int(raw["ram"]),
            "ethernet": int(raw.get("ethernet", 4)),
            "console": str(raw.get("console_type", "serial")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
