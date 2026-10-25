"""Liveness and readiness endpoints (REQUIREMENTS.md WF-23, BO-03).

- ``GET /health``  liveness: always 200 while the process answers; returns the version.
- ``GET /ready``   readiness: ``SELECT 1`` through the async engine plus the Temporal gateway
  healthcheck. 503 with a JSON body naming each failing component when anything is down.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from wholesaling import __version__
from wholesaling.integrations.base import HealthStatus
from wholesaling.integrations.temporal.interface import TemporalGateway
from wholesaling.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["health"])


async def check_database(engine: AsyncEngine) -> HealthStatus:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # readiness must report, never raise
        return HealthStatus(name="database", mode="live", ok=False, detail=str(exc))
    return HealthStatus(name="database", mode="live", ok=True)


async def check_temporal(gateway: TemporalGateway) -> HealthStatus:
    try:
        return await gateway.healthcheck()
    except Exception as exc:
        return HealthStatus(name="temporal", mode=gateway.mode, ok=False, detail=str(exc))


def _as_json(status_: HealthStatus) -> dict[str, Any]:
    return {"ok": status_.ok, "mode": status_.mode, "detail": status_.detail}


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    engine: AsyncEngine = request.app.state.engine
    gateway: TemporalGateway = request.app.state.temporal

    checks = [await check_database(engine), await check_temporal(gateway)]
    failing = [c.name for c in checks if not c.ok]
    body: dict[str, Any] = {
        "status": "ok" if not failing else "unavailable",
        "version": __version__,
        "checks": {c.name: _as_json(c) for c in checks},
        "failing": failing,
    }
    if failing:
        log.warning("readiness.failed", failing=failing)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return body
