# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Image & template library API tests (#264).

Seeds a temp images/templates/labs tree and exercises the inventory,
checksum verify+cache, reference-guarded delete, and template edit
(shadow-edit for builtin templates) through the admin endpoints.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.dependencies import get_current_admin


@pytest.fixture()
def lib_settings(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        LABS_DIR=tmp_path / "labs",
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        USER_TEMPLATES_DIR=tmp_path / "user-templates",
    )
    for path in (
        settings.LABS_DIR,
        settings.IMAGES_DIR,
        settings.TMP_DIR,
        settings.TEMPLATES_DIR,
        settings.USER_TEMPLATES_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.services.image_library_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    return settings


def _seed_veos(settings):
    """A qemu template with one matching image dir + one non-matching."""
    templates = settings.TEMPLATES_DIR / "qemu"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "veos.yml").write_text(
        "type: qemu\nname: vEOS\ncpu: 1\nram: 2048\nethernet: 4\n"
        "console_type: telnet\nicon_type: router\ncpulimit: 1\n"
        "image_aliases:\n  - veos\n"
    )
    (templates / "vyos.yml").write_text(
        "type: qemu\nname: VyOS\ncpu: 1\nram: 1024\nethernet: 2\n"
        "console_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )
    image_dir = settings.IMAGES_DIR / "qemu" / "veos"
    image_dir.mkdir(parents=True, exist_ok=True)
    content = b"fake-qcow2-bytes"
    (image_dir / "veos-4.31.qcow2").write_bytes(content)
    return image_dir / "veos-4.31.qcow2", hashlib.sha256(content).hexdigest()


def _seed_referencing_lab(settings):
    lab = {
        "schema": 2,
        "id": "arc-lab",
        "meta": {"name": "Arc Lab"},
        "nodes": {
            "1": {
                "id": 1,
                "name": "sw1",
                "type": "qemu",
                "template": "veos",
                "image": "veos",
            }
        },
    }
    (settings.LABS_DIR / "arc-lab.json").write_text(json.dumps(lab))


@pytest_asyncio.fixture()
async def client(lib_settings):
    admin = SimpleNamespace(username="admin", role="admin", html5=True, folder="/")
    app.dependency_overrides[get_current_admin] = lambda: admin
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_inventory_lists_images_with_size_and_references(client, lib_settings):
    image_file, _sha = _seed_veos(lib_settings)
    _seed_referencing_lab(lib_settings)

    response = await client.get("/api/library/images")

    assert response.status_code == 200
    images = response.json()["data"]["images"]
    veos = next(image for image in images if image["name"] == "veos")
    assert veos["type"] == "qemu"
    assert veos["size"] == image_file.stat().st_size
    assert veos["state"] == "available"
    assert veos["sha256"] is None  # never computed — checksums are lazy
    assert veos["referenced_by"][0]["lab"] == "arc-lab.json"
    assert veos["referenced_by"][0]["node"] == "sw1"


@pytest.mark.asyncio
async def test_templates_list_with_matches_and_missing_state(client, lib_settings):
    _seed_veos(lib_settings)

    response = await client.get("/api/library/templates")

    assert response.status_code == 200
    templates = response.json()["data"]["templates"]
    veos = next(t for t in templates if t["key"] == "veos")
    assert [image["name"] for image in veos["images"]] == ["veos"]
    vyos = next(t for t in templates if t["key"] == "vyos")
    assert vyos["images"] == []  # explicit missing-image state


@pytest.mark.asyncio
async def test_verify_checksum_computes_and_caches(client, lib_settings):
    image_file, expected = _seed_veos(lib_settings)

    response = await client.post("/api/library/images/qemu/veos/verify")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sha256"] == expected
    assert data["file"] == "veos-4.31.qcow2"

    # The cached value now flows through the inventory without recompute.
    images_response = await client.get("/api/library/images")
    veos = next(
        image
        for image in images_response.json()["data"]["images"]
        if image["name"] == "veos"
    )
    assert veos["sha256"] == expected


@pytest.mark.asyncio
async def test_cached_checksum_invalidated_by_mtime_change(client, lib_settings):
    image_file, expected = _seed_veos(lib_settings)

    await client.post("/api/library/images/qemu/veos/verify")

    # Rewrite the same content at a later mtime → cache entry stale.
    import os

    stat_before = image_file.stat()
    os.utime(image_file, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 1_000_000))

    images_response = await client.get("/api/library/images")
    veos = next(
        image
        for image in images_response.json()["data"]["images"]
        if image["name"] == "veos"
    )
    assert veos["sha256"] is None


@pytest.mark.asyncio
async def test_delete_referenced_image_is_rejected(client, lib_settings):
    _seed_veos(lib_settings)
    _seed_referencing_lab(lib_settings)

    response = await client.delete("/api/library/images/qemu/veos")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 409
    assert payload["data"]["references"][0]["lab"] == "arc-lab.json"
    assert (lib_settings.IMAGES_DIR / "qemu" / "veos").exists()


@pytest.mark.asyncio
async def test_delete_unreferenced_image_removes_files(client, lib_settings):
    _seed_veos(lib_settings)

    response = await client.delete("/api/library/images/qemu/veos")

    assert response.status_code == 200
    assert response.json()["code"] == 200
    assert not (lib_settings.IMAGES_DIR / "qemu" / "veos").exists()


@pytest.mark.asyncio
async def test_delete_docker_image_is_rejected(client, lib_settings):
    response = await client.delete("/api/library/images/docker/alpine:3.20")

    assert response.status_code == 200
    assert response.json()["code"] == 400
    assert "docker daemon" in response.json()["message"]


@pytest.mark.asyncio
async def test_patch_user_template_edits_in_place(client, lib_settings):
    user_dir = lib_settings.USER_TEMPLATES_DIR
    (user_dir / "custom.yml").write_text(
        "type: qemu\nname: Custom\ncpu: 1\nram: 1024\nethernet: 2\n"
        "console_type: telnet\nicon_type: router\ncpulimit: 1\n"
    )

    response = await client.patch(
        "/api/library/templates/qemu/custom",
        json={"ram": 4096, "name": "Custom X"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ram"] == 4096
    assert data["name"] == "Custom X"
    # Edited in place — no shadow copy.
    assert not (user_dir / "custom.yml").read_text().count("Custom X") == 2
    assert "ram: 4096" in (user_dir / "custom.yml").read_text()


@pytest.mark.asyncio
async def test_patch_builtin_template_writes_shadow(client, lib_settings):
    _seed_veos(lib_settings)  # builtin veos.yml under TEMPLATES_DIR

    response = await client.patch(
        "/api/library/templates/qemu/veos",
        json={"ram": 8192, "image_aliases": ["veos", "arista"]},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ram"] == 8192
    assert data["image_aliases"] == ["veos", "arista"]

    shadow = lib_settings.USER_TEMPLATES_DIR / "veos.yml"
    assert shadow.exists()
    assert "ram: 8192" in shadow.read_text()
    # Builtin file untouched.
    assert "ram: 8192" not in (lib_settings.TEMPLATES_DIR / "qemu" / "veos.yml").read_text()


@pytest.mark.asyncio
async def test_patch_template_rejects_invalid_fields(client, lib_settings):
    _seed_veos(lib_settings)

    # Pydantic's ge=1 rejects non-positive integers at request-validation
    # level → native FastAPI 422.
    response = await client.patch(
        "/api/library/templates/qemu/veos",
        json={"ram": 0},
    )
    assert response.status_code == 422

    # A well-typed but empty edit is rejected by the service with the
    # body-wrapped 422 shape.
    response = await client.patch("/api/library/templates/qemu/veos", json={})
    assert response.status_code == 200
    assert response.json()["code"] == 422


@pytest.mark.asyncio
async def test_patch_missing_template_returns_404(client, lib_settings):
    response = await client.patch(
        "/api/library/templates/qemu/ghost",
        json={"ram": 2048},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 404
