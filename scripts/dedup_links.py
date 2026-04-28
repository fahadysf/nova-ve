#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""One-shot dedup script for existing labs — US-103.

Walks every ``*.json`` file under ``LABS_DIR``, collapses canonical-pair
duplicate links (keeps the entry with the lowest ``id`` string, drops the
rest), and writes the result back atomically.  Running twice is a no-op.

Pair-key logic is imported from ``app.services.link_utils`` — the same
canonical key used by the production POST /links duplicate check (US-102).

Usage::

    # dry-run (no files written):
    python scripts/dedup_links.py --labs-dir /var/lib/nova-ve/labs --dry-run

    # live run:
    sudo /home/ubuntu/nova-ve-git/.venv/bin/python scripts/dedup_links.py \\
        --labs-dir /var/lib/nova-ve/labs

Exit codes:
    0  — all labs processed (including labs that had nothing to dedup)
    1  — one or more labs could not be processed (errors printed to stderr)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure the backend package is importable when the script is run from the
# repo root (python scripts/dedup_links.py) or from backend/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Import the canonical dedup helper, lock, and lab JSON write surface.
from app.config import get_settings  # noqa: E402
from app.services.link_utils import _link_pair_key  # noqa: E402
from app.services.lab_service import LabService  # noqa: E402
from app.services.lab_lock import lab_lock  # noqa: E402


def _dedup_links(links: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Return (deduped_links, n_removed).

    Uses the same canonical pair-key logic as the production POST /links
    duplicate check (US-102).  Keeps the first occurrence of each pair
    (lowest lnk_NNN id) and drops subsequent duplicates.
    """
    seen: set[tuple] = set()
    kept: list[dict[str, Any]] = []
    removed = 0

    for link in links:
        ep_from = link.get("from")
        ep_to = link.get("to")
        if not isinstance(ep_from, dict) or not isinstance(ep_to, dict):
            # Malformed link — keep as-is, do not crash.
            kept.append(link)
            continue
        try:
            pair_key = _link_pair_key(ep_from, ep_to)
        except (ValueError, TypeError, KeyError):
            # Unparseable endpoints — keep as-is.
            kept.append(link)
            continue

        if pair_key not in seen:
            seen.add(pair_key)
            kept.append(link)
        else:
            removed += 1

    return kept, removed


def process_lab(
    lab_path: Path,
    labs_dir: Path,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """Process a single lab file.

    Returns ``(links_before, links_removed)``.
    Raises on I/O or JSON errors.
    """
    # lab_id is the path relative to labs_dir, POSIX-style.
    try:
        rel = lab_path.relative_to(labs_dir)
    except ValueError:
        rel = lab_path.name  # type: ignore[assignment]
    lab_id = Path(rel).as_posix()

    with lab_lock(lab_id, labs_dir):
        raw = lab_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)

        if not isinstance(data, dict) or data.get("schema") != 2:
            # Legacy v1 or non-lab file — skip silently.
            return 0, 0

        links: list[dict[str, Any]] = data.get("links") or []
        before = len(links)

        deduped, removed = _dedup_links(links)

        if removed == 0:
            return before, 0

        if dry_run:
            return before, removed

        data["links"] = deduped
        # Remove synthesized legacy shim so write does not regenerate links[].
        data.pop("topology", None)
        settings = get_settings()
        original_labs_dir = settings.LABS_DIR
        settings.LABS_DIR = labs_dir
        try:
            LabService.write_lab_json_static(lab_id, data)
        finally:
            settings.LABS_DIR = original_labs_dir

    return before, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--labs-dir",
        type=Path,
        default=Path("/var/lib/nova-ve/labs"),
        help="Path to the labs directory (default: /var/lib/nova-ve/labs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would change without writing any files",
    )
    args = parser.parse_args(argv)

    labs_dir: Path = args.labs_dir.resolve()
    dry_run: bool = args.dry_run

    if not labs_dir.is_dir():
        print(f"[dedup_links] ERROR: labs_dir does not exist: {labs_dir}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dedup_links] DRY RUN — no files will be written. labs_dir={labs_dir}")
    else:
        print(f"[dedup_links] Starting link dedup. labs_dir={labs_dir}")

    lab_files = sorted(labs_dir.rglob("*.json"))
    total_labs = 0
    total_removed = 0
    errors = 0

    for lab_path in lab_files:
        # Skip temp files accidentally globbed.
        if lab_path.stem.endswith(".tmp"):
            continue

        try:
            before, removed = process_lab(lab_path, labs_dir, dry_run=dry_run)
        except Exception as exc:
            print(
                f"[dedup_links] ERROR processing {lab_path}: {exc}",
                file=sys.stderr,
            )
            errors += 1
            continue

        if before == 0 and removed == 0:
            # Non-lab file or legacy schema — skip reporting.
            continue

        total_labs += 1
        total_removed += removed

        if removed > 0:
            action = "would remove" if dry_run else "removed"
            print(
                f"[dedup_links] {lab_path.name}: {action} {removed} duplicate link(s) "
                f"({before} → {before - removed})"
            )
        else:
            print(f"[dedup_links] {lab_path.name}: clean (no duplicates)")

    suffix = " (dry run)" if dry_run else ""
    print(
        f"[dedup_links] Done{suffix}. "
        f"labs={total_labs}, links_removed={total_removed}, errors={errors}"
    )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
