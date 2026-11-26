from collections.abc import Iterator
from pathlib import Path


FOLDER_DELETIONS = ".folder-deletions"


class LabMembershipPathError(PermissionError):
    """A lab mutation was requested through a noncanonical path."""


def iter_lab_json_files(root: Path) -> Iterator[Path]:
    """Scan lab files without exposing folders staged for database deletion."""
    return (
        path for path in root.rglob("*.json")
        if FOLDER_DELETIONS not in path.relative_to(root).parts
    )
