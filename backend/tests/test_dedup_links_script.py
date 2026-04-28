# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-103 — Tests for scripts/dedup_links.py.

Strategy: import the script module directly (not subprocess) so tests run
fast and get proper pytest tracebacks.  The process_lab() and
_dedup_links_v2() functions are the units under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the repo scripts/ directory importable.
# File lives at backend/tests/test_dedup_links_script.py → parents[2] = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dedup_links  # noqa: E402 — imported after sys.path setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_link(
    link_id: str,
    from_ep: dict[str, Any],
    to_ep: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": link_id,
        "from": from_ep,
        "to": to_ep,
        "style_override": None,
        "label": "",
        "color": "",
        "width": "1",
        "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
    }


def _seed_lab(
    labs_dir: Path,
    name: str = "lab.json",
    links: list[dict[str, Any]] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema": 2,
        "id": name.replace(".json", ""),
        "meta": {"name": name},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {},
        "networks": {},
        "links": links or [],
        "defaults": {"link_style": "orthogonal"},
    }
    lab_path = labs_dir / name
    lab_path.write_text(json.dumps(payload), encoding="utf-8")
    return lab_path


# ---------------------------------------------------------------------------
# Unit tests for _dedup_links_v2
# ---------------------------------------------------------------------------


def test_dedup_no_duplicates():
    """Clean lab with no duplicates — zero removed."""
    links = [
        _make_link("lnk_001", {"node_id": 1, "interface_index": 0}, {"network_id": 5}),
        _make_link("lnk_002", {"node_id": 2, "interface_index": 0}, {"network_id": 5}),
    ]
    deduped, removed = dedup_links._dedup_links(links)
    assert removed == 0
    assert len(deduped) == 2


def test_dedup_identical_pair():
    """A→B followed by A→B keeps first, removes second."""
    ep_a = {"node_id": 1, "interface_index": 0}
    ep_b = {"network_id": 5}
    links = [
        _make_link("lnk_001", ep_a, ep_b),
        _make_link("lnk_002", ep_a, ep_b),
    ]
    deduped, removed = dedup_links._dedup_links(links)
    assert removed == 1
    assert len(deduped) == 1
    assert deduped[0]["id"] == "lnk_001"


def test_dedup_reversed_pair():
    """B→A after A→B is detected as a duplicate (bidirectional)."""
    ep_a = {"node_id": 1, "interface_index": 0}
    ep_b = {"network_id": 5}
    links = [
        _make_link("lnk_001", ep_a, ep_b),
        _make_link("lnk_002", ep_b, ep_a),  # reversed
    ]
    deduped, removed = dedup_links._dedup_links(links)
    assert removed == 1
    assert deduped[0]["id"] == "lnk_001"


def test_dedup_keeps_first_occurrence():
    """When three copies exist, only the first is kept."""
    ep_a = {"node_id": 3, "interface_index": 1}
    ep_b = {"network_id": 7}
    links = [
        _make_link("lnk_001", ep_a, ep_b),
        _make_link("lnk_002", ep_a, ep_b),
        _make_link("lnk_003", ep_b, ep_a),
    ]
    deduped, removed = dedup_links._dedup_links(links)
    assert removed == 2
    assert len(deduped) == 1
    assert deduped[0]["id"] == "lnk_001"


def test_dedup_distinct_pairs_untouched():
    """Two different node→network pairs are both kept."""
    links = [
        _make_link("lnk_001", {"node_id": 1, "interface_index": 0}, {"network_id": 5}),
        _make_link("lnk_002", {"node_id": 2, "interface_index": 0}, {"network_id": 5}),
        _make_link("lnk_003", {"node_id": 1, "interface_index": 1}, {"network_id": 6}),
    ]
    deduped, removed = dedup_links._dedup_links(links)
    assert removed == 0
    assert len(deduped) == 3


def test_dedup_empty_links():
    """Empty links list is a no-op."""
    deduped, removed = dedup_links._dedup_links([])
    assert removed == 0
    assert deduped == []


# ---------------------------------------------------------------------------
# Integration tests using process_lab()
# ---------------------------------------------------------------------------


def test_process_lab_no_op_when_clean(tmp_path):
    """process_lab returns 0 removed on an already-clean lab."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    lab_path = _seed_lab(
        labs_dir,
        links=[
            _make_link("lnk_001", {"node_id": 1, "interface_index": 0}, {"network_id": 5}),
        ],
    )
    before, removed = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)
    assert removed == 0
    assert before == 1
    # File is untouched (no tmp written).
    assert not (lab_path.with_suffix(".json.tmp")).exists()


def test_process_lab_removes_ab_ba_duplicate(tmp_path):
    """process_lab collapses A→B + B→A duplicate, keeps lnk_001."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    ep_a = {"node_id": 1, "interface_index": 0}
    ep_b = {"network_id": 5}
    lab_path = _seed_lab(
        labs_dir,
        links=[
            _make_link("lnk_001", ep_a, ep_b),
            _make_link("lnk_002", ep_b, ep_a),
        ],
    )

    before, removed = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)

    assert before == 2
    assert removed == 1

    saved = json.loads(lab_path.read_text())
    assert len(saved["links"]) == 1
    assert saved["links"][0]["id"] == "lnk_001"


def test_process_lab_removes_ab_ab_duplicate(tmp_path):
    """process_lab collapses A→B + A→B duplicate, keeps lnk_001."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    ep_a = {"node_id": 2, "interface_index": 0}
    ep_b = {"network_id": 10}
    lab_path = _seed_lab(
        labs_dir,
        links=[
            _make_link("lnk_001", ep_a, ep_b),
            _make_link("lnk_002", ep_a, ep_b),
        ],
    )

    before, removed = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)

    assert removed == 1
    saved = json.loads(lab_path.read_text())
    assert len(saved["links"]) == 1
    assert saved["links"][0]["id"] == "lnk_001"


def test_process_lab_multi_lab_batch(tmp_path):
    """main() processes multiple labs and reports correct totals."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()

    ep_a = {"node_id": 1, "interface_index": 0}
    ep_b = {"network_id": 5}

    # lab_a has a duplicate
    _seed_lab(
        labs_dir,
        name="lab_a.json",
        links=[
            _make_link("lnk_001", ep_a, ep_b),
            _make_link("lnk_002", ep_b, ep_a),
        ],
    )
    # lab_b is clean
    _seed_lab(
        labs_dir,
        name="lab_b.json",
        links=[
            _make_link("lnk_001", ep_a, ep_b),
        ],
    )

    rc = dedup_links.main(["--labs-dir", str(labs_dir)])
    assert rc == 0

    saved_a = json.loads((labs_dir / "lab_a.json").read_text())
    assert len(saved_a["links"]) == 1

    saved_b = json.loads((labs_dir / "lab_b.json").read_text())
    assert len(saved_b["links"]) == 1


def test_process_lab_dry_run_does_not_write(tmp_path):
    """dry_run=True reports duplicates but does not modify the file."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    ep_a = {"node_id": 1, "interface_index": 0}
    ep_b = {"network_id": 5}
    lab_path = _seed_lab(
        labs_dir,
        links=[
            _make_link("lnk_001", ep_a, ep_b),
            _make_link("lnk_002", ep_b, ep_a),
        ],
    )
    original_text = lab_path.read_text()

    before, removed = dedup_links.process_lab(lab_path, labs_dir, dry_run=True)

    assert removed == 1
    # File must be unchanged.
    assert lab_path.read_text() == original_text
    assert not (lab_path.with_suffix(".json.tmp")).exists()


def test_main_dry_run_does_not_write(tmp_path):
    """main() with --dry-run leaves files untouched."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    ep_a = {"node_id": 1, "interface_index": 0}
    ep_b = {"network_id": 5}
    lab_path = _seed_lab(
        labs_dir,
        links=[
            _make_link("lnk_001", ep_a, ep_b),
            _make_link("lnk_002", ep_b, ep_a),
        ],
    )
    original_text = lab_path.read_text()

    rc = dedup_links.main(["--labs-dir", str(labs_dir), "--dry-run"])

    assert rc == 0
    assert lab_path.read_text() == original_text


def test_process_lab_idempotent(tmp_path):
    """Running process_lab twice on the same lab is a no-op on the second run."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    ep_a = {"node_id": 1, "interface_index": 0}
    ep_b = {"network_id": 5}
    lab_path = _seed_lab(
        labs_dir,
        links=[
            _make_link("lnk_001", ep_a, ep_b),
            _make_link("lnk_002", ep_b, ep_a),
        ],
    )

    before1, removed1 = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)
    assert removed1 == 1

    # Second run — should report 0 removed.
    before2, removed2 = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)
    assert removed2 == 0
    assert before2 == 1


def test_main_nonexistent_labs_dir(tmp_path):
    """main() exits 1 when labs_dir does not exist."""
    rc = dedup_links.main(["--labs-dir", str(tmp_path / "nonexistent")])
    assert rc == 1
