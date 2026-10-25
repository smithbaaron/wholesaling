"""The review queue (REQUIREMENTS.md BO-07, POL-01, INV-17/18, ENG-Q13).

- ``GET /review``                    HTML page: queued drafts grouped by policy action.
- ``GET /api/review``                JSON variant of the same list (tests, tooling).
- ``POST /review/{id}/approve``      approve, optionally with ``edited_body`` (edit-and-approve).
- ``POST /review/{id}/reject``       reject with a ``reason``.

Approve / reject semantics (the contract with the workflow builders):

1. resolve the owning Temporal workflow id (``ReviewRepository.resolve_workflow_id``);
2. ``gateway.signal(workflow_id, "human_decision", HumanDecision(...))``;
3. only if the signal succeeded: flip ``messages.policy_decision`` to ``send`` / ``blocked``,
   store the edited body, write a ``review.approved`` / ``review.approved_with_edits`` /
   ``review.rejected`` audit event with ``actor = human`` and the operator username, commit.

If the signal raises, nothing is written and the route answers 502 so the operator retries. A
message that is not (or no longer) queued answers 409; one with no resolvable workflow 422.

The queue is not time-gated (WF-18 was rejected): the operator can act at any hour.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from wholesaling.api.auth import Operator
from wholesaling.api.deps import SessionDep, TemplatesDep, TemporalDep, is_htmx, wants_html
from wholesaling.db.models.enums import Actor, PolicyDecision
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.review import (
    HUMAN_DECISION_SIGNAL,
    HumanDecision,
    QueuedItem,
    ReviewRepository,
)
from wholesaling.logging import get_logger
from wholesaling.policy.actions import PolicyAction

log = get_logger(__name__)
router = APIRouter(tags=["review"])

EVENT_APPROVED = "review.approved"
EVENT_APPROVED_WITH_EDITS = "review.approved_with_edits"
EVENT_REJECTED = "review.rejected"


def _group_by_action(items: list[QueuedItem]) -> OrderedDict[str, list[QueuedItem]]:
    order = [a.value for a in PolicyAction]
    groups: OrderedDict[str, list[QueuedItem]] = OrderedDict()
    for action in sorted(
        {i.action for i in items},
        key=lambda a: (a not in order, order.index(a) if a in order else a),
    ):
        groups[action] = [i for i in items if i.action == action]
    return groups


def _parse_market(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="market must be a UUID") from exc


async def _load(
    session: SessionDep, action: str | None, market: str | None
) -> tuple[list[QueuedItem], list[Any], datetime]:
    repo = ReviewRepository(session)
    items = await repo.list_queue(action=action or None, market_id=_parse_market(market))
    markets = await repo.markets_in_queue()
    return items, markets, datetime.now(tz=UTC)


@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    operator: Operator,
    session: SessionDep,
    templates: TemplatesDep,
    action: Annotated[str | None, Query()] = None,
    market: Annotated[str | None, Query()] = None,
) -> Response:
    items, markets, now = await _load(session, action, market)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "operator": operator,
            "groups": _group_by_action(items),
            "total": len(items),
            "actions": [a.value for a in PolicyAction],
            "markets": markets,
            "selected_action": action or "",
            "selected_market": market or "",
            "now": now,
        },
    )


@router.get("/api/review")
async def review_json(
    operator: Operator,
    session: SessionDep,
    action: Annotated[str | None, Query()] = None,
    market: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    items, _markets, now = await _load(session, action, market)
    return {
        "count": len(items),
        "items": [i.as_dict(now) for i in items],
        "filters": {"action": action, "market": market},
    }


def _not_queued(message_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"message {message_id} is not in the review queue",
    )


async def _decide(
    *,
    request: Request,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    operator: str,
    message_id: uuid.UUID,
    approved: bool,
    edited_body: str | None,
    reason: str | None,
) -> Response:
    repo = ReviewRepository(session)
    message = await repo.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail=f"message {message_id} not found")
    if not (message.draft and message.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN):
        raise _not_queued(message_id)

    workflow_id = await repo.resolve_workflow_id(message)
    if workflow_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"message {message_id} has no owning workflow (no sequence, no "
                "meta.temporal_workflow_id, no inbound.workflow_started audit event)"
            ),
        )

    # Normalise the edit: an unchanged or blank textarea is not an edit.
    body_edit = edited_body.strip() if edited_body is not None else None
    if not approved or not body_edit or body_edit == message.body.strip():
        body_edit = None

    decision = HumanDecision(
        approved=approved,
        edited_body=body_edit,
        step=message.sequence_step,
        message_id=str(message.id),
        actor_id=operator,
    )
    try:
        await temporal.signal(workflow_id, HUMAN_DECISION_SIGNAL, decision)
    except Exception as exc:
        log.error(
            "review.signal_failed",
            message_id=str(message.id),
            workflow_id=workflow_id,
            approved=approved,
            error=str(exc)[:300],
        )
        detail = f"could not signal workflow {workflow_id}: {exc}"
        if wants_html(request):
            return templates.TemplateResponse(
                request,
                "_review_result.html",
                {"ok": False, "message_id": str(message.id), "detail": detail},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        return JSONResponse({"detail": detail}, status_code=status.HTTP_502_BAD_GATEWAY)

    now = datetime.now(tz=UTC)
    await repo.apply_decision(
        message,
        approved=approved,
        edited_body=body_edit,
        operator=operator,
        reason=reason,
        decided_at=now,
    )
    event_type = (
        EVENT_REJECTED
        if not approved
        else (EVENT_APPROVED_WITH_EDITS if body_edit is not None else EVENT_APPROVED)
    )
    await AuditRepository(session).record(
        actor=Actor.HUMAN,
        actor_id=operator,
        event_type=event_type,
        entity_type="message",
        entity_id=message.id,
        payload={
            "action": message.policy_action,
            "approved": approved,
            "edited": body_edit is not None,
            "reason": reason,
            "workflow_id": workflow_id,
            "signal": HUMAN_DECISION_SIGNAL,
            "step": message.sequence_step,
        },
        correlation_id=workflow_id,
    )
    await session.commit()
    log.info(
        "review.decided",
        message_id=str(message.id),
        workflow_id=workflow_id,
        approved=approved,
        edited=body_edit is not None,
        operator=operator,
    )
    result = {
        "ok": True,
        "message_id": str(message.id),
        "workflow_id": workflow_id,
        "approved": approved,
        "edited": body_edit is not None,
        "policy_decision": message.policy_decision.value if message.policy_decision else None,
        "event_type": event_type,
    }
    if is_htmx(request):
        return templates.TemplateResponse(
            request,
            "_review_result.html",
            {**result, "detail": None, "reason": reason},
        )
    if wants_html(request):  # plain form post without JS: back to the queue
        return RedirectResponse("/review", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(result)


@router.post("/review/{message_id}/approve")
async def approve(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    message_id: uuid.UUID,
    edited_body: Annotated[str | None, Form()] = None,
) -> Response:
    return await _decide(
        request=request,
        session=session,
        temporal=temporal,
        templates=templates,
        operator=operator,
        message_id=message_id,
        approved=True,
        edited_body=edited_body,
        reason=None,
    )


@router.post("/review/{message_id}/reject")
async def reject(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    message_id: uuid.UUID,
    reason: Annotated[str | None, Form()] = None,
) -> Response:
    return await _decide(
        request=request,
        session=session,
        temporal=temporal,
        templates=templates,
        operator=operator,
        message_id=message_id,
        approved=False,
        edited_body=None,
        reason=(reason or "").strip() or None,
    )


__all__ = [
    "EVENT_APPROVED",
    "EVENT_APPROVED_WITH_EDITS",
    "EVENT_REJECTED",
    "HUMAN_DECISION_SIGNAL",
    "HumanDecision",
    "approve",
    "reject",
    "review_json",
    "review_page",
    "router",
]
