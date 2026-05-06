"""Generic Linux fallback adapter (#186).

Catch-all qemu adapter for any EVE-NG template that no vendor-specific adapter
claims. ``priority = 0`` ensures it dispatches last; ``match()`` returns True
unconditionally so the registry never has an unmatched-but-known template.

Emits a virtio-blk + virtio-net node template; everything else from the raw
intermediate dict goes into ``extras._eveng_raw`` so the importer never
silently loses information.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter


class GenericLinuxAdapter(VendorAdapter):
    name: ClassVar[str] = "generic_linux"
    priority: ClassVar[int] = 0
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image"}

    def match(self, raw: dict[str, Any]) -> bool:
        return True

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        eveng_raw = raw.get("_eveng_raw")
        return {
            "schema": 1,
            "id": str(raw.get("name") or raw.get("image") or "generic-linux"),
            "name": str(raw.get("name") or raw.get("image") or "generic-linux"),
            "vendor": "generic",
            "kind": "qemu",
            "image": str(raw["image"]),
            "cpu": int(raw.get("cpu", 1)),
            "ram": int(raw.get("ram", 512)),
            "ethernet": int(raw.get("ethernet", 1)),
            "console": str(raw.get("console", "telnet")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "_eveng_raw": eveng_raw if eveng_raw is not None else dict(raw),
            },
        }
