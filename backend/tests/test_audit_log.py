# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""User activity audit trail (#297).

Four layers, bottom up: the writer (row shaping, secret redaction,
best-effort persistence), client-address resolution behind the reverse
proxy, event emission from every audited handler, and the read API behind
System → Logs → Activity log. The autouse ``audit_events`` fixture
(conftest) is what the handlers produce in place of a database; tests that
exercise the real writer restore it explicitly.
"""

import asyncio
import inspect
import json
import logging
import re
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from app.config import get_settings
from app.core.security import hash_password
from app.database import engine, get_db
from app.dependencies import get_current_admin, get_current_user
from app.main import app
from app.models.audit import AuditLog
from app.models.user import User
from app.request_context import (
    RequestContextMiddleware,
    _current_scope,
    client_ip,
    current_scope,
)
from app.routers import folders, labs, links, manage, networks
from app.schemas.folder import FolderCreateRequest, FolderRenameRequest
from app.schemas.lab import LabMetaCreate, LabMetaUpdate
from app.services import audit_service
from app.services.audit_service import AuditEvent
from app.services.auth_service import AuthService
from app.services.cloud_inventory_service import SharedCloudReferenceError
from app.services.docker_image_service import DockerImageError
from app.services.html5_service import Html5SessionError
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.template_service import TemplateError

# Captured at import time — before the autouse fixture swaps it out.
_REAL_RECORD_BATCH = audit_service.record_batch


@pytest.fixture(autouse=True)
def _isolate_folder_locks(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={"LABS_DIR": tmp_path / "labs", "BASE_DATA_DIR": tmp_path})
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.folder_service.get_settings", lambda: settings)


def _real_writer(monkeypatch):
    monkeypatch.setattr(audit_service, "record_batch", _REAL_RECORD_BATCH)


def _of(audit_events, name):
    return [event for event in audit_events if event.event == name]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _row(event, **overrides):
    fields = {"created_at": 42, "ip": None}
    fields.update(overrides)
    return audit_service._to_row(event, **fields)


class TestWriter:
    async def test_record_builds_one_event(self, audit_events):
        await audit_service.record(
            "node.start",
            actor="alice",
            target_type="node",
            target_id=7,
            target_name="R1",
            lab_path="/lab.json",
            lab_name="Lab",
        )

        assert audit_events == [
            AuditEvent(
                event="node.start",
                actor="alice",
                target_type="node",
                target_id=7,
                target_name="R1",
                lab_path="/lab.json",
                lab_name="Lab",
            )
        ]

    @pytest.mark.parametrize(
        "bad", ["nodestart", "node", ".start", "Node.Start", "node.start.now", "", None]
    )
    def test_row_rejects_malformed_event_names(self, bad):
        with pytest.raises(ValueError):
            _row(AuditEvent(event=bad))

    async def test_malformed_event_is_a_programming_error_not_a_swallowed_failure(
        self, audit_events, monkeypatch
    ):
        """A bad name must surface in tests rather than vanish into the
        writer's best-effort handling."""
        _real_writer(monkeypatch)
        monkeypatch.setattr(audit_service, "async_session_maker", None)

        with pytest.raises(ValueError):
            await audit_service.record("nodestart", actor="alice")

    def test_row_splits_the_event_and_keeps_the_context(self):
        row = _row(
            AuditEvent(
                "auth.login_failed",
                actor=None,
                outcome="failure",
                target_type="user",
                target_id="root",
            ),
            ip="198.51.100.1",
        )

        assert row.event == "auth.login_failed"
        assert row.category == "auth"
        assert row.action == "login_failed"
        assert row.outcome == "failure"
        assert row.actor is None
        assert row.created_at == 42
        # The address is the request's — an event cannot carry its own.
        assert row.ip == "198.51.100.1"

    def test_row_cannot_be_broken_by_user_supplied_text(self):
        row = _row(
            AuditEvent(
                "auth.login_failed",
                target_id="x" * 500,
                target_name="bad\x00name\n",
                lab_path="labs/a.json",
                detail={"k": "v"},
            )
        )

        assert len(row.target_id) == 128
        assert row.target_name == "bad name"
        assert row.detail == '{"k": "v"}'

    def test_detail_stays_json_for_values_that_cannot_be_serialized(self):
        row = _row(AuditEvent("lab.move", detail={"path": object()}))

        assert json.loads(row.detail)["path"].startswith("<object")

    def test_credential_values_never_reach_the_row(self):
        detail = {
            "password": "hunter2",
            "session_token": "abc",
            "nested": {
                "client_secret": "s3",
                "password_hash": "h",
                "authorization": "Bearer x",
                "private_key": "pk",
            },
            "api_key": "k",
            "list": [{"cookie": "sid=1"}],
        }

        row = _row(AuditEvent("user.update", detail=detail))
        parsed = json.loads(row.detail)

        assert parsed == {
            "password": "[redacted]",
            "session_token": "[redacted]",
            "nested": {
                "client_secret": "[redacted]",
                "password_hash": "[redacted]",
                "authorization": "[redacted]",
                "private_key": "[redacted]",
            },
            "api_key": "[redacted]",
            "list": [{"cookie": "[redacted]"}],
        }
        for secret in ("hunter2", "abc", "s3", "Bearer x", "sid=1"):
            assert secret not in row.detail

    def test_flags_and_lookalike_keys_are_kept(self):
        detail = {
            "password_changed": True,
            "idempotency_key": "k-123",
            "keyboard": "layout",
            "tokens_used": 3,
        }

        row = _row(AuditEvent("user.update", detail=detail))

        assert json.loads(row.detail) == detail

    def test_oversized_detail_is_summarized(self):
        row = _row(AuditEvent("lab.wipe", detail={"big": "x" * 20000}))

        assert json.loads(row.detail) == {"truncated": True, "keys": ["big"]}

    def test_empty_detail_stays_null(self):
        assert _row(AuditEvent("auth.logout", actor="a")).detail is None
        assert _row(AuditEvent("auth.logout", actor="a", detail={})).detail is None


def _fake_session_maker(store, *, fail=False):
    class _Session:
        def add_all(self, rows):
            store["rows"] = list(rows)

        async def commit(self):
            if fail:
                raise RuntimeError("db down")
            store["committed"] = True

    class _Context:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *exc_info):
            return False

    return lambda: _Context()


class TestWriterPersistence:
    async def test_batch_commits_through_one_session(self, audit_events, monkeypatch):
        _real_writer(monkeypatch)
        store = {}
        monkeypatch.setattr(audit_service, "async_session_maker", _fake_session_maker(store))

        await audit_service.record_batch(
            [AuditEvent("node.stop", actor="alice"), AuditEvent("lab.stop", actor="alice")]
        )

        assert [row.event for row in store["rows"]] == ["node.stop", "lab.stop"]
        assert store["committed"] is True

    async def test_a_database_failure_never_reaches_the_caller(
        self, audit_events, monkeypatch, caplog
    ):
        _real_writer(monkeypatch)
        monkeypatch.setattr(
            audit_service, "async_session_maker", _fake_session_maker({}, fail=True)
        )

        with caplog.at_level(logging.ERROR, logger="app.services.audit_service"):
            await audit_service.record("auth.login", actor="alice")  # must not raise

        assert "could not persist 1 event(s): auth.login" in caplog.text

    async def test_empty_batch_touches_nothing(self, monkeypatch):
        _real_writer(monkeypatch)

        def _unexpected():
            raise AssertionError("no session may be opened for an empty batch")

        monkeypatch.setattr(audit_service, "async_session_maker", _unexpected)
        await audit_service.record_batch([])

    async def test_rows_carry_the_request_client_address(self, monkeypatch):
        _real_writer(monkeypatch)
        store = {}
        monkeypatch.setattr(audit_service, "async_session_maker", _fake_session_maker(store))
        token = _current_scope.set(
            {"client": ("127.0.0.1", 5), "headers": [(b"x-forwarded-for", b"203.0.113.9")]}
        )
        try:
            await audit_service.record("auth.login", actor="alice")
        finally:
            _current_scope.reset(token)

        assert store["rows"][0].ip == "203.0.113.9"
        assert abs(store["rows"][0].created_at - time.time()) < 5

    def test_writer_has_its_own_pool(self):
        """A handler backed by a DB session already holds a request-pool
        connection when it audits; the writer must not need a second one."""
        assert audit_service.async_session_maker.kw["bind"] is not engine


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


LAB = {
    "meta": {"name": "Audit Lab"},
    "nodes": {
        "1": {
            "id": 1,
            "name": "R1",
            "type": "qemu",
            "template": "csr",
            "interfaces": [{"index": 0, "name": "Gi0/1"}, {"index": 1, "name": "Gi0/2"}],
        },
        "2": {
            "id": 2,
            "name": "R2",
            "type": "docker",
            "template": "docker",
            "interfaces": [{"index": 0, "name": "eth0"}],
        },
    },
    "networks": {"5": {"id": 5, "name": "Lab LAN", "type": "linux_bridge"}},
}


class TestContextBuilders:
    def test_node_context_resolves_an_id_and_accepts_a_record(self):
        by_id = audit_service.node_context(LAB, 1, "lab.json")
        # Creation handlers pass the record they just built.
        by_record = audit_service.node_context(LAB, dict(LAB["nodes"]["1"]), "lab.json")

        assert by_id == by_record
        assert by_id["target_id"] == 1
        assert by_id["target_name"] == "R1"
        assert by_id["lab_path"] == "/lab.json"
        assert by_id["lab_name"] == "Audit Lab"
        assert by_id["detail"] == {"node_type": "qemu", "template": "csr"}

    def test_node_context_tolerates_a_missing_node(self):
        context = audit_service.node_context(LAB, 99, "lab.json")

        assert context["target_id"] == 99
        assert context["target_name"] is None
        assert context["detail"] == {"node_type": None, "template": None}

    def test_network_context(self):
        context = audit_service.network_context(
            LAB, 5, "lab.json", detail={"extra": 1}
        )

        assert context["target_id"] == 5
        assert context["target_name"] == "Lab LAN"
        assert context["detail"] == {"network_type": "linux_bridge", "extra": 1}

    @pytest.mark.parametrize(
        "link, expected",
        [
            (
                {"id": "l1", "from": {"node_id": 1, "interface_index": 0}, "to": {"node_id": 2, "interface_index": 0}},
                "R1:Gi0/1 ↔ R2:eth0",
            ),
            (
                {"id": "l2", "from": {"node_id": 1, "interface_index": 1}, "to": {"network_id": 5}},
                "R1:Gi0/2 ↔ Lab LAN",
            ),
            # Interface index beyond the node's list, and objects gone missing.
            (
                {"id": "l3", "from": {"node_id": 1, "interface_index": 7}, "to": {"network_id": 8}},
                "R1:eth7 ↔ network 8",
            ),
            ({"id": "l4", "from": {"node_id": 40}, "to": None}, "node 40:ethNone ↔ ?"),
        ],
    )
    def test_link_context_names_both_endpoints(self, link, expected):
        context = audit_service.link_context(LAB, link, "lab.json")

        assert context["target_name"] == expected
        assert context["detail"]["from"] == expected.split(" ↔ ")[0]
        assert context["detail"]["to"] == expected.split(" ↔ ")[1]

    @pytest.mark.parametrize(
        "path, expected",
        [("lab.json", "/lab.json"), ("/lanes/a/lab.json", "/lanes/a/lab.json"), ("", None), (None, None)],
    )
    def test_display_path(self, path, expected):
        assert audit_service.display_path(path) == expected

    def test_field_changes_reports_only_what_moved(self):
        before = {"name": "R1", "cpu": 1, "ram": 512, "interfaces": [1]}
        after = {"name": "R1", "cpu": 2, "ram": 512, "interfaces": [1, 2]}

        assert audit_service.field_changes(before, after, skip=("interfaces",)) == {
            "cpu": {"from": 1, "to": 2}
        }

    def test_field_changes_ignores_fields_the_payload_does_not_carry(self):
        """``after`` may be a response payload listing only returned fields."""
        before = {"name": "LAN", "type": "linux_bridge", "visibility": 1}

        assert audit_service.field_changes(before, {"name": "LAN"}) == {}


# ---------------------------------------------------------------------------
# Client-address resolution
# ---------------------------------------------------------------------------


def _scope(peer, forwarded=None):
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return {"type": "http", "client": peer, "headers": headers}


class TestClientIp:
    @pytest.mark.parametrize(
        "scope, expected",
        [
            (None, None),
            (_scope(None), None),
            # A direct caller's forwarded header is its own claim: ignored.
            (_scope(("198.51.100.4", 9), "1.1.1.1"), "198.51.100.4"),
            (_scope(("198.51.100.4", 9)), "198.51.100.4"),
            # Behind the local proxy.
            (_scope(("127.0.0.1", 9)), "127.0.0.1"),
            (_scope(("127.0.0.1", 9), "203.0.113.5"), "203.0.113.5"),
            (_scope(("::1", 9)), "::1"),
            (_scope(("::1", 9), "2001:db8::5"), "2001:db8::5"),
            # The proxy appends the address it saw; anything left of it may
            # be forged by the client.
            (_scope(("127.0.0.1", 9), "1.2.3.4, 203.0.113.5"), "203.0.113.5"),
            (_scope(("127.0.0.1", 9), "1.2.3.4, 5.6.7.8, 203.0.113.5"), "203.0.113.5"),
            # A further local hop (tunnel connector on the same host).
            (_scope(("127.0.0.1", 9), "203.0.113.5, 127.0.0.1"), "203.0.113.5"),
            # Nothing usable behind the proxy.
            (_scope(("127.0.0.1", 9), "unknown"), "127.0.0.1"),
            (_scope(("127.0.0.1", 9), "127.0.0.1, ::1"), "127.0.0.1"),
            # Address forms with a port or brackets.
            (_scope(("127.0.0.1", 9), "203.0.113.5:8123"), "203.0.113.5"),
            (_scope(("127.0.0.1", 9), "[2001:db8::5]:443"), "2001:db8::5"),
            (_scope(("127.0.0.1", 9), ' "203.0.113.5" '), "203.0.113.5"),
        ],
    )
    def test_resolution(self, scope, expected):
        assert client_ip(scope) == expected

    async def test_no_request_in_context_means_no_address(self):
        assert current_scope() is None
        assert client_ip() is None


def _mini_app():
    app_ = FastAPI()
    app_.add_middleware(RequestContextMiddleware)

    @app_.get("/ip")
    async def ip():
        return {"ip": client_ip()}

    @app_.get("/ip-after-wait")
    async def ip_after_wait():
        await asyncio.sleep(0.02)
        return {"ip": client_ip()}

    return app_


class TestRequestContextMiddleware:
    async def test_scope_is_cleared_even_when_the_request_fails(self):
        seen = {}

        async def endpoint(scope, receive, send):
            seen["during"] = current_scope()
            raise RuntimeError("boom")

        scope = {"type": "http", "client": ("127.0.0.1", 1), "headers": []}
        with pytest.raises(RuntimeError):
            await RequestContextMiddleware(endpoint)(scope, None, None)

        assert seen["during"] is scope
        assert current_scope() is None

    async def test_non_http_scopes_pass_through_untouched(self):
        seen = []

        async def endpoint(scope, receive, send):
            seen.append(current_scope())

        await RequestContextMiddleware(endpoint)({"type": "lifespan"}, None, None)

        assert seen == [None]

    async def test_handlers_see_the_client_behind_the_proxy(self):
        transport = ASGITransport(app=_mini_app(), client=("127.0.0.1", 4321))
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            via_proxy = await client.get("/ip", headers={"X-Forwarded-For": "203.0.113.5"})
            local = await client.get("/ip")

        assert via_proxy.json() == {"ip": "203.0.113.5"}
        assert local.json() == {"ip": "127.0.0.1"}

    async def test_direct_callers_cannot_choose_their_recorded_address(self):
        transport = ASGITransport(app=_mini_app(), client=("198.51.100.4", 4321))
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get("/ip", headers={"X-Forwarded-For": "1.1.1.1"})

        assert response.json() == {"ip": "198.51.100.4"}

    async def test_concurrent_requests_do_not_share_an_address(self):
        transport = ASGITransport(app=_mini_app(), client=("127.0.0.1", 4321))
        async with AsyncClient(transport=transport, base_url="http://t") as first, AsyncClient(
            transport=transport, base_url="http://t"
        ) as second:
            one, two = await asyncio.gather(
                first.get("/ip-after-wait", headers={"X-Forwarded-For": "203.0.113.1"}),
                second.get("/ip-after-wait", headers={"X-Forwarded-For": "203.0.113.2"}),
            )

        assert one.json() == {"ip": "203.0.113.1"}
        assert two.json() == {"ip": "203.0.113.2"}


# ---------------------------------------------------------------------------
# Database stand-ins — no Postgres in this suite
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


def _account(**overrides):
    base = {
        "username": "alice",
        "email": "alice@example.com",
        "name": "Alice",
        "role": "user",
        "extauth": "internal",
        "password_hash": hash_password("correct-horse"),
        "ram": -1,
        "cpu": -1,
        "sat": -1,
        "expiration": -1,
        "datestart": -1,
        "diskusage": 0.0,
        "html5": True,
        "folder": "/",
        "lab": None,
        "pod": 0,
        "session_token": None,
        "session_expires": None,
        "ip": None,
        "online": False,
        "last_login": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeDb:
    """AsyncSession stand-in for the auth/users routers: select-by-username
    plus write ops. A plain ``select(User)`` (or an aggregate) returns every
    account, with the row count as the scalar."""

    def __init__(self, *accounts):
        self.accounts = {account.username: account for account in accounts}
        self.commits = 0

    @staticmethod
    def _username(stmt):
        try:
            if stmt.whereclause.left.key == "username" and stmt.whereclause.right.value is not None:
                return str(stmt.whereclause.right.value)
        except AttributeError:
            pass
        return None

    async def execute(self, stmt):
        username = self._username(stmt)
        if username is None:
            return _Result(list(self.accounts.values()), scalar=len(self.accounts))
        return _Result([self.accounts[username]] if username in self.accounts else [])

    def add(self, obj):
        self.accounts[obj.username] = obj

    async def delete(self, obj):
        self.accounts.pop(obj.username, None)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        # Column defaults normally arrive with the INSERT; fill them in so
        # the response schema sees what the database would have stored.
        for column in User.__table__.columns:
            default = column.default
            if getattr(obj, column.name, None) is None and default is not None:
                value = default.arg(None) if callable(default.arg) else default.arg
                setattr(obj, column.name, value)

    async def close(self):
        return None


def _admin(username="admin", role="admin"):
    return SimpleNamespace(username=username, role=role, html5=True, folder="/")


@pytest.fixture()
def as_admin():
    user = _admin()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_admin, None)


@pytest.fixture()
def api_db():
    db = _FakeDb()
    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture()
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 5_000))
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Last-login tracking
# ---------------------------------------------------------------------------


class TestLastLogin:
    async def test_signing_in_stamps_last_login_and_source_address(self):
        alice = _account()

        await AuthService(_FakeDb(alice)).create_session(alice, ip="203.0.113.9")

        assert abs(alice.last_login - time.time()) < 5
        assert alice.ip == "203.0.113.9"
        assert alice.online is True

    async def test_an_ordinary_request_does_not_restamp_it(self):
        alice = _account(session_token="tok", session_expires=int(time.time()) + 3600, last_login=111)

        assert await AuthService(_FakeDb(alice)).validate_session("tok", "alice") is alice

        assert alice.last_login == 111

    async def test_login_response_carries_last_login(self, audit_events, monkeypatch, api_db, client):
        api_db.accounts["alice"] = _account()
        _silence_guacamole(monkeypatch)

        response = await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct-horse"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

        body = response.json()
        assert body["code"] == 200
        assert abs(body["data"]["last_login"] - time.time()) < 5
        assert api_db.accounts["alice"].ip == "203.0.113.9"

    async def test_last_login_cannot_be_set_through_the_update_payload(
        self, audit_events, api_db, as_admin, client
    ):
        api_db.accounts["alice"] = _account()

        response = await client.patch("/api/users/alice", json={"last_login": 12345})

        assert response.status_code == 200
        assert api_db.accounts["alice"].last_login is None
        assert _of(audit_events, "user.update") == []


def _silence_guacamole(monkeypatch):
    """Login pre-warms the HTML5 console token off the request path."""

    class _NoGuacamole:
        def prepare_login(self, _username):
            return asyncio.sleep(0)

    monkeypatch.setattr("app.routers.auth.GuacamoleDatabaseService", _NoGuacamole)


# ---------------------------------------------------------------------------
# Authentication events
# ---------------------------------------------------------------------------


async def _sign_in(client, username, password, **kwargs):
    return await client.post(
        "/api/auth/login", json={"username": username, "password": password}, **kwargs
    )


class TestAuthEvents:
    async def test_sign_in_and_sign_out_are_recorded_with_the_client_address(
        self, audit_events, monkeypatch, api_db, client
    ):
        api_db.accounts["alice"] = _account()
        _silence_guacamole(monkeypatch)

        signed_in = await _sign_in(client, "alice", "correct-horse")
        signed_out = await client.post("/api/auth/logout")

        assert signed_in.json()["code"] == 200
        assert signed_out.json()["code"] == 200
        assert [(e.event, e.actor) for e in audit_events] == [
            ("auth.login", "alice"),
            ("auth.logout", "alice"),
        ]

    async def test_failed_sign_in_records_the_attempt_not_the_password(
        self, audit_events, api_db, client
    ):
        api_db.accounts["alice"] = _account()

        wrong_password = await _sign_in(client, "alice", "battery-staple")
        unknown_user = await _sign_in(client, "mallory", "battery-staple")

        assert wrong_password.json()["code"] == 401
        assert unknown_user.json()["code"] == 401
        # Same answer either way: the response never says which it was.
        assert wrong_password.json() == unknown_user.json()
        assert audit_events == [
            AuditEvent(
                event="auth.login_failed",
                outcome="failure",
                target_type="user",
                target_id="alice",
            ),
            AuditEvent(
                event="auth.login_failed",
                outcome="failure",
                target_type="user",
                target_id="mallory",
            ),
        ]
        assert "battery-staple" not in json.dumps([e.__dict__ for e in audit_events])

    async def test_logout_without_a_session_is_not_an_event(self, audit_events, api_db, client):
        response = await client.post("/api/auth/logout")

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_the_first_account_is_created_by_no_one(
        self, audit_events, api_db, client
    ):
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "root-admin",
                "password": "bootstrap-pass",
                "email": "root@example.com",
                "name": "Root",
                "role": "user",
            },
        )

        assert response.json()["code"] == 200
        assert api_db.accounts["root-admin"].role == "admin"
        assert audit_events == [
            AuditEvent(
                event="user.create",
                actor=None,
                target_type="user",
                target_id="root-admin",
                target_name="Root",
                detail={"role": "admin", "bootstrap": True},
            )
        ]
        assert "bootstrap-pass" not in json.dumps(audit_events[0].__dict__)


# ---------------------------------------------------------------------------
# User-management events
# ---------------------------------------------------------------------------


NEW_USER = {
    "username": "bob",
    "password": "bob-pass-1",
    "email": "bob@example.com",
    "name": "Bob",
    "role": "user",
}


class TestUserEvents:
    async def test_create_update_and_delete_are_attributed_to_the_admin(
        self, audit_events, api_db, as_admin, client
    ):
        created = await client.post("/api/users/", json=NEW_USER)
        promoted = await client.patch("/api/users/bob", json={"role": "admin", "ram": 8192})
        deleted = await client.delete("/api/users/bob")

        assert created.json()["code"] == 200
        assert promoted.json()["code"] == 200
        assert deleted.json()["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="user.create",
                actor="admin",
                target_type="user",
                target_id="bob",
                target_name="Bob",
                detail={"role": "user"},
            ),
            AuditEvent(
                event="user.update",
                actor="admin",
                target_type="user",
                target_id="bob",
                target_name="Bob",
                detail={
                    "changes": {
                        "role": {"from": "user", "to": "admin"},
                        "ram": {"from": -1, "to": 8192},
                    },
                    "password_changed": False,
                },
            ),
            AuditEvent(
                event="user.delete",
                actor="admin",
                target_type="user",
                target_id="bob",
                target_name="Bob",
                detail={"role": "admin"},
            ),
        ]
        # The account is gone; its trail is not.
        assert "bob" not in api_db.accounts

    async def test_a_password_change_is_a_flag_never_the_value(
        self, audit_events, api_db, as_admin, client
    ):
        api_db.accounts["bob"] = _account(**{k: v for k, v in NEW_USER.items() if k != "password"})

        response = await client.patch("/api/users/bob", json={"password": "n3w-secret-pass"})

        assert response.json()["code"] == 200
        (event,) = audit_events
        assert event.detail == {"changes": {}, "password_changed": True}
        assert "n3w-secret-pass" not in json.dumps(event.__dict__)

    async def test_an_unchanged_save_leaves_no_row(self, audit_events, api_db, as_admin, client):
        api_db.accounts["bob"] = _account(**{k: v for k, v in NEW_USER.items() if k != "password"})

        response = await client.patch("/api/users/bob", json={"role": "user", "name": None})

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_self_service_edits_name_the_user_as_both_sides(
        self, audit_events, api_db, client
    ):
        api_db.accounts["alice"] = _account()
        app.dependency_overrides[get_current_user] = lambda: api_db.accounts["alice"]

        response = await client.patch("/api/users/alice", json={"name": "Alicia"})

        assert response.json()["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="user.update",
                actor="alice",
                target_type="user",
                target_id="alice",
                target_name="Alicia",
                detail={"changes": {"name": {"from": "Alice", "to": "Alicia"}}, "password_changed": False},
            )
        ]

    async def test_a_refused_privilege_escalation_leaves_no_row(
        self, audit_events, api_db, client
    ):
        api_db.accounts["alice"] = _account()
        app.dependency_overrides[get_current_user] = lambda: api_db.accounts["alice"]

        response = await client.patch("/api/users/alice", json={"role": "admin"})

        assert response.status_code == 403
        assert api_db.accounts["alice"].role == "user"
        assert audit_events == []


# ---------------------------------------------------------------------------
# Lab events — real lab files, the runtime stubbed out
# ---------------------------------------------------------------------------


DOCKER_IMAGE = "nova-ve/alpine-telnet:latest"
LAB_FILE = "audit-lab.json"


def _node(node_id, name, left):
    return {
        "id": node_id,
        "name": name,
        "type": "docker",
        "template": "docker",
        "image": DOCKER_IMAGE,
        "console": "telnet",
        "cpu": 1,
        "ram": 256,
        "ethernet": 2,
        "status": 0,
        "left": left,
        "top": 100,
        "interfaces": [
            {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None},
            {"index": 1, "name": "eth1", "planned_mac": None, "port_position": None},
        ],
    }


def _lab_document():
    return {
        "schema": 2,
        "id": "audit-lab",
        "meta": {"name": "Audit Lab"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {"1": _node(1, "alpine-a", 100), "2": _node(2, "alpine-b", 300)},
        "networks": {
            "1": {
                "id": 1,
                "name": "Lab LAN",
                "type": "linux_bridge",
                "left": 500,
                "top": 300,
                "icon": "01-Cloud-Default.svg",
                "width": 54,
                "style": "Solid",
                "linkstyle": "Straight",
                "color": "",
                "label": "",
                "visibility": 1,
                "smart": 0,
                "implicit": False,
                "config": {},
            }
        },
        "links": [
            {
                "id": "lnk_001",
                "style_override": None,
                "label": "",
                "color": "",
                "width": "1",
                "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 1},
            }
        ],
        "defaults": {"link_style": "orthogonal"},
    }


@pytest.fixture()
def lab_env(tmp_path, monkeypatch):
    labs_dir = tmp_path / "labs"
    settings = SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=tmp_path / "images",
        TMP_DIR=tmp_path / "tmp",
        TEMPLATES_DIR=tmp_path / "templates",
        USER_TEMPLATES_DIR=tmp_path / "user-templates",
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        # Consoles and recordings (#300): no Guacamole database (the
        # encrypted-URL path is used), external console launching off.
        GUACAMOLE_DATABASE_URL="",
        EXTERNAL_CONSOLE_HOST="",
        RECORDINGS_ENABLED=True,
        RECORDINGS_DIR=str(tmp_path / "recordings"),
    )
    for directory in (labs_dir, settings.IMAGES_DIR, settings.TMP_DIR, settings.TEMPLATES_DIR):
        directory.mkdir(parents=True)
    for module in (
        "app.services.lab_service",
        "app.services.template_service",
        "app.services.node_runtime_service",
        "app.routers.labs",
        "app.routers.recordings",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)

    docker_templates = settings.TEMPLATES_DIR / "docker"
    docker_templates.mkdir()
    (docker_templates / "docker.yml").write_text(
        "type: docker\nname: Docker Host\ncpu: 1\nram: 1024\nethernet: 1\n"
        "console_type: telnet\nicon_type: server\ncpulimit: 1\n"
    )
    monkeypatch.setattr(
        "app.services.template_service.TemplateService._docker_image_catalog",
        lambda _self: {DOCKER_IMAGE: {"image": DOCKER_IMAGE, "files": [], "path": DOCKER_IMAGE, "source": "docker"}},
    )

    (labs_dir / LAB_FILE).write_text(json.dumps(_lab_document()))

    # The runtime backends need a hypervisor; only their effect on lab.json
    # matters here. Like the real ones, they refuse a node that is not there.
    def _start(self, data, node_id):
        if str(node_id) not in (data.get("nodes") or {}):
            raise NodeRuntimeError("Node does not exist")
        return {"status": 2}

    monkeypatch.setattr(NodeRuntimeService, "start_node", _start)
    monkeypatch.setattr(NodeRuntimeService, "stop_node", lambda self, data, node_id: None)
    monkeypatch.setattr(NodeRuntimeService, "wipe_node", lambda self, data, node_id: None)
    monkeypatch.setattr(
        NodeRuntimeService, "enrich_node", lambda self, lab_id, node_id, node: dict(node)
    )

    user = _admin()
    app.dependency_overrides[get_current_user] = lambda: user
    yield SimpleNamespace(labs_dir=labs_dir, settings=settings, user=user)
    app.dependency_overrides.pop(get_current_user, None)


async def _lab(client, method, suffix, **kwargs):
    return await client.request(method, f"/api/labs/{LAB_FILE}{suffix}", **kwargs)


class TestNodeEvents:
    async def test_start_stop_and_wipe_name_the_node_and_its_lab(
        self, audit_events, lab_env, client
    ):
        for action in ("start", "stop", "wipe"):
            response = await _lab(client, "GET", f"/nodes/1/{action}")
            assert response.json()["code"] == 200

        assert audit_events == [
            AuditEvent(
                event=f"node.{action}",
                actor="admin",
                target_type="node",
                target_id=1,
                target_name="alpine-a",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"node_type": "docker", "template": "docker"},
            )
            for action in ("start", "stop", "wipe")
        ]

    async def test_an_action_that_fails_leaves_no_row(self, audit_events, lab_env, client, monkeypatch):
        def _refuse(self, data, node_id):
            raise NodeRuntimeError("bridge missing")

        monkeypatch.setattr(NodeRuntimeService, "start_node", _refuse)

        response = await _lab(client, "GET", "/nodes/1/start")

        assert response.json()["code"] == 400
        assert audit_events == []

    async def test_a_missing_node_leaves_no_row(self, audit_events, lab_env, client):
        response = await _lab(client, "GET", "/nodes/99/start")

        assert response.json()["code"] == 400
        assert audit_events == []

    async def test_create_update_and_delete(self, audit_events, lab_env, client):
        created = await _lab(
            client,
            "POST",
            "/nodes",
            json={
                "name": "alpine-c",
                "type": "docker",
                "template": "docker",
                "image": DOCKER_IMAGE,
                "console": "telnet",
                "ethernet": 2,
            },
        )
        node_id = created.json()["data"]["id"]
        renamed = await _lab(client, "PUT", f"/nodes/{node_id}", json={"name": "alpine-see"})
        deleted = await _lab(client, "DELETE", f"/nodes/{node_id}")

        assert created.json()["code"] == 200
        assert renamed.json()["code"] == 200
        assert deleted.json()["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="node.create",
                actor="admin",
                target_type="node",
                target_id=3,
                target_name="alpine-c",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"node_type": "docker", "template": "docker", "image": DOCKER_IMAGE},
            ),
            AuditEvent(
                event="node.update",
                actor="admin",
                target_type="node",
                target_id=3,
                target_name="alpine-see",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={
                    "node_type": "docker",
                    "template": "docker",
                    "changes": {"name": {"from": "alpine-c", "to": "alpine-see"}},
                },
            ),
            # Described as it was before it left the lab.
            AuditEvent(
                event="node.delete",
                actor="admin",
                target_type="node",
                target_id=3,
                target_name="alpine-see",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"node_type": "docker", "template": "docker"},
            ),
        ]

    async def test_an_unchanged_save_leaves_no_row(self, audit_events, lab_env, client):
        response = await _lab(client, "PUT", "/nodes/1", json={"name": "alpine-a", "cpu": 1})

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_template_creation_is_audited_once(self, audit_events, lab_env, client):
        response = await _lab(
            client,
            "POST",
            "/nodes/from-template",
            json={
                "template_type": "docker",
                "template_key": "docker",
                "name": "from-template",
                "image": DOCKER_IMAGE,
            },
        )

        assert response.json()["code"] == 200
        assert [(e.event, e.target_name) for e in audit_events] == [("node.create", "from-template")]

    async def test_batch_creation_is_one_entry_per_node(self, audit_events, lab_env, client):
        response = await _lab(
            client,
            "POST",
            "/nodes/batch",
            json={
                "name_prefix": "sw",
                "count": 3,
                "type": "docker",
                "template": "docker",
                "image": DOCKER_IMAGE,
            },
        )

        assert response.json()["code"] == 200
        created = _of(audit_events, "node.create")
        assert [e.target_name for e in created] == ["sw-1", "sw-2", "sw-3"]
        assert [e.detail["batch_size"] for e in created] == [3, 3, 3]
        assert len(audit_events) == 3


class TestNetworkAndLinkEvents:
    async def test_network_lifecycle(self, audit_events, lab_env, client, monkeypatch):
        service = networks.network_service
        monkeypatch.setattr(
            service,
            "create_network",
            AsyncMock(return_value={"id": 2, "name": "WAN", "type": "linux_bridge"}),
        )
        monkeypatch.setattr(service, "delete_network", AsyncMock(return_value=None))
        monkeypatch.setattr(
            service,
            "patch_network",
            AsyncMock(return_value=({"id": 1, "name": "Core LAN", "type": "linux_bridge"}, "renamed")),
        )

        created = await _lab(client, "POST", "/networks", json={"name": "WAN"})
        renamed = await _lab(client, "PATCH", "/networks/1", json={"name": "Core LAN"})
        deleted = await _lab(client, "DELETE", "/networks/1")

        assert created.status_code == 201
        assert renamed.json()["code"] == 200
        assert deleted.json()["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="network.create",
                actor="admin",
                target_type="network",
                target_id=2,
                target_name="WAN",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"network_type": "linux_bridge"},
            ),
            # The rename is reported under the name the network now has.
            AuditEvent(
                event="network.update",
                actor="admin",
                target_type="network",
                target_id=1,
                target_name="Core LAN",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={
                    "network_type": "linux_bridge",
                    "changes": {"name": {"from": "Lab LAN", "to": "Core LAN"}},
                },
            ),
            # The delete handler re-read the untouched lab file.
            AuditEvent(
                event="network.delete",
                actor="admin",
                target_type="network",
                target_id=1,
                target_name="Lab LAN",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"network_type": "linux_bridge"},
            ),
        ]

    async def test_an_unchanged_patch_leaves_no_row(self, audit_events, lab_env, client, monkeypatch):
        monkeypatch.setattr(
            networks.network_service,
            "patch_network",
            AsyncMock(return_value=({"id": 1, "name": "Lab LAN"}, "noop")),
        )

        response = await _lab(client, "PATCH", "/networks/1", json={"name": "Lab LAN"})

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_a_refused_network_change_leaves_no_row(self, audit_events, lab_env, client, monkeypatch):
        from app.services.network_service import NetworkServiceError

        monkeypatch.setattr(
            networks.network_service,
            "delete_network",
            AsyncMock(side_effect=NetworkServiceError(409, "still linked")),
        )

        response = await _lab(client, "DELETE", "/networks/1")

        assert response.status_code == 409
        assert audit_events == []

    async def test_link_lifecycle_names_both_endpoints(self, audit_events, lab_env, client, monkeypatch):
        service = links.link_service
        new_link = {"id": "lnk_002", "from": {"node_id": 2, "interface_index": 1}, "to": {"network_id": 1}}
        monkeypatch.setattr(service, "create_link", AsyncMock(return_value=(new_link, None, False)))
        monkeypatch.setattr(service, "delete_link", AsyncMock(return_value=None))
        monkeypatch.setattr(
            service,
            "patch_link",
            AsyncMock(return_value={**_lab_document()["links"][0], "label": "uplink"}),
        )

        created = await _lab(
            client,
            "POST",
            "/links",
            json={"from": {"node_id": 2, "interface_index": 1}, "to": {"network_id": 1}},
        )
        relabelled = await _lab(client, "PATCH", "/links/lnk_001", json={"label": "uplink"})
        deleted = await _lab(client, "DELETE", "/links/lnk_001")
        never_existed = await _lab(client, "DELETE", "/links/lnk_nope")

        assert created.status_code == 201
        assert relabelled.json()["code"] == 200
        assert deleted.json()["code"] == 200
        assert never_existed.json()["already_deleted"] is True
        assert audit_events == [
            AuditEvent(
                event="link.create",
                actor="admin",
                target_type="link",
                target_id="lnk_002",
                target_name="alpine-b:eth1 ↔ Lab LAN",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"from": "alpine-b:eth1", "to": "Lab LAN"},
            ),
            AuditEvent(
                event="link.update",
                actor="admin",
                target_type="link",
                target_id="lnk_001",
                target_name="alpine-a:eth0 ↔ Lab LAN",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={
                    "from": "alpine-a:eth0",
                    "to": "Lab LAN",
                    "changes": {"label": {"from": "", "to": "uplink"}},
                },
            ),
            AuditEvent(
                event="link.delete",
                actor="admin",
                target_type="link",
                target_id="lnk_001",
                target_name="alpine-a:eth0 ↔ Lab LAN",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"from": "alpine-a:eth0", "to": "Lab LAN"},
            ),
        ]

    async def test_an_idempotent_replay_created_nothing_and_says_so(
        self, audit_events, lab_env, client, monkeypatch
    ):
        monkeypatch.setattr(
            links.link_service,
            "create_link",
            AsyncMock(return_value=({"id": "lnk_001"}, None, True)),
        )

        response = await _lab(client, "POST", "/links", json={"from": {}, "to": {}})

        assert response.json()["code"] == 200
        assert audit_events == []


class TestTopologyPatches:
    """The canvas saves link styling through PUT /topology on every edit."""

    async def test_a_topology_only_save_is_silent(self, audit_events, lab_env, client):
        response = await _lab(client, "PUT", "/topology", json={"topology": []})

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_a_field_patch_is_audited_like_any_other_edit(self, audit_events, lab_env, client):
        response = await _lab(
            client,
            "PUT",
            "/topology",
            json={
                "topology": [],
                "nodes": {"1": {"name": "renamed-a", "left": 999}},
                "networks": {"1": {"label": "core"}},
            },
        )

        assert response.json()["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="node.update",
                actor="admin",
                target_type="node",
                target_id=1,
                target_name="renamed-a",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={
                    "node_type": "docker",
                    "template": "docker",
                    "changes": {"name": {"from": "alpine-a", "to": "renamed-a"}},
                    "via": "topology",
                },
            ),
            AuditEvent(
                event="network.update",
                actor="admin",
                target_type="network",
                target_id=1,
                target_name="Lab LAN",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={
                    "network_type": "linux_bridge",
                    "changes": {"label": {"from": "", "to": "core"}},
                    "via": "topology",
                },
            ),
        ]
        # left/top belong to PUT /layout and are neither applied nor audited.
        assert json.loads((lab_env.labs_dir / LAB_FILE).read_text())["nodes"]["1"]["left"] == 100


# ---------------------------------------------------------------------------
# Lab, folder and Lab Management events — handlers called directly
# ---------------------------------------------------------------------------


class _StubLabService:
    """Only the members the handlers under test touch."""

    def __init__(self, db):
        self.db = db

    row = None
    create_kwargs = None

    async def create_lab(self, **kwargs):
        type(self).create_kwargs = kwargs
        return SimpleNamespace(
            id="0b0d0a40-0000-0000-0000-000000000001",
            filename="training.json",
            name="Training",
            path="/training.json",
            owner=kwargs["owner"],
            author="",
            description="",
            body="",
            version="0",
            scripttimeout=300,
            countdown=0,
            linkwidth="1",
            grid=True,
            lock=False,
        )

    async def get_lab_by_filename(self, filename):
        return type(self).row

    async def delete_lab(self, lab, *, locked=False):
        return None


class TestLabLifecycleEvents:
    async def test_create_and_delete(self, audit_events, monkeypatch):
        monkeypatch.setattr(labs, "LabService", _StubLabService)
        _StubLabService.row = SimpleNamespace(
            owner="alice", name="Training", path="/training.json", id="x"
        )

        created = await labs.create_lab(
            LabMetaCreate(name="Training", filename="training.json"),
            current_user=_admin(),
            db=None,
        )
        deleted = await labs.delete_lab("training.json", current_user=_admin(), db=None)

        assert created["code"] == 200
        assert deleted["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="lab.create",
                actor="admin",
                target_type="lab",
                target_id="/training.json",
                target_name="Training",
                lab_path="/training.json",
                lab_name="Training",
            ),
            AuditEvent(
                event="lab.delete",
                actor="admin",
                target_type="lab",
                target_id="/training.json",
                target_name="Training",
                lab_path="/training.json",
                lab_name="Training",
            ),
        ]

    async def test_a_refused_delete_leaves_no_row(self, audit_events, monkeypatch):
        class _Refusing(_StubLabService):
            async def delete_lab(self, lab, *, locked=False):
                raise SharedCloudReferenceError("still referenced", references=[{"lab": "y"}])

        _Refusing.row = SimpleNamespace(owner="alice", name="Training", path="/training.json", id="x")
        monkeypatch.setattr(labs, "LabService", _Refusing)

        response = await labs.delete_lab("training.json", current_user=_admin(), db=None)

        assert response["code"] == 409
        assert audit_events == []

    async def test_deleting_another_users_lab_as_a_non_admin_leaves_no_row(
        self, audit_events, monkeypatch
    ):
        _StubLabService.row = SimpleNamespace(owner="alice", name="Training", path="/training.json", id="x")
        monkeypatch.setattr(labs, "LabService", _StubLabService)

        response = await labs.delete_lab(
            "training.json", current_user=_admin("bob", role="user"), db=None
        )

        assert response["code"] == 403
        assert audit_events == []


class TestLabManagementEvents:
    @staticmethod
    def _running_lab(lab_env):
        """Stop-all only touches nodes that are running."""
        document = _lab_document()
        for node in document["nodes"].values():
            node["status"] = 2
        (lab_env.labs_dir / LAB_FILE).write_text(json.dumps(document))

    async def test_stop_all_records_the_lab_name_and_the_outcome(
        self, audit_events, lab_env, monkeypatch
    ):
        self._running_lab(lab_env)
        monkeypatch.setattr("app.routers.manage._labs_dir_from_config", lambda: lab_env.labs_dir)
        stopped = []
        monkeypatch.setattr(
            NodeRuntimeService, "stop_node", lambda self, data, node_id: stopped.append(node_id)
        )

        response = await manage.stop_lab(LAB_FILE, current_user=_admin())

        assert response["code"] == 200
        assert stopped == [1, 2]
        assert response["data"]["name"] == "Audit Lab"
        assert audit_events == [
            AuditEvent(
                event="lab.stop",
                actor="admin",
                target_type="lab",
                target_id=f"/{LAB_FILE}",
                target_name="Audit Lab",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"stopped": [1, 2], "wiped": [], "failed": []},
            )
        ]

    async def test_stopping_a_lab_with_nothing_running_leaves_no_row(
        self, audit_events, lab_env, monkeypatch
    ):
        monkeypatch.setattr("app.routers.manage._labs_dir_from_config", lambda: lab_env.labs_dir)

        response = await manage.stop_lab(LAB_FILE, current_user=_admin())

        assert response["data"]["stopped"] == []
        assert audit_events == []

    async def test_a_failed_node_is_reported_not_hidden(
        self, audit_events, lab_env, monkeypatch
    ):
        self._running_lab(lab_env)
        monkeypatch.setattr("app.routers.manage._labs_dir_from_config", lambda: lab_env.labs_dir)

        def _stop(self, data, node_id):
            if node_id == 2:
                raise NodeRuntimeError("qemu did not exit")

        monkeypatch.setattr(NodeRuntimeService, "stop_node", _stop)

        response = await manage.stop_lab(LAB_FILE, current_user=_admin())

        assert response["data"]["stopped"] == [1]
        (event,) = audit_events
        assert event.detail["failed"] == [{"node_id": 2, "error": "qemu did not exit"}]

    async def test_wipe_all(self, audit_events, lab_env, monkeypatch):
        monkeypatch.setattr("app.routers.manage._labs_dir_from_config", lambda: lab_env.labs_dir)
        monkeypatch.setattr(NodeRuntimeService, "wipe_node", lambda self, data, node_id: None)

        response = await manage.wipe_lab(LAB_FILE, current_user=_admin())

        assert response["code"] == 200
        assert [e.event for e in audit_events] == ["lab.wipe"]
        assert audit_events[0].detail["wiped"] == [1, 2]

    async def test_nothing_happened_no_row(self, audit_events, monkeypatch):
        monkeypatch.setattr(
            manage, "_stop_or_wipe", lambda lab_path, wipe: manage._fail("Lab not found: x", code=404)
        )

        assert (await manage.stop_lab("x.json", current_user=_admin()))["code"] == 404
        assert audit_events == []

    async def test_a_non_admin_is_refused_before_anything_runs(self, audit_events, monkeypatch):
        def _unexpected(lab_path, wipe):
            raise AssertionError("must not run for a non-admin")

        monkeypatch.setattr(manage, "_stop_or_wipe", _unexpected)

        response = await manage.wipe_lab("x.json", current_user=_admin("bob", role="user"))

        assert response["code"] == 403
        assert audit_events == []


class TestFolderEvents:
    @pytest.fixture()
    def folder_env(self, monkeypatch):
        for name in ("create_folder_path", "rename_folder"):
            monkeypatch.setattr(folders.FolderService, name, lambda *args: None)
        monkeypatch.setattr(folders.FolderService, "delete_folder", lambda *_: nullcontext())
        monkeypatch.setattr(
            folders, "assert_no_external_members_for_scope", lambda path: None
        )

        class _NoLabs:
            def __init__(self, db):
                pass

            async def list_labs(self):
                return []

        monkeypatch.setattr(folders, "LabService", _NoLabs)
        return _FakeDb()

    async def test_create_rename_and_delete(self, audit_events, folder_env):
        db = folder_env
        created = await folders.create_folder(
            FolderCreateRequest(path="", name="Training"), current_user=_admin()
        )
        renamed = await folders.rename_folder(
            "Training", FolderRenameRequest(path="Courses"), current_user=_admin(), db=db
        )
        deleted = await folders.delete_folder("Courses", current_user=_admin(), db=db)

        assert created["data"] == {"path": "/Training"}
        assert renamed["code"] == 200
        assert deleted["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="folder.create",
                actor="admin",
                target_type="folder",
                target_id="/Training",
                target_name="Training",
            ),
            AuditEvent(
                event="folder.rename",
                actor="admin",
                target_type="folder",
                target_id="/Training",
                detail={"from": "/Training", "to": "/Courses"},
            ),
            AuditEvent(
                event="folder.delete",
                actor="admin",
                target_type="folder",
                target_id="/Courses",
            ),
        ]

    async def test_a_refused_delete_leaves_no_row(self, audit_events, monkeypatch):
        def _refuse(path):
            raise FileNotFoundError(path)

        monkeypatch.setattr(folders.FolderService, "delete_folder", _refuse)
        monkeypatch.setattr(
            folders, "assert_no_external_members_for_scope", lambda path: None
        )

        response = await folders.delete_folder("Courses", current_user=_admin(), db=_FakeDb())

        assert response["code"] == 404
        assert audit_events == []


# ---------------------------------------------------------------------------
# Consoles (#300)
# ---------------------------------------------------------------------------


class _NoConsoleRelay:
    """No recording relay: guacd dials the node directly."""

    async def start_session(self, **_kwargs):
        return None


class _Guacamole:
    """Hands out a console URL without a Guacamole stack."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def create_console_url(self, *_args, **_kwargs):
        return "/html5/#/c/abc123"


@pytest.fixture()
def console_env(lab_env, monkeypatch):
    """A running node whose console can be opened in the browser."""
    monkeypatch.setattr(
        NodeRuntimeService,
        "console_info",
        lambda self, data, node_id, mode="primary": {
            "host": "127.0.0.1",
            "port": 15005,
            "console": "telnet",
            "name": "alpine-a",
        },
    )
    monkeypatch.setattr(NodeRuntimeService, "is_node_running", lambda self, lab_id, node_id: True)
    monkeypatch.setattr("app.routers.labs.get_console_recorder", lambda: _NoConsoleRelay())
    monkeypatch.setattr("app.routers.labs.Html5SessionService", _Guacamole)
    # Each test starts with no console known to be open.
    monkeypatch.setattr(audit_service, "_open_consoles", audit_service._OpenConsoles())
    return lab_env


def _open_console(client, console="primary"):
    params = {"console": console} if console != "primary" else None
    return client.get(f"/api/labs/{LAB_FILE}/nodes/1/html5", params=params, follow_redirects=False)


def _wind_back(seconds):
    """Pretend every open console was opened ``seconds`` ago."""
    for key in audit_service._open_consoles._opened:
        audit_service._open_consoles._opened[key] -= seconds


ALICE_OPEN = dict(
    target_type="node",
    target_id=1,
    target_name="alpine-a",
    lab_path=f"/{LAB_FILE}",
    lab_name="Audit Lab",
)
ON_ALPINE = {"node_type": "docker", "template": "docker"}


class TestOpenConsoles:
    def test_how_long_a_console_was_open_is_measured_from_its_open(self):
        clock = audit_service._OpenConsoles()
        key = ("lab", 1, "primary", "alice")
        clock.opened(key)
        clock._opened[key] -= 90

        assert clock.closed(key) == pytest.approx(90, abs=1)

    def test_a_close_with_no_known_open_has_no_duration(self):
        clock = audit_service._OpenConsoles()

        assert clock.closed(("lab", 1, "primary", "alice")) is None

    def test_a_close_is_answered_once(self):
        clock = audit_service._OpenConsoles()
        key = ("lab", 1, "primary", "alice")
        clock.opened(key)

        assert clock.closed(key) is not None
        assert clock.closed(key) is None

    def test_each_user_and_each_console_of_a_node_is_timed_separately(self):
        clock = audit_service._OpenConsoles()
        clock.opened(("lab", 1, "primary", "alice"))
        clock._opened[("lab", 1, "primary", "alice")] -= 30
        clock.opened(("lab", 1, "second", "alice"))
        clock.opened(("lab", 1, "primary", "bob"))
        clock._opened[("lab", 1, "primary", "bob")] -= 300

        assert clock.closed(("lab", 1, "second", "alice")) == pytest.approx(0, abs=1)
        assert clock.closed(("lab", 1, "primary", "alice")) == pytest.approx(30, abs=1)
        # Bob's console is untouched by Alice's closes.
        assert clock.closed(("lab", 1, "primary", "bob")) == pytest.approx(300, abs=1)

    def test_reopening_a_console_restarts_the_clock(self):
        clock = audit_service._OpenConsoles()
        key = ("lab", 1, "primary", "alice")
        clock.opened(key)
        clock.opened(key)
        clock._opened[key] -= 7

        assert clock.closed(key) == pytest.approx(7, abs=1)

    def test_an_open_never_reported_closed_is_forgotten_after_half_a_day(self):
        clock = audit_service._OpenConsoles()
        clock.opened(("lab", 1, "primary", "alice"))
        clock._opened[("lab", 1, "primary", "alice")] -= audit_service._CONSOLE_OPEN_MAX_AGE + 1

        clock.opened(("lab", 2, "primary", "bob"))

        assert clock.closed(("lab", 1, "primary", "alice")) is None

    def test_the_key_is_the_path_as_shown_the_node_the_console_and_the_user(self):
        assert audit_service.console_key("lanes/alice/lab.json", "3", "second", "alice") == (
            "/lanes/alice/lab.json",
            3,
            "second",
            "alice",
        )


class TestConsoleOpenAndClose:
    async def test_opening_a_console_is_one_entry(self, audit_events, console_env, client):
        response = await _open_console(client)

        assert response.status_code == 307
        assert response.headers["location"] == "/html5/#/c/abc123"
        assert audit_events == [
            AuditEvent(
                event="console.open",
                actor="admin",
                **ALICE_OPEN,
                detail={**ON_ALPINE, "console_mode": "primary", "protocol": "telnet", "via": "html5"},
            )
        ]

    async def test_closing_it_says_how_long_it_was_open(self, audit_events, console_env, client):
        await _open_console(client)
        audit_events.clear()
        _wind_back(90)

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/console/close")

        assert response.json()["data"] == {"duration": 90}
        assert audit_events == [
            AuditEvent(
                event="console.close",
                actor="admin",
                **ALICE_OPEN,
                detail={**ON_ALPINE, "console_mode": "primary", "duration": 90},
            )
        ]

    async def test_the_second_console_of_a_node_is_its_own_session(self, audit_events, console_env, client):
        await _open_console(client, "second")
        await _open_console(client, "primary")
        audit_events.clear()
        _wind_back(60)

        await client.post(f"/api/labs/{LAB_FILE}/nodes/1/console/close", params={"console": "second"})

        (event,) = audit_events
        assert event.event == "console.close"
        assert event.detail["console_mode"] == "second"
        assert event.detail["duration"] == 60
        # The first console is still open.
        assert len(audit_service._open_consoles._opened) == 1

    async def test_a_reload_starts_a_new_session_with_a_fresh_clock(self, audit_events, console_env, client):
        await _open_console(client)
        _wind_back(600)
        await _open_console(client)  # the page reloaded the console
        audit_events.clear()

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/console/close")

        assert response.json()["data"]["duration"] < 60

    async def test_a_close_the_server_never_saw_opened_is_still_recorded(self, audit_events, console_env, client):
        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/console/close")

        assert response.json()["data"] == {"duration": None}
        (event,) = audit_events
        assert event.event == "console.close"
        assert event.detail["duration"] is None

    async def test_the_close_is_recorded_even_after_the_lab_is_gone(self, audit_events, console_env, client):
        (console_env.labs_dir / LAB_FILE).unlink()

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/console/close")

        assert response.json()["code"] == 200
        (event,) = audit_events
        assert event.event == "console.close"
        assert event.lab_path == f"/{LAB_FILE}"
        assert event.lab_name is None
        assert event.target_id == 1

    async def test_a_console_outside_a_users_folder_cannot_be_reported(self, audit_events, console_env, client):
        alice = SimpleNamespace(username="alice", role="editor", html5=True, folder="/lanes/alice")
        app.dependency_overrides[get_current_user] = lambda: alice

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/console/close")

        assert response.json()["code"] == 403
        assert audit_events == []

    async def test_an_unknown_console_mode_is_refused(self, audit_events, console_env, client):
        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/console/close", params={"console": "third"})

        assert response.json()["code"] == 400
        assert audit_events == []

    async def test_a_refused_open_leaves_no_row(self, audit_events, console_env, client, monkeypatch):
        def as_user(user):
            app.dependency_overrides[get_current_user] = lambda: user

        scoped = _admin("bob", role="editor")
        scoped.folder = "/lanes/bob"
        as_user(scoped)
        denied = await _open_console(client)  # the lab is outside bob's folder

        as_user(SimpleNamespace(username="carol", role="editor", html5=False, folder="/"))
        no_html5 = await _open_console(client)

        def _not_running(self, data, node_id, mode="primary"):
            raise NodeRuntimeError("node is not running")

        as_user(_admin())
        monkeypatch.setattr(NodeRuntimeService, "console_info", _not_running)
        not_running = await _open_console(client)

        assert denied.json()["code"] == 403
        assert no_html5.json()["code"] == 403
        assert not_running.json()["code"] == 400
        assert audit_events == []

    async def test_a_console_that_cannot_be_created_leaves_no_row(self, audit_events, console_env, client, monkeypatch):
        class _BrokenGuacamole(_Guacamole):
            async def create_console_url(self, *_args, **_kwargs):
                raise Html5SessionError("guacamole is away")

        monkeypatch.setattr("app.routers.labs.Html5SessionService", _BrokenGuacamole)

        response = await _open_console(client)

        assert response.json()["code"] == 500
        assert audit_events == []


class TestConsoleHandOff:
    """External and native consoles go to a client the server never sees again."""

    @pytest.fixture()
    def external_on(self, console_env):
        console_env.settings.EXTERNAL_CONSOLE_HOST = "console.example.test"
        return console_env

    async def test_an_external_console_launch_is_an_open(self, audit_events, external_on, client):
        response = await client.get(f"/api/labs/{LAB_FILE}/nodes/1/external-console")

        assert response.json()["data"]["host"] == "console.example.test"
        assert audit_events == [
            AuditEvent(
                event="console.open",
                actor="admin",
                **ALICE_OPEN,
                detail={**ON_ALPINE, "console_mode": "primary", "protocol": "telnet", "via": "external"},
            )
        ]
        # Nothing to time: this session will never be reported closed.
        assert audit_service._open_consoles._opened == {}

    async def test_no_launch_when_no_external_host_is_configured(self, audit_events, console_env, client):
        response = await client.get(f"/api/labs/{LAB_FILE}/nodes/1/external-console")

        assert response.json()["code"] == 400
        assert audit_events == []

    async def test_a_stopped_node_hands_off_nothing(self, audit_events, external_on, client, monkeypatch):
        monkeypatch.setattr(NodeRuntimeService, "is_node_running", lambda self, lab_id, node_id: False)

        response = await client.get(f"/api/labs/{LAB_FILE}/nodes/1/external-console")

        assert response.json()["code"] == 400
        assert audit_events == []

    async def test_a_native_launcher_download_is_an_open(self, audit_events, console_env, client):
        telnet = await client.get(f"/api/labs/{LAB_FILE}/nodes/1/telnet")

        assert telnet.status_code == 200
        assert "telnet://127.0.0.1:15005" in telnet.text
        assert audit_events == [
            AuditEvent(
                event="console.open",
                actor="admin",
                **ALICE_OPEN,
                detail={**ON_ALPINE, "console_mode": "primary", "protocol": "telnet", "via": "native"},
            )
        ]

    async def test_a_launcher_for_the_wrong_protocol_leaves_no_row(self, audit_events, console_env, client):
        response = await client.get(f"/api/labs/{LAB_FILE}/nodes/1/rdp")  # the console is telnet

        assert response.json()["code"] == 400
        assert audit_events == []


# ---------------------------------------------------------------------------
# Recordings (#300)
# ---------------------------------------------------------------------------


class _RecordingConsole:
    """A console session with a working record button."""

    def __init__(self, recording=False):
        self.recording = recording
        self.started_at = None
        self.failures = 0

    def status(self):
        return {
            "console_active": True,
            "recording": self.recording,
            "recording_id": "r1" if self.recording else None,
            "started_at": self.started_at,
        }

    def start_recording(self):
        if self.failures:
            self.failures -= 1
            raise OSError("disk full")
        self.recording = True
        self.started_at = datetime.now(UTC).isoformat()
        return self.status()

    def stop_recording(self):
        self.recording = False
        return self.status()


class _Recorder:
    def __init__(self, session=None):
        self.session = session
        self.active = set()

    def find(self, *_args):
        return self.session

    def is_active(self, cast):
        return cast in self.active


@pytest.fixture()
def recording_env(lab_env, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr("app.routers.recordings.get_console_recorder", lambda: recorder)
    app.dependency_overrides[get_db] = lambda: _FakeDb()
    lab_env.recorder = recorder
    yield lab_env
    app.dependency_overrides.pop(get_db, None)


def _write_recording(lab_env, node_id=3, by="bob", console="second", duration=42):
    """A finished recording as the recorder leaves it on disk."""
    directory = lab_env.labs_dir / ".recordings" / LAB_FILE / "audit-lab" / str(node_id)
    directory.mkdir(parents=True)
    cast = directory / "alpine-c-20261124T110000Z-abc12345-1.cast"
    cast.write_text(
        json.dumps(
            {
                "version": 2,
                "width": 120,
                "height": 30,
                "timestamp": 1_700_000_000,
                "duration": duration,
                "title": "Audit Lab / alpine-c",
                "env": {"TERM": "xterm"},
                "nova": {
                    "node_name": "alpine-c",
                    "username": by,
                    "console": console,
                    "started_at": "2026-11-24T11:00:00+00:00",
                },
            }
        )
        + '\n[1.0, "o", "hello\\n"]\n'
    )
    cast.with_suffix(".log").write_text("# Recording started 2026-11-24T11:00:00+00:00 by bob\nhello\n")
    return cast


class TestRecordingEvents:
    async def test_start_then_stop(self, audit_events, recording_env, client):
        recording_env.recorder.session = _RecordingConsole()
        url = f"/api/labs/{LAB_FILE}/nodes/1/recording"

        started = await client.post(url, json={"action": "start"})
        stopped = await client.post(url, json={"action": "stop"})

        # "stopped" is spelled out — appending "ed" to the action read "stoped".
        assert started.json()["message"] == "Recording started."
        assert stopped.json()["message"] == "Recording stopped."

        assert [(e.event, e.detail["console_mode"]) for e in audit_events] == [
            ("recording.start", "primary"),
            ("recording.stop", "primary"),
        ]
        start, stop = audit_events
        assert start.actor == "admin" and stop.actor == "admin"
        assert start.target_name == "alpine-a" and stop.target_name == "alpine-a"
        assert start.lab_name == "Audit Lab"
        assert start.detail == {**ON_ALPINE, "console_mode": "primary"}
        # How long the recording ran.
        assert isinstance(stop.detail["duration"], int)

    async def test_starting_a_recording_that_is_already_running_is_not_an_event(self, audit_events, recording_env, client):
        recording_env.recorder.session = _RecordingConsole(recording=True)

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/recording", json={"action": "start"})

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_stopping_a_recording_that_is_not_running_is_not_an_event(self, audit_events, recording_env, client):
        recording_env.recorder.session = _RecordingConsole(recording=False)

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/recording", json={"action": "stop"})

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_no_console_open_no_recording_no_event(self, audit_events, recording_env, client):
        recording_env.recorder.session = None

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/recording", json={"action": "start"})

        assert response.json()["code"] == 404
        assert audit_events == []

    async def test_a_recording_that_could_not_be_written_leaves_no_row(self, audit_events, recording_env, client):
        recording_env.recorder.session = session = _RecordingConsole()
        session.failures = 1

        response = await client.post(f"/api/labs/{LAB_FILE}/nodes/1/recording", json={"action": "start"})

        assert response.json()["code"] == 500
        assert audit_events == []

    async def test_deleting_a_recording_describes_it(self, audit_events, recording_env, client):
        cast = _write_recording(recording_env)

        response = await client.delete(f"/api/labs/{LAB_FILE}/recordings/3--{cast.name}")

        assert response.json()["code"] == 200
        assert not cast.exists()
        assert audit_events == [
            AuditEvent(
                event="recording.delete",
                actor="admin",
                target_type="recording",
                target_id=f"3--{cast.name}",
                target_name="alpine-c",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={"node_id": 3, "recorded_by": "bob", "console_mode": "second", "duration": 42},
            )
        ]

    async def test_a_recording_still_being_made_cannot_be_deleted(self, audit_events, recording_env, client):
        cast = _write_recording(recording_env)
        recording_env.recorder.active.add(cast)

        response = await client.delete(f"/api/labs/{LAB_FILE}/recordings/3--{cast.name}")

        assert response.json()["code"] == 409
        assert cast.exists()
        assert audit_events == []

    async def test_a_recording_that_is_not_there_leaves_no_row(self, audit_events, recording_env, client):
        response = await client.delete(f"/api/labs/{LAB_FILE}/recordings/3--nope.cast")

        assert response.json()["code"] == 404
        assert audit_events == []


# ---------------------------------------------------------------------------
# Lab settings and node interfaces (#300)
# ---------------------------------------------------------------------------


class _SettingsLabService:
    row = None

    def __init__(self, db):
        pass

    async def get_lab_by_filename(self, filename):
        return type(self).row

    async def update_lab(self, lab, *, locked=False, **fields):
        for field, value in fields.items():
            setattr(lab, field, value)
        return lab


def _training_lab_row():
    return SimpleNamespace(
        id="x",
        filename="training.json",
        path="/training.json",
        owner="admin",
        name="Training",
        author="",
        description="",
        body="",
        version="0",
        scripttimeout=300,
        countdown=0,
        linkwidth="1",
        grid=True,
        lock=False,
    )


class TestLabSettings:
    @pytest.fixture()
    def lab_settings(self, monkeypatch):
        monkeypatch.setattr(labs, "LabService", _SettingsLabService)
        _SettingsLabService.row = _training_lab_row()
        return _SettingsLabService.row

    async def test_saving_lab_settings_records_what_changed(self, audit_events, lab_settings):
        body = "Long text " * 100
        response = await labs.update_lab(
            "training.json",
            LabMetaUpdate(name="Training v2", lock=True, description="A" * 50, body=body),
            current_user=_admin(),
            db=None,
        )

        assert response["code"] == 200
        (event,) = audit_events
        assert event.event == "lab.update"
        assert event.actor == "admin"
        assert event.target_id == "/training.json"
        assert event.target_name == "Training v2"
        assert event.detail["changes"] == {
            # Free text is reported by length, never stored.
            "name": {"from": "Training", "to": "Training v2"},
            "description": {"from": None, "to": "50 characters"},
            "body": {"from": None, "to": f"{len(body)} characters"},
            "lock": {"from": False, "to": True},
        }

    async def test_an_unchanged_save_is_not_an_event(self, audit_events, lab_settings):
        await labs.update_lab(
            "training.json", LabMetaUpdate(name="Training", lock=False), current_user=_admin(), db=None
        )

        assert audit_events == []


class TestNodeInterfaceChanges:
    async def test_a_changed_mac_address_is_an_event(self, audit_events, lab_env, client):
        response = await client.patch(
            f"/api/labs/{LAB_FILE}/nodes/1/interfaces/0", json={"planned_mac": "AA:BB:CC:00:00:02"}
        )

        assert response.json()["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="node.update",
                actor="admin",
                target_type="node",
                target_id=1,
                target_name="alpine-a",
                lab_path=f"/{LAB_FILE}",
                lab_name="Audit Lab",
                detail={
                    **ON_ALPINE,
                    "changes": {"eth0.planned_mac": {"from": None, "to": "aa:bb:cc:00:00:02"}},
                },
            )
        ]

    async def test_setting_the_same_mac_address_again_is_not_an_event(self, audit_events, lab_env, client):
        await client.patch(f"/api/labs/{LAB_FILE}/nodes/1/interfaces/0", json={"planned_mac": "aa:bb:cc:00:00:02"})
        audit_events.clear()

        response = await client.patch(
            f"/api/labs/{LAB_FILE}/nodes/1/interfaces/0", json={"planned_mac": "AA:BB:CC:00:00:02"}
        )

        assert response.json()["code"] == 200
        assert audit_events == []

    async def test_a_moved_port_is_layout_and_not_an_event(self, audit_events, lab_env, client):
        response = await client.patch(
            f"/api/labs/{LAB_FILE}/nodes/1/interfaces/0", json={"port_position": "left"}
        )

        assert response.json()["code"] == 200
        assert audit_events == []


# ---------------------------------------------------------------------------
# Images and system settings (#300)
# ---------------------------------------------------------------------------


class _FakeDockerImages:
    behavior = {}

    def __init__(self):
        pass

    def _act(self, name, reference):
        outcome = type(self).behavior.get(name)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if outcome is not None else {"reference": reference}

    def mark(self, image):
        return self._act("mark", image)

    def unmark(self, image):
        return self._act("unmark", image)

    def pull(self, reference, mark_after=True):
        return self._act("pull", reference)


class _FakeTemplateUpload:
    reject = False

    def __init__(self):
        pass

    async def upload_image(self, **kwargs):
        if type(self).reject:
            raise TemplateError("Unsupported image type.")
        return {"image": "csr1000v.qcow2", "path": "/images/qemu/csr/csr1000v.qcow2"}, 12


class TestImageAndSystemEvents:
    async def test_mark_unmark_and_pull(self, audit_events, as_admin, client, monkeypatch):
        monkeypatch.setattr("app.routers.docker_images.DockerImageService", _FakeDockerImages)
        _FakeDockerImages.behavior = {}
        base = "/api/docker/images"

        marked = await client.post(f"{base}/mark", json={"image": "alpine:3.22"})
        pulled = await client.post(f"{base}/pull", json={"reference": "busybox:latest", "mark": False})
        unmarked = await client.post(f"{base}/unmark", json={"image": "alpine:3.22"})

        assert marked.json()["code"] == pulled.json()["code"] == unmarked.json()["code"] == 200
        assert [(e.event, e.target_id, e.detail) for e in audit_events] == [
            ("image.mark", "alpine:3.22", {"source": "docker"}),
            ("image.pull", "busybox:latest", {"source": "docker", "marked": False}),
            ("image.unmark", "alpine:3.22", {"source": "docker"}),
        ]
        assert all(e.actor == "admin" and e.target_type == "image" for e in audit_events)

    async def test_a_failed_image_operation_leaves_no_row(self, audit_events, as_admin, client, monkeypatch):
        monkeypatch.setattr("app.routers.docker_images.DockerImageService", _FakeDockerImages)
        _FakeDockerImages.behavior = {"pull": DockerImageError("registry unreachable")}

        response = await client.post("/api/docker/images/pull", json={"reference": "busybox:latest"})

        assert response.json()["code"] == 400
        assert audit_events == []

    async def test_uploading_an_image(self, audit_events, as_admin, client, monkeypatch):
        monkeypatch.setattr("app.routers.listing.TemplateService", _FakeTemplateUpload)
        _FakeTemplateUpload.reject = False

        response = await client.post(
            "/api/list/images/qemu/csr", files={"image": ("csr1000v.qcow2", b"qcow2-data!!", "application/octet-stream")}
        )

        assert response.json()["code"] == 200
        assert audit_events == [
            AuditEvent(
                event="image.upload",
                actor="admin",
                target_type="image",
                target_id="csr1000v.qcow2",
                target_name="csr1000v.qcow2",
                detail={"image_type": "qemu", "template": "csr", "bytes": 12},
            )
        ]

    async def test_a_rejected_upload_leaves_no_row(self, audit_events, as_admin, client, monkeypatch):
        monkeypatch.setattr("app.routers.listing.TemplateService", _FakeTemplateUpload)
        _FakeTemplateUpload.reject = True

        response = await client.post(
            "/api/list/images/qemu/csr", files={"image": ("x.txt", b"nope", "text/plain")}
        )

        assert response.json()["code"] == 400
        assert audit_events == []

    async def test_a_settings_change_records_which_settings_took_a_new_value(
        self, audit_events, as_admin, client, monkeypatch, tmp_path
    ):
        monkeypatch.setattr("app.routers.system.get_settings", lambda: SimpleNamespace(BASE_DATA_DIR=tmp_path))

        first = await client.put("/api/system/settings", json={"SESSION_MAX_AGE": 28800})
        # Saving what is already in force is not an event.
        await client.put("/api/system/settings", json={"SESSION_MAX_AGE": 28800})
        changed = await client.put("/api/system/settings", json={"SESSION_MAX_AGE": 43200})

        assert first.json()["code"] == 200
        assert [(e.detail["changes"]) for e in audit_events] == [
            {"SESSION_MAX_AGE": {"from": None, "to": 28800}},
            {"SESSION_MAX_AGE": {"from": 28800, "to": 43200}},
        ]
        assert audit_events[0].event == "system.settings"
        assert audit_events[0].actor == "admin"
        assert audit_events[0].detail["restart_required"] is True

    async def test_a_rejected_settings_change_leaves_no_row(self, audit_events, as_admin, client, monkeypatch, tmp_path):
        monkeypatch.setattr("app.routers.system.get_settings", lambda: SimpleNamespace(BASE_DATA_DIR=tmp_path))

        response = await client.put("/api/system/settings", json={"SESSION_MAX_AGE": 10})

        assert response.status_code == 422
        assert audit_events == []


# ---------------------------------------------------------------------------
# Read API — System → Logs → Activity log
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Captures the statements the reader issues and replays canned results."""

    def __init__(self, *results):
        self.statements = []
        self._results = list(results)

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._results.pop(0)

    def sql(self, index):
        compiled = self.statements[index].compile(
            dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
        )
        return " ".join(compiled.string.split()), compiled.params


def _entry_row(**overrides):
    fields = {
        "id": 7,
        "created_at": 1_700_000_000,
        "actor": "alice",
        "event": "node.start",
        "category": "node",
        "action": "start",
        "outcome": "success",
        "target_type": "node",
        "target_id": "3",
        "target_name": "R1",
        "lab_path": "/lab.json",
        "lab_name": "Lab",
        "ip": "203.0.113.9",
        "detail": '{"node_type": "qemu"}',
    }
    fields.update(overrides)
    return AuditLog(**fields)


def _reader(results=(42, [_entry_row()], ["alice", None])):
    session = _RecordingSession(*(_Result([], scalar=r) if isinstance(r, int) else _Result(r) for r in results))
    app.dependency_overrides[get_db] = lambda: session
    return session


def _in_order(params, sql):
    """Bind values in the order the placeholders appear in ``sql``."""
    return [params[name] for name in re.findall(r"%\((\w+)\)s", sql)]


@pytest.fixture()
def drop_reader():
    yield
    app.dependency_overrides.pop(get_db, None)


class TestActivityEndpoint:
    async def test_returns_the_page_with_its_total_and_filter_options(
        self, audit_events, as_admin, client, drop_reader
    ):
        session = _reader()

        response = await client.get("/api/system/logs/activity")

        body = response.json()
        assert body["code"] == 200
        assert body["data"] == {
            "entries": [
                {
                    "id": 7,
                    "time": 1_700_000_000,
                    "actor": "alice",
                    "event": "node.start",
                    "category": "node",
                    "action": "start",
                    "outcome": "success",
                    "target_type": "node",
                    "target_id": "3",
                    "target_name": "R1",
                    "lab_path": "/lab.json",
                    "lab_name": "Lab",
                    "ip": "203.0.113.9",
                    "detail": {"node_type": "qemu"},
                }
            ],
            "total": 42,
            "limit": 200,
            "offset": 0,
            "actors": ["alice"],
        }
        # Count, page, actor list — in that order.
        assert len(session.statements) == 3
        page_sql, page_params = session.sql(1)
        assert "ORDER BY audit_log.created_at DESC, audit_log.id DESC" in page_sql
        assert page_sql.endswith("LIMIT %(param_1)s OFFSET %(param_2)s")
        assert _in_order(page_params, page_sql) == [200, 0]
        assert "WHERE" not in session.sql(0)[0]
        assert "IS NOT NULL" in session.sql(2)[0]

    async def test_search_and_every_filter_reach_the_query(
        self, audit_events, as_admin, client, drop_reader
    ):
        session = _reader()

        response = await client.get(
            "/api/system/logs/activity",
            params={
                "q": "R1",
                "category": "node",
                "action": "start",
                "actor": "alice",
                "from": 1_700_000_000,
                "to": 1_700_000_999,
                "limit": 50,
                "offset": 100,
            },
        )

        assert response.json()["code"] == 200
        count_sql, count_params = session.sql(0)
        assert count_sql.count(" ILIKE %(") == 8  # q, against eight columns
        assert count_sql.count("ESCAPE ") == 8  # each with its escape character
        assert "R1" not in count_sql and "alice" not in count_sql  # binds, never inlined
        assert _in_order(count_params, count_sql) == [
            *(["%R1%"] * 8),  # actor, event, target, lab, address, detail
            "node",  # category
            "start",  # action
            "alice",  # actor
            1_700_000_000,  # from
            1_700_000_999,  # to
        ]
        assert "audit_log.created_at >=" in count_sql and "audit_log.created_at <=" in count_sql
        page_sql, page_params = session.sql(1)
        assert page_sql.endswith("LIMIT %(param_1)s OFFSET %(param_2)s")
        assert _in_order(page_params, page_sql) == _in_order(count_params, count_sql) + [50, 100]
        assert page_sql.split(" WHERE ", 1)[1].split(" ORDER BY ")[0] == count_sql.split(
            " WHERE ", 1
        )[1]  # the page and its total share one filter

    async def test_search_text_cannot_widen_itself_with_wildcards(self, audit_events, as_admin, client, drop_reader):
        session = _reader()

        await client.get("/api/system/logs/activity", params={"q": "100%_ \\"})

        count_sql, count_params = session.sql(0)
        assert _in_order(count_params, count_sql) == ["%100\\%\\_ \\\\%"] * 8

    async def test_total_counts_the_matches_not_the_page(self, audit_events, as_admin, client, drop_reader):
        session = _reader(results=(0, [], []))

        body = (await client.get("/api/system/logs/activity")).json()

        assert body["data"]["total"] == 0
        assert body["data"]["entries"] == []

    async def test_admins_only(self, audit_events, client, drop_reader):
        bob = _admin("bob", role="user")
        app.dependency_overrides[get_current_user] = lambda: bob
        app.dependency_overrides[get_current_admin] = _refuse_non_admin

        response = await client.get("/api/system/logs/activity")

        assert response.status_code == 403
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_current_admin, None)

    @pytest.mark.parametrize(
        "params", [{"limit": 0}, {"limit": 1001}, {"offset": -1}, {"from": "yesterday"}]
    )
    async def test_out_of_range_parameters_are_rejected(self, audit_events, as_admin, client, drop_reader, params):
        response = await client.get("/api/system/logs/activity", params=params)

        assert response.status_code == 422

    async def test_unparseable_stored_detail_does_not_break_the_view(self, drop_reader):
        assert audit_service.serialize_entry(_entry_row(detail="{not json"))["detail"] == {
            "unparseable": True
        }


def _refuse_non_admin():
    from fastapi import HTTPException

    raise HTTPException(status_code=403, detail="Admin access required.")


# pytest-asyncio runs in strict mode: mark every coroutine test, and only
# those — the marker on a plain function is itself a warning.
def _mark_coroutine_tests(namespace):
    for name, obj in list(namespace.items()):
        if inspect.isclass(obj) and name.startswith("Test"):
            for attr, member in list(vars(obj).items()):
                if inspect.iscoroutinefunction(member):
                    setattr(obj, attr, pytest.mark.asyncio(member))
        elif name.startswith("test_") and inspect.iscoroutinefunction(obj):
            namespace[name] = pytest.mark.asyncio(obj)


_mark_coroutine_tests(globals())
