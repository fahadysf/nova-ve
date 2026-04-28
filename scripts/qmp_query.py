#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-300 — QMP query helper for objective QEMU NIC verification.

Connects to a QEMU QMP Unix socket, performs the capability negotiation
handshake, sends a single command, and prints the JSON response to stdout.

Usage::

    scripts/qmp_query.py <socket-path> <command> [args-as-json]

Examples::

    scripts/qmp_query.py /var/lib/nova-ve/runtime/mylab/node-1/qmp.sock query-pci
    scripts/qmp_query.py /var/lib/nova-ve/runtime/mylab/node-1/qmp.sock query-rx-filter
    scripts/qmp_query.py /var/lib/nova-ve/runtime/mylab/node-1/qmp.sock device_add \\
        '{"driver":"virtio-net-pci","id":"dev2","netdev":"net2","bus":"rp2"}'

Socket path convention::

    /var/lib/nova-ve/runtime/<lab-id>/<node-id>/qmp.sock

The helper is designed to be executable from a tester's shell on the test VM
with no Python project setup — it uses only the stdlib.

QMP handshake (from QEMU Machine Protocol spec)::

    1. Server sends a greeting JSON line (QMP version info + capabilities).
    2. Client sends ``{"execute":"qmp_capabilities"}`` to leave capabilities
       negotiation mode.  Server responds with ``{"return": {}}``.
    3. Client may now send commands and read responses one-for-one.

Exit codes::

    0  — command response printed successfully
    1  — usage error
    2  — socket connection / protocol error
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path


def _connect_and_negotiate(sock_path: str) -> socket.socket:
    """Open the QMP socket and complete the capability handshake.

    Returns the connected socket in command mode.  Raises ``OSError`` if the
    socket cannot be opened; raises ``ValueError`` if the protocol handshake
    fails (e.g. truncated greeting).
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock_path)
    except OSError as exc:
        s.close()
        raise OSError(f"Cannot connect to QMP socket {sock_path!r}: {exc}") from exc

    rfile = s.makefile("r", encoding="utf-8")

    # Step 1: read the greeting line sent by QEMU on connect.
    greeting = rfile.readline()
    if not greeting:
        s.close()
        raise ValueError("QMP socket closed before sending greeting")

    # Step 2: leave capabilities negotiation mode.
    s.sendall(b'{"execute":"qmp_capabilities"}\n')
    ack = rfile.readline()
    if not ack:
        s.close()
        raise ValueError("QMP socket closed before sending capabilities ack")

    return s


def qmp_command(sock_path: str, cmd: str, args: dict | None = None) -> dict:
    """Send one QMP command and return the parsed response dict.

    Parameters
    ----------
    sock_path:
        Path to the QEMU QMP Unix domain socket.
    cmd:
        QMP command name, e.g. ``"query-pci"`` or ``"query-rx-filter"``.
    args:
        Optional arguments dict forwarded as the ``"arguments"`` field.

    Returns
    -------
    dict
        Parsed JSON response from QEMU (includes the ``"return"`` key on
        success, or ``"error"`` on QMP-level errors).
    """
    s = _connect_and_negotiate(sock_path)
    try:
        rfile = s.makefile("r", encoding="utf-8")
        payload: dict = {"execute": cmd}
        if args:
            payload["arguments"] = args
        s.sendall(json.dumps(payload).encode() + b"\n")
        response_line = rfile.readline().rstrip()
        if not response_line:
            raise ValueError("QMP socket closed before sending command response")
        return json.loads(response_line)
    finally:
        s.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns an exit code: 0 on success, 1 for usage errors, 2 for runtime
    errors.
    """
    args = argv if argv is not None else sys.argv[1:]

    if len(args) < 2:
        print(
            "Usage: qmp_query.py <socket-path> <command> [args-as-json]",
            file=sys.stderr,
        )
        return 1

    sock_path = args[0]
    cmd = args[1]
    cmd_args: dict | None = None

    if len(args) >= 3:
        try:
            cmd_args = json.loads(args[2])
        except json.JSONDecodeError as exc:
            print(f"Error: args-as-json is not valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(cmd_args, dict):
            print("Error: args-as-json must be a JSON object (dict)", file=sys.stderr)
            return 1

    if not Path(sock_path).exists():
        print(f"Error: socket path does not exist: {sock_path!r}", file=sys.stderr)
        return 2

    try:
        response = qmp_command(sock_path, cmd, cmd_args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())
