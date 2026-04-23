import base64
import hashlib
import hmac
import json
import secrets
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urljoin

import httpx

from app.config import get_settings
from app.schemas.user import UserRead


class Html5SessionError(Exception):
    pass


class Html5SessionService:
    def __init__(self):
        self.settings = get_settings()

    async def create_console_url(
        self,
        current_user: UserRead,
        *,
        host: str,
        port: int,
        protocol: str,
        connection_name: str,
    ) -> str:
        encrypted_payload = self._encrypted_payload(
            current_user=current_user,
            host=self._target_host(host),
            port=port,
            protocol=protocol,
            connection_name=connection_name,
        )
        auth_token, data_source = await self._request_auth_token(encrypted_payload)
        connection_identifier = await self._connection_identifier(auth_token, data_source, connection_name)
        client_identifier = self._client_identifier(connection_identifier, data_source)
        return f"{self._public_path()}#/client/{quote(client_identifier)}?token={quote(auth_token)}"

    def _encrypted_payload(
        self,
        *,
        current_user: UserRead,
        host: str,
        port: int,
        protocol: str,
        connection_name: str,
    ) -> str:
        secret_key = self.settings.GUACAMOLE_JSON_SECRET_KEY.strip().lower()
        if not secret_key:
            raise Html5SessionError("Guacamole JSON auth is not configured.")
        if len(secret_key) != 32 or any(character not in "0123456789abcdef" for character in secret_key):
            raise Html5SessionError("Guacamole JSON auth secret must be a 32-digit hexadecimal value.")

        payload = json.dumps(
            {
                "username": current_user.username,
                "expires": self._expires_timestamp(),
                "connections": {
                    connection_name: {
                        "id": self._connection_id(connection_name, host, port, protocol),
                        "protocol": self._guacamole_protocol(protocol),
                        "parameters": self._connection_parameters(host, port, protocol),
                    }
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

        key_bytes = bytes.fromhex(secret_key)
        signed_payload = hmac.new(key_bytes, payload, hashlib.sha256).digest() + payload
        openssl_binary = shutil.which("openssl")
        if not openssl_binary:
            raise Html5SessionError("OpenSSL is required for Guacamole JSON auth payload generation.")

        encrypted = subprocess.run(
            [
                openssl_binary,
                "enc",
                "-aes-128-cbc",
                "-K",
                secret_key,
                "-iv",
                "00000000000000000000000000000000",
                "-nosalt",
                "-base64",
                "-A",
            ],
            input=signed_payload,
            capture_output=True,
            check=False,
        )
        if encrypted.returncode != 0:
            error = encrypted.stderr.decode("utf-8", errors="ignore").strip() or "OpenSSL encryption failed."
            raise Html5SessionError(error)

        return encrypted.stdout.decode("utf-8", errors="ignore").strip()

    def _expires_timestamp(self) -> int:
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.GUACAMOLE_JSON_EXPIRE_SECONDS)
        return int(expires_at.timestamp() * 1000)

    def _public_path(self) -> str:
        public_path = self.settings.GUACAMOLE_PUBLIC_PATH.strip() or "/html5/"
        if not public_path.startswith("/"):
            public_path = f"/{public_path}"
        if not public_path.endswith("/"):
            public_path = f"{public_path}/"
        return public_path

    def _target_host(self, fallback_host: str) -> str:
        target_host = self.settings.GUACAMOLE_TARGET_HOST.strip()
        if target_host:
            return target_host
        return fallback_host

    def _internal_url(self, path: str) -> str:
        base = self.settings.GUACAMOLE_INTERNAL_URL.strip() or "http://127.0.0.1:8081/html5/"
        if not base.endswith("/"):
            base = f"{base}/"
        return urljoin(base, path.lstrip("/"))

    async def _request_auth_token(self, encrypted_payload: str) -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._internal_url("/api/tokens"),
                data={"data": encrypted_payload},
            )
        if response.status_code >= 400:
            raise Html5SessionError(f"Guacamole token exchange failed: HTTP {response.status_code}")

        payload = response.json()
        auth_token = str(payload.get("authToken", "")).strip()
        data_source = str(payload.get("dataSource", "")).strip()
        if not auth_token or not data_source:
            raise Html5SessionError("Guacamole token exchange returned an incomplete response.")
        return auth_token, data_source

    async def _connection_identifier(self, auth_token: str, data_source: str, connection_name: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                self._internal_url(f"/api/session/data/{data_source}/connections"),
                params={"token": auth_token},
            )
        if response.status_code >= 400:
            raise Html5SessionError(f"Guacamole connection lookup failed: HTTP {response.status_code}")

        connections = response.json()
        connection = connections.get(connection_name)
        if connection is None:
            for value in connections.values():
                if value.get("name") == connection_name:
                    connection = value
                    break
        if connection is None:
            raise Html5SessionError(f"Guacamole connection was not found for {connection_name}.")

        identifier = str(connection.get("identifier", "")).strip()
        if not identifier:
            raise Html5SessionError(f"Guacamole connection identifier was missing for {connection_name}.")
        return identifier

    @staticmethod
    def _client_identifier(connection_identifier: str, data_source: str) -> str:
        return base64.b64encode(f"{connection_identifier}\0c\0{data_source}".encode("utf-8")).decode("utf-8")

    @staticmethod
    def _connection_id(connection_name: str, host: str, port: int, protocol: str) -> str:
        return secrets.token_hex(16).upper() + hashlib.sha256(
            f"{connection_name}:{host}:{port}:{protocol}".encode("utf-8")
        ).hexdigest()[:16].upper()

    @staticmethod
    def _guacamole_protocol(protocol: str) -> str:
        if protocol in {"telnet", "vnc", "rdp"}:
            return protocol
        raise Html5SessionError(f"Unsupported HTML5 console protocol: {protocol}")

    @staticmethod
    def _connection_parameters(host: str, port: int, protocol: str) -> dict[str, str]:
        parameters = {
            "hostname": host,
            "port": str(port),
        }
        if protocol == "rdp":
            parameters["ignore-cert"] = "true"
            parameters["security"] = "any"
        return parameters
