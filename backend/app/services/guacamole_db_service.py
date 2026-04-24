# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import hashlib
import secrets
from functools import lru_cache

import asyncpg
import httpx

from app.config import get_settings
from app.schemas.user import UserRead


class GuacamoleDatabaseError(Exception):
    pass


_AUTH_TOKEN_CACHE: dict[tuple[str, str], str] = {}

class GuacamoleDatabaseService:
    ROOT_GROUP_NAME = "nova-ve"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.database_url = self.settings.GUACAMOLE_DATABASE_URL.strip()
        if not self.database_url:
            raise GuacamoleDatabaseError("Guacamole database auth is not configured.")

    async def create_console_url(
        self,
        current_user: UserRead,
        *,
        host: str,
        port: int,
        protocol: str,
        connection_name: str,
        connection_key: str | None = None,
    ) -> str:
        username = self._guacamole_username(current_user)
        password = self._guacamole_password(current_user)
        target_host = self._target_host(host)
        stable_connection_key = connection_key or connection_name

        connection_id = None
        entity_id = None
        for attempt in range(3):
            try:
                conn = await asyncpg.connect(self._asyncpg_dsn(), ssl=False)
                async with conn.transaction():
                    entity_id = await self._ensure_entity(conn, username)
                    await self._ensure_user(conn, entity_id, password)
                    root_group_id = await self._ensure_root_connection_group(conn)
                    group_id = await self._ensure_connection_group(conn, username, root_group_id)
                    connection_id = await self._ensure_connection(
                        conn,
                        group_id=group_id,
                        name=self._connection_name(current_user, stable_connection_key, protocol),
                        protocol=protocol,
                        host=target_host,
                        port=port,
                    )
                    await self._ensure_connection_permission(conn, entity_id, connection_id)
                await conn.close()
                break
            except Exception as exc:
                try:
                    await conn.close()
                except Exception:
                    pass
                if attempt == 2:
                    raise GuacamoleDatabaseError(f"Guacamole database provisioning failed: {exc}") from exc
                await asyncio.sleep(0.25 * (attempt + 1))

        if connection_id is None:
            raise GuacamoleDatabaseError("Guacamole database provisioning did not produce a connection id.")

        auth_token = await self._auth_token(username, password)

        client_identifier = self._client_identifier(connection_id)
        return f"{self._public_path()}#/client/{client_identifier}?token={auth_token}"

    def _target_host(self, fallback_host: str) -> str:
        target_host = self.settings.GUACAMOLE_TARGET_HOST.strip()
        if target_host:
            return target_host
        return fallback_host

    def _public_path(self) -> str:
        public_path = self.settings.GUACAMOLE_PUBLIC_PATH.strip() or "/html5/"
        if "://" not in public_path and not public_path.startswith("/"):
            public_path = f"/{public_path}"
        if not public_path.endswith("/"):
            public_path = f"{public_path}/"
        return public_path

    def _asyncpg_dsn(self) -> str:
        if self.database_url.startswith("postgresql+asyncpg://"):
            return "postgresql://" + self.database_url.split("://", 1)[1]
        return self.database_url

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
    def _connection_name(current_user: UserRead, connection_key: str, protocol: str) -> str:
        username = str(current_user.username).strip() or "user"
        base = f"{connection_key}-{protocol}-{username}"
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
        safe_prefix = connection_key.replace(":", "-")[:72]
        return f"{safe_prefix}-{protocol}-{digest}"[:120]

    def _client_identifier(self, connection_id: int) -> str:
        raw = f"{connection_id}\0c\0{self.settings.GUACAMOLE_DATA_SOURCE}".encode("utf-8")
        import base64

        return base64.b64encode(raw).decode("utf-8")

    async def _ensure_entity(self, conn, username: str) -> int:
        entity_id = await conn.fetchval(
            """
            SELECT entity_id
            FROM guacamole_entity
            WHERE type = 'USER' AND name = $1
            """,
            username,
        )
        if entity_id is not None:
            return int(entity_id)

        entity_id = await conn.fetchval(
            """
            INSERT INTO guacamole_entity (name, type)
            VALUES ($1, 'USER')
            RETURNING entity_id
            """,
            username,
        )
        return int(entity_id)

    async def _ensure_user(self, conn, entity_id: int, password: str) -> int:
        password_hash = bytes.fromhex(hashlib.sha256(password.encode("utf-8")).hexdigest())
        user_id = await conn.fetchval(
            """
            SELECT user_id
            FROM guacamole_user
            WHERE entity_id = $1
            """,
            entity_id,
        )
        if user_id is None:
            user_id = await conn.fetchval(
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
                    $1,
                    $2,
                    NULL,
                    NOW(),
                    FALSE,
                    FALSE
                )
                RETURNING user_id
                """,
                entity_id,
                password_hash,
            )
            return int(user_id)

        await conn.execute(
            """
            UPDATE guacamole_user
            SET password_hash = $1,
                password_salt = NULL,
                password_date = NOW(),
                disabled = FALSE,
                expired = FALSE
            WHERE user_id = $2
            """,
            password_hash,
            int(user_id),
        )
        return int(user_id)

    async def _ensure_root_connection_group(self, conn) -> int:
        group_id = await conn.fetchval(
            """
            SELECT connection_group_id
            FROM guacamole_connection_group
            WHERE connection_group_name = $1 AND parent_id IS NULL
            ORDER BY connection_group_id
            LIMIT 1
            """,
            self.ROOT_GROUP_NAME,
        )
        if group_id is not None:
            return int(group_id)

        group_id = await conn.fetchval(
            """
            INSERT INTO guacamole_connection_group (connection_group_name, type)
            VALUES ($1, 'ORGANIZATIONAL')
            RETURNING connection_group_id
            """,
            self.ROOT_GROUP_NAME,
        )
        return int(group_id)

    async def _ensure_connection_group(self, conn, username: str, root_group_id: int) -> int:
        group_name = f"nova-ve:{username}"
        group_id = await conn.fetchval(
            """
            SELECT connection_group_id
            FROM guacamole_connection_group
            WHERE connection_group_name = $1 AND parent_id = $2
            ORDER BY connection_group_id
            LIMIT 1
            """,
            group_name,
            root_group_id,
        )
        if group_id is not None:
            return int(group_id)

        group_id = await conn.fetchval(
            """
            INSERT INTO guacamole_connection_group (connection_group_name, parent_id, type)
            VALUES ($1, $2, 'ORGANIZATIONAL')
            RETURNING connection_group_id
            """,
            group_name,
            root_group_id,
        )
        return int(group_id)

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
        connection_id = await conn.fetchval(
            """
            SELECT connection_id
            FROM guacamole_connection
            WHERE connection_name = $1 AND parent_id = $2
            """,
            name,
            group_id,
        )
        if connection_id is None:
            connection_id = await conn.fetchval(
                """
                INSERT INTO guacamole_connection (connection_name, parent_id, protocol)
                VALUES ($1, $2, $3)
                RETURNING connection_id
                """,
                name,
                group_id,
                protocol,
            )
            connection_id = int(connection_id)
        else:
            connection_id = int(connection_id)
            await conn.execute(
                """
                UPDATE guacamole_connection
                SET protocol = $1
                WHERE connection_id = $2
                """,
                protocol,
                connection_id,
            )

        await conn.execute(
            "DELETE FROM guacamole_connection_parameter WHERE connection_id = $1",
            connection_id,
        )

        params = self._connection_parameters(host=host, port=port, protocol=protocol)

        for parameter_name, parameter_value in params.items():
            await conn.execute(
                """
                INSERT INTO guacamole_connection_parameter (
                    connection_id,
                    parameter_name,
                    parameter_value
                )
                VALUES ($1, $2, $3)
                """,
                connection_id,
                parameter_name,
                parameter_value,
            )

        return connection_id

    def _connection_parameters(self, *, host: str, port: int, protocol: str) -> dict[str, str]:
        params = {
            "hostname": host,
            "port": str(port),
        }
        if protocol == "rdp":
            params["ignore-cert"] = "true"
            params["security"] = "any"
            return params

        params["disable-auth"] = "true"

        if protocol == "telnet":
            params["font-name"] = self.settings.GUACAMOLE_TERMINAL_FONT_NAME
            params["font-size"] = str(self.settings.GUACAMOLE_TERMINAL_FONT_SIZE)

        return params

    async def _ensure_connection_permission(self, conn, entity_id: int, connection_id: int) -> None:
        await conn.execute(
            """
            DELETE FROM guacamole_connection_permission
            WHERE entity_id = $1
              AND connection_id = $2
              AND permission = 'READ'
            """,
            entity_id,
            connection_id,
        )
        await conn.execute(
            """
            INSERT INTO guacamole_connection_permission (
                entity_id,
                connection_id,
                permission
            )
            VALUES ($1, $2, 'READ')
            """,
            entity_id,
            connection_id,
        )

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

    async def _auth_token(self, username: str, password: str) -> str:
        cache_key = (self.settings.GUACAMOLE_INTERNAL_URL.strip(), username)
        cached = _AUTH_TOKEN_CACHE.get(cache_key)
        if cached:
            return cached

        auth_token = await self._request_auth_token(username, password)
        _AUTH_TOKEN_CACHE[cache_key] = auth_token
        return auth_token
