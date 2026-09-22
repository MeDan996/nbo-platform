"""Alembic environment.

The database URL comes from the application settings rather than alembic.ini, so
`NBO_DATABASE_URL` is the single place that decides which database is in use for
both the app and its migrations.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import Base, EnumType, UtcDateTime  # noqa: E402
import app.models  # noqa: F401,E402  (import side effect: registers every table)


def render_item(type_, obj, autogen_context):
    """Render the app's TypeDecorators as the plain types they store as.

    `EnumType` and `UtcDateTime` are Python-side conveniences; in the database
    they are just VARCHAR and TIMESTAMP. Rendering them by name would emit
    `app.db.EnumType(length=32)` into the migration, which cannot be constructed
    because the enum class is not part of that call - and would needlessly tie
    the migration history to application code that will keep changing.
    """
    if type_ != "type":
        return False
    if isinstance(obj, EnumType):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.String(length=%d)" % (obj.length or 32)
    if isinstance(obj, UtcDateTime):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.DateTime(timezone=True)"
    return False


config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead, and is a no-op on Postgres.
        render_as_batch=settings.database_url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
            render_as_batch=settings.database_url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
