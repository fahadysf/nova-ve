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


@pytest.mark.parametrize(
    "phrase",
    [
        "--delete-source",  # the destructive opt-in flag
        "move + symlink back",  # the #271 default
        "--copy-only",  # the conservative opt-out
        "/var/log/eve-ng-importer.log",  # the #271 log file
        "--dry-run",  # how to plan first
        "import-manifest.json",  # where to inspect output
        "needs-manual-review",  # how to triage flagged templates
    ],
)
def test_guide_documents_operator_contract(guide_text: str, phrase: str) -> None:
    """The guide must keep naming each flag, path and status operators rely on."""
    assert phrase in guide_text, f"migration guide must mention {phrase!r}"
