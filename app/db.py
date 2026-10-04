"""Database engine, session factory and declarative base."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EnumType(TypeDecorator):
    """Store a ``StrEnum`` as text, and load it back as the enum member.

    With a plain ``String`` column the value survives a round-trip as a bare
    ``str``, so ``attempt.status is AttemptStatus.IN_PROGRESS`` is True for an
    object built in Python and False for the same row loaded from the database -
    silently, and only in the paths that read persisted state. Converting on load
    keeps identity comparisons meaningful everywhere.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls, length: int = 32, **kwargs):
        self.enum_cls = enum_cls
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return self.enum_cls(value).value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self.enum_cls(value)


class UtcDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC, in Python and in the database.

    SQLite has no native timestamp type and hands back naive datetimes, so a
    value written as aware comes back naive and then raises
    ``can't compare offset-naive and offset-aware datetimes`` the moment it is
    compared against ``utcnow()`` - which is exactly what every deadline,
    exam-window and expiry check does. Normalising on the way in and on the way
    out makes SQLite behave like Postgres's ``timestamptz`` here.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = dict(pool_pre_ping=True, future=True)
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10

engine = create_engine(settings.database_url, **_engine_kwargs)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """WAL + foreign keys make SQLite behave close enough to Postgres for us."""
    if not _is_sqlite:
        return
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base with the columns every table carries."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow
    )


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
