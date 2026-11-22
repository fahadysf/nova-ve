# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Console session recording (#291).

Every in-built (HTML5/Guacamole) telnet console session is relayed through a
per-session proxy: a telnet server toward guacd and a telnet client toward
the node's real console port. While relaying, the proxy writes an asciinema
cast v2 file (https://docs.asciinema.org/manual/asciicast/v2/) plus the raw
output log under ``RECORDINGS_DIR/<lab-id>/<node-id>/``.

Cast ``"o"`` events carry the raw terminal byte stream, so any asciinema
player renders exactly what the user saw — no terminal emulation needed
here. ``"i"`` events capture keystrokes.

Recordings capture everything shown and typed, including echoed passwords;
read access is gated by the lab's scoped permissions (see routers/recordings).
"""

import asyncio
import json
import logging
import re
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
    """One guacd<->node telnet relay writing a cast file and a raw log."""

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
        started = datetime.now(UTC)
        base = f"{started.strftime('%Y%m%d-%H%M%S')}-{self.session_id}"
        directory = Path(get_settings().RECORDINGS_DIR) / _slug(lab_id) / str(node_id)
        self.cast_path = directory / f"{base}.cast"
        self.log_path = directory / f"{base}.log"
        self.listen_port = 0
        self._server: asyncio.AbstractServer | None = None
        self._pending_task: asyncio.Task | None = None
        self._attached = False
        self._cast_file = None
        self._log_file = None
        self._t0 = 0.0
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

    async def close(self) -> None:
        """Close the listener and any open files (idempotent)."""
        self._closed = True
        if self._pending_task is not None:
            self._pending_task.cancel()
        if self._server is not None:
            self._server.close()
        for handle in (self._cast_file, self._log_file):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self.service._forget(self)

    async def _expire_if_unattached(self) -> None:
        await asyncio.sleep(PENDING_ATTACH_TIMEOUT)
        if not self._attached:
            _logger.warning("recording %s: no console attached, expiring", self.session_id)
            await self.close()

    async def _shell(self, reader, writer) -> None:
        # One relay per proxy: the node console (QEMU telnet chardev) serves a
        # single client, so a second guacd attachment cannot be multiplexed.
        if self._attached:
            writer.write("\r\nConsole recording session already attached.\r\n")
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
            _logger.warning("recording %s: upstream connect failed: %s", self.session_id, exc)
            writer.write(f"\r\nConsole is not reachable: {exc}\r\n")
            writer.close()
            await self.close()
            return

        self.cast_path.parent.mkdir(parents=True, exist_ok=True)
        self._t0 = time.monotonic()
        self._cast_file = self.cast_path.open("w", encoding="utf-8")
        self._cast_file.write(
            json.dumps(
                {
                    "version": 2,
                    "width": CAST_WIDTH,
                    "height": CAST_HEIGHT,
                    "timestamp": int(time.time()),
                    "title": f"{self.lab_name} / {self.node_name}",
                    "env": {"TERM": "xterm"},
                }
            )
            + "\n"
        )
        self._log_file = self.log_path.open("w", encoding="utf-8")

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
            self._log_file.write(data)
            self._log_file.flush()
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
        if self._cast_file is None:
            return
        dt = round(time.monotonic() - self._t0, 6)
        self._cast_file.write(json.dumps([dt, kind, data], ensure_ascii=False) + "\n")
        self._cast_file.flush()


class ConsoleRecordingService:
    """Registry of active recording proxies; one per HTML5 console session."""

    def __init__(self) -> None:
        self._sessions: dict[str, RecordingSession] = {}

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
        """Start a recording proxy; None means "connect directly, unrecorded"."""
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
        _logger.info(
            "recording %s: %s/%s port %d -> %s:%d",
            session.session_id, lab_name, node_name, session.listen_port, target_host, target_port,
        )
        return session

    def is_active(self, cast_path: Path) -> bool:
        return any(s.cast_path == cast_path and not s._closed for s in self._sessions.values())

    def _forget(self, session: RecordingSession) -> None:
        self._sessions.pop(session.session_id, None)

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()


_service: ConsoleRecordingService | None = None


def get_console_recorder() -> ConsoleRecordingService:
    global _service
    if _service is None:
        _service = ConsoleRecordingService()
    return _service
