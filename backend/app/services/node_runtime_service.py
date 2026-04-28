# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

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
from typing import Any, Callable, Iterator

import psutil

from app.config import get_settings
from app.services import host_net, runtime_pids


_DOCKER_RESTART_POLICIES = {"no", "on-failure", "unless-stopped", "always"}
_HEARTBEAT_INTERVAL_S: float = 5.0
_TRANSITION_SUPPRESS_S: float = 30.0

_logger = logging.getLogger("nova-ve.heartbeat")


def _node_extras(node: dict[str, Any]) -> dict[str, Any]:
    extras = node.get("extras")
    return dict(extras) if isinstance(extras, dict) else {}


def _extra_str(extras: dict[str, Any], key: str, default: str = "") -> str:
    value = extras.get(key, default)
    return "" if value is None else str(value).strip()


class NodeRuntimeError(Exception):
    pass


def _default_qmp_client(socket_path: str, command: str) -> dict:
    """Connect to a QEMU QMP socket, send `command`, and return the parsed response.

    Performs a minimal QMP handshake (read greeting, send qmp_capabilities, send command).
    Raises FileNotFoundError or OSError when the socket is missing/unreachable.
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
        sock.sendall(json.dumps({"execute": command}).encode("utf-8") + b"\n")
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

    def __init__(self) -> None:
        self.settings = get_settings()
        self.runtime_dir = self.settings.TMP_DIR / "node-runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        # Dependency-injectable hooks so tests can monkey-patch QMP/docker IO.
        self._qmp_client: Callable[[str, str], dict] = _default_qmp_client
        self._docker_inspect: Callable[[str, str, str], dict] = _default_docker_inspect
        self._load_registry()

    @classmethod
    def reset_registry(cls) -> None:
        with cls._lock:
            cls._registry.clear()
            cls._loaded = False
        cls._transition_timestamps.clear()

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
            runtime = self._start_qemu_node(lab_id, node)
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
        container_name = runtime.get("container_name")
        if not container_name:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": "docker runtime not started",
            }

        docker_binary = self._resolve_binary("docker") or "docker"
        try:
            inspected = self._docker_inspect(docker_binary, self.settings.DOCKER_HOST, container_name)
        except Exception as exc:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": f"docker inspect failed: {exc}",
            }

        target_network_name: str | None = None
        if interface and lab_data is not None:
            network_id = 0
            try:
                network_id = int(interface.get("network_id") or 0)
            except (TypeError, ValueError):
                network_id = 0
            if not network_id:
                try:
                    interface_index = int(interface.get("index", 0))
                except (TypeError, ValueError):
                    interface_index = 0
                node_id_value = 0
                try:
                    node_id_value = int(runtime.get("node_id", 0))
                except (TypeError, ValueError):
                    node_id_value = 0
                for link in lab_data.get("links") or []:
                    endpoints = (link.get("from") or {}, link.get("to") or {})
                    node_endpoint = next(
                        (
                            endpoint for endpoint in endpoints
                            if isinstance(endpoint, dict)
                            and "node_id" in endpoint
                            and int(endpoint.get("node_id", -1)) == node_id_value
                            and int(endpoint.get("interface_index", -1)) == interface_index
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
                        try:
                            network_id = int(network_endpoint.get("network_id", 0))
                        except (TypeError, ValueError):
                            network_id = 0
                        break
            if network_id:
                target_network_name = self._docker_network_name(lab_id, network_id)

        live_mac: str | None = None
        if target_network_name:
            networks = inspected.get("Networks") or {}
            entry = networks.get(target_network_name)
            if isinstance(entry, dict) and entry.get("MacAddress"):
                live_mac = str(entry["MacAddress"])
        if live_mac is None:
            top_mac = inspected.get("MacAddress")
            if top_mac:
                live_mac = str(top_mac)

        if not live_mac:
            return {
                "state": "unavailable",
                "planned_mac": planned_mac,
                "live_mac": None,
                "runtime_type": "docker",
                "reason": "docker inspect returned no MacAddress",
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

        return {
            "lab_id": lab_id,
            "node_id": node_id,
            "name": node.get("name", f"node-{node_id}"),
            "console": runtime.get("console", node.get("console", "telnet")),
            "host": host,
            "port": int(runtime.get("console_port", 0)),
            "url": self._console_url(runtime),
        }

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

    def _start_qemu_node(self, lab_id: str, node: dict[str, Any]) -> dict[str, Any]:
        extras = _node_extras(node)
        architecture = _extra_str(extras, "architecture") or "x86_64"
        qemu_binary = self._resolve_qemu_binary(architecture)
        if not qemu_binary:
            raise NodeRuntimeError(
                f"QEMU binary not found for arch {architecture}: {self.settings.QEMU_BINARY}"
            )

        work_dir = self._work_dir(lab_id, node["id"])
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
            f"type=pc,accel={accel}",
            "-smp",
            str(node.get("cpu", 1)),
            "-m",
            str(node.get("ram", 1024)),
            "-name",
            str(node.get("name", f"node-{node['id']}")),
            "-uuid",
            str(node.get("uuid") or extras.get("uuid") or f"{lab_id}-{node['id']}"),
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

        nic_model = _extra_str(extras, "qemu_nic") or "e1000"
        first_mac = node.get("firstmac") or extras.get("firstmac")
        for index in range(int(node.get("ethernet", 0))):
            cmd += [
                "-netdev",
                f"user,id=net{index}",
                "-device",
                f"{nic_model},netdev=net{index},mac={self._mac_for_index(first_mac, index)}",
            ]

        if iso_path:
            cmd += ["-cdrom", str(iso_path), "-boot", "order=dc"]

        extra_args = _extra_str(extras, "qemu_options")
        if extra_args:
            try:
                cmd += shlex.split(extra_args)
            except ValueError as exc:
                raise NodeRuntimeError(f"Invalid qemu_options: {exc}") from exc

        with stdout_log.open("ab") as stdout_handle, stderr_log.open("ab") as stderr_handle:
            process = subprocess.Popen(
                cmd,
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )

        time.sleep(0.1)
        if process.poll() is not None:
            error = self._tail_text(stderr_log, 40) or self._tail_text(stdout_log, 40)
            raise NodeRuntimeError(error or "QEMU exited immediately after start")

        process_info = psutil.Process(process.pid)

        # US-201/US-203: register the PID into the runtime registry BEFORE
        # any helper-verb call. QEMU does not use the helper today, but
        # US-303 (hot-attach) will, and the registry is the single source
        # of truth for pid authorization. Best-effort: a registry write
        # failure does not abort the QEMU start path because nothing in
        # the QEMU happy path needs the registry yet.
        try:
            runtime_pids.register(process.pid, "qemu", lab_id, int(node["id"]))
        except Exception:  # noqa: BLE001 — best-effort, see comment above
            pass

        return {
            "lab_id": lab_id,
            "node_id": node["id"],
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
            "started_at": time.time(),
        }

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
            return

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._unregister_pid(pid)
            return

        try:
            psutil.Process(pid).wait(timeout=5)
        except (psutil.Error, psutil.TimeoutExpired):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # US-201/US-203: drop the QEMU pid from the registry synchronously
        # so a recycled pid cannot inherit this entry's authorization.
        self._unregister_pid(pid)

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

    def _ensure_docker_network(self, docker_binary: str, spec: dict[str, Any]) -> None:
        inspect = subprocess.run(
            [
                docker_binary,
                "--host",
                self.settings.DOCKER_HOST,
                "network",
                "inspect",
                spec["name"],
            ],
            capture_output=True,
            text=True,
        )
        if inspect.returncode == 0:
            return

        create_cmd = [
            docker_binary,
            "--host",
            self.settings.DOCKER_HOST,
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            f"nova-ve.network_id={spec['id']}",
        ]
        if spec["internal"]:
            create_cmd.append("--internal")
        create_cmd.append(spec["name"])

        result = subprocess.run(create_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise NodeRuntimeError(result.stderr.strip() or result.stdout.strip() or "Failed to create Docker network")

    def _prune_docker_networks(self, docker_binary: str, network_names: list[str]) -> None:
        for network_name in network_names:
            inspect = subprocess.run(
                [
                    docker_binary,
                    "--host",
                    self.settings.DOCKER_HOST,
                    "network",
                    "inspect",
                    network_name,
                ],
                capture_output=True,
                text=True,
            )
            if inspect.returncode != 0:
                continue

            try:
                inspected = json.loads(inspect.stdout)
            except json.JSONDecodeError:
                continue

            containers = {}
            if inspected and isinstance(inspected, list):
                containers = inspected[0].get("Containers") or {}
            if containers:
                continue

            subprocess.run(
                [
                    docker_binary,
                    "--host",
                    self.settings.DOCKER_HOST,
                    "network",
                    "rm",
                    network_name,
                ],
                capture_output=True,
                text=True,
            )

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
