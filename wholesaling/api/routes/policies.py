"""The policy page (REQUIREMENTS.md POL-01..07, RUL-03).

- ``GET /policies``            HTML: the 12 actions with level, behaviour at that level, daily cap,
                               QA threshold, and read-only POL-04 promotion readiness.
- ``GET /api/policies``        JSON variant.
- ``POST /policies/{action}``  change level / cap / threshold / note. Hard-floor actions
                               (contract execution, earnest money) cannot leave L0: the repository
                               raises ``HardFloorViolation`` and the route answers 400 with the
                               reason. Every change writes an ``audit_events`` row with
                               ``actor = human`` and the operator username (POL-01: a level change
                               is a human-visible event).

Nothing here promotes or demotes automatically (POL-07 is a later build; this build never
promotes anything, POL-04 note).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.api.auth import Operator
from wholesaling.api.deps import SessionDep, TemplatesDep, is_htmx, wants_html
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.policies import HardFloorViolation, PolicyRepository
from wholesaling.logging import get_logger
from wholesaling.policy.actions import (
    HARD_FLOOR_ACTIONS,
    POLICY_TABLE,
    AutonomyLevel,
    PolicyAction,
)
from wholesaling.policy.engine import DEFAULT_QA_CONFIDENCE_THRESHOLD
from wholesaling.reporting.kpis import PromotionReadiness, all_promotion_readiness

log = get_logger(__name__)
router = APIRouter(tags=["policies"])

EVENT_POLICY_CHANGED = "policy.changed"


def _policy_view(
    action: PolicyAction,
    row: Any | None,
    readiness: PromotionReadiness | None,
) -> dict[str, Any]:
    level = row.level if row is not None else AutonomyLevel.L0
    behaviour, condition = POLICY_TABLE[action][level]
    threshold = row.threshold if row is not None and row.threshold is not None else None
    return {
        "action": action.value,
        "level": level.value,
        "behaviour": behaviour.value,
        "condition": condition,
        "daily_cap": row.daily_cap if row is not None else None,
        "threshold": float(threshold) if threshold is not None else None,
        "threshold_effective": float(threshold or DEFAULT_QA_CONFIDENCE_THRESHOLD),
        "hard_floor": action in HARD_FLOOR_ACTIONS,
        "updated_by": row.updated_by if row is not None else None,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
        "note": row.note if row is not None else None,
        "levels": {
            lvl.value: {"behaviour": b.value, "condition": c}
            for lvl, (b, c) in POLICY_TABLE[action].items()
        },
        "readiness": readiness.as_dict() if readiness is not None else None,
    }


async def _views(session: AsyncSession, *, now: datetime | None = None) -> list[dict[str, Any]]:
    rows = await PolicyRepository(session).all()
    readiness = {r.action: r for r in await all_promotion_readiness(session, now=now)}
    return [_policy_view(a, rows.get(a), readiness.get(a)) for a in PolicyAction]


@router.get("/policies", response_class=HTMLResponse)
async def policies_page(
    request: Request, operator: Operator, session: SessionDep, templates: TemplatesDep
) -> Response:
    return templates.TemplateResponse(
        request,
        "policies.html",
        {
            "operator": operator,
            "policies": await _views(session),
            "levels": [lvl.value for lvl in AutonomyLevel],
            "now": datetime.now(tz=UTC),
        },
    )


@router.get("/api/policies")
async def policies_json(operator: Operator, session: SessionDep) -> dict[str, Any]:
    views = await _views(session)
    return {"count": len(views), "policies": views}


def _parse_action(value: str) -> PolicyAction:
    try:
        return PolicyAction(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"unknown policy action {value!r}") from exc


def _parse_level(value: str | None) -> AutonomyLevel | None:
    if value is None or value == "":
        return None
    try:
        return AutonomyLevel(value.upper())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="level must be one of L0, L1, L2") from exc


def _parse_cap(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        cap = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="daily_cap must be an integer") from exc
    if cap < 0:
        raise HTTPException(status_code=422, detail="daily_cap must be >= 0")
    return cap


def _parse_threshold(value: str | None) -> Decimal | None:
    if value is None or value.strip() == "":
        return None
    try:
        threshold = Decimal(value)
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="threshold must be a number") from exc
    if not Decimal("0") <= threshold <= Decimal("1"):
        raise HTTPException(status_code=422, detail="threshold must be between 0 and 1")
    return threshold


@router.post("/policies/{action}")
async def change_policy(
    request: Request,
    operator: Operator,
    session: SessionDep,
    templates: TemplatesDep,
    action: str,
    level: Annotated[str | None, Form()] = None,
    daily_cap: Annotated[str | None, Form()] = None,
    threshold: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> Response:
    policy_action = _parse_action(action)
    new_level = _parse_level(level)
    new_cap = _parse_cap(daily_cap)
    new_threshold = _parse_threshold(threshold)
    new_note = (note or "").strip() or None
    if new_level is None and new_cap is None and new_threshold is None and new_note is None:
        raise HTTPException(status_code=422, detail="nothing to change")

    repo = PolicyRepository(session)
    before_row = await repo.get(policy_action)
    before = {
        "level": before_row.level.value if before_row else AutonomyLevel.L0.value,
        "daily_cap": before_row.daily_cap if before_row else None,
        "threshold": str(before_row.threshold) if before_row and before_row.threshold else None,
        "note": before_row.note if before_row else None,
    }
    target_level = new_level or (before_row.level if before_row else AutonomyLevel.L0)
    try:
        row = await repo.set_level(
            policy_action,
            target_level,
            updated_by=operator,
            daily_cap=new_cap,
            threshold=new_threshold,
            note=new_note,
        )
    except HardFloorViolation as exc:
        await session.rollback()
        log.warning("policy.hard_floor_rejected", action=policy_action.value, operator=operator)
        detail = str(exc)
        if wants_html(request):
            return templates.TemplateResponse(
                request,
                "_policy_result.html",
                {"ok": False, "action": policy_action.value, "detail": detail},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return JSONResponse({"detail": detail}, status_code=status.HTTP_400_BAD_REQUEST)

    after = {
        "level": row.level.value,
        "daily_cap": row.daily_cap,
        "threshold": str(row.threshold) if row.threshold is not None else None,
        "note": row.note,
    }
    await AuditRepository(session).record(
        actor=Actor.HUMAN,
        actor_id=operator,
        event_type=EVENT_POLICY_CHANGED,
        entity_type="policy",
        entity_id=row.id,
        payload={"action": policy_action.value, "before": before, "after": after},
    )
    await session.commit()
    log.info(
        "policy.changed",
        action=policy_action.value,
        operator=operator,
        before=before,
        after=after,
    )
    readiness = {r.action: r for r in await all_promotion_readiness(session)}
    view = _policy_view(policy_action, row, readiness.get(policy_action))
    if is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_policy_row.html",
            {"p": view, "levels": [lvl.value for lvl in AutonomyLevel], "flash": "saved"},
        )
    if wants_html(request):  # plain form post without JS: back to the table
        return RedirectResponse("/policies", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse({"ok": True, "policy": view, "before": before, "after": after})


__all__ = ["EVENT_POLICY_CHANGED", "change_policy", "policies_json", "policies_page", "router"]
