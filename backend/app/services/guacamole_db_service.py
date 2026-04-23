import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.database import async_session_maker
from app.models.html5_session import Html5Session
from app.schemas.user import UserRead


class GuacamoleDatabaseError(Exception):
    pass


@lru_cache()
def _guacamole_engine(database_url: str):
    return create_async_engine(database_url, future=True)


class GuacamoleDatabaseService:
    AUTH_CACHE_KEY = "__guac_auth__"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.database_url = self.settings.GUACAMOLE_DATABASE_URL.strip()
        if not self.database_url:
            raise GuacamoleDatabaseError("Guacamole database auth is not configured.")
        self.engine = _guacamole_engine(self.database_url)

    async def create_console_url(
        self,
        current_user: UserRead,
        *,
        host: str,
        port: int,
        protocol: str,
        connection_name: str,
    ) -> str:
        username = self._guacamole_username(current_user)
        password = self._guacamole_password(current_user)
        target_host = self._target_host(host)

        async with self.engine.begin() as conn:
            entity_id = await self._ensure_entity(conn, username)
            await self._ensure_user(conn, entity_id, password)
            group_id = await self._ensure_connection_group(conn, username)
            connection_id = await self._ensure_connection(
                conn,
                group_id=group_id,
                name=self._connection_name(current_user, connection_name, protocol, target_host, port),
                protocol=protocol,
                host=target_host,
                port=port,
            )
            await self._ensure_connection_permission(conn, entity_id, connection_id)

        auth_token = await self._get_cached_auth_token(username)
        if not auth_token:
            auth_token = await self._request_auth_token(username, password)
            await self._cache_auth_token(username, auth_token)

        client_identifier = self._client_identifier(connection_id)
        return f"{self._public_path()}#/client/{client_identifier}?token={auth_token}"

    def _target_host(self, fallback_host: str) -> str:
        target_host = self.settings.GUACAMOLE_TARGET_HOST.strip()
        if target_host:
            return target_host
        return fallback_host

    def _public_path(self) -> str:
        public_path = self.settings.GUACAMOLE_PUBLIC_PATH.strip() or "/html5/"
        if not public_path.startswith("/"):
            public_path = f"/{public_path}"
        if not public_path.endswith("/"):
            public_path = f"{public_path}/"
        return public_path

    def _internal_url(self, path: str) -> str:
        base = self.settings.GUACAMOLE_INTERNAL_URL.strip() or "http://127.0.0.1:8081/html5/"
        if not base.endswith("/"):
            base = f"{base}/"
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _guacamole_username(current_user: UserRead) -> str:
        return str(current_user.username).strip() or "user"

    def _guacamole_password(self, current_user: UserRead) -> str:
        username = self._guacamole_username(current_user)
        material = f"{self.settings.SECRET_KEY}:{username}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _connection_name(current_user: UserRead, connection_name: str, protocol: str, host: str, port: int) -> str:
        username = str(current_user.username).strip() or "user"
        base = f"{connection_name}-{protocol}-{host}-{port}-{username}"
        return base[:120]

    def _client_identifier(self, connection_id: int) -> str:
        raw = f"{connection_id}\0c\0{self.settings.GUACAMOLE_DATA_SOURCE}".encode("utf-8")
        import base64

        return base64.b64encode(raw).decode("utf-8")

    async def _ensure_entity(self, conn, username: str) -> int:
        existing = await conn.execute(
            text(
                """
                SELECT entity_id
                FROM guacamole_entity
                WHERE type = 'USER' AND name = :name
                """
            ),
            {"name": username},
        )
        entity_id = existing.scalar_one_or_none()
        if entity_id is not None:
            return int(entity_id)

        inserted = await conn.execute(
            text(
                """
                INSERT INTO guacamole_entity (name, type)
                VALUES (:name, 'USER')
                RETURNING entity_id
                """
            ),
            {"name": username},
        )
        return int(inserted.scalar_one())

    async def _ensure_user(self, conn, entity_id: int, password: str) -> int:
        password_hash = bytes.fromhex(hashlib.sha256(password.encode("utf-8")).hexdigest())
        existing = await conn.execute(
            text(
                """
                SELECT user_id
                FROM guacamole_user
                WHERE entity_id = :entity_id
                """
            ),
            {"entity_id": entity_id},
        )
        user_id = existing.scalar_one_or_none()
        if user_id is None:
            inserted = await conn.execute(
                text(
                    """
                    INSERT INTO guacamole_user (
                        entity_id,
                        password_hash,
                        password_salt,
                        password_date,
                        disabled,
                        expired
                    )
                    VALUES (
                        :entity_id,
                        :password_hash,
                        NULL,
                        NOW(),
                        FALSE,
                        FALSE
                    )
                    RETURNING user_id
                    """
                ),
                {"entity_id": entity_id, "password_hash": password_hash},
            )
            return int(inserted.scalar_one())

        await conn.execute(
            text(
                """
                UPDATE guacamole_user
                SET password_hash = :password_hash,
                    password_salt = NULL,
                    password_date = NOW(),
                    disabled = FALSE,
                    expired = FALSE
                WHERE user_id = :user_id
                """
            ),
            {"user_id": int(user_id), "password_hash": password_hash},
        )
        return int(user_id)

    async def _ensure_connection_group(self, conn, username: str) -> int:
        group_name = f"nova-ve:{username}"
        existing = await conn.execute(
            text(
                """
                SELECT connection_group_id
                FROM guacamole_connection_group
                WHERE connection_group_name = :name AND parent_id IS NULL
                """
            ),
            {"name": group_name},
        )
        group_id = existing.scalar_one_or_none()
        if group_id is not None:
            return int(group_id)

        inserted = await conn.execute(
            text(
                """
                INSERT INTO guacamole_connection_group (connection_group_name, type)
                VALUES (:name, 'ORGANIZATIONAL')
                RETURNING connection_group_id
                """
            ),
            {"name": group_name},
        )
        return int(inserted.scalar_one())

    async def _ensure_connection(
        self,
        conn,
        *,
        group_id: int,
        name: str,
        protocol: str,
        host: str,
        port: int,
    ) -> int:
        existing = await conn.execute(
            text(
                """
                SELECT connection_id
                FROM guacamole_connection
                WHERE connection_name = :name AND parent_id = :parent_id
                """
            ),
            {"name": name, "parent_id": group_id},
        )
        connection_id = existing.scalar_one_or_none()
        if connection_id is None:
            inserted = await conn.execute(
                text(
                    """
                    INSERT INTO guacamole_connection (connection_name, parent_id, protocol)
                    VALUES (:name, :parent_id, :protocol)
                    RETURNING connection_id
                    """
                ),
                {"name": name, "parent_id": group_id, "protocol": protocol},
            )
            connection_id = int(inserted.scalar_one())
        else:
            connection_id = int(connection_id)
            await conn.execute(
                text(
                    """
                    UPDATE guacamole_connection
                    SET protocol = :protocol
                    WHERE connection_id = :connection_id
                    """
                ),
                {"connection_id": connection_id, "protocol": protocol},
            )

        await conn.execute(
            text("DELETE FROM guacamole_connection_parameter WHERE connection_id = :connection_id"),
            {"connection_id": connection_id},
        )

        params = {
            "hostname": host,
            "port": str(port),
        }
        if protocol == "rdp":
            params["ignore-cert"] = "true"
            params["security"] = "any"
        else:
            params["disable-auth"] = "true"

        for parameter_name, parameter_value in params.items():
            await conn.execute(
                text(
                    """
                    INSERT INTO guacamole_connection_parameter (
                        connection_id,
                        parameter_name,
                        parameter_value
                    )
                    VALUES (:connection_id, :parameter_name, :parameter_value)
                    """
                ),
                {
                    "connection_id": connection_id,
                    "parameter_name": parameter_name,
                    "parameter_value": parameter_value,
                },
            )

        return connection_id

    async def _ensure_connection_permission(self, conn, entity_id: int, connection_id: int) -> None:
        await conn.execute(
            text(
                """
                DELETE FROM guacamole_connection_permission
                WHERE entity_id = :entity_id
                  AND connection_id = :connection_id
                  AND permission = 'READ'
                """
            ),
            {"entity_id": entity_id, "connection_id": connection_id},
        )
        await conn.execute(
            text(
                """
                INSERT INTO guacamole_connection_permission (
                    entity_id,
                    connection_id,
                    permission
                )
                VALUES (:entity_id, :connection_id, 'READ')
                """
            ),
            {"entity_id": entity_id, "connection_id": connection_id},
        )

    async def _get_cached_auth_token(self, username: str) -> str | None:
        async with async_session_maker() as session:
            session_row = await session.get(Html5Session, {"username": username, "connection_id": self.AUTH_CACHE_KEY})
            if not session_row or not session_row.token or not session_row.expires_at:
                return None
            if session_row.expires_at <= datetime.now(UTC).replace(tzinfo=None):
                return None
            return session_row.token

    async def _cache_auth_token(self, username: str, token: str) -> None:
        expires_at = (datetime.now(UTC) + timedelta(minutes=55)).replace(tzinfo=None)
        async with async_session_maker() as session:
            session_row = await session.get(Html5Session, {"username": username, "connection_id": self.AUTH_CACHE_KEY})
            if session_row is None:
                session_row = Html5Session(
                    username=username,
                    connection_id=self.AUTH_CACHE_KEY,
                    token=token,
                    expires_at=expires_at,
                )
                session.add(session_row)
            else:
                session_row.token = token
                session_row.expires_at = expires_at
            await session.commit()

    async def _request_auth_token(self, username: str, password: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._internal_url("/api/tokens"),
                data={"username": username, "password": password},
            )
        if response.status_code >= 400:
            raise GuacamoleDatabaseError(f"Guacamole token exchange failed: HTTP {response.status_code}")
        payload = response.json()
        auth_token = str(payload.get("authToken", "")).strip()
        if not auth_token:
            raise GuacamoleDatabaseError("Guacamole token exchange returned no auth token.")
        return auth_token
