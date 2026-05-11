"""Dynamips adapter for the EVE-NG importer.

Matches EVE-NG raw template entries whose ``type == "dynamips"``, or whose
``image`` filename starts with a known Cisco router prefix (c3725, c7200,
c3745, c3640, c3620, c2691, c2620, c2610, c1700). Phase 1 of the
nova-ve Dynamips runtime ships only c3725 + c7200 — images for other
chassis are still matched (so they show up in the manifest) but the
adapter flags them as ``needs-manual-review`` via :class:`NeedsManualReview`
to avoid creating templates the runtime cannot launch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import NeedsManualReview, VendorAdapter


# Phase 1 runtime-supported platforms. Any other prefix matched by
# ``_IMAGE_RE`` lands in needs-manual-review.
_SUPPORTED_PLATFORMS: set[str] = {"c3725", "c7200"}

# Image-name detection. EVE-NG ships Dynamips images either with the bare
# platform-prefixed filename (e.g. ``c3725-adventerprisek9-mz...``) or with
# the ``.image`` extension used by unetlab's addons tree. Both forms appear
# in real-world dumps.
_IMAGE_RE = re.compile(
    r"^(c1700|c2600|c2610|c2620|c2691|c3620|c3640|c3725|c3745|c7200)",
    re.IGNORECASE,
)


def _platform_from_image(image: str) -> str | None:
    name = Path(image).name
    match = _IMAGE_RE.match(name)
    if not match:
        return None
    return match.group(1).lower()


class DynamipsAdapter(VendorAdapter):
    name: ClassVar[str] = "dynamips"
    priority: ClassVar[int] = 85
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image"}

    def match(self, raw: dict[str, Any]) -> bool:
        if str(raw.get("type", "")).lower() == "dynamips":
            return True
        image = str(raw.get("image", ""))
        return _platform_from_image(image) is not None

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image = str(raw["image"])
        platform = (
            str(raw.get("platform") or "").lower()
            or _platform_from_image(image)
            or ""
        )
        if platform not in _SUPPORTED_PLATFORMS:
            raise NeedsManualReview(
                f"dynamips: platform {platform!r} not yet supported "
                f"(image={image}); Phase 1 ships only {sorted(_SUPPORTED_PLATFORMS)}"
            )

        # Carry across slot bindings; EVE-NG uses keys ``slot0`` .. ``slot6``.
        extras: dict[str, Any] = {"platform": platform}
        for index in range(7):
            slot_key = f"slot{index}"
            value = raw.get(slot_key)
            if value:
                extras[slot_key] = str(value)

        # NVRAM and idle-PC pass straight through when present.
        if "nvram" in raw:
            extras["nvram"] = int(raw["nvram"])
        idle = raw.get("idle") or raw.get("idle_pc") or raw.get("idlepc")
        if idle:
            extras["idlepc"] = str(idle)
        # c7200-specific fields.
        if platform == "c7200":
            npe = raw.get("npe")
            if npe:
                extras["npe"] = str(npe)
            midplane = raw.get("midplane")
            if midplane:
                extras["midplane"] = str(midplane)

        extras["_eveng_raw"] = raw.get("_eveng_raw") or dict(raw)

        ram_default = 128 if platform == "c3725" else 512
        ethernet_default = self._infer_ethernet_count(platform, extras)

        return {
            "schema": 1,
            "id": str(raw.get("name") or image),
            "name": str(raw.get("name") or f"Cisco {platform}"),
            "vendor": "cisco",
            "kind": "dynamips",
            "image": image,
            "cpu": int(raw.get("cpu", 1)),
            "ram": int(raw.get("ram", ram_default)),
            "ethernet": int(raw.get("ethernet", ethernet_default)),
            "console": str(raw.get("console_type") or raw.get("console", "telnet")),
            "extras": extras,
        }

    @staticmethod
    def _infer_ethernet_count(platform: str, extras: dict[str, Any]) -> int:
        # Default ethernet counts when the EVE-NG template doesn't state one.
        if platform == "c3725":
            # GT96100-FE built-in = 2 FastEthernet; slot 1 NM may add more.
            return 2
        # c7200 with the default C7200-IO-FE = 1 FastEthernet on slot 0.
        return 1
