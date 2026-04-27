# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for WebSocket hub: seq counter, 200-event ring buffer, and replay."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

from app.main import app
from app.services import ws_hub as ws_hub_module
from app.services.ws_hub import WsHub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_hub() -> WsHub:
    """Return a brand-new WsHub and patch the singleton used by the router."""
    hub = WsHub(ring_size=200)
    ws_hub_module.ws_hub = hub
    # Also patch the reference already imported inside app.routers.ws
    import app.routers.ws as ws_router_mod
    ws_router_mod.ws_hub = hub
    return hub


def _make_fake_user():
    return SimpleNamespace(
        id=1,
        username="testuser",
        role="user",
        email="test@example.com",
        name="Test User",
        html5=True,
        folder="/",
    )


def _patch_auth_ok(monkeypatch):
    """Make the WebSocket auth path succeed without a real DB."""
    fake_user_obj = _make_fake_user()

    async def fake_validate_session(self, token, username):
        return fake_user_obj

    monkeypatch.setattr(
        "app.services.auth_service.AuthService.validate_session",
        fake_validate_session,
    )
    # Patch UserRead.model_validate to return the SimpleNamespace directly
    import app.routers.ws as ws_router_mod
    monkeypatch.setattr(
        "app.schemas.user.UserRead.model_validate",
        lambda obj: obj,
    )


def _cookies_with_session():
    return {"nova_session": "fake-token", "nova_user": "testuser"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unauth_close_1008():
    """Connecting without a session cookie must result in close code 1008."""
    _fresh_hub()
    client = TestClient(app, raise_server_exceptions=False)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/labs/testlab"):
            pass
    assert exc_info.value.code == 1008


def test_seq_replay_buffer(monkeypatch):
    """Publish 3 events then connect with last_seq=0; receive hello + 3 replayed events."""
    hub = _fresh_hub()
    _patch_auth_ok(monkeypatch)

    # Publish 3 events synchronously using asyncio.run on a fresh loop
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(hub.publish("X", "link_created", {"a": 1}))
        loop.run_until_complete(hub.publish("X", "link_created", {"a": 2}))
        loop.run_until_complete(hub.publish("X", "link_created", {"a": 3}))
    finally:
        loop.close()

    client = TestClient(app, raise_server_exceptions=True)
    with client.websocket_connect(
        "/ws/labs/X?last_seq=0", cookies=_cookies_with_session()
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["seq"] == 3  # head_seq after 3 publishes

        # Receive the 3 replayed events
        ev1 = ws.receive_json()
        ev2 = ws.receive_json()
        ev3 = ws.receive_json()
        assert ev1["seq"] == 1
        assert ev2["seq"] == 2
        assert ev3["seq"] == 3

        # Publish a 4th event; the subscriber (our WS) should receive it
        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(hub.publish("X", "link_created", {"a": 4}))
        finally:
            loop2.close()

        ev4 = ws.receive_json()
        assert ev4["seq"] == 4


def test_force_resnapshot_when_seq_too_old(monkeypatch):
    """Overflow ring (250 events), connect with last_seq=10; expect force_resnapshot + lab_topology."""
    hub = _fresh_hub()
    _patch_auth_ok(monkeypatch)

    # Stub LabService.read_lab_json_static to avoid filesystem dependency
    monkeypatch.setattr(
        "app.routers.ws.LabService.read_lab_json_static",
        lambda lab_id: {"id": lab_id, "nodes": {}, "networks": {}, "topology": []},
    )

    loop = asyncio.new_event_loop()
    try:
        for i in range(250):
            loop.run_until_complete(hub.publish("X", "node_updated", {"i": i}))
    finally:
        loop.close()

    # Ring has 200 events; oldest seq should be 51
    state = hub._labs["X"]
    ring_events = list(state.events)
    assert len(ring_events) == 200
    assert ring_events[0].seq == 51

    client = TestClient(app, raise_server_exceptions=True)
    with client.websocket_connect(
        "/ws/labs/X?last_seq=10", cookies=_cookies_with_session()
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"

        resnap = ws.receive_json()
        assert resnap["type"] == "force_resnapshot"

        topo = ws.receive_json()
        assert topo["type"] == "lab_topology"
        assert "payload" in topo


def test_seq_monotonic_per_lab():
    """Events for labs A and B each start at seq=1 and count up independently."""
    hub = _fresh_hub()

    loop = asyncio.new_event_loop()
    try:
        ev_a1 = loop.run_until_complete(hub.publish("A", "node_created", {"x": 1}))
        ev_b1 = loop.run_until_complete(hub.publish("B", "node_created", {"x": 1}))
        ev_a2 = loop.run_until_complete(hub.publish("A", "node_created", {"x": 2}))
        ev_b2 = loop.run_until_complete(hub.publish("B", "node_created", {"x": 2}))
    finally:
        loop.close()

    assert ev_a1.seq == 1
    assert ev_a2.seq == 2
    assert ev_b1.seq == 1
    assert ev_b2.seq == 2
    assert hub.head_seq("A") == 2
    assert hub.head_seq("B") == 2


def test_ring_evicts_oldest():
    """Publishing 250 events to a 200-ring leaves exactly 200 events (seq 51..250)."""
    hub = _fresh_hub()

    loop = asyncio.new_event_loop()
    try:
        for i in range(250):
            loop.run_until_complete(hub.publish("lab1", "tick", {"i": i}))
    finally:
        loop.close()

    state = hub._labs["lab1"]
    ring_events = list(state.events)
    assert len(ring_events) == 200
    assert ring_events[0].seq == 51
    assert ring_events[-1].seq == 250
