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
import json
import logging
import math
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services import iourc_service


_logger = logging.getLogger("nova-ve.runtime.iol")

_UBRIDGE_BINARY = os.environ.get("NOVA_VE_UBRIDGE_BIN", "ubridge")
_IMAGES_ROOT = Path("/var/lib/nova-ve/images/iol")
_SNAPSHOT_NAME = "iol-snapshot.tar"
_SOURCE_NAME = ".iol-source.json"
_SAVED_WORK_PREFIX = ".saved-"


class IolError(RuntimeError):
    """Raised on IOL launch, bridge, or console failures."""


class IolStartError(IolError):
    def __init__(self, record: IolRecord, message: str) -> None:
        super().__init__(message)
        self.record = record


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
class IolSource:
    workspace: str = "."
    application_id: int | None = None
    pid: int | None = None
    pid_create_time: float | None = None


@dataclass
class _SavedSnapshot:
    application_id: int


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
    pid: int | None
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
    ubridge_create_time: float | None = None
    owned_work: bool = False

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
            "ubridge_create_time": self.ubridge_create_time,
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
        active_ids = active_application_ids or set()
        extras = node.get("extras") if isinstance(node.get("extras"), dict) else {}
        saved_snapshot = (
            cls._read_saved_snapshot(work_dir, active_ids)
            if extras.get("config") == "Saved"
            else None
        )
        app_id = (
            saved_snapshot.application_id
            if saved_snapshot is not None
            else cls.application_id_for_start(lab_id, node_id, node, work_dir, active_ids)
        )
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

        command = cls.build_command(image_path, node, app_id)
        owned_work = saved_snapshot is not None
        ubridge_popen_attempted = False
        try:
            if saved_snapshot is not None:
                work_dir = cls._materialize_saved_work(work_dir, saved_snapshot)
            else:
                work_dir.mkdir(parents=True, exist_ok=True)
            bridge_id = app_id + 512
            cls.write_netmap(work_dir / "NETMAP", app_id, bridge_id)

            ubridge_port = cls._free_tcp_port()
            ubridge_popen_attempted = True
            ubridge_proc = subprocess.Popen(
                [ubridge_binary, "-H", f"127.0.0.1:{ubridge_port}"],
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            if owned_work and not ubridge_popen_attempted:
                cls._remove_saved_work(work_dir)
            raise
        with cls._lock:
            cls._processes[ubridge_proc.pid] = ubridge_proc
        client = None
        tap_names: list[str] = []
        ubridge_bridges: list[str] = []
        udp_ports: list[int] = []
        iol_bridge_name = cls.iol_bridge_name(lab_id, node_id, app_id)
        stdout_log = work_dir / "stdout.log"
        stderr_log = work_dir / "stderr.log"
        iourc_path: Path | None = None

        process: subprocess.Popen[bytes] | None = None
        bridge: IolConsoleBridge | None = None
        pid_create_time = None
        ubridge_create_time = None
        try:
            client = _UbridgeClient(ubridge_port)
            stdout_log.unlink(missing_ok=True)
            stderr_log.unlink(missing_ok=True)
            iourc_path = cls.resolve_iourc(image_path)
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
            try:
                ubridge_create_time = __import__("psutil").Process(ubridge_proc.pid).create_time()
            except Exception:
                ubridge_create_time = None
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
                ubridge_create_time=ubridge_create_time,
                owned_work=owned_work,
            )
        except Exception as exc:
            if process is not None:
                try:
                    pid_create_time = __import__("psutil").Process(process.pid).create_time()
                except Exception:
                    pid_create_time = None
            try:
                ubridge_create_time = __import__("psutil").Process(ubridge_proc.pid).create_time()
            except Exception:
                ubridge_create_time = None
            partial_record = IolRecord(
                lab_id=lab_id,
                node_id=node_id,
                application_id=app_id,
                bridge_id=bridge_id,
                iol_bridge_name=iol_bridge_name,
                image_path=image_path,
                work_dir=work_dir,
                console_port=console_port,
                pid=process.pid if process is not None else None,
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
                ubridge_create_time=ubridge_create_time,
                owned_work=owned_work,
            )
            try:
                if bridge is not None:
                    bridge.stop()
                if process is not None and process.poll() is None:
                    cls._terminate_pid(process.pid)
                cls.stop_ubridge(ubridge_proc.pid)
                if owned_work and cls._popen_exited(process) and cls._popen_exited(ubridge_proc):
                    cls._remove_saved_work(work_dir)
            finally:
                raise IolStartError(partial_record, str(exc)) from exc
        finally:
            if client is not None:
                client.close()

    @classmethod
    def owns_live_process(cls, pid: int) -> bool:
        with cls._lock:
            process = cls._processes.get(pid)
            return process is not None and process.poll() is None

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
    def _popen_exited(process: subprocess.Popen[bytes] | None) -> bool:
        try:
            return process is None or process.poll() is not None
        except Exception:
            return False

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
        try:
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
        except (TypeError, ValueError, OverflowError) as exc:
            raise IolError(
                "Invalid IOL numeric settings: ethernet, serial_adapters/serial, "
                "nvram, and ram must be integers."
            ) from exc

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
    def resolve_iourc(image_path: Path, system_root: Path | None = None) -> Path | None:
        candidate = image_path.parent / "iourc"
        if candidate.is_file():
            return candidate
        return iourc_service.find_iourc_file(system_root)

    @staticmethod
    def build_environment(iourc_path: Path | None) -> dict[str, str] | None:
        if iourc_path is None:
            return None
        env = os.environ.copy()
        env["IOURC"] = str(iourc_path)
        return env

    @classmethod
    def capture_snapshot(cls, work_dir: Path, source: IolSource | None = None) -> None:
        source = source or IolSource()
        native = cls._open_native_file(cls.source_directory(work_dir, source), nonempty=True)
        if native is None:
            raise IolError("no canonical native NVRAM file is available to capture")
        native_file, native_path, application_id = native
        if source.application_id is not None and source.application_id != application_id:
            native_file.close()
            raise IolError("the native NVRAM application ID does not match the last launch")

        snapshot_path = work_dir / _SNAPSHOT_NAME
        tmp_path: Path | None = None
        try:
            with native_file:
                descriptor, tmp_name = tempfile.mkstemp(
                    prefix=".iol-snapshot-", suffix=".tmp", dir=work_dir
                )
                tmp_path = Path(tmp_name)
                with os.fdopen(descriptor, "wb") as handle:
                    with tarfile.open(fileobj=handle, mode="w") as archive:
                        member = tarfile.TarInfo(native_path.name)
                        member.size = os.fstat(native_file.fileno()).st_size
                        member.mode = 0o600
                        member.mtime = 0
                        archive.addfile(member, native_file)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, snapshot_path)
        except (OSError, tarfile.TarError) as exc:
            raise IolError("could not publish the IOL snapshot; the previous snapshot was retained") from exc
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _invalid_saved_snapshot() -> IolError:
        return IolError(
            "Saved mode requires a valid explicitly captured IOL snapshot. "
            "Capture a snapshot while the node is stopped, then retry."
        )

    @classmethod
    def _read_saved_snapshot(cls, work_dir: Path, active_ids: set[int]) -> _SavedSnapshot:
        path = work_dir / _SNAPSHOT_NAME
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                raise ValueError("invalid snapshot file")
            with path.open("rb") as handle, tarfile.open(fileobj=handle, mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isreg() or members[0].size <= 0:
                    raise ValueError("invalid snapshot member")
                member = members[0]
                application_id = cls.native_application_id(member.name)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("missing snapshot payload")
                payload_size = 0
                while chunk := extracted.read(1024 * 1024):
                    payload_size += len(chunk)
            if payload_size != member.size:
                raise ValueError("invalid snapshot payload")
        except (OSError, ValueError, tarfile.TarError) as exc:
            raise cls._invalid_saved_snapshot() from exc
        if application_id in active_ids:
            raise IolError(
                f"saved snapshot requires IOL application ID {application_id}, which is in use. "
                "Retry when that application ID is available"
            )
        return _SavedSnapshot(application_id=application_id)

    @classmethod
    def _materialize_saved_work(cls, work_dir: Path, snapshot: _SavedSnapshot) -> Path:
        saved_work = Path(tempfile.mkdtemp(prefix=_SAVED_WORK_PREFIX, dir=work_dir))
        try:
            native_path = saved_work / f"nvram_{snapshot.application_id:05d}"
            with (work_dir / _SNAPSHOT_NAME).open("rb") as source_handle:
                with tarfile.open(fileobj=source_handle, mode="r:") as archive:
                    members = archive.getmembers()
                    if len(members) != 1 or members[0].name != native_path.name:
                        raise cls._invalid_saved_snapshot()
                    extracted = archive.extractfile(members[0])
                    if extracted is None:
                        raise cls._invalid_saved_snapshot()
                    with native_path.open("xb") as handle:
                        shutil.copyfileobj(extracted, handle)
                        if handle.tell() != members[0].size:
                            raise cls._invalid_saved_snapshot()
            return saved_work
        except Exception:
            cls._remove_saved_work(saved_work)
            raise

    @staticmethod
    def _remove_saved_work(path: Path) -> None:
        try:
            if (
                path.name.startswith(_SAVED_WORK_PREFIX)
                and stat.S_ISDIR(path.lstat().st_mode)
                and not path.is_symlink()
            ):
                shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _logger.warning("could not remove superseded IOL Saved work %r: %s", path.name, exc)

    @classmethod
    def discard_owned_work(cls, record: IolRecord) -> None:
        if record.owned_work:
            cls._remove_saved_work(record.work_dir)

    @classmethod
    def cleanup_superseded_work(
        cls, work_dir: Path, previous: IolSource | None, current_work_dir: Path
    ) -> None:
        if previous is None or previous.workspace == ".":
            return
        previous_path = work_dir / previous.workspace
        if previous_path != current_work_dir and previous_path.parent == work_dir:
            cls._remove_saved_work(previous_path)

    @classmethod
    def read_source(cls, work_dir: Path) -> IolSource:
        path = work_dir / _SOURCE_NAME
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return IolSource()
        except OSError as exc:
            raise IolError("could not read the last IOL launch source") from exc
        try:
            if not stat.S_ISREG(mode):
                raise ValueError("invalid source marker")
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls._validated_source(data)
        except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise IolError("could not read the last IOL launch source") from exc

    @classmethod
    def _validated_source(cls, data: dict[str, Any]) -> IolSource:
        if not isinstance(data, dict):
            raise ValueError("invalid source marker")
        workspace = data.get("workspace")
        application_id = data.get("application_id")
        pid = data.get("pid")
        pid_create_time = data.get("pid_create_time")
        if (
            not isinstance(workspace, str)
            or not cls._valid_workspace(workspace)
            or type(application_id) is not int
            or not 1 <= application_id <= 512
            or type(pid) is not int
            or pid <= 0
            or pid_create_time is not None
            and (
                type(pid_create_time) not in (int, float)
                or not 0 < pid_create_time <= sys.float_info.max
            )
        ):
            raise ValueError("invalid source marker")
        return IolSource(
            workspace=workspace,
            application_id=application_id,
            pid=pid,
            pid_create_time=pid_create_time,
        )

    @staticmethod
    def _valid_workspace(workspace: str) -> bool:
        return workspace == "." or (
            Path(workspace).name == workspace and workspace.startswith(_SAVED_WORK_PREFIX)
        )

    @classmethod
    def source_directory(cls, work_dir: Path, source: IolSource) -> Path:
        if source.workspace == ".":
            return work_dir
        source_dir = work_dir / source.workspace
        try:
            if (
                not cls._valid_workspace(source.workspace)
                or source_dir.parent != work_dir
                or not stat.S_ISDIR(source_dir.lstat().st_mode)
            ):
                raise IolError("managed IOL source must be a real contained directory")
        except OSError as exc:
            raise IolError("could not inspect the managed IOL source directory") from exc
        return source_dir

    @classmethod
    def runtime_source(cls, work_dir: Path, runtime: dict[str, Any]) -> IolSource:
        try:
            runtime_work = Path(runtime.get("work_dir"))
            workspace = runtime_work.relative_to(work_dir).as_posix()
            source = cls._validated_source(
                {
                    "workspace": workspace,
                    "application_id": runtime.get("application_id"),
                    "pid": runtime.get("pid"),
                    "pid_create_time": runtime.get("pid_create_time"),
                }
            )
            cls.source_directory(work_dir, source)
            return source
        except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise IolError("could not reconcile the IOL runtime source") from exc

    @classmethod
    def publish_source(cls, work_dir: Path, record: IolRecord) -> IolSource | None:
        source = cls.runtime_source(work_dir, record.as_runtime())
        return cls._publish_source(work_dir, source)

    @classmethod
    def publish_runtime_source(
        cls, work_dir: Path, runtime: dict[str, Any]
    ) -> tuple[IolSource, IolSource | None]:
        source = cls.runtime_source(work_dir, runtime)
        return source, cls._publish_source(work_dir, source)

    @classmethod
    def _publish_source(cls, work_dir: Path, source: IolSource) -> IolSource | None:
        try:
            previous = cls.read_source(work_dir)
        except IolError:
            previous = None
        cls._atomic_private_write(
            work_dir / _SOURCE_NAME,
            json.dumps(source.__dict__, separators=(",", ":")).encode("utf-8"),
        )
        return previous

    @staticmethod
    def _atomic_private_write(path: Path, payload: bytes) -> None:
        tmp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError as exc:
            raise IolError(f"could not publish {path.name}") from exc
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def native_application_id(name: str) -> int:
        if (
            len(name) != 11
            or not name.startswith("nvram_")
            or not name[6:].isascii()
            or not name[6:].isdecimal()
        ):
            raise ValueError("invalid native NVRAM name")
        application_id = int(name[6:])
        if not 1 <= application_id <= 512:
            raise ValueError("unsupported native NVRAM application ID")
        return application_id

    @classmethod
    def _open_native_file(cls, work_dir: Path, *, nonempty: bool = False):
        directory_fd = None
        native_file = None
        try:
            directory_fd = os.open(
                work_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise IolError(
                "could not inspect native NVRAM. Restore access to the node work "
                "directory and native file, then retry"
            ) from exc
        try:
            native_entries = sorted(
                name
                for name in os.listdir(directory_fd)
                if name.startswith("nvram_")
                and len(name) == 11
                and name[6:].isascii()
                and name[6:].isdecimal()
            )
            if not native_entries:
                return None
            if len(native_entries) != 1:
                raise IolError(
                    "multiple native NVRAM files make the application ID ambiguous. Keep exactly "
                    "one canonical native file and retry"
                )
            native_path = work_dir / native_entries[0]
            native_id = int(native_path.name[6:])
            if not 1 <= native_id <= 512:
                raise IolError(
                    "native NVRAM filename must contain a supported IOL application ID from 1 "
                    "to 512. Use a supported native file and retry"
                )
            if not stat.S_ISREG(native_path.lstat().st_mode):
                raise IolError(
                    "native NVRAM must be a regular, non-symlink file. Replace it with "
                    "a regular file and retry"
                )
            native_fd = os.open(
                native_path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            native_file = os.fdopen(native_fd, "rb")
            if not stat.S_ISREG(os.fstat(native_file.fileno()).st_mode):
                raise IolError("native NVRAM must be a regular, non-symlink file")
            if nonempty and not native_file.read(1):
                raise IolError("native NVRAM must be nonempty before it can be captured")
            native_file.seek(0)
            result = (native_file, native_path, native_id)
            native_file = None
            return result
        except OSError as exc:
            raise IolError(
                "could not inspect native NVRAM. Restore access to the node work "
                "directory and native file, then retry"
            ) from exc
        finally:
            if native_file is not None:
                native_file.close()
            if directory_fd is not None:
                os.close(directory_fd)

    @classmethod
    def _canonical_native_file(
        cls, work_dir: Path, *, nonempty: bool = False
    ) -> tuple[Path, int] | None:
        native = cls._open_native_file(work_dir, nonempty=nonempty)
        if native is None:
            return None
        native_file, native_path, native_id = native
        native_file.close()
        return native_path, native_id

    @classmethod
    def application_id_for_start(
        cls,
        lab_id: str,
        node_id: int,
        node: dict[str, Any],
        work_dir: Path,
        active_ids: set[int],
    ) -> int:
        extras = node.get("extras") if isinstance(node.get("extras"), dict) else {}
        if extras.get("config") == "Saved":
            return cls._read_saved_snapshot(work_dir, active_ids).application_id
        if extras.get("config") == "Exported":
            return cls.application_id(lab_id, node_id, active_ids)
        native = cls._canonical_native_file(work_dir)
        if native is None:
            return cls.application_id(lab_id, node_id, active_ids)
        _, native_id = native
        if native_id in active_ids:
            raise IolError(
                f"native NVRAM requires IOL application ID {native_id}, which is in use. "
                "Retry when that application ID is available"
            )
        return native_id

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
