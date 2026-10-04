"""Application settings, loaded from environment or a local .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NBO_", env_file=BASE_DIR / ".env", extra="ignore"
    )

    secret_key: str = "dev-only-insecure-secret-change-me"
    database_url: str = f"sqlite:///{(DATA_DIR / 'nbo.db').as_posix()}"
    debug: bool = True

    # Optional first-admin bootstrap: when both are set, startup creates this
    # admin if no user with that email exists. Never overwrites an existing user.
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    # i18n
    default_locale: str = "ru"
    locales: tuple[str, ...] = ("ru", "ky", "en")

    # Sessions
    session_cookie: str = "nbo_session"
    session_max_age: int = 60 * 60 * 24  # seconds
    cookie_secure: bool = False  # set true behind HTTPS

    # Networking / anti-cheat
    trusted_proxy_hops: int = 0

    # Anti-cheat thresholds (all overridable per exam)
    ip_shared_account_threshold: int = 3  # distinct accounts per IP before flagging
    ip_hard_block_unallowlisted: bool = True
    min_seconds_per_question: float = 4.0  # faster than this looks automated
    collusion_similarity_threshold: float = 0.93

    @property
    def upload_dir(self) -> Path:
        return UPLOAD_DIR


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
