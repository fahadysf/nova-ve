#!/usr/bin/env python3
"""Create dummy node images for the UI sandbox (#252, #253).

Walks the builtin qemu/iol/dynamips templates and, for each one, creates an
empty (zero-byte) image file in ``IMAGES_DIR/<type>/`` whose stem contains the
template's lowercased display name. ``TemplateService._matches_template_image_name``
matches by substring, so each dummy shows up as an available image for its own
template — enough for the add-node modal and creation-time validation to work
in the sandbox without any real images. Nodes created from them never launch
(sandbox mode start guard in node_runtime_service).

Idempotent and conservative: existing files are never overwritten or removed,
so real images deployed later are untouched.

Usage:
    cd backend && python -m scripts.sandbox_images

Environment:
    IMAGES_DIR     target images dir (default /var/lib/nova-ve/images)
    TEMPLATES_DIR  builtin templates dir (default <repo>/backend/templates)
"""

import os
import sys
from pathlib import Path

import yaml

# Per-type file extensions. Only the stem matters for catalog matching;
# realistic extensions keep the picker and any future runtime plumbing sane.
_EXTENSIONS = {
    "qemu": ".qcow2",
    "iol": ".bin",
    "dynamips": ".image",
}

# Template type dirs that map to filesystem image listings. Docker is
# excluded: its catalog comes from the daemon (synthetic in sandbox mode).
_TEMPLATE_TYPES = ("qemu", "iol", "dynamips")


def _templates_dir() -> Path:
    raw = os.environ.get("TEMPLATES_DIR", "").strip()
    if raw:
        return Path(raw)
    # scripts/sandbox_images.py -> backend/templates
    return Path(__file__).resolve().parent.parent / "templates"


def _images_dir() -> Path:
    return Path(
        os.environ.get("IMAGES_DIR", "/var/lib/nova-ve/images")
    ).resolve()


def _safe_stem(name: str) -> str:
    """Lowercased template name made filename-safe, plus the sandbox marker."""
    cleaned = name.strip().lower()
    for ch in ("/", "\\", "\x00"):
        cleaned = cleaned.replace(ch, "-")
    return f"{cleaned} sandbox"


def _template_candidates(path: Path) -> list[str]:
    """Return display names usable as image stems for one template file."""
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as exc:  # noqa: BLE001 — one bad template must not abort the walk
        print(f"[sandbox-images] WARNING: cannot parse {path.name}: {exc}", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        return []
    # Paired templates instantiate multiple nodes — no single dummy applies.
    if str(data.get("kind") or "").strip().lower() == "paired":
        return []
    if data.get("paired_parent"):
        return []
    name = str(data.get("name") or "").strip()
    return [name] if name else []


def main() -> int:
    templates_root = _templates_dir()
    images_root = _images_dir()
    created = 0
    skipped = 0

    for template_type in _TEMPLATE_TYPES:
        ext = _EXTENSIONS[template_type]
        type_dir = templates_root / template_type
        if not type_dir.is_dir():
            continue
        out_dir = images_root / template_type
        out_dir.mkdir(parents=True, exist_ok=True)
        for template_file in sorted(type_dir.glob("*.y*ml")):
            for name in _template_candidates(template_file):
                target = out_dir / f"{_safe_stem(name)}{ext}"
                if target.exists():
                    skipped += 1
                    continue
                target.touch()
                created += 1

    print(
        f"[sandbox-images] dummy images: created={created} existing={skipped} "
        f"under {images_root}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
