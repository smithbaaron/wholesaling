"""/health and /ready against create_app with SQLite in-memory and TEMPORAL_MODE=mock.

These tests build their own settings and do not use the shared ``engine`` fixture, so they
run even while ``wholesaling.db.models`` is still being written.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from wholesaling import __version__
from wholesaling.api.main import create_app
from wholesaling.config.settings import Settings
from wholesaling.db.engine import make_engine
from wholesaling.integrations.temporal.mock import MockTemporalGateway


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        temporal_mode="mock",
    )


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app(_settings())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_health_is_live_and_reports_version(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


async def test_ready_ok_with_sqlite_and_mock_temporal(client: AsyncClient, app: FastAPI) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["failing"] == []
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["temporal"] == {"ok": True, "mode": "mock", "detail": "mock gateway"}
    gateway = app.state.temporal
    assert isinstance(gateway, MockTemporalGateway)
    assert [c.method for c in gateway.calls] == ["connect", "healthcheck"]


async def test_ready_503_when_database_is_broken(
    client: AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = make_engine("sqlite+aiosqlite:////nonexistent-dir/for-sure/x.db")
    monkeypatch.setattr(app.state, "engine", broken)
    try:
        r = await client.get("/ready")
    finally:
        await broken.dispose()
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["failing"] == ["database"]
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["database"]["detail"]
    assert body["checks"]["temporal"]["ok"] is True


async def test_ready_503_names_temporal_when_unhealthy(client: AsyncClient, app: FastAPI) -> None:
    gateway = app.state.temporal
    assert isinstance(gateway, MockTemporalGateway)
    gateway.healthy = False
    r = await client.get("/ready")
    assert r.status_code == 503
    assert r.json()["failing"] == ["temporal"]


def test_create_app_defaults_to_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from wholesaling.config.settings import get_settings

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEMPORAL_MODE", "mock")
    get_settings.cache_clear()
    try:
        application = create_app()
        assert application.title == "Wholesaling Automation"
        assert {"/health", "/ready"} <= set(application.openapi()["paths"])
    finally:
        get_settings.cache_clear()
