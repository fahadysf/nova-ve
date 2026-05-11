"""Unit tests for the Dynamips idle-PC cache."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.services.runtime.dynamips import IdlePcCache


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "idle_pc_cache.json"


def test_set_then_get_roundtrips(cache_path: Path) -> None:
    cache = IdlePcCache(cache_path)
    cache.set("aa" * 32, "0x60a5b780")
    cache.set("bb" * 32, "0x60c09320")

    fresh = IdlePcCache(cache_path)
    assert fresh.get("aa" * 32) == "0x60a5b780"
    assert fresh.get("bb" * 32) == "0x60c09320"


def test_get_unknown_returns_none(cache_path: Path) -> None:
    cache = IdlePcCache(cache_path)
    assert cache.get("deadbeef" * 8) is None


def test_set_overwrites_previous_value(cache_path: Path) -> None:
    cache = IdlePcCache(cache_path)
    cache.set("aa" * 32, "0x1111")
    cache.set("aa" * 32, "0x2222")
    assert cache.get("aa" * 32) == "0x2222"


def test_malformed_cache_file_is_treated_as_empty(cache_path: Path) -> None:
    cache_path.write_text("{not valid json")
    cache = IdlePcCache(cache_path)
    # Reading a corrupt file returns no entries — write should still
    # succeed and clobber the malformed contents.
    assert cache.get("aa" * 32) is None
    cache.set("aa" * 32, "0xfresh")
    assert cache.get("aa" * 32) == "0xfresh"


def test_hash_image_matches_sha256(tmp_path: Path) -> None:
    image = tmp_path / "image.bin"
    payload = b"\x7fELF" + b"X" * 4096  # mimic an unpacked IOS .bin header
    image.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert IdlePcCache.hash_image(image) == expected


def test_non_dict_payload_returns_no_entries(cache_path: Path) -> None:
    cache_path.write_text('["not", "a", "dict"]')
    cache = IdlePcCache(cache_path)
    assert cache.get("aa" * 32) is None


def test_non_string_values_filtered(cache_path: Path) -> None:
    cache_path.write_text('{"aa": 1, "bb": "0xvalid"}')
    cache = IdlePcCache(cache_path)
    assert cache.get("aa") is None
    assert cache.get("bb") == "0xvalid"
