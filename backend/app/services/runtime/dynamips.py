"""Dynamips runtime backend.

Dynamips emulates Cisco router hardware (c3725, c7200, ...) by running an
unpacked IOS ``.bin`` image under a MIPS/PowerPC instruction translator.
nova-ve drives Dynamips through its hypervisor TCP protocol: one long-lived
``dynamips -H 0`` process per host, with per-node VM instances created
through line-oriented requests.

Phase 1 scope (locked in plan):
  - c3725 + c7200 platforms only.
  - Initial-attach interface binding only (no hot-attach; link changes
    require node restart, matching the established iol/vpcs pattern in
    ``link_service.py``).
  - Idle-PC sourced from template ``idlepc`` field or the per-image
    ``IdlePcCache``. **A node will refuse to start with no idle-PC.** This
    is a deliberate fail-fast: an unset idle-PC pegs a CPU core at 100%
    per node, so silently proceeding would be a worse outcome than
    surfacing a clear error.

The per-host singleton model matches how GNS3 and EVE-NG operate Dynamips:
the hypervisor process is shared by all running labs on the host, and each
VM is identified by a name that embeds the lab + node IDs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_logger = logging.getLogger("nova-ve.runtime.dynamips")


_RUNTIME_ROOT = Path("/var/lib/nova-ve/runtime")
_IMAGES_ROOT = Path("/var/lib/nova-ve/images/dynamips")
_IDLE_PC_CACHE_PATH = _RUNTIME_ROOT / "idle_pc_cache.json"
_HYPERVISOR_HOST = "127.0.0.1"
_HYPERVISOR_CONNECT_TIMEOUT_S = 5.0
_HYPERVISOR_READ_TIMEOUT_S = 30.0
_DYNAMIPS_BINARY = os.environ.get("NOVA_VE_DYNAMIPS_BIN", "dynamips")


# Platforms and their hypervisor command-module names. Phase 1 ships only
# the two below; extending to c3745/c2691/c2600/c1700 is a 1-line addition.
_PLATFORM_MODULE = {
    "c3725": "c3600",   # c3725 is part of Dynamips' c3600 module family
    "c7200": "c7200",
}

# Dynamips' c3600 module requires `set_chassis` so it knows which c3600
# variant to emulate (3620 / 3640 / 3660 / 3725 / 3745).
_PLATFORM_CHASSIS = {
    "c3725": "3725",
}

# RAM defaults per platform. User can override via template ``ram`` field.
_PLATFORM_RAM_DEFAULT_MB = {
    "c3725": 256,
    "c7200": 512,
}

# Default port adapter in slot 0 if the template did not specify one. Each
# platform has a distinct slot-0 PA convention.
_PLATFORM_SLOT0_DEFAULT_PA = {
    "c3725": "GT96100-FE",   # built-in 2x FastEthernet on the motherboard
    "c7200": "C7200-IO-FE",  # I/O FastEthernet card in slot 0
}

# c7200 NPE (Network Processing Engine) selector. Templates may override.
_PLATFORM_DEFAULT_NPE = {
    "c7200": "npe-400",
}


class DynamipsError(RuntimeError):
    """Raised on hypervisor or launcher failures."""


# ---------- Hypervisor protocol client ----------------------------------


@dataclass
class _Reply:
    code: int
    lines: list[str]

    @property
    def ok(self) -> bool:
        return 100 <= self.code < 200


class HypervisorClient:
    """Text-line TCP client for the Dynamips hypervisor protocol.

    Each ``request`` call sends one command, reads the multi-line reply
    until a terminal line is received, and returns the parsed reply.
    The client is not thread-safe; the launcher serialises access through
    a single lock.
    """

    def __init__(self, port: int, host: str = _HYPERVISOR_HOST) -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._buf = b""

    def connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.create_connection(
            (self._host, self._port), timeout=_HYPERVISOR_CONNECT_TIMEOUT_S
        )
        sock.settimeout(_HYPERVISOR_READ_TIMEOUT_S)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    def _readline(self) -> bytes:
        assert self._sock is not None
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except ConnectionResetError as exc:
                raise DynamipsError(
                    "hypervisor closed the connection unexpectedly"
                ) from exc
            if not chunk:
                raise DynamipsError("hypervisor closed the connection unexpectedly")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line.rstrip(b"\r")

    def request(self, command: str) -> _Reply:
        """Send one command, read until the terminal reply line.

        Dynamips replies use the format ``CODE-text`` for continuation
        lines and ``CODE text`` (space separator) for the terminal line.
        Codes 100-199 indicate success.
        """
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        self._sock.sendall(command.encode("utf-8") + b"\r\n")

        code = 0
        lines: list[str] = []
        while True:
            raw = self._readline().decode("utf-8", errors="replace")
            if len(raw) < 4 or not raw[:3].isdigit():
                raise DynamipsError(f"malformed hypervisor reply: {raw!r}")
            code = int(raw[:3])
            sep = raw[3]
            payload = raw[4:]
            lines.append(payload)
            if sep == " ":
                break
            if sep != "-":
                raise DynamipsError(f"unknown reply separator {sep!r} in {raw!r}")
        reply = _Reply(code=code, lines=lines)
        if not reply.ok:
            raise DynamipsError(
                f"hypervisor refused command {command!r}: {code} {'; '.join(lines)}"
            )
        return reply


# ---------- Idle-PC cache -----------------------------------------------


class IdlePcCache:
    """JSON-backed cache mapping image SHA-256 → idle-PC string.

    Lives at ``/var/lib/nova-ve/runtime/idle_pc_cache.json``. Concurrency
    is handled by reading the whole file, mutating in memory, and writing
    atomically (write-temp-then-rename).
    """

    def __init__(self, path: Path = _IDLE_PC_CACHE_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    @staticmethod
    def hash_image(image_path: Path) -> str:
        h = hashlib.sha256()
        with image_path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _read(self) -> dict[str, str]:
        try:
            with self._path.open("r") as fp:
                data = json.load(fp)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            _logger.warning("idle_pc_cache.malformed", extra={"path": str(self._path)})
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def _write(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w") as fp:
            json.dump(data, fp, indent=2, sort_keys=True)
        tmp.replace(self._path)

    def get(self, image_sha: str) -> str | None:
        with self._lock:
            return self._read().get(image_sha)

    def set(self, image_sha: str, idle_pc: str) -> None:
        with self._lock:
            data = self._read()
            data[image_sha] = idle_pc
            self._write(data)


# ---------- Launcher ----------------------------------------------------


@dataclass
class _Vm:
    lab_id: str
    node_id: int
    vm_name: str
    vm_id: int
    platform: str
    work_dir: Path
    console_port: int
    tap_names: list[str]


class DynamipsLauncher:
    """Per-host singleton that owns one hypervisor process and dispatches
    create/start/stop calls for every Dynamips node on the host.
    """

    _instance: "DynamipsLauncher | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "DynamipsLauncher":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hypervisor_proc: subprocess.Popen[bytes] | None = None
        self._hypervisor_port: int = 0
        self._client: HypervisorClient | None = None
        self._next_vm_id = 1
        self._idle_pc_cache = IdlePcCache()

    # ------------------------------------------------------------------
    # Hypervisor process management
    # ------------------------------------------------------------------

    def _hypervisor_running(self) -> bool:
        proc = self._hypervisor_proc
        return proc is not None and proc.poll() is None

    def _start_hypervisor(self) -> None:
        if self._hypervisor_running():
            return
        if shutil.which(_DYNAMIPS_BINARY) is None:
            raise DynamipsError(
                f"{_DYNAMIPS_BINARY!r} not found on PATH — "
                "install the dynamips package or set NOVA_VE_DYNAMIPS_BIN"
            )
        # `-H 0` asks Dynamips to bind to an OS-chosen TCP port; the chosen
        # port is printed to stdout as "Hypervisor TCP control server started
        # (port <N>)." Parse it back out.
        proc = subprocess.Popen(
            [_DYNAMIPS_BINARY, "-H", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        port = self._wait_for_hypervisor_port(proc)
        self._hypervisor_proc = proc
        self._hypervisor_port = port
        self._client = HypervisorClient(port)
        self._client.connect()
        _logger.info(
            "dynamips.hypervisor.started",
            extra={"pid": proc.pid, "port": port},
        )

    @staticmethod
    def _wait_for_hypervisor_port(proc: "subprocess.Popen[bytes]") -> int:
        deadline = time.monotonic() + 10.0
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    raise DynamipsError(
                        f"dynamips hypervisor exited before announcing its port "
                        f"(exit code {proc.returncode})"
                    )
                continue
            decoded = line.decode("utf-8", errors="replace").strip()
            # Match either:
            #   "Hypervisor TCP control server started (port 12345)."
            #   "Hypervisor listening on TCP port 12345"
            for token in decoded.replace("(", " ").replace(")", " ").split():
                if token.isdigit():
                    return int(token)
            if "tcp" in decoded.lower() and "port" in decoded.lower():
                # Heuristic continues — fall through and try again.
                pass
        raise DynamipsError("timed out waiting for dynamips hypervisor port announcement")

    def _client_locked(self) -> HypervisorClient:
        if not self._hypervisor_running() or self._client is None:
            self._start_hypervisor()
        assert self._client is not None
        return self._client

    # ------------------------------------------------------------------
    # Lifecycle API (called by NodeRuntimeService)
    # ------------------------------------------------------------------

    def start_node(
        self,
        *,
        lab_id: str,
        node_id: int,
        template: dict[str, Any],
        node: dict[str, Any],
        attachments: Iterable[dict[str, Any]],
        console_port: int,
        tap_factory,
    ) -> dict[str, Any]:
        """Bring one Dynamips VM up and return the runtime record.

        ``tap_factory(interface_index, bridge_name)`` is supplied by the
        caller (node_runtime_service); it must create and bring up a TAP
        device mastered to ``bridge_name``, and return the TAP name. This
        keeps host-net side effects in the caller's lock domain.
        """
        platform = self._resolve_platform(template)
        module = _PLATFORM_MODULE[platform]
        image_path = self._resolve_image_path(template)
        idle_pc = self._resolve_idle_pc(template, image_path)

        with self._lock:
            client = self._client_locked()
            vm_name = self._vm_name(lab_id, node_id)
            vm_id = self._next_vm_id
            self._next_vm_id += 1
            work_dir = _RUNTIME_ROOT / lab_id / str(node_id)
            work_dir.mkdir(parents=True, exist_ok=True)

            # 1. Create the VM.
            client.request(f"{module} create {vm_name} {vm_id} {platform}")
            try:
                # 2. Per-platform configuration.
                ram = int(template.get("ram") or _PLATFORM_RAM_DEFAULT_MB[platform])
                client.request(f"{module} set_ram {vm_name} {ram}")
                client.request(f"{module} set_image {vm_name} {image_path}")
                client.request(f"{module} set_idle_pc {vm_name} {idle_pc}")
                client.request(f"{module} set_con_tcp_port {vm_name} {console_port}")

                chassis = _PLATFORM_CHASSIS.get(platform)
                if chassis:
                    client.request(f"{module} set_chassis {vm_name} {chassis}")
                npe = template.get("npe") or _PLATFORM_DEFAULT_NPE.get(platform)
                if npe and platform == "c7200":
                    client.request(f"{module} set_npe {vm_name} {npe}")

                # 3. Default slot 0 binding (interfaces live here).
                slot0_pa = (
                    template.get("slot0")
                    or _PLATFORM_SLOT0_DEFAULT_PA[platform]
                )
                client.request(
                    f"{module} add_slot_binding {vm_name} 0 0 {slot0_pa}"
                )

                # 4. Per-interface TAP+NIO wiring.
                tap_names: list[str] = []
                for attachment in attachments:
                    iface_idx = int(attachment["interface_index"])
                    bridge_name = str(attachment["bridge_name"])
                    tap = tap_factory(iface_idx, bridge_name)
                    nio_name = self._nio_name(lab_id, node_id, iface_idx)
                    client.request(f"nio create_tap {nio_name} {tap}")
                    client.request(
                        f"{module} add_nio_binding {vm_name} 0 {iface_idx} {nio_name}"
                    )
                    tap_names.append(tap)

                # 5. Start the VM.
                client.request(f"vm start {vm_name}")
            except Exception:
                # Best-effort teardown of the half-built VM so a retry can succeed.
                self._destroy_vm_locked(client, module, vm_name)
                raise

            runtime = {
                "kind": "dynamips",
                "lab_id": lab_id,
                "node_id": node_id,
                "vm_name": vm_name,
                "vm_id": vm_id,
                "platform": platform,
                "console_port": console_port,
                "hypervisor_port": self._hypervisor_port,
                "work_dir": str(work_dir),
                "tap_names": tap_names,
                "idle_pc": idle_pc,
                "image": str(image_path),
            }
            self._persist_runtime(runtime)
            return runtime

    def stop_node(self, runtime: dict[str, Any]) -> None:
        platform = str(runtime.get("platform") or "c7200")
        module = _PLATFORM_MODULE.get(platform, "c7200")
        vm_name = str(runtime["vm_name"])
        with self._lock:
            client = self._client_locked()
            self._destroy_vm_locked(client, module, vm_name)
            self._clear_runtime(runtime)

    def is_alive(self, runtime: dict[str, Any]) -> bool:
        vm_name = str(runtime.get("vm_name") or "")
        if not vm_name:
            return False
        try:
            with self._lock:
                client = self._client_locked()
                reply = client.request(f"vm get_status {vm_name}")
                # Dynamips returns "<code> 2" (running) / "1" (suspended) /
                # "0" (stopped). We treat running as alive.
                if reply.lines:
                    text = reply.lines[-1].strip()
                    return text.endswith("2") or text == "2"
                return False
        except DynamipsError:
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vm_name(lab_id: str, node_id: int) -> str:
        # Hypervisor identifiers can include dashes; namespacing prevents
        # collisions across labs sharing the host.
        return f"nv_{lab_id}_n{node_id}"

    @staticmethod
    def _nio_name(lab_id: str, node_id: int, interface_index: int) -> str:
        return f"nv_{lab_id}_n{node_id}_i{interface_index}"

    @staticmethod
    def _resolve_platform(template: dict[str, Any]) -> str:
        platform = str(template.get("platform") or "c7200").lower()
        if platform not in _PLATFORM_MODULE:
            raise DynamipsError(
                f"unsupported dynamips platform {platform!r}: "
                f"only {sorted(_PLATFORM_MODULE)} are supported in Phase 1"
            )
        return platform

    @staticmethod
    def _resolve_image_path(template: dict[str, Any]) -> Path:
        """Resolve a dynamips template's ``image`` field to a real path.

        Two on-disk layouts are accepted:

        * **Flat**: ``/var/lib/nova-ve/images/dynamips/<filename>`` — what
          a hand-authored template typically points at.
        * **EVE-NG-imported (per-image subdir)**:
          ``/var/lib/nova-ve/images/dynamips/<stem>/<filename>`` — what
          the importer produces for ``<source>/addons/dynamips/*.image``.

        Templates store just the filename in ``image`` and let the
        runtime locate it; absolute paths are honoured as-is.
        """
        image = template.get("image")
        if not image:
            raise DynamipsError("dynamips template has no image path")
        path = Path(str(image))
        if path.is_absolute():
            if not path.is_file():
                raise DynamipsError(f"dynamips image not found at {path}")
            return path

        flat = _IMAGES_ROOT / path
        if flat.is_file():
            return flat
        nested = _IMAGES_ROOT / path.stem / path.name
        if nested.is_file():
            return nested
        raise DynamipsError(
            f"dynamips image {path.name!r} not found at {flat} or {nested}"
        )

    def _resolve_idle_pc(self, template: dict[str, Any], image_path: Path) -> str:
        # 1. Explicit template override always wins.
        explicit = str(template.get("idlepc") or template.get("idle_pc") or "").strip()
        if explicit:
            return explicit
        # 2. Cached value for this image.
        sha = IdlePcCache.hash_image(image_path)
        cached = self._idle_pc_cache.get(sha)
        if cached:
            return cached
        # 3. No value available — fail fast. Silently booting with no
        #    idle-pc pegs a CPU core per node and degrades the whole host,
        #    which is a worse outcome than a clear error.
        raise DynamipsError(
            f"no idle-PC value for image {image_path.name} (sha256 prefix "
            f"{sha[:12]}). Set the `idlepc` field on the template, or run "
            f"the dynamips calibration CLI to populate the cache."
        )

    def _destroy_vm_locked(
        self,
        client: HypervisorClient,
        module: str,
        vm_name: str,
    ) -> None:
        # Stop is safe to call on a stopped VM; delete fails if the VM
        # never existed. We swallow stop errors but bubble delete errors
        # so an asymmetric tear-down is visible.
        try:
            client.request(f"vm stop {vm_name}")
        except DynamipsError as exc:
            _logger.info(
                "dynamips.stop.ignored",
                extra={"vm_name": vm_name, "error": str(exc)},
            )
        client.request(f"{module} delete {vm_name}")

    def _persist_runtime(self, runtime: dict[str, Any]) -> None:
        work_dir = Path(runtime["work_dir"])
        with (work_dir / "dynamips.json").open("w") as fp:
            json.dump(runtime, fp, indent=2)

    def _clear_runtime(self, runtime: dict[str, Any]) -> None:
        work_dir = Path(runtime["work_dir"])
        record = work_dir / "dynamips.json"
        if record.exists():
            record.unlink()


__all__ = [
    "DynamipsError",
    "DynamipsLauncher",
    "HypervisorClient",
    "IdlePcCache",
]
