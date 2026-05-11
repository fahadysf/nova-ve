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
# ``vm start`` triggers IOS image load + decompress + initial MIPS
# translation; for a stock c3725 with 12.4 IOS this can take 30-50s
# before the hypervisor returns ``100 OK``. Give it room.
_HYPERVISOR_READ_TIMEOUT_S = 120.0
_DYNAMIPS_BINARY = os.environ.get("NOVA_VE_DYNAMIPS_BIN", "dynamips")


# Platforms and their hypervisor command-module names. In dynamips 0.2.14
# every supported platform is its OWN module (verified via
# ``hypervisor module_list``: c1700, c2600, c2691, c3745, c3725, c3600,
# c7200). The c3600 module is reserved for the true c3600 family
# (3620/3640/3660); c3725 is independent. Phase 1 ships the two below;
# extending to c3745/c2691/c2600/c1700 is a 1-line addition.
_PLATFORM_MODULE = {
    "c3725": "c3725",
    "c7200": "c7200",
}

# Dynamips' c3600 module requires `set_chassis` so it knows which c3600
# variant to emulate (3620 / 3640 / 3660). c3725 / c3745 are SEPARATE
# top-level modules in 0.2.14 (created via ``vm create ... c3725``) — they
# do NOT support set_chassis (the c3725 module doesn't expose it, and
# trying ``c3600 set_chassis`` on them errors with "is not a VM type
# c3600"). So this map is empty in Phase 1 and only gets populated if
# we add a true c3600-class platform.
_PLATFORM_CHASSIS: dict[str, str] = {}

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

# Platforms whose slot 0 is fixed hardware that dynamips pre-binds at
# ``vm create`` time. Re-binding fails with "unable to add binding for
# slot 0/0", so we skip the explicit slot_add_binding for these.
_PLATFORM_SLOT0_PREBOUND = {"c3725"}

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
            except socket.timeout as exc:
                raise DynamipsError(
                    f"hypervisor read timed out after "
                    f"{_HYPERVISOR_READ_TIMEOUT_S}s — the previous command "
                    f"did not complete"
                ) from exc
            if not chunk:
                raise DynamipsError("hypervisor closed the connection unexpectedly")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line.rstrip(b"\r")

    def request(self, command: str) -> _Reply:
        """Send one command, read until the terminal reply line.

        Dynamips 0.2.14 replies use the format ``CODE text`` (space
        separator) for data/continuation lines (typically code 101) and
        ``CODE-text`` (dash separator) for the TERMINAL line. ``100-OK``
        is the canonical success terminator; ``1xx-message`` is a
        single-line success; ``2xx-message`` is a single-line error.
        This is the inverse of SMTP-style protocols — beware.
        """
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        self._sock.sendall(command.encode("utf-8") + b"\n")

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
            if sep == "-":
                break
            if sep != " ":
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

    def __init__(self, path: Path | None = None) -> None:
        # Defer to the module-level constant at call time so tests that
        # monkeypatch ``_IDLE_PC_CACHE_PATH`` see their override take
        # effect (default-argument evaluation happens once at class-
        # definition time and would otherwise capture the production
        # path forever).
        self._path = path or _IDLE_PC_CACHE_PATH
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
        # Pre-pick a free TCP port. ``dynamips -H 0`` is NOT an
        # "auto-pick" knob in 0.2.14 — it prints "Hypervisor: unable
        # to create TCP sockets." and silently falls back to its
        # default port (7200), which would collide with any other
        # dynamips on the host. Choose explicitly.
        port = self._pick_free_tcp_port()
        proc = subprocess.Popen(
            [_DYNAMIPS_BINARY, "-H", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Poll connect() until dynamips is listening, capped at 10s.
        # No stdout parsing — we KNOW the port.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise DynamipsError(
                    f"dynamips hypervisor exited before binding port {port} "
                    f"(exit code {proc.returncode})"
                )
            try:
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=0.5
                ) as probe:
                    probe.close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            raise DynamipsError(
                f"dynamips hypervisor did not start listening on 127.0.0.1:{port} "
                f"within 10s"
            )

        self._hypervisor_proc = proc
        self._hypervisor_port = port
        self._client = HypervisorClient(port)
        self._client.connect()
        _logger.info(
            "dynamips.hypervisor.started",
            extra={"pid": proc.pid, "port": port},
        )

    @staticmethod
    def _pick_free_tcp_port() -> int:
        """Bind to port 0, read back the kernel-assigned port, close.

        A tiny TOCTOU window exists between the close and dynamips
        binding the same port — acceptable for a per-host singleton
        started once at first use.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

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

        # Allocate the port dynamips itself will bind (127.0.0.1 only —
        # dynamips' ``vm set_con_tcp_port`` has no listen-addr knob in
        # 0.2.14). The caller-supplied ``console_port`` is the EXTERNAL
        # port guacd will dial via ``host.docker.internal``; the runtime
        # service spawns a 0.0.0.0:<external> → 127.0.0.1:<internal>
        # proxy after we return. Allocating before lock entry keeps the
        # critical section short.
        console_internal_port = self._pick_free_tcp_port()

        with self._lock:
            client = self._client_locked()
            vm_name = self._vm_name(lab_id, node_id)
            vm_id = self._next_vm_id
            self._next_vm_id += 1
            work_dir = _RUNTIME_ROOT / lab_id / str(node_id)
            work_dir.mkdir(parents=True, exist_ok=True)

            # 1. Create the VM under the generic ``vm`` namespace — in
            #    dynamips 0.2.14 only ``vm create`` exists; the
            #    platform-prefixed modules (c3600/c7200) have no
            #    ``create`` command.
            client.request(f"vm create {vm_name} {vm_id} {platform}")
            try:
                # 2. Per-VM configuration. Everything common (RAM, IOS
                #    image, idle-PC, console port) lives in the ``vm``
                #    namespace; only hardware-shape knobs (chassis, NPE)
                #    live under the platform module.
                ram = int(template.get("ram") or _PLATFORM_RAM_DEFAULT_MB[platform])
                client.request(f"vm set_ram {vm_name} {ram}")
                client.request(f"vm set_ios {vm_name} {image_path}")
                client.request(f"vm set_idle_pc {vm_name} {idle_pc}")
                client.request(
                    f"vm set_con_tcp_port {vm_name} {console_internal_port}"
                )

                chassis = _PLATFORM_CHASSIS.get(platform)
                if chassis and module == "c3600":
                    # c3600 family needs the chassis variant (3620/3640/3660),
                    # but c3725 is its own module without a chassis command.
                    client.request(f"{module} set_chassis {vm_name} {chassis}")
                npe = template.get("npe") or _PLATFORM_DEFAULT_NPE.get(platform)
                if npe and platform == "c7200":
                    client.request(f"{module} set_npe {vm_name} {npe}")

                # 3. Default slot 0 binding (interfaces live here).
                #    On c3725 (and other motherboard-FE platforms) the
                #    builtin GT96100-FE is pre-bound at ``vm create``;
                #    re-binding fails. We skip the call for those and
                #    only emit slot_add_binding where it's required
                #    (e.g. c7200 needs an explicit C7200-IO-FE).
                if platform not in _PLATFORM_SLOT0_PREBOUND:
                    slot0_pa = (
                        template.get("slot0")
                        or _PLATFORM_SLOT0_DEFAULT_PA[platform]
                    )
                    client.request(
                        f"vm slot_add_binding {vm_name} 0 0 {slot0_pa}"
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
                        f"vm slot_add_nio_binding {vm_name} 0 {iface_idx} {nio_name}"
                    )
                    tap_names.append(tap)

                # 5. Start the VM.
                client.request(f"vm start {vm_name}")
            except Exception:
                # Best-effort teardown of the half-built VM so a retry can succeed.
                self._destroy_vm_locked(client, vm_name)
                raise

            runtime = {
                "kind": "dynamips",
                "lab_id": lab_id,
                "node_id": node_id,
                "vm_name": vm_name,
                "vm_id": vm_id,
                "platform": platform,
                # External port: what guacd dials via host.docker.internal.
                # Internal port: what dynamips bound on 127.0.0.1. The
                # runtime service bridges them with a 0.0.0.0 proxy.
                "console_port": console_port,
                "console_internal_port": console_internal_port,
                "hypervisor_port": self._hypervisor_port,
                "work_dir": str(work_dir),
                "tap_names": tap_names,
                "idle_pc": idle_pc,
                "image": str(image_path),
            }
            self._persist_runtime(runtime)
            return runtime

    def stop_node(self, runtime: dict[str, Any]) -> None:
        vm_name = str(runtime["vm_name"])
        with self._lock:
            client = self._client_locked()
            self._destroy_vm_locked(client, vm_name)
            self._clear_runtime(runtime)

    def is_alive(self, runtime: dict[str, Any]) -> bool:
        vm_name = str(runtime.get("vm_name") or "")
        if not vm_name:
            return False
        try:
            with self._lock:
                client = self._client_locked()
                reply = client.request(f"vm get_status {vm_name}")
                # Dynamips returns the status as the terminator payload:
                # ``100-0`` (stopped), ``100-1`` (suspended), ``100-2``
                # (running). We treat running as alive.
                if reply.lines:
                    text = reply.lines[-1].strip()
                    return text == "2"
                return False
        except DynamipsError:
            return False

    # ------------------------------------------------------------------
    # Idle-PC calibration
    # ------------------------------------------------------------------

    def calibrate_image(
        self,
        image_path: Path,
        *,
        boot_wait_s: float = 90.0,
        retry_wait_s: float = 20.0,
    ) -> dict[str, Any]:
        """Boot a throwaway VM against ``image_path``, harvest idle-PC
        candidates via the hypervisor's ``vm extract_idle_pc`` command,
        cache the first candidate keyed by image SHA-256, and return
        a result record.

        Blocks for ``boot_wait_s`` seconds while IOS settles. Caller
        is responsible for surfacing that latency to the user (the
        HTTP layer treats this as a long-running sync call).

        Result shape::

            {
              "image": <basename>,
              "image_sha256": <hex>,
              "idle_pc": "0x...",
              "candidates": ["0x...", ...],
              "duration_s": <float>,
              "platform": "c3725" | "c7200",
            }
        """
        platform = self._platform_for_image(image_path.name)
        module = _PLATFORM_MODULE[platform]
        ram = _PLATFORM_RAM_DEFAULT_MB[platform]
        vm_name = f"calibrate_{platform}_{int(time.time())}"
        vm_id = (int(time.time()) % 65000) + 1
        started = time.monotonic()

        with self._lock:
            client = self._client_locked()
            client.request(f"vm create {vm_name} {vm_id} {platform}")
            try:
                client.request(f"vm set_ram {vm_name} {ram}")
                client.request(f"vm set_ios {vm_name} {image_path}")
                chassis = _PLATFORM_CHASSIS.get(platform)
                if chassis and module == "c3600":
                    client.request(f"{module} set_chassis {vm_name} {chassis}")
                if platform == "c7200":
                    client.request(
                        f"{module} set_npe {vm_name} "
                        f"{_PLATFORM_DEFAULT_NPE[platform]}"
                    )
                client.request(f"vm start {vm_name}")
            except Exception:
                self._destroy_vm_locked(client, vm_name)
                raise

        # Release the launcher lock while IOS boots — calibration is
        # the only consumer of this VM, but holding the lock would
        # serialise unrelated start_node calls behind the 90 s wait.
        # The retry sleep is also outside the lock-held window so a
        # caller starting a regular node can interleave.
        try:
            time.sleep(boot_wait_s)
            with self._lock:
                client = self._client_locked()
                candidates = self._extract_idle_pc_candidates(client, vm_name)
            if not candidates:
                time.sleep(retry_wait_s)
                with self._lock:
                    client = self._client_locked()
                    candidates = self._extract_idle_pc_candidates(client, vm_name)
        finally:
            with self._lock:
                client = self._client_locked()
                self._destroy_vm_locked(client, vm_name)

        if not candidates:
            raise DynamipsError(
                f"dynamips extracted no idle-PC candidates for "
                f"{image_path.name} after {boot_wait_s + retry_wait_s}s"
            )

        idle_pc = candidates[0]
        image_sha = IdlePcCache.hash_image(image_path)
        self._idle_pc_cache.set(image_sha, idle_pc)
        duration = time.monotonic() - started

        return {
            "image": image_path.name,
            "image_sha256": image_sha,
            "idle_pc": idle_pc,
            "candidates": candidates,
            "duration_s": round(duration, 2),
            "platform": platform,
        }

    @staticmethod
    def _platform_for_image(image_name: str) -> str:
        lower = image_name.lower()
        for platform in _PLATFORM_MODULE:
            if lower.startswith(platform):
                return platform
        raise DynamipsError(
            f"cannot infer platform from image name {image_name!r}; "
            f"expected a prefix in {sorted(_PLATFORM_MODULE)}"
        )

    @staticmethod
    def _extract_idle_pc_candidates(
        client: HypervisorClient, vm_name: str
    ) -> list[str]:
        """Ask dynamips for idle-PC candidates on CPU 0 of ``vm_name``.

        Stock dynamips 0.2.14 has no ``vm extract_idle_pc`` command
        (that's a GNS3-fork addition). The shipping idle-PC discovery
        path is ``vm show_idle_pc_prop <vm> <cpu_id>`` which prints
        candidate PCs as 101-data lines like ``0x60c09320 [12]``.
        """
        reply = client.request(f"vm show_idle_pc_prop {vm_name} 0")
        candidates: list[str] = []
        for line in reply.lines:
            text = line.strip()
            # Format: ``0x<hex> [<count>]`` — we only want the PC value.
            if text.startswith("0x"):
                candidates.append(text.split(None, 1)[0])
        return candidates

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

        Three input shapes are accepted, all reflecting how the frontend
        catalog labels images:

        * **Full filename with extension** (``foo.image`` / ``foo.bin``)
          — what hand-authored templates typically write.
        * **Stem only** (``foo``) — what the node-create catalog hands
          back when the EVE-NG importer used the per-image-subdir
          layout: ``TemplateService._image_info`` reports
          ``dir.name`` (no extension) as the image label, so when the
          frontend saves the user's selection onto the node, the bare
          stem is what we see at start time.
        * **Absolute path** — passes through if it exists.

        For each of the first two, both the flat
        (``/var/lib/nova-ve/images/dynamips/<file>``) and EVE-NG nested
        (``.../dynamips/<stem>/<file>``) layouts are searched. Known
        extensions ``.image`` (EVE-NG) and ``.bin`` (Cisco) are probed
        when a stem is supplied.
        """
        image = template.get("image")
        if not image:
            raise DynamipsError("dynamips template has no image path")
        path = Path(str(image))
        if path.is_absolute():
            if not path.is_file():
                raise DynamipsError(f"dynamips image not found at {path}")
            return path

        # Cisco image filenames have dots that ``Path.suffix`` happily
        # treats as extensions (``...mz.124-25d`` → suffix=``.124-25d``),
        # so we cannot use ``path.suffix`` to decide "is this a stem or a
        # filename". Instead, probe every plausible layout in order and
        # return the first hit.
        name = path.name
        candidates: list[Path] = [
            # Input is exactly the filename (flat layout).
            _IMAGES_ROOT / name,
            # Input is the filename and EVE-NG used a subdir of that
            # filename's stem.
            _IMAGES_ROOT / path.stem / name,
        ]
        # Input might also be a stem (no extension). Probe each known
        # extension in both layouts. ``.image`` comes first because
        # that's what the EVE-NG importer — the most common source of
        # stem-style image labels — produces.
        for ext in (".image", ".bin"):
            candidates.append(_IMAGES_ROOT / f"{name}{ext}")
            candidates.append(_IMAGES_ROOT / name / f"{name}{ext}")

        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = ", ".join(str(c) for c in candidates)
        raise DynamipsError(
            f"dynamips image {name!r} not found; searched: {searched}"
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
        vm_name: str,
    ) -> None:
        # ``vm clean_delete`` stops the VM (if running) and removes both
        # the in-memory state and on-disk artifacts in one call — more
        # forgiving than ``vm stop`` + ``vm delete``, which can fail
        # 207 "unable to delete" if the VM is in a half-crashed state.
        # We log and swallow errors here so a failed teardown never
        # masks the original lifecycle error in the caller.
        try:
            client.request(f"vm clean_delete {vm_name}")
        except DynamipsError as exc:
            _logger.warning(
                "dynamips.destroy.failed",
                extra={"vm_name": vm_name, "error": str(exc)},
            )

    def _persist_runtime(self, runtime: dict[str, Any]) -> None:
        work_dir = Path(runtime["work_dir"])
        with (work_dir / "dynamips.json").open("w") as fp:
            json.dump(runtime, fp, indent=2)

    def _clear_runtime(self, runtime: dict[str, Any]) -> None:
        work_dir = Path(runtime["work_dir"])
        record = work_dir / "dynamips.json"
        if record.exists():
            record.unlink()


def list_dynamips_images(
    *, images_root: Path | None = None
) -> list[dict[str, Any]]:
    """Enumerate Dynamips images on disk and report calibration status.

    Mirrors the launcher's image-path resolution: both flat
    (``<root>/<file>``) and per-image-subdir (``<root>/<stem>/<file>``)
    layouts are accepted, so a single image is reported once regardless
    of which on-disk layout the importer or operator used.

    Each entry::

        {
          "image": "<basename>",
          "path": "<absolute path>",
          "size_bytes": <int>,
          "platform": "c3725" | "c7200" | null,
          "image_sha256": "<hex>",
          "calibrated": <bool>,
          "idle_pc": "0x..." | null,
        }
    """
    root = images_root or _IMAGES_ROOT
    if not root.is_dir():
        return []

    cache = IdlePcCache()
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []

    def _report(image_path: Path) -> None:
        name = image_path.name
        if name in seen:
            return
        seen.add(name)
        try:
            platform = DynamipsLauncher._platform_for_image(name)
        except DynamipsError:
            platform = None
        sha = IdlePcCache.hash_image(image_path)
        idle_pc = cache.get(sha)
        entries.append(
            {
                "image": name,
                "path": str(image_path),
                "size_bytes": image_path.stat().st_size,
                "platform": platform,
                "image_sha256": sha,
                "calibrated": idle_pc is not None,
                "idle_pc": idle_pc,
            }
        )

    # Flat layout first, then nested. The launcher prefers flat → nested
    # for path resolution; mirror that order here so the "primary" path
    # surfaced in the report matches what start_node would pick.
    for child in sorted(root.iterdir()):
        if child.is_file() and child.suffix in {".bin", ".image"}:
            _report(child)
        elif child.is_dir():
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_file() and grandchild.suffix in {".bin", ".image"}:
                    _report(grandchild)

    return entries


__all__ = [
    "DynamipsError",
    "DynamipsLauncher",
    "HypervisorClient",
    "IdlePcCache",
    "list_dynamips_images",
]
