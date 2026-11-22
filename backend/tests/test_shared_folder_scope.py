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
