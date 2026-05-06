"""VyOS adapter (#189).

Wraps the existing ``deploy/scripts/install-vyos-template.sh`` output: when an
operator imports a VyOS image, the adapter checks whether the resulting node
template body's sha256 matches a list of known-pristine hashes. Pristine
templates dispatch as ``vyos_status="pristine"``; customised templates (any
sha256 not in the pristine list) dispatch as ``vyos_status="customised"`` so
the operator's hand edits are surfaced for review rather than blindly
overwritten.

Per Architect refinement: the marker check is **content-aware** (sha256 of
the body vs a known-pristine list), NOT a string-substring match on a
specific marker comment that operators might preserve while editing.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(r"vyos.*\.(?:qcow2|img|iso)$", re.IGNORECASE)


# Module-level registry of known-pristine sha256 digests. Test fixtures
# register their own pristine hashes via :func:`register_pristine_hash`.
_VYOS_PRISTINE_HASHES: set[str] = set()


def register_pristine_hash(digest_hex: str) -> None:
    """Register ``digest_hex`` as a known-pristine VyOS body hash."""
    _VYOS_PRISTINE_HASHES.add(digest_hex.lower())


def clear_pristine_hashes() -> None:
    """Clear the pristine-hash registry. Test-only."""
    _VYOS_PRISTINE_HASHES.clear()


def is_pristine_body(body: bytes) -> bool:
    """Return True iff the sha256 of ``body`` is in the pristine-hash registry."""
    return hashlib.sha256(body).hexdigest() in _VYOS_PRISTINE_HASHES


def vyos_status_for_path(path: Path) -> str:
    """Return ``"pristine"`` / ``"customised"`` / ``"unknown"`` for ``path``."""
    if not path.is_file():
        return "unknown"
    return "pristine" if is_pristine_body(path.read_bytes()) else "customised"


class VyOSAdapter(VendorAdapter):
    name: ClassVar[str] = "vyos"
    priority: ClassVar[int] = 70
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image"}

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        return bool(_IMAGE_RE.search(image))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image = str(raw["image"])
        # If the operator passed a vyos_template_path, classify it.
        template_path = raw.get("vyos_template_path")
        vyos_status = "unknown"
        if isinstance(template_path, (str, Path)):
            vyos_status = vyos_status_for_path(Path(template_path))
        return {
            "schema": 1,
            "id": str(raw.get("name") or image),
            "name": str(raw.get("name") or "VyOS"),
            "vendor": "vyos",
            "kind": "qemu",
            "image": image,
            "cpu": int(raw.get("cpu", 1)),
            "ram": int(raw.get("ram", 512)),
            "ethernet": int(raw.get("ethernet", 4)),
            "console": str(raw.get("console_type", "serial")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "vyos_status": vyos_status,
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
