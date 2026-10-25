"""On-demand view of the daily digest / BO-21 KPIs (REQUIREMENTS.md BO-21 "delivered as part of
the daily digest / review queue page").

- ``GET /digest``          HTML: today's KPIs, rolling 7 days, queue depth, promotion readiness.
- ``GET /digest?day=...``  same for another local day (``YYYY-MM-DD`` in ``OPERATOR_TIMEZONE``).
- ``GET /api/digest``      JSON ``KpiReport``.
- ``GET /digest.txt``      the plain-text digest exactly as the sinks receive it.

The scheduled delivery is ``workflows/daily_digest.py``; this page only reads.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from wholesaling.api.auth import Operator
from wholesaling.api.deps import SessionDep, SettingsDep, TemplatesDep
from wholesaling.reporting.digest import render_digest
from wholesaling.reporting.kpis import KpiReport, compute_report

router = APIRouter(tags=["digest"])


def _parse_day(value: str | None, tz: str) -> date:
    if not value:
        return datetime.now(tz=ZoneInfo(tz)).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD") from exc


async def _report(session: SessionDep, settings: SettingsDep, day: str | None) -> KpiReport:
    tz = settings.operator_timezone
    return await compute_report(session, _parse_day(day, tz), tz=tz, now=datetime.now(tz=UTC))


@router.get("/digest", response_class=HTMLResponse)
async def digest_page(
    request: Request,
    operator: Operator,
    session: SessionDep,
    settings: SettingsDep,
    templates: TemplatesDep,
    day: Annotated[str | None, Query()] = None,
) -> Response:
    report = await _report(session, settings, day)
    return templates.TemplateResponse(
        request,
        "digest_page.html",
        {
            "operator": operator,
            "report": report,
            "today": report.today,
            "week": report.rolling_7d,
            "digest_hour": settings.digest_hour_local,
            "timezone": settings.operator_timezone,
            "slack_enabled": settings.slack_webhook_url is not None,
        },
    )


@router.get("/api/digest")
async def digest_json(
    operator: Operator,
    session: SessionDep,
    settings: SettingsDep,
    day: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    report = await _report(session, settings, day)
    return report.as_dict()


@router.get("/digest.txt", response_class=PlainTextResponse)
async def digest_text(
    operator: Operator,
    session: SessionDep,
    settings: SettingsDep,
    day: Annotated[str | None, Query()] = None,
) -> Response:
    report = await _report(session, settings, day)
    rendered = render_digest(report)
    return PlainTextResponse(rendered.text)


__all__ = ["digest_json", "digest_page", "digest_text", "router"]
