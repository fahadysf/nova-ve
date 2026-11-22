# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Console session recording (#291).

In-built (HTML5/Guacamole) telnet console sessions are relayed through a
per-session proxy: a telnet server toward guacd and a telnet client toward
the node's real console port. Recording is ON DEMAND — the proxy relays
without writing anything until the user presses record, and each
start/stop cycle produces one asciinema cast v2 file plus a raw output log
under the lab's adjacent ``.recordings`` directory.

Each recording carries its start and stop timestamps: the cast header
holds ``timestamp`` (start) and ``duration`` (until stop was pressed), and
the raw log gets explicit ``# Recording started/stopped ...`` marker
lines. Cast ``"o"`` events carry the raw terminal byte stream, so any
asciinema player renders exactly what the user saw; ``"i"`` events capture
keystrokes.

Recordings capture everything shown and typed, including echoed passwords;
read access is gated by the lab's scoped permissions (see routers/recordings).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import telnetlib3

from app.config import get_settings

_logger = logging.getLogger(__name__)

CAST_WIDTH = 120
CAST_HEIGHT = 30
PENDING_ATTACH_TIMEOUT = 120.0
RECORDINGS_FOLDER = ".recordings"


def _slug(text: object) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "lab"


def lab_recordings_sidecar(lab_file: Path) -> Path:
    """Storage that moves with a lab JSON without mixing adjacent labs."""
    sidecar = lab_file.parent / RECORDINGS_FOLDER / lab_file.name
    if sidecar.resolve() != sidecar:
        raise ValueError("Invalid lab recordings directory.")
    return sidecar


def lab_recordings_dir(lab_file: Path, lab_id: str) -> Path:
    # The stable ID prevents a new lab reusing the same filename from
    # inheriting the deleted lab's recording history.
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", lab_id):
        identity = lab_id
    else:
        identity = "." + hashlib.sha256(lab_id.encode()).hexdigest()
    directory = lab_recordings_sidecar(lab_file) / identity
    if directory.resolve() != directory:
        raise ValueError("Invalid lab recordings directory.")
    return directory


def recording_lab_id(lab_file: Path, data: dict) -> str:
    return str(data.get("id") or lab_file.name)


def _private_file(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


class RecordingSession:
    """One guacd<->node telnet relay with on-demand cast/log recording."""

    def __init__(
        self,
        service: "ConsoleRecordingService",
        *,
        lab_id: str,
        lab_file: Path,
        lab_name: str,
        node_id: int,
        node_name: str,
        target_host: str,
        target_port: int,
        username: str,
        mode: str,
    ) -> None:
        self.service = service
        self.lab_id = lab_id
        self.lab_file = lab_file
        self.lab_name = lab_name
        self.node_id = node_id
        self.node_name = node_name
        self.target_host = target_host
        self.target_port = target_port
        self.username = username
        self.mode = mode
        self.session_id = uuid.uuid4().hex[:8]
        self.listen_port = 0
        self._server: asyncio.AbstractServer | None = None
        self._pending_task: asyncio.Task | None = None
        self._attached = False
        self._recording = False
        self._cycle = 0
        self._cast_path: Path | None = None
        self._cast_file = None
        self._log_file = None
        self._t0 = 0.0
        self._cycle_started: datetime | None = None
        self._closed = False

    async def start(self, listen_host: str = "0.0.0.0") -> None:
        self._server = await telnetlib3.create_server(
            host=listen_host,
            port=0,
            shell=self._shell,
            term="xterm",
        )
        self.listen_port = self._server.sockets[0].getsockname()[1]
        self._pending_task = asyncio.create_task(self._expire_if_unattached())

    def status(self) -> dict:
        return {
            "console_active": not self._closed,
            "recording": self._recording,
            "recording_id": (
                f"{self.node_id}--{self._cast_path.name}" if self._recording and self._cast_path else None
            ),
            "started_at": self._cycle_started.isoformat() if self._recording and self._cycle_started else None,
        }

    def start_recording(self) -> dict:
        """Begin a new recording cycle (one file pair per start/stop)."""
        if self._closed:
            raise ValueError("Console session has ended.")
        if self._recording:
            return self.status()
        cycle = self._cycle + 1
        started = datetime.now(UTC)
        t0 = time.monotonic()
        node_slug = _slug(self.node_name).strip(".")[:100] or "node"
        base = f"{node_slug}-{started:%Y%m%dT%H%M%SZ}-{self.session_id}-{cycle}"
        directory = lab_recordings_dir(self.lab_file, self.lab_id) / str(self.node_id)
        if directory.resolve() != directory or not self.lab_file.is_file():
            raise ValueError("The lab recording location is unavailable.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        cast_path = directory / f"{base}.cast"
        log_path = directory / f"{base}.log"
        cast_file = log_file = None
        try:
            cast_file = open(cast_path, "x", encoding="utf-8", opener=_private_file)
            log_file = open(log_path, "x", encoding="utf-8", opener=_private_file)
            cast_file.write(
                json.dumps(
                    {
                        "version": 2,
                        "width": CAST_WIDTH,
                        "height": CAST_HEIGHT,
                        "timestamp": int(started.timestamp()),
                        "title": f"{self.lab_name} / {self.node_name}",
                        "env": {"TERM": "xterm"},
                        "nova": {
                            "node_name": self.node_name,
                            "username": self.username,
                            "console": self.mode,
                            "started_at": started.isoformat(),
                        },
                    }
                ) + "\n"
            )
            cast_file.flush()
            log_file.write(f"# Recording started {started.isoformat()} by {self.username}\n")
            log_file.flush()
        except OSError:
            for handle, path in ((cast_file, cast_path), (log_file, log_path)):
                if handle is not None:
                    with suppress(OSError):
                        handle.close()
                    with suppress(OSError):
                        path.unlink(missing_ok=True)
            raise
        self._cycle = cycle
        self._cast_path = cast_path
        self._cycle_started = started
        self._t0 = t0
        self._cast_file = cast_file
        self._log_file = log_file
        self._recording = True
        _logger.info("recording %s cycle %d started by %s", self.session_id, self._cycle, self.username)
        return self.status()

    def stop_recording(self) -> dict:
        """Stop the current cycle and stamp stop time into both files."""
        if not self._recording:
            return self.status()
        self._recording = False
        stopped = datetime.now(UTC)
        elapsed = time.monotonic() - self._t0
        log_file, cast_file = self._log_file, self._cast_file
        self._log_file = self._cast_file = None
        try:
            if log_file is not None:
                with log_file:
                    log_file.write(f"# Recording stopped {stopped.isoformat()} (duration {elapsed:.1f}s)\n")
        finally:
            if cast_file is not None:
                cast_file.close()
                self._finalize_cast_header(elapsed, stopped)
        _logger.info("recording %s cycle %d stopped (%.1fs)", self.session_id, self._cycle, elapsed)
        return self.status()

    def _finalize_cast_header(self, elapsed: float, stopped: datetime) -> None:
        """Stamp ``duration`` into the cast header once stop is pressed."""
        if self._cast_path is None:
            return
        tmp_path = None
        try:
            with (
                self._cast_path.open("r", encoding="utf-8") as src,
                tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self._cast_path.parent, delete=False
                ) as dst,
            ):
                tmp_path = Path(dst.name)
                header = json.loads(src.readline())
                header["duration"] = round(elapsed, 3)
                header["nova"]["stopped_at"] = stopped.isoformat()
                dst.write(json.dumps(header, ensure_ascii=False) + "\n")
                shutil.copyfileobj(src, dst)
            tmp_path.replace(self._cast_path)
        except (OSError, ValueError) as exc:
            _logger.warning("could not finalize cast header %s: %s", self._cast_path, exc)
            if tmp_path is not None:
                with suppress(OSError):
                    tmp_path.unlink(missing_ok=True)

    async def close(self) -> None:
        """Stop any active cycle, close the listener and files (idempotent)."""
        if self._recording:
            try:
                self.stop_recording()
            except OSError:
                _logger.exception("Could not stop console recording %s cleanly.", self.session_id)
        self._closed = True
        if self._pending_task is not None:
            self._pending_task.cancel()
        if self._server is not None:
            self._server.close()
        self.service._forget(self)

    async def _expire_if_unattached(self) -> None:
        await asyncio.sleep(PENDING_ATTACH_TIMEOUT)
        if not self._attached:
            _logger.warning("recording proxy %s: no console attached, expiring", self.session_id)
            await self.close()

    async def _shell(self, reader, writer) -> None:
        # One relay per proxy: the node console (QEMU telnet chardev) serves a
        # single client, so a second guacd attachment cannot be multiplexed.
        if self._attached:
            writer.write("\r\nConsole session already attached.\r\n")
            writer.close()
            return
        self._attached = True
        if self._pending_task is not None:
            self._pending_task.cancel()

        try:
            up_reader, up_writer = await telnetlib3.open_connection(
                host=self.target_host,
                port=self.target_port,
                term="xterm",
            )
        except Exception as exc:
            _logger.warning("proxy %s: upstream connect failed: %s", self.session_id, exc)
            writer.write(f"\r\nConsole is not reachable: {exc}\r\n")
            writer.close()
            await self.close()
            return

        pumps = [
            asyncio.create_task(self._pump_output(up_reader, writer)),
            asyncio.create_task(self._pump_input(reader, up_writer)),
        ]
        try:
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
        finally:
            for w in (up_writer, writer):
                try:
                    w.close()
                except Exception:
                    pass
            await self.close()

    async def _pump_output(self, up_reader, down_writer) -> None:
        while not self._closed:
            data = await up_reader.read(4096)
            if not data:
                return
            self._record("o", data)
            down_writer.write(data)
            await down_writer.drain()

    async def _pump_input(self, down_reader, up_writer) -> None:
        while not self._closed:
            data = await down_reader.read(1024)
            if not data:
                return
            self._record("i", data)
            up_writer.write(data)
            await up_writer.drain()

    def _record(self, kind: str, data: str) -> None:
        if not self._recording or self._cast_file is None:
            return
        dt = round(time.monotonic() - self._t0, 6)
        self._cast_file.write(json.dumps([dt, kind, data], ensure_ascii=False) + "\n")
        self._cast_file.flush()
        if kind == "o" and self._log_file is not None:
            self._log_file.write(data)
            self._log_file.flush()


class ConsoleRecordingService:
    """Registry of active console proxies; recording toggled per session."""

    def __init__(self) -> None:
        self._sessions: dict[str, RecordingSession] = {}
        self._by_key: dict[tuple[Path, str, int, str, str], RecordingSession] = {}

    @staticmethod
    def _key(session: RecordingSession) -> tuple[Path, str, int, str, str]:
        return (session.lab_file, session.lab_id, session.node_id, session.mode, session.username)

    async def start_session(
        self,
        *,
        lab_id: str,
        lab_file: Path,
        lab_name: str,
        node_id: int,
        node_name: str,
        target_host: str,
        target_port: int,
        username: str,
        mode: str = "primary",
        listen_host: str = "0.0.0.0",
    ) -> RecordingSession | None:
        """Start the console relay (recording off until explicitly started)."""
        if not get_settings().RECORDINGS_ENABLED:
            return None
        session = RecordingSession(
            self,
            lab_id=lab_id,
            lab_file=lab_file,
            lab_name=lab_name,
            node_id=node_id,
            node_name=node_name,
            target_host=target_host,
            target_port=target_port,
            username=username,
            mode=mode,
        )
        # Track the relay while the listener binds so a concurrent lab move
        # can update its storage location before it becomes controllable.
        self._sessions[session.session_id] = session
        try:
            await session.start(listen_host=listen_host)
        except asyncio.CancelledError:
            await session.close()
            raise
        except Exception as exc:
            _logger.warning("console recording proxy failed to start, using direct console: %s", exc)
            await session.close()
            return None
        self._by_key[self._key(session)] = session
        _logger.info(
            "console proxy %s: %s/%s port %d -> %s:%d",
            session.session_id, lab_name, node_name, session.listen_port, target_host, target_port,
        )
        return session

    def find(self, lab_file: Path, lab_id: str, node_id: int, mode: str, username: str) -> RecordingSession | None:
        """Latest open console session for a user/node, if any."""
        session = self._by_key.get((lab_file, lab_id, node_id, mode, username))
        return session if session is not None and not session._closed else None

    def is_active(self, cast_path: Path) -> bool:
        return any(s._recording and s._cast_path == cast_path for s in self._sessions.values())

    def relocate_lab(self, source: Path, target: Path) -> None:
        """Update open relays after a lab JSON and its sidecar move."""
        source_sidecar = lab_recordings_sidecar(source)
        target_sidecar = lab_recordings_sidecar(target)
        for session in self._sessions.values():
            if session.lab_file == source:
                key = self._key(session)
                was_current = self._by_key.get(key) is session
                if was_current:
                    self._by_key.pop(key)
                session.lab_file = target
                if was_current:
                    self._by_key[self._key(session)] = session
                if session._cast_path is not None:
                    session._cast_path = target_sidecar / session._cast_path.relative_to(source_sidecar)

    def relocate_folder(self, source: Path, target: Path) -> None:
        """Update open relays after a containing folder is renamed."""
        labs = {session.lab_file for session in self._sessions.values() if session.lab_file.is_relative_to(source)}
        for lab_file in labs:
            self.relocate_lab(lab_file, target / lab_file.relative_to(source))

    def _forget(self, session: RecordingSession) -> None:
        self._sessions.pop(session.session_id, None)
        key = self._key(session)
        if self._by_key.get(key) is session:
            self._by_key.pop(key, None)

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()


_service: ConsoleRecordingService | None = None


def get_console_recorder() -> ConsoleRecordingService:
    global _service
    if _service is None:
        _service = ConsoleRecordingService()
    return _service
