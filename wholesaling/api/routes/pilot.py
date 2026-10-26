"""``GET /pilot/report``: the latest synthetic-pilot report (REQUIREMENTS.md BO-09).

Operator-authenticated like ``/review``. The pilot runner stores its report as a
``pilot.completed`` audit event in the database it ran against; when the API shares that
database the newest one is served. Otherwise the JSON file named by ``PILOT_REPORT_PATH``
(default ``pilot-report.json`` in the working directory, what ``wholesaling-pilot run --json``
writes) is used. ``?format=json|text|markdown`` picks the render; 404 when no report exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select

from wholesaling.api.auth import Operator
from wholesaling.api.deps import SessionDep
from wholesaling.db.models.ops import AuditEvent
from wholesaling.pilot.report import PilotReport

router = APIRouter(tags=["pilot"])

PILOT_REPORT_EVENT = "pilot.completed"
PILOT_REPORT_PATH_ENV = "PILOT_REPORT_PATH"
DEFAULT_REPORT_PATH = "pilot-report.json"

Format = Literal["json", "text", "markdown"]


def report_path() -> Path:
    return Path(os.environ.get(PILOT_REPORT_PATH_ENV, DEFAULT_REPORT_PATH))


async def latest_report_payload(session: Any) -> dict[str, Any] | None:
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.event_type == PILOT_REPORT_EVENT)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    event = (await session.execute(stmt)).scalars().first()
    if event is not None and event.payload:
        return dict(event.payload)
    path = report_path()
    if path.is_file():
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    return None


@router.get("/pilot/report")
async def pilot_report(
    operator: Operator,
    session: SessionDep,
    format: Annotated[Format, Query()] = "json",
) -> Response:
    payload = await latest_report_payload(session)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "no pilot report: run `wholesaling-pilot run --json pilot-report.json` against "
                "this database or set PILOT_REPORT_PATH"
            ),
        )
    if format == "json":
        return JSONResponse(payload)
    report = PilotReport.from_dict(payload)
    if format == "markdown":
        return PlainTextResponse(report.render_markdown(), media_type="text/markdown")
    return PlainTextResponse(report.render_text())


__all__ = [
    "DEFAULT_REPORT_PATH",
    "PILOT_REPORT_EVENT",
    "PILOT_REPORT_PATH_ENV",
    "latest_report_payload",
    "pilot_report",
    "report_path",
    "router",
]
