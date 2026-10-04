"""Application settings, loaded from environment or a local .env file."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"


def _normalize_database_url(url: str) -> str:
    """Rewrite dialect prefixes so SQLAlchemy gets the right driver.

    Railway (and Heroku) set ``DATABASE_URL`` with the ``postgres://`` scheme,
    which SQLAlchemy 2.x no longer accepts.  We also make sure the psycopg
    (v3) async driver name is present.
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NBO_", env_file=BASE_DIR / ".env", extra="ignore"
    )

    secret_key: str = "dev-only-insecure-secret-change-me"
    database_url: str = f"sqlite:///{(DATA_DIR / 'nbo.db').as_posix()}"
    debug: bool = True

    @model_validator(mode="before")
    @classmethod
    def _pick_database_url(cls, values):
        """Fall back to the bare DATABASE_URL env var that Railway injects."""
        if "database_url" not in values and not os.environ.get("NBO_DATABASE_URL"):
            raw = os.environ.get("DATABASE_URL")
            if raw:
                values["database_url"] = raw
        return values

    @model_validator(mode="after")
    def _normalise_db_url(self):
        self.database_url = _normalize_database_url(self.database_url)
        return self

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
