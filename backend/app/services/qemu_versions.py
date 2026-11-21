"""Installed side-by-side QEMU version registry (#278).

nova-ve's install.sh optionally provisions multi-version QEMU builds under
``/opt/nova-ve/qemu/<version>/`` (companion ``nova-ve-qemu`` deb packages).
Each tree is self-contained::

    /opt/nova-ve/qemu/<version>/bin/qemu-system-<arch>

This module is the single authority on what is installed: the boot-time
binary resolution, the ``/api/system/qemu-versions`` listing, and the node
form dropdown all read through it. The distribution QEMU
(``Settings.QEMU_BINARY``) stays the global default and is untouched by
anything here.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

from app.config import get_settings

_VERSION_DIR_RE = re.compile(r"^[0-9]+(\.[0-9]+)+$")


def _version_sort_key(version: str) -> tuple[int, ...]:
    """Numeric-aware sort key so 4.10.0 sorts after 4.2.0."""
    return tuple(int(part) for part in version.split("."))


def _versions_root() -> Path:
    return Path(get_settings().QEMU_VERSIONS_ROOT)


def version_binary(version: str, arch: str = "x86_64") -> Path:
    """Path where ``<version>``'s ``qemu-system-<arch>`` would live."""
    return _versions_root() / version / "bin" / f"qemu-system-{arch}"


def installed_versions(arch: str = "x86_64") -> list[str]:
    """Versions currently installed under the registry root.

    A directory counts as installed only when its version-named binary
    exists. Sorted ascending, oldest first (numeric-aware). Returns ``[]``
    when the root does not exist — a host without the pack simply offers no
    alternatives.
    """
    root = _versions_root()
    if not root.is_dir():
        return []
    found: list[str] = []
    for entry in root.iterdir():
        if not entry.is_dir() or not _VERSION_DIR_RE.match(entry.name):
            continue
        if version_binary(entry.name, arch).is_file():
            found.append(entry.name)
    return sorted(found, key=_version_sort_key)


def resolve_version_binary(version: str, arch: str = "x86_64") -> Path | None:
    """Resolve a requested registry version to its binary path.

    Returns ``None`` when the version is not installed (the caller turns
    that into the typed boot error — never a bare FileNotFoundError).
    """
    if not version:
        return None
    binary = version_binary(version, arch)
    return binary if binary.is_file() else None


@functools.lru_cache(maxsize=8)
def _cached_binary_version(binary: str) -> str:
    """Parse ``<binary> --version`` once per binary path."""
    try:
        out = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    match = re.search(r"version\s+([0-9][^\s,)]*)", out.stdout)
    return match.group(1) if match else "unknown"


def default_version() -> str:
    """Version string of the distribution QEMU (``Settings.QEMU_BINARY``)."""
    settings = get_settings()
    return _cached_binary_version(settings.QEMU_BINARY)


def catalog() -> dict[str, object]:
    """Payload for ``GET /api/system/qemu-versions``."""
    versions = installed_versions()
    return {
        "default": {
            "binary": get_settings().QEMU_BINARY,
            "version": default_version(),
        },
        "versions": [
            {
                "version": version,
                "binary": str(version_binary(version)),
            }
            for version in versions
        ],
    }
