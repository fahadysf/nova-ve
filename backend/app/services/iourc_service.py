# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.config import get_settings

IOURC_CANONICAL_NAME = "iourc"
IOURC_MAX_BYTES = 1024 * 1024


class IourcError(ValueError):
    pass


@dataclass(frozen=True)
class IourcStatus:
    installed: bool
    directory: Path
    path: Path | None
    size: int | None
    hostname: str
    fqdn: str
    ips: list[str]
    host_id: str | None

    def as_dict(self) -> dict:
        return {
            "installed": self.installed,
            "directory": str(self.directory),
            "path": str(self.path) if self.path is not None else None,
            "filename": self.path.name if self.path is not None else None,
            "size": self.size,
            "hostname": self.hostname,
            "fqdn": self.fqdn,
            "ips": self.ips,
            "host_id": self.host_id,
        }


def iourc_directory() -> Path:
    return get_settings().IOURC_DIR


def find_iourc_file(directory: Path | None = None) -> Path | None:
    directory = directory or iourc_directory()
    if not directory.is_dir():
        return None

    preferred = [directory / IOURC_CANONICAL_NAME, directory / ".iourc"]
    for candidate in preferred:
        if _valid_iourc_candidate(candidate):
            return candidate

    for candidate in sorted(directory.iterdir(), key=lambda item: item.name):
        if _valid_iourc_candidate(candidate):
            return candidate
    return None


def status(directory: Path | None = None) -> IourcStatus:
    directory = directory or iourc_directory()
    candidate = find_iourc_file(directory)
    size = candidate.stat().st_size if candidate is not None else None
    host_info = host_identity()
    return IourcStatus(
        installed=candidate is not None,
        directory=directory,
        path=candidate,
        size=size,
        hostname=host_info["hostname"],
        fqdn=host_info["fqdn"],
        ips=host_info["ips"],
        host_id=host_info["host_id"],
    )


def store_uploaded_iourc(content: bytes, directory: Path | None = None) -> IourcStatus:
    directory = directory or iourc_directory()
    if not content or not content.strip():
        raise IourcError("Uploaded IOURC file is empty.")
    if len(content) > IOURC_MAX_BYTES:
        raise IourcError("Uploaded IOURC file is too large.")

    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = directory / IOURC_CANONICAL_NAME

    with NamedTemporaryFile("wb", delete=False, dir=directory, prefix=".iourc-", suffix=".tmp") as handle:
        tmp_path = Path(handle.name)
        handle.write(content)
    try:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
        os.chmod(target, 0o600)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

    return status(directory)


def host_identity() -> dict[str, object]:
    hostname = socket.gethostname()
    fqdn = socket.getfqdn() or hostname
    ips = _host_ips(hostname, fqdn)
    return {
        "hostname": hostname,
        "fqdn": fqdn,
        "ips": ips,
        "host_id": _host_id(),
    }


def _valid_iourc_candidate(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _host_ips(hostname: str, fqdn: str) -> list[str]:
    addresses: set[str] = set()
    for name in {hostname, fqdn}:
        try:
            for family, _, _, _, sockaddr in socket.getaddrinfo(name, None):
                if family == socket.AF_INET:
                    addresses.add(str(sockaddr[0]))
        except socket.gaierror:
            continue

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass

    return sorted(addresses)


def _host_id() -> str | None:
    try:
        result = subprocess.run(
            ["hostid"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None
