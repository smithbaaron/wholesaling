"""Investor outreach activities (REQUIREMENTS.md INV-10, INV-12, INV-16, INV-23, POL-03a/b,
WF-02, WF-21, XC-03).

Called only by ``workflows/investor_sequence.py``. One step of a sequence runs through:

    prepare_step      render the locked template into a ``messages`` draft row (no Graph call)
    decide_and_send   PolicyEngine -> send (Graph draft + send) | queue_for_human | blocked
    send_approved     a human approved a queued draft in the review queue -> Graph draft + send
    check_cancel_conditions   reply / opt-out / bounce / suppression / manual pause on the thread
    send_window       seconds until the market's business-hour window opens (INV-12)
    update_sequence   step counter, completion, cancellation on the ``sequences`` row

Idempotency: the draft row's ``external_message_id`` is ``seq:{sequence_id}:step:{n}`` (the
same key the workflow uses), so a retried ``prepare_step`` returns the same row, a retried
``decide_and_send`` on a sent row returns ``sent`` without touching Graph, and every audit event
is keyed on that value.

Threading: step 0 starts a new conversation; steps 1..3 are created with the thread's
``conversation_id`` so the gateway replies to the latest message in that conversation
(milestone-2 decision 3). ``create_reply_draft`` on the step-0 item is not used because a sent
item is not addressable that way in every gateway; the conversation id is, and it is what
Outlook threads on (INV-23).

Dependencies come from ``GraphActivityContext`` (gateway, session factory, settings, clock), the
same context the Graph activities use, so one ``set_graph_context`` wires both for tests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from wholesaling.activities.graph import (
    GraphActivityContext,
    SendDraftInput,
    get_graph_context,
    send_draft,
)
from wholesaling.agents.a3 import A3Outreach, OutreachDraft
from wholesaling.agents.templates.investor_outreach import SenderIdentity, footer
from wholesaling.db.models.enums import Actor, PolicyDecision, SequenceStatus
from wholesaling.db.models.investors import Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Sequence, Thread
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.messages import MessageRepository
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.graph import OutboundDraft
from wholesaling.logging import bind_correlation_id, clear_context, get_logger
from wholesaling.policy.actions import PolicyAction
from wholesaling.policy.engine import PolicyContext, PolicyEngine

log = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Chicago"
AGENT_ACTOR_ID = "A3"


def idempotency_key(sequence_id: str, step: int) -> str:
    """``seq:{sequence_id}:step:{n}`` - the outbound idempotency key for one step (XC-03)."""
    return f"seq:{sequence_id}:step:{step}"


def _bind() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _action_for_step(step: int) -> PolicyAction:
    return PolicyAction.INVESTOR_INITIAL_OUTREACH if step == 0 else PolicyAction.INVESTOR_FOLLOWUP


# --------------------------------------------------------------------------------------------
# Send window (INV-12)
# --------------------------------------------------------------------------------------------


def seconds_until_send_window(
    now: datetime,
    tz: str,
    *,
    start_hour: int,
    end_hour: int,
    weekdays_only: bool = True,
) -> float:
    """Seconds to wait until ``[start_hour, end_hour)`` local time on an allowed day; 0 if open.

    Pure function so the rule is unit-testable without a database.
    """
    if not 0 <= start_hour < end_hour <= 24:
        msg = f"invalid send window {start_hour}-{end_hour}"
        raise ValueError(msg)
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    local = (_aware(now) or now).astimezone(zone)
    day = local.date()
    for _ in range(8):  # at most a week of closed days plus today
        if not (weekdays_only and day.weekday() >= 5):
            opens = datetime.combine(day, time(hour=start_hour), tzinfo=zone)
            closes = (
                datetime.combine(day, time.min, tzinfo=zone) + timedelta(days=1)
                if end_hour == 24
                else datetime.combine(day, time(hour=end_hour), tzinfo=zone)
            )
            if local < opens:
                return max(0.0, (opens - local).total_seconds())
            if local < closes:
                return 0.0
        day = day + timedelta(days=1)
    msg = "no open send-window day found within a week"  # pragma: no cover - impossible
    raise RuntimeError(msg)


@dataclass(slots=True)
class SendWindowInput:
    market: str
    sequence_id: str | None = None


@dataclass(slots=True)
class SendWindowResult:
    seconds_until_open: float
    timezone: str
    start_hour: int
    end_hour: int


async def _market_timezone(session: AsyncSession, market: str) -> str:
    name = market.strip()
    stmt = select(Market.timezone).where(Market.name == name).limit(1)
    tz = (await session.execute(stmt)).scalar_one_or_none()
    return tz or DEFAULT_TIMEZONE


@activity.defn(name="outreach_send_window")
async def send_window(input: SendWindowInput) -> SendWindowResult:
    """How long the workflow must wait before the market's business-hour window is open."""
    _bind()
    ctx = get_graph_context()
    try:
        async with ctx.session_factory() as session:
            tz = await _market_timezone(session, input.market)
        s = ctx.settings
        secs = seconds_until_send_window(
            ctx.clock(),
            tz,
            start_hour=s.send_window_start_hour,
            end_hour=s.send_window_end_hour,
            weekdays_only=s.send_window_weekdays_only,
        )
        return SendWindowResult(
            seconds_until_open=secs,
            timezone=tz,
            start_hour=s.send_window_start_hour,
            end_hour=s.send_window_end_hour,
        )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# prepare_step
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class PrepareStepInput:
    sequence_id: str
    step: int
    contact_email: str
    market: str


@dataclass(slots=True)
class PrepareStepResult:
    message_id: str
    thread_id: str
    idempotency_key: str
    subject: str
    template_key: str
    approval_status: str
    policy_action: str
    created: bool
    already_sent: bool


async def _load_sequence(session: AsyncSession, sequence_id: str) -> Sequence:
    row = await SequenceRepository(session).get(uuid.UUID(sequence_id))
    if row is None:
        msg = f"sequence {sequence_id} not found"
        raise LookupError(msg)
    return row


def _a3(ctx: GraphActivityContext) -> A3Outreach:
    return A3Outreach(SenderIdentity.from_settings(ctx.settings))


@activity.defn(name="outreach_prepare_step")
async def prepare_step(input: PrepareStepInput) -> PrepareStepResult:
    """Render the step's template and write the draft ``messages`` row. No Graph call."""
    _bind()
    ctx = get_graph_context()
    try:
        key = idempotency_key(input.sequence_id, input.step)
        async with ctx.session_factory() as session:
            seq = await _load_sequence(session, input.sequence_id)
            if seq.investor_id is None:
                msg = f"sequence {seq.id} has no investor"
                raise ValueError(msg)
            messages = MessageRepository(session)
            existing = await messages.get_by_external_id(key)
            if existing is not None:
                return PrepareStepResult(
                    message_id=str(existing.id),
                    thread_id=str(existing.thread_id),
                    idempotency_key=key,
                    subject=existing.subject or "",
                    template_key=str(existing.meta.get("template_key", "")),
                    approval_status=str(existing.meta.get("template_approval_status", "")),
                    policy_action=existing.policy_action or "",
                    created=False,
                    already_sent=existing.sent_at is not None,
                )
            investor = await session.get(Investor, seq.investor_id)
            if investor is None:
                msg = f"investor {seq.investor_id} not found"
                raise LookupError(msg)
            draft: OutreachDraft = _a3(ctx).draft(
                step=input.step,
                investor_id=investor.id,
                contact_email=input.contact_email,
                market=input.market,
                first_name=investor.first_name,
            )
            thread: Thread | None = None
            if seq.thread_id is not None:
                thread = await messages.get_thread(seq.thread_id)
            if thread is None:
                thread = await messages.create_thread(
                    investor_id=investor.id,
                    counterpart_address=input.contact_email,
                    subject=draft.subject,
                )
                await SequenceRepository(session).set_thread(seq.id, thread.id)
            row, created = await messages.create_outbound_draft(
                thread=thread,
                to=draft.to,
                subject=draft.subject,
                body=draft.body,
                policy_action=draft.policy_action,
                idempotency_key=key,
                from_address=ctx.settings.graph_mailbox,
                sequence_id=seq.id,
                sequence_step=input.step,
                meta={
                    "template_key": draft.template_key,
                    "template_approval_status": draft.approval_status,
                    "template_source_row": draft.source_row,
                    "subject_variant": draft.subject_variant,
                    "thread_strategy": draft.thread_strategy,
                    "market": input.market,
                },
            )
            await AuditRepository(session).record(
                actor=Actor.AGENT,
                actor_id=AGENT_ACTOR_ID,
                event_type="outreach.step_prepared",
                entity_type="message",
                entity_id=row.id,
                payload={
                    "sequence_id": str(seq.id),
                    "step": input.step,
                    "template_key": draft.template_key,
                    "approval_status": draft.approval_status,
                    "subject_variant": draft.subject_variant,
                },
                correlation_id=str(seq.id),
                idempotency_key=f"out:{key}:prepared",
            )
            await session.commit()
            log.info(
                "outreach.step_prepared",
                sequence_id=str(seq.id),
                step=input.step,
                message_id=str(row.id),
                created=created,
            )
            return PrepareStepResult(
                message_id=str(row.id),
                thread_id=str(thread.id),
                idempotency_key=key,
                subject=draft.subject,
                template_key=draft.template_key,
                approval_status=draft.approval_status,
                policy_action=draft.policy_action.value,
                created=created,
                already_sent=False,
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# Graph draft creation shared by decide_and_send and send_approved
# --------------------------------------------------------------------------------------------


async def _ensure_graph_draft(
    ctx: GraphActivityContext, session: AsyncSession, row: Message
) -> str:
    """Create the mailbox draft for ``row`` once; return its Graph draft id.

    Step 0 starts a conversation. Later steps pass the thread's ``conversation_id`` so the
    gateway files them as replies in the same Outlook conversation (INV-23).
    """
    existing = row.meta.get("graph_draft_id")
    if existing:
        return str(existing)
    thread = await session.get(Thread, row.thread_id)
    conversation_id = thread.external_thread_id if thread is not None else None
    if (row.sequence_step or 0) > 0 and conversation_id is None:
        # The initial was never sent (or its conversation id is unknown): a follow-up cannot be
        # threaded. The policy should have queued it; refuse rather than start a new thread.
        msg = f"message {row.id} is a follow-up but its thread has no conversation id"
        raise RuntimeError(msg)
    info = await ctx.gateway.create_draft(
        OutboundDraft(
            to=list(row.to_addresses),
            subject=row.subject or "",
            body_text=row.body,
            body_html=row.body_html,
            conversation_id=conversation_id if (row.sequence_step or 0) > 0 else None,
        )
    )
    conv = info.conversation_id or conversation_id
    row.meta = {
        **row.meta,
        "graph_draft_id": info.draft_id,
        "internet_message_id": info.internet_message_id,
        "conversation_id": conv,
    }
    if thread is not None and thread.external_thread_id is None and conv:
        thread.external_thread_id = conv
    await session.flush()
    return info.draft_id


async def _send(ctx: GraphActivityContext, message_id: uuid.UUID) -> bool:
    """Send through the Graph ``send_draft`` activity function (idempotent on ``sent_at``)."""
    result = await send_draft(SendDraftInput(message_id=str(message_id)))
    return result.sent or result.already_sent


# --------------------------------------------------------------------------------------------
# decide_and_send
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DecideAndSendInput:
    message_id: str
    step: int
    timezone: str = DEFAULT_TIMEZONE


@dataclass(slots=True)
class DecideAndSendResult:
    message_id: str
    decision: str  # PolicyDecision value
    reason: str
    level: str
    action: str
    sent: bool
    sends_today: int | None = None
    qa_verdict: str | None = None


async def build_policy_context(
    session: AsyncSession, row: Message, *, tz: str, now: datetime
) -> PolicyContext:
    """Assemble the facts the policy engine needs for one outbound sequence message."""
    messages = MessageRepository(session)
    action = _action_for_step(row.sequence_step or 0)
    sends_today = await messages.count_sends_today(action, tz, now=now)
    to = row.to_addresses[0] if row.to_addresses else ""
    suppressed = await messages.is_suppressed(to) if to else True
    initial_was_approved: bool | None = None
    if (row.sequence_step or 0) > 0 and row.sequence_id is not None:
        initial = await messages.get_step_message(row.sequence_id, 0)
        # "Initial was approved" (POL-03b): the step-0 message went out, either QA-approved under
        # policy or approved by a human in the review queue. An unsent initial is not approved.
        initial_was_approved = initial is not None and initial.sent_at is not None
    review = await messages.latest_qa_verdict(row.id)
    return PolicyContext(
        qa_verdict=review.verdict if review is not None else None,
        qa_confidence=review.confidence if review is not None else None,
        qa_flags=list(review.flags) if review is not None else [],
        recipient_suppressed=suppressed,
        sends_today=sends_today,
        initial_was_approved=initial_was_approved,
        now=now,
    )


@activity.defn(name="outreach_decide_and_send")
async def decide_and_send(input: DecideAndSendInput) -> DecideAndSendResult:
    """Run the policy engine on a prepared draft and act on the decision.

    ``send``: create the Graph draft and send it. ``queue_for_human``: leave the draft row with
    ``policy_decision=queue_for_human`` for the review queue. ``blocked``: mark blocked (the
    workflow then cancels the sequence). Every outcome writes one audit event.
    """
    _bind()
    ctx = get_graph_context()
    try:
        message_id = uuid.UUID(input.message_id)
        async with ctx.session_factory() as session:
            messages = MessageRepository(session)
            row = await messages.get(message_id)
            if row is None:
                msg = f"message {input.message_id} not found"
                raise LookupError(msg)
            action = _action_for_step(input.step)
            if row.sent_at is not None:
                return DecideAndSendResult(
                    message_id=input.message_id,
                    decision=PolicyDecision.SEND.value,
                    reason="already sent",
                    level=str(row.meta.get("policy_level", "")),
                    action=action.value,
                    sent=True,
                )
            now = ctx.clock()
            pctx = await build_policy_context(session, row, tz=input.timezone, now=now)
            decision = await PolicyEngine(session).decide(action, pctx)
            row.policy_action = action.value
            row.policy_decision = decision.decision
            row.meta = {
                **row.meta,
                "policy_reason": decision.reason,
                "policy_level": decision.level.value,
                "policy_behaviour": decision.behaviour.value,
                "sends_today_at_decision": pctx.sends_today,
            }
            key = row.external_message_id or input.message_id
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="policy",
                event_type="outreach.policy_decided",
                entity_type="message",
                entity_id=row.id,
                payload={
                    "action": action.value,
                    "level": decision.level.value,
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "sends_today": pctx.sends_today,
                    "qa_verdict": pctx.qa_verdict.value if pctx.qa_verdict else None,
                    "recipient_suppressed": pctx.recipient_suppressed,
                    "initial_was_approved": pctx.initial_was_approved,
                },
                correlation_id=str(row.sequence_id) if row.sequence_id else None,
                idempotency_key=f"out:{key}:decided",
            )
            if decision.sends:
                await _ensure_graph_draft(ctx, session, row)
            await session.commit()

        sent = False
        if decision.sends:
            sent = await _send(ctx, message_id)
        log.info(
            "outreach.policy_decided",
            message_id=input.message_id,
            step=input.step,
            decision=decision.decision.value,
            reason=decision.reason,
            level=decision.level.value,
            sent=sent,
        )
        return DecideAndSendResult(
            message_id=input.message_id,
            decision=decision.decision.value,
            reason=decision.reason,
            level=decision.level.value,
            action=action.value,
            sent=sent,
            sends_today=pctx.sends_today,
            qa_verdict=pctx.qa_verdict.value if pctx.qa_verdict else None,
        )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# send_approved (human decision from the review queue)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SendApprovedInput:
    message_id: str
    step: int
    edited_body: str | None = None
    actor_id: str | None = None


@dataclass(slots=True)
class SendApprovedResult:
    message_id: str
    sent: bool
    body_edited: bool
    blocked_reason: str | None = None


@activity.defn(name="outreach_send_approved")
async def send_approved(input: SendApprovedInput) -> SendApprovedResult:
    """A human approved a queued draft: apply the edit (if any), create the Graph draft, send.

    Suppression is still honoured (POL-03l: opt-out suppression is automatic at every level),
    so an address suppressed between queueing and approval is not mailed.
    """
    _bind()
    ctx = get_graph_context()
    try:
        message_id = uuid.UUID(input.message_id)
        async with ctx.session_factory() as session:
            messages = MessageRepository(session)
            row = await messages.get(message_id)
            if row is None:
                msg = f"message {input.message_id} not found"
                raise LookupError(msg)
            if row.sent_at is not None:
                return SendApprovedResult(message_id=input.message_id, sent=True, body_edited=False)
            to = row.to_addresses[0] if row.to_addresses else ""
            if not to or await messages.is_suppressed(to):
                row.policy_decision = PolicyDecision.BLOCKED
                row.meta = {**row.meta, "policy_reason": "recipient is suppressed"}
                await session.commit()
                return SendApprovedResult(
                    message_id=input.message_id,
                    sent=False,
                    body_edited=False,
                    blocked_reason="recipient is suppressed",
                )
            edited = False
            if input.edited_body is not None and input.edited_body.strip():
                new_body = input.edited_body
                # CMP-01: an edit may not drop the identity / address / opt-out block.
                footer_text = footer(SenderIdentity.from_settings(ctx.settings))
                if footer_text not in new_body:
                    new_body = new_body.rstrip() + "\n\n" + footer_text
                if new_body != row.body:
                    row.meta = {**row.meta, "original_body": row.body}
                    row.body = new_body
                    edited = True
            row.policy_decision = PolicyDecision.SEND
            row.meta = {
                **row.meta,
                "human_approved": True,
                "approved_by": input.actor_id,
                "policy_reason": "approved by human in review queue",
            }
            key = row.external_message_id or input.message_id
            await AuditRepository(session).record(
                actor=Actor.HUMAN,
                actor_id=input.actor_id,
                event_type="outreach.human_approved",
                entity_type="message",
                entity_id=row.id,
                payload={"step": input.step, "body_edited": edited},
                correlation_id=str(row.sequence_id) if row.sequence_id else None,
                idempotency_key=f"out:{key}:approved",
            )
            await _ensure_graph_draft(ctx, session, row)
            await session.commit()
        sent = await _send(ctx, message_id)
        log.info(
            "outreach.human_approved_sent",
            message_id=input.message_id,
            step=input.step,
            edited=edited,
        )
        return SendApprovedResult(message_id=input.message_id, sent=sent, body_edited=edited)
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# check_cancel_conditions
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CheckCancelInput:
    sequence_id: str
    contact_email: str


@dataclass(slots=True)
class CheckCancelResult:
    cancel_reason: str | None
    paused: bool


@activity.defn(name="outreach_check_cancel_conditions")
async def check_cancel_conditions(input: CheckCancelInput) -> CheckCancelResult:
    """Reason to stop the sequence now, if any: ``reply`` / ``opt_out`` / ``bounce`` /
    ``suppressed`` / the row's own cancel reason; plus whether an operator paused it."""
    _bind()
    ctx = get_graph_context()
    try:
        async with ctx.session_factory() as session:
            seq = await _load_sequence(session, input.sequence_id)
            if seq.status is SequenceStatus.CANCELLED:
                return CheckCancelResult(
                    cancel_reason=seq.cancel_reason or "cancelled", paused=False
                )
            if seq.status is SequenceStatus.COMPLETED:
                return CheckCancelResult(cancel_reason="completed", paused=False)
            messages = MessageRepository(session)
            if await messages.is_suppressed(input.contact_email):
                return CheckCancelResult(cancel_reason="suppressed", paused=False)
            reason: str | None = None
            if seq.thread_id is not None:
                reason = await messages.thread_cancel_reason(seq.thread_id)
            return CheckCancelResult(
                cancel_reason=reason, paused=seq.status is SequenceStatus.PAUSED
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# update_sequence
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class UpdateSequenceInput:
    sequence_id: str
    step_done: int | None = None  # 0-based step just handled
    next_send_in_seconds: float | None = None
    cancel_reason: str | None = None
    completed: bool = False
    paused: bool | None = None


@dataclass(slots=True)
class UpdateSequenceResult:
    status: str
    step: int
    cancel_reason: str | None


@activity.defn(name="outreach_update_sequence")
async def update_sequence(input: UpdateSequenceInput) -> UpdateSequenceResult:
    """Persist workflow progress on the ``sequences`` row and audit cancellations."""
    _bind()
    ctx = get_graph_context()
    try:
        async with ctx.session_factory() as session:
            repo = SequenceRepository(session)
            seq_id = uuid.UUID(input.sequence_id)
            seq = await _load_sequence(session, input.sequence_id)
            if input.step_done is not None:
                next_at = (
                    ctx.clock() + timedelta(seconds=input.next_send_in_seconds)
                    if input.next_send_in_seconds is not None
                    else None
                )
                seq = await repo.mark_step(seq_id, input.step_done, next_send_at=next_at)
            if input.paused is not None:
                seq = await repo.set_paused(seq_id, input.paused)
            if input.cancel_reason:
                seq = await repo.cancel(seq_id, input.cancel_reason, at=ctx.clock())
                await AuditRepository(session).record(
                    actor=Actor.SYSTEM,
                    actor_id="investor_sequence",
                    event_type="sequence.cancelled",
                    entity_type="sequence",
                    entity_id=seq.id,
                    payload={"reason": input.cancel_reason, "step": seq.step},
                    correlation_id=str(seq.id),
                    idempotency_key=f"seq:{seq.id}:cancelled",
                )
            elif input.completed:
                seq = await repo.complete(seq_id)
                await AuditRepository(session).record(
                    actor=Actor.SYSTEM,
                    actor_id="investor_sequence",
                    event_type="sequence.completed",
                    entity_type="sequence",
                    entity_id=seq.id,
                    payload={"steps": seq.step},
                    correlation_id=str(seq.id),
                    idempotency_key=f"seq:{seq.id}:completed",
                )
            await session.commit()
            return UpdateSequenceResult(
                status=seq.status.value, step=seq.step, cancel_reason=seq.cancel_reason
            )
    finally:
        clear_context()


__all__ = [
    "AGENT_ACTOR_ID",
    "DEFAULT_TIMEZONE",
    "CheckCancelInput",
    "CheckCancelResult",
    "DecideAndSendInput",
    "DecideAndSendResult",
    "PrepareStepInput",
    "PrepareStepResult",
    "SendApprovedInput",
    "SendApprovedResult",
    "SendWindowInput",
    "SendWindowResult",
    "UpdateSequenceInput",
    "UpdateSequenceResult",
    "build_policy_context",
    "check_cancel_conditions",
    "decide_and_send",
    "idempotency_key",
    "prepare_step",
    "seconds_until_send_window",
    "send_approved",
    "send_window",
    "update_sequence",
]
