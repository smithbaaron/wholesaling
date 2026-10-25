"""Async engine and session factory."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wholesaling.config import get_settings


def make_engine(url: str, **kwargs: Any) -> AsyncEngine:
    is_sqlite = url.startswith("sqlite")
    engine_kwargs: dict[str, Any] = {"pool_pre_ping": not is_sqlite, "future": True}
    engine_kwargs.update(kwargs)
    engine = create_async_engine(url, **engine_kwargs)
    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_fks(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return make_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory(get_engine())
