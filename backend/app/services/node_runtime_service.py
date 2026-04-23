import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import psutil

from app.config import get_settings


class NodeRuntimeError(Exception):
    pass


class NodeRuntimeService:
    _registry: dict[str, dict[str, Any]] = {}
    _loaded = False
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.settings = get_settings()
        self.runtime_dir = self.settings.TMP_DIR / "node-runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._load_registry()

    @classmethod
    def reset_registry(cls) -> None:
        with cls._lock:
            cls._registry.clear()
            cls._loaded = False

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

        with self._lock:
            self._registry[key] = runtime
        self._persist_runtime(runtime)
        return runtime

    def stop_node(self, lab_data: dict[str, Any], node_id: int) -> None:
        lab_id = self._lab_id(lab_data)
        runtime = self._runtime_record(lab_id, node_id)
        if not runtime:
            return

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

    def _load_registry(self) -> None:
        with self._lock:
            if self._loaded:
                return
            for state_file in self.runtime_dir.glob("*.json"):
                try:
                    runtime = json.loads(state_file.read_text())
                except json.JSONDecodeError:
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
        qemu_binary = self._resolve_binary(self.settings.QEMU_BINARY)
        if not qemu_binary:
            raise NodeRuntimeError(f"QEMU binary not found: {self.settings.QEMU_BINARY}")

        work_dir = self._work_dir(lab_id, node["id"])
        work_dir.mkdir(parents=True, exist_ok=True)
        overlay_path = self._ensure_qemu_overlay(work_dir, node)
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
            str(node.get("uuid") or f"{lab_id}-{node['id']}"),
            "-drive",
            f"file={overlay_path},if=virtio,cache=writeback,format=qcow2",
        ]

        if accel == "kvm":
            cmd += ["-cpu", "host,vmx=off,svm=off"]
        else:
            cmd += ["-cpu", "max"]

        if console_mode == "vnc":
            cmd += ["-vnc", f"127.0.0.1:{console_port - 5900}"]
        else:
            cmd += ["-serial", f"telnet:127.0.0.1:{console_port},server,nowait"]

        for index in range(int(node.get("ethernet", 0))):
            cmd += [
                "-netdev",
                f"user,id=net{index}",
                "-device",
                f"e1000,netdev=net{index},mac={self._mac_for_index(node.get('firstmac'), index)}",
            ]

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
            "work_dir": str(work_dir),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "command": cmd,
            "started_at": time.time(),
        }

    def _start_docker_node(self, lab_data: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        docker_binary = self._resolve_binary("docker")
        if not docker_binary:
            raise NodeRuntimeError("Docker binary not found")

        lab_id = self._lab_id(lab_data)
        console_mode = node.get("console", "rdp")
        console_port = self._allocate_console_port(console_mode)
        container_name = self._container_name(lab_id, node["id"])
        network_specs = self._docker_network_specs(lab_data, node)

        for spec in network_specs:
            self._ensure_docker_network(docker_binary, spec)

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
            "-p",
            f"{console_port}:{self._container_console_port(console_mode)}",
        ]

        if network_specs:
            cmd += [
                "--network",
                network_specs[0]["name"],
                "--network-alias",
                self._docker_network_alias(node),
            ]

        cmd += [
            str(node.get("image")),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise NodeRuntimeError(result.stderr.strip() or result.stdout.strip() or "Failed to start Docker container")

        for spec in network_specs[1:]:
            attach = subprocess.run(
                [
                    docker_binary,
                    "--host",
                    self.settings.DOCKER_HOST,
                    "network",
                    "connect",
                    "--alias",
                    self._docker_network_alias(node),
                    spec["name"],
                    container_name,
                ],
                capture_output=True,
                text=True,
            )
            if attach.returncode != 0:
                self._stop_docker_runtime(
                    {
                        "container_name": container_name,
                        "network_names": [item["name"] for item in network_specs],
                    }
                )
                raise NodeRuntimeError(
                    attach.stderr.strip() or attach.stdout.strip() or "Failed to attach Docker network"
                )

        pid = self._docker_container_pid(docker_binary, container_name)
        pid_create_time = None
        if pid:
            try:
                pid_create_time = psutil.Process(pid).create_time()
            except psutil.Error:
                pid = None

        work_dir = self._work_dir(lab_id, node["id"])
        work_dir.mkdir(parents=True, exist_ok=True)
        return {
            "lab_id": lab_id,
            "node_id": node["id"],
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
            "network_names": [spec["name"] for spec in network_specs],
            "started_at": time.time(),
        }

    def _stop_qemu_runtime(self, runtime: dict[str, Any]) -> None:
        pid = runtime.get("pid")
        if not pid:
            return

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            psutil.Process(pid).wait(timeout=5)
        except (psutil.Error, psutil.TimeoutExpired):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
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
        self._prune_docker_networks(docker_binary, runtime.get("network_names", []))

    def _ensure_qemu_overlay(self, work_dir: Path, node: dict[str, Any]) -> Path:
        overlay_path = work_dir / "virtioa.qcow2"
        if overlay_path.exists():
            return overlay_path

        base_image = self._resolve_qemu_image(node)
        if not base_image:
            raise NodeRuntimeError(f"QEMU base image not found for node image: {node.get('image')}")

        qemu_img_binary = self._resolve_binary(self.settings.QEMU_IMG_BINARY)
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

        for interface in node.get("interfaces", []):
            network_id = int(interface.get("network_id") or 0)
            if not network_id or network_id in seen:
                continue

            network = networks.get(str(network_id))
            if not network:
                continue

            network_type = str(network.get("type", "bridge"))
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
