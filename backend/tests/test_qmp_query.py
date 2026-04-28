# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/qmp_query.py — US-300.

Acceptance criteria covered:
  - test_handshake_negotiation    : greeting + qmp_capabilities ack exchanged correctly
  - test_query_pci                : query-pci command returns mock response
  - test_query_rx_filter          : query-rx-filter command returns mock response
  - test_command_with_args        : command with arguments dict forwarded correctly
  - test_missing_args_cli         : <2 CLI args → exit 1
  - test_nonexistent_socket_cli   : missing socket path → exit 2
  - test_invalid_args_json_cli    : malformed args-as-json → exit 1
  - test_args_json_not_dict_cli   : non-dict args-as-json → exit 1
  - test_error_response_printed   : QMP-level error envelope still printed on stdout
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path
from typing import Generator

import tempfile

import pytest

# Add repo scripts directory to path so we can import qmp_query as a module.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import qmp_query  # noqa: E402


# ---------------------------------------------------------------------------
# Mock QMP server fixture
# ---------------------------------------------------------------------------

_QMP_GREETING = json.dumps(
    {
        "QMP": {
            "version": {
                "qemu": {"micro": 0, "minor": 2, "major": 8},
                "package": "",
            },
            "capabilities": ["oob"],
        }
    }
)

_QMP_CAPS_ACK = json.dumps({"return": {}})


def _make_mock_server(
    sock_path: str,
    responses: list[str],
) -> tuple[socket.socket, threading.Thread]:
    """Spawn a background thread that acts as a minimal QMP server.

    The server:
    1. Sends the QMP greeting on connect.
    2. Reads (and discards) the ``qmp_capabilities`` line.
    3. Sends the capabilities ack.
    4. For each subsequent client line (command), pops and sends the next
       entry from *responses*.

    Returns ``(server_socket, thread)``.  The thread is daemon so it is
    collected automatically when the test process exits.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    server.settimeout(5)

    recorded_commands: list[dict] = []

    def _serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            wfile = conn.makefile("w", encoding="utf-8")
            rfile = conn.makefile("r", encoding="utf-8")

            # Step 1: send greeting
            wfile.write(_QMP_GREETING + "\n")
            wfile.flush()

            # Step 2: read qmp_capabilities
            caps_line = rfile.readline()
            if caps_line:
                recorded_commands.append(json.loads(caps_line))

            # Step 3: send ack
            wfile.write(_QMP_CAPS_ACK + "\n")
            wfile.flush()

            # Step 4: handle one command per response entry
            for resp in responses:
                cmd_line = rfile.readline()
                if cmd_line:
                    recorded_commands.append(json.loads(cmd_line))
                wfile.write(resp + "\n")
                wfile.flush()
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return server, thread, recorded_commands


@pytest.fixture()
def qmp_sock() -> Generator[str, None, None]:
    """Yield a short tmp socket path under /tmp (AF_UNIX has a 104-char limit on macOS)."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="qmp") as d:
        yield str(Path(d) / "q.sock")


# ---------------------------------------------------------------------------
# Helper: run one mock-server + qmp_command() round-trip
# ---------------------------------------------------------------------------


def _run_command(
    sock_path: str,
    cmd: str,
    args: dict | None,
    response: dict,
) -> tuple[dict, list[dict]]:
    """Start a mock server, send *cmd*, return (parsed_response, recorded_commands)."""
    resp_str = json.dumps(response)
    _server, thread, recorded = _make_mock_server(sock_path, [resp_str])
    result = qmp_query.qmp_command(sock_path, cmd, args)
    thread.join(timeout=3)
    return result, recorded


# ---------------------------------------------------------------------------
# Tests: qmp_command() function (unit)
# ---------------------------------------------------------------------------


def test_handshake_negotiation(qmp_sock: str) -> None:
    """The mock server receives qmp_capabilities before the actual command."""
    response = {"return": []}
    _server, thread, recorded = _make_mock_server(qmp_sock, [json.dumps(response)])

    qmp_query.qmp_command(qmp_sock, "query-pci")
    thread.join(timeout=3)

    # recorded[0] is qmp_capabilities, recorded[1] is query-pci
    assert len(recorded) == 2
    assert recorded[0] == {"execute": "qmp_capabilities"}
    assert recorded[1] == {"execute": "query-pci"}


def test_query_pci(qmp_sock: str) -> None:
    """query-pci response is parsed and returned."""
    mock_pci = {
        "return": [
            {
                "bus": 0,
                "devices": [
                    {
                        "bus": 0,
                        "slot": 0,
                        "function": 0,
                        "class_info": {"class": 1536, "desc": "Host bridge"},
                        "id": {"device": 4663, "vendor": 28672},
                    }
                ],
            }
        ]
    }
    result, _ = _run_command(qmp_sock, "query-pci", None, mock_pci)
    assert result == mock_pci
    assert isinstance(result["return"], list)
    assert result["return"][0]["bus"] == 0


def test_query_rx_filter(qmp_sock: str) -> None:
    """query-rx-filter with name argument returns mock NIC info."""
    mock_rx = {
        "return": [
            {
                "name": "net0",
                "promiscuous": False,
                "multicast": "normal",
                "unicast": "normal",
                "vlan": "normal",
                "broadcast-allowed": True,
                "multicast-overflow": False,
                "unicast-overflow": False,
                "main-mac": "52:54:00:12:34:56",
                "vlan-table": [],
                "unicast-table": ["52:54:00:12:34:56"],
                "multicast-table": [],
            }
        ]
    }
    result, recorded = _run_command(
        qmp_sock, "query-rx-filter", {"name": "net0"}, mock_rx
    )
    assert result == mock_rx
    # Verify arguments were forwarded
    cmd_sent = recorded[1]
    assert cmd_sent["execute"] == "query-rx-filter"
    assert cmd_sent.get("arguments") == {"name": "net0"}


def test_command_with_args(qmp_sock: str) -> None:
    """Arguments dict is forwarded in the JSON payload."""
    response = {"return": {}}
    _args = {"driver": "virtio-net-pci", "id": "dev2", "netdev": "net2", "bus": "rp2"}
    result, recorded = _run_command(qmp_sock, "device_add", _args, response)
    assert result == response
    cmd_sent = recorded[1]
    assert cmd_sent["execute"] == "device_add"
    assert cmd_sent["arguments"] == _args


def test_error_response_returned(qmp_sock: str) -> None:
    """QMP-level error envelope is returned as-is (not raised)."""
    error_resp = {
        "error": {
            "class": "DeviceNotFound",
            "desc": "Device 'dev99' not found",
        }
    }
    result, _ = _run_command(qmp_sock, "device_del", {"id": "dev99"}, error_resp)
    assert "error" in result
    assert result["error"]["class"] == "DeviceNotFound"


# ---------------------------------------------------------------------------
# Tests: main() CLI entry point
# ---------------------------------------------------------------------------


def test_missing_args_cli() -> None:
    """Fewer than 2 CLI arguments → exit code 1."""
    rc = qmp_query.main([])
    assert rc == 1

    rc = qmp_query.main(["/some/path"])
    assert rc == 1


def test_nonexistent_socket_cli(tmp_path: Path) -> None:
    """Non-existent socket path → exit code 2."""
    missing = str(tmp_path / "no-such.sock")
    rc = qmp_query.main([missing, "query-pci"])
    assert rc == 2


def test_invalid_args_json_cli(qmp_sock: str) -> None:
    """Malformed args-as-json → exit code 1 (before connecting)."""
    rc = qmp_query.main([qmp_sock, "query-pci", "not-valid-json{"])
    assert rc == 1


def test_args_json_not_dict_cli(qmp_sock: str) -> None:
    """args-as-json that is valid JSON but not an object → exit code 1."""
    rc = qmp_query.main([qmp_sock, "query-pci", '["a","b"]'])
    assert rc == 1


def test_cli_success_prints_json(
    qmp_sock: str, capsys: pytest.CaptureFixture
) -> None:
    """Successful CLI invocation prints JSON to stdout and returns 0."""
    response = {"return": [{"bus": 0, "devices": []}]}
    _server, thread, _ = _make_mock_server(qmp_sock, [json.dumps(response)])

    rc = qmp_query.main([qmp_sock, "query-pci"])
    thread.join(timeout=3)

    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed == response


def test_cli_error_response_printed(
    qmp_sock: str, capsys: pytest.CaptureFixture
) -> None:
    """Even QMP error envelopes are printed on stdout (not stderr) with exit 0."""
    error_resp = {"error": {"class": "CommandNotFound", "desc": "no such cmd"}}
    _server, thread, _ = _make_mock_server(qmp_sock, [json.dumps(error_resp)])

    rc = qmp_query.main([qmp_sock, "no-such-cmd"])
    thread.join(timeout=3)

    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert "error" in parsed
