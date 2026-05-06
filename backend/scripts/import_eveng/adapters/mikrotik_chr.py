"""Mikrotik CHR adapter (#189).

Matches ``chr-*.img`` and ``chr-*.qcow2``. CHR (Cloud Hosted Router) is
sensitive to NIC model, so the adapter forces e1000 (not virtio-net). Console
is telnet-via-serial. License-aware: any license file present in the image
dir is preserved by the migrate orchestrator's per-file copy logic.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(r"chr-.*\.(?:qcow2|img)$", re.IGNORECASE)


class MikrotikCHRAdapter(VendorAdapter):
    name: ClassVar[str] = "mikrotik_chr"
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
            "name": str(raw.get("name") or "Mikrotik CHR"),
            "vendor": "mikrotik",
            "kind": "qemu",
            "image": image,
            "cpu": int(raw.get("cpu", 1)),
            "ram": int(raw["ram"]),
            "ethernet": int(raw.get("ethernet", 4)),
            "console": str(raw.get("console_type", "telnet")),
            "extras": {
                "qemu_nic": "e1000",  # CHR is sensitive: forced e1000, not virtio
                "qemu_options": str(raw.get("qemu_options", "")),
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
