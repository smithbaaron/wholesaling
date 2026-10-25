"""Alembic environment (async; docs/adr/0002-portable-schema.md).

URL resolution order: ``-x url=...`` on the command line, then ``sqlalchemy.url`` in the ini
(unset by default), then ``DATABASE_URL`` via ``wholesaling.config.settings``. Tests pass an
open connection through ``config.attributes["connection"]`` and run on SQLite in memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

import wholesaling.db.models  # noqa: F401  (registers every table on Base.metadata)
from wholesaling.db.base import Base

config = context.config
target_metadata = Base.metadata


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    # Alembic's own bookkeeping table is not part of the model.
    return not (type_ == "table" and name == "alembic_version")


def resolve_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if url := x_args.get("url"):
        return str(url)
    if ini_url := config.get_main_option("sqlalchemy.url"):
        return ini_url
    from wholesaling.config.settings import get_settings

    return get_settings().database_url


def _configure(connection: Connection | None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=False,
        literal_binds=connection is None,
        dialect_opts={"paramstyle": "named"} if connection is None else None,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection."""
    _configure(None, url=resolve_url())
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations(url: str) -> None:
    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = url
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    provided: Connection | None = config.attributes.get("connection")
    if provided is not None:
        do_run_migrations(provided)
        return
    url = resolve_url()
    drivername = make_url(url).get_dialect().driver
    if make_url(url).get_dialect().is_async or drivername in {"asyncpg", "aiosqlite"}:
        asyncio.run(run_async_migrations(url))
        return
    # Sync driver (e.g. psycopg2 or plain sqlite) — connect directly.
    from sqlalchemy import create_engine

    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        do_run_migrations(connection)
    engine.dispose()


runner: Callable[[], None]
runner = run_migrations_offline if context.is_offline_mode() else run_migrations_online
runner()
