"""Tests for the EVE-NG importer CLI scaffold (#183)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from scripts.import_eveng import main as package_main
from scripts.import_eveng._hash import sha256_file, sha256_stream
from scripts.import_eveng.cli import (
    DEFAULT_DEST,
    DEFAULT_MANIFEST,
    DEFAULT_SOURCE,
    build_parser,
    main,
)
from scripts.import_eveng.idempotency import evaluate
from scripts.import_eveng.manifest import (
    ErrorEntry,
    ImportManifest,
    ImportedEntry,
    MANIFEST_VERSION,
    SkippedEntry,
    TemplateEntry,
)


# --- argparse defaults ---------------------------------------------------


def test_argparse_defaults_match_gh_body_verbatim() -> None:
    """Default flag values must match GH #183 verbatim."""
    parser = build_parser()
    args = parser.parse_args([])
    assert args.source == DEFAULT_SOURCE == Path("/opt/unetlab")
    assert args.dest == DEFAULT_DEST == Path("/var/lib/nova-ve/images")
    assert args.manifest == DEFAULT_MANIFEST == Path("/var/lib/nova-ve/import-manifest.json")
    assert args.dry_run is False
    assert args.force is False
    assert args.copy_only is False
    assert args.move is False
    assert args.delete_source is False
    assert args.verbose is False


def test_copy_only_and_move_are_mutually_exclusive() -> None:
    """argparse must reject --copy-only and --move passed together."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--copy-only", "--move"])


def test_dry_run_flag_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(["--dry-run", "--verbose", "--force"])
    assert args.dry_run is True
    assert args.verbose is True
    assert args.force is True


def test_explicit_paths_override_defaults(tmp_path: Path) -> None:
    parser = build_parser()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    mf = tmp_path / "m.json"
    args = parser.parse_args(
        ["--source", str(src), "--dest", str(dst), "--manifest", str(mf)]
    )
    assert args.source == src
    assert args.dest == dst
    assert args.manifest == mf


# --- package-level import ------------------------------------------------


def test_package_main_is_importable_with_no_side_effects() -> None:
    """`from scripts.import_eveng import main` must work and be the cli.main."""
    assert package_main is main


# --- manifest schema -----------------------------------------------------


def test_manifest_dataclass_round_trips_through_json(tmp_path: Path) -> None:
    manifest = ImportManifest(
        imported=[
            ImportedEntry(src="/a", dst="/b", sha256="deadbeef", bytes=42, mode="default"),
        ],
        templates=[
            TemplateEntry(name="vyos-1.4", status="ok", json="/var/lib/nova-ve/templates/vyos-1.4.json"),
            TemplateEntry(
                name="exotic",
                status="needs-manual-review",
                reason="qemu_options uses unrecognised -device tree",
                eveng_raw={"qemu_options": "-device foo"},
            ),
        ],
        skipped=[SkippedEntry(src="/x", dst="/y", reason="exists, sha256 match")],
        errors=[ErrorEntry(path="/z", error="permission denied")],
    )
    manifest.mark_finished()

    out = tmp_path / "manifest.json"
    manifest.write(out)

    raw = json.loads(out.read_text())
    assert raw["version"] == MANIFEST_VERSION
    assert raw["started_at"] is not None
    assert raw["finished_at"] is not None
    assert {"imported", "templates", "skipped", "errors"} <= set(raw.keys())
    assert raw["imported"][0]["sha256"] == "deadbeef"
    assert raw["imported"][0]["mode"] == "default"
    assert raw["templates"][1]["status"] == "needs-manual-review"
    assert raw["templates"][1]["_eveng_raw"]["qemu_options"] == "-device foo"
    assert raw["skipped"][0]["reason"] == "exists, sha256 match"
    assert raw["errors"][0]["error"] == "permission denied"

    parsed = ImportManifest.read(out)
    assert parsed.to_dict() == manifest.to_dict()


def test_empty_manifest_has_all_four_top_level_keys(tmp_path: Path) -> None:
    """Per GH #183: --dry-run against empty source produces a manifest with all empty arrays."""
    manifest = ImportManifest()
    manifest.mark_finished()
    out = tmp_path / "manifest.json"
    manifest.write(out)
    raw = json.loads(out.read_text())
    assert raw["imported"] == []
    assert raw["templates"] == []
    assert raw["skipped"] == []
    assert raw["errors"] == []


# --- sha256 streaming helper --------------------------------------------


def test_sha256_stream_chunked_matches_one_shot(tmp_path: Path) -> None:
    """Streaming over arbitrary chunk sizes must yield the same digest."""
    payload = b"nova-ve-eveng-importer-#183" * 4096  # ~100 KiB
    f = tmp_path / "blob"
    f.write_bytes(payload)

    expected = sha256_file(f)
    assert len(expected) == 64

    # Single-byte chunks must agree with default chunk size.
    one_byte = sha256_stream(io.BytesIO(payload), chunk_size=1)
    assert one_byte == expected


def test_sha256_file_distinguishes_changed_bytes(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"hello")
    b.write_bytes(b"hello!")
    assert sha256_file(a) != sha256_file(b)


# --- idempotency primitive ----------------------------------------------


def test_idempotency_skip_when_dst_exists_and_sha_matches(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_bytes(b"identical")
    dst.write_bytes(b"identical")

    decision = evaluate(src, dst)
    assert decision.skip is True
    assert decision.reason == "exists, sha256 match"
    assert decision.src_sha256 == decision.dst_sha256


def test_idempotency_no_skip_when_dst_missing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_bytes(b"new content")
    decision = evaluate(src, dst)
    assert decision.skip is False
    assert "destination missing" in decision.reason


def test_idempotency_no_skip_on_sha_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_bytes(b"old")
    dst.write_bytes(b"new")
    decision = evaluate(src, dst)
    assert decision.skip is False
    assert "sha256 mismatch" in decision.reason
    assert "--force" in decision.reason


# --- main() integration: --dry-run is a no-op against empty source ------


def test_main_dry_run_against_empty_source_writes_no_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "empty"
    src.mkdir()
    dst = tmp_path / "dst"
    mf = tmp_path / "import-manifest.json"

    rc = main(
        [
            "--source",
            str(src),
            "--dest",
            str(dst),
            "--manifest",
            str(mf),
            "--dry-run",
        ]
    )
    assert rc == 0
    # Per GH body: --dry-run does not touch the destination filesystem.
    assert not mf.exists(), "dry-run must not write the manifest"

    out = capsys.readouterr().out
    assert "run summary" in out


def test_main_real_run_writes_manifest_with_empty_arrays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real (non-dry-run) invocation against an empty source still emits a shaped manifest."""
    from scripts.import_eveng._app_owner import AppOwner

    src = tmp_path / "empty"
    src.mkdir()
    dst = tmp_path / "dst"
    mf = tmp_path / "import-manifest.json"

    # Pretend we are root so the root-check does not bail.
    monkeypatch.setattr("scripts.import_eveng.cli._is_root", lambda: True)

    # Stub APP_OWNER resolution: macOS test runners do not have a 'ubuntu'
    # user, but the production target (Ubuntu 26.04 host) does.
    fake_owner = AppOwner(
        name="ubuntu", uid=1000, group="ubuntu", gid=1000, home="/home/ubuntu", source="default"
    )
    monkeypatch.setattr("scripts.import_eveng.cli.resolve_app_owner", lambda: fake_owner)

    rc = main(
        [
            "--source",
            str(src),
            "--dest",
            str(dst),
            "--manifest",
            str(mf),
        ]
    )
    assert rc == 0
    assert mf.exists()
    raw = json.loads(mf.read_text())
    assert raw["imported"] == []
    assert raw["templates"] == []
    assert raw["skipped"] == []
    assert raw["errors"] == []


def test_main_non_root_real_run_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-dry-run without root must exit non-zero with a clear stderr message."""
    src = tmp_path / "empty"
    src.mkdir()
    dst = tmp_path / "dst"
    mf = tmp_path / "import-manifest.json"

    monkeypatch.setattr("scripts.import_eveng.cli._is_root", lambda: False)

    rc = main(
        [
            "--source",
            str(src),
            "--dest",
            str(dst),
            "--manifest",
            str(mf),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "must run as root" in err
    assert not mf.exists()


def test_main_dry_run_skips_root_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run must not require root (so operators can plan without sudo)."""
    src = tmp_path / "empty"
    src.mkdir()
    monkeypatch.setattr("scripts.import_eveng.cli._is_root", lambda: False)

    rc = main(["--source", str(src), "--dry-run"])
    assert rc == 0
