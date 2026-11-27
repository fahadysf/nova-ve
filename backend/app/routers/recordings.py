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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user
from app.routers.labs import LAB_ACCESS, _read_lab_data, _recording_lab_file, _scoped_lab_path
from app.schemas.user import UserRead
from app.services import audit_service
from app.services.console_recording import (
    RECORDING_FILENAME_RE,
    _slug,
    delete_recording_files,
    get_console_recorder,
    lab_recordings_dir,
    read_recording_header,
    recording_lab_id,
)
from app.services.lab_service import LabService, lab_membership_lock

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labs", tags=["labs"])

CONTENT_TAIL_BYTES = 512 * 1024
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class RecordingControl(BaseModel):
    action: Literal["start", "stop"]
    console: Literal["primary", "second"] = "primary"


def _recording_lab(current_user: UserRead, lab_path: str) -> tuple[str, Path, dict]:
    scoped = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    lab_file = _recording_lab_file(scoped)
    return scoped, lab_file, _read_lab_data(scoped)


def _lab_for_audit(current_user: UserRead, lab_path: str) -> tuple[str, dict | None]:
    """Scoped path and lab data to describe an audited recording with.

    #300: called after the action has already succeeded, so it must never
    turn that success into an error — a lab that cannot be read is named
    by its path alone.
    """
    try:
        scoped, _, data = _recording_lab(current_user, lab_path)
    except Exception:  # noqa: BLE001 — the audit entry must not fail the action
        return lab_path, None
    return scoped, data


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


def _seconds_since(moment: object) -> float | None:
    """Whole seconds from an ISO timestamp to now, or None if unusable."""
    try:
        started = datetime.fromisoformat(str(moment))
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (datetime.now(UTC) - started).total_seconds()))


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
    if not sep or not node_dir.isascii() or not node_dir.isdigit() or not RECORDING_FILENAME_RE.fullmatch(filename):
        raise FileNotFoundError("Recording not found.")
    scope = await _recording_scope(current_user, lab_path, db)
    for base in scope.directories:
        cast_path = (base / node_dir / filename).with_suffix(".cast")
        path = cast_path.with_suffix(suffix)
        if not _is_regular_path(cast_path):
            continue
        if not scope.can_view(cast_path, read_recording_header(cast_path)):
            raise PermissionError("Access denied.")
        if _is_regular_path(path):
            return path
        break
    raise FileNotFoundError("Recording not found.")


@router.delete("/{lab_path:path}/recordings/{recording_id}", dependencies=[LAB_ACCESS])
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
    # #300: describe the recording from its header before the file is gone.
    node_dir, _, _ = recording_id.partition("--")
    header = read_recording_header(cast)
    metadata = _recording_metadata(header)
    duration = header.get("duration")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        duration = _cast_duration(cast)
    try:
        delete_recording_files(cast)
    except ValueError as exc:
        return {"code": 409, "status": "fail", "message": str(exc)}
    except OSError:
        _logger.exception("Could not delete console recording %s", cast)
        return {"code": 500, "status": "fail", "message": "Could not delete the recording."}
    scoped, lab_data = _lab_for_audit(current_user, lab_path)
    await audit_service.record(
        "recording.delete",
        actor=current_user.username,
        target_type="recording",
        target_id=recording_id,
        target_name=metadata.get("node_name"),
        **audit_service.lab_context(
            lab_data,
            scoped,
            detail={
                "node_id": int(node_dir) if node_dir.isdigit() else None,
                "recorded_by": metadata.get("username"),
                "console_mode": metadata.get("console"),
                "duration": duration,
            },
        ),
    )
    return {"code": 200, "status": "success", "message": "Recording deleted.", "data": {"id": recording_id}}


@router.get("/{lab_path:path}/recordings", dependencies=[LAB_ACCESS])
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
                    header = read_recording_header(cast_path)
                    if scope.can_view(cast_path, header):
                        entry = _entry_for(cast_path, int(node_dir.name), header)
                        entries.setdefault(entry["id"], entry)
                except (OSError, ValueError, OverflowError):
                    continue
    ordered = sorted(entries.values(), key=lambda entry: entry["started_at"], reverse=True)
    return {"code": 200, "status": "success", "message": "OK", "data": ordered}


class RecordingFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        path = self.path
        cast = path.with_suffix(".cast")
        if not _is_regular_path(cast) or not _is_regular_path(path):
            response = JSONResponse({"code": 404, "status": "fail", "message": "Recording not found."})
            await response(scope, receive, send)
            return
        with get_console_recorder().downloading(cast):
            await super().__call__(scope, receive, send)


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
    return RecordingFileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/{lab_path:path}/recordings/{recording_id}/cast", dependencies=[LAB_ACCESS])
async def download_cast(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _download_recording(current_user, lab_path, recording_id, ".cast", "application/x-asciicast", db)


@router.get("/{lab_path:path}/recordings/{recording_id}/log", dependencies=[LAB_ACCESS])
async def download_log(
    lab_path: str,
    recording_id: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _download_recording(current_user, lab_path, recording_id, ".log", "text/plain", db)


@router.get("/{lab_path:path}/recordings/{recording_id}/content", dependencies=[LAB_ACCESS])
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


@router.get("/{lab_path:path}/nodes/{node_id}/recording", dependencies=[LAB_ACCESS])
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


@router.post("/{lab_path:path}/nodes/{node_id}/recording", dependencies=[LAB_ACCESS])
async def node_recording_control(
    lab_path: str,
    node_id: int,
    body: RecordingControl,
    current_user: UserRead = Depends(get_current_user),
):
    """On-demand record button (#291): start/stop a recording cycle."""
    try:
        scoped = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        async with lab_membership_lock(scoped):
            session = _session_for(current_user, lab_path, node_id, body.console)
            if session is None:
                return {
                    "code": 404,
                    "status": "fail",
                    "message": "No active console session for this node. Open the console first.",
                }
            status = session.status()
            was_recording = bool(status.get("recording"))
            try:
                data = session.start_recording() if body.action == "start" else session.stop_recording()
            except ValueError as exc:
                return {"code": 400, "status": "fail", "message": str(exc)}
            except OSError:
                _logger.exception("Could not write console recording.")
                return {"code": 500, "status": "fail", "message": "Could not save console recording."}
    except PermissionError as exc:
        return {"code": 403, "status": "fail", "message": str(exc)}
    except (FileNotFoundError, ValueError):
        return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
    # #300: only a real change of state is an event. Starting a recording
    # that is already running — or stopping one that is not — is a no-op.
    if bool(data.get("recording")) != was_recording:
        scoped, lab_data = _lab_for_audit(current_user, lab_path)
        detail = {"console_mode": body.console}
        if body.action == "stop":
            detail["duration"] = _seconds_since(status.get("started_at"))
        await audit_service.record(
            f"recording.{body.action}",
            actor=current_user.username,
            **audit_service.node_context(lab_data, node_id, scoped, detail=detail),
        )
    return {
        "code": 200,
        "status": "success",
        "message": "Recording started." if body.action == "start" else "Recording stopped.",
        "data": data,
    }
