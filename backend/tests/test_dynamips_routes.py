"""HTTP-layer tests for /api/dynamips/* endpoints.

The calibration path is exercised by mocking the launcher rather than
spawning a real Dynamips hypervisor — the launcher's own protocol
tests cover the real-hypervisor surface.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app
from app.services.runtime.dynamips import DynamipsError


@pytest.fixture()
def stub_auth():
    """Bypass auth for these tests — they exercise the dynamips
    surface specifically, not the auth layer."""
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, username="test"
    )
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def images_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher + lister at a fixture images dir, and the
    idle-PC cache at an isolated location so tests don't pollute or
    inherit each other's state.
    """
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    monkeypatch.setattr(
        "app.services.runtime.dynamips._IDLE_PC_CACHE_PATH",
        tmp_path / "idle_pc_cache.json",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_list_images_empty(stub_auth, images_root: Path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/dynamips/images")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


@pytest.mark.asyncio
async def test_list_images_flat_and_nested(
    stub_auth, images_root: Path
):
    # Flat layout
    flat = images_root / "c7200-flat.image"
    flat.write_bytes(b"FLAT")
    # Nested (EVE-NG) layout
    nested_dir = images_root / "c3725-nested-stem"
    nested_dir.mkdir()
    nested = nested_dir / "c3725-nested-stem.image"
    nested.write_bytes(b"NESTED")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/dynamips/images")
    assert resp.status_code == 200

    rows = {entry["image"]: entry for entry in resp.json()["data"]}
    assert set(rows) == {"c7200-flat.image", "c3725-nested-stem.image"}

    assert rows["c7200-flat.image"]["platform"] == "c7200"
    assert rows["c7200-flat.image"]["calibrated"] is False
    assert rows["c7200-flat.image"]["idle_pc"] is None
    assert rows["c7200-flat.image"]["size_bytes"] == 4

    assert rows["c3725-nested-stem.image"]["platform"] == "c3725"
    assert rows["c3725-nested-stem.image"]["calibrated"] is False


@pytest.mark.asyncio
async def test_list_images_marks_cached_calibrated(
    stub_auth, images_root: Path
):
    image = images_root / "c3725-cached.image"
    image.write_bytes(b"X" * 1024)

    # Pre-populate the cache with the value the lister expects to find.
    from app.services.runtime.dynamips import IdlePcCache

    cache = IdlePcCache()  # uses the monkeypatched _IDLE_PC_CACHE_PATH
    cache.set(IdlePcCache.hash_image(image), "0xdeadbeef")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/dynamips/images")

    rows = {e["image"]: e for e in resp.json()["data"]}
    assert rows["c3725-cached.image"]["calibrated"] is True
    assert rows["c3725-cached.image"]["idle_pc"] == "0xdeadbeef"


@pytest.mark.asyncio
async def test_calibrate_rejects_invalid_name(stub_auth, images_root: Path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/dynamips/calibrate", json={"image": "../etc/passwd"}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_calibrate_404_when_image_missing(stub_auth, images_root: Path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/dynamips/calibrate", json={"image": "nope.image"}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_calibrate_success(
    stub_auth, images_root: Path, monkeypatch: pytest.MonkeyPatch
):
    image = images_root / "c3725-real.image"
    image.write_bytes(b"FAKE")

    calls: dict = {}

    def fake_calibrate_image(self, image_path: Path, **kwargs):
        calls["image_path"] = image_path
        return {
            "image": image_path.name,
            "image_sha256": "abc123",
            "idle_pc": "0x60c09320",
            "candidates": ["0x60c09320", "0x60c0a000"],
            "duration_s": 12.3,
            "platform": "c3725",
        }

    monkeypatch.setattr(
        "app.services.runtime.dynamips.DynamipsLauncher.calibrate_image",
        fake_calibrate_image,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/dynamips/calibrate", json={"image": "c3725-real.image"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["idle_pc"] == "0x60c09320"
    assert body["data"]["platform"] == "c3725"
    assert body["data"]["candidates"] == ["0x60c09320", "0x60c0a000"]
    assert calls["image_path"] == image


@pytest.mark.asyncio
async def test_calibrate_502_on_dynamips_error(
    stub_auth, images_root: Path, monkeypatch: pytest.MonkeyPatch
):
    image = images_root / "c3725-broken.image"
    image.write_bytes(b"FAKE")

    def fake_calibrate_image(self, image_path: Path, **kwargs):
        raise DynamipsError("hypervisor refused: 209 invalid argument")

    monkeypatch.setattr(
        "app.services.runtime.dynamips.DynamipsLauncher.calibrate_image",
        fake_calibrate_image,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/dynamips/calibrate", json={"image": "c3725-broken.image"}
        )
    assert resp.status_code == 502
    assert "hypervisor refused" in resp.json()["detail"]
