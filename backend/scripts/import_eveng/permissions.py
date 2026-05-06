"""Owner/perm fixup for the EVE-NG importer (#184).

After every successful file or directory creation, chown to the resolved
``APP_OWNER:APP_GROUP`` and chmod (0755 for dirs, 0644 for files). This also
heals the pre-existing ``root:root`` ownership on ``<dest>/qemu/`` parent dirs
that earlier nova-ve releases left behind.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ._app_owner import AppOwner

_DIR_MODE = 0o755
_FILE_MODE = 0o644

_logger = logging.getLogger("nova_ve.import_eveng")


def fixup(path: Path, owner: AppOwner) -> None:
    """Apply the canonical owner + mode to ``path``.

    Idempotent: if the path is already owned/moded correctly, nothing changes.
    Symlinks are chown'd via :func:`os.lchown` (no chmod — the link's mode is
    not meaningful on Linux) so that a symlink to a relative target keeps its
    mode untouched and the target file's mode is left to the file-level fixup.
    """
    is_link = path.is_symlink()
    is_dir = path.is_dir() and not is_link

    if is_link:
        os.lchown(path, owner.uid, owner.gid)
        return

    os.chown(path, owner.uid, owner.gid)
    desired = _DIR_MODE if is_dir else _FILE_MODE
    current = path.stat().st_mode & 0o777
    if current != desired:
        path.chmod(desired)


def fixup_tree(root: Path, owner: AppOwner) -> int:
    """Recursively apply :func:`fixup` to ``root`` and everything beneath it.

    Returns the number of paths touched. Healing the pre-existing root-owned
    ``<dest>/qemu/`` parent dir bug is just a normal call against the parent.
    """
    if not root.exists():
        return 0

    fixup(root, owner)
    count = 1
    for current_root, dirs, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in dirs:
            fixup(current / name, owner)
            count += 1
        for name in files:
            fixup(current / name, owner)
            count += 1
    return count
