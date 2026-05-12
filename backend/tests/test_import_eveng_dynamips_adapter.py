"""Unit tests for the EVE-NG Dynamips adapter (Phase 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.import_eveng.adapters.base import NeedsManualReview
from scripts.import_eveng.adapters.dynamips import DynamipsAdapter


@pytest.fixture
def adapter() -> DynamipsAdapter:
    return DynamipsAdapter()


def test_match_by_type_field(adapter: DynamipsAdapter) -> None:
    assert adapter.match({"type": "dynamips", "image": "anything.bin"}) is True


def test_match_by_image_prefix_c3725(adapter: DynamipsAdapter) -> None:
    assert adapter.match({"image": "c3725-adventerprisek9-mz.124-15.T14.image"})


def test_match_by_image_prefix_c7200(adapter: DynamipsAdapter) -> None:
    assert adapter.match({"image": "c7200-adventerprisek9-mz.124-24.T5.bin"})


def test_no_match_on_other_image_names(adapter: DynamipsAdapter) -> None:
    assert adapter.match({"image": "i86bi-linux-l3-adventerprisek9.bin"}) is False
    assert adapter.match({"image": "vmx-bundle-21.4R3.tgz"}) is False


def test_convert_c3725_minimal(adapter: DynamipsAdapter, tmp_path: Path) -> None:
    raw = {
        "image": "c3725-adventerprisek9-mz.124-15.T14.image",
        "ram": 256,
    }
    result = adapter.convert(raw, tmp_path)
    assert result["kind"] == "dynamips"
    assert result["vendor"] == "cisco"
    assert result["extras"]["platform"] == "c3725"
    assert result["ram"] == 256
    # Default ethernet inferred from c3725's built-in GT96100-FE = 2.
    assert result["ethernet"] == 2


def test_convert_c7200_with_slots(adapter: DynamipsAdapter, tmp_path: Path) -> None:
    raw = {
        "image": "c7200-adventerprisek9-mz.124-24.T5.bin",
        "ram": 512,
        "slot0": "C7200-IO-FE",
        "slot1": "PA-GE",
        "npe": "npe-400",
        "idlepc": "0x606a7e54",
    }
    result = adapter.convert(raw, tmp_path)
    assert result["extras"]["platform"] == "c7200"
    assert result["extras"]["slot0"] == "C7200-IO-FE"
    assert result["extras"]["slot1"] == "PA-GE"
    assert result["extras"]["npe"] == "npe-400"
    assert result["extras"]["idlepc"] == "0x606a7e54"


def test_unsupported_platform_raises_needs_manual_review(
    adapter: DynamipsAdapter, tmp_path: Path
) -> None:
    raw = {"image": "c2691-adventerprisek9-mz.124-15.T14.image"}
    with pytest.raises(NeedsManualReview, match="not yet supported"):
        adapter.convert(raw, tmp_path)


def test_missing_image_raises_needs_manual_review(
    adapter: DynamipsAdapter, tmp_path: Path
) -> None:
    raw: dict = {"name": "router1"}
    with pytest.raises(NeedsManualReview, match="image"):
        adapter.convert(raw, tmp_path)


def test_infer_ethernet_count_sums_slot_inventory() -> None:
    # 2 (GT96100-FE) + 16 (NM-16ESW) = 18
    assert DynamipsAdapter._infer_ethernet_count(
        "c3725",
        {"slot0": "GT96100-FE", "slot1": "NM-16ESW"},
    ) == 18


def test_infer_ethernet_count_falls_back_to_default_for_empty_slots() -> None:
    assert DynamipsAdapter._infer_ethernet_count("c3725", {}) == 2
    assert DynamipsAdapter._infer_ethernet_count("c7200", {}) == 1


def test_idle_pc_aliases_normalized(adapter: DynamipsAdapter, tmp_path: Path) -> None:
    # EVE-NG uses `idle` while GNS3 uses `idlepc`; the adapter accepts
    # either and writes the canonical key.
    raw_idle = {
        "image": "c3725-adventerprisek9-mz.image",
        "idle": "0xaaaa",
    }
    assert adapter.convert(raw_idle, tmp_path)["extras"]["idlepc"] == "0xaaaa"
    raw_idle_pc = {
        "image": "c3725-adventerprisek9-mz.image",
        "idle_pc": "0xbbbb",
    }
    assert (
        adapter.convert(raw_idle_pc, tmp_path)["extras"]["idlepc"] == "0xbbbb"
    )
