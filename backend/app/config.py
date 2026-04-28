import os
from functools import lru_cache
from pathlib import Path
from sys import platform

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


def _default_docker_host() -> str:
    explicit = os.getenv("DOCKER_HOST", "").strip()
    if explicit:
        return explicit

    if platform == "darwin":
        rancher_socket = Path.home() / ".rd" / "docker.sock"
        if rancher_socket.exists():
            return f"unix://{rancher_socket}"

    return "unix:///var/run/docker.sock"


class Settings(BaseSettings):
    # App
    APP_NAME: str = "nova-ve"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://nova:nova@localhost:5432/novadb"

    # Redis (for sessions/cache, optional for MVP)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Paths
    BASE_DATA_DIR: Path = Path("/var/lib/nova-ve")
    LABS_DIR: Path = Path("/var/lib/nova-ve/labs")
    IMAGES_DIR: Path = Path("/var/lib/nova-ve/images")
    TMP_DIR: Path = Path("/var/lib/nova-ve/tmp")
    TEMPLATES_DIR: Path = Path(__file__).resolve().parents[1] / "templates"

    # Auth
    SESSION_COOKIE_NAME: str = "nova_session"
    SESSION_USER_COOKIE: str = "nova_user"
    SESSION_MAX_AGE: int = 14400  # 4 hours
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    COOKIE_SECURE: bool = False  # Set True in production with HTTPS
    COOKIE_SAMESITE: str = "lax"

    # QEMU / System
    QEMU_BINARY: str = "/usr/bin/qemu-system-x86_64"
    QEMU_IMG_BINARY: str = "/usr/bin/qemu-img"
    DOCKER_HOST: str = _default_docker_host()
    GUACAMOLE_PUBLIC_PATH: str = "/html5/"
    GUACAMOLE_INTERNAL_URL: str = "http://127.0.0.1:8081/html5/"
    GUACAMOLE_TARGET_HOST: str = "host.docker.internal"
    GUACAMOLE_DATABASE_URL: str = ""
    GUACAMOLE_DATA_SOURCE: str = "postgresql"
    GUACAMOLE_JSON_SECRET_KEY: str = ""
    GUACAMOLE_JSON_EXPIRE_SECONDS: int = 300
    GUACAMOLE_TERMINAL_FONT_NAME: str = "Roboto Mono"
    GUACAMOLE_TERMINAL_FONT_SIZE: int = 10

    # Reconciliation / discovery (US-402)
    # The discovery loop polls kernel-side bridge state every N seconds and
    # cross-references against ``links[]`` in lab.json.  Operators tune this
    # via ``NOVA_VE_DISCOVERY_CADENCE_SECONDS``.  Live edits land within one
    # cycle because ``_discovery_loop`` reads ``get_settings()`` per
    # iteration; reload via ``get_settings.cache_clear()``.
    DISCOVERY_CADENCE_SECONDS: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "DISCOVERY_CADENCE_SECONDS",
            "NOVA_VE_DISCOVERY_CADENCE_SECONDS",
        ),
    )

    @field_validator("DISCOVERY_CADENCE_SECONDS")
    @classmethod
    def _validate_discovery_cadence(cls, value: int) -> int:
        if not 5 <= value <= 300:
            raise ValueError(
                "DISCOVERY_CADENCE_SECONDS must be between 5 and 300 seconds "
                f"(got {value})"
            )
        return value

    class Config:
        env_file = ".env"
        case_sensitive = True
        populate_by_name = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()
