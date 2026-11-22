# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Session recordings API (#291).

Per-lab listing, viewing and download of asciinema cast files and raw logs
produced by ``app.services.console_recording``. Routes live under
``/api/labs`` and this router must be included BEFORE ``labs.router`` so the
``/recordings`` suffixes win over the lab catch-all.

Recordings capture console output and keystrokes, so every endpoint enforces
the same scoped-lab permissions as the lab itself.
"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.config import get_settings
from app.dependencies import get_current_user
from app.schemas.user import UserRead
from app.services.console_recording import _slug, get_console_recorder

from app.routers.labs import _read_lab_data, _scoped_lab_path

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labs", tags=["labs"])

CONTENT_TAIL_BYTES = 512 * 1024
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(cast|log)$")
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _lab_id_for(current_user: UserRead, lab_path: str) -> str:
    data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
    return str(data.get("id") or lab_path)


def _lab_recordings_dir(lab_id: str) -> Path:
    return Path(get_settings().RECORDINGS_DIR) / _slug(lab_id)


def _cast_duration(path: Path) -> float:
    """Last event timestamp in the cast = session duration in seconds."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.strip().splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, list) and event and isinstance(event[0], (int, float)):
                return round(float(event[0]), 1)
    except OSError:
        pass
    return 0.0


def _entry_for(cast_path: Path, node_id: int) -> dict:
    started_at = None
    title = ""
    try:
        with cast_path.open("r", encoding="utf-8") as fh:
            header = json.loads(fh.readline())
        started_at = datetime.fromtimestamp(int(header.get("timestamp", 0)), tz=UTC).isoformat()
        title = str(header.get("title") or "")
    except (OSError, ValueError):
        pass
    if not started_at:
        started_at = datetime.fromtimestamp(cast_path.stat().st_mtime, tz=UTC).isoformat()
    log_path = cast_path.with_suffix(".log")
    return {
        "id": f"{node_id}--{cast_path.name}",
        "node_id": node_id,
        "filename": cast_path.name,
        "title": title,
        "started_at": started_at,
        "duration": _cast_duration(cast_path),
        "cast_size": cast_path.stat().st_size,
        "log_size": log_path.stat().st_size if log_path.exists() else 0,
        "in_progress": get_console_recorder().is_active(cast_path),
    }


def _resolve_recording(lab_id: str, recording_id: str, suffix: str) -> Path:
    """Map ``<node_id>--<filename>`` to a file inside the lab recordings dir."""
    node_dir, sep, filename = recording_id.partition("--")
    if not sep or not node_dir.isdigit() or not _FILENAME_RE.match(filename):
        raise FileNotFoundError("Unknown recording")
    base = _lab_recordings_dir(lab_id)
    path = (base / node_dir / filename).with_suffix(suffix)
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()) or not resolved.is_file():
        raise FileNotFoundError("Unknown recording")
    return resolved


@router.get("/{lab_path:path}/recordings")
async def list_recordings(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        lab_id = _lab_id_for(current_user, lab_path)
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}

    base = _lab_recordings_dir(lab_id)
    entries = []
    if base.is_dir():
        for node_dir in base.iterdir():
            if not node_dir.is_dir() or not node_dir.name.isdigit():
                continue
            for cast_path in node_dir.glob("*.cast"):
                try:
                    entries.append(_entry_for(cast_path, int(node_dir.name)))
                except OSError:
                    continue
    entries.sort(key=lambda e: e["started_at"], reverse=True)
    return {"code": 200, "status": "success", "message": "OK", "data": entries}


@router.get("/{lab_path:path}/recordings/{recording_id}/cast")
async def download_cast(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        lab_id = _lab_id_for(current_user, lab_path)
        path = _resolve_recording(lab_id, recording_id, ".cast")
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Recording not found."}
    return FileResponse(path, media_type="application/x-asciicast", filename=path.name)


@router.get("/{lab_path:path}/recordings/{recording_id}/log")
async def download_log(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        lab_id = _lab_id_for(current_user, lab_path)
        path = _resolve_recording(lab_id, recording_id, ".log")
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Recording not found."}
    return FileResponse(path, media_type="text/plain", filename=path.name)


@router.get("/{lab_path:path}/recordings/{recording_id}/content")
async def recording_content(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
):
    """ANSI-stripped log text for the in-browser viewer (tail-capped)."""
    try:
        lab_id = _lab_id_for(current_user, lab_path)
        path = _resolve_recording(lab_id, recording_id, ".log")
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Recording not found."}

    size = path.stat().st_size
    truncated = size > CONTENT_TAIL_BYTES
    with path.open("rb") as fh:
        fh.seek(max(0, size - CONTENT_TAIL_BYTES))
        raw = fh.read().decode("utf-8", errors="replace")
    text = _ANSI_RE.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
    return {
        "code": 200,
        "status": "success",
        "message": "OK",
        "data": {"text": text, "truncated": truncated},
    }
