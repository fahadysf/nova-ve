# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""IOL/IOU runtime backend.

Cisco IOL/IOU images do not attach directly to TAP devices. They identify a
running instance with an application id and exchange Ethernet frames through
``/tmp/netio<uid>/<application_id>`` according to a per-process ``NETMAP``.
The bridge side is handled by uBridge's ``iol_bridge`` module.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import select
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_logger = logging.getLogger("nova-ve.runtime.iol")

_UBRIDGE_BINARY = os.environ.get("NOVA_VE_UBRIDGE_BIN", "ubridge")
_IMAGES_ROOT = Path("/var/lib/nova-ve/images/iol")


class IolError(RuntimeError):
    """Raised on IOL launch, bridge, or console failures."""


_TELNET_IAC = 255
_TELNET_WILL = 251
_TELNET_DO = 253
_TELNET_SB = 250
_TELNET_SE = 240
_TELNET_ECHO = 1
_TELNET_SUPPRESS_GO_AHEAD = 3
_TELNET_OPTION_COMMANDS = {251, 252, 253, 254}  # WILL, WONT, DO, DONT
_TELNET_SERVER_NEGOTIATION = bytes(
    [
        _TELNET_IAC,
        _TELNET_WILL,
        _TELNET_ECHO,
        _TELNET_IAC,
        _TELNET_WILL,
        _TELNET_SUPPRESS_GO_AHEAD,
    ]
)


@dataclass
class IolRecord:
    lab_id: str
    node_id: int
    application_id: int
    bridge_id: int
    iol_bridge_name: str
    image_path: Path
    work_dir: Path
    console_port: int
    pid: int
    pid_create_time: float | None
    command: list[str]
    stdout_log: Path
    stderr_log: Path
    tap_names: list[str]
    ubridge_pid: int
    ubridge_port: int
    ubridge_bridges: list[str]
    udp_ports: list[int]
    iourc_path: Path | None = None

    def as_runtime(self) -> dict[str, Any]:
        runtime = {
            "kind": "iol",
            "lab_id": self.lab_id,
            "node_id": self.node_id,
            "started_at": time.time(),
            "console": "telnet",
            "console_port": self.console_port,
            "pid": self.pid,
            "pid_create_time": self.pid_create_time,
            "application_id": self.application_id,
            "bridge_id": self.bridge_id,
            "iol_bridge_name": self.iol_bridge_name,
            "image": str(self.image_path),
            "work_dir": str(self.work_dir),
            "stdout_log": str(self.stdout_log),
            "stderr_log": str(self.stderr_log),
            "command": self.command,
            "tap_names": self.tap_names,
            "ubridge_pid": self.ubridge_pid,
            "ubridge_port": self.ubridge_port,
            "ubridge_bridges": self.ubridge_bridges,
            "udp_ports": self.udp_ports,
        }
        if self.iourc_path is not None:
            runtime["iourc_path"] = str(self.iourc_path)
        return runtime


@dataclass
class _Reply:
    code: int
    lines: list[str]

    @property
    def ok(self) -> bool:
        return 100 <= self.code < 200


class _UbridgeClient:
    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._file = None

    def connect(self) -> None:
        if self._sock is not None:
            return
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._sock = socket.create_connection((self._host, self._port), timeout=1.0)
                self._file = self._sock.makefile("rwb", buffering=0)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        raise IolError(f"could not connect to uBridge hypervisor on {self._host}:{self._port}: {last_error}")

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        finally:
            self._file = None
            if self._sock is not None:
                self._sock.close()
                self._sock = None

    def request(self, command: str) -> _Reply:
        self.connect()
        assert self._file is not None
        self._file.write(command.encode("utf-8") + b"\n")

        lines: list[str] = []
        while True:
            raw = self._file.readline()
            if not raw:
                raise IolError("uBridge hypervisor closed the connection")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if len(line) < 4 or not line[:3].isdigit():
                raise IolError(f"malformed uBridge reply: {line!r}")
            code = int(line[:3])
            sep = line[3]
            payload = line[4:]
            if 200 <= code < 300 and sep == "-":
                raise IolError(f"uBridge refused {command!r}: {code} {payload}")
            if code == 100 and sep == "-":
                if payload and payload != "OK":
                    lines.append(payload)
                break
            if 100 <= code < 200 and sep == " ":
                lines.append(payload)
                continue
            if sep not in (" ", "-"):
                raise IolError(f"unknown uBridge reply separator {sep!r} in {line!r}")

        return _Reply(code=100, lines=lines)


class _TelnetInputFilter:
    """Strip telnet command bytes before forwarding client input to IOS."""

    def __init__(self) -> None:
        self._state = "data"

    def feed(self, data: bytes) -> bytes:
        output = bytearray()
        for byte in data:
            if self._state == "data":
                if byte == _TELNET_IAC:
                    self._state = "iac"
                else:
                    output.append(byte)
                continue

            if self._state == "iac":
                if byte == _TELNET_IAC:
                    output.append(_TELNET_IAC)
                    self._state = "data"
                elif byte in _TELNET_OPTION_COMMANDS:
                    self._state = "option"
                elif byte == _TELNET_SB:
                    self._state = "subnegotiation"
                else:
                    self._state = "data"
                continue

            if self._state == "option":
                self._state = "data"
                continue

            if self._state == "subnegotiation":
                if byte == _TELNET_IAC:
                    self._state = "subnegotiation_iac"
                continue

            if self._state == "subnegotiation_iac":
                self._state = "data" if byte == _TELNET_SE else "subnegotiation"

        return bytes(output)


class IolConsoleBridge:
    """Tiny TCP-to-PTY bridge for IOL stdio consoles."""

    def __init__(
        self,
        *,
        process: subprocess.Popen[bytes],
        master_fd: int,
        listen_port: int,
        log_path: Path,
    ) -> None:
        self.process = process
        self.master_fd = master_fd
        self.listen_port = listen_port
        self.log_path = log_path
        self._server: socket.socket | None = None
        self._clients: dict[socket.socket, _TelnetInputFilter] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"iol-console-{process.pid}",
            daemon=True,
        )

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", self.listen_port))
        server.listen(4)
        server.setblocking(False)
        self._server = server
        os.set_blocking(self.master_fd, False)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        for client in list(self._clients):
            try:
                client.close()
            except OSError:
                pass
        self._clients.clear()
        try:
            os.close(self.master_fd)
        except OSError:
            pass

    def _drop_client(self, client: socket.socket) -> None:
        self._clients.pop(client, None)
        try:
            client.close()
        except OSError:
            pass

    def _run(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab", buffering=0) as log_handle:
            while not self._stop.is_set() and self.process.poll() is None:
                readables: list[Any] = [self.master_fd]
                if self._server is not None:
                    readables.append(self._server)
                readables.extend(self._clients)
                try:
                    ready, _, _ = select.select(readables, [], [], 0.2)
                except (OSError, ValueError):
                    break
                for item in ready:
                    if item is self._server and self._server is not None:
                        try:
                            client, _ = self._server.accept()
                            client.setblocking(False)
                            client.sendall(_TELNET_SERVER_NEGOTIATION)
                            self._clients[client] = _TelnetInputFilter()
                        except OSError:
                            pass
                        continue
                    if item == self.master_fd:
                        try:
                            data = os.read(self.master_fd, 4096)
                        except BlockingIOError:
                            continue
                        except OSError:
                            return
                        if not data:
                            return
                        log_handle.write(data)
                        for client in list(self._clients):
                            try:
                                client.sendall(data)
                            except OSError:
                                self._drop_client(client)
                        continue
                    try:
                        data = item.recv(4096)
                    except OSError:
                        self._drop_client(item)
                        continue
                    if not data:
                        self._drop_client(item)
                        continue
                    data = self._clients[item].feed(data)
                    if not data:
                        continue
                    try:
                        os.write(self.master_fd, data)
                    except OSError:
                        self._drop_client(item)


class IolLauncher:
    """Launch one IOL process plus its per-node uBridge hypervisor."""

    _bridges: dict[int, IolConsoleBridge] = {}
    _processes: dict[int, subprocess.Popen[bytes]] = {}
    _lock = threading.Lock()

    @classmethod
    def start_node(
        cls,
        *,
        lab_id: str,
        node_id: int,
        node: dict[str, Any],
        images_root: Path,
        work_dir: Path,
        console_port: int,
        attachments: list[dict[str, Any]],
        tap_factory: Any,
        active_application_ids: set[int] | None = None,
    ) -> IolRecord:
        ubridge_binary = shutil.which(_UBRIDGE_BINARY) if not Path(_UBRIDGE_BINARY).exists() else _UBRIDGE_BINARY
        if not ubridge_binary:
            raise IolError(
                "ubridge not found on PATH — install ubridge or set NOVA_VE_UBRIDGE_BIN "
                "before starting IOL/IOU nodes"
            )

        image_path = cls.resolve_image(node, images_root)
        try:
            image_path.chmod(image_path.stat().st_mode | 0o111)
        except OSError as exc:
            raise IolError(f"could not make IOL image executable: {image_path}: {exc}") from exc

        work_dir.mkdir(parents=True, exist_ok=True)
        app_id = cls.application_id(lab_id, node_id, active_application_ids or set())
        bridge_id = app_id + 512
        cls.write_netmap(work_dir / "NETMAP", app_id, bridge_id)

        ubridge_port = cls._free_tcp_port()
        ubridge_proc = subprocess.Popen(
            [ubridge_binary, "-H", f"127.0.0.1:{ubridge_port}"],
            cwd=work_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with cls._lock:
            cls._processes[ubridge_proc.pid] = ubridge_proc
        client = _UbridgeClient(ubridge_port)
        tap_names: list[str] = []
        ubridge_bridges: list[str] = []
        udp_ports: list[int] = []
        iol_bridge_name = cls.iol_bridge_name(lab_id, node_id, app_id)
        stdout_log = work_dir / "stdout.log"
        stderr_log = work_dir / "stderr.log"
        stdout_log.unlink(missing_ok=True)
        stderr_log.unlink(missing_ok=True)
        iourc_path = cls.resolve_iourc(image_path)

        process: subprocess.Popen[bytes] | None = None
        bridge: IolConsoleBridge | None = None
        try:
            client.request(f"iol_bridge create {iol_bridge_name} {bridge_id}")
            ubridge_bridges.append(iol_bridge_name)
            for attachment in attachments:
                interface_index = int(attachment["interface_index"])
                bay = interface_index // 4
                unit = interface_index % 4
                tap = tap_factory(interface_index, attachment["bridge_name"], attachment.get("driver"))
                tap_names.append(tap)

                tap_bridge = cls.tap_bridge_name(lab_id, node_id, interface_index, app_id)
                tap_udp = cls._free_udp_port()
                iol_udp = cls._free_udp_port({tap_udp})
                udp_ports.extend([tap_udp, iol_udp])

                client.request(f"bridge create {tap_bridge}")
                ubridge_bridges.append(tap_bridge)
                client.request(f"bridge add_nio_udp {tap_bridge} {tap_udp} 127.0.0.1 {iol_udp}")
                client.request(f"bridge add_nio_tap {tap_bridge} {tap}")
                client.request(f"bridge start {tap_bridge}")
                client.request(
                    f"iol_bridge add_nio_udp {iol_bridge_name} {app_id} {bay} {unit} "
                    f"{iol_udp} 127.0.0.1 {tap_udp}"
                )
            client.request(f"iol_bridge start {iol_bridge_name}")

            command = cls.build_command(image_path, node, app_id)
            master_fd, slave_fd = os.openpty()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=work_dir,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env=cls.build_environment(iourc_path),
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise IolError(cls.exec_not_found_message(image_path, exc)) from exc
            finally:
                os.close(slave_fd)
            with cls._lock:
                cls._processes[process.pid] = process

            bridge = IolConsoleBridge(
                process=process,
                master_fd=master_fd,
                listen_port=console_port,
                log_path=stdout_log,
            )
            bridge.start()
            time.sleep(0.1)
            if process.poll() is not None:
                raise IolError(cls._tail_text(stdout_log, 40) or "IOL exited immediately after start")

            try:
                pid_create_time = __import__("psutil").Process(process.pid).create_time()
            except Exception:
                pid_create_time = None
            with cls._lock:
                cls._bridges[process.pid] = bridge

            return IolRecord(
                lab_id=lab_id,
                node_id=node_id,
                application_id=app_id,
                bridge_id=bridge_id,
                iol_bridge_name=iol_bridge_name,
                image_path=image_path,
                work_dir=work_dir,
                console_port=console_port,
                pid=process.pid,
                pid_create_time=pid_create_time,
                command=command,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                tap_names=tap_names,
                ubridge_pid=ubridge_proc.pid,
                ubridge_port=ubridge_port,
                ubridge_bridges=ubridge_bridges,
                udp_ports=udp_ports,
                iourc_path=iourc_path,
            )
        except Exception:
            if bridge is not None:
                bridge.stop()
            if process is not None and process.poll() is None:
                cls._terminate_pid(process.pid)
            cls.stop_ubridge(ubridge_proc.pid)
            raise
        finally:
            client.close()

    @classmethod
    def stop_runtime(cls, runtime: dict[str, Any]) -> None:
        pid = runtime.get("pid")
        if pid:
            with cls._lock:
                bridge = cls._bridges.pop(int(pid), None)
            if bridge is not None:
                bridge.stop()
            cls._terminate_pid(int(pid))
        ubridge_pid = runtime.get("ubridge_pid")
        if ubridge_pid:
            cls.stop_ubridge(int(ubridge_pid))

    @staticmethod
    def stop_ubridge(pid: int) -> None:
        IolLauncher._terminate_pid(pid)

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        with IolLauncher._lock:
            process = IolLauncher._processes.pop(pid, None)
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            if process is not None:
                try:
                    process.wait(timeout=0)
                except Exception:
                    pass
            return
        except PermissionError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        if process is not None:
            try:
                process.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @staticmethod
    def build_command(image_path: Path, node: dict[str, Any], application_id: int) -> list[str]:
        extras = node.get("extras") if isinstance(node.get("extras"), dict) else {}
        ethernet_count = max(0, int(node.get("ethernet", 4)))
        ethernet_adapters = max(1, math.ceil(ethernet_count / 4))
        serial_raw = extras.get("serial_adapters")
        if serial_raw is None:
            serial_raw = extras.get("serial", 2)
        nvram_raw = extras.get("nvram")
        if nvram_raw is None:
            nvram_raw = node.get("nvram", 256)
        ram_raw = node.get("ram")
        if ram_raw is None:
            ram_raw = extras.get("ram", 1024)
        serial_adapters = int(serial_raw)
        nvram = int(nvram_raw)
        ram = int(ram_raw)

        command = [str(image_path)]
        if ethernet_adapters != 2:
            command += ["-e", str(ethernet_adapters)]
        if serial_adapters != 2:
            command += ["-s", str(serial_adapters)]
        command += ["-n", str(nvram), "-m", str(ram)]
        if extras.get("l1_keepalives"):
            command.append("-l")
        command.append(str(application_id))
        return command

    @staticmethod
    def exec_not_found_message(image_path: Path, exc: FileNotFoundError) -> str:
        if image_path.exists():
            return (
                f"could not execute IOL image {image_path}: {exc}. "
                "The file exists, so the host is likely missing the ELF loader "
                "or shared libraries required by this image. Most IOL images are "
                "32-bit i386 binaries; re-run the nova-ve installer to install "
                "the 32-bit runtime packages."
            )
        return f"IOL image executable not found: {image_path}"

    @staticmethod
    def resolve_image(node: dict[str, Any], images_root: Path | None = None) -> Path:
        images_root = images_root or _IMAGES_ROOT
        image = str(node.get("image") or "").strip()
        if not image:
            raise IolError("IOL node has no image selected")
        candidate = Path(image)
        if candidate.is_file():
            return candidate
        image_dir = images_root / image
        if image_dir.is_dir():
            exact = image_dir / image
            if exact.is_file():
                return exact
            exact_bin = image_dir / f"{image}.bin"
            if exact_bin.is_file():
                return exact_bin
            bins = sorted(image_dir.glob("*.bin"))
            if bins:
                return bins[0]
            files = sorted(p for p in image_dir.iterdir() if p.is_file() and p.name != "iourc")
            if files:
                return files[0]
        nested = images_root / Path(image).stem / Path(image).name
        if nested.is_file():
            return nested
        raise IolError(f"IOL image not found under {images_root}: {image}")

    @staticmethod
    def resolve_iourc(image_path: Path) -> Path | None:
        candidate = image_path.parent / "iourc"
        if candidate.is_file():
            return candidate
        return None

    @staticmethod
    def build_environment(iourc_path: Path | None) -> dict[str, str] | None:
        if iourc_path is None:
            return None
        env = os.environ.copy()
        env["IOURC"] = str(iourc_path)
        return env

    @staticmethod
    def application_id(lab_id: str, node_id: int, active_ids: set[int]) -> int:
        digest = hashlib.sha256(f"{lab_id}:{node_id}".encode("utf-8")).digest()
        start = int.from_bytes(digest[:2], "big") % 512 + 1
        for offset in range(512):
            candidate = ((start + offset - 1) % 512) + 1
            if candidate not in active_ids:
                return candidate
        raise IolError("no free IOL application IDs available on this host")

    @staticmethod
    def iol_bridge_name(lab_id: str, node_id: int, application_id: int) -> str:
        digest = hashlib.sha1(lab_id.encode("utf-8")).hexdigest()[:8]
        return f"nve-iol-{digest}-{node_id}-{application_id}"

    @staticmethod
    def tap_bridge_name(lab_id: str, node_id: int, interface_index: int, application_id: int) -> str:
        digest = hashlib.sha1(lab_id.encode("utf-8")).hexdigest()[:8]
        return f"nve-iolp-{digest}-{node_id}-{interface_index}-{application_id}"

    @staticmethod
    def write_netmap(path: Path, application_id: int, bridge_id: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for bay in range(16):
                for unit in range(4):
                    handle.write(f"{bridge_id}:{bay}/{unit}{application_id:>5d}:{bay}/{unit}\n")

    @staticmethod
    def _free_tcp_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _free_udp_port(exclude: set[int] | None = None) -> int:
        exclude = exclude or set()
        for _ in range(100):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            if port not in exclude:
                return port
        raise IolError("could not allocate a free UDP port for IOL networking")

    @staticmethod
    def _tail_text(path: Path, tail: int) -> str:
        if not path.exists():
            return ""
        return "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail:])
