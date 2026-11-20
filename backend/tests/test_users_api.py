# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""User-management route tests (#261).

Covers the admin/self-service privilege split on PATCH, the self-delete
guard, and the full-field listing. Uses a fake AsyncSession — the users
router only touches execute/commit/refresh/delete, so no database is
required; the username filter is recovered from the statement's simple
equality predicate.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.database import get_db
from app.dependencies import get_current_admin, get_current_user
from app.schemas.user import UserRead


def _user(**overrides):
    base = {
        "username": "alice",
        "email": "alice@example.com",
        "name": "Alice",
        "role": "user",
        "extauth": "internal",
        "password_hash": "hashed",
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
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Minimal AsyncSession stand-in: select-by-username + write ops."""

    def __init__(self, users):
        self.users = {u.username: u for u in users}
        self.deleted = []

    @staticmethod
    def _username_filter(stmt):
        where = getattr(stmt, "whereclause", None)
        if where is None:
            return None
        try:
            if where.left.key == "username" and where.right.value is not None:
                return str(where.right.value)
        except AttributeError:
            return None
        return None

    async def execute(self, stmt):
        username = self._username_filter(stmt)
        if username is None:
            return _FakeResult(list(self.users.values()))
        return _FakeResult(
            [self.users[username]] if username in self.users else []
        )

    async def delete(self, user):
        self.deleted.append(user.username)
        self.users.pop(user.username, None)

    async def commit(self):
        return None

    async def refresh(self, user):
        return None


def _install(current, fake_db):
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_current_admin] = lambda: current
    app.dependency_overrides[get_db] = lambda: fake_db


@pytest_asyncio.fixture()
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_users_returns_full_profile_fields(client):
    admin = _user(username="admin", role="admin")
    alice = _user(cpu=4, ram=8192, pod=3, expiration=1735689600)
    fake = FakeSession([admin, alice])
    _install(admin, fake)

    response = await client.get("/api/users/")

    assert response.status_code == 200
    data = response.json()["data"]
    assert "admin" in data and "alice" in data
    assert data["alice"]["cpu"] == 4
    assert data["alice"]["ram"] == 8192
    assert data["alice"]["pod"] == 3
    assert data["alice"]["expiration"] == 1735689600
    assert data["alice"]["role"] == "user"


@pytest.mark.asyncio
async def test_self_service_cannot_escalate_role(client):
    alice = _user()
    fake = FakeSession([alice])
    _install(alice, fake)

    response = await client.patch("/api/users/alice", json={"role": "admin"})

    assert response.status_code == 403
    assert alice.role == "user"


@pytest.mark.asyncio
async def test_self_service_cannot_change_quotas(client):
    alice = _user()
    fake = FakeSession([alice])
    _install(alice, fake)

    response = await client.patch("/api/users/alice", json={"cpu": 99, "pod": 7})

    assert response.status_code == 403
    assert alice.cpu == -1
    assert alice.pod == 0


@pytest.mark.asyncio
async def test_self_service_updates_own_profile(client):
    alice = _user()
    fake = FakeSession([alice])
    _install(alice, fake)

    response = await client.patch(
        "/api/users/alice",
        json={"email": "new@example.com", "name": "Alice A.", "html5": False},
    )

    assert response.status_code == 200
    assert alice.email == "new@example.com"
    assert alice.name == "Alice A."
    assert alice.html5 is False


@pytest.mark.asyncio
async def test_admin_updates_quotas_and_tenancy(client):
    admin = _user(username="admin", role="admin")
    alice = _user()
    fake = FakeSession([admin, alice])
    _install(admin, fake)

    response = await client.patch(
        "/api/users/alice",
        json={
            "cpu": 2,
            "ram": 4096,
            "pod": 5,
            "expiration": 1767225600,
            "datestart": 1735689600,
            "folder": "/alice",
            "sat": 1,
        },
    )

    assert response.status_code == 200
    assert alice.cpu == 2
    assert alice.ram == 4096
    assert alice.pod == 5
    assert alice.expiration == 1767225600
    assert alice.datestart == 1735689600
    assert alice.folder == "/alice"
    assert alice.sat == 1


@pytest.mark.asyncio
async def test_admin_role_change_rejects_unknown_value(client):
    admin = _user(username="admin", role="admin")
    alice = _user()
    fake = FakeSession([admin, alice])
    _install(admin, fake)

    response = await client.patch("/api/users/alice", json={"role": "superadmin"})

    assert response.status_code == 422
    assert alice.role == "user"


@pytest.mark.asyncio
async def test_non_admin_cannot_patch_other_user(client):
    alice = _user()
    bob = _user(username="bob", email="bob@example.com")
    fake = FakeSession([alice, bob])
    _install(alice, fake)

    response = await client.patch("/api/users/bob", json={"email": "hax@example.com"})

    assert response.status_code == 403
    assert bob.email == "bob@example.com"


@pytest.mark.asyncio
async def test_admin_cannot_delete_own_account(client):
    admin = _user(username="admin", role="admin")
    fake = FakeSession([admin])
    _install(admin, fake)

    response = await client.delete("/api/users/admin")

    assert response.status_code == 400
    assert "admin" in fake.users


@pytest.mark.asyncio
async def test_admin_deletes_other_user(client):
    admin = _user(username="admin", role="admin")
    alice = _user()
    fake = FakeSession([admin, alice])
    _install(admin, fake)

    response = await client.delete("/api/users/alice")

    assert response.status_code == 200
    assert "alice" not in fake.users
    assert fake.deleted == ["alice"]


@pytest.mark.asyncio
async def test_update_missing_user_returns_404(client):
    admin = _user(username="admin", role="admin")
    fake = FakeSession([admin])
    _install(admin, fake)

    response = await client.patch("/api/users/ghost", json={"email": "x@y.com"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_duplicate_user_returns_error_code(client):
    admin = _user(username="admin", role="admin")
    fake = FakeSession([admin])
    _install(admin, fake)

    response = await client.post(
        "/api/users/",
        json={
            "username": "admin",
            "password": "secret123",
            "email": "a@b.com",
            "name": "Dup",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 400
    assert "already exists" in payload["message"]
