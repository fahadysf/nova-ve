"""Arista vEOS adapter (#189).

Matches both ``vEOS-lab-*.vmdk`` and ``vEOS-lab-*.qcow2``. Aboot+vEOS uses a
two-disk layout: ``aboot-veos.iso`` is the boot CD-ROM and ``vEOS-lab*.qcow2``
is the system disk. The adapter emits a qemu template referencing both.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(r"vEOS-lab-.*\.(?:qcow2|vmdk)$", re.IGNORECASE)


class AristaVEosAdapter(VendorAdapter):
    name: ClassVar[str] = "arista_veos"
    priority: ClassVar[int] = 70
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image", "ram"}

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        return bool(_IMAGE_RE.search(image))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image = str(raw["image"])
        return {
            "schema": 1,
            "id": str(raw.get("name") or image),
            "name": str(raw.get("name") or "Arista vEOS"),
            "vendor": "arista",
            "kind": "qemu",
            "image": image,
            "cpu": int(raw.get("cpu", 1)),
            "ram": int(raw["ram"]),
            "ethernet": int(raw.get("ethernet", 4)),
            "console": str(raw.get("console_type", "serial")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "boot_cdrom": "aboot-veos.iso",
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
