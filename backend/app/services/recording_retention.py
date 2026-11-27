# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Opt-in expiration of saved console recordings (#291)."""

import asyncio
import logging
import re
import stat
import time
from collections.abc import Iterator
from itertools import islice
from pathlib import Path

from app.config import Settings
from app.services.console_recording import (
    RECORDING_FILENAME_RE,
    RECORDINGS_FOLDER,
    ConsoleRecordingService,
    delete_recording_files,
    read_recording_header,
)
from app.services.lab_paths import FOLDER_DELETIONS

_logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 3600
_SCAN_BATCH_SIZE = 64
_DAY_NS = 86400 * 10**9
_IDENTITY_RE = re.compile(r"[A-Za-z0-9_-]{1,80}|\.[a-f0-9]{64}")


def _directories(base: Path) -> Iterator[Path]:
    try:
        for path in base.iterdir():
            if not path.is_symlink() and path.is_dir():
                yield path
    except FileNotFoundError:
        # A lab or folder may move during the read-only scan.
        return
    except OSError:
        _logger.warning("Could not scan a console recordings directory", exc_info=True)


def _recording_directories(settings: Settings) -> Iterator[Path]:
    for directory, subdirs, _ in settings.LABS_DIR.resolve().walk():
        if FOLDER_DELETIONS in subdirs:
            subdirs.remove(FOLDER_DELETIONS)
        if RECORDINGS_FOLDER in subdirs:
            subdirs.remove(RECORDINGS_FOLDER)
            sidecars = directory / RECORDINGS_FOLDER
            if sidecars.is_symlink():
                continue
            for lab in _directories(sidecars):
                yield from (path for path in _directories(lab) if _IDENTITY_RE.fullmatch(path.name))

    legacy_root = Path(settings.RECORDINGS_DIR).resolve()
    if legacy_root.is_dir():
        yield from _directories(legacy_root)


def _expired_cast(cast: Path, cutoff: int) -> bool:
    if cast.resolve() != cast:
        return False
    info = cast.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mtime_ns > cutoff:
        return False
    try:
        log_info = cast.with_suffix(".log").lstat()
    except FileNotFoundError:
        return True
    return stat.S_ISREG(log_info.st_mode) and log_info.st_mtime_ns <= cutoff


def _expired_candidates(settings: Settings, cutoff: int) -> Iterator[Path]:
    for directory in _recording_directories(settings):
        for node in _directories(directory):
            if not (node.name.isascii() and node.name.isdigit()):
                continue
            for cast in node.glob("*.cast"):
                if not RECORDING_FILENAME_RE.fullmatch(cast.name):
                    continue
                try:
                    if _expired_cast(cast, cutoff):
                        yield cast
                except FileNotFoundError:
                    continue
                except (OSError, RuntimeError):
                    _logger.warning("Could not inspect a saved console recording", exc_info=True)


async def expire_recordings(settings: Settings, recorder: ConsoleRecordingService) -> int:
    days = settings.RECORDINGS_RETENTION_DAYS
    if days == 0:
        return 0
    cutoff = time.time_ns() - days * _DAY_NS
    candidates = _expired_candidates(settings, cutoff)
    removed = 0
    while batch := await asyncio.to_thread(lambda: list(islice(candidates, _SCAN_BATCH_SIZE))):
        for cast in batch:
            # Remove on the same loop as recording and lab moves. Recheck
            # the read-only scan before mutating anything at this path.
            await asyncio.sleep(0)
            if recorder.is_active(cast) or recorder.is_downloading(cast):
                continue
            try:
                if _expired_cast(cast, cutoff) and read_recording_header(cast).get("version") == 2:
                    delete_recording_files(cast)
                    removed += 1
            except FileNotFoundError:
                continue
            except (OSError, ValueError, RuntimeError):
                _logger.warning("Could not expire a saved console recording", exc_info=True)
    return removed


async def run_recording_retention(settings: Settings, recorder: ConsoleRecordingService) -> None:
    while True:
        try:
            removed = await expire_recordings(settings, recorder)
            if removed:
                _logger.info("Expired %d saved console recordings", removed)
        except Exception:
            _logger.exception("Console recording retention sweep failed")
        await asyncio.sleep(_INTERVAL_SECONDS)
