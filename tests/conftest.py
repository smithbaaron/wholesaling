"""Shared fixtures.

Default DB for tests is SQLite in memory (no services). Set TEST_DATABASE_URL to a Postgres URL to
run the same suite against Postgres; tests marked ``postgres`` only run then.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from wholesaling.config.settings import Settings
from wholesaling.db import Base, make_engine, make_session_factory

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def _test_db_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or SQLITE_URL


@pytest.fixture(scope="session")
def test_db_url() -> str:
    return _test_db_url()


@pytest.fixture
def settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Settings:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", _test_db_url())
    monkeypatch.setenv("BLOB_LOCAL_DIR", str(tmp_path / "blobs"))
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
async def engine(test_db_url: str) -> AsyncIterator[AsyncEngine]:
    # Import models so Base.metadata is fully populated before create_all.
    import wholesaling.db.models  # noqa: F401

    eng = make_engine(test_db_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(engine)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"):
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL is not PostgreSQL")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)
