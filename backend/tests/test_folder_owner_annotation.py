# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""#322: folder/lab listing entries carry an additive ``owner`` field."""

from app.routers.folders import SHARED_OWNER, SYSTEM_OWNER, annotate_owners, folder_owner


def test_folder_owner_rules():
    assert folder_owner("/Shared") == SHARED_OWNER
    assert folder_owner("/Shared/team") == SHARED_OWNER
    assert folder_owner("/Running") == SYSTEM_OWNER
    assert folder_owner("/alice") == "alice"
    assert folder_owner("/alice/sub/deeper") == "alice"
    assert folder_owner("/") == SYSTEM_OWNER


def test_annotate_owners_prefers_db_owner_then_folder_owner():
    result = {
        "folders": [{"name": "alice", "path": "/alice"}, {"name": "Shared", "path": "/Shared"}],
        "labs": [
            {"file": "a.json", "path": "/alice/a.json"},
            {"file": "b.json", "path": "/alice/b.json"},
            {"file": "root.json", "path": "/root.json"},
        ],
    }
    out = annotate_owners(result, {"/alice/b.json": "bob"})
    assert [f["owner"] for f in out["folders"]] == ["alice", SHARED_OWNER]
    assert [x["owner"] for x in out["labs"]] == ["alice", "bob", SYSTEM_OWNER]
