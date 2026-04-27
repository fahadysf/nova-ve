#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-082 one-shot fixer: backfill ``schema_version: 2`` on v2 lab.json files.

Real-browser testing on the deployed stack revealed that lab.json files on
disk had ``schema_version: null`` despite carrying full v2-shape data
(``links[]``, dict-typed ``networks``, etc.). New writes from
``LabService.write_lab_json_static`` set the field correctly, but existing
labs need a one-time backfill.

Usage::

    python scripts/fix_schema_version.py [--dry-run] [--labs-dir /path]

If ``--labs-dir`` is omitted, the script falls back (in order) to:
  1. ``backend.app.config.get_settings().LABS_DIR`` if importable.
  2. ``/var/lib/nova-ve/labs`` (the production default).

Files are skipped when:
  - they already declare ``schema_version: 2``;
  - they are not v2-shape (no ``links`` field AND networks is not a dict).

Files are mutated when ``schema_version`` is missing or null AND the file is
v2-shape. The mutation is a single-key set; the rest of the JSON is preserved
verbatim aside from ``json.dump`` formatting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = 2

# Add the backend directory to sys.path so the optional config import succeeds
# regardless of how the script is invoked (CLI vs ``python -m``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _default_labs_dir() -> Path:
    """Resolve the default labs dir, falling back to the production path."""
    try:
        from app.config import get_settings  # type: ignore

        return Path(get_settings().LABS_DIR).resolve()
    except Exception:
        return Path("/var/lib/nova-ve/labs")


def _has_v2_shape(data: dict) -> bool:
    """Mirror of ``app.services.lab_service._has_v2_shape``.

    Inlined so the script stays usable even when the backend package is not
    importable (e.g. running the fixer on a stripped-down deployment image).
    """

    if not isinstance(data, dict):
        return False
    if "links" in data:
        return True
    networks = data.get("networks")
    return isinstance(networks, dict)


def _needs_fix(data: dict) -> bool:
    if not _has_v2_shape(data):
        return False
    return data.get("schema_version") != SCHEMA_VERSION


def _write_via_lab_service(filename: str, data: dict, labs_dir: Path) -> bool:
    """Try the LabService writer (preferred — same flock + formatting).

    Returns True on success, False if the writer is not importable or the
    settings.LABS_DIR doesn't match the requested ``labs_dir`` (in which case
    we fall through to manual JSON dump).
    """

    try:
        os.environ.setdefault("LABS_DIR", str(labs_dir))
        from app.config import get_settings  # type: ignore
        from app.services.lab_service import LabService  # type: ignore

        if Path(get_settings().LABS_DIR).resolve() != labs_dir.resolve():
            return False
        LabService.write_lab_json_static(filename, data)
        return True
    except Exception:
        return False


def _write_manual(path: Path, data: dict) -> None:
    """Manual JSON dump matching ``LabService.write_lab_json_static`` formatting."""
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)


def fix_lab_file(path: Path, labs_dir: Path, dry_run: bool) -> bool:
    """Process a single lab.json file. Returns True if it was (or would be) modified."""

    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[skip] {path}: cannot parse JSON ({exc})", file=sys.stderr)
        return False

    if not _needs_fix(data):
        return False

    if dry_run:
        print(f"[dry-run] would set schema_version=2 on {path}")
        return True

    data["schema_version"] = SCHEMA_VERSION
    relative = path.relative_to(labs_dir).as_posix()
    if not _write_via_lab_service(relative, data, labs_dir):
        _write_manual(path, data)
    print(f"[fixed] schema_version=2 on {path}")
    return True


def run(labs_dir: Path, dry_run: bool) -> int:
    if not labs_dir.exists():
        print(f"[error] labs dir does not exist: {labs_dir}", file=sys.stderr)
        return 1

    fixed = 0
    scanned = 0
    for path in sorted(labs_dir.rglob("*.json")):
        scanned += 1
        if fix_lab_file(path, labs_dir, dry_run):
            fixed += 1

    label = "would-fix" if dry_run else "fixed"
    print(f"[done] scanned={scanned} {label}={fixed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill schema_version=2 on v2-shape lab.json files."
    )
    parser.add_argument(
        "--labs-dir",
        type=Path,
        default=None,
        help="Root directory containing lab.json files (default: backend settings or /var/lib/nova-ve/labs).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without modifying any files.",
    )
    args = parser.parse_args(argv)

    labs_dir = args.labs_dir.resolve() if args.labs_dir else _default_labs_dir()
    return run(labs_dir, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
