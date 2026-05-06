"""Doc lint for the EVE-NG migration guide (#190).

Asserts the migration guide leads with the non-destructive-default warning
and documents the explicit ``--delete-source`` opt-in. This is a CC-7 /
S1 mitigation: it locks the operator-visible contract into the guide so
any future docs refactor that drops the warning is caught at PR time.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_GUIDE = REPO_ROOT / "docs" / "migrating-from-eve-ng.md"


@pytest.fixture(scope="module")
def guide_text() -> str:
    assert MIGRATION_GUIDE.is_file(), f"migration guide missing at {MIGRATION_GUIDE}"
    return MIGRATION_GUIDE.read_text()


def test_guide_mentions_non_destructive_default(guide_text: str) -> None:
    """The guide must surface the non-destructive default contract."""
    assert "non-destructive" in guide_text.lower(), (
        "migration guide must contain the literal 'non-destructive' so operators "
        "see the safe-by-default contract; see CC-7 + S1 mitigations in the plan"
    )


def test_guide_documents_delete_source_opt_in(guide_text: str) -> None:
    """The guide must mention --delete-source as the destructive opt-in flag."""
    assert "--delete-source" in guide_text, (
        "migration guide must mention --delete-source so operators know how to "
        "explicitly opt in to source deletion"
    )


def test_guide_documents_dry_run(guide_text: str) -> None:
    """The guide must mention --dry-run so operators know how to plan first."""
    assert "--dry-run" in guide_text


def test_guide_documents_manifest_paths(guide_text: str) -> None:
    """The guide must reference the manifest path so operators know where to inspect output."""
    assert "import-manifest.json" in guide_text


def test_guide_documents_needs_manual_review(guide_text: str) -> None:
    """The guide must explain how to triage 'needs-manual-review' templates."""
    assert "needs-manual-review" in guide_text
