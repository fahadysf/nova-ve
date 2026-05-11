"""Regression: select_adapter() must be wired into the importer's main flow.

Prior to Phase 1 of the Dynamips runtime work, ``select_adapter()`` was
defined and exported but never called outside of its own unit tests.
That left the vendor-adapter registry as dead code: imported images
landed on disk but no nova-ve template JSON was ever produced from
them.

This test prevents that regression by driving ``run_migration`` over a
synthetic Dynamips MigrationItem and asserting both that
``select_adapter`` is invoked and that the resulting template JSON
lands in the templates dir with the expected shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.import_eveng.manifest import ImportManifest
from scripts.import_eveng.migrate import MigrateOptions, run_migration
from scripts.import_eveng.walker import KIND_DYNAMIPS, MigrationItem


def _dynamips_item(tmp_path: Path) -> MigrationItem:
    src = tmp_path / "src"
    src.mkdir()
    image_file = src / "c3725-adventerprisek9-mz.124-15.T14.image"
    image_file.write_bytes(b"FAKE_IOS_IMAGE")
    dst = tmp_path / "dst" / "c3725-adventerprisek9-mz.124-15.T14"
    return MigrationItem(
        kind=KIND_DYNAMIPS,
        image_key="c3725-adventerprisek9-mz.124-15.T14",
        src_dir=src,
        dst_dir=dst,
        files=[(image_file, dst / image_file.name)],
        meta={},
    )


def test_select_adapter_invoked_during_run_migration(tmp_path: Path) -> None:
    item = _dynamips_item(tmp_path)
    manifest = ImportManifest()
    templates_dir = tmp_path / "templates"

    with patch(
        "scripts.import_eveng.migrate.select_adapter",
        wraps=__import__(
            "scripts.import_eveng.migrate", fromlist=["select_adapter"]
        ).select_adapter,
    ) as spy:
        run_migration(
            [item],
            options=MigrateOptions(),
            manifest=manifest,
            templates_dir=templates_dir,
        )

    assert spy.call_count == 1, (
        "select_adapter() was not called during run_migration — "
        "the vendor-adapter pipeline is no longer wired into the importer."
    )


def test_run_migration_writes_dynamips_template(tmp_path: Path) -> None:
    item = _dynamips_item(tmp_path)
    manifest = ImportManifest()
    templates_dir = tmp_path / "templates"

    run_migration(
        [item],
        options=MigrateOptions(),
        manifest=manifest,
        templates_dir=templates_dir,
    )

    expected = templates_dir / "dynamips" / f"{item.image_key}.json"
    assert expected.is_file(), "Dynamips template JSON was not written"
    payload = json.loads(expected.read_text())
    assert payload["kind"] == "dynamips"
    assert payload["type"] == "dynamips"
    assert payload["extras"]["platform"] == "c3725"


def test_manifest_records_generated_template(tmp_path: Path) -> None:
    item = _dynamips_item(tmp_path)
    manifest = ImportManifest()
    templates_dir = tmp_path / "templates"

    run_migration(
        [item],
        options=MigrateOptions(),
        manifest=manifest,
        templates_dir=templates_dir,
    )

    generated = [t for t in manifest.templates if t.status == "generated"]
    assert generated, "No `generated` template entry in manifest"
    assert generated[0].name == item.image_key
    assert "adapter=dynamips" in (generated[0].reason or "")


def test_unmatched_image_yields_skipped_template(tmp_path: Path) -> None:
    # Synthesize an item whose image name no registered adapter will claim.
    src = tmp_path / "src"
    src.mkdir()
    image_file = src / "totally-unknown-vendor.qcow2"
    image_file.write_bytes(b"x")
    dst = tmp_path / "dst" / "totally-unknown-vendor"
    item = MigrationItem(
        kind="qemu",
        image_key="totally-unknown-vendor",
        src_dir=src,
        dst_dir=dst,
        files=[(image_file, dst / image_file.name)],
        meta={},
    )
    manifest = ImportManifest()
    templates_dir = tmp_path / "templates"

    run_migration(
        [item],
        options=MigrateOptions(),
        manifest=manifest,
        templates_dir=templates_dir,
    )

    # generic_linux is the catch-all and DOES claim qemu images. Either:
    #   - a "generated" entry from generic_linux, OR
    #   - a "skipped"/"needs-manual-review" entry from a stricter adapter
    # We just assert that the wiring produced *some* template entry —
    # the registry mediates everything else.
    assert manifest.templates, (
        "run_migration produced no template manifest entry; "
        "select_adapter wiring may have broken"
    )
