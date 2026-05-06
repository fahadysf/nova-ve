"""argparse entry point for the EVE-NG importer CLI (#183).

This module wires the user-facing flag set; #184 will plug in the walker and
copy logic. Today the CLI only emits an empty manifest in ``--dry-run`` mode and
exits 0 — sufficient to integration-test the scaffold and the manifest schema.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from .logging_setup import configure_logging, write_summary
from .manifest import ImportManifest

DEFAULT_SOURCE = Path("/opt/unetlab")
DEFAULT_DEST = Path("/var/lib/nova-ve/images")
DEFAULT_MANIFEST = Path("/var/lib/nova-ve/import-manifest.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_eveng",
        description=(
            "Import an EVE-NG / UNetLab / PNETLab tree into the nova-ve image "
            "layout (umbrella #182). Default mode is non-destructive copy + "
            "sha256 verify; source files are preserved unless --delete-source "
            "is passed."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"EVE-NG install root (default: {DEFAULT_SOURCE}).",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"nova-ve images destination (default: {DEFAULT_DEST}).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Where to write the run manifest JSON (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the run without touching the destination filesystem.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing destination files when sha256 differs.",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--copy-only",
        dest="copy_only",
        action="store_true",
        help=(
            "Default behaviour: copy + sha256 verify; sources preserved. "
            "Accepted for symmetry with --move; passing --copy-only is "
            "equivalent to passing no copy-mode flag."
        ),
    )
    mode.add_argument(
        "--move",
        dest="move",
        action="store_true",
        help=(
            "Skip sha256 verify, copy + delete source. UNSAFE: only use when "
            "you have already verified the source tree elsewhere."
        ),
    )

    parser.add_argument(
        "--delete-source",
        dest="delete_source",
        action="store_true",
        help=(
            "After sha256 verification at the destination, delete the source "
            "file. This is the explicit opt-in for destructive moves; the "
            "default leaves sources intact."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level structured logging on stderr.",
    )
    return parser


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logger = configure_logging(verbose=args.verbose)

    if not args.dry_run and not _is_root():
        print(
            "import_eveng: must run as root unless --dry-run is passed.",
            file=sys.stderr,
        )
        return 2

    logger.info(
        "importer.start",
        extra={
            "source": str(args.source),
            "dest": str(args.dest),
            "dry_run": args.dry_run,
            "force": args.force,
            "move": args.move,
            "delete_source": args.delete_source,
            "copy_only": args.copy_only,
        },
    )

    manifest = ImportManifest()

    if args.dry_run and not args.source.exists():
        logger.info("importer.dry_run.empty", extra={"source": str(args.source)})

    manifest.mark_finished()
    if not args.dry_run:
        manifest.write(args.manifest)
    else:
        logger.info(
            "importer.dry_run.no_manifest_write",
            extra={"would_write": str(args.manifest)},
        )

    write_summary(sys.stdout, manifest.to_dict(), manifest_path=str(args.manifest))
    logger.info("importer.done", extra={"manifest": str(args.manifest)})
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
