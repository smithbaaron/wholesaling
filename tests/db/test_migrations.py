"""Migration 0001 must produce exactly ``Base.metadata`` and seed the policy rows.

REQUIREMENTS.md DM-04, POL-02.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import wholesaling.db.models as models
from wholesaling.db import Base, make_engine
from wholesaling.db.models import Policy
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

REPO_ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "wholesaling" / "db" / "migrations"))
    return cfg


def _upgrade(conn: Connection, revision: str = "head") -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, revision)


def _downgrade(conn: Connection, revision: str = "base") -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    command.downgrade(cfg, revision)


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(
        conn,
        opts={
            "compare_type": True,
            "compare_server_default": False,
            "render_as_batch": True,
            "include_object": lambda obj, name, type_, reflected, compare_to: (
                not (type_ == "table" and name == "alembic_version")
            ),
        },
    )
    return compare_metadata(ctx, Base.metadata)


def _table_names(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names()) - {"alembic_version"}


async def _run_checks(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
        tables = await conn.run_sync(_table_names)
        assert tables == set(models.TABLE_NAMES), tables ^ set(models.TABLE_NAMES)
        assert len(tables) == 20
        diff = await conn.run_sync(_diff)
        assert diff == [], f"migration drifts from Base.metadata: {diff}"

    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessions() as session:
        rows = (await session.execute(select(Policy))).scalars().all()
        assert {r.action for r in rows} == set(PolicyAction)
        assert len(rows) == 12
        assert all(r.level is AutonomyLevel.L0 for r in rows)

    async with engine.begin() as conn:
        await conn.run_sync(_downgrade)
        assert await conn.run_sync(_table_names) == set()
        # Upgrade is repeatable after a full downgrade.
        await conn.run_sync(_upgrade)
        assert len(await conn.run_sync(_table_names)) == 20


async def test_upgrade_head_matches_metadata_sqlite(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}")
    try:
        await _run_checks(engine)
    finally:
        await engine.dispose()


async def test_create_all_matches_migration_sqlite(tmp_path: Path) -> None:
    """``Base.metadata.create_all`` (used by the test fixtures) and the migration agree."""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'create_all.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            diff = await conn.run_sync(_diff)
            assert diff == []
    finally:
        await engine.dispose()


async def test_no_dialect_specific_types_in_metadata() -> None:
    """ADR 0002: every column type compiles on both SQLite and Postgres."""
    from sqlalchemy.dialects import postgresql, sqlite

    for table in Base.metadata.tables.values():
        for column in table.columns:
            for dialect in (sqlite.dialect(), postgresql.dialect()):
                compiled = column.type.compile(dialect=dialect)
                assert compiled, (table.name, column.name)
                if dialect.name == "postgresql":
                    # No native ENUM types: enums are VARCHAR + CHECK.
                    assert not compiled.upper().startswith("ENUM"), (table.name, column.name)


@pytest.mark.postgres
async def test_upgrade_head_matches_metadata_postgres() -> None:
    url = os.environ["TEST_DATABASE_URL"]
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await _run_checks(engine)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()
