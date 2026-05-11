#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""TCP forwarder from host:LISTEN_PORT to a target:TARGET_PORT.

Used by the nova-ve runtime in two shapes:

1. **Docker / netns-confined target** — when the container was started with
   ``--network none`` (Wave-6 manual-veth path) the ``docker run -p`` flag
   does nothing because Docker only spawns its userland ``docker-proxy`` for
   containers with at least one Docker-managed network. Pass the container's
   init pid as ``<node_pid>`` and the worker enters that netns via setns(2)
   before dialing ``127.0.0.1:TARGET_PORT``.

2. **Dynamips / default-netns target (pid=0)** — dynamips binds its console
   on ``127.0.0.1`` only, which guacd (in its own compose container) cannot
   reach via ``host.docker.internal``. Pass ``0`` as ``<node_pid>`` and the
   worker skips setns and dials ``127.0.0.1:TARGET_PORT`` directly in the
   default netns — a plain TCP-to-TCP forwarder.

Listens on ``0.0.0.0:LISTEN_PORT`` in the default netns — same wildcard bind
the stock ``docker-proxy`` uses so guacd (running in its own compose
container) can reach the listener via ``host.docker.internal``. For each
accepted connection it forks a worker that splices bytes both ways with
select(2). The worker exits when either side closes; the parent keeps
accepting.

Invocation (root):
    nova-ve-console-proxy.py <node_pid> <listen_port> <target_port>
        node_pid = 0 → no setns (default netns target)
        node_pid > 1 → setns into /proc/<node_pid>/ns/net

The script blocks. The privileged helper detaches it via ``setsid`` + ``Popen``
and returns the spawned PID.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import signal
import socket
import sys
from typing import NoReturn

CLONE_NEWNET = 0x40000000


def _setns_to_pid(pid: int) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    fd = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)
    try:
        rc = libc.setns(fd, CLONE_NEWNET)
    finally:
        os.close(fd)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"setns(/proc/{pid}/ns/net, NEWNET) failed: {os.strerror(err)}")


def _splice(a: socket.socket, b: socket.socket) -> None:
    """Bidirectionally forward bytes between two sockets until either closes."""
    socks = [a, b]
    try:
        while True:
            readable, _, errored = select.select(socks, [], socks, None)
            if errored:
                return
            for src in readable:
                try:
                    chunk = src.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                dst = b if src is a else a
                try:
                    dst.sendall(chunk)
                except OSError:
                    return
    finally:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


def _serve_one(client: socket.socket, target_pid: int, target_port: int) -> NoReturn:
    """Worker: dial ``127.0.0.1:target_port`` and splice bytes to the client.

    When ``target_pid > 0`` the worker first enters that pid's network
    namespace so the dial happens inside the container. When
    ``target_pid == 0`` the dial is performed in the default netns —
    the dynamips path, where the console socket lives on the host's
    loopback and only needs to be re-published on ``0.0.0.0``.
    """
    try:
        if target_pid > 0:
            _setns_to_pid(target_pid)
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.connect(("127.0.0.1", target_port))
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        os._exit(1)
    _splice(client, upstream)
    os._exit(0)


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: nova-ve-console-proxy.py <pid> <listen_port> <target_port>", file=sys.stderr)
        return 2

    try:
        target_pid = int(sys.argv[1])
        listen_port = int(sys.argv[2])
        target_port = int(sys.argv[3])
    except ValueError:
        print("non-integer argument", file=sys.stderr)
        return 2

    # pid=0 is the no-setns sentinel (dynamips path); >1 is a real pid;
    # pid=1 (init) is rejected — never the legitimate target.
    if (target_pid != 0 and target_pid <= 1) or not (
        1024 <= listen_port <= 65535
    ) or not (1 <= target_port <= 65535):
        print("argument out of range", file=sys.stderr)
        return 2

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Bind 0.0.0.0 so guacd (in its own compose container) can reach this
    # listener via host.docker.internal — matches stock docker-proxy.
    listener.bind(("0.0.0.0", listen_port))
    listener.listen(64)

    # Reap zombie workers.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    while True:
        try:
            client, _addr = listener.accept()
        except OSError:
            continue
        # Refuse to forward if the target netns has gone away (container
        # died). Skipped for the pid=0 path — dynamips' default-netns
        # target has no netns to outlive.
        if target_pid > 0 and not os.path.exists(f"/proc/{target_pid}/ns/net"):
            try:
                client.close()
            except OSError:
                pass
            return 0
        worker = os.fork()
        if worker == 0:
            try:
                listener.close()
            except OSError:
                pass
            _serve_one(client, target_pid, target_port)
        else:
            try:
                client.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
