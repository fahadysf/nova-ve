"""Unit tests for the Dynamips hypervisor client.

These tests stand up a real loopback TCP server that speaks the
Dynamips hypervisor reply format and verify the client's request/reply
parser. They do NOT require the ``dynamips`` binary; the goal is to
catch regressions in the protocol surface before any deploy.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from app.services.runtime.dynamips import (
    DynamipsError,
    DynamipsLauncher,
    HypervisorClient,
)


def _serve_replies(replies: list[bytes]) -> tuple[int, threading.Event]:
    """Spin up a one-shot loopback server that emits each reply per request.

    Returns the bound port and a ``done`` event the test can wait on.
    The server accepts one connection, then for each line the client
    sends it pops one entry from ``replies`` and writes it back.
    """
    done = threading.Event()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                buf = b""
                for reply in replies:
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    line, _, buf = buf.partition(b"\n")
                    conn.sendall(reply)
        finally:
            server.close()
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    return port, done


def test_request_parses_single_line_success() -> None:
    port, done = _serve_replies([b"100 OK\r\n"])
    client = HypervisorClient(port)
    try:
        reply = client.request("hypervisor version")
        assert reply.code == 100
        assert reply.ok is True
        assert reply.lines == ["OK"]
    finally:
        client.close()
    done.wait(2.0)


def test_request_parses_multiline_success() -> None:
    # Multi-line replies use the "CODE-text" continuation format until the
    # terminal "CODE text" line.
    port, done = _serve_replies(
        [b"100-line1\r\n100-line2\r\n100 final\r\n"]
    )
    client = HypervisorClient(port)
    try:
        reply = client.request("vm extract_idle_pc r1")
        assert reply.code == 100
        assert reply.lines == ["line1", "line2", "final"]
    finally:
        client.close()
    done.wait(2.0)


def test_request_raises_on_error_code() -> None:
    port, done = _serve_replies([b"209 invalid argument\r\n"])
    client = HypervisorClient(port)
    try:
        with pytest.raises(DynamipsError, match="209"):
            client.request("vm bogus_command r1")
    finally:
        client.close()
    done.wait(2.0)


def test_request_raises_on_malformed_reply() -> None:
    port, done = _serve_replies([b"garbage line\r\n"])
    client = HypervisorClient(port)
    try:
        with pytest.raises(DynamipsError, match="malformed"):
            client.request("hypervisor version")
    finally:
        client.close()
    done.wait(2.0)


def test_request_raises_on_premature_close() -> None:
    # Server immediately closes the connection without sending anything.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _run() -> None:
        try:
            conn, _ = server.accept()
            conn.close()
        finally:
            server.close()

    threading.Thread(target=_run, daemon=True).start()

    client = HypervisorClient(port)
    try:
        with pytest.raises(DynamipsError, match="closed the connection"):
            client.request("hypervisor version")
    finally:
        client.close()


# ---- Image-path resolution -----------------------------------------------


def test_resolve_image_path_accepts_flat_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    filename = "c7200-adventerprisek9-mz.124-24.T5.image"
    (tmp_path / filename).write_bytes(b"FAKE")

    resolved = DynamipsLauncher._resolve_image_path({"image": filename})
    assert resolved == tmp_path / filename


def test_resolve_image_path_accepts_eveng_subdir_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EVE-NG importer drops images at ``<root>/<key>/<key>.image`` —
    one image per subdirectory, where the subdir name equals the
    filename stem. The resolver must find images in this layout when
    the template stores only the bare filename.
    """
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    key = "c3725-adventerprisek9-mz.124-15.T14"
    filename = f"{key}.image"
    (tmp_path / key).mkdir()
    (tmp_path / key / filename).write_bytes(b"FAKE")

    resolved = DynamipsLauncher._resolve_image_path({"image": filename})
    assert resolved == tmp_path / key / filename


def test_resolve_image_path_prefers_flat_over_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the same filename exists in both layouts (rare but possible
    after a manual ``cp``), the flat-layout copy wins — matches the
    resolver's documented search order.
    """
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    key = "c7200-image"
    filename = f"{key}.image"
    (tmp_path / filename).write_bytes(b"FLAT")
    (tmp_path / key).mkdir()
    (tmp_path / key / filename).write_bytes(b"NESTED")

    resolved = DynamipsLauncher._resolve_image_path({"image": filename})
    assert resolved.read_bytes() == b"FLAT"


def test_resolve_image_path_absolute_passthrough(tmp_path: Path) -> None:
    image = tmp_path / "absolute-c7200.image"
    image.write_bytes(b"FAKE")
    resolved = DynamipsLauncher._resolve_image_path({"image": str(image)})
    assert resolved == image


def test_resolve_image_path_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    with pytest.raises(DynamipsError, match="not found"):
        DynamipsLauncher._resolve_image_path({"image": "missing.image"})


def test_resolve_image_path_no_image_field_raises() -> None:
    with pytest.raises(DynamipsError, match="no image path"):
        DynamipsLauncher._resolve_image_path({})
