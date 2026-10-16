"""Brings the database schema up to date on startup, so new versions of the app never break existing data."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False  # keep the app's logging setup
    return cfg


def _stamp(conn: Connection, revision: str) -> None:
    conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    conn.execute(text("DELETE FROM alembic_version"))
    conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": revision})


def _inspect(conn: Connection) -> tuple[set[str], set[str], str | None]:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    task_cols = {c["name"] for c in insp.get_columns("tasks")} if "tasks" in tables else set()
    if "time_blocks" in tables:
        task_cols |= {f"time_blocks.{c['name']}" for c in insp.get_columns("time_blocks")}
    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar() if "alembic_version" in tables else None
    return tables, task_cols, version


def _legacy_revision(tables: set[str], task_cols: set[str]) -> str:
    """Databases made by create_all before migrations were tracked: work out which schema they have."""
    if "time_blocks.pinned" in task_cols and "remind_before_min" in task_cols:
        return "0004"
    if "routines" in tables and "started_at" in task_cols:
        return "0003"
    if "push_subscriptions" in tables:
        return "0002"
    return "0001"


async def prepare_database(engine: AsyncEngine, url: str) -> str:
    """Create a new database, or migrate an existing one to the latest schema. Returns what it did."""
    cfg = alembic_config(url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    async with engine.begin() as conn:
        tables, task_cols, version = await conn.run_sync(_inspect)

    if not tables - {"alembic_version"}:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_stamp, head)
        return "created"

    if version is None:
        version = _legacy_revision(tables, task_cols)
        log.info("Database has no migration history; recognised it as revision %s", version)
        async with engine.begin() as conn:
            await conn.run_sync(_stamp, version)

    if version == head:
        return "up to date"
    log.info("Upgrading database schema %s -> %s", version, head)
    await engine.dispose()  # release our connections so the migration can lock the database
    await asyncio.to_thread(command.upgrade, cfg, "head")
    return "upgraded"
