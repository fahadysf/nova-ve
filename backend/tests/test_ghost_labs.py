# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Ghost lab rows (#284): DB lab rows whose backing JSON file was removed
out-of-band must not appear in the labs listing, and the startup sweep
must converge them away."""

from types import SimpleNamespace

import pytest

from app.routers.folders import _merge_db_labs
from app.services.lab_service import LabService


class _FakeDb:
    """Records delete/commit calls — enough for the reconcile path."""

    def __init__(self):
        self.deleted = []
        self.commits = 0

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeLabService(LabService):
    """Stands in for LabService when _merge_db_labs builds one — subclasses
    the real service so static helpers (file_exists) stay genuine."""

    instances = []
    rows = []

    def __init__(self, db):
        self.db = db
        _FakeLabService.instances.append(self)

    async def list_labs(self):
        return list(self.rows)


def _lab_row(filename: str):
    return SimpleNamespace(filename=filename, path=f"/{filename}")


@pytest.fixture()
def seeded_labs(tmp_path, monkeypatch):
    """LABS_DIR with one real lab file on disk; rows for it + one ghost."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    (labs_dir / "real.json").write_text("{}")

    rows = [_lab_row("real.json"), _lab_row("ghost.json")]
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: SimpleNamespace(LABS_DIR=labs_dir))
    monkeypatch.setattr("app.routers.folders.LabService", _FakeLabService)
    _FakeLabService.instances = []
    _FakeLabService.rows = rows
    return labs_dir


@pytest.mark.asyncio
async def test_merge_db_labs_skips_ghost_rows(seeded_labs):
    # Mirror production: the file scan already listed the real lab.
    result = {"labs": [{"file": "real.json", "path": "/real.json"}], "folders": []}

    merged = await _merge_db_labs(result, _FakeDb(), "", recursive=True)

    listed = [lab["file"] for lab in merged["labs"]]
    # The real lab is not double-listed (path dedupe against the file scan)
    # and the ghost row is filtered out entirely.
    assert listed == ["real.json"]
    assert "ghost.json" not in listed


@pytest.mark.asyncio
async def test_merge_db_labs_lists_db_row_for_scoped_folder(seeded_labs):
    """A DB row whose file exists still surfaces when the file scan didn't
    cover it (scoped folder listing, empty scan result)."""
    result = {"labs": [], "folders": []}

    _FakeLabService.rows = [_lab_row("nested/inner.json")]
    (seeded_labs / "nested").mkdir()
    (seeded_labs / "nested" / "inner.json").write_text("{}")

    merged = await _merge_db_labs(result, _FakeDb(), "nested", recursive=False)

    assert "inner.json" in [lab["file"] for lab in merged["labs"]]


@pytest.mark.asyncio
async def test_reconcile_removes_ghost_rows_and_keeps_real(seeded_labs):
    db = _FakeDb()
    service = LabService(db)
    rows = [SimpleNamespace(filename="real.json"), SimpleNamespace(filename="ghost.json")]
    service.list_labs = lambda: _async_list(rows)

    removed = await service.reconcile_missing_lab_files()

    assert removed == ["ghost.json"]
    assert [row.filename for row in db.deleted] == ["ghost.json"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_reconcile_is_a_noop_when_files_match_rows(seeded_labs):
    db = _FakeDb()
    service = LabService(db)
    service.list_labs = lambda: _async_list([SimpleNamespace(filename="real.json")])

    removed = await service.reconcile_missing_lab_files()

    assert removed == []
    assert db.deleted == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_reconcile_treats_unsafe_filenames_as_missing(seeded_labs):
    db = _FakeDb()
    service = LabService(db)
    service.list_labs = lambda: _async_list(
        [SimpleNamespace(filename="../../etc/passwd.json")]
    )

    removed = await service.reconcile_missing_lab_files()

    # Traversal filename cannot be a real lab file — row is removed.
    assert removed == ["../../etc/passwd.json"]
    assert db.commits == 1


async def _async_list(items):
    return list(items)
