# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Node runtime adapter for QEMU and Docker labs.

US-301 — pcie-root-port slot policy
------------------------------------
QEMU PCI hot-plug requires pre-allocated ``pcie-root-port`` chassis on q35.
At VM start we pre-allocate ``template.capabilities.max_nics`` root ports.
Slot 0 is reserved on q35 (the root complex itself); the first usable slot
is ``1``. Initial NICs declared at boot occupy ``rp0..rp{N-1}`` in
``interface_index`` order. Hot-add (US-303) scans for free slots starting
from ``rp{max_nics-1}`` downward so additions never collide with the
boot-time positional layout. Hot-remove (US-304) frees the matching slot.

Machine-type discrimination:
- ``node.machine_override`` (set by ``scripts/migrate_runtime_network.py``
  on pre-Wave-7 QEMU nodes) wins if present.
- Otherwise the launcher reads ``template.capabilities.machine`` (default
  ``q35`` for new templates, ``pc`` for legacy YAMLs that omit the field
  via inferred defaults).
- Templates with ``capabilities.machine='pc' AND hotplug=true`` are
  rejected at template-load time (template_service._validate_capabilities).
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import psutil

from app.config import get_settings
from app.services import host_net, runtime_pids


_DOCKER_RESTART_POLICIES = {"no", "on-failure", "unless-stopped", "always"}
_HEARTBEAT_INTERVAL_S: float = 5.0
_TRANSITION_SUPPRESS_S: float = 30.0

_logger = logging.getLogger("nova-ve.heartbeat")
_discovery_logger = logging.getLogger("nova-ve.discovery")


def _node_extras(node: dict[str, Any]) -> dict[str, Any]:
    extras = node.get("extras")
    return dict(extras) if isinstance(extras, dict) else {}


def _extra_str(extras: dict[str, Any], key: str, default: str = "") -> str:
    value = extras.get(key, default)
    return "" if value is None else str(value).strip()


def _parse_qemu_tokens(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Group a QEMU argv list into [(flag, [value_tokens...]), ...].

    Tokens starting with '-' are flags; following non-flag tokens are their
    values. Multiple occurrences of the same flag are kept as separate entries.
    Leading non-flag tokens (e.g. the binary path) are represented as (token, []).
    """
    groups: list[tuple[str, list[str]]] = []
    current_flag: str | None = None
    current_vals: list[str] = []
    for tok in tokens:
        if tok.startswith("-"):
            if current_flag is not None:
                groups.append((current_flag, current_vals))
            current_flag = tok
            current_vals = []
        else:
            if current_flag is None:
                groups.append((tok, []))
            else:
                current_vals.append(tok)
    if current_flag is not None:
        groups.append((current_flag, current_vals))
    return groups


def _merge_qemu_args(base_cmd: list[str], extra_str: str) -> list[str]:
    """Merge user extra_str into base_cmd with flag-level override semantics.

    If the user supplies any instance of flag F, ALL default instances of F
    are removed and replaced by the user's instance(s). Flags not present in
    extra_str pass through unchanged.
    """
    if not extra_str:
        return base_cmd
    extra_tokens = shlex.split(extra_str)
    base_groups = _parse_qemu_tokens(base_cmd)
    extra_groups = _parse_qemu_tokens(extra_tokens)
    override_flags = {flag for flag, _ in extra_groups if flag.startswith("-")}
    merged = [g for g in base_groups if g[0] not in override_flags]
    merged.extend(extra_groups)
    result: list[str] = []
    for flag, vals in merged:
        result.append(flag)
        result.extend(vals)
    return result



class NodeRuntimeError(Exception):
    pass


class NodeRuntimeQMPTimeout(NodeRuntimeError):
    """US-303 codex iter1 HIGH-1: raised when a QMP transport-level
    error or socket timeout is observed while sending a command.

    Subclass of :class:`NodeRuntimeError` so existing
    ``except NodeRuntimeError`` clauses still catch it. The distinct
    subclass lets the rollback dispatcher in
    ``_attach_qemu_interface_locked`` recognise the "may have succeeded
    in QEMU" case and run the FULL rollback chain (both ``device_del``
    AND ``netdev_del``) regardless of which step the timeout fired on,
    because after a transport-level failure we cannot tell whether QEMU
    applied the command.
    """

    pass


def _default_qmp_client(socket_path: str, command: str) -> dict:
    """Connect to a QEMU QMP socket, send `command`, and return the parsed response.

    Performs a minimal QMP handshake (read greeting, send qmp_capabilities, send command).
    Raises FileNotFoundError or OSError when the socket is missing/unreachable.
    """
    return _qmp_send_with_args(socket_path, command, None)


def _qmp_send_with_args(
    socket_path: str, command: str, arguments: dict[str, Any] | None
) -> dict:
    """Connect to a QEMU QMP socket, send `command` with optional `arguments`,
    return the parsed response.

    Used by US-303 hot-add (which needs ``netdev_add`` / ``device_add``
    arguments) and as the implementation backing the bare-2-arg
    :func:`_default_qmp_client` for the simple ``query-rx-filter`` /
    ``query-pci`` path.
    """
    if not Path(socket_path).exists():
        raise FileNotFoundError(f"qmp socket not found: {socket_path}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(socket_path)
        buffer = b""

        def _read_line() -> dict:
            nonlocal buffer
            while b"\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("qmp socket closed during read")
                buffer += chunk
            line, _, buffer = buffer.partition(b"\n")
            return json.loads(line.decode("utf-8"))

        # Greeting (QMP banner)
        _read_line()
        sock.sendall(json.dumps({"execute": "qmp_capabilities"}).encode("utf-8") + b"\n")
        _read_line()
        payload: dict[str, Any] = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        response = _read_line()
        return response if isinstance(response, dict) else {}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _default_docker_inspect(docker_binary: str, docker_host: str, container_name: str) -> dict:
    result = subprocess.run(
        [
            docker_binary,
            "--host",
            docker_host,
            "inspect",
            container_name,
            "--format",
            "{{json .NetworkSettings}}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise NodeRuntimeError(result.stderr.strip() or "docker inspect failed")
    return json.loads(result.stdout.strip() or "{}")


class NodeRuntimeService:
    _registry: dict[str, dict[str, Any]] = {}
    _loaded = False
    _lock = threading.Lock()
    # Maps (lab_id, node_id) -> monotonic timestamp of last deliberate start/stop.
    # Heartbeat skips reconciliation for entries younger than _TRANSITION_SUPPRESS_S.
    _transition_timestamps: dict[tuple[str, int], float] = {}
    # Per-lab discovery generation counter (US-403 MEDIUM-3 gen-token fix).
    # Incremented at the start of each _discover_lab call so every emitted
    # discovered_link / link_divergent event carries the generation that produced
    # it.  lab_topology snapshots include the current value so the frontend can
    # reject events from older generations (gen <= topology_generation is stale).
    _discovery_generation: dict[str, int] = {}

    def __init__(self) -> None:
        self.settings = get_settings()
        self.runtime_dir = self.settings.TMP_DIR / "node-runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        # Dependency-injectable hooks so tests can monkey-patch QMP/docker IO.
        self._qmp_client: Callable[[str, str], dict] = _default_qmp_client
        self._docker_inspect: Callable[[str, str, str], dict] = _default_docker_inspect
        # US-205b: read MAC from inside the container's netns via the privileged
        # helper.  After US-207 containers run with ``--network=none`` so
        # ``docker inspect .NetworkSettings.Networks`` is empty; sysfs is the
        # only source of truth for the live MAC.
        self._read_iface_mac: Callable[[int, str], str] = host_net.read_iface_mac
        self._load_registry()

    @classmethod
    def reset_registry(cls) -> None:
        with cls._lock:
            cls._registry.clear()
            cls._loaded = False
        cls._transition_timestamps.clear()
        cls._discovery_generation.clear()

    @classmethod
    def get_discovery_generation(cls, lab_id: str) -> int:
        """Return the current discovery generation for *lab_id*.

        Used by the WS reconnect handler (``ws.py``) to include the generation
        in ``lab_topology`` snapshots so the frontend can reject
        ``discovered_link`` / ``link_divergent`` events from older cycles.
        """
        return cls._discovery_generation.get(lab_id, 0)

    @classmethod
    def _record_transition(cls, lab_id: str, node_id: int) -> None:
        """Mark that a deliberate start/stop just happened; suppresses heartbeat reconcile."""
        cls._transition_timestamps[(lab_id, node_id)] = time.monotonic()

    @classmethod
    def _is_suppressed(cls, lab_id: str, node_id: int) -> bool:
        """Return True if the node is within the post-transition suppression window."""
        ts = cls._transition_timestamps.get((lab_id, node_id))
        if ts is None:
            return False
        return (time.monotonic() - ts) < _TRANSITION_SUPPRESS_S

    def start_node(self, lab_data: dict[str, Any], node_id: int) -> dict[str, Any]:
        lab_id = self._lab_id(lab_data)
        node = self._node_data(lab_data, node_id)
        key = self._key(lab_id, node_id)
        runtime = self._runtime_record(lab_id, node_id)
        if runtime:
            return runtime

        if node.get("type") == "qemu":
            runtime = self._start_qemu_node(lab_data, node)
        elif node.get("type") == "docker":
            runtime = self._start_docker_node(lab_data, node)
        else:
            raise NodeRuntimeError(f"Unsupported node type: {node.get('type')}")

        self._record_transition(lab_id, node_id)
        with self._lock:
            self._registry[key] = runtime
        self._persist_runtime(runtime)

        # Cadence: schedule live-MAC reads at t=1s/3s/8s after start. No steady-state poll.
        interfaces = node.get("interfaces") or []
        if interfaces:
            self._schedule_live_mac_cadence(lab_data, lab_id, node_id, interfaces)

        return runtime

    def _schedule_live_mac_cadence(
        self,
        lab_data: dict[str, Any],
        lab_id: str,
        node_id: int,
        interfaces: list[dict[str, Any]],
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run_cadence() -> None:
            for delay in (1.0, 3.0, 8.0):
                await asyncio.sleep(delay)
                for index, iface in enumerate(interfaces):
                    if not isinstance(iface, dict):
                        continue
                    interface_index = int(iface.get("index", index))
                    result = self.read_live_mac(lab_id, node_id, interface_index, lab_data=lab_data)
                    await self._publish_live_mac(lab_id, node_id, interface_index, result)

        loop.create_task(_run_cadence())

    async def _publish_live_mac(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        result: dict[str, Any],
    ) -> None:
        try:
            from app.services.ws_hub import ws_hub  # local import to avoid cycles
        except ImportError:
            return
        payload = {
            "node_id": node_id,
            "interface_index": interface_index,
            "state": result.get("state"),
            "planned_mac": result.get("planned_mac"),
            "live_mac": result.get("live_mac"),
            "reason": result.get("reason"),
        }
        try:
            await ws_hub.publish(lab_id, "interface_live_mac", payload, rev=str(lab_id))
        except Exception:
            return

    def read_live_mac(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        lab_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the live MAC state for a single interface. Never raises."""
        try:
            return self._read_live_mac_inner(lab_id, node_id, interface_index, lab_data)
        except Exception as exc:  # never raise — degrade to "unavailable"
            runtime_type = "unknown"
            if lab_data is not None:
                try:
                    node = self._node_data(lab_data, node_id)
                    runtime_type = str(node.get("type", "unknown"))
                except NodeRuntimeError:
                    pass
            if runtime_type == "unknown":
                runtime = self._registry.get(self._key(lab_id, node_id))
                if runtime:
                    runtime_type = str(runtime.get("kind", "unknown"))
            return {
                "state": "unavailable",
                "planned_mac": "",
                "live_mac": None,
                "runtime_type": runtime_type,
                "reason": f"live-mac read failed: {exc}",
            }

    def _read_live_mac_inner(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        lab_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        node: dict[str, Any] = {}
        if lab_data is not None:
            try:
                node = self._node_data(lab_data, node_id)
            except NodeRuntimeError:
                node = {}

        runtime = self._runtime_record(lab_id, node_id, include_stopped=True) or {}
        runtime_type = (
            str(node.get("type", ""))
            or str(runtime.get("kind", ""))
            or "qemu"
        )

        interface = self._lookup_interface(node, interface_index)
        planned_mac = ""
        if interface and interface.get("planned_mac"):
            planned_mac = str(interface["planned_mac"])

        if runtime_type in ("iol", "dynamips"):
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": runtime_type,
                "reason": "runtime adapter not implemented",
            }

        if runtime_type == "qemu":
            return self._read_qemu_live_mac(runtime, planned_mac, interface_index)

        if runtime_type == "docker":
            return self._read_docker_live_mac(
                runtime,
                planned_mac,
                lab_id,
                lab_data,
                interface,
            )

        return {
            "state": "unavailable",
            "planned_mac": planned_mac,
            "live_mac": None,
            "runtime_type": runtime_type,
            "reason": f"unsupported runtime type: {runtime_type}",
        }

    def _read_qemu_live_mac(
        self,
        runtime: dict[str, Any],
        planned_mac: str,
        interface_index: int,
    ) -> dict[str, Any]:
        socket_path = runtime.get("qmp_socket") or ""
        if not socket_path:
            work_dir = runtime.get("work_dir")
            socket_path = str(Path(work_dir) / "qmp.sock") if work_dir else ""
        if not socket_path:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "qemu",
                "reason": "qmp unreachable: no socket path",
            }
        try:
            response = self._qmp_client(socket_path, "query-rx-filter")
        except Exception as exc:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "qemu",
                "reason": f"qmp unreachable: {exc}",
            }

        entries = response.get("return") if isinstance(response, dict) else None
        if not isinstance(entries, list):
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "qemu",
                "reason": "qmp returned no rx-filter data",
            }

        target_id = f"net{interface_index}"
        match = next(
            (entry for entry in entries if isinstance(entry, dict) and entry.get("name") == target_id),
            None,
        )
        if match is None and entries:
            match = entries[interface_index] if 0 <= interface_index < len(entries) else None

        if not isinstance(match, dict) or not match.get("main-mac"):
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "qemu",
                "reason": "qmp returned no main-mac for interface",
            }

        live_mac = str(match["main-mac"])
        state = "confirmed" if planned_mac.lower() == live_mac.lower() else "mismatch"
        return {
            "state": state,
            "planned_mac": planned_mac,
            "live_mac": live_mac,
            "runtime_type": "qemu",
            "reason": None if state == "confirmed" else "live mac differs from planned",
        }

    def _read_docker_live_mac(
        self,
        runtime: dict[str, Any],
        planned_mac: str,
        lab_id: str,
        lab_data: dict[str, Any] | None,
        interface: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Read the live MAC of a Docker container's NIC from inside its netns.

        US-205b: after US-207 containers start with ``--network=none`` so
        ``docker inspect .NetworkSettings.Networks`` is permanently empty.
        The MAC for ``eth{interface_index}`` (created by US-204's veth + nsenter
        rename path) lives only in sysfs inside the container's netns.  We
        invoke the privileged helper's ``read-iface-mac`` verb which performs
        ``nsenter -t <pid> -n cat /sys/class/net/<iface>/address`` after
        validating that ``pid`` is a runtime nova-ve registered.

        The PID is resolved fresh on every call via ``docker inspect
        {{.State.Pid}}`` (``_docker_container_pid``) — never from the cached
        ``runtime["pid"]`` recorded at start time.  Docker restart policies
        are explicitly supported (cf. start_node), so the kernel PID can
        change after ``docker restart`` / crash-restart / PID rollover; using
        the cached value risks reading a stale netns or, worst case, an
        unrelated process's namespace if the PID was reused.
        """
        container_name = runtime.get("container_name")
        if not container_name:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": "docker runtime not started",
            }

        docker_binary = self._resolve_binary("docker")
        if not docker_binary:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": "docker binary not found",
            }

        # Resolve PID fresh on every read — see docstring.
        try:
            pid = self._docker_container_pid(docker_binary, container_name) or 0
        except Exception as exc:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": f"docker inspect pid failed: {exc}",
            }
        if pid <= 0:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": "docker inspect returned no pid",
            }

        if not interface:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": "interface metadata missing",
            }
        try:
            interface_index = int(interface.get("index", 0))
        except (TypeError, ValueError):
            interface_index = 0
        iface_name = f"eth{interface_index}"

        try:
            live_mac_raw = self._read_iface_mac(pid, iface_name)
        except Exception as exc:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": f"read-iface-mac failed: {exc}",
            }

        live_mac = (live_mac_raw or "").strip()
        if not live_mac:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": "read-iface-mac returned empty MAC",
            }

        state = "confirmed" if planned_mac.lower() == live_mac.lower() else "mismatch"
        return {
            "state": state,
            "planned_mac": planned_mac,
            "live_mac": live_mac,
            "runtime_type": "docker",
            "reason": None if state == "confirmed" else "live mac differs from planned",
        }

    @staticmethod
    def _lookup_interface(node: dict[str, Any], interface_index: int) -> dict[str, Any] | None:
        interfaces = node.get("interfaces") if isinstance(node, dict) else None
        if not isinstance(interfaces, list):
            return None
        if 0 <= interface_index < len(interfaces):
            iface = interfaces[interface_index]
            if isinstance(iface, dict):
                return iface
        for iface in interfaces:
            if isinstance(iface, dict) and int(iface.get("index", -1)) == interface_index:
                return iface
        return None

    def stop_node(self, lab_data: dict[str, Any], node_id: int) -> None:
        lab_id = self._lab_id(lab_data)
        runtime = self._runtime_record(lab_id, node_id)
        if not runtime:
            return

        self._record_transition(lab_id, node_id)
        kind = runtime.get("kind")
        if kind == "qemu":
            self._stop_qemu_runtime(runtime)
        elif kind == "docker":
            self._stop_docker_runtime(runtime)

        self._delete_runtime(lab_id, node_id)

    def wipe_node(self, lab_data: dict[str, Any], node_id: int) -> None:
        lab_id = self._lab_id(lab_data)
        self.stop_node(lab_data, node_id)

        work_dir = self._work_dir(lab_id, node_id)
        if work_dir.exists():
            shutil.rmtree(work_dir)

    def enrich_nodes(self, lab_data: dict[str, Any]) -> dict[str, Any]:
        lab_id = self._lab_id(lab_data)
        enriched: dict[str, Any] = {}
        for key, node in lab_data.get("nodes", {}).items():
            node_id = int(key)
            enriched[key] = self.enrich_node(lab_id, node_id, node)
        return enriched

    def enrich_node(self, lab_id: str, node_id: int, node: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime_record(lab_id, node_id)
        enriched = dict(node)
        enriched["status"] = 2 if runtime else 0
        enriched["url"] = self._console_url(runtime)
        if runtime:
            metrics = self._runtime_metrics(runtime)
            enriched.update(metrics)
        else:
            enriched.setdefault("cpu_usage", 0)
            enriched.setdefault("ram_usage", 0)
            enriched["disk_usage"] = self._disk_usage(self._overlay_path(lab_id, node_id))
        return enriched

    def runtime_counts(self) -> dict[str, int]:
        counts = {"qemu": 0, "docker": 0, "iol": 0, "dynamips": 0, "vpcs": 0}
        for runtime in list(self._registry.values()):
            live_runtime = self._runtime_record(runtime["lab_id"], runtime["node_id"])
            if not live_runtime:
                continue
            kind = live_runtime.get("kind")
            if kind in counts:
                counts[kind] += 1
        return counts

    def read_logs(self, lab_id: str, node_id: int, tail: int = 200) -> str:
        runtime = self._runtime_record(lab_id, node_id, include_stopped=True)
        if not runtime:
            return ""

        if runtime.get("kind") == "docker":
            return self._read_docker_logs(runtime, tail=tail)

        return self._read_qemu_logs(runtime, tail=tail)

    def console_info(self, lab_data: dict[str, Any], node_id: int, host: str = "127.0.0.1") -> dict[str, Any]:
        lab_id = self._lab_id(lab_data)
        node = self._node_data(lab_data, node_id)
        runtime = self._runtime_record(lab_id, node_id)
        if not runtime:
            raise NodeRuntimeError(f"Node is not running: {node_id}")

        if runtime.get("kind") == "docker":
            runtime = self._ensure_console_proxy(lab_id, node_id, runtime)

        return {
            "lab_id": lab_id,
            "node_id": node_id,
            "name": node.get("name", f"node-{node_id}"),
            "console": runtime.get("console", node.get("console", "telnet")),
            "host": host,
            "port": int(runtime.get("console_port", 0)),
            "url": self._console_url(runtime),
        }

    def _ensure_console_proxy(
        self, lab_id: str, node_id: int, runtime: dict[str, Any]
    ) -> dict[str, Any]:
        """Re-spawn a dead console proxy for a running Docker node.

        Checks liveness via /proc. If the container is still up but the proxy
        died, stops the stale entry, re-spawns, and persists the new PID.
        Returns the (possibly updated) runtime dict.
        """
        proxy_pid = runtime.get("console_proxy_pid")
        if proxy_pid and host_net.console_proxy_alive(int(proxy_pid)):
            return runtime

        container_pid = runtime.get("pid")
        if not container_pid:
            return runtime

        # Verify the container is still alive. Check /proc/{pid} (the directory
        # itself is always world-accessible); /proc/{pid}/ns/net requires root.
        if not Path(f"/proc/{container_pid}").is_dir():
            return runtime

        _logger.info(
            "console proxy dead or missing for lab=%s node=%s (old_pid=%s); respawning",
            lab_id,
            node_id,
            proxy_pid,
        )

        if proxy_pid:
            try:
                host_net.console_proxy_stop(int(proxy_pid))
            except Exception:
                pass

        console_mode = runtime.get("console", "telnet")
        listen_port = int(runtime.get("console_port", 0))
        if not listen_port:
            return runtime

        try:
            new_pid = host_net.console_proxy_start(
                node_pid=int(container_pid),
                listen_port=listen_port,
                target_port=self._container_console_port(console_mode),
            )
        except Exception as exc:
            _logger.warning(
                "console proxy respawn failed for lab=%s node=%s: %s",
                lab_id,
                node_id,
                exc,
            )
            return runtime

        runtime = dict(runtime)
        runtime["console_proxy_pid"] = new_pid
        key = self._key(lab_id, node_id)
        with self._lock:
            self._registry[key] = runtime
        self._persist_runtime(runtime)
        _logger.info(
            "console proxy respawned for lab=%s node=%s new_pid=%s",
            lab_id,
            node_id,
            new_pid,
        )
        return runtime

    def stream_logs(self, lab_id: str, node_id: int, tail: int = 200) -> Iterator[str]:
        runtime = self._runtime_record(lab_id, node_id, include_stopped=True)
        if not runtime:
            yield ""
            return

        if runtime.get("kind") == "docker":
            yield self._read_docker_logs(runtime, tail=tail)
            return

        log_path = Path(runtime["stdout_log"])
        if not log_path.exists():
            yield ""
            return

        yield self._read_qemu_logs(runtime, tail=tail)
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(0, os.SEEK_END)
            idle_rounds = 0
            while idle_rounds < 30:
                chunk = handle.read()
                if chunk:
                    idle_rounds = 0
                    yield chunk
                else:
                    idle_rounds += 1
                    time.sleep(0.2)

    @classmethod
    def start_heartbeat(cls) -> None:
        """Schedule _heartbeat_loop as a background asyncio task.

        Safe to call from app.startup — creates a task on the running loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(cls._heartbeat_loop())

    @classmethod
    async def _heartbeat_loop(cls) -> None:
        """Periodic reconciliation: polls live runtime state and updates lab.json."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                await cls._run_heartbeat_cycle()
            except Exception:
                _logger.exception("heartbeat cycle error")

    @classmethod
    async def _run_heartbeat_cycle(cls) -> None:
        """Single heartbeat cycle: reconcile node.status for all known runtimes."""
        from app.services.lab_service import LabService  # avoid import cycle at module load
        from app.services.lab_lock import lab_lock
        from app.services.ws_hub import ws_hub

        settings = get_settings()
        labs_dir = settings.LABS_DIR

        # Snapshot registry keys under lock; iterate without holding the lock.
        with cls._lock:
            registry_snapshot = list(cls._registry.items())

        if not registry_snapshot:
            return

        # Build lab_id -> relative filename map by scanning LABS_DIR once per cycle.
        lab_id_to_filename: dict[str, str] = {}
        if labs_dir.exists():
            for lab_file in labs_dir.rglob("*.json"):
                try:
                    raw = json.loads(lab_file.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                file_lab_id = str(raw.get("id", "")).strip()
                if file_lab_id and file_lab_id not in lab_id_to_filename:
                    try:
                        relative = lab_file.relative_to(labs_dir).as_posix()
                    except ValueError:
                        continue
                    lab_id_to_filename[file_lab_id] = relative

        for _key, runtime in registry_snapshot:
            lab_id = str(runtime.get("lab_id", "")).strip()
            node_id_raw = runtime.get("node_id")
            if not lab_id or node_id_raw is None:
                continue
            try:
                node_id = int(node_id_raw)
            except (TypeError, ValueError):
                continue

            if cls._is_suppressed(lab_id, node_id):
                continue

            filename = lab_id_to_filename.get(lab_id)
            if not filename:
                continue

            kind = runtime.get("kind", "")
            is_alive = await asyncio.get_running_loop().run_in_executor(
                None, cls._check_alive_sync, runtime, kind, settings
            )

            # status: 2 = running, 0 = stopped
            expected_status = 2 if is_alive else 0

            try:
                with lab_lock(filename, labs_dir):
                    data = LabService.read_lab_json_static(filename)
                    nodes: dict = data.get("nodes") or {}
                    node = nodes.get(str(node_id))
                    if not isinstance(node, dict):
                        continue
                    current_status = int(node.get("status", -1))
                    if current_status == expected_status:
                        continue
                    node["status"] = expected_status
                    LabService.write_lab_json_static(filename, data)
            except Exception:
                _logger.exception(
                    "heartbeat: failed to reconcile lab=%s node=%s", lab_id, node_id
                )
                continue

            _logger.info(
                "heartbeat reconciled: lab=%s node=%s status %d -> %d",
                lab_id, node_id, current_status, expected_status,
            )

            # If the process is dead according to reality, clean up the registry entry.
            if not is_alive:
                cls._delete_runtime_class(lab_id, node_id, settings)

            try:
                await ws_hub.publish(
                    lab_id,
                    "node_status_reconciled",
                    {"node_id": node_id, "status": expected_status},
                    rev=str(lab_id),
                )
            except Exception:
                _logger.exception(
                    "heartbeat: ws publish failed for lab=%s node=%s", lab_id, node_id
                )

    # ------------------------------------------------------------------
    # US-402 — Discovery cadence
    # ------------------------------------------------------------------
    @classmethod
    def start_discovery(cls) -> None:
        """Schedule ``_discovery_loop`` as a background asyncio task.

        Mirrors :meth:`start_heartbeat` — safe to call from app startup.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(cls._discovery_loop())

    @classmethod
    async def _discovery_loop(cls) -> None:
        """Periodic discovery: walks kernel-side bridge state and emits
        ``discovered_link`` WS events for any bridge member that has no
        matching entry in ``links[]``.

        Cadence is read from ``get_settings().DISCOVERY_CADENCE_SECONDS``
        on every iteration so live config edits land within one cycle
        (operators clear the ``get_settings`` lru_cache via reload).
        """
        while True:
            cadence = max(5, int(get_settings().DISCOVERY_CADENCE_SECONDS))
            await asyncio.sleep(cadence)
            try:
                await cls._run_discovery_cycle()
            except Exception:
                _discovery_logger.exception("discovery cycle error")

    @classmethod
    async def _run_discovery_cycle(cls) -> None:
        """Single discovery cycle: scan every lab.json's network bridges,
        compare against ``links[]``, and publish ``discovered_link`` events
        for kernel-side members with no declared link.
        """
        from app.services.lab_service import LabService  # avoid cycles
        from app.services.ws_hub import ws_hub

        settings = get_settings()
        labs_dir = settings.LABS_DIR
        if not labs_dir.exists():
            return

        for lab_file in labs_dir.rglob("*.json"):
            try:
                data = json.loads(lab_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            lab_id = str(data.get("id", "")).strip()
            if not lab_id:
                continue
            try:
                await cls._discover_lab(lab_id, data, ws_hub)
            except Exception:
                _discovery_logger.exception(
                    "discovery: failed lab=%s", lab_id
                )

    @classmethod
    async def _discover_lab(
        cls, lab_id: str, lab_data: dict[str, Any], ws_hub: Any
    ) -> None:
        """Scan bridges for one lab and publish ``discovered_link`` /
        ``link_divergent`` WS events.

        - ``discovered_link``: kernel-side bridge member with no matching
          ``links[]`` entry (US-402).
        - ``link_divergent``: ``links[]`` entry whose declared veth/TAP is
          NOT present on any of this lab's bridges (US-404).

        Lease respect: links currently in a transition lease (in-flight
        hot-plug from US-204b) are NOT flagged.  We import
        ``transition_lease`` lazily so this module remains importable
        before US-204b lands; missing module is treated as "no leases".

        Generation token (US-403 MEDIUM-3): increment the per-lab counter at
        the very start of every call.  Every event emitted within this call
        carries the same ``generation`` value.  The frontend rejects events
        whose ``generation`` is <= the generation recorded in the last
        ``lab_topology`` snapshot — this is the producer-side guarantee that
        stale events from a previous cycle are never silently accepted.
        """
        gen = cls._discovery_generation.get(lab_id, 0) + 1
        cls._discovery_generation[lab_id] = gen

        networks = lab_data.get("networks") or {}
        if not isinstance(networks, dict) or not networks:
            return

        # Build the set of "declared" host-side ifaces per (network, iface).
        # An entry in links[] like {from: {node_id, interface_index},
        # to: {network_id}} corresponds to either a veth host-end or a TAP
        # — both share the prefix nve{hash}d{node_id}i{iface}.
        declared = cls._declared_iface_set(lab_id, lab_data)

        # Optional lease check (US-204b).  When the module isn't present
        # we degrade to "no leases" rather than failing the cycle.
        try:
            from app.services import transition_lease  # type: ignore
            is_leased = getattr(transition_lease, "is_leased", None)
        except Exception:
            is_leased = None

        # Aggregate kernel-side bridge members across this lab so the
        # divergent-link check (US-404) below can answer "does the kernel
        # have any veth/TAP for this declared link?" without a second
        # bridge-scan pass.
        kernel_members: set[str] = set()

        for network_id_raw, network in networks.items():
            if not isinstance(network, dict):
                continue
            try:
                network_id = int(network_id_raw)
            except (TypeError, ValueError):
                continue

            runtime = network.get("runtime") or {}
            bridge = runtime.get("bridge_name") if isinstance(runtime, dict) else None
            if not bridge:
                # US-401 may not have populated this yet — derive on the fly.
                try:
                    bridge = host_net.bridge_name(lab_id, network_id)
                except Exception:
                    continue

            try:
                members = await asyncio.get_running_loop().run_in_executor(
                    None, cls._bridge_members_sync, bridge
                )
            except Exception:
                _discovery_logger.exception(
                    "discovery: bridge_link failed bridge=%s", bridge
                )
                continue

            kernel_members.update(members)

            for iface in members:
                if iface in declared:
                    continue
                # Decode the peer node from the iface name when possible so
                # the frontend can render a hint.  Failures degrade to None.
                peer_node_id = cls._parse_iface_node_id(iface)
                # Lease check — skip if a hot-plug for this (lab, link) is
                # in flight.  We don't always have a link_id here; pass the
                # iface name as the lease key for forward-compatibility.
                if is_leased is not None:
                    try:
                        if is_leased(lab_id=lab_id, link_id=iface):
                            continue
                    except Exception:
                        pass
                payload = {
                    "lab_id": lab_id,
                    "network_id": network_id,
                    "bridge_name": bridge,
                    "iface": iface,
                    "peer_node_id": peer_node_id,
                    "generation": gen,
                }
                try:
                    await ws_hub.publish(lab_id, "discovered_link", payload)
                except Exception:
                    _discovery_logger.exception(
                        "discovery: ws publish failed lab=%s iface=%s",
                        lab_id, iface,
                    )

        # ------------------------------------------------------------------
        # US-404 — Divergent links (declared in lab.json but kernel says no)
        # ------------------------------------------------------------------
        await cls._publish_divergent_links(
            lab_id=lab_id,
            lab_data=lab_data,
            kernel_members=kernel_members,
            is_leased=is_leased,
            ws_hub=ws_hub,
            generation=gen,
        )

    @classmethod
    async def _publish_divergent_links(
        cls,
        *,
        lab_id: str,
        lab_data: dict[str, Any],
        kernel_members: set[str],
        is_leased: Callable[..., bool] | None,
        ws_hub: Any,
        generation: int = 0,
    ) -> None:
        """For each entry in ``links[]`` whose declared veth/TAP host-side
        name is missing from ``kernel_members``, publish a
        ``link_divergent`` WS event with payload ``{link_id, lab_id, reason,
        last_checked}``.

        Lease respect: a link currently held by ``transition_lease.is_leased``
        is in the middle of a hot-plug operation and MUST NOT be flagged
        divergent.
        """
        links = lab_data.get("links") or []
        if not isinstance(links, list) or not links:
            return

        from datetime import datetime, timezone

        last_checked = datetime.now(timezone.utc).isoformat()

        for link in links:
            if not isinstance(link, dict):
                continue
            link_id = link.get("id")
            if not isinstance(link_id, str) or not link_id:
                continue
            src = link.get("from") or {}
            if not isinstance(src, dict):
                continue
            node_id_raw = src.get("node_id")
            iface_idx_raw = src.get("interface_index")
            if node_id_raw is None or iface_idx_raw is None:
                continue
            try:
                node_id = int(node_id_raw)
                iface_idx = int(iface_idx_raw)
            except (TypeError, ValueError):
                continue

            try:
                veth_iface = host_net.veth_host_name(lab_id, node_id, iface_idx)
                tap_iface = host_net.tap_name(lab_id, node_id, iface_idx)
            except Exception:
                continue

            # If either the veth host-end OR the TAP exists on any bridge
            # this lab owns, the kernel state is consistent — not divergent.
            if veth_iface in kernel_members or tap_iface in kernel_members:
                continue

            # Lease respect — skip if a hot-plug is currently in flight for
            # this link.  We probe by both the link_id and the iface names
            # so leases registered under either key are honored.
            if is_leased is not None:
                leased = False
                for lease_key in (link_id, veth_iface, tap_iface):
                    try:
                        if is_leased(lab_id=lab_id, link_id=lease_key):
                            leased = True
                            break
                    except Exception:
                        continue
                if leased:
                    continue

            payload = {
                "link_id": link_id,
                "lab_id": lab_id,
                "reason": "declared in lab.json but no matching veth/TAP found in kernel",
                "last_checked": last_checked,
                "generation": generation,
            }
            try:
                await ws_hub.publish(lab_id, "link_divergent", payload)
            except Exception:
                _discovery_logger.exception(
                    "discovery: ws publish failed lab=%s link_id=%s",
                    lab_id, link_id,
                )

    @staticmethod
    def _declared_iface_set(lab_id: str, lab_data: dict[str, Any]) -> set[str]:
        """Return the set of host-side iface names that ARE declared in
        ``links[]`` — the discovery loop ignores these.

        Each link from a node to a network corresponds to either
        ``veth_host_name`` (docker) or ``tap_name`` (qemu); both share
        the same prefix so we record both candidates and let the diff
        match either form.
        """
        declared: set[str] = set()
        nodes = lab_data.get("nodes") or {}
        for link in lab_data.get("links") or []:
            if not isinstance(link, dict):
                continue
            src = link.get("from") or {}
            if not isinstance(src, dict):
                continue
            node_id_raw = src.get("node_id")
            iface_idx_raw = src.get("interface_index")
            if node_id_raw is None or iface_idx_raw is None:
                continue
            try:
                node_id = int(node_id_raw)
                iface_idx = int(iface_idx_raw)
            except (TypeError, ValueError):
                continue
            try:
                declared.add(host_net.veth_host_name(lab_id, node_id, iface_idx))
                declared.add(host_net.tap_name(lab_id, node_id, iface_idx))
            except Exception:
                continue
        # Guard against unused-arg warnings when ``nodes`` is empty.
        _ = nodes
        return declared

    @staticmethod
    def _bridge_members_sync(bridge: str) -> list[str]:
        """Run ``bridge link show master <bridge>`` and return member names.

        Returns ``[]`` on any failure so the discovery cycle never blocks.
        Lines look like ``3: nve12abd1i0h@if4: <BROADCAST,...> master noveXn1``.
        """
        bridge_bin = shutil.which("bridge") or "/usr/sbin/bridge"
        try:
            proc = subprocess.run(
                [bridge_bin, "link", "show", "master", bridge],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        names: list[str] = []
        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            raw = parts[1].strip().split("@")[0]
            if raw:
                names.append(raw)
        return names

    @staticmethod
    def _parse_iface_node_id(iface: str) -> int | None:
        """Decode the ``node_id`` from an ``nve<hash>d<node>i<iface>...`` name.

        Returns ``None`` for any name that doesn't match the nova-ve scheme
        (e.g. an unrelated kernel-side interface attached to the bridge).
        """
        # Format: nve{4hex}d{node}i{iface}[h|p]
        if not iface.startswith("nve") or "d" not in iface or "i" not in iface:
            return None
        try:
            after_d = iface.split("d", 1)[1]
            node_part = after_d.split("i", 1)[0]
            return int(node_part)
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _check_alive_sync(runtime: dict[str, Any], kind: str, settings: Any) -> bool:
        """Synchronous liveness check suitable for run_in_executor."""
        if kind == "docker":
            import subprocess as _sp
            import shutil as _sh
            docker_binary = _sh.which("docker")
            if not docker_binary:
                docker_binary = "docker"
            container_name = runtime.get("container_name")
            if not container_name:
                return False
            result = _sp.run(
                [
                    docker_binary,
                    "--host",
                    settings.DOCKER_HOST,
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    container_name,
                ],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"

        # QEMU / other — use psutil
        pid = runtime.get("pid")
        if not pid:
            return False
        try:
            process = psutil.Process(int(pid))
        except psutil.Error:
            return False
        expected_create_time = runtime.get("pid_create_time")
        if expected_create_time and abs(process.create_time() - expected_create_time) > 1:
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE

    @classmethod
    def _delete_runtime_class(cls, lab_id: str, node_id: int, settings: Any) -> None:
        """Remove registry entry and state file (class-level, no instance needed)."""
        key = f"{lab_id}:{node_id}"
        with cls._lock:
            cls._registry.pop(key, None)
        runtime_dir = settings.TMP_DIR / "node-runtime"
        state_path = runtime_dir / f"{lab_id}-{node_id}.json"
        try:
            if state_path.exists():
                state_path.unlink()
        except OSError:
            pass

    def _load_registry(self) -> None:
        with self._lock:
            if self._loaded:
                return
            for state_file in self.runtime_dir.glob("*.json"):
                try:
                    runtime = json.loads(state_file.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                key = self._key(runtime["lab_id"], runtime["node_id"])
                self._registry[key] = runtime
            self._loaded = True

    def _persist_runtime(self, runtime: dict[str, Any]) -> None:
        self._state_path(runtime["lab_id"], runtime["node_id"]).write_text(
            json.dumps(runtime, indent=2)
        )

    def _delete_runtime(self, lab_id: str, node_id: int) -> None:
        key = self._key(lab_id, node_id)
        with self._lock:
            self._registry.pop(key, None)
        state_path = self._state_path(lab_id, node_id)
        if state_path.exists():
            state_path.unlink()

    def _runtime_record(
        self,
        lab_id: str,
        node_id: int,
        include_stopped: bool = False,
    ) -> dict[str, Any] | None:
        key = self._key(lab_id, node_id)
        runtime = self._registry.get(key)
        if not runtime:
            state_path = self._state_path(lab_id, node_id)
            if state_path.exists():
                runtime = json.loads(state_path.read_text())
                with self._lock:
                    self._registry[key] = runtime

        if not runtime:
            return None

        if include_stopped:
            return runtime

        if not self._is_runtime_alive(runtime):
            self._delete_runtime(lab_id, node_id)
            return None
        return runtime

    def _is_runtime_alive(self, runtime: dict[str, Any]) -> bool:
        if runtime.get("kind") == "docker":
            return self._is_docker_running(runtime)

        pid = runtime.get("pid")
        if not pid:
            return False

        try:
            process = psutil.Process(pid)
        except psutil.Error:
            return False

        expected_create_time = runtime.get("pid_create_time")
        if expected_create_time and abs(process.create_time() - expected_create_time) > 1:
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE

    def _start_qemu_node(
        self, lab_data: dict[str, Any], node: dict[str, Any]
    ) -> dict[str, Any]:
        extras = _node_extras(node)
        architecture = _extra_str(extras, "architecture") or "x86_64"
        qemu_binary = self._resolve_qemu_binary(architecture)
        if not qemu_binary:
            raise NodeRuntimeError(
                f"QEMU binary not found for arch {architecture}: {self.settings.QEMU_BINARY}"
            )

        lab_id = self._lab_id(lab_data)
        node_id = int(node["id"])
        machine, max_nics, hotplug_capable = self._resolve_qemu_machine(node)

        # US-302: per-NIC TAP attachments. Pre-flight every declared
        # interface's bridge BEFORE we spawn QEMU so a missing bridge
        # surfaces a typed NodeRuntimeError instead of leaking a half-
        # started VM. SLIRP is opt-in via per-interface ``extras.uplink``.
        attachments = self._qemu_attachments(lab_data, node)
        for attachment in attachments:
            bridge = attachment["bridge_name"]
            if not host_net.bridge_exists(bridge):
                raise NodeRuntimeError(
                    f"Bridge {bridge} for network_id={attachment['network_id']} "
                    f"is not present on the host; provision it via create_network "
                    f"(US-202) before starting the node."
                )

        work_dir = self._work_dir(lab_id, node_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        overlay_path = self._ensure_qemu_overlay(work_dir, node)
        iso_path = self._resolve_qemu_iso(node)
        stdout_log = work_dir / "stdout.log"
        stderr_log = work_dir / "stderr.log"
        console_mode = node.get("console", "telnet")
        console_port = self._allocate_console_port(console_mode)
        accel = "kvm" if Path("/dev/kvm").exists() else "tcg"

        cmd = [
            qemu_binary,
            "-display",
            "none",
            "-machine",
            f"type={machine},accel={accel}",
            "-smp",
            str(node.get("cpu", 1)),
            "-m",
            str(node.get("ram", 1024)),
            "-name",
            str(node.get("name", f"node-{node_id}")),
            "-uuid",
            str(node.get("uuid") or extras.get("uuid") or f"{lab_id}-{node_id}"),
            "-drive",
            f"file={overlay_path},if=virtio,cache=writeback,format=qcow2",
        ]

        if accel == "kvm":
            cmd += ["-cpu", "host,vmx=off,svm=off"]
        else:
            cmd += ["-cpu", "max"]

        if console_mode == "vnc":
            cmd += ["-vnc", f":{console_port - 5900}"]
        else:
            cmd += ["-serial", f"telnet::{console_port},server,nowait"]

        qmp_socket_path = work_dir / "qmp.sock"
        cmd += ["-qmp", f"unix:{qmp_socket_path},server,nowait"]

        # US-301: q35 pre-allocates pcie-root-port chassis for hot-plug.
        # Slot 0 is reserved on q35 (root complex); first usable slot is 1.
        allocated_slots: list[int] = []
        if machine == "q35" and max_nics > 0:
            for i in range(max_nics):
                slot = i + 1
                cmd += [
                    "-device",
                    f"pcie-root-port,id=rp{i},chassis={slot},slot={slot}",
                ]
                allocated_slots.append(i)

        nic_model = _extra_str(extras, "qemu_nic") or "e1000"
        first_mac = node.get("firstmac") or extras.get("firstmac")
        attachment_by_index: dict[int, dict[str, Any]] = {
            int(a["interface_index"]): a for a in attachments
        }
        node_interfaces = node.get("interfaces") or []
        ethernet_count = int(node.get("ethernet", 0))

        # US-302: provision TAPs BEFORE spawning QEMU. Track every TAP we
        # successfully created so a partial-failure path can sweep them.
        provisioned_taps: list[str] = []
        try:
            tap_names: dict[int, str] = {}
            for index in range(ethernet_count):
                if index in attachment_by_index:
                    tap = host_net.tap_name(lab_id, node_id, index)
                    bridge = attachment_by_index[index]["bridge_name"]
                    if not host_net.tap_exists(tap):
                        host_net.tap_add(tap)
                        provisioned_taps.append(tap)
                    host_net.link_master(tap, bridge)
                    host_net.link_up(tap)
                    tap_names[index] = tap
        except Exception:
            for tap in provisioned_taps:
                host_net.try_link_del(tap)
            raise

        # ----- Build per-NIC -netdev / -device argv ------------------------
        unconnected_nic_indices: list[int] = []
        for index in range(ethernet_count):
            device_args = (
                f"{nic_model},netdev=net{index},mac={self._mac_for_index(first_mac, index)}"
            )
            if machine == "q35" and index < max_nics:
                device_args += f",bus=rp{index}"

            if index in tap_names:
                tap = tap_names[index]
                cmd += [
                    "-netdev",
                    f"tap,id=net{index},ifname={tap},script=no,downscript=no",
                    "-device",
                    device_args,
                ]
            elif self._interface_uplink(node_interfaces, index):
                # SLIRP opt-in (extras.uplink: true) — gives the NIC NAT
                # access to the host's outbound network without a bridge.
                cmd += [
                    "-netdev",
                    f"user,id=net{index}",
                    "-device",
                    device_args,
                ]
            else:
                # No network attached and no uplink request. The carrier
                # is forced off via QMP ``set_link`` AFTER spawn — e1000
                # (and most other NIC models) reject ``link=`` as a
                # device-line option, so the boot-time argv stays plain
                # and the post-spawn step pins these to ``up=False``.
                cmd += [
                    "-netdev",
                    f"hubport,id=net{index},hubid={index}",
                    "-device",
                    device_args,
                ]
                unconnected_nic_indices.append(index)

        boot_order = _extra_str(extras, "boot_order")
        if iso_path:
            cmd += ["-cdrom", str(iso_path)]
        if boot_order or iso_path:
            effective_order = boot_order or "cd"
            cmd += ["-boot", f"order={effective_order}"]

        extra_args = _extra_str(extras, "qemu_options")
        try:
            cmd = _merge_qemu_args(cmd, extra_args)
        except ValueError as exc:
            # Sweep TAPs we already created so the lab does not leak
            # kernel objects when arg parsing rejects the launch.
            for tap in provisioned_taps:
                host_net.try_link_del(tap)
            raise NodeRuntimeError(f"Invalid qemu_options: {exc}") from exc

        try:
            with stdout_log.open("ab") as stdout_handle, stderr_log.open("ab") as stderr_handle:
                process = subprocess.Popen(
                    cmd,
                    cwd=work_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
        except Exception:
            for tap in provisioned_taps:
                host_net.try_link_del(tap)
            raise

        time.sleep(0.1)
        if process.poll() is not None:
            error = self._tail_text(stderr_log, 40) or self._tail_text(stdout_log, 40)
            for tap in provisioned_taps:
                host_net.try_link_del(tap)
            raise NodeRuntimeError(error or "QEMU exited immediately after start")

        process_info = psutil.Process(process.pid)

        # Pin the carrier OFF on every unconnected NIC index. QMP may
        # not be bound the very first ms after spawn, so we retry each
        # call once on transport failure. Best-effort: if QMP still
        # rejects, the next ``ensure_lab_bridges`` reconcile catches it.
        if unconnected_nic_indices:
            qmp_path = str(qmp_socket_path)
            for unconnected_index in unconnected_nic_indices:
                args = {"name": f"net{unconnected_index}", "up": False}
                for attempt in range(2):
                    try:
                        self._qmp_command(qmp_path, "set_link", args)
                        break
                    except Exception:  # noqa: BLE001 — best-effort
                        if attempt == 0:
                            time.sleep(0.1)
                        # else: give up, reconcile will retry.

        # US-201/US-203: register the PID into the runtime registry. On
        # registry failure we kill the QEMU process and sweep the TAPs to
        # keep the rollback symmetric with the docker start path
        # (``_start_docker_node`` step 4).
        try:
            runtime_pids.register(process.pid, "qemu", lab_id, node_id)
        except Exception as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for tap in provisioned_taps:
                host_net.try_link_del(tap)
            raise NodeRuntimeError(
                f"Failed to register QEMU PID in runtime registry: {exc}"
            ) from exc

        return {
            "lab_id": lab_id,
            "node_id": node_id,
            "kind": "qemu",
            "name": node.get("name"),
            "console": console_mode,
            "console_port": console_port,
            "pid": process.pid,
            "pid_create_time": process_info.create_time(),
            "overlay_path": str(overlay_path),
            "cdrom_path": str(iso_path) if iso_path else None,
            "work_dir": str(work_dir),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "qmp_socket": str(qmp_socket_path),
            "command": cmd,
            "machine": machine,
            "max_nics": max_nics,
            "hotplug_capable": hotplug_capable,
            "allocated_slots": allocated_slots,
            "tap_names": list(provisioned_taps),
            "interface_attachments": [
                {
                    "interface_index": a["interface_index"],
                    "network_id": a["network_id"],
                    "bridge_name": a["bridge_name"],
                    "tap_name": tap_names.get(int(a["interface_index"])),
                }
                for a in attachments
            ],
            "started_at": time.time(),
        }

    def qemu_command_preview(
        self, lab_data: dict[str, Any], node_id: int
    ) -> dict[str, Any]:
        """Return an annotated token list for the QEMU command of *node_id*.

        Running nodes: tokens come from the recorded command and are all
        marked ``"actual"`` (authoritative from the live process).

        Stopped nodes: performs a dry-run command assembly (no TAPs created,
        placeholder paths for overlay/sockets) and marks each token as
        ``"default"`` or ``"user"`` depending on whether the token's flag was
        supplied in ``extras.qemu_options``.
        """
        node = (lab_data.get("nodes") or {}).get(str(node_id))
        if node is None:
            raise NodeRuntimeError(f"Node {node_id} not found in lab.")
        if node.get("type") not in ("qemu",):
            raise NodeRuntimeError("QEMU command preview is only available for QEMU nodes.")

        lab_id = self._lab_id(lab_data)
        runtime = self._runtime_record(lab_id, node_id, include_stopped=False)

        if runtime and runtime.get("kind") == "qemu":
            actual_cmd: list[str] = runtime.get("command") or []
            return {
                "tokens": [{"token": tok, "source": "actual"} for tok in actual_cmd],
                "running": True,
            }

        # ── Stopped node: dry-run ─────────────────────────────────────────
        extras = _node_extras(node)
        architecture = _extra_str(extras, "architecture") or "x86_64"
        qemu_binary = self._resolve_qemu_binary(architecture) or f"qemu-system-{architecture}"
        machine, max_nics, _ = self._resolve_qemu_machine(node)
        accel = "kvm" if Path("/dev/kvm").exists() else "tcg"
        console_mode = node.get("console", "telnet")

        overlay_ph = "<work-dir>/overlay.qcow2"
        qmp_ph = "<work-dir>/qmp.sock"
        console_port_ph = 5900

        cmd: list[str] = [
            qemu_binary,
            "-display", "none",
            "-machine", f"type={machine},accel={accel}",
            "-smp", str(node.get("cpu", 1)),
            "-m", str(node.get("ram", 1024)),
            "-name", str(node.get("name", f"node-{node_id}")),
            "-uuid", str(node.get("uuid") or extras.get("uuid") or f"{lab_id}-{node_id}"),
            "-drive", f"file={overlay_ph},if=virtio,cache=writeback,format=qcow2",
        ]
        cmd += ["-cpu", "host,vmx=off,svm=off"] if accel == "kvm" else ["-cpu", "max"]
        if console_mode == "vnc":
            cmd += ["-vnc", f":{console_port_ph - 5900}"]
        else:
            cmd += ["-serial", f"telnet::{console_port_ph},server,nowait"]
        cmd += ["-qmp", f"unix:{qmp_ph},server,nowait"]

        if machine == "q35" and max_nics > 0:
            for i in range(max_nics):
                slot = i + 1
                cmd += ["-device", f"pcie-root-port,id=rp{i},chassis={slot},slot={slot}"]

        nic_model = _extra_str(extras, "qemu_nic") or "e1000"
        first_mac = node.get("firstmac") or extras.get("firstmac")
        ethernet_count = int(node.get("ethernet", 0))
        node_interfaces = node.get("interfaces") or []
        attachments = self._qemu_attachments(lab_data, node)
        attachment_by_index: dict[int, dict[str, Any]] = {
            int(a["interface_index"]): a for a in attachments
        }
        for index in range(ethernet_count):
            tap_name = host_net.tap_name(lab_id, node_id, index)
            device_args = (
                f"{nic_model},netdev=net{index},mac={self._mac_for_index(first_mac, index)}"
            )
            if machine == "q35" and index < max_nics:
                device_args += f",bus=rp{index}"
            if index in attachment_by_index:
                cmd += [
                    "-netdev", f"tap,id=net{index},ifname={tap_name},script=no,downscript=no",
                    "-device", device_args,
                ]
            elif self._interface_uplink(node_interfaces, index):
                cmd += ["-netdev", f"user,id=net{index}", "-device", device_args]
            else:
                cmd += ["-netdev", f"hubport,id=net{index},hubid={index}", "-device", device_args]

        boot_order_str = _extra_str(extras, "boot_order")
        iso_path = self._resolve_qemu_iso(node)
        if iso_path:
            cmd += ["-cdrom", str(iso_path)]
        if boot_order_str or iso_path:
            cmd += ["-boot", f"order={boot_order_str or 'cd'}"]

        extra_args = _extra_str(extras, "qemu_options")
        try:
            merged_cmd = _merge_qemu_args(cmd, extra_args)
        except ValueError as exc:
            raise NodeRuntimeError(f"Invalid qemu_options: {exc}") from exc

        user_flags: set[str] = set()
        if extra_args:
            try:
                for tok in shlex.split(extra_args):
                    if tok.startswith("-"):
                        user_flags.add(tok)
            except ValueError:
                pass

        tokens: list[dict[str, str]] = []
        current_flag: str | None = None
        for tok in merged_cmd:
            if tok.startswith("-"):
                current_flag = tok
            source = "user" if current_flag in user_flags else "default"
            tokens.append({"token": tok, "source": source})

        return {"tokens": tokens, "running": False}

    @staticmethod
    def _interface_uplink(interfaces: list[Any], interface_index: int) -> bool:
        """Return True iff the interface's ``extras.uplink`` flag is set.

        SLIRP/user-mode networking is opt-in per interface (US-302). When
        the interface entry has no explicit network attachment, we keep
        the legacy ``-netdev user`` only when the operator has marked the
        interface as an uplink — otherwise the NIC is wired into an
        isolated socket netdev to keep the guest from leaking onto the
        host's outbound network.
        """
        for entry in interfaces or []:
            if not isinstance(entry, dict):
                continue
            if int(entry.get("index", -1)) == int(interface_index):
                extras = entry.get("extras") or {}
                if isinstance(extras, dict) and bool(extras.get("uplink")):
                    return True
                return False
        return False

    def _qemu_attachments(
        self, lab_data: dict[str, Any], node: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Resolve per-interface bridge attachments for a QEMU node.

        Mirrors :meth:`_docker_attachments`: returns
        ``{interface_index, network_id, bridge_name}`` records ordered by
        ``interface_index``. Interfaces with no resolvable network (no
        link, ``pnet`` external network, or missing network record) are
        skipped — those NICs fall back to the SLIRP/opt-in uplink path.
        """
        lab_id = self._lab_id(lab_data)
        networks = lab_data.get("networks", {}) or {}

        node_id = int(node.get("id", 0))
        link_map: dict[int, int] = {}
        for link in lab_data.get("links", []) or []:
            endpoints = (link.get("from") or {}, link.get("to") or {})
            node_endpoint = next(
                (
                    endpoint for endpoint in endpoints
                    if isinstance(endpoint, dict)
                    and "node_id" in endpoint
                    and int(endpoint.get("node_id", -1)) == node_id
                ),
                None,
            )
            network_endpoint = next(
                (
                    endpoint for endpoint in endpoints
                    if isinstance(endpoint, dict) and "network_id" in endpoint
                ),
                None,
            )
            if node_endpoint and network_endpoint:
                interface_index = int(node_endpoint.get("interface_index", 0))
                network_id = int(network_endpoint.get("network_id", 0))
                if network_id:
                    link_map[interface_index] = network_id

        attachments: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        interfaces = node.get("interfaces") or []
        ethernet_count = int(node.get("ethernet", 0))
        for index, interface in enumerate(interfaces):
            if not isinstance(interface, dict):
                continue
            interface_index = int(interface.get("index", index))
            if interface_index in seen_indices:
                continue
            network_id = int(interface.get("network_id") or 0)
            if not network_id:
                network_id = link_map.get(interface_index, 0)
            if not network_id:
                continue
            network = networks.get(str(network_id))
            if not isinstance(network, dict):
                continue
            network_type = str(network.get("type", "linux_bridge"))
            if network_type.startswith("pnet"):
                continue
            runtime_record = network.get("runtime") or {}
            bridge = runtime_record.get("bridge_name")
            if not bridge:
                bridge = host_net.bridge_name(lab_id, network_id)
            seen_indices.add(interface_index)
            attachments.append(
                {
                    "interface_index": interface_index,
                    "network_id": network_id,
                    "bridge_name": bridge,
                }
            )

        # Also pick up interfaces that exist only via a `links[]` entry
        # (no explicit ``interfaces[]`` record) — the QEMU NIC count is
        # driven by ``ethernet`` and these still need a TAP.
        for interface_index, network_id in link_map.items():
            if interface_index in seen_indices:
                continue
            if interface_index >= ethernet_count:
                continue
            network = networks.get(str(network_id))
            if not isinstance(network, dict):
                continue
            network_type = str(network.get("type", "linux_bridge"))
            if network_type.startswith("pnet"):
                continue
            runtime_record = network.get("runtime") or {}
            bridge = runtime_record.get("bridge_name")
            if not bridge:
                bridge = host_net.bridge_name(lab_id, network_id)
            seen_indices.add(interface_index)
            attachments.append(
                {
                    "interface_index": interface_index,
                    "network_id": network_id,
                    "bridge_name": bridge,
                }
            )

        attachments.sort(key=lambda item: item["interface_index"])
        return attachments

    def _resolve_qemu_machine(self, node: dict[str, Any]) -> tuple[str, int, bool]:
        """Resolve machine type, max_nics, and hotplug capability for a QEMU node.

        Resolution chain (US-301):
        1. ``node.machine_override`` (set by US-202b migration on pre-Wave-7 nodes
           or by an operator opt-in flow) — wins unconditionally.
        2. ``template.capabilities.machine`` from the YAML template — new
           templates default to ``q35``; legacy YAMLs default to ``pc`` via
           ``_default_capabilities`` inferred defaults.
        3. Final fallback ``pc`` if no template can be resolved.

        Returns ``(machine, max_nics, hotplug_capable)``. ``max_nics`` is read
        from the same template's ``capabilities.max_nics`` (default 8).
        """
        override = node.get("machine_override")
        capabilities: dict[str, Any] = {}
        try:
            from app.services.template_service import (  # local to avoid cycles
                TemplateError,
                TemplateService,
            )

            template_key = str(node.get("template") or "").strip()
            if template_key:
                try:
                    template = TemplateService().get_template("qemu", template_key)
                    capabilities = dict(template.capabilities or {})
                except TemplateError:
                    capabilities = {}
        except ImportError:
            capabilities = {}

        if isinstance(override, str) and override in ("pc", "q35"):
            machine = override
        else:
            template_machine = capabilities.get("machine")
            if isinstance(template_machine, str) and template_machine in ("pc", "q35"):
                machine = template_machine
            else:
                machine = "pc"

        max_nics_value = capabilities.get("max_nics", 8)
        try:
            max_nics = int(max_nics_value)
        except (TypeError, ValueError):
            max_nics = 8
        if max_nics < 1:
            max_nics = 8

        hotplug_capable = bool(capabilities.get("hotplug", False)) and machine == "q35"
        return machine, max_nics, hotplug_capable

    def _start_docker_node(self, lab_data: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        """US-203: containers always start with ``--network=none``.

        Sequence (rollback-safe):

          1. Pre-flight: confirm every declared network's bridge exists on
             the host (``host_net.bridge_exists``).
          2. ``docker run --network=none ...`` — no Docker-managed network.
          3. ``docker inspect`` to get the container PID.
          4. Register the PID into ``/var/lib/nova-ve/runtime/pids.json``
             via ``runtime_pids.register`` BEFORE any helper-verb call
             (US-201 sequencing contract).
          5. For each declared interface, attach manually via the privileged
             helper: ``veth_pair_add`` → ``link_master`` (host end ↔ bridge)
             → ``link_up`` (host end) → ``link_netns`` (peer → container)
             → ``link_set_name_in_netns`` (rename peer to ``eth{iface}``)
             → ``addr_up_in_netns`` (bring ``eth{iface}`` up).
          6. On any failure mid-sequence: roll back to a clean state
             (``try_link_del`` host-ends, ``docker rm -f`` the container,
             ``runtime_pids.unregister``) and raise ``NodeRuntimeError``.

        Docker plays NO role in networking — no ``docker network create``,
        no ``docker network connect``, no ``--network-alias``.
        """
        docker_binary = self._resolve_binary("docker")
        if not docker_binary:
            raise NodeRuntimeError("Docker binary not found")

        lab_id = self._lab_id(lab_data)
        console_mode = node.get("console", "rdp")
        console_port = self._allocate_console_port(console_mode)
        container_name = self._container_name(lab_id, node["id"])
        network_specs = self._docker_network_specs(lab_data, node)
        extras = _node_extras(node)
        node_id = int(node["id"])

        # ----- Step 1: pre-flight bridge presence check ---------------------
        # Every declared network must have its bridge present on the host
        # before we start the container. This catches the case where US-202
        # has not yet provisioned the bridge (e.g. cold lab.json without
        # `runtime.bridge_name`) and surfaces a typed error rather than
        # crashing later in the helper-verb sequence.
        attachments: list[dict[str, Any]] = self._docker_attachments(lab_data, node)
        for attachment in attachments:
            bridge = attachment["bridge_name"]
            if not host_net.bridge_exists(bridge):
                raise NodeRuntimeError(
                    f"Bridge {bridge} for network_id={attachment['network_id']} "
                    f"is not present on the host; provision it via create_network "
                    f"(US-202) before starting the node."
                )

        # ----- Build the docker run command (no networking flags) ----------
        cmd = [
            docker_binary,
            "--host",
            self.settings.DOCKER_HOST,
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "--cpus",
            str(node.get("cpu", 1)),
            "--memory",
            f"{node.get('ram', 1024)}m",
            "--hostname",
            self._docker_network_alias(node),
            "--network",
            "none",
            "--privileged",
            "-p",
            f"{console_port}:{self._container_console_port(console_mode)}",
        ]

        restart_policy = _extra_str(extras, "restart_policy")
        if restart_policy and restart_policy != "no":
            if restart_policy not in _DOCKER_RESTART_POLICIES:
                raise NodeRuntimeError(f"Invalid restart_policy: {restart_policy}")
            cmd += ["--restart", restart_policy]

        for entry in extras.get("environment") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key", "")).strip()
            if not key:
                continue
            value = str(entry.get("value", ""))
            cmd += ["-e", f"{key}={value}"]

        extra_args = _extra_str(extras, "extra_args")
        if extra_args:
            try:
                cmd += shlex.split(extra_args)
            except ValueError as exc:
                raise NodeRuntimeError(f"Invalid extra_args: {exc}") from exc

        cmd += [str(node.get("image"))]

        # ----- Step 2: docker run -d --network=none ------------------------
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise NodeRuntimeError(
                result.stderr.strip()
                or result.stdout.strip()
                or "Failed to start Docker container"
            )

        # ----- Step 3: resolve PID via docker inspect ----------------------
        pid = self._docker_container_pid(docker_binary, container_name)
        if not pid:
            # Container started but we cannot find its PID — bail and clean.
            self._docker_force_remove(docker_binary, container_name)
            raise NodeRuntimeError(
                "Could not resolve container PID via docker inspect; "
                "cannot proceed with manual veth setup"
            )

        pid_create_time: float | None = None
        try:
            pid_create_time = psutil.Process(pid).create_time()
        except psutil.Error:
            # PID resolved but psutil cannot see it (privilege boundary in
            # rootless docker). The helper still authorizes via the registry,
            # so we keep the pid but skip the create_time fingerprint.
            pid_create_time = None

        # ----- Step 4: register PID BEFORE any helper-verb call ------------
        try:
            runtime_pids.register(pid, "docker", lab_id, node_id)
        except Exception as exc:
            self._docker_force_remove(docker_binary, container_name)
            raise NodeRuntimeError(
                f"Failed to register container PID in runtime registry: {exc}"
            ) from exc

        # ----- Step 5: manual veth + nsenter rename per interface ----------
        provisioned_host_ends: list[str] = []
        try:
            for attachment in attachments:
                self._attach_docker_interface_initial(
                    lab_id=lab_id,
                    node_id=node_id,
                    pid=pid,
                    attachment=attachment,
                    provisioned_host_ends=provisioned_host_ends,
                )
        except Exception:
            # ----- Step 6: rollback on partial veth setup -----------------
            for host_end in provisioned_host_ends:
                host_net.try_link_del(host_end)
            self._docker_force_remove(docker_binary, container_name)
            try:
                runtime_pids.unregister(pid)
            except Exception:
                pass
            raise

        # ----- Step 7: console TCP forwarder -------------------------------
        # docker run --network=none never spawns docker-proxy, so the
        # advertised host port for the console (-p HOST:CONTAINER above) is
        # unreachable.  Splice the host port into the container's netns via
        # the privileged helper.  Best-effort: a missing/failed proxy still
        # leaves the container usable for non-console operations.
        console_proxy_pid: int | None = None
        try:
            console_proxy_pid = host_net.console_proxy_start(
                node_pid=pid,
                listen_port=int(console_port),
                target_port=int(self._container_console_port(console_mode)),
            )
        except Exception as exc:
            _logger.warning(
                "console proxy failed to start for lab=%s node=%s: %s",
                lab_id,
                node_id,
                exc,
            )

        work_dir = self._work_dir(lab_id, node_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        return {
            "lab_id": lab_id,
            "node_id": node_id,
            "kind": "docker",
            "name": node.get("name"),
            "console": console_mode,
            "console_port": console_port,
            "container_name": container_name,
            "container_id": result.stdout.strip(),
            "pid": pid,
            "pid_create_time": pid_create_time,
            "console_proxy_pid": console_proxy_pid,
            "work_dir": str(work_dir),
            "stdout_log": str(work_dir / "stdout.log"),
            "stderr_log": str(work_dir / "stderr.log"),
            "command": cmd,
            # network_names retained for backwards compatibility with
            # existing readers (live-MAC, log readers); no Docker network
            # actually exists post-US-203.
            "network_names": [spec["name"] for spec in network_specs],
            "veth_host_ends": list(provisioned_host_ends),
            "interface_attachments": [
                {
                    "interface_index": a["interface_index"],
                    "network_id": a["network_id"],
                    "bridge_name": a["bridge_name"],
                    "host_end": host_net.veth_host_name(
                        lab_id, node_id, a["interface_index"]
                    ),
                }
                for a in attachments
            ],
            "started_at": time.time(),
        }

    def _attach_docker_interface_initial(
        self,
        *,
        lab_id: str,
        node_id: int,
        pid: int,
        attachment: dict[str, Any],
        provisioned_host_ends: list[str],
    ) -> None:
        """Attach a single interface for a freshly-started container.

        On any failure mid-sequence, raises and the caller rolls back. This
        helper appends the host-end name to ``provisioned_host_ends``
        BEFORE the kernel object is created so the rollback path can sweep
        a partially-created pair (``ip link add`` is the first step that
        leaks state).
        """
        interface_index = attachment["interface_index"]
        bridge = attachment["bridge_name"]
        host_end = host_net.veth_host_name(lab_id, node_id, interface_index)
        peer_end = host_net.veth_peer_name(lab_id, node_id, interface_index)
        netns_iface = f"eth{interface_index}"

        # Track host_end BEFORE creation so rollback sweeps a partial pair.
        provisioned_host_ends.append(host_end)
        host_net.veth_pair_add(host_end, peer_end)
        host_net.link_master(host_end, bridge)
        host_net.link_up(host_end)
        host_net.link_netns(peer_end, pid)
        host_net.link_set_name_in_netns(pid, peer_end, netns_iface)
        host_net.addr_up_in_netns(pid, netns_iface)

    def attach_docker_interface(
        self,
        lab_id: str,
        node_id: int,
        network_id: int,
        interface_index: int,
        *,
        bridge_name: str | None = None,
    ) -> dict[str, Any]:
        """US-204 / US-204b: PUBLIC hot-attach.

        Acquires the per-``(lab_id, node_id, interface_index)`` mutex
        internally and delegates to the private locked helper. Used by
        start-path callers and any other caller that does NOT already
        hold the mutex on entry. ``link_service.create_link`` instead
        acquires the mutex itself and calls
        :meth:`_attach_docker_interface_locked` directly to avoid double-
        acquiring (the deadlock case).

        Returns the attachment record (which includes ``attach_generation``
        per US-204b) describing the newly-created host-side objects.
        """
        from app.services.runtime_mutex import runtime_mutex

        with runtime_mutex.acquire_sync(lab_id, node_id, interface_index):
            return self._attach_docker_interface_locked(
                lab_id,
                node_id,
                network_id,
                interface_index,
                bridge_name=bridge_name,
            )

    def _attach_docker_interface_locked(
        self,
        lab_id: str,
        node_id: int,
        network_id: int,
        interface_index: int,
        *,
        bridge_name: str | None = None,
    ) -> dict[str, Any]:
        """US-204 / US-204b: PRIVATE hot-attach. Mutex MUST be held.

        Symmetric with the initial-attach path used by ``_start_docker_node``
        (US-203): both invoke the same 6-step ``_attach_docker_interface_initial``
        helper so host-side iface naming (``nve…d…i…h``) is identical between
        first-NIC and Nth-NIC attachments — there is no special-case for the
        first NIC.

        Sequence:

          1. Defensive contract: assert the per-``(lab, node, iface)`` mutex
             is held (US-204b — Codex v5 finding #1). Catches start-path-
             bypass bugs at the layer that has the most context.
          2. Pre-flight: confirm the runtime record exists, the container is
             alive, and the kind is ``docker`` (rejects QEMU / stopped nodes
             with ``NodeRuntimeError``).
          3. Resolve / verify the target bridge name. Surface a typed
             ``NodeRuntimeError`` when the bridge is not present on the host
             (US-202 must have created it).
          4. Drive the same per-iface attach sequence as initial attach via
             ``_attach_docker_interface_initial``: ``veth_pair_add`` →
             ``link_master`` → ``link_up`` → ``link_netns`` →
             ``link_set_name_in_netns`` → ``addr_up_in_netns``.
          5. On any failure mid-sequence, sweep the partial host-end veth
             (``host_net.try_link_del``) and re-raise.
          6. On success, bump ``current_attach_generation`` on the runtime
             record's interface entry, append the new attachment + host-end
             to the runtime, and persist.

        Returns the attachment record (``attach_generation`` included) for
        the link router / link_service to stamp on ``Link.runtime``.
        """
        from app.services.runtime_mutex import runtime_mutex

        # Defensive contract: catches accidental bypass of the public API.
        assert runtime_mutex.is_held(lab_id, node_id, interface_index), (
            f"_attach_docker_interface_locked called without the per-"
            f"(lab, node, iface) mutex held for "
            f"({lab_id!r}, {node_id}, {interface_index}); use the public "
            f"attach_docker_interface(...) entrypoint or acquire "
            f"runtime_mutex.acquire(...) yourself."
        )

        runtime = self._runtime_record(lab_id, node_id)
        if runtime is None:
            raise NodeRuntimeError(
                f"Cannot hot-attach interface: node {node_id} in lab {lab_id} is "
                "not running (no runtime record)."
            )
        if runtime.get("kind") != "docker":
            raise NodeRuntimeError(
                f"Cannot hot-attach docker interface: node {node_id} runtime kind is "
                f"{runtime.get('kind')!r}, expected 'docker'."
            )

        pid = runtime.get("pid")
        if not pid:
            raise NodeRuntimeError(
                f"Cannot hot-attach interface: node {node_id} has no resolved "
                "container PID."
            )

        # Reject duplicate interface indices on the same node — the host_end
        # name only encodes (lab, node, iface), so re-attaching the same iface
        # would collide on ``ip link add``.
        existing_attachments = runtime.get("interface_attachments") or []
        for existing in existing_attachments:
            if int(existing.get("interface_index", -1)) == int(interface_index):
                raise NodeRuntimeError(
                    f"interface_index={interface_index} already attached on "
                    f"node {node_id}; detach before re-attaching."
                )

        bridge = bridge_name or host_net.bridge_name(lab_id, int(network_id))
        if not host_net.bridge_exists(bridge):
            raise NodeRuntimeError(
                f"Bridge {bridge} for network_id={network_id} is not present on "
                "the host; provision it via create_network (US-202) before "
                "hot-attaching interfaces."
            )

        attachment = {
            "interface_index": int(interface_index),
            "network_id": int(network_id),
            "bridge_name": bridge,
        }

        provisioned_host_ends: list[str] = []
        try:
            self._attach_docker_interface_initial(
                lab_id=lab_id,
                node_id=int(node_id),
                pid=int(pid),
                attachment=attachment,
                provisioned_host_ends=provisioned_host_ends,
            )
        except Exception:
            for host_end in provisioned_host_ends:
                host_net.try_link_del(host_end)
            raise

        host_end = host_net.veth_host_name(lab_id, int(node_id), int(interface_index))

        # US-204b: bump the per-interface ``current_attach_generation``
        # atomically with the runtime-record write so the new generation is
        # never visible without the matching attachment present.
        new_generation = self._bump_interface_attach_generation(
            runtime, int(interface_index)
        )

        new_attachment = {
            "interface_index": int(interface_index),
            "network_id": int(network_id),
            "bridge_name": bridge,
            "host_end": host_end,
            "attach_generation": new_generation,
        }

        # Persist the new attachment onto the runtime record so stop-time
        # cleanup sweeps the host-end veth (matches initial-attach contract
        # at ``_stop_docker_runtime``).
        with self._lock:
            attachments_list = list(runtime.get("interface_attachments") or [])
            attachments_list.append(new_attachment)
            runtime["interface_attachments"] = attachments_list
            host_ends = list(runtime.get("veth_host_ends") or [])
            if host_end not in host_ends:
                host_ends.append(host_end)
            runtime["veth_host_ends"] = host_ends
        self._persist_runtime(runtime)

        return new_attachment

    def detach_docker_interface(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        *,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """US-204b PUBLIC hot-detach. Acquires the mutex; delegates to
        :meth:`_detach_docker_interface_locked`. Mirrors the public/private
        split of attach (full detach IPAM-release semantics arrive in
        US-205; US-204b ships the gen-token freshness check + the kernel-
        side veth removal).
        """
        from app.services.runtime_mutex import runtime_mutex

        with runtime_mutex.acquire_sync(lab_id, node_id, interface_index):
            return self._detach_docker_interface_locked(
                lab_id,
                node_id,
                interface_index,
                expected_generation=expected_generation,
            )

    def _detach_docker_interface_locked(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        *,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """US-204b PRIVATE hot-detach. Mutex MUST be held.

        Generation-token semantics (US-204b — Codex v5 finding #2): if
        ``expected_generation`` is supplied and does NOT equal the
        runtime's ``current_attach_generation`` for this interface, the
        detach is logged + no-ops. The matching link's
        ``Link.runtime.attach_generation`` was stamped under ``lab_lock``
        at attach time, so a "newer attach already happened" reading is
        unambiguous.

        Returns a dict with at least ``state`` ∈
        ``{"detached", "stale_noop", "absent"}``.
        """
        from app.services.runtime_mutex import runtime_mutex

        assert runtime_mutex.is_held(lab_id, node_id, interface_index), (
            f"_detach_docker_interface_locked called without the per-"
            f"(lab, node, iface) mutex held for "
            f"({lab_id!r}, {node_id}, {interface_index})."
        )

        runtime = self._runtime_record(lab_id, node_id, include_stopped=True)
        if runtime is None:
            return {"state": "absent", "reason": "no runtime record"}

        # Locate the matching attachment record. Missing means the iface is
        # already detached — idempotent no-op.
        attachments = runtime.get("interface_attachments") or []
        target = None
        target_index = None
        for index, entry in enumerate(attachments):
            if int(entry.get("interface_index", -1)) == int(interface_index):
                target = entry
                target_index = index
                break
        if target is None:
            return {"state": "absent", "reason": "no attachment record"}

        # US-204b generation-token check. Use the interface's
        # ``current_attach_generation`` (the freshness oracle) — NOT the
        # attachment's own ``attach_generation`` — so a fresh re-attach
        # (which bumped ``current_attach_generation`` past the caller's
        # ``expected_generation``) correctly invalidates a stale rollback.
        current_gen = self._interface_attach_generation(runtime, int(interface_index))
        if expected_generation is not None and int(expected_generation) != int(current_gen):
            _logger.info(
                "stale detach for gen %s (current %s), ignoring "
                "(lab=%s node=%s iface=%s)",
                expected_generation,
                current_gen,
                lab_id,
                node_id,
                interface_index,
            )
            return {
                "state": "stale_noop",
                "expected_generation": int(expected_generation),
                "current_attach_generation": int(current_gen),
            }

        host_end = target.get("host_end") or host_net.veth_host_name(
            lab_id, int(node_id), int(interface_index)
        )
        # US-205 Codex critic v2: hot-detach must surface real kernel-side
        # failures so the caller (link_service.delete_link) can leave
        # lab.json + IPAM + runtime_attachments intact on error. We use
        # ``link_del`` (raises) instead of ``try_link_del`` (swallows
        # everything) but preserve idempotency by treating
        # :class:`host_net.HostNetEINVAL` ("no such link") as success —
        # someone (e.g. an orphan sweep) already removed the host-end.
        # Any other :class:`host_net.HostNetError` propagates: the
        # attachment row + host-end remain on the runtime record so the
        # caller can roll back lab.json / IPAM consistently.
        try:
            host_net.link_del(host_end)
        except host_net.HostNetEINVAL:
            # Host-end already gone; fall through to runtime-record cleanup.
            pass

        # Drop the attachment + host-end from the runtime record so
        # stop-time cleanup does not double-sweep.
        with self._lock:
            attachments_list = list(runtime.get("interface_attachments") or [])
            if target_index is not None and target_index < len(attachments_list):
                attachments_list.pop(target_index)
            runtime["interface_attachments"] = attachments_list
            host_ends = [
                h for h in (runtime.get("veth_host_ends") or [])
                if h != host_end
            ]
            runtime["veth_host_ends"] = host_ends
        self._persist_runtime(runtime)

        return {
            "state": "detached",
            "host_end": host_end,
            "current_attach_generation": int(current_gen),
        }

    @staticmethod
    def _bump_interface_attach_generation(
        runtime: dict[str, Any], interface_index: int
    ) -> int:
        """US-204b: increment ``current_attach_generation`` for the named
        interface on the runtime record. Returns the new generation value.

        The runtime record carries an ``interface_runtime`` map keyed by
        stringified interface_index — we do not mutate ``node.interfaces``
        here because that is part of the lab.json schema persisted by
        ``LabService.write_lab_json_static`` under ``lab_lock``;
        ``link_service.create_link`` is responsible for the lab.json side
        of the bump. Here we only track the in-memory / runtime-state
        copy used by the gen-check during detach.
        """
        iface_runtime = runtime.setdefault("interface_runtime", {})
        key = str(int(interface_index))
        record = iface_runtime.setdefault(key, {"current_attach_generation": 0})
        new_gen = int(record.get("current_attach_generation", 0)) + 1
        record["current_attach_generation"] = new_gen
        return new_gen

    @staticmethod
    def _interface_attach_generation(
        runtime: dict[str, Any], interface_index: int
    ) -> int:
        iface_runtime = runtime.get("interface_runtime") or {}
        record = iface_runtime.get(str(int(interface_index))) or {}
        return int(record.get("current_attach_generation", 0))

    # ------------------------------------------------------------------
    # US-303 — QMP-driven hot-add NIC for running QEMU nodes
    # ------------------------------------------------------------------

    def attach_qemu_interface(
        self,
        lab_id: str,
        node_id: int,
        network_id: int,
        interface_index: int,
        *,
        bridge_name: str | None = None,
        nic_model: str | None = None,
        planned_mac: str | None = None,
    ) -> dict[str, Any]:
        """US-303 PUBLIC hot-add NIC for a running QEMU node.

        Acquires the per-``(lab_id, node_id, interface_index)`` mutex
        internally (mirrors :meth:`attach_docker_interface`) and delegates
        to :meth:`_attach_qemu_interface_locked`. Used by callers that do
        NOT already hold the mutex on entry (e.g. start-path callers,
        direct API entry points). ``link_service.create_link`` instead
        acquires the mutex itself and calls the locked helper directly to
        avoid double-acquisition.

        Returns the attachment record (which includes ``attach_generation``
        per US-204b) describing the newly-created TAP + QMP-side objects.
        """
        from app.services.runtime_mutex import runtime_mutex

        with runtime_mutex.acquire_sync(lab_id, node_id, interface_index):
            return self._attach_qemu_interface_locked(
                lab_id,
                node_id,
                network_id,
                interface_index,
                bridge_name=bridge_name,
                nic_model=nic_model,
                planned_mac=planned_mac,
            )

    def _attach_qemu_interface_locked(
        self,
        lab_id: str,
        node_id: int,
        network_id: int,
        interface_index: int,
        *,
        bridge_name: str | None = None,
        nic_model: str | None = None,
        planned_mac: str | None = None,
    ) -> dict[str, Any]:
        """US-303 PRIVATE hot-add NIC. Mutex MUST be held.

        Sequence (rollback per the plan §US-303):

          1. Acquire lock — done by caller; assert mutex is held here.
          2. ``query-pci`` → find the highest free ``pcie-root-port`` slot
             (descending scan per US-301 policy: hot-add never collides
             with the boot-time positional layout).
          3. ``host_net.tap_add(tap_name)``.
          4. ``host_net.link_master(tap_name, bridge_name)``.
          5. QMP ``netdev_add type=tap id=net{interface_index}
             ifname={tap_name} script=no downscript=no``.
          6. QMP ``device_add driver={qemu_nic_model} id=dev{interface_index}
             netdev=net{interface_index} bus=rp{slot} mac={planned_mac}``.

        Rollback (Codex critic enumerated, no hand-waving):

          * Step 2 (query-pci) fails → release lock, raise NodeRuntimeError.
          * Step 3 (``host_net.tap_add``) fails → raise NodeRuntimeError.
          * Step 4 (``host_net.link_master``) fails → ``host_net.tap_del``.
          * Step 5 (QMP ``netdev_add``) fails → ``host_net.link_set_nomaster``,
            ``host_net.tap_del``.
          * Step 6 (QMP ``device_add``) fails → QMP ``netdev_del``,
            ``host_net.link_set_nomaster``, ``host_net.tap_del``.

        All rollback steps are wrapped in ``try/except`` so a rollback
        failure logs but does NOT mask the original error.

        QMP ``id=`` MUST use ``interface_index``, NOT slot number — this
        preserves the ``_read_qemu_live_mac`` invariant (see line ~367
        of this module: ``query-rx-filter`` lookups use
        ``f"net{interface_index}"``). Slot is just topology placement.

        ``nic_model`` MUST come from the same ``extras.qemu_nic`` /
        ``node.template.qemu_nic`` used at boot — Codex critic finding #4.
        Hardcoding ``virtio-net-pci`` would mix boot/hotplug device types
        in the same VM.

        Returns the attachment record (with ``attach_generation``) for the
        link router / link_service to stamp on ``Link.runtime``.
        """
        from app.services.runtime_mutex import runtime_mutex

        # Defensive contract: catches accidental bypass of the public API.
        assert runtime_mutex.is_held(lab_id, node_id, interface_index), (
            f"_attach_qemu_interface_locked called without the per-"
            f"(lab, node, iface) mutex held for "
            f"({lab_id!r}, {node_id}, {interface_index}); use the public "
            f"attach_qemu_interface(...) entrypoint or acquire "
            f"runtime_mutex.acquire(...) yourself."
        )

        runtime = self._runtime_record(lab_id, node_id)
        if runtime is None:
            raise NodeRuntimeError(
                f"Cannot hot-attach interface: node {node_id} in lab {lab_id} "
                "is not running (no runtime record)."
            )
        if runtime.get("kind") != "qemu":
            raise NodeRuntimeError(
                f"Cannot hot-attach qemu interface: node {node_id} runtime kind "
                f"is {runtime.get('kind')!r}, expected 'qemu'."
            )

        if not runtime.get("hotplug_capable", False):
            raise NodeRuntimeError(
                f"Cannot hot-attach interface on node {node_id}: template "
                f"capabilities.hotplug is false or machine is not q35. "
                "Restart the node with a hot-plug-capable template."
            )

        socket_path = runtime.get("qmp_socket") or ""
        if not socket_path:
            work_dir = runtime.get("work_dir")
            socket_path = str(Path(work_dir) / "qmp.sock") if work_dir else ""
        if not socket_path:
            raise NodeRuntimeError(
                f"Cannot hot-attach interface on node {node_id}: QMP socket "
                "path is not set on the runtime record."
            )

        # Reject duplicate interface indices on the same node — the QMP
        # ``id=net{interface_index}`` would collide with an existing one.
        existing_attachments = runtime.get("interface_attachments") or []
        for existing in existing_attachments:
            if int(existing.get("interface_index", -1)) == int(interface_index):
                raise NodeRuntimeError(
                    f"interface_index={interface_index} already attached on "
                    f"node {node_id}; detach before re-attaching."
                )

        bridge = bridge_name or host_net.bridge_name(lab_id, int(network_id))
        if not host_net.bridge_exists(bridge):
            raise NodeRuntimeError(
                f"Bridge {bridge} for network_id={network_id} is not present on "
                "the host; provision it via create_network (US-202) before "
                "hot-attaching interfaces."
            )

        # Resolve the NIC model from the same source the boot path uses
        # (Codex critic finding #4). Caller may override (link_service can
        # plumb extras through), otherwise read from the runtime record's
        # cached extras snapshot or default to ``e1000`` (matches boot
        # default at ``_start_qemu_node`` line ~954).
        if not nic_model:
            nic_model = self._resolve_qemu_nic_model(lab_id, node_id, runtime)

        # Resolve the planned MAC the same way the boot path does:
        # ``firstmac`` + ``interface_index`` offset.
        if not planned_mac:
            planned_mac = self._resolve_qemu_planned_mac(
                lab_id, node_id, runtime, int(interface_index)
            )

        max_nics = int(runtime.get("max_nics", 8) or 8)

        # ----- Step 2: query-pci → find free pcie-root-port slot --------
        # US-303 codex iter1 HIGH-2: PCIe slots are NODE-wide, not
        # iface-local. Two concurrent attaches to different ifaces on the
        # same VM can both call query-pci, both see the same free rpN,
        # and both attempt device_add → race. Wrap the slot-pick →
        # device_add window in a per-(lab, node) "node-scoped" lock so
        # only one attach picks a slot at a time. The per-(lab, node,
        # iface) mutex (US-204b contract) is still held by the caller
        # for delete-vs-attach serialization on the same iface.
        from app.services.runtime_mutex import runtime_mutex as _mutex

        with _mutex.acquire_node_sync(lab_id, int(node_id)):
            try:
                slot = self._find_free_pcie_slot(
                    socket_path,
                    max_nics,
                    reserved_slots=runtime.get("allocated_slots") or [],
                )
            except NodeRuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 — typed below
                raise NodeRuntimeError(
                    f"QMP query-pci failed during hot-add: {exc}"
                ) from exc

            if slot is None:
                raise NodeRuntimeError(
                    f"All {max_nics} hot-plug slots in use on this VM. To grow the "
                    "pool, edit the template's `capabilities.max_nics` in "
                    "backend/templates/qemu/{type}/{template}.yml and restart "
                    "this node."
                )

            # Reserve the slot in-runtime BEFORE issuing device_add so a
            # concurrent attach that grabs the node-lock immediately
            # after we release it sees the slot as taken even though
            # query-pci has not yet observed it. We move pending →
            # final on success or strip on rollback.
            with self._lock:
                allocated_slots = list(runtime.get("allocated_slots") or [])
                if int(slot) not in allocated_slots:
                    allocated_slots.append(int(slot))
                runtime["allocated_slots"] = allocated_slots

            tap = host_net.tap_name(lab_id, int(node_id), int(interface_index))
            netdev_id = f"net{int(interface_index)}"
            device_id = f"dev{int(interface_index)}"

            tap_provisioned = False
            bridge_attached = False
            netdev_added = False
            device_added = False
            slot_reserved_in_runtime = True
            timeout_seen = False
            try:
                # ----- Step 3: tap_add ------------------------------------
                host_net.tap_add(tap)
                tap_provisioned = True

                # ----- Step 4: link_master (TAP -> bridge) ----------------
                host_net.link_master(tap, bridge)
                bridge_attached = True
                # Bring the host side of the TAP up so traffic can flow.
                host_net.link_up(tap)

                # ----- Step 5: QMP netdev_add -----------------------------
                try:
                    netdev_response = self._qmp_command(
                        socket_path,
                        "netdev_add",
                        {
                            "type": "tap",
                            "id": netdev_id,
                            "ifname": tap,
                            "script": "no",
                            "downscript": "no",
                        },
                    )
                except NodeRuntimeQMPTimeout:
                    # Transport timeout: QEMU may already have created
                    # the netdev — assume YES so rollback issues
                    # netdev_del idempotently.
                    netdev_added = True
                    timeout_seen = True
                    raise
                if isinstance(netdev_response, dict) and "error" in netdev_response:
                    raise NodeRuntimeError(
                        f"QMP netdev_add failed: {netdev_response['error']}"
                    )
                netdev_added = True

                # ----- Step 6: QMP device_add -----------------------------
                device_args: dict[str, Any] = {
                    "driver": nic_model,
                    "id": device_id,
                    "netdev": netdev_id,
                    "bus": f"rp{slot}",
                }
                if planned_mac:
                    device_args["mac"] = planned_mac
                try:
                    device_response = self._qmp_command(
                        socket_path, "device_add", device_args
                    )
                except NodeRuntimeQMPTimeout:
                    # Transport timeout: QEMU may already have created
                    # the device — assume YES so rollback issues
                    # device_del idempotently.
                    device_added = True
                    timeout_seen = True
                    raise
                if isinstance(device_response, dict) and "error" in device_response:
                    raise NodeRuntimeError(
                        f"QMP device_add failed: {device_response['error']}"
                    )
                device_added = True
            except Exception:
                # 6-step rollback per plan §US-303. Each cleanup is wrapped in
                # try/except so a rollback failure logs but does not mask the
                # original error.
                #
                # US-303 codex iter1 HIGH-1: on a transport-level
                # timeout, we cannot tell whether QEMU applied the
                # command. We must run BOTH device_del and netdev_del
                # idempotently — if QEMU never applied the command, the
                # *_del will return "no such device" / "no such netdev"
                # and we swallow it (the inner try/except).
                if device_added:
                    try:
                        self._qmp_command(
                            socket_path, "device_del", {"id": device_id}
                        )
                    except Exception:  # noqa: BLE001
                        _logger.exception(
                            "rollback: QMP device_del(%s) failed", device_id
                        )
                if netdev_added:
                    try:
                        self._qmp_command(
                            socket_path, "netdev_del", {"id": netdev_id}
                        )
                    except Exception:  # noqa: BLE001
                        _logger.exception(
                            "rollback: QMP netdev_del(%s) failed", netdev_id
                        )
                if bridge_attached:
                    try:
                        host_net.link_set_nomaster(tap)
                    except Exception:  # noqa: BLE001
                        _logger.exception(
                            "rollback: link_set_nomaster(%s) failed", tap
                        )
                if tap_provisioned:
                    try:
                        host_net.tap_del(tap)
                    except Exception:  # noqa: BLE001
                        try:
                            host_net.try_link_del(tap)
                        except Exception:  # noqa: BLE001
                            _logger.exception(
                                "rollback: tap_del(%s) failed", tap
                            )
                # Release the pending slot reservation so a retry can
                # pick it up.
                if slot_reserved_in_runtime:
                    with self._lock:
                        allocated = list(runtime.get("allocated_slots") or [])
                        runtime["allocated_slots"] = [
                            s for s in allocated if int(s) != int(slot)
                        ]
                # Suppress the unused-flag lint warning while keeping
                # the timeout context for future debugging.
                _ = timeout_seen
                raise

        # ----- US-204b: bump current_attach_generation ----------------
        new_generation = self._bump_interface_attach_generation(
            runtime, int(interface_index)
        )

        new_attachment = {
            "interface_index": int(interface_index),
            "network_id": int(network_id),
            "bridge_name": bridge,
            "tap_name": tap,
            "slot": int(slot),
            "nic_model": nic_model,
            "attach_generation": new_generation,
            "planned_mac": planned_mac or "",
        }

        # Persist the new attachment + tap onto the runtime record so
        # stop-time cleanup sweeps the TAP (matches initial-attach
        # contract at ``_stop_qemu_runtime``). The slot was already
        # reserved during the slot-pick window above; we just add the
        # attachment record + TAP name here.
        with self._lock:
            attachments_list = list(runtime.get("interface_attachments") or [])
            attachments_list.append(new_attachment)
            runtime["interface_attachments"] = attachments_list
            tap_names = list(runtime.get("tap_names") or [])
            if tap not in tap_names:
                tap_names.append(tap)
            runtime["tap_names"] = tap_names
        self._persist_runtime(runtime)

        # US-303 codex iter1: persist the computed planned_mac onto
        # ``interface.planned_mac`` in lab.json so the live-MAC mismatch
        # detection path (``_read_qemu_live_mac``) can compare against
        # the value we actually passed to device_add. Without this, the
        # ``firstmac`` default case would leave ``interface.planned_mac``
        # empty and the mismatch detector would have nothing to compare
        # against.
        if planned_mac:
            try:
                self._persist_planned_mac_to_lab_json(
                    lab_id, int(node_id), int(interface_index), planned_mac
                )
            except Exception:  # noqa: BLE001 — best-effort, observability only
                _logger.exception(
                    "failed to persist planned_mac=%s for "
                    "lab=%s node=%s iface=%s",
                    planned_mac,
                    lab_id,
                    node_id,
                    interface_index,
                )

        return new_attachment

    # ------------------------------------------------------------------
    # US-304 — QMP-driven hot-remove NIC for running QEMU nodes
    # ------------------------------------------------------------------

    def detach_qemu_interface(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        *,
        lab_path: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """US-304 PUBLIC hot-remove NIC for a running QEMU node.

        Acquires the per-``(lab_id, node_id, interface_index)`` mutex
        internally (mirrors :meth:`attach_qemu_interface`) and delegates
        to :meth:`_detach_qemu_interface_locked`. Used by callers that do
        NOT already hold the mutex on entry (e.g. direct API entrypoints).
        ``link_service.delete_link`` instead acquires the mutex itself
        and calls the locked helper directly to avoid double-acquisition.

        ``lab_path`` is accepted as a forward-compat hook for log
        contextualisation; the detach itself is fully driven by the
        in-process runtime registry (no lab.json read required).

        ``expected_generation`` mirrors the US-204b freshness contract on
        the docker detach path: when supplied, it is compared against the
        runtime's per-interface ``current_attach_generation`` before any
        QMP traffic. A stale rollback (older generation than the live
        attachment) returns ``state='stale_noop'`` without issuing
        ``device_del`` — preventing a stale delete/rollback from tearing
        down a NEWER QEMU NIC that reuses the same ``dev{iface}`` /
        ``net{iface}`` QMP IDs.

        Returns a result dict whose ``state`` field is one of:
          * ``"detached"`` — device gone from QMP within the bounded
            poll window; ``netdev_del`` + ``tap_del`` were issued.
          * ``"forced"`` — guest never ejected the device; the TAP was
            detached from the bridge (kernel-side stop) but ``netdev_del``
            and ``tap_del`` were intentionally skipped because the QMP
            side still holds the device.
          * ``"absent"`` — no runtime record / no matching attachment
            (idempotent double-delete).
          * ``"stale_noop"`` — ``expected_generation`` did not match the
            current runtime generation; nothing was changed.
        """
        from app.services.runtime_mutex import runtime_mutex

        with runtime_mutex.acquire_sync(lab_id, node_id, interface_index):
            return self._detach_qemu_interface_locked(
                lab_id,
                node_id,
                interface_index,
                lab_path=lab_path,
                expected_generation=expected_generation,
            )

    def _detach_qemu_interface_locked(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        *,
        lab_path: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """US-304 PRIVATE hot-remove NIC. Mutex MUST be held.

        Sequence (per plan §US-304 acceptance criteria):

          1. QMP ``device_del id=dev{interface_index}``. If QEMU returns
             ``DeviceNotFound`` (race A — delete arrived before add
             completed), sleep 0.5s and retry once; if still
             ``DeviceNotFound``, treat as already-detached success
             (idempotent double-delete).
          2. Bounded poll: ``query-pci`` every 500ms for up to 8s
             (16 iterations). Device is "gone" when no
             ``qdev_id == f"dev{interface_index}"`` appears anywhere in
             the bus->devices arrays.
          3a. Happy path (gone within timeout): QMP ``netdev_del`` ->
              ``host_net.tap_del(tap_name)``.
          3b. Forced fallback (still present at 8s):
              ``host_net.link_set_nomaster(tap_name)`` so packets stop
              flowing kernel-side; do NOT call ``tap_del`` or
              ``netdev_del`` because the QEMU side still references the
              device until guest reboot. Publish a ``node_warning`` WS
              event so the UI can surface "restart node to fully clean".

        Transport-level errors (``NodeRuntimeQMPTimeout``) on
        ``device_del``, ``netdev_del``, or ``query-pci`` propagate — they
        are NOT swallowed as forced-fallback (transport timeout != guest
        didn't eject).

        Generation tokens: bump ``current_attach_generation`` for this
        interface AFTER the kernel objects are removed (happy path) so a
        stale concurrent attach doesn't race a freshly torn-down NIC.
        Forced fallback also bumps the generation because lab.json's
        ``links[]`` will be the source of truth and the kernel-side
        object is no longer reachable.
        """
        from app.services.runtime_mutex import runtime_mutex

        # Defensive contract: catches accidental bypass of the public API.
        assert runtime_mutex.is_held(lab_id, node_id, interface_index), (
            f"_detach_qemu_interface_locked called without the per-"
            f"(lab, node, iface) mutex held for "
            f"({lab_id!r}, {node_id}, {interface_index}); use the public "
            f"detach_qemu_interface(...) entrypoint or acquire "
            f"runtime_mutex.acquire(...) yourself."
        )

        runtime = self._runtime_record(lab_id, node_id, include_stopped=True)
        if runtime is None:
            # No runtime record means the node already stopped or never
            # started — nothing to detach. Treat as idempotent absent.
            return {"state": "absent", "reason": "no runtime record"}
        if runtime.get("kind") != "qemu":
            # Wrong runtime kind — caller (link_service) routes by kind so
            # this should never happen, but keep it defensive.
            return {
                "state": "absent",
                "reason": f"runtime kind={runtime.get('kind')!r}, expected 'qemu'",
            }

        # Locate the matching attachment record. Missing means the iface
        # is already detached — idempotent no-op.
        attachments = runtime.get("interface_attachments") or []
        target = None
        target_index = None
        for index, entry in enumerate(attachments):
            if int(entry.get("interface_index", -1)) == int(interface_index):
                target = entry
                target_index = index
                break
        if target is None:
            return {"state": "absent", "reason": "no attachment record"}

        # US-204b generation-token check (mirrors
        # :meth:`_detach_docker_interface_locked`). Use the interface's
        # ``current_attach_generation`` (the freshness oracle) — NOT the
        # attachment's own ``attach_generation`` — so a fresh re-attach
        # (which bumped ``current_attach_generation`` past the caller's
        # ``expected_generation``) correctly invalidates a stale rollback.
        # Without this guard, a stale delete/rollback could tear down a
        # NEWER QEMU NIC because QMP IDs reuse ``dev{iface}`` /
        # ``net{iface}`` deterministically.
        current_gen = self._interface_attach_generation(
            runtime, int(interface_index)
        )
        if expected_generation is not None and int(expected_generation) != int(current_gen):
            _logger.info(
                "stale qemu detach for gen %s (current %s), ignoring "
                "(lab=%s node=%s iface=%s)",
                expected_generation,
                current_gen,
                lab_id,
                node_id,
                interface_index,
            )
            return {
                "state": "stale_noop",
                "expected_generation": int(expected_generation),
                "current_attach_generation": int(current_gen),
            }

        socket_path = runtime.get("qmp_socket") or ""
        if not socket_path:
            work_dir = runtime.get("work_dir")
            socket_path = str(Path(work_dir) / "qmp.sock") if work_dir else ""
        if not socket_path:
            raise NodeRuntimeError(
                f"Cannot hot-detach interface on node {node_id}: QMP socket "
                "path is not set on the runtime record."
            )

        device_id = f"dev{int(interface_index)}"
        netdev_id = f"net{int(interface_index)}"
        tap = target.get("tap_name") or host_net.tap_name(
            lab_id, int(node_id), int(interface_index)
        )
        slot = target.get("slot")

        # ----- Step 1: device_del with race-A retry --------------------
        device_already_gone = False
        device_del_response = self._qmp_command(
            socket_path, "device_del", {"id": device_id}
        )
        if (
            isinstance(device_del_response, dict)
            and isinstance(device_del_response.get("error"), dict)
        ):
            err = device_del_response["error"]
            err_class = str(err.get("class", "")) if isinstance(err, dict) else ""
            if err_class == "DeviceNotFound":
                # Race A: ``device_del`` arrived before the create-side
                # flushed ``device_add`` to ``query-pci`` visibility.
                # Plan §US-304 acceptance criteria: "wait up to 2s for
                # the create-side lock to finish, then retry once."
                #
                # Codex hotfix MEDIUM-1: the previous implementation was
                # a fixed ``sleep(0.5)`` + one retry. If ``device_add``
                # visibility lagged past 500ms, that classified the NIC
                # as "already gone" and tore down ``netdev`` / TAP while
                # the device was about to appear.
                #
                # Real bounded wait: poll ``query-pci`` every 100ms for
                # the device to APPEAR, up to 2.0s total. As soon as it
                # is visible (or the budget expires), retry ``device_del``
                # exactly once. Treat second-call ``DeviceNotFound`` as
                # idempotent success (the device was never created).
                _RACE_A_BUDGET_S = 2.0
                _RACE_A_CADENCE_S = 0.1
                _race_a_iters = max(1, int(_RACE_A_BUDGET_S / _RACE_A_CADENCE_S))
                for _race_a_i in range(_race_a_iters):
                    if not self._qemu_device_gone(socket_path, device_id):
                        # Device became visible — break and retry.
                        break
                    time.sleep(_RACE_A_CADENCE_S)
                retry_response = self._qmp_command(
                    socket_path, "device_del", {"id": device_id}
                )
                if (
                    isinstance(retry_response, dict)
                    and isinstance(retry_response.get("error"), dict)
                    and str(retry_response["error"].get("class", ""))
                    == "DeviceNotFound"
                ):
                    device_already_gone = True
                elif (
                    isinstance(retry_response, dict)
                    and isinstance(retry_response.get("error"), dict)
                ):
                    raise NodeRuntimeError(
                        f"QMP device_del retry failed: {retry_response['error']}"
                    )
                # else: retry succeeded — fall through to bounded poll.
            else:
                raise NodeRuntimeError(
                    f"QMP device_del failed: {err}"
                )

        # ----- Step 2: bounded poll for device removal -----------------
        # 16 iterations * 500ms = 8s ceiling per plan §US-304.
        device_gone = device_already_gone
        if not device_gone:
            for _iteration in range(16):
                if self._qemu_device_gone(socket_path, device_id):
                    device_gone = True
                    break
                time.sleep(0.5)

        forced_fallback = False
        if device_gone:
            # ----- Step 3a: happy path — netdev_del + tap_del ----------
            # Order: issue netdev_del first; on transport timeout we
            # STILL run tap_del so the kernel TAP doesn't leak (the
            # host-side cleanup is not impacted by a QMP transport
            # blip). The exception is re-raised AFTER the host-side
            # cleanup. In-band ``netdev_del`` errors that look like
            # "no such netdev" are absorbed (the netdev was already
            # removed); other in-band errors are deferred until after
            # tap_del runs so the host-side still gets cleaned up.
            netdev_del_error: dict[str, Any] | None = None
            netdev_transport_error: NodeRuntimeQMPTimeout | None = None
            try:
                netdev_del_response = self._qmp_command(
                    socket_path, "netdev_del", {"id": netdev_id}
                )
            except NodeRuntimeQMPTimeout as exc:
                # Transport timeout: cleanup host side first, raise after.
                netdev_transport_error = exc
                netdev_del_response = None
            if isinstance(netdev_del_response, dict) and isinstance(
                netdev_del_response.get("error"), dict
            ):
                err = netdev_del_response["error"]
                err_class = str(err.get("class", ""))
                err_desc = str(err.get("desc", "")).lower()
                # An idempotent "no such netdev" reply is fine — netdev
                # was already removed by an earlier rollback. Match by
                # err_class plus a description heuristic (QEMU's reply
                # is a GenericError with "not found" in the desc).
                idempotent = (
                    err_class == "DeviceNotFound"
                    or "no" in err_desc
                    or "not found" in err_desc
                )
                if not idempotent:
                    netdev_del_error = err

            # tap_del is best-effort with a typed propagation: if the
            # TAP is already gone (HostNetEINVAL), swallow it; any
            # other helper error propagates so the caller can roll back.
            try:
                host_net.tap_del(tap)
            except host_net.HostNetEINVAL:
                pass

            if netdev_transport_error is not None:
                raise netdev_transport_error
            if netdev_del_error is not None:
                raise NodeRuntimeError(
                    f"QMP netdev_del failed: {netdev_del_error}"
                )
        else:
            # ----- Step 3b: forced fallback ---------------------------
            # Codex hotfix HIGH-2: ``link_set_nomaster`` failure is a
            # HARD detach failure. Previously we swallowed it, then
            # still tore down runtime state, released the slot, bumped
            # the generation, and let ``link_service.delete_link``
            # remove the link from ``lab.json`` — leaving live bridge
            # connectivity behind while reporting success and making
            # retry impossible because the attachment row was gone.
            #
            # Re-raise as ``host_net.HostNetError`` so ``delete_link``
            # surfaces the failure to the caller, leaves
            # ``interface_attachments`` / ``allocated_slots`` /
            # ``current_attach_generation`` UNCHANGED, and keeps
            # ``lab.json`` + IPAM intact for retry.
            forced_fallback = True
            host_net.link_set_nomaster(tap)
            # Publish a ws_hub warning so the UI can surface that the
            # guest did not eject the NIC.
            try:
                self._publish_node_warning(
                    lab_id=lab_id,
                    node_id=int(node_id),
                    interface_index=int(interface_index),
                    message="guest did not eject NIC; restart node to fully clean",
                )
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "us-304 forced-fallback: ws warning publish failed "
                    "(lab=%s node=%s iface=%s)",
                    lab_id,
                    node_id,
                    interface_index,
                )

        # ----- Drop attachment + slot reservation from runtime ---------
        with self._lock:
            attachments_list = list(runtime.get("interface_attachments") or [])
            if target_index is not None and target_index < len(attachments_list):
                # Re-locate by interface_index to be robust against any
                # concurrent mutation between probe and now.
                for idx, entry in enumerate(attachments_list):
                    if int(entry.get("interface_index", -1)) == int(interface_index):
                        attachments_list.pop(idx)
                        break
            runtime["interface_attachments"] = attachments_list

            # Happy path: drop the TAP from the runtime's tap_names so
            # stop-time sweep doesn't double-process. Forced fallback:
            # leave the TAP entry in place so stop-time cleanup still
            # removes the kernel object on node restart.
            if not forced_fallback:
                tap_names = [
                    t for t in (runtime.get("tap_names") or []) if t != tap
                ]
                runtime["tap_names"] = tap_names

            # Release the slot reservation so a future hot-add can pick
            # it up. Forced fallback also releases — the QEMU side still
            # holds the device, but our in-process tracking should
            # reflect "intent to remove" so a re-attach with a different
            # iface_index can use the slot pool. The QEMU device_add
            # would still collide with the stale guest device but that
            # is exactly the "restart node" message we surfaced.
            if slot is not None:
                allocated = list(runtime.get("allocated_slots") or [])
                runtime["allocated_slots"] = [
                    s for s in allocated if int(s) != int(slot)
                ]

        # Bump the generation so concurrent / future attach operations
        # observe a fresh ``current_attach_generation`` and a stale
        # rollback re-attach is invalidated.
        new_generation = self._bump_interface_attach_generation(
            runtime, int(interface_index)
        )
        self._persist_runtime(runtime)

        result: dict[str, Any] = {
            "state": "forced" if forced_fallback else "detached",
            "interface_index": int(interface_index),
            "tap_name": tap,
            "current_attach_generation": int(new_generation),
        }
        if device_already_gone:
            result["device_already_gone"] = True
        return result

    def _qemu_device_gone(self, socket_path: str, device_id: str) -> bool:
        """Return True iff ``query-pci`` shows no device with
        ``qdev_id == device_id`` anywhere in the bus->devices arrays.

        Used by US-304 hot-remove to bound-poll guest ejection.

        Codex hotfix MEDIUM-2: walks the FULL nested ``pci_bridge``
        subtree exhaustively. The previous implementation only walked
        the top-level ``bus.devices`` array and ONE ``pci_bridge.devices``
        level — for deeper PCIe topologies (bridge under bridge under
        bridge), it returned ``True`` (gone) too early, causing the
        detach path to run ``netdev_del`` / ``tap_del`` while the device
        was still present in QEMU. We now recurse into every nested
        ``pci_bridge.devices`` subtree until exhaustion.
        """
        response = self._qmp_command(socket_path, "query-pci")
        if not isinstance(response, dict):
            raise NodeRuntimeError(
                "QMP query-pci returned non-dict response during detach poll"
            )
        if "error" in response:
            raise NodeRuntimeError(
                f"QMP query-pci returned error during detach poll: "
                f"{response['error']}"
            )
        buses = response.get("return")
        if not isinstance(buses, list):
            return True  # malformed but device certainly not present

        def _device_present_in_subtree(devices: Any) -> bool:
            """Recurse through ``devices`` and any nested
            ``pci_bridge.devices`` subtrees until exhaustion.
            """
            if not isinstance(devices, list):
                return False
            for device in devices:
                if not isinstance(device, dict):
                    continue
                if str(device.get("qdev_id") or "") == device_id:
                    return True
                bridge = device.get("pci_bridge")
                if isinstance(bridge, dict):
                    if _device_present_in_subtree(bridge.get("devices")):
                        return True
            return False

        for bus in buses:
            if not isinstance(bus, dict):
                continue
            if _device_present_in_subtree(bus.get("devices")):
                return False
        return True

    def _publish_node_warning(
        self,
        *,
        lab_id: str,
        node_id: int,
        interface_index: int,
        message: str,
    ) -> None:
        """US-304 forced-fallback: publish a ``node_warning`` WS event so
        the UI can surface the "guest did not eject" condition.

        Goes through :class:`app.services.ws_hub.ws_hub` (the existing
        per-lab event hub) — there is no separate ``ws_manager`` service.
        Fire-and-forget: we schedule the coroutine on the running event
        loop if one is available, otherwise we fall back to
        ``asyncio.run`` so sync callers still get the broadcast.
        """
        from app.services.ws_hub import ws_hub  # local import — cycle-free

        payload = {
            "node_id": int(node_id),
            "interface_index": int(interface_index),
            "message": message,
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(ws_hub.publish(lab_id, "node_warning", payload))
            return
        # No running loop — synchronous caller. Run the publish in a
        # short-lived loop so the event still goes out.
        asyncio.run(ws_hub.publish(lab_id, "node_warning", payload))

    def _qmp_command(
        self, socket_path: str, command: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a QMP command with optional arguments and return the parsed
        response dict. Wraps :func:`_default_qmp_client` so tests can
        monkey-patch the underlying transport via ``self._qmp_client``.

        US-303 codex iter1 HIGH-1: socket-level errors / timeouts
        (``OSError``, :class:`socket.timeout`) are wrapped in
        :class:`NodeRuntimeQMPTimeout` so the hot-add rollback dispatcher
        can recognise the ambiguous "may have applied in QEMU" case and
        run the FULL idempotent rollback chain. In-band command errors
        (``response["error"]``) are still returned verbatim — they are
        unambiguous failures.
        """
        # The default_qmp_client signature ``(socket_path, command)`` does
        # not accept arguments; for commands needing arguments we go
        # through a lightweight inline path (``netdev_add``, ``device_add``,
        # ``netdev_del``, ``device_del``). Tests can monkey-patch
        # ``self._qmp_client`` to a callable accepting
        # ``(socket_path, command, arguments)`` to capture both pieces.
        client = self._qmp_client
        try:
            try:
                # Test-injected client may accept a 3rd positional arg.
                return client(socket_path, command, arguments) if arguments else client(socket_path, command)  # type: ignore[call-arg]
            except TypeError:
                # Fall back to default 2-arg signature: encode arguments inline
                # via the bare socket protocol.
                return _qmp_send_with_args(socket_path, command, arguments)
        except (OSError, socket.timeout) as exc:
            # Transport-level failure: socket closed mid-flight, host
            # network blip, command exceeded ``sock.settimeout``. We do
            # NOT know whether QEMU applied the command, so the caller
            # MUST run the full rollback chain.
            raise NodeRuntimeQMPTimeout(
                f"QMP {command} transport error on {socket_path}: {exc}"
            ) from exc

    def set_qemu_nic_link(
        self, lab_id: str, node_id: int, interface_index: int, *, up: bool
    ) -> tuple[bool, str | None]:
        """Best-effort QMP ``set_link`` for ``net{interface_index}``.

        Returns ``(ok, reason)``. ``ok`` is True only when QEMU returned a
        successful (non-error) response. The QMP ``name`` is the **netdev
        id** (``net{index}``) which exists on both boot-time and hot-added
        NICs, so this works for hubport-backed and tap-backed NICs alike.

        The runtime mutex is intentionally NOT acquired — set_link is
        idempotent and a missed flip during a concurrent attach will be
        corrected by the caller's own attach/detach path or the next
        reconcile pass.
        """
        runtime = self._runtime_record(lab_id, node_id)
        if runtime is None:
            return False, "no runtime record"
        if str(runtime.get("kind") or "") != "qemu":
            return False, f"runtime kind={runtime.get('kind')!r}"
        socket_path = runtime.get("qmp_socket") or ""
        if not socket_path:
            work_dir = runtime.get("work_dir")
            socket_path = str(Path(work_dir) / "qmp.sock") if work_dir else ""
        if not socket_path:
            return False, "no qmp socket"
        try:
            response = self._qmp_command(
                socket_path,
                "set_link",
                {"name": f"net{int(interface_index)}", "up": bool(up)},
            )
        except NodeRuntimeQMPTimeout as exc:
            return False, f"qmp transport: {exc}"
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            return False, f"qmp error: {response['error']}"
        return True, None

    def _find_free_pcie_slot(
        self,
        socket_path: str,
        max_nics: int,
        *,
        reserved_slots: list[int] | None = None,
    ) -> int | None:
        """Scan ``query-pci`` for the highest free pcie-root-port (US-301
        policy: hot-add scans descending so additions never collide with
        the boot-time positional layout ``rp0..rp{N-1}``).

        Returns the slot index (0-based, matching ``rp{i}`` ids) or
        ``None`` if every pre-allocated slot is occupied.

        ``reserved_slots`` (US-303 codex iter1 HIGH-2) lists slots
        already reserved in-runtime by an earlier (still-pending) hot-add
        on this same VM. We treat them as occupied even if ``query-pci``
        has not yet observed the device — covers the window between
        slot-pick and ``device_add`` where two concurrent attaches on
        the same VM (different ifaces) would otherwise pick the same
        slot.

        QMP ``query-pci`` returns a list of bus dicts; each bus has
        ``devices`` with the actual NIC devices, plus ``pci_bridge`` for
        root ports. We walk the tree looking for ``rp{i}`` ids whose
        ``pci_bridge.devices`` is empty.
        """
        response = self._qmp_client(socket_path, "query-pci")
        if not isinstance(response, dict):
            raise NodeRuntimeError("QMP query-pci returned non-dict response")
        if "error" in response:
            raise NodeRuntimeError(
                f"QMP query-pci returned error: {response['error']}"
            )
        buses = response.get("return")
        if not isinstance(buses, list):
            raise NodeRuntimeError(
                "QMP query-pci returned no bus data (return field missing)"
            )

        occupied: set[int] = set()
        for bus in buses:
            if not isinstance(bus, dict):
                continue
            for device in bus.get("devices") or []:
                if not isinstance(device, dict):
                    continue
                qdev_id = device.get("qdev_id")
                if isinstance(qdev_id, str) and qdev_id.startswith("rp"):
                    bridge = device.get("pci_bridge")
                    if isinstance(bridge, dict):
                        children = bridge.get("devices") or []
                        if children:
                            try:
                                occupied.add(int(qdev_id[2:]))
                            except ValueError:
                                continue

        # Treat in-runtime reservations as occupied so a concurrent
        # attach that has already picked a slot but not yet flushed
        # device_add to QEMU does not collide.
        for reserved in reserved_slots or []:
            try:
                occupied.add(int(reserved))
            except (TypeError, ValueError):
                continue

        # Descending scan: pick the highest free slot.
        for slot_index in range(int(max_nics) - 1, -1, -1):
            if slot_index not in occupied:
                return slot_index
        return None

    def _resolve_qemu_nic_model(
        self, lab_id: str, node_id: int, runtime: dict[str, Any]
    ) -> str:
        """Resolve the QEMU NIC model from the lab.json node extras.

        Mirrors the boot-path resolution at ``_start_qemu_node`` line ~954:
        ``_extra_str(extras, "qemu_nic") or "e1000"``. Boot and hot-add
        MUST use the same model — Codex critic finding #4 (mixing types
        confuses guest interface ordering).

        Reads lab.json on demand via :class:`LabService` to avoid stamping
        the model into the runtime record at start-time (which would
        require a migration for already-running VMs).
        """
        try:
            from app.services.lab_service import LabService  # noqa: WPS433
        except ImportError:
            return "e1000"

        # Locate the lab.json by lab_id. Walk LABS_DIR for a matching id.
        try:
            settings = get_settings()
        except Exception:  # noqa: BLE001
            return "e1000"

        labs_dir = Path(settings.LABS_DIR)
        if not labs_dir.exists():
            return "e1000"

        for path in labs_dir.glob("*.json"):
            try:
                data = LabService.read_lab_json_static(path.name)
            except Exception:  # noqa: BLE001
                continue
            if str(data.get("id") or "") != lab_id:
                continue
            node = data.get("nodes", {}).get(str(node_id))
            if not isinstance(node, dict):
                return "e1000"
            extras = _node_extras(node)
            return _extra_str(extras, "qemu_nic") or "e1000"
        return "e1000"

    def _resolve_qemu_planned_mac(
        self,
        lab_id: str,
        node_id: int,
        runtime: dict[str, Any],
        interface_index: int,
    ) -> str:
        """Resolve the planned MAC for a QEMU interface.

        Resolution chain (mirrors the boot path):
          1. ``node.interfaces[i].planned_mac`` if explicitly set.
          2. ``_mac_for_index(node.firstmac, interface_index)``.
        Returns "" if neither source is available — caller may pass an
        empty string to QMP, in which case QEMU assigns a random MAC
        (acceptable for tests; real callers should always have firstmac).
        """
        try:
            from app.services.lab_service import LabService  # noqa: WPS433
        except ImportError:
            return ""

        try:
            settings = get_settings()
        except Exception:  # noqa: BLE001
            return ""

        labs_dir = Path(settings.LABS_DIR)
        if not labs_dir.exists():
            return ""

        for path in labs_dir.glob("*.json"):
            try:
                data = LabService.read_lab_json_static(path.name)
            except Exception:  # noqa: BLE001
                continue
            if str(data.get("id") or "") != lab_id:
                continue
            node = data.get("nodes", {}).get(str(node_id))
            if not isinstance(node, dict):
                return ""
            iface = self._lookup_interface(node, interface_index)
            if iface and iface.get("planned_mac"):
                return str(iface["planned_mac"])
            extras = _node_extras(node)
            first_mac = node.get("firstmac") or extras.get("firstmac")
            return self._mac_for_index(first_mac, interface_index)
        return ""

    def _persist_planned_mac_to_lab_json(
        self,
        lab_id: str,
        node_id: int,
        interface_index: int,
        planned_mac: str,
    ) -> None:
        """US-303 codex iter1: persist the computed ``planned_mac`` onto
        ``node.interfaces[i].planned_mac`` in lab.json so live-MAC
        mismatch detection (``_read_qemu_live_mac``) can compare against
        the value we actually passed to ``device_add``.

        Without this, the ``firstmac`` default case leaves
        ``interface.planned_mac = None`` and the mismatch detector
        returns ``state="confirmed"`` against an empty string regardless
        of what the guest reports.

        Idempotent: a non-empty existing value is left untouched (the
        operator may have set an explicit MAC; we never overwrite it).
        """
        from app.services.lab_lock import lab_lock  # local import: cycle-free
        from app.services.lab_service import LabService  # noqa: WPS433

        try:
            settings = get_settings()
        except Exception:  # noqa: BLE001
            return
        labs_dir = Path(settings.LABS_DIR)
        if not labs_dir.exists():
            return

        # Find the lab.json file whose ``id`` matches lab_id.
        target: Optional[Path] = None
        for path in labs_dir.glob("*.json"):
            try:
                data = LabService.read_lab_json_static(path.name)
            except Exception:  # noqa: BLE001
                continue
            if str(data.get("id") or "") == lab_id:
                target = path
                break
        if target is None:
            return

        with lab_lock(target.name, labs_dir):
            data = LabService.read_lab_json_static(target.name)
            node = data.get("nodes", {}).get(str(int(node_id)))
            if not isinstance(node, dict):
                return
            iface = self._lookup_interface(node, int(interface_index))
            if not isinstance(iface, dict):
                return
            existing = iface.get("planned_mac")
            if existing:  # non-empty: respect operator-supplied MAC.
                return
            iface["planned_mac"] = str(planned_mac)
            # Strip the read-time topology shim so the writer does not
            # regenerate links[] from a stale snapshot.
            data.pop("topology", None)
            LabService.write_lab_json_static(target.name, data)

    def _docker_force_remove(self, docker_binary: str, container_name: str) -> None:
        """Force-remove a container, swallowing any error (best-effort cleanup)."""
        subprocess.run(
            [
                docker_binary,
                "--host",
                self.settings.DOCKER_HOST,
                "rm",
                "-f",
                container_name,
            ],
            capture_output=True,
            text=True,
        )

    def _docker_attachments(
        self, lab_data: dict[str, Any], node: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Resolve per-interface attachment records for a docker node.

        Each record is ``{interface_index, network_id, bridge_name}`` and
        the list is ordered by ``interface_index`` ascending — the same
        order used to drive the manual veth setup so ``eth0`` is always
        the first interface declared on the node.

        Skips interfaces with no resolvable network (no link, ``pnet``
        external network, or missing network record) — the container will
        simply have fewer interfaces than declared.
        """
        lab_id = self._lab_id(lab_data)
        networks = lab_data.get("networks", {}) or {}

        node_id = int(node.get("id", 0))
        link_map: dict[int, int] = {}
        for link in lab_data.get("links", []) or []:
            endpoints = (link.get("from") or {}, link.get("to") or {})
            node_endpoint = next(
                (
                    endpoint for endpoint in endpoints
                    if isinstance(endpoint, dict)
                    and "node_id" in endpoint
                    and int(endpoint.get("node_id", -1)) == node_id
                ),
                None,
            )
            network_endpoint = next(
                (
                    endpoint for endpoint in endpoints
                    if isinstance(endpoint, dict) and "network_id" in endpoint
                ),
                None,
            )
            if node_endpoint and network_endpoint:
                interface_index = int(node_endpoint.get("interface_index", 0))
                network_id = int(network_endpoint.get("network_id", 0))
                if network_id:
                    link_map[interface_index] = network_id

        attachments: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        for index, interface in enumerate(node.get("interfaces", []) or []):
            if not isinstance(interface, dict):
                continue
            interface_index = int(interface.get("index", index))
            if interface_index in seen_indices:
                continue
            network_id = int(interface.get("network_id") or 0)
            if not network_id:
                network_id = link_map.get(interface_index, 0)
            if not network_id:
                continue
            network = networks.get(str(network_id))
            if not isinstance(network, dict):
                continue
            network_type = str(network.get("type", "linux_bridge"))
            if network_type.startswith("pnet"):
                continue
            runtime_record = network.get("runtime") or {}
            bridge = runtime_record.get("bridge_name")
            if not bridge:
                # Pre-Wave-6 lab.json — derive the canonical name on the fly.
                bridge = host_net.bridge_name(lab_id, network_id)
            seen_indices.add(interface_index)
            attachments.append(
                {
                    "interface_index": interface_index,
                    "network_id": network_id,
                    "bridge_name": bridge,
                }
            )
        attachments.sort(key=lambda item: item["interface_index"])
        return attachments

    def _stop_qemu_runtime(self, runtime: dict[str, Any]) -> None:
        pid = runtime.get("pid")
        if not pid:
            self._sweep_qemu_taps(runtime)
            return

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._sweep_qemu_taps(runtime)
            self._unregister_pid(pid)
            return

        try:
            psutil.Process(pid).wait(timeout=5)
        except (psutil.Error, psutil.TimeoutExpired):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # US-302: sweep the per-NIC TAPs owned by this VM. Best-effort
        # via ``try_link_del`` so already-removed TAPs (cleanup sweeper
        # from US-206 ran first) do not raise.
        self._sweep_qemu_taps(runtime)

        # US-201/US-203: drop the QEMU pid from the registry synchronously
        # so a recycled pid cannot inherit this entry's authorization.
        self._unregister_pid(pid)

        # US-206: sweep per-node TAP/veth host-ends left behind by this QEMU node.
        # Best-effort — failures are logged inside sweep_node_host_ifaces.
        lab_id = runtime.get("lab_id", "")
        node_id = runtime.get("node_id")
        if lab_id and node_id is not None:
            host_net.sweep_node_host_ifaces(lab_id, int(node_id))

    @staticmethod
    def _sweep_qemu_taps(runtime: dict[str, Any]) -> None:
        for tap in runtime.get("tap_names", []) or []:
            host_net.try_link_del(tap)

    @staticmethod
    def _unregister_pid(pid: int | None) -> None:
        if not pid:
            return
        try:
            runtime_pids.unregister(int(pid))
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    def _stop_docker_runtime(self, runtime: dict[str, Any]) -> None:
        docker_binary = self._resolve_binary("docker")
        if not docker_binary:
            return

        container_name = runtime.get("container_name")
        if not container_name:
            return

        # Stop the console TCP forwarder before tearing down the container so
        # in-flight client connections see a clean refused-after-stop instead
        # of hanging until the proxy notices the netns is gone.
        proxy_pid = runtime.get("console_proxy_pid")
        if proxy_pid:
            try:
                host_net.console_proxy_stop(int(proxy_pid))
            except Exception:
                pass

        subprocess.run(
            [docker_binary, "--host", self.settings.DOCKER_HOST, "stop", "-t", "5", container_name],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [docker_binary, "--host", self.settings.DOCKER_HOST, "rm", "-f", container_name],
            capture_output=True,
            text=True,
        )

        # US-203: sweep veth host-ends owned by this container. The peer end
        # was renamed into the container netns and is destroyed when the
        # container exits, but the host end persists until we remove it.
        for host_end in runtime.get("veth_host_ends", []) or []:
            host_net.try_link_del(host_end)

        # US-201/US-203: unregister the PID from the runtime registry. Done
        # synchronously here (not deferred to the heartbeat) so a recycled
        # PID cannot reuse this entry's authorization.
        self._unregister_pid(runtime.get("pid"))

        # US-206: sweep any remaining veth host-ends for this node not already
        # caught by the explicit ``veth_host_ends`` loop above (e.g. interfaces
        # created by hot-attach after start-time).  Best-effort.
        lab_id = runtime.get("lab_id", "")
        node_id = runtime.get("node_id")
        if lab_id and node_id is not None:
            host_net.sweep_node_host_ifaces(lab_id, int(node_id))

        # US-203: no Docker network record exists for nova-ve labs any
        # more. ``network_names`` is retained on the runtime record only
        # for backwards-compatibility with live-MAC reads — we do NOT
        # prune any docker network here.

    def _ensure_qemu_overlay(self, work_dir: Path, node: dict[str, Any]) -> Path:
        overlay_path = work_dir / "virtioa.qcow2"
        if overlay_path.exists():
            return overlay_path

        base_image = self._resolve_qemu_image(node)
        qemu_img_binary = self._resolve_binary(self.settings.QEMU_IMG_BINARY)

        if base_image:
            if qemu_img_binary:
                result = subprocess.run(
                    [
                        qemu_img_binary,
                        "create",
                        "-f",
                        "qcow2",
                        "-b",
                        str(base_image),
                        "-F",
                        "qcow2",
                        str(overlay_path),
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    raise NodeRuntimeError(result.stderr.strip() or "Failed to create QCOW2 overlay")
            else:
                shutil.copy2(base_image, overlay_path)
            return overlay_path

        if self._resolve_qemu_iso(node):
            if not qemu_img_binary:
                raise NodeRuntimeError(
                    f"qemu-img binary required to create blank install disk: {self.settings.QEMU_IMG_BINARY}"
                )
            result = subprocess.run(
                [qemu_img_binary, "create", "-f", "qcow2", str(overlay_path), "10G"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise NodeRuntimeError(result.stderr.strip() or "Failed to create blank QCOW2 install disk")
            return overlay_path

        raise NodeRuntimeError(f"QEMU base image not found for node image: {node.get('image')}")

    def _resolve_qemu_image(self, node: dict[str, Any]) -> Path | None:
        image_name = str(node.get("image", "")).strip()
        if not image_name:
            return None

        candidates = [
            self.settings.IMAGES_DIR / "qemu" / image_name / "hda.qcow2",
            self.settings.IMAGES_DIR / image_name / "hda.qcow2",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        for directory in [self.settings.IMAGES_DIR / "qemu" / image_name, self.settings.IMAGES_DIR / image_name]:
            if directory.exists():
                qcow_images = sorted(directory.glob("*.qcow2"))
                if qcow_images:
                    return qcow_images[0]

        return None

    def _resolve_qemu_iso(self, node: dict[str, Any]) -> Path | None:
        image_name = str(node.get("image", "")).strip()
        if not image_name:
            return None

        candidates = [
            self.settings.IMAGES_DIR / "qemu" / image_name / "cdrom.iso",
            self.settings.IMAGES_DIR / image_name / "cdrom.iso",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        for directory in [self.settings.IMAGES_DIR / "qemu" / image_name, self.settings.IMAGES_DIR / image_name]:
            if directory.exists():
                iso_images = sorted(directory.glob("*.iso"))
                if iso_images:
                    return iso_images[0]

        return None

    def _runtime_metrics(self, runtime: dict[str, Any]) -> dict[str, Any]:
        metrics = {
            "cpu_usage": 0,
            "ram_usage": 0,
            "disk_usage": self._disk_usage(Path(runtime.get("overlay_path", ""))),
        }

        pid = runtime.get("pid")
        if not pid:
            return metrics

        try:
            process = psutil.Process(pid)
            metrics["cpu_usage"] = int(process.cpu_percent(interval=0.0))
            metrics["ram_usage"] = process.memory_info().rss
        except psutil.Error:
            return metrics
        return metrics

    def _read_qemu_logs(self, runtime: dict[str, Any], tail: int) -> str:
        stdout_text = self._tail_text(Path(runtime["stdout_log"]), tail)
        stderr_text = self._tail_text(Path(runtime["stderr_log"]), tail)
        combined = []
        if stdout_text:
            combined.append(stdout_text)
        if stderr_text:
            combined.append(stderr_text)
        return "\n".join(combined)

    def _read_docker_logs(self, runtime: dict[str, Any], tail: int) -> str:
        docker_binary = self._resolve_binary("docker")
        if not docker_binary or not runtime.get("container_name"):
            return ""

        result = subprocess.run(
            [
                docker_binary,
                "--host",
                self.settings.DOCKER_HOST,
                "logs",
                "--tail",
                str(tail),
                runtime["container_name"],
            ],
            capture_output=True,
            text=True,
        )
        return (result.stdout + result.stderr).strip()

    def _is_docker_running(self, runtime: dict[str, Any]) -> bool:
        docker_binary = self._resolve_binary("docker")
        if not docker_binary or not runtime.get("container_name"):
            return False

        result = subprocess.run(
            [
                docker_binary,
                "--host",
                self.settings.DOCKER_HOST,
                "inspect",
                "-f",
                "{{.State.Running}}",
                runtime["container_name"],
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _docker_container_pid(self, docker_binary: str, container_name: str) -> int | None:
        result = subprocess.run(
            [
                docker_binary,
                "--host",
                self.settings.DOCKER_HOST,
                "inspect",
                "-f",
                "{{.State.Pid}}",
                container_name,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        try:
            pid = int(result.stdout.strip())
        except ValueError:
            return None
        return pid or None

    def _console_url(self, runtime: dict[str, Any] | None) -> str:
        if not runtime:
            return "/html5/#/client/unknowntoken"

        connection = base64.b64encode(
            f"{runtime['lab_id']}:{runtime['node_id']}:{runtime.get('console_port', 0)}".encode()
        ).decode()
        token = hashlib.sha256(
            f"{runtime['lab_id']}:{runtime['node_id']}:{runtime.get('started_at', 0)}".encode()
        ).hexdigest().upper()
        return f"/html5/#/client/{connection}?token={token}"

    def _allocate_console_port(self, console_mode: str) -> int:
        if console_mode == "vnc":
            for port in range(5900, 6000):
                if self._port_available(port):
                    return port
            raise NodeRuntimeError("No VNC console ports available")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    @staticmethod
    def _resolve_binary(binary: str) -> str | None:
        if Path(binary).exists():
            return binary
        return shutil.which(binary)

    def _resolve_qemu_binary(self, arch: str) -> str | None:
        configured = self.settings.QEMU_BINARY
        if not arch or arch == "x86_64":
            return self._resolve_binary(configured)
        candidate = f"qemu-system-{arch}"
        return self._resolve_binary(candidate) or self._resolve_binary(configured)

    @staticmethod
    def _container_console_port(console_mode: str) -> int:
        if console_mode == "rdp":
            return 3389
        if console_mode == "vnc":
            return 5900
        return 23

    @staticmethod
    def _container_name(lab_id: str, node_id: int) -> str:
        safe_lab_id = lab_id.replace("-", "")[:12]
        return f"nova-ve-{safe_lab_id}-{node_id}"

    def _docker_network_specs(self, lab_data: dict[str, Any], node: dict[str, Any]) -> list[dict[str, Any]]:
        lab_id = self._lab_id(lab_data)
        networks = lab_data.get("networks", {})
        seen: set[int] = set()
        specs: list[dict[str, Any]] = []

        node_id = int(node.get("id", 0))
        link_map: dict[int, int] = {}
        for link in lab_data.get("links", []) or []:
            endpoints = (link.get("from") or {}, link.get("to") or {})
            node_endpoint = next(
                (
                    endpoint for endpoint in endpoints
                    if isinstance(endpoint, dict)
                    and "node_id" in endpoint
                    and int(endpoint.get("node_id", -1)) == node_id
                ),
                None,
            )
            network_endpoint = next(
                (
                    endpoint for endpoint in endpoints
                    if isinstance(endpoint, dict) and "network_id" in endpoint
                ),
                None,
            )
            if node_endpoint and network_endpoint:
                interface_index = int(node_endpoint.get("interface_index", 0))
                network_id = int(network_endpoint.get("network_id", 0))
                if network_id:
                    link_map[interface_index] = network_id

        for index, interface in enumerate(node.get("interfaces", [])):
            interface_index = int(interface.get("index", index)) if isinstance(interface, dict) else index
            network_id = int(interface.get("network_id") or 0) if isinstance(interface, dict) else 0
            if not network_id:
                network_id = link_map.get(interface_index, 0)
            if not network_id or network_id in seen:
                continue

            network = networks.get(str(network_id))
            if not network:
                continue

            network_type = str(network.get("type", "linux_bridge"))
            if network_type.startswith("pnet"):
                continue

            seen.add(network_id)
            specs.append(
                {
                    "id": network_id,
                    "name": self._docker_network_name(lab_id, network_id),
                    "internal": network_type.startswith("internal") or network_type.startswith("private"),
                }
            )

        return specs

    @staticmethod
    def _docker_network_name(lab_id: str, network_id: int) -> str:
        safe_lab_id = "".join(character for character in lab_id.lower() if character.isalnum())[:12]
        return f"nova-ve-{safe_lab_id}-net{network_id}"

    @staticmethod
    def _docker_network_alias(node: dict[str, Any]) -> str:
        name = str(node.get("name") or f"node-{node.get('id', 'x')}")
        cleaned = "".join(character.lower() if character.isalnum() else "-" for character in name)
        alias = "-".join(filter(None, cleaned.split("-")))
        return alias or f"node-{node.get('id', 'x')}"

    @staticmethod
    def _mac_for_index(first_mac: str | None, index: int) -> str:
        if not first_mac:
            return f"52:54:00:00:{index // 256:02x}:{index % 256:02x}"

        parts = [int(part, 16) for part in first_mac.split(":")]
        value = int("".join(f"{part:02x}" for part in parts), 16) + index
        mac_hex = f"{value:012x}"
        return ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2))

    def _node_data(self, lab_data: dict[str, Any], node_id: int) -> dict[str, Any]:
        node = lab_data.get("nodes", {}).get(str(node_id))
        if not node:
            raise NodeRuntimeError(f"Node does not exist: {node_id}")
        return node

    @staticmethod
    def _lab_id(lab_data: dict[str, Any]) -> str:
        lab_id = str(lab_data.get("id", "")).strip()
        if not lab_id:
            raise NodeRuntimeError("Lab is missing an id")
        return lab_id

    @staticmethod
    def _key(lab_id: str, node_id: int) -> str:
        return f"{lab_id}:{node_id}"

    def _work_dir(self, lab_id: str, node_id: int) -> Path:
        return self.settings.TMP_DIR / lab_id / str(node_id)

    def _state_path(self, lab_id: str, node_id: int) -> Path:
        return self.runtime_dir / f"{lab_id}-{node_id}.json"

    def _overlay_path(self, lab_id: str, node_id: int) -> Path:
        return self._work_dir(lab_id, node_id) / "virtioa.qcow2"

    @staticmethod
    def _disk_usage(path: Path) -> str:
        if not path.exists():
            return "0.0000"
        return f"{path.stat().st_size / (1024 ** 2):.4f}"

    @staticmethod
    def _tail_text(path: Path, tail: int) -> str:
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-tail:])
