#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Snapshot / restore ``/etc/netplan`` YAML files with symlink refusal.

Refuses to read OR write through a symlink at both snapshot and restore.
Copies are written ``0600 root:root``.  See
.omc/plans/bridge-cloud-feature.md §4.6 / §6.2 T3 for the threat model.
"""

from __future__ import annotations

import glob
import os
import shutil
import sys


def _check_no_symlinks(path: str) -> None:
    if os.path.islink(path):
        sys.stderr.write(f"refusing symlink: {path}\n")
        sys.exit(1)


def snapshot(src_dir: str, dst_dir: str) -> None:
    _check_no_symlinks(src_dir)
    _check_no_symlinks(dst_dir)
    os.makedirs(dst_dir, exist_ok=True)
    for src in sorted(glob.glob(os.path.join(src_dir, "*.yaml"))):
        _check_no_symlinks(src)
        dst = os.path.join(dst_dir, os.path.basename(src))
        shutil.copy2(src, dst, follow_symlinks=False)
        os.chmod(dst, 0o600)
        try:
            os.chown(dst, 0, 0)
        except (OSError, PermissionError):  # pragma: no cover — non-root testing
            pass


def restore(src_dir: str, dst_dir: str) -> None:
    _check_no_symlinks(src_dir)
    _check_no_symlinks(dst_dir)
    for src in sorted(glob.glob(os.path.join(src_dir, "*.yaml"))):
        _check_no_symlinks(src)
        dst = os.path.join(dst_dir, os.path.basename(src))
        if os.path.islink(dst):
            sys.stderr.write(f"refusing to restore over symlink: {dst}\n")
            sys.exit(1)
        shutil.copy2(src, dst, follow_symlinks=False)
        os.chmod(dst, 0o600)
        try:
            os.chown(dst, 0, 0)
        except (OSError, PermissionError):  # pragma: no cover — non-root testing
            pass


def _usage() -> None:
    sys.stderr.write("usage: nova-ve-backup.py {snapshot|restore} <src> <dst>\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        _usage()
        return 2
    op, src, dst = argv
    if op == "snapshot":
        snapshot(src, dst)
    elif op == "restore":
        restore(src, dst)
    else:
        _usage()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
