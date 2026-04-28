# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# File lives at backend/tests/test_dedup_links.py -> parents[2] = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dedup_links  # noqa: E402


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
    *,
    name: str = "lab.json",
    links: list[dict[str, Any]] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema": 2,
        "id": name.removesuffix(".json"),
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


def test_process_lab_uses_labservice_write_surface(tmp_path, monkeypatch):
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

    writes: list[str] = []
    original_write = dedup_links.LabService.write_lab_json_static

    def spy(filename: str, data: dict[str, Any]) -> None:
        writes.append(filename)
        original_write(filename, data)

    monkeypatch.setattr(
        dedup_links.LabService,
        "write_lab_json_static",
        staticmethod(spy),
    )

    before, removed = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)

    assert before == 2
    assert removed == 1
    assert writes == ["lab.json"]


def test_process_lab_second_run_is_noop_and_skips_write(tmp_path, monkeypatch):
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

    writes: list[str] = []
    original_write = dedup_links.LabService.write_lab_json_static

    def spy(filename: str, data: dict[str, Any]) -> None:
        writes.append(filename)
        original_write(filename, data)

    monkeypatch.setattr(
        dedup_links.LabService,
        "write_lab_json_static",
        staticmethod(spy),
    )

    before_first, removed_first = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)
    before_second, removed_second = dedup_links.process_lab(lab_path, labs_dir, dry_run=False)

    assert before_first == 2
    assert removed_first == 1
    assert before_second == 1
    assert removed_second == 0
    assert writes == ["lab.json"]
