"""Cisco IOL adapter (#187).

Matches ``i86bi-linux*`` binaries. Special-case: emits
``extras.iol_license_path`` pointing at ``${IMAGES_DIR}/iol/<image-key>/iourc``
(relative-style for portability across hosts as long as the iol image dir is
intact). kind=``iol`` (not qemu).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(r"i86bi-linux", re.IGNORECASE)


class CiscoIOLAdapter(VendorAdapter):
    name: ClassVar[str] = "cisco_iol"
    priority: ClassVar[int] = 80
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image", "ram"}

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        return bool(_IMAGE_RE.search(image))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image = str(raw["image"])
        # Derive image key from the image filename (drop extension for iol bin keys).
        image_key = Path(image).stem or image
        license_path = f"${{IMAGES_DIR}}/iol/{image_key}/iourc"
        return {
            "schema": 1,
            "id": str(raw.get("name") or image),
            "name": str(raw.get("name") or "Cisco IOL"),
            "vendor": "cisco",
            "kind": "iol",
            "image": image,
            "cpu": int(raw.get("cpu", 1)),
            "ram": int(raw["ram"]),
            "ethernet": int(raw.get("ethernet", 4)),
            "console": str(raw.get("console_type", "telnet")),
            "extras": {
                "iol_license_path": license_path,
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
