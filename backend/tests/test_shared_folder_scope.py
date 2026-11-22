# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""#285: the top-level Shared space is reachable by every authenticated user.

A normalized path equal to ``Shared`` or starting with ``Shared/`` resolves
to the real top-level folder — never prefixed into a scoped user's root.
"""

from types import SimpleNamespace

import pytest

from app.routers.folders import _scoped_folder_path
from app.routers.labs import _scoped_lab_path
from app.services.folder_service import FolderService


def _scoped_user():
    return SimpleNamespace(role="user", folder="/lanes/alpha")


def _admin():
    return SimpleNamespace(role="admin", folder="")


@pytest.mark.parametrize("helper", [_scoped_lab_path, _scoped_folder_path])
class TestSharedPrefix:
    def test_shared_exact_resolves_top_level_for_scoped_user(self, helper):
        assert helper(_scoped_user(), "Shared", treat_as_absolute=True) == "Shared"

    def test_shared_child_resolves_top_level_for_scoped_user(self, helper):
        assert helper(_scoped_user(), "Shared/team.json", treat_as_absolute=True) == "Shared/team.json"

    def test_leading_slash_shared_resolves_top_level(self, helper):
        assert helper(_scoped_user(), "/Shared", treat_as_absolute=True) == "Shared"

    def test_relative_shared_is_not_prefixed_into_user_root(self, helper):
        # The key regression guard: relative "Shared/x" must NOT become
        # "lanes/alpha/Shared/x" for a folder-scoped user.
        assert helper(_scoped_user(), "Shared/x", treat_as_absolute=False) == "Shared/x"

    def test_other_absolute_path_still_denied(self, helper):
        with pytest.raises(PermissionError):
            helper(_scoped_user(), "lanes/beta/x.json", treat_as_absolute=True)

    def test_relative_non_shared_still_prefixed_into_user_root(self, helper):
        assert helper(_scoped_user(), "x.json", treat_as_absolute=False) == "lanes/alpha/x.json"

    def test_admin_unaffected(self, helper):
        assert helper(_admin(), "lanes/beta/x.json", treat_as_absolute=True) == "lanes/beta/x.json"
        assert helper(_admin(), "Shared/x.json", treat_as_absolute=True) == "Shared/x.json"


@pytest.fixture()
def empty_labs_dir(tmp_path, monkeypatch):
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    monkeypatch.setattr(
        "app.services.folder_service.get_settings",
        lambda: SimpleNamespace(LABS_DIR=labs_dir),
    )
    return labs_dir


class TestSharedListing:
    def test_root_listing_synthesizes_shared_when_dir_missing(self, empty_labs_dir):
        result = FolderService.list_folder("")
        names = [f["name"] for f in result["folders"]]
        # The pre-#285 static stub order is preserved on a fresh tree.
        assert names == ["Running", "Shared", "Users"]

    def test_root_listing_dedupes_shared_when_dir_exists(self, empty_labs_dir):
        (empty_labs_dir / "Shared").mkdir()
        result = FolderService.list_folder("")
        shared = [f for f in result["folders"] if f["path"] == "/Shared"]
        assert len(shared) == 1
        names = [f["name"] for f in result["folders"]]
        assert names[0] == "Running"
        assert "Users" in names

    def test_scoped_user_root_gets_shared_entry(self, empty_labs_dir):
        (empty_labs_dir / "lanes" / "alpha" / "inner").mkdir(parents=True)
        result = FolderService.list_folder("lanes/alpha", recursive=True, at_user_root=True)
        assert "/Shared" in [f["path"] for f in result["folders"]]

    def test_scoped_user_root_dedupes_shared(self, empty_labs_dir):
        (empty_labs_dir / "lanes" / "alpha").mkdir(parents=True)
        (empty_labs_dir / "Shared").mkdir()
        result = FolderService.list_folder("lanes/alpha", recursive=True, at_user_root=True)
        shared = [f for f in result["folders"] if f["path"] == "/Shared"]
        assert len(shared) == 1

    def test_entering_shared_creates_dir_lazily(self, empty_labs_dir):
        result = FolderService.list_folder("Shared")
        assert (empty_labs_dir / "Shared").is_dir()
        assert result == {"folders": [], "labs": []}

    def test_at_user_root_default_false_keeps_nested_listings_unchanged(self, empty_labs_dir):
        (empty_labs_dir / "lanes" / "alpha").mkdir(parents=True)
        result = FolderService.list_folder("lanes/alpha", recursive=True)
        assert "/Shared" not in [f["path"] for f in result["folders"]]
