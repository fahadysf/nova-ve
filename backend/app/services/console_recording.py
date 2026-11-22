# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Console session recording (#291).

In-built (HTML5/Guacamole) telnet console sessions are relayed through a
per-session proxy: a telnet server toward guacd and a telnet client toward
the node's real console port. Recording is ON DEMAND — the proxy relays
without writing anything until the user presses record, and each
start/stop cycle produces one asciinema cast v2 file plus a raw output log
under ``RECORDINGS_DIR/<lab-id>/<node-id>/``.

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
import json
import logging
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import telnetlib3

from app.config import get_settings

_logger = logging.getLogger(__name__)

CAST_WIDTH = 120
CAST_HEIGHT = 30
PENDING_ATTACH_TIMEOUT = 120.0


def _slug(text: object) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "lab"


class RecordingSession:
    """One guacd<->node telnet relay with on-demand cast/log recording."""

    def __init__(
        self,
        service: "ConsoleRecordingService",
        *,
        lab_id: str,
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
        self._cycle += 1
        started = datetime.now(UTC)
        base = f"{started.strftime('%Y%m%d-%H%M%S')}-{self.session_id}-{self._cycle}"
        directory = Path(get_settings().RECORDINGS_DIR) / _slug(self.lab_id) / str(self.node_id)
        directory.mkdir(parents=True, exist_ok=True)
        self._cast_path = directory / f"{base}.cast"
        self._cycle_started = started
        self._t0 = time.monotonic()
        self._cast_file = self._cast_path.open("w", encoding="utf-8")
        self._cast_file.write(
            json.dumps(
                {
                    "version": 2,
                    "width": CAST_WIDTH,
                    "height": CAST_HEIGHT,
                    "timestamp": int(started.timestamp()),
                    "title": f"{self.lab_name} / {self.node_name}",
                    "env": {"TERM": "xterm"},
                }
            )
            + "\n"
        )
        self._cast_file.flush()
        self._log_file = self._cast_path.with_suffix(".log").open("w", encoding="utf-8")
        self._log_file.write(f"# Recording started {started.isoformat()} by {self.username}\n")
        self._log_file.flush()
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
        if self._log_file is not None:
            self._log_file.write(f"# Recording stopped {stopped.isoformat()} (duration {elapsed:.1f}s)\n")
            self._log_file.close()
            self._log_file = None
        if self._cast_file is not None:
            self._cast_file.close()
            self._cast_file = None
            self._finalize_cast_header(elapsed)
        _logger.info("recording %s cycle %d stopped (%.1fs)", self.session_id, self._cycle, elapsed)
        return self.status()

    def _finalize_cast_header(self, elapsed: float) -> None:
        """Stamp ``duration`` into the cast header once stop is pressed."""
        if self._cast_path is None:
            return
        tmp_path = self._cast_path.with_suffix(".cast.tmp")
        try:
            with self._cast_path.open("r", encoding="utf-8") as src, tmp_path.open("w", encoding="utf-8") as dst:
                header = json.loads(src.readline())
                header["duration"] = round(elapsed, 3)
                dst.write(json.dumps(header, ensure_ascii=False) + "\n")
                shutil.copyfileobj(src, dst)
            tmp_path.replace(self._cast_path)
        except (OSError, ValueError) as exc:
            _logger.warning("could not finalize cast header %s: %s", self._cast_path, exc)
            tmp_path.unlink(missing_ok=True)

    async def close(self) -> None:
        """Stop any active cycle, close the listener and files (idempotent)."""
        if self._recording:
            self.stop_recording()
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
        self._by_key: dict[str, RecordingSession] = {}

    @staticmethod
    def _key(lab_id: str, node_id: int, mode: str, username: str) -> str:
        return f"{lab_id}:{node_id}:{mode}:{username}"

    async def start_session(
        self,
        *,
        lab_id: str,
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
            lab_name=lab_name,
            node_id=node_id,
            node_name=node_name,
            target_host=target_host,
            target_port=target_port,
            username=username,
            mode=mode,
        )
        try:
            await session.start(listen_host=listen_host)
        except Exception as exc:
            _logger.warning("console recording proxy failed to start, using direct console: %s", exc)
            await session.close()
            return None
        self._sessions[session.session_id] = session
        self._by_key[self._key(lab_id, node_id, mode, username)] = session
        _logger.info(
            "console proxy %s: %s/%s port %d -> %s:%d",
            session.session_id, lab_name, node_name, session.listen_port, target_host, target_port,
        )
        return session

    def find(self, lab_id: str, node_id: int, mode: str, username: str) -> RecordingSession | None:
        """Latest open console session for a user/node, if any."""
        session = self._by_key.get(self._key(lab_id, node_id, mode, username))
        return session if session is not None and not session._closed else None

    def is_active(self, cast_path: Path) -> bool:
        return any(s._recording and s._cast_path == cast_path for s in self._sessions.values())

    def _forget(self, session: RecordingSession) -> None:
        self._sessions.pop(session.session_id, None)
        key = self._key(session.lab_id, session.node_id, session.mode, session.username)
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
