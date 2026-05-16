# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Docker image curation for nova-ve labs.

Lab nodes pull from a curated subset of locally available Docker images, not
from every image the host happens to have. This module owns the local
convention: an image counts as lab-available iff it carries a marker tag in
the ``nova-ve-lab/`` namespace.

Marking is reversible (``docker rmi`` of the marker tag does not delete the
underlying image data because Docker reference-counts by ID). The original
``repository:tag`` stays canonical -- saved lab JSON keeps referencing
``alpine:latest`` regardless of marker state, and a lab opens on a fresh host
once the operator marks the same image there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings

MARKER_NAMESPACE = "nova-ve-lab"
_MARKER_PREFIX = f"{MARKER_NAMESPACE}/"
_SANITIZE_RE = re.compile(r"[^a-z0-9_.\-]+")


class DockerImageError(RuntimeError):
    """User-actionable failure from a docker subprocess."""


@dataclass(frozen=True)
class _DockerCmd:
    binary: str
    env: dict[str, str] = field(default_factory=dict)


def _resolve_docker() -> _DockerCmd | None:
    binary = shutil.which("docker")
    if not binary:
        return None
    env = os.environ.copy()
    host = getattr(get_settings(), "DOCKER_HOST", "") or ""
    if host:
        env["DOCKER_HOST"] = host
    return _DockerCmd(binary=binary, env=env)


def _run(cmd: _DockerCmd, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [cmd.binary, *args],
        capture_output=True,
        text=True,
        check=False,
        env=cmd.env,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "docker command failed"
        raise DockerImageError(stderr)
    return proc


def _sanitize_repo_for_marker(repo: str) -> str:
    """Turn ``repo`` into a path-safe single segment for the marker namespace.

    Docker reference grammar requires every path component to be lowercase
    alphanumerics with ``_``, ``.``, or ``-``. Registry hosts (with ``:`` for
    a port) and multi-segment paths both collapse to a single segment so the
    marker namespace stays exactly one level deep.
    """
    lowered = repo.strip().lower()
    flat = lowered.replace("/", "_").replace(":", "_")
    sanitized = _SANITIZE_RE.sub("_", flat).strip("_")
    return sanitized or "untitled"


def marker_for(reference: str) -> str:
    """Compute the marker tag we would add for ``reference`` (``repo:tag``).

    Bare references default to ``:latest`` so the marker mirrors what
    ``docker pull`` would have produced.
    """
    repo, _, tag = reference.partition(":")
    return f"{_MARKER_PREFIX}{_sanitize_repo_for_marker(repo)}:{tag or 'latest'}"


def is_marker_tag(reference: str) -> bool:
    return reference.startswith(_MARKER_PREFIX)


@dataclass
class ImageRecord:
    image_id: str
    repo_tags: list[str]
    marker_tags: list[str]
    size: int
    created: str

    @property
    def marked(self) -> bool:
        return bool(self.marker_tags)

    @property
    def primary_repo_tag(self) -> str | None:
        for tag in self.repo_tags:
            if tag and tag != "<none>:<none>":
                return tag
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.image_id,
            "repo_tags": list(self.repo_tags),
            "marker_tags": list(self.marker_tags),
            "marked": self.marked,
            "size": self.size,
            "created": self.created,
            "primary_repo_tag": self.primary_repo_tag,
        }


@dataclass(frozen=True)
class ImageConsoleHints:
    vnc_port: int | None = None


class DockerImageService:
    """Read/mutate the Docker image registry for lab availability."""

    def list_all_images(self) -> list[ImageRecord]:
        """Return every local image, grouped by image ID.

        Images with only marker tags pointing at them are still returned so
        the admin UI can offer "Unmark" even if the original ``repo:tag`` was
        deleted.
        """
        cmd = _resolve_docker()
        if cmd is None:
            return []
        proc = _run(cmd, ["image", "ls", "--no-trunc", "--format", "{{json .}}"], check=False)
        if proc.returncode != 0:
            return []

        records: dict[str, ImageRecord] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            image_id = (row.get("ID") or "").strip()
            repository = (row.get("Repository") or "").strip()
            tag = (row.get("Tag") or "").strip()
            if not image_id or not repository or repository == "<none>":
                continue
            reference = f"{repository}:{tag}" if tag and tag != "<none>" else repository
            record = records.setdefault(
                image_id,
                ImageRecord(
                    image_id=image_id,
                    repo_tags=[],
                    marker_tags=[],
                    size=_parse_size(row.get("Size", "")),
                    created=str(row.get("CreatedAt") or row.get("CreatedSince") or ""),
                ),
            )
            if is_marker_tag(reference):
                if reference not in record.marker_tags:
                    record.marker_tags.append(reference)
            else:
                if reference not in record.repo_tags:
                    record.repo_tags.append(reference)
        return list(records.values())

    def list_marked_image_names(self) -> list[str]:
        """Return the canonical ``repo:tag`` strings that are marked.

        This is the data the node catalog actually wants -- it should advertise
        the upstream image name (``alpine:latest``) rather than the local
        marker name. An image marked but missing any upstream tag still shows
        up by its marker tag so the operator notices the orphan and can
        retag it.
        """
        out: list[str] = []
        for record in self.list_all_images():
            if not record.marked:
                continue
            non_marker = record.repo_tags
            if non_marker:
                out.extend(non_marker)
            else:
                out.extend(record.marker_tags)
        # Stable sort: case-insensitive by name.
        return sorted(set(out), key=str.lower)

    def console_hints(self, reference: str) -> ImageConsoleHints:
        """Return console defaults inferred from a local image's config."""
        cmd = _resolve_docker()
        if cmd is None or not reference:
            return ImageConsoleHints()
        proc = _run(cmd, ["image", "inspect", reference], check=False)
        if proc.returncode != 0:
            return ImageConsoleHints()
        try:
            payload = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return ImageConsoleHints()
        if not payload or not isinstance(payload[0], dict):
            return ImageConsoleHints()
        config = payload[0].get("Config") or {}
        if not isinstance(config, dict):
            return ImageConsoleHints()
        return ImageConsoleHints(vnc_port=_infer_vnc_port(config))

    def mark(self, reference: str) -> str:
        """Apply the marker tag for ``reference``. Returns the marker tag."""
        reference = self._require_local(reference)
        marker = marker_for(reference)
        cmd = self._require_docker()
        _run(cmd, ["tag", reference, marker])
        return marker

    def unmark(self, reference: str) -> str:
        """Remove the marker tag corresponding to ``reference``.

        ``reference`` may be the original ``repo:tag`` or the marker tag
        itself; either way we resolve the marker tag and run ``docker rmi``
        on that tag only (the underlying image stays because other tags --
        including the original repo:tag -- still reference it).
        """
        cmd = self._require_docker()
        marker = reference if is_marker_tag(reference) else marker_for(reference)
        proc = _run(cmd, ["rmi", marker], check=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            if "No such image" in stderr or "reference does not exist" in stderr:
                # Idempotent: already gone.
                return marker
            raise DockerImageError(stderr or "docker rmi failed")
        return marker

    def pull(self, reference: str, *, mark_after: bool = True) -> dict[str, Any]:
        """Run ``docker pull`` (foreground) and optionally mark the result."""
        cmd = self._require_docker()
        if not reference or ":" not in reference:
            # Default to :latest the same way docker pull does.
            reference = f"{reference}:latest" if reference else reference
        if not reference:
            raise DockerImageError("missing image reference")
        proc = _run(cmd, ["pull", reference])
        marker: str | None = None
        if mark_after:
            marker = self.mark(reference)
        return {
            "reference": reference,
            "marker": marker,
            "output": (proc.stdout or "").strip(),
        }

    # ------------------------------------------------------------------ utils

    def _require_docker(self) -> _DockerCmd:
        cmd = _resolve_docker()
        if cmd is None:
            raise DockerImageError("docker CLI not available on this host")
        return cmd

    def _require_local(self, reference: str) -> str:
        """Confirm the image exists locally before mutating tags.

        ``docker tag`` against an unknown source emits a confusing error and
        leaves no marker behind, so a precheck makes the failure mode
        explicit for the admin UI.
        """
        if not reference:
            raise DockerImageError("missing image reference")
        cmd = self._require_docker()
        proc = _run(cmd, ["image", "inspect", reference], check=False)
        if proc.returncode != 0:
            raise DockerImageError(
                f"image not present locally: {reference!r} (pull it first or correct the tag)"
            )
        return reference


_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _parse_size(raw: str) -> int:
    """Parse Docker's human size column (``"123MB"``) into bytes.

    Returns 0 when the input is empty or unparseable; the admin UI displays
    the raw value separately so this loss-of-precision is acceptable.
    """
    raw = (raw or "").strip()
    if not raw:
        return 0
    match = re.match(r"^([0-9.]+)\s*([KMGT]?B)$", raw, flags=re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    return int(value * _SIZE_UNITS.get(unit, 1))


def _parse_port(value: Any) -> int | None:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _infer_vnc_port(config: dict[str, Any]) -> int | None:
    env = config.get("Env") or []
    if isinstance(env, list):
        for key in ("VNC_PORT", "NOVA_VE_VNC_PORT"):
            prefix = f"{key}="
            for entry in env:
                if isinstance(entry, str) and entry.startswith(prefix):
                    port = _parse_port(entry[len(prefix):])
                    if port is not None:
                        return port

    exposed = config.get("ExposedPorts") or {}
    candidates: list[int] = []
    if isinstance(exposed, dict):
        for raw in exposed.keys():
            port = _parse_port(str(raw).split("/", 1)[0])
            if port is not None:
                candidates.append(port)
    for preferred in (5900, 5901):
        if preferred in candidates:
            return preferred
    for port in sorted(candidates):
        if 5900 <= port < 6900:
            return port
    return None
