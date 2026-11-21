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


def _validate_database_url_value(value: str) -> str:
    cleaned = value.strip()
    if "nova:nova" in cleaned:
        raise ValueError(
            "DATABASE_URL must not use the example credentials (nova:nova); "
            "set a strong database password"
        )
    return cleaned


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
    # SECRET_KEY is resolved by _resolve_secret_key() after model construction.
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
    IOURC_DIR: Path = Path("/var/lib/nova-ve/iourc")
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

    # External console launch (#269). Hostname or IPv4 address operators use
    # on their workstation to reach node console listeners (e.g. the server's
    # LAN address). Used verbatim in the telnet://, vnc:// and rdp:// URLs the
    # external-console endpoint advertises — never credentials, never a port.
    #
    # Advertisement-only since #276: console listeners always bind the
    # wildcard on fixed per-protocol port ranges (below) — the HTML5 path
    # (guacd via host.docker.internal) requires that, and native clients
    # share the same surface. This setting only gates whether external
    # links are emitted; leaving it empty keeps external launching
    # disabled (the endpoint refuses and the UI falls back to the
    # in-browser console).
    #
    # SECURITY: raw protocol connections on the console port ranges bypass
    # the HTML5 session layer, so those ports are reachable by anything
    # that can route to this host — scope them with a firewall. The fixed
    # ranges exist precisely so such an allow-list is practical.
    EXTERNAL_CONSOLE_HOST: str = ""

    # Console port ranges (#276). Public host-side listener ports for node
    # consoles, allocated first-free per running node and persisted on the
    # runtime record. One range per protocol so operators can allow-list
    # exactly the console types they expose. The telnet/vnc defaults sit
    # below the Linux ephemeral port range; RDP overlaps it by design
    # (see #276's deploy note on ip_local_port_range).
    CONSOLE_PORT_RANGE_TELNET: str = Field(
        default="15000-20000",
        validation_alias=AliasChoices("CONSOLE_PORT_RANGE_TELNET", "NOVA_VE_CONSOLE_PORT_RANGE_TELNET"),
    )
    CONSOLE_PORT_RANGE_VNC: str = Field(
        default="25000-30000",
        validation_alias=AliasChoices("CONSOLE_PORT_RANGE_VNC", "NOVA_VE_CONSOLE_PORT_RANGE_VNC"),
    )
    CONSOLE_PORT_RANGE_RDP: str = Field(
        default="35000-40000",
        validation_alias=AliasChoices("CONSOLE_PORT_RANGE_RDP", "NOVA_VE_CONSOLE_PORT_RANGE_RDP"),
    )

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value or not value.strip():
            return ""
        return _validate_database_url_value(value)

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

    @field_validator(
        "CONSOLE_PORT_RANGE_TELNET",
        "CONSOLE_PORT_RANGE_VNC",
        "CONSOLE_PORT_RANGE_RDP",
    )
    @classmethod
    def _validate_console_port_range(cls, value: str) -> str:
        # "#276" ranges: "lo-hi", 1024 <= lo < hi <= 65535. VNC additionally
        # needs lo >= 5900 — QEMU binds VNC via display arithmetic
        # (-vnc :<port-5900>), which requires a non-negative display.
        import re

        text = str(value).strip()
        match = re.fullmatch(r"(\d{1,5})-(\d{1,5})", text)
        if not match:
            raise ValueError(
                f"Console port range must be formatted 'lo-hi' (got {value!r})"
            )
        lo, hi = int(match.group(1)), int(match.group(2))
        if not 1024 <= lo < hi <= 65535:
            raise ValueError(
                f"Console port range must satisfy 1024 <= lo < hi <= 65535 (got {value!r})"
            )
        return f"{lo}-{hi}"

    @field_validator("CONSOLE_PORT_RANGE_VNC")
    @classmethod
    def _validate_console_port_range_vnc_floor(cls, value: str) -> str:
        lo = int(str(value).strip().split("-", 1)[0])
        if lo < 5900:
            raise ValueError(
                "CONSOLE_PORT_RANGE_VNC must start at 5900 or above — QEMU VNC "
                "listeners bind via display arithmetic (port = 5900 + display)"
            )
        return value

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
        settings.SECRET_KEY = settings.SECRET_KEY.strip()
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
    key_file.chmod(0o600)
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
        settings.DATABASE_URL = _validate_database_url_value(settings.DATABASE_URL)
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

    settings.DATABASE_URL = _validate_database_url_value(
        f"postgresql+asyncpg://{settings.DB_USER}:{password}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )


@lru_cache()
def get_settings() -> Settings:
    settings = Settings()
    _resolve_database_url(settings)
    _resolve_secret_key(settings)
    _apply_persisted_settings_overrides(settings)
    return settings


# Editable settings subset (#265). The settings UI persists these to
# BASE_DATA_DIR/system-settings.json with "applies on backend restart"
# semantics; get_settings() applies them to the cached instance on first
# construction (that application point is what makes the restart matter).
# Kept here (not in the settings router) so boot-time application and the
# PUT/GET validation surface cannot drift apart.
EDITABLE_SETTING_KEYS = (
    "DISCOVERY_CADENCE_SECONDS",
    "NAT_CLOUD_POOL",
    "SESSION_MAX_AGE",
    "EXTERNAL_CONSOLE_HOST",
)


def _apply_persisted_settings_overrides(settings: Settings) -> None:
    """Apply persisted settings overrides to a freshly built instance (#265).

    The router validates values on write; entries are still coerced
    defensively here — an invalid or unknown entry is logged and skipped so
    a hand-edited file cannot keep the backend from booting.
    """
    import json

    path = settings.BASE_DATA_DIR / "system-settings.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return

    for key, value in raw.items():
        if key not in EDITABLE_SETTING_KEYS or not hasattr(settings, key):
            continue
        try:
            current = getattr(settings, key)
            if isinstance(current, bool):
                applied: object = str(value).strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int):
                applied = int(value)
            else:
                applied = str(value).strip()
        except (TypeError, ValueError):
            logger.warning(
                "Skipping invalid persisted setting override %s=%r", key, value
            )
            continue
        if applied != current:
            setattr(settings, key, applied)
            logger.info("Applied persisted setting override %s=%r", key, applied)
