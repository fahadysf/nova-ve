"""Tests for the EVE-NG importer CLI scaffold (#183) + #271 mode/output flags."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from scripts.import_eveng import main as package_main
from scripts.import_eveng._app_owner import AppOwner
from scripts.import_eveng._hash import sha256_file, sha256_stream
from scripts.import_eveng.cli import (
    DEFAULT_DEST,
    DEFAULT_LOG_PATH,
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


def test_argparse_defaults_match_issue_contract() -> None:
    """Paths per GH #183; mode + log defaults per #271 (move-symlink default)."""
    parser = build_parser()
    args = parser.parse_args([])
    assert args.source == DEFAULT_SOURCE == Path("/opt/unetlab")
    assert args.dest == DEFAULT_DEST == Path("/var/lib/nova-ve/images")
    assert args.manifest == DEFAULT_MANIFEST == Path("/var/lib/nova-ve/import-manifest.json")
    assert args.dry_run is False
    assert args.force is False
    assert args.mode == "move-symlink"
    assert args.json is False
    assert args.log_file == DEFAULT_LOG_PATH == Path("/var/log/eve-ng-importer.log")
    assert args.verbose is False


def test_mode_flags_are_mutually_exclusive() -> None:
    """The copy strategies compete for the source file; argparse must reject combos."""
    parser = build_parser()
    for combo in (
        ["--copy-only", "--move"],
        ["--move-symlink", "--delete-source"],
        ["--move-symlink", "--copy-only"],
        ["--copy-only", "--delete-source"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(combo)


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
    templates = tmp_path / "templates"

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
    templates = tmp_path / "templates"

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
            "--templates-dir",
            str(templates),
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


# --- #271: move+symlink default, step output, log file --------------------


def _fake_owner() -> AppOwner:
    """Owner pointing at the current process uid/gid (macOS test runners safe)."""
    return AppOwner(
        name="ubuntu",
        uid=os.getuid(),
        group="ubuntu",
        gid=os.getgid(),
        home="/home/ubuntu",
        source="default",
    )


def _seed_qemu_vendor(src_root: Path, image_key: str = "vyos-1.4") -> Path:
    vendor = src_root / "addons" / "qemu" / image_key
    vendor.mkdir(parents=True)
    (vendor / "hda.qcow2").write_bytes(b"vyos image bytes")
    return vendor / "hda.qcow2"


def test_main_default_mode_moves_and_symlinks_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default run (#271): the image is moved, the EVE-NG path becomes a symlink,
    stdout shows numbered step lines, and the run appends to the log file."""
    src_root = tmp_path / "opt-unetlab"
    disk = _seed_qemu_vendor(src_root)
    dst = tmp_path / "images"
    mf = tmp_path / "import-manifest.json"
    templates = tmp_path / "templates"
    log = tmp_path / "eve-ng-importer.log"

    monkeypatch.setattr("scripts.import_eveng.cli._is_root", lambda: True)
    monkeypatch.setattr("scripts.import_eveng.cli.resolve_app_owner", _fake_owner)

    rc = main(
        [
            "--source", str(src_root),
            "--dest", str(dst),
            "--manifest", str(mf),
            "--templates-dir", str(templates),
            "--log-file", str(log),
        ]
    )
    assert rc == 0

    moved = dst / "qemu" / "vyos-1.4" / "hda.qcow2"
    assert moved.is_file() and not moved.is_symlink()
    assert moved.read_bytes() == b"vyos image bytes"
    assert disk.is_symlink()
    assert os.path.realpath(disk) == str(moved)

    out = capsys.readouterr().out
    assert "[1/" in out, "numbered step lines expected on stdout"
    assert "moving hda.qcow2" in out
    assert "symlinked back to" in out
    assert "generating template YAML" in out
    assert f"log                : {log}" in out

    log_text = log.read_text()
    assert "importer.step" in log_text
    assert "run summary" in out

    raw = json.loads(mf.read_text())
    entry = next(e for e in raw["imported"] if e["dst"].endswith("hda.qcow2"))
    assert entry["mode"] == "move-symlink"
    assert entry["symlinked"] is True
    assert "symlinked back to" in entry["note"]


def test_main_copy_only_preserves_sources_as_real_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--copy-only opts out of the #271 default: plain copy, source untouched."""
    src_root = tmp_path / "opt-unetlab"
    disk = _seed_qemu_vendor(src_root)
    dst = tmp_path / "images"
    mf = tmp_path / "import-manifest.json"

    monkeypatch.setattr("scripts.import_eveng.cli._is_root", lambda: True)
    monkeypatch.setattr("scripts.import_eveng.cli.resolve_app_owner", _fake_owner)

    rc = main(
        [
            "--source", str(src_root),
            "--dest", str(dst),
            "--manifest", str(mf),
            "--templates-dir", "-",
            "--copy-only",
            "--log-file", str(tmp_path / "importer.log"),
        ]
    )
    assert rc == 0
    assert disk.is_file() and not disk.is_symlink()
    assert (dst / "qemu" / "vyos-1.4" / "hda.qcow2").read_bytes() == b"vyos image bytes"


def test_main_json_mode_suppresses_step_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--json restores the machine-readable stream and hides the step lines."""
    src_root = tmp_path / "opt-unetlab"
    _seed_qemu_vendor(src_root)

    monkeypatch.setattr("scripts.import_eveng.cli._is_root", lambda: False)
    rc = main(
        [
            "--source", str(src_root),
            "--dest", str(tmp_path / "dst"),
            "--manifest", str(tmp_path / "m.json"),
            "--dry-run",
            "--json",
            "--log-file", str(tmp_path / "importer.log"),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "[1/" not in captured.out, "human step lines must be suppressed under --json"
    assert "run summary" in captured.out
    assert '"message"' in captured.err, "JSON events expected on stderr under --json"


def test_main_dry_run_prints_human_plan_without_touching_dest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run narrates the planned move+symlink but mutates nothing."""
    src_root = tmp_path / "opt-unetlab"
    disk = _seed_qemu_vendor(src_root)
    dst = tmp_path / "images"

    monkeypatch.setattr("scripts.import_eveng.cli._is_root", lambda: False)
    rc = main(
        [
            "--source", str(src_root),
            "--dest", str(dst),
            "--manifest", str(tmp_path / "m.json"),
            "--dry-run",
            "--log-file", str(tmp_path / "importer.log"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "would be moving hda.qcow2" in out
    assert "dry-run — no changes made" in out
    assert disk.is_file() and not disk.is_symlink()
    assert not dst.exists()
