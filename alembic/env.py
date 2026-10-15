"""Alembic environment. Uses the app's own settings (DATABASE_URL) and models, async engine."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import models  # noqa: F401  (importing registers the tables)
from app.db.base import Base
from app.db.session import make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
# The app passes its own URL when it migrates on startup; the alembic CLI uses DATABASE_URL.
URL = config.attributes.get("database_url") or get_settings().DATABASE_URL


def render_item(type_, obj, autogen_context):
    """Migrations must not import app code: write our UTC type as the plain column type it is."""
    if type_ == "type" and obj.__class__.__name__ == "UTCDateTime":
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True, render_as_batch=True,
                      render_item=render_item)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    # render_as_batch: SQLite can't ALTER most things, so Alembic copies the table instead.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True,
                      render_item=render_item)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(URL)
    async with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            # SQLite migrations rebuild tables (copy, drop, rename). With foreign keys enforced, dropping the old
            # table would CASCADE-delete reminders and unlink time blocks. Must be set outside a transaction.
            await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await connection.commit()
        await connection.run_sync(_run)
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
