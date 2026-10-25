"""FastAPI application factory and the ``wholesaling-api`` script entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from wholesaling import __version__
from wholesaling.api.routes import health
from wholesaling.config.settings import Settings, get_settings
from wholesaling.db.engine import make_engine
from wholesaling.integrations.temporal import get_temporal
from wholesaling.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. ``settings`` defaults to the environment; tests pass their own."""
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = make_engine(settings.database_url)
        app.state.temporal = get_temporal(settings)
        if settings.temporal_mode == "mock":
            await app.state.temporal.connect()
        else:
            # Live Temporal connects lazily inside healthcheck so an unreachable server
            # degrades /ready instead of blocking startup.
            log.info("temporal.lazy_connect", address=settings.temporal_address)
        log.info(
            "api.started",
            version=__version__,
            app_env=settings.app_env.value,
            database=("sqlite" if settings.is_sqlite else "postgresql"),
            temporal_mode=settings.temporal_mode,
            task_queue=settings.temporal_task_queue,
        )
        try:
            yield
        finally:
            await app.state.temporal.close()
            await app.state.engine.dispose()
            log.info("api.stopped")

    app = FastAPI(
        title="Wholesaling Automation",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    app.include_router(health.router)
    return app


def run() -> None:
    """Console-script entrypoint: serve the app with uvicorn on ``PORT``."""
    settings = get_settings()
    if settings.is_production:
        problems = settings.validate_for_production()
        if problems:
            configure_logging(settings)
            log.error("api.refused", problems=problems)
            raise SystemExit("; ".join(problems))
    uvicorn.run(
        "wholesaling.api.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container port binding
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
