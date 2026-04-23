from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path


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
    DOCKER_HOST: str = "unix:///var/run/docker.sock"
    GUACAMOLE_PUBLIC_PATH: str = "/html5/"
    GUACAMOLE_INTERNAL_URL: str = "http://127.0.0.1:8081/html5/"
    GUACAMOLE_TARGET_HOST: str = "host.docker.internal"
    GUACAMOLE_JSON_SECRET_KEY: str = ""
    GUACAMOLE_JSON_EXPIRE_SECONDS: int = 300

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()
