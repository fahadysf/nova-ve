import os
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROXY = Path(__file__).resolve().parents[2] / "deploy" / "nova-ve-console-proxy.py"


@pytest.fixture
def proxy(tmp_path):
    module = runpy.run_path(str(PROXY))
    module["main"].__globals__["_LOG_DIR"] = tmp_path / "diagnostics"
    return module["main"].__globals__


def test_console_proxy_diagnostics_append_for_the_same_node(proxy, tmp_path):
    log_id = "a" * 32
    proxy["_log_event"](log_id, "listening")
    proxy["_log_event"](log_id, "upstream-connect-failed", OSError(111, "fixture"))
    proxy["_log_event"]("b" * 32, "listening")

    related = tmp_path / "diagnostics" / f"{log_id}.log"
    text = related.read_text()
    assert "listening" in text
    assert "upstream-connect-failed errno=111" in text
    assert "fixture" not in text
    assert related.stat().st_mode & 0o777 == 0o600
    assert "upstream-connect-failed" not in (related.parent / ("b" * 32 + ".log")).read_text()


def test_console_proxy_diagnostics_refuse_nonfiles_and_bound_size(proxy, tmp_path):
    proxy["_MAX_LOG_BYTES"] = 256
    log_id = "c" * 32
    root = tmp_path / "diagnostics"
    root.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.write_text("keep")
    blocked = root / ("d" * 32 + ".log")
    blocked.symlink_to(other)

    proxy["_log_event"]("d" * 32, "listening")
    assert other.read_text() == "keep"
    for _ in range(30):
        proxy["_log_event"](log_id, "upstream-connect-failed", OSError(111, "fixture"))

    log = root / f"{log_id}.log"
    assert log.stat().st_size <= 256
    assert "upstream-connect-failed errno=111" in log.read_text()


def test_console_proxy_diagnostics_key_is_stable_and_console_specific():
    from app.services.host_net import console_proxy_log_id

    key = console_proxy_log_id("test-lab", 1)
    assert len(key) == 32
    assert int(key, 16) > 0
    assert key == console_proxy_log_id("test-lab", 1, "primary")
    assert key != console_proxy_log_id("test-lab", 1, "second")
    assert key != console_proxy_log_id("other-test-lab", 1)
    assert key != console_proxy_log_id("test-lab", 2)


def test_console_proxy_diagnostics_bound_the_number_of_logs(proxy, tmp_path):
    proxy["_MAX_LOG_FILES"] = 2
    root = tmp_path / "diagnostics"
    proxy["_log_event"]("a" * 32, "listening")
    proxy["_log_event"]("b" * 32, "listening")
    sentinel = root / "operator-note"
    sentinel.write_text("keep")
    oldest = root / ("a" * 32 + ".log")
    os.utime(oldest, (1, 1))
    proxy["_log_event"]("c" * 32, "listening")
    assert {path.stem for path in root.glob("*.log")} == {"b" * 32, "c" * 32}
    assert sentinel.read_text() == "keep"


def test_console_proxy_forwards_after_restart_and_keeps_diagnostics(tmp_path):
    wrapper = """
import runpy
import socket
import sys
from pathlib import Path

class LoopbackSocket(socket.socket):
    def bind(self, address):
        return super().bind(('127.0.0.1', address[1]))

script, log_root, listen, target, log_id = sys.argv[1:]
run = runpy.run_path(script)['main']
run.__globals__['_LOG_DIR'] = Path(log_root)
socket.socket = LoopbackSocket
sys.argv = [script, '0', listen, target, log_id]
raise SystemExit(run())
"""
    log_id = "e" * 32
    root = tmp_path / "diagnostics"
    log = root / f"{log_id}.log"
    wire_bytes = b"console-fixture-only"

    def wait_for_record(expected, count=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if log.exists() and log.read_text().count(expected) >= count:
                return
            time.sleep(0.02)
        pytest.fail(f"Proxy did not record {expected}.")

    with socket.socket() as target_reservation:
        target_reservation.bind(("127.0.0.1", 0))
        target_port = target_reservation.getsockname()[1]
    with socket.socket() as upstream, socket.socket() as reserve:
        upstream.settimeout(2)
        reserve.bind(("127.0.0.1", 0))
        listen_port = reserve.getsockname()[1]
        reserve.close()
        args = [sys.executable, "-c", wrapper, str(PROXY), str(root), str(listen_port), str(target_port), log_id]

        for generation in range(1, 3):
            proc = subprocess.Popen(args)
            try:
                wait_for_record("listening", generation)
                if generation == 1:
                    with socket.create_connection(("127.0.0.1", listen_port), timeout=2) as client:
                        assert client.recv(1) == b""
                    wait_for_record("upstream-connect-failed")
                    upstream.bind(("127.0.0.1", target_port))
                    upstream.listen(1)
                else:
                    with socket.create_connection(("127.0.0.1", listen_port), timeout=2) as client:
                        connection, _ = upstream.accept()
                        with connection:
                            connection.settimeout(2)
                            client.sendall(wire_bytes)
                            assert connection.recv(4096) == wire_bytes
                            connection.sendall(wire_bytes)
                            assert client.recv(4096) == wire_bytes
            finally:
                proc.terminate()
                proc.wait(timeout=3)
    assert wire_bytes not in log.read_bytes()


@pytest.mark.parametrize("mode", ["primary", "second"])
def test_recovery_uses_configured_vnc_port_and_preserves_log_identity(monkeypatch, mode):
    from app.services import host_net
    from app.services.node_runtime_service import NodeRuntimeService

    service = NodeRuntimeService()
    monkeypatch.setattr(service, "_registry", {})
    monkeypatch.setattr(service, "_persist_runtime", lambda _: None)
    path_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: str(path) == "/proc/58221" or path_is_dir(path))
    monkeypatch.setattr(host_net, "console_proxy_alive", lambda _: False)
    stopped = []
    monkeypatch.setattr(host_net, "console_proxy_stop", stopped.append)
    launched = []

    def spawn(**kwargs):
        launched.append(kwargs)
        return 58223

    monkeypatch.setattr(host_net, "console_proxy_start", spawn)
    runtime = {"pid": 58221, "console_port": 25001, "second_console_port": 25002}
    port_key = "console" if mode == "primary" else "second_console"
    pid_key = f"{port_key}_proxy_pid"
    runtime.update({port_key: "vnc", pid_key: 58222})

    updated = service._ensure_console_proxy("test-lab", 12, runtime, mode=mode, node_extras={"vnc_port": 5901})

    assert stopped == [58222]
    assert launched == [{
        "node_pid": 58221, "listen_port": runtime[f"{port_key}_port"], "target_port": 5901,
        "log_id": host_net.console_proxy_log_id("test-lab", 12, mode),
    }]
    assert updated[pid_key] == 58223
