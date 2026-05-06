"""Cisco CSR1000v / Cat8000v adapter (#187).

Matches both ``csr1000v-universalk9*.qcow2`` and ``cat8kv-universalk9*.qcow2``
(Cat8000v is the modern rename of the same VM family). virtio-net NICs,
serial-over-tcp console.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(
    r"(?:csr1000v-universalk9|cat8kv-universalk9).*\.qcow2$", re.IGNORECASE
)


class CiscoCSR1000vAdapter(VendorAdapter):
    name: ClassVar[str] = "cisco_csr1000v"
    priority: ClassVar[int] = 80
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image", "ram"}

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        return bool(_IMAGE_RE.search(image))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image = str(raw["image"])
        is_cat8k = "cat8kv" in image.lower()
        return {
            "schema": 1,
            "id": str(raw.get("name") or image),
            "name": str(raw.get("name") or ("Cisco Cat8000v" if is_cat8k else "Cisco CSR1000v")),
            "vendor": "cisco",
            "kind": "qemu",
            "image": image,
            "cpu": int(raw.get("cpu", 2)),
            "ram": int(raw["ram"]),
            "ethernet": int(raw.get("ethernet", 4)),
            "console": str(raw.get("console_type", "serial")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
