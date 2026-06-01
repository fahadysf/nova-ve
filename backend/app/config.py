import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from sys import platform

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger("nova-ve")

_KNOWN_WEAK_KEYS = frozenset({"change-me-in-production", "dev-secret-change-me"})


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
    # SECRET_KEY is resolved by _ensure_secret_key() after model construction.
    # When set via env / .env file it's used directly (after validation).
    # When NOT set, the key is auto-generated on first run and persisted to
    # BASE_DATA_DIR/secret_key so it survives restarts.
    SECRET_KEY: str = ""

    # Database
    DATABASE_URL: str = ""
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "nova"
    DB_NAME: str = "novadb"

    # Redis (for sessions/cache, optional for MVP)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Paths
    BASE_DATA_DIR: Path = Path("/var/lib/nova-ve")
    LABS_DIR: Path = Path("/var/lib/nova-ve/labs")
    IMAGES_DIR: Path = Path("/var/lib/nova-ve/images")
    TMP_DIR: Path = Path("/var/lib/nova-ve/tmp")
    TEMPLATES_DIR: Path = Path(__file__).resolve().parents[1] / "templates"
    # USER_TEMPLATES_DIR (#185): operator-imported templates (e.g. from the
    # EVE-NG importer #182). Walked alongside the builtin TEMPLATES_DIR; on
    # filename collisions, user-dir entries shadow builtin and a WARNING is
    # logged. Additive (TEMPLATES_DIR is preserved verbatim) so existing
    # /etc/nova-ve/backend.env files don't need changes.
    USER_TEMPLATES_DIR: Path = Path("/var/lib/nova-ve/templates")

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
    NAT_CLOUD_POOL: str = Field(
        default="10.255.0.0/16",
        validation_alias=AliasChoices("NAT_CLOUD_POOL", "NOVA_VE_NAT_CLOUD_POOL"),
    )

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "DATABASE_URL must be set via environment variable or .env file"
            )
        if "nova:nova" in value:
            raise ValueError(
                "DATABASE_URL must not use the example credentials (nova:nova); "
                "set a strong database password"
            )
        return value.strip()

    @field_validator("SECRET_KEY", mode="before")
    @classmethod
    def _reject_known_weak_keys(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() in _KNOWN_WEAK_KEYS:
            raise ValueError(
                f"SECRET_KEY must not be a known default ({value!r}); "
                "generate a strong random key or leave it unset for auto-generation"
            )
        if isinstance(value, str) and value.strip() and len(value.strip()) < 32:
            raise ValueError(
                f"SECRET_KEY must be at least 32 characters (got {len(value.strip())})"
            )
        return value

    @field_validator("DISCOVERY_CADENCE_SECONDS")
    @classmethod
    def _validate_discovery_cadence(cls, value: int) -> int:
        if not 5 <= value <= 300:
            raise ValueError(
                "DISCOVERY_CADENCE_SECONDS must be between 5 and 300 seconds "
                f"(got {value})"
            )
        return value

    @field_validator("NAT_CLOUD_POOL")
    @classmethod
    def _validate_nat_cloud_pool(cls, value: str) -> str:
        import ipaddress

        try:
            network = ipaddress.ip_network(str(value).strip(), strict=True)
        except ValueError as exc:
            raise ValueError(f"NAT_CLOUD_POOL must be a valid IPv4 CIDR: {exc}") from exc
        if isinstance(network, ipaddress.IPv6Network):
            raise ValueError("NAT_CLOUD_POOL must be IPv4")
        if network.prefixlen > 24:
            raise ValueError("NAT_CLOUD_POOL must be at least large enough to allocate /24 networks")
        return str(network)

    class Config:
        env_file = ".env"
        case_sensitive = True
        populate_by_name = True


def _resolve_secret_key(settings: Settings) -> None:
    """Auto-generate SECRET_KEY on first run if not explicitly configured.

    When SECRET_KEY is set via env var or .env file (and passes validation),
    it's used as-is. Otherwise a random key is generated and persisted to
    ``BASE_DATA_DIR/secret_key`` so it survives restarts.
    """
    if settings.SECRET_KEY and settings.SECRET_KEY.strip():
        return

    key_file = settings.BASE_DATA_DIR / "secret_key"
    try:
        existing = key_file.read_text(encoding="ascii").strip()
    except (FileNotFoundError, PermissionError):
        existing = ""

    if existing and len(existing) >= 32 and existing not in _KNOWN_WEAK_KEYS:
        settings.SECRET_KEY = existing
        return

    if existing:
        logger.warning(
            "Persisted secret_key is too short or matches a known default; regenerating"
        )

    key = secrets.token_hex(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key, encoding="ascii")
    settings.SECRET_KEY = key
    logger.info("Generated new SECRET_KEY and persisted to %s", key_file)


def _resolve_database_url(settings: Settings) -> None:
    """Auto-construct DATABASE_URL from the persisted db_password file.

    When DATABASE_URL is explicitly set via env var or .env file, it's used
    as-is after validation. Otherwise the password is read from
    ``BASE_DATA_DIR/db_password`` (generated by the db-init compose service
    or the deploy provisioning script).
    """
    if settings.DATABASE_URL and settings.DATABASE_URL.strip():
        return

    pw_file = settings.BASE_DATA_DIR / "db_password"
    try:
        password = pw_file.read_text(encoding="ascii").strip()
    except (FileNotFoundError, PermissionError):
        password = ""

    if not password:
        raise ValueError(
            f"DATABASE_URL is not set and no db_password file found at {pw_file}; "
            "ensure the db-init service or provisioning script has run"
        )

    settings.DATABASE_URL = (
        f"postgresql+asyncpg://{settings.DB_USER}:{password}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )


@lru_cache()
def get_settings() -> Settings:
    settings = Settings()
    _resolve_database_url(settings)
    _resolve_secret_key(settings)
    return settings
