# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-request context for code that has no ``Request`` in its signature (#297).

The audit trail records the client address of every action, but most route
handlers take no ``Request`` parameter — and many are called directly by
unit tests. A tiny pure-ASGI middleware stores the current connection scope
in a ContextVar instead, so any code on the request path can read it
without a signature change. ContextVars are copied into tasks at creation
and never leak between concurrent requests.

``client_ip`` resolves the real client address behind the local reverse
proxy. The backend listens on loopback, so for browser traffic the peer is
always 127.0.0.1 and only the proxy's ``X-Forwarded-For`` names the client.
Solely loopback peers are trusted to have set that header: a caller that
connects directly cannot choose the address the audit log records. Among
forwarded hops the right-most non-loopback address wins, because a trusted
proxy *appends* the address it saw — anything to its left may be spoofed.
"""

from __future__ import annotations

import contextvars
import ipaddress
from typing import Any, Mapping, Optional

_current_scope: "contextvars.ContextVar[Optional[Mapping[str, Any]]]" = contextvars.ContextVar(
    "nova_ve_request_scope", default=None
)


class RequestContextMiddleware:
    """Expose the current ASGI scope through :func:`current_scope`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = _current_scope.set(scope)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_scope.reset(token)


def current_scope() -> Optional[Mapping[str, Any]]:
    """The ASGI scope of the request being handled, or ``None`` off-request."""
    return _current_scope.get()


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_ipv4(value: str) -> bool:
    try:
        return ipaddress.IPv4Address(value) is not None
    except ValueError:
        return False


def _normalize_address(raw: Any) -> Optional[str]:
    """Accept ``1.2.3.4``, ``1.2.3.4:5678``, ``[::1]``, ``[::1]:5678``."""
    value = str(raw or "").strip().strip('"')
    if value.startswith("["):
        value = value[1:].partition("]")[0]
    else:
        host, separator, port = value.rpartition(":")
        if separator and port.isdigit() and _is_ipv4(host):
            value = host
    # RFC 7239 spelling for "the proxy does not know".
    return None if not value or value.lower() == "unknown" else value


def client_ip(scope: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Best-known address of the client behind the current request."""
    scope = scope if scope is not None else _current_scope.get()
    if not scope:
        return None

    client = scope.get("client") or ()
    peer = _normalize_address(client[0]) if client else None
    if peer is None or not _is_loopback(peer):
        # Direct caller: its own forwarded header is untrusted, so the
        # connection peer is the answer.
        return peer

    forwarded = [
        raw.decode("latin-1")
        for name, raw in scope.get("headers") or ()
        if name == b"x-forwarded-for"
    ]
    if not forwarded:
        return peer
    hops = [hop for value in forwarded for hop in value.split(",")]
    for hop in reversed(hops):
        address = _normalize_address(hop)
        if address and not _is_loopback(address):
            return address
    # Every hop is loopback (a same-host caller behind the proxy).
    return peer
