"""argparse entry point for the EVE-NG importer CLI (#183, #271).

This module wires the user-facing flag set, walks the source addon tree, and
dispatches to the migration orchestrator. The shell wrapper at
``deploy/scripts/import-eveng-templates.sh`` is the normal operator entrypoint.

Since #271 the default mode is **move + symlink back**: image files are renamed
into the nova-ve image store and a symlink is left at the original EVE-NG
path, so the bytes exist exactly once on disk and both trees keep working.
Terminal output is human-readable step-by-step progress on stdout plus an
appending log at ``/var/log/eve-ng-importer.log``; ``--json`` restores the
machine-readable event stream and ``--copy-only`` the pre-#271 copy+verify
behaviour.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from ._app_owner import AppOwnerError, resolve as resolve_app_owner
from .copy_engine import CopyMode
from .idempotency import evaluate
from .logging_setup import (
    DEFAULT_LOG_PATH,
    NULL_REPORTER,
    StepReporter,
    configure_logging,
    human_size,
    write_summary,
)
from .manifest import (
    ImportedEntry,
    ImportManifest,
    SkippedEntry,
)
from .migrate import MigrateOptions, effective_file_mode, run_migration
from .walker import KIND_DOCKER, MigrationItem, walk_all

DEFAULT_SOURCE = Path("/opt/unetlab")
DEFAULT_DEST = Path("/var/lib/nova-ve/images")
DEFAULT_MANIFEST = Path("/var/lib/nova-ve/import-manifest.json")

# CLI flag value -> engine mode. The CLI default ("move-symlink") is the #271
# behaviour; --copy-only selects the conservative pre-#271 copy+verify mode.
_MODE_FROM_FLAG = {
    "move-symlink": CopyMode.MOVE_SYMLINK,
    "copy-only": CopyMode.DEFAULT,
    "delete-source": CopyMode.DELETE_SOURCE,
    "move": CopyMode.MOVE,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_eveng",
        description=(
            "Import an EVE-NG / UNetLab / PNETLab tree into the nova-ve image "
            "layout (umbrella #182). Default mode (#271) moves image files "
            "into the destination and symlinks the original EVE-NG path back "
            "at the moved file — no duplicate bytes, and EVE-NG keeps booting "
            "its nodes. Cross-filesystem sources fall back to copy + sha256 "
            "verify."
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
        "--templates-dir",
        type=Path,
        default=Path("/var/lib/nova-ve/templates"),
        help=(
            "Destination for nova-ve template YAML produced by the "
            "vendor-adapter pipeline. Pass an empty path or '-' to skip "
            "template generation. Default: /var/lib/nova-ve/templates."
        ),
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

    # The four copy strategies are mutually exclusive — they are competing
    # answers to "what happens to the source file" (#271).
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--move-symlink",
        action="store_const",
        dest="mode",
        const="move-symlink",
        help=(
            "(default) move image files into <dest> and leave a symlink at "
            "the original path; cross-filesystem sources fall back to copy "
            "+ sha256 verify."
        ),
    )
    mode.add_argument(
        "--copy-only",
        action="store_const",
        dest="mode",
        const="copy-only",
        help=(
            "conservative opt-out: copy + sha256 verify, sources preserved "
            "(the pre-#271 default)."
        ),
    )
    mode.add_argument(
        "--delete-source",
        action="store_const",
        dest="mode",
        const="delete-source",
        help=(
            "copy + sha256 verify, then delete the source file. Explicit "
            "opt-in for destructive moves."
        ),
    )
    mode.add_argument(
        "--move",
        action="store_const",
        dest="mode",
        const="move",
        help=(
            "copy + delete source, SKIP sha256 verify. UNSAFE: only use when "
            "you have already verified the source tree elsewhere."
        ),
    )
    parser.set_defaults(mode="move-symlink")

    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Machine-readable output: one JSON log event per line on stderr "
            "instead of the human step-by-step stdout."
        ),
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"Appending log file for every run (default: {DEFAULT_LOG_PATH}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (log file, and stderr under --json).",
    )
    return parser


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _plan_step_count(
    items: Sequence[MigrationItem], *, templates_enabled: bool, fixup: bool
) -> int:
    """Number of step lines a run will emit (files/builds + templates + fixup)."""
    total = 0
    for item in items:
        total += max(len(item.files), 1)  # docker items have no files -> 1 build step
        if templates_enabled:
            total += 1
    if fixup:
        total += 1
    return total


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logger = configure_logging(
        verbose=args.verbose, json_stream=args.json, log_path=args.log_file
    )

    if not args.dry_run and not _is_root():
        print(
            "import_eveng: must run as root unless --dry-run is passed.",
            file=sys.stderr,
        )
        return 2

    mode = _MODE_FROM_FLAG[args.mode]

    logger.info(
        "importer.start",
        extra={
            "source": str(args.source),
            "dest": str(args.dest),
            "dry_run": args.dry_run,
            "force": args.force,
            "mode": mode.value,
            "json": args.json,
            "log_file": str(args.log_file),
        },
    )

    manifest = ImportManifest()

    items = walk_all(args.source, args.dest) if args.source.exists() else []
    logger.info("importer.walk_complete", extra={"items": len(items)})

    templates_dir = args.templates_dir
    # Operator opt-out: pass '-' or an empty path to skip generation.
    if templates_dir is not None and str(templates_dir) in {"", "-"}:
        templates_dir = None

    reporter = StepReporter(
        sys.stdout,
        _plan_step_count(
            items,
            templates_enabled=templates_dir is not None and not args.dry_run,
            fixup=not args.dry_run,
        ),
        enabled=not args.json,
    )

    if args.dry_run:
        _emit_dry_run_plan(items, mode, manifest, reporter=reporter, force=args.force)
    else:
        try:
            owner = resolve_app_owner()
        except AppOwnerError as exc:
            print(f"import_eveng: {exc}", file=sys.stderr)
            return 3
        logger.info(
            "importer.app_owner_resolved",
            extra={
                "owner": owner.name,
                "group": owner.group,
                "uid": owner.uid,
                "gid": owner.gid,
                "source": owner.source,
            },
        )
        options = MigrateOptions(mode=mode, force=args.force)
        run_migration(
            items,
            options=options,
            manifest=manifest,
            owner=owner,
            dest_root=args.dest,
            templates_dir=templates_dir,
            reporter=reporter,
        )

    manifest.mark_finished()
    if not args.dry_run:
        manifest.write(args.manifest)
    else:
        logger.info(
            "importer.dry_run.no_manifest_write",
            extra={"would_write": str(args.manifest)},
        )

    write_summary(
        sys.stdout,
        manifest.to_dict(),
        manifest_path=str(args.manifest),
        log_path=str(args.log_file),
    )
    logger.info(
        "importer.summary",
        extra={
            "imported": len(manifest.imported),
            "skipped": len(manifest.skipped),
            "errors": len(manifest.errors),
            "manifest": str(args.manifest),
        },
    )
    logger.info("importer.done", extra={"manifest": str(args.manifest)})
    return 0


def _emit_dry_run_plan(
    items,
    mode: CopyMode,
    manifest: ImportManifest,
    reporter: StepReporter | None = None,
    *,
    force: bool = False,
) -> None:
    """Populate ``manifest`` with would-imported / would-skipped entries.

    Walks every item's planned files and checks for existing matches;
    items with no source files (e.g. ``docker`` build contexts) emit a single
    ``imported`` entry pointing at the planned image marker. Each planned
    action renders one ``would ...`` step line (#271).
    """
    reporter = reporter or NULL_REPORTER
    for item in items:
        if not item.files and item.kind == KIND_DOCKER:
            tag = str(item.meta.get("image_tag", f"nova-ve/{item.image_key}:latest"))
            with reporter.step(
                f"docker/{item.image_key} — would build image {tag}"
            ) as step:
                manifest.imported.append(
                    ImportedEntry(
                        src=str(item.src_dir),
                        dst=str(item.dst_dir / "image.txt"),
                        sha256="",
                        bytes=0,
                        mode=f"dry-run/{mode.value}",
                    )
                )
                step.skip("dry-run — no changes made")
            continue
        for src, dst in item.files:
            file_mode = effective_file_mode(item, src, mode)
            verb = "moving" if file_mode is CopyMode.MOVE_SYMLINK else "copying"
            try:
                size = f" ({human_size(src.stat().st_size)})"
            except OSError:
                size = ""
            with reporter.step(
                f"{item.kind}/{item.image_key} — would be {verb} {src.name}{size}"
            ) as step:
                decision = evaluate(src, dst, verify_full=force)
                if decision.skip:
                    manifest.skipped.append(
                        SkippedEntry(src=str(src), dst=str(dst), reason=decision.reason)
                    )
                    step.skip(decision.reason)
                else:
                    manifest.imported.append(
                        ImportedEntry(
                            src=str(src),
                            dst=str(dst),
                            sha256=decision.src_sha256 or "",
                            bytes=src.stat().st_size if src.exists() else 0,
                            mode=f"dry-run/{mode.value}",
                        )
                    )
                    step.skip("dry-run — no changes made")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
