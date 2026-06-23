from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app
from app.services import iourc_service


def test_iourc_status_reports_absent_license(tmp_path):
    status = iourc_service.status(tmp_path / "iourc")

    assert status.installed is False
    assert status.path is None
    assert status.size is None
    assert status.hostname
    assert isinstance(status.ips, list)


def test_iourc_status_reports_existing_license_without_contents(tmp_path):
    directory = tmp_path / "iourc"
    directory.mkdir()
    license_file = directory / "iourc"
    license_file.write_text("[license]\n")

    payload = iourc_service.status(directory).as_dict()

    assert payload["installed"] is True
    assert payload["filename"] == "iourc"
    assert payload["size"] == len("[license]\n")
    assert "[license]" not in str(payload)


def test_iourc_upload_stores_canonical_file_with_restrictive_permissions(tmp_path):
    directory = tmp_path / "iourc"

    status = iourc_service.store_uploaded_iourc(b"[license]\n", directory)

    target = directory / "iourc"
    assert status.installed is True
    assert status.path == target
    assert target.read_text() == "[license]\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700


def test_iourc_upload_rejects_empty_file(tmp_path):
    with pytest.raises(iourc_service.IourcError, match="empty"):
        iourc_service.store_uploaded_iourc(b"  \n", tmp_path / "iourc")


@pytest.mark.asyncio
async def test_iourc_status_and_upload_routes(monkeypatch, tmp_path):
    settings = SimpleNamespace(IOURC_DIR=tmp_path / "iourc")
    monkeypatch.setattr("app.services.iourc_service.get_settings", lambda: settings)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="admin",
        role="admin",
        html5=True,
        folder="/",
    )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            status_response = await client.get("/api/system/iourc")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            assert status_payload["data"]["installed"] is False

            upload_response = await client.post(
                "/api/system/iourc",
                files={"file": ("iourc", b"[license]\n", "text/plain")},
            )
            assert upload_response.status_code == 200
            upload_payload = upload_response.json()
            assert upload_payload["data"]["installed"] is True
            assert upload_payload["data"]["filename"] == "iourc"
            assert (settings.IOURC_DIR / "iourc").read_text() == "[license]\n"

            empty_response = await client.post(
                "/api/system/iourc",
                files={"file": ("iourc", b"\n", "text/plain")},
            )
            assert empty_response.status_code == 400
            assert empty_response.json()["message"] == "Uploaded IOURC file is empty."
    finally:
        app.dependency_overrides.clear()
