# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Session recordings API (#291).

Per-lab listing, viewing, download and deletion of asciinema cast files and raw logs
produced by ``app.services.console_recording``. Routes live under
``/api/labs`` and this router must be included BEFORE ``labs.router`` so the
``/recordings`` suffixes win over the lab catch-all.

Recordings capture console output and keystrokes. Access requires
both lab scope and recorder, lab owner, or administrator access.
"""

import json
import logging
import math
import re
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user
from app.routers.labs import _read_lab_data, _recording_lab_file, _scoped_lab_path
from app.schemas.user import UserRead
from app.services.console_recording import _slug, get_console_recorder, lab_recordings_dir, recording_lab_id
from app.services.lab_service import LabService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labs", tags=["labs"])

CONTENT_TAIL_BYTES = 512 * 1024
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(cast|log)$")
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class RecordingControl(BaseModel):
    action: Literal["start", "stop"]
    console: Literal["primary", "second"] = "primary"


def _recording_lab(current_user: UserRead, lab_path: str) -> tuple[str, Path, dict]:
    scoped = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    lab_file = _recording_lab_file(scoped)
    return scoped, lab_file, _read_lab_data(scoped)


@dataclass
class RecordingScope:
    directories: list[Path]
    username: str
    can_view_all: bool

    def can_view(self, cast_path: Path, header: dict) -> bool:
        if self.can_view_all:
            return True
        username = _recording_metadata(header).get("username")
        if username is None:
            log_path = cast_path.with_suffix(".log")
            if _is_regular_path(log_path):
                with log_path.open(encoding="utf-8", errors="replace") as log:
                    marker = re.fullmatch(r"# Recording started [^ ]+ by ([^\n]+)\n", log.readline(1024))
                username = marker[1] if marker else None
        return username == self.username


async def _recording_scope(current_user: UserRead, lab_path: str, db: AsyncSession) -> RecordingScope:
    scoped, lab_file, data = _recording_lab(current_user, lab_path)
    directories = [lab_recordings_dir(lab_file, recording_lab_id(lab_file, data))]
    legacy_root = Path(get_settings().RECORDINGS_DIR).resolve()
    legacy_dir = legacy_root / _slug(data.get("id") or lab_path)
    if legacy_dir != legacy_root and legacy_dir.resolve() == legacy_dir and legacy_dir not in directories:
        directories.append(legacy_dir)
    can_view_all = current_user.role == "admin"
    if not can_view_all:
        lab = await LabService(db).get_lab_by_filename(scoped)
        can_view_all = lab is not None and lab.owner == current_user.username
    return RecordingScope(directories, current_user.username, can_view_all)


def _is_regular_path(path: Path) -> bool:
    return path.resolve() == path and path.is_file()


def _read_header(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as cast:
            header = json.loads(cast.readline(65536))
        return header if isinstance(header, dict) else {}
    except ValueError:
        return {}


def _recording_metadata(header: dict) -> dict:
    metadata = header.get("nova")
    return metadata if isinstance(metadata, dict) else {}


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


def _entry_for(cast_path: Path, node_id: int, header: dict) -> dict:
    metadata = _recording_metadata(header)
    title = str(header.get("title") or "")
    started_at = metadata.get("started_at")
    try:
        started_dt = (
            datetime.fromisoformat(started_at)
            if isinstance(started_at, str)
            else datetime.fromtimestamp(int(header["timestamp"]), tz=UTC)
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        started_dt = datetime.fromtimestamp(cast_path.stat().st_mtime, tz=UTC)
    duration = header.get("duration")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        duration = _cast_duration(cast_path)
    in_progress = get_console_recorder().is_active(cast_path)
    stopped_at = None
    if not in_progress:
        stopped_at = metadata.get("stopped_at") or (started_dt + timedelta(seconds=duration)).isoformat()
    log_path = cast_path.with_suffix(".log")
    return {
        "id": f"{node_id}--{cast_path.name}",
        "node_id": node_id,
        "node_name": metadata.get("node_name") or title.partition(" / ")[2] or None,
        "console": metadata.get("console"),
        "filename": cast_path.name,
        "title": title,
        "started_at": started_dt.isoformat(),
        "stopped_at": stopped_at,
        "duration": duration,
        "cast_size": cast_path.stat().st_size,
        "log_size": log_path.stat().st_size if _is_regular_path(log_path) else 0,
        "in_progress": in_progress,
    }


async def _resolve_recording(
    current_user: UserRead,
    lab_path: str,
    recording_id: str,
    suffix: str,
    db: AsyncSession,
) -> Path:
    """Resolve a recording ID and enforce identical access for all formats."""
    node_dir, sep, filename = recording_id.partition("--")
    if not sep or not node_dir.isascii() or not node_dir.isdigit() or not _FILENAME_RE.fullmatch(filename):
        raise FileNotFoundError("Recording not found.")
    scope = await _recording_scope(current_user, lab_path, db)
    for base in scope.directories:
        cast_path = (base / node_dir / filename).with_suffix(".cast")
        path = cast_path.with_suffix(suffix)
        if not _is_regular_path(cast_path):
            continue
        if not scope.can_view(cast_path, _read_header(cast_path)):
            raise PermissionError("Access denied.")
        if _is_regular_path(path):
            return path
        break
    raise FileNotFoundError("Recording not found.")


def _delete_recording_files(cast: Path) -> None:
    log = cast.with_suffix(".log")
    try:
        mode = log.lstat().st_mode
    except FileNotFoundError:
        cast.unlink()
        return
    if not stat.S_ISREG(mode):
        raise ValueError("The recording has an invalid log file.")

    # Keep the log recoverable until the cast is removed. The log can hold
    # the only recorder identity for legacy recordings, so a failed cast
    # removal must leave the original pair readable and retryable.
    staging_dir = Path(tempfile.mkdtemp(prefix=".delete-", dir=cast.parent))
    staged_log = staging_dir / log.name
    try:
        log.rename(staged_log)
        try:
            cast.unlink()
        except OSError:
            staged_log.rename(log)
            raise
        staged_log.unlink()
    finally:
        with suppress(OSError):
            staging_dir.rmdir()


@router.delete("/{lab_path:path}/recordings/{recording_id}")
async def delete_recording(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        cast = await _resolve_recording(current_user, lab_path, recording_id, ".cast", db)
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except (FileNotFoundError, ValueError, RuntimeError):
        return {"code": 404, "status": "fail", "message": "Recording not found."}

    # Recording controls and filesystem changes run synchronously after
    # authorization, so another request cannot interleave with this check.
    if get_console_recorder().is_active(cast):
        return {"code": 409, "status": "fail", "message": "Stop the recording before deleting it."}
    try:
        _delete_recording_files(cast)
    except ValueError as exc:
        return {"code": 409, "status": "fail", "message": str(exc)}
    except OSError:
        _logger.exception("Could not delete console recording %s", cast)
        return {"code": 500, "status": "fail", "message": "Could not delete the recording."}
    return {"code": 200, "status": "success", "message": "Recording deleted.", "data": {"id": recording_id}}


@router.get("/{lab_path:path}/recordings")
async def list_recordings(
    lab_path: str,
    node_id: int | None = None,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        scope = await _recording_scope(current_user, lab_path, db)
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
    except ValueError:
        return {"code": 400, "status": "fail", "message": "Invalid lab or recordings path."}

    entries = {}
    for base in scope.directories:
        if not base.is_dir():
            continue
        node_dirs = [base / str(node_id)] if node_id is not None else base.iterdir()
        for node_dir in node_dirs:
            if not node_dir.name.isascii() or not node_dir.name.isdigit() or node_dir.resolve() != node_dir:
                continue
            for cast_path in node_dir.glob("*.cast"):
                try:
                    if not _is_regular_path(cast_path):
                        continue
                    header = _read_header(cast_path)
                    if scope.can_view(cast_path, header):
                        entry = _entry_for(cast_path, int(node_dir.name), header)
                        entries.setdefault(entry["id"], entry)
                except (OSError, ValueError, OverflowError):
                    continue
    ordered = sorted(entries.values(), key=lambda entry: entry["started_at"], reverse=True)
    return {"code": 200, "status": "success", "message": "OK", "data": ordered}


async def _download_recording(
    current_user: UserRead,
    lab_path: str,
    recording_id: str,
    suffix: str,
    media_type: str,
    db: AsyncSession,
):
    try:
        path = await _resolve_recording(current_user, lab_path, recording_id, suffix, db)
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except (FileNotFoundError, ValueError):
        return {"code": 404, "status": "fail", "message": "Recording not found."}
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/{lab_path:path}/recordings/{recording_id}/cast")
async def download_cast(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _download_recording(current_user, lab_path, recording_id, ".cast", "application/x-asciicast", db)


@router.get("/{lab_path:path}/recordings/{recording_id}/log")
async def download_log(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _download_recording(current_user, lab_path, recording_id, ".log", "text/plain", db)


@router.get("/{lab_path:path}/recordings/{recording_id}/content")
async def recording_content(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """ANSI-stripped log text for the in-browser viewer (tail-capped)."""
    try:
        path = await _resolve_recording(current_user, lab_path, recording_id, ".log", db)
        size = path.stat().st_size
        truncated = size > CONTENT_TAIL_BYTES
        with path.open("rb") as fh:
            fh.seek(max(0, size - CONTENT_TAIL_BYTES))
            raw = fh.read().decode("utf-8", errors="replace")
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except (FileNotFoundError, ValueError):
        return {"code": 404, "status": "fail", "message": "Recording not found."}

    text = _ANSI_RE.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
    return {
        "code": 200,
        "status": "success",
        "message": "OK",
        "data": {"text": text, "truncated": truncated},
    }


def _session_for(current_user: UserRead, lab_path: str, node_id: int, console: str):
    _, lab_file, data = _recording_lab(current_user, lab_path)
    return get_console_recorder().find(
        lab_file, recording_lab_id(lab_file, data), node_id, console, current_user.username
    )


@router.get("/{lab_path:path}/nodes/{node_id}/recording")
async def node_recording_status(
    lab_path: str,
    node_id: int,
    console: Literal["primary", "second"] = "primary",
    current_user: UserRead = Depends(get_current_user),
):
    """Recording state of the caller's live console session for this node."""
    try:
        session = _session_for(current_user, lab_path, node_id, console)
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except (FileNotFoundError, ValueError):
        return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
    data = session.status() if session is not None else {"console_active": False, "recording": False}
    return {"code": 200, "status": "success", "message": "OK", "data": data}


@router.post("/{lab_path:path}/nodes/{node_id}/recording")
async def node_recording_control(
    lab_path: str,
    node_id: int,
    body: RecordingControl,
    current_user: UserRead = Depends(get_current_user),
):
    """On-demand record button (#291): start/stop a recording cycle."""
    try:
        session = _session_for(current_user, lab_path, node_id, body.console)
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except (FileNotFoundError, ValueError):
        return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
    if session is None:
        return {
            "code": 404,
            "status": "fail",
            "message": "No active console session for this node. Open the console first.",
        }
    try:
        data = session.start_recording() if body.action == "start" else session.stop_recording()
    except ValueError as exc:
        return {"code": 400, "status": "fail", "message": str(exc)}
    except OSError:
        _logger.exception("Could not write console recording.")
        return {"code": 500, "status": "fail", "message": "Could not save console recording."}
    return {
        "code": 200,
        "status": "success",
        "message": f"Recording {body.action}ed.",
        "data": data,
    }
