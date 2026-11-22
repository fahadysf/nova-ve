# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lab move endpoint (#285): the JSON file and the LabMeta DB row move
together — collision fails with 409, DB failure rolls the file back, and
both ends are scope-checked (own scope or /Shared for scoped users)."""

from types import SimpleNamespace

import pytest

from app.routers.labs import move_lab
from app.schemas.lab import LabMoveRequest
from app.services.lab_service import LabService


class _FakeDb:
    """Records commits; can be told to fail the first one."""

    def __init__(self, fail_commit=False):
        self.commits = 0
        self.fail_commit = fail_commit

    async def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("db boom")


class _FakeLabService(LabService):
    """Stub-DB style from test_ghost_labs.py: subclasses the real service
    so static helpers stay genuine, overrides the DB-touching parts."""

    instances = []
    row = None

    def __init__(self, db):
        self.db = db
        _FakeLabService.instances.append(self)

    async def get_lab_by_filename(self, filename):
        return self.row


def _lab_row(filename: str):
    return SimpleNamespace(filename=filename, path=f"/{filename}", owner="alpha")


def _scoped_user():
    return SimpleNamespace(role="user", folder="/lanes/alpha", username="alpha")


def _admin():
    return SimpleNamespace(role="admin", folder="", username="admin")


@pytest.fixture()
def labs_env(tmp_path, monkeypatch):
    labs_dir = tmp_path / "labs"
    (labs_dir / "lanes" / "alpha").mkdir(parents=True)
    monkeypatch.setattr(
        "app.services.lab_service.get_settings",
        lambda: SimpleNamespace(LABS_DIR=labs_dir),
    )
    monkeypatch.setattr(
        "app.routers.labs.get_settings",
        lambda: SimpleNamespace(LABS_DIR=labs_dir),
    )
    monkeypatch.setattr("app.routers.labs.LabService", _FakeLabService)
    _FakeLabService.instances = []
    _FakeLabService.row = None
    return labs_dir


async def _move(lab_path, target_folder, user, db):
    return await move_lab(
        lab_path,
        LabMoveRequest(target_folder=target_folder),
        current_user=user,
        db=db,
    )


@pytest.mark.asyncio
async def test_move_updates_file_location_and_db_row(labs_env):
    source = labs_env / "lanes" / "alpha" / "foo.json"
    source.write_text("{}")
    row = _lab_row("lanes/alpha/foo.json")
    _FakeLabService.row = row
    db = _FakeDb()

    payload = await _move("lanes/alpha/foo.json", "/Shared", _scoped_user(), db)

    assert payload["code"] == 200
    assert payload["status"] == "success"
    assert not source.exists()
    assert (labs_env / "Shared" / "foo.json").is_file()
    assert row.filename == "Shared/foo.json"
    assert row.path == "/Shared/foo.json"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_move_conflict_returns_409(labs_env):
    source = labs_env / "lanes" / "alpha" / "foo.json"
    source.write_text("{}")
    (labs_env / "Shared").mkdir()
    (labs_env / "Shared" / "foo.json").write_text("{}")
    _FakeLabService.row = _lab_row("lanes/alpha/foo.json")
    db = _FakeDb()

    payload = await _move("lanes/alpha/foo.json", "/Shared", _scoped_user(), db)

    assert payload["code"] == 409
    assert payload["status"] == "fail"
    assert source.is_file()
    assert db.commits == 0


@pytest.mark.asyncio
async def test_move_rolls_file_back_when_db_commit_fails(labs_env):
    source = labs_env / "lanes" / "alpha" / "foo.json"
    source.write_text("{}")
    row = _lab_row("lanes/alpha/foo.json")
    _FakeLabService.row = row
    db = _FakeDb(fail_commit=True)

    payload = await _move("lanes/alpha/foo.json", "/Shared", _scoped_user(), db)

    assert payload["code"] == 500
    assert payload["status"] == "fail"
    assert source.is_file()
    assert not (labs_env / "Shared" / "foo.json").exists()


@pytest.mark.asyncio
async def test_move_denied_for_cross_scope_source(labs_env):
    (labs_env / "lanes" / "beta").mkdir()
    beta_lab = labs_env / "lanes" / "beta" / "foo.json"
    beta_lab.write_text("{}")
    _FakeLabService.row = _lab_row("lanes/beta/foo.json")
    db = _FakeDb()

    payload = await _move("lanes/beta/foo.json", "/lanes/alpha", _scoped_user(), db)

    assert payload["code"] == 403
    assert beta_lab.is_file()
    assert db.commits == 0


@pytest.mark.asyncio
async def test_move_denied_for_cross_scope_target(labs_env):
    source = labs_env / "lanes" / "alpha" / "foo.json"
    source.write_text("{}")
    (labs_env / "lanes" / "beta").mkdir()
    _FakeLabService.row = _lab_row("lanes/alpha/foo.json")
    db = _FakeDb()

    payload = await _move("lanes/alpha/foo.json", "/lanes/beta", _scoped_user(), db)

    assert payload["code"] == 403
    assert source.is_file()
    assert db.commits == 0


@pytest.mark.asyncio
async def test_scoped_user_moves_lab_from_shared_to_own_root(labs_env):
    (labs_env / "Shared").mkdir()
    shared_lab = labs_env / "Shared" / "foo.json"
    shared_lab.write_text("{}")
    row = _lab_row("Shared/foo.json")
    _FakeLabService.row = row
    db = _FakeDb()

    payload = await _move("Shared/foo.json", "/lanes/alpha", _scoped_user(), db)

    assert payload["code"] == 200
    assert (labs_env / "lanes" / "alpha" / "foo.json").is_file()
    assert row.filename == "lanes/alpha/foo.json"
    assert row.path == "/lanes/alpha/foo.json"


@pytest.mark.asyncio
async def test_move_source_not_found_returns_404(labs_env):
    _FakeLabService.row = None
    db = _FakeDb()

    payload = await _move("lanes/alpha/missing.json", "/Shared", _scoped_user(), db)

    assert payload["code"] == 404
    assert payload["status"] == "fail"


@pytest.mark.asyncio
async def test_admin_can_move_anywhere(labs_env):
    (labs_env / "lanes" / "beta").mkdir()
    beta_lab = labs_env / "lanes" / "beta" / "foo.json"
    beta_lab.write_text("{}")
    row = _lab_row("lanes/beta/foo.json")
    _FakeLabService.row = row
    db = _FakeDb()

    payload = await _move("lanes/beta/foo.json", "/Shared", _admin(), db)

    assert payload["code"] == 200
    assert (labs_env / "Shared" / "foo.json").is_file()
    assert row.filename == "Shared/foo.json"
    assert row.path == "/Shared/foo.json"
