"""Seller-funnel activities: S1 list refresh + S2 skip trace + sequence start, the S3 sequence
steps, and the S4 inbound pipeline (REQUIREMENTS.md SEL-01..08, SEL-12, WF-03, WF-10, WF-21,
POL-03d/f, POL-03l, POL-06, POL-09, XC-01, XC-03; build step BO-10).

Called by ``workflows/seller_sequence.py`` (``SellerSequenceWorkflow``,
``SellerListRefreshWorkflow``) and ``workflows/seller_inbound.py`` (``SellerInboundWorkflow``).
The investor-side activities are reused where the row shapes are the same:

- ``outreach.check_cancel_conditions`` / ``send_window`` / ``update_sequence`` work on the
  ``sequences`` row and the thread, so the seller workflow calls them unchanged;
- ``outreach.qa_review_draft`` (A5 on every draft, INV-17), ``_ensure_graph_draft`` and
  ``_send`` are called from the seller step activities below with the seller policy action;
- ``graph.fetch_message`` / ``create_draft`` / ``send_draft`` are the only Graph calls.

What is seller-specific lives here: the S3 draft row (``seller_prepare_step``), the policy
action ``seller_outreach_email`` (``seller_decide_and_send`` / ``seller_send_approved``), the
compliance check that every seller message carries the TX disclosure + identity + address +
opt-out (SEL-05; refused, not patched, when a template-rendered draft lacks it), and the S4
pipeline (``seller_classify_inbound`` -> ``seller_intake`` -> ``seller_apply_human_decision``).

Contracts coded against (the A7 builder's side is defined concurrently):

- ``OfferWorkflow`` started by name with one ``OfferWorkflowInput(intake_id, seller_lead_id)``
  argument and workflow id ``offer:{intake_id}`` (``offer_workflow_id``); Temporal maps the
  JSON object onto their dataclass by field name, so this module never imports theirs.
- The seller sequence receives ``intake_complete(IntakeCompleteSignal(intake_id,
  seller_lead_id))`` (WF-03) besides the shared ``reply`` / ``opt_out`` / ``bounce`` payloads.

Idempotency: every activity looks for the rows a previous attempt left (``messages.meta["s4"]``,
``meta["s4_intake"]``, the reply row keyed ``seller-reply:<inbound id>``, audit idempotency
keys) and repeats no model call, Graph call or Temporal call.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.activities.conversation import (
    SUPPRESSION_REASON_OPT_OUT,
    _is_suppressed,
    _thread_rows,
    _to_thread_messages,
    get_conversation_context,
)
from wholesaling.activities.graph import (
    CreateDraftInput,
    SendDraftInput,
    create_draft,
    get_graph_context,
    send_draft,
)
from wholesaling.activities.outreach import (
    DecideAndSendInput,
    DecideAndSendResult,
    PrepareStepResult,
    SendApprovedInput,
    SendApprovedResult,
    _ensure_graph_draft,
    _send,
    idempotency_key,
    qa_review_draft,
)
from wholesaling.agents.a4 import ThreadMessage
from wholesaling.agents.a5 import A5QaAgent, QaDraft, QaInput, QaResult
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.s1 import S1Result, S1SellerListBuilder, owner_first_name
from wholesaling.agents.s2 import S2Result, S2SkipTrace
from wholesaling.agents.s3 import S3SellerOutreach, SellerOutreachDraft
from wholesaling.agents.s4 import (
    S4Classification,
    S4Extraction,
    S4IntakeAgent,
    S4ReplyDraft,
    SellerClassification,
    assemble_reply_body,
    choose_asks,
    photo_upload_link,
)
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.agents.templates.seller_outreach import (
    ensure_compliance_block,
    missing_compliance_blocks,
)
from wholesaling.config.settings import Settings
from wholesaling.config.tunables import get_tunables
from wholesaling.db.models.enums import (
    Actor,
    Channel,
    Direction,
    MarketTier,
    PolicyDecision,
    QaVerdict,
    SellerLeadStatus,
    SequenceKind,
)
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Sequence, Suppression, Thread
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.normalize import normalize_address, normalize_email
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.messages import MessageRepository
from wholesaling.db.repositories.qa_reviews import QaReviewRepository
from wholesaling.db.repositories.sellers import (
    SellerRepository,
    missing_required,
    unasked_optional,
)
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.data import DataProviders, get_data_providers
from wholesaling.integrations.temporal import TemporalGateway, get_temporal
from wholesaling.logging import bind_correlation_id, clear_context, get_logger
from wholesaling.policy.actions import PolicyAction
from wholesaling.policy.engine import PolicyContext, PolicyEngine
from wholesaling.signals import (
    SIGNAL_BOUNCE,
    SIGNAL_OPT_OUT,
    SIGNAL_REPLY,
    BounceSignal,
    OptOutSignal,
    ReplySignal,
)

log = get_logger(__name__)

Clock = Callable[[], datetime]

# -- names and ids the workflows and the A7 builder code against -------------------------------

SELLER_SEQUENCE_WORKFLOW_NAME = "SellerSequenceWorkflow"
SELLER_SEQUENCE_WORKFLOW_ID_PREFIX = "seller-sequence:"
SELLER_INBOUND_WORKFLOW_NAME = "SellerInboundWorkflow"
SELLER_INBOUND_WORKFLOW_ID_PREFIX = "seller-inbound:"
OFFER_WORKFLOW_NAME = "OfferWorkflow"
OFFER_WORKFLOW_ID_PREFIX = "offer:"
SIGNAL_INTAKE_COMPLETE = "intake_complete"

SELLER_OUTREACH_ACTION = PolicyAction.SELLER_OUTREACH_EMAIL
INTAKE_REPLY_ACTION = PolicyAction.SELLER_INTAKE_REPLY
INTAKE_REPLY_ACTION_VALUE = INTAKE_REPLY_ACTION.value

EVENT_SEQUENCE_STARTED = "sequence.started"
EVENT_LEAD_UNREACHABLE = "seller_lead.unreachable"
EVENT_INTAKE_COMPLETED = "intake.completed"
EVENT_OFFER_REQUESTED = "offer.requested"
EVENT_INTAKE_REPLY_DECIDED = "intake.reply_decided"
EVENT_PARCEL_VERIFIED = "seller_lead.parcel_verified"
START_FAILED_REASON = "start_failed"
SOURCE_EMAIL = "email"
SOURCE_WEB_FORM = "web_form"


def seller_sequence_workflow_id(sequence_id: str) -> str:
    return f"{SELLER_SEQUENCE_WORKFLOW_ID_PREFIX}{sequence_id}"


def seller_inbound_workflow_id(source: str, ref: str) -> str:
    """``seller-inbound:form:<submission id>`` / ``seller-inbound:email:<graph id>``."""
    return f"{SELLER_INBOUND_WORKFLOW_ID_PREFIX}{source}:{ref}"


def offer_workflow_id(intake_id: str) -> str:
    return f"{OFFER_WORKFLOW_ID_PREFIX}{intake_id}"


def seller_reply_idempotency_key(inbound_message_id: str) -> str:
    return f"seller-reply:{inbound_message_id}"


@dataclass(slots=True)
class IntakeCompleteSignal:
    """Payload of the ``intake_complete`` signal to ``SellerSequenceWorkflow`` (WF-03)."""

    intake_id: str
    seller_lead_id: str


@dataclass(slots=True)
class OfferWorkflowInput:
    """What ``OfferWorkflow`` (A7 builder) is started with. Same field names as theirs."""

    intake_id: str
    seller_lead_id: str


# --------------------------------------------------------------------------------------------
# Dependency context
# --------------------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class SellerContext:
    session_factory: async_sessionmaker[AsyncSession]
    providers: DataProviders
    temporal: TemporalGateway
    settings: Settings
    clock: Clock = _utcnow
    runner: AgentRunner | None = None  # None -> the conversation context's runner
    prompt_values: dict[str, Any] = field(default_factory=lambda: {"company": "our team"})


_context: SellerContext | None = None


def set_seller_context(ctx: SellerContext | None) -> None:
    global _context
    _context = ctx


def get_seller_context() -> SellerContext:
    """Built on first use from the Graph activity context plus the settings-driven providers
    and Temporal gateway."""
    global _context
    if _context is None:
        graph_ctx = get_graph_context()
        settings = graph_ctx.settings
        _context = SellerContext(
            session_factory=graph_ctx.session_factory,
            providers=get_data_providers(settings),
            temporal=get_temporal(settings),
            settings=settings,
            clock=graph_ctx.clock,
        )
    return _context


def _runner(ctx: SellerContext) -> AgentRunner:
    if ctx.runner is not None:
        return ctx.runner
    return get_conversation_context().runner


def _bind() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


def _correlation_id() -> str | None:
    return activity.info().workflow_id if activity.in_activity() else None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _sender(settings: Settings) -> SenderIdentity:
    return SenderIdentity.from_settings(settings)


def _property_address(lead: SellerLead) -> str:
    parts = [lead.property_address_line1, lead.property_city]
    tail = " ".join(p for p in (lead.property_state, lead.property_postal_code) if p)
    if tail:
        parts.append(tail)
    return ", ".join(p for p in parts if p)


async def _market_name(session: AsyncSession, lead: SellerLead) -> str:
    if lead.market_id is None:
        return lead.property_city or "your area"
    market = await session.get(Market, lead.market_id)
    return market.name if market is not None else (lead.property_city or "your area")


# --------------------------------------------------------------------------------------------
# S1 + S2 + sequence start (weekly refresh, WF-10)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SellerListRefreshInput:
    market_id: str | None = None  # None = every Active market
    start_sequences: bool = True
    limit: int | None = None  # max sequences started per market this run
    honor_send_window: bool = True


@dataclass(slots=True)
class MarketRefreshResult:
    market: str
    market_id: str
    s1: dict[str, Any]
    traced: int = 0
    reachable: int = 0
    unreachable: int = 0
    sequences_started: int = 0
    skipped: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class SellerListRefreshResult:
    markets: list[MarketRefreshResult] = field(default_factory=list)

    @property
    def sequences_started(self) -> int:
        return sum(m.sequences_started for m in self.markets)

    @property
    def created(self) -> int:
        return sum(int(m.s1.get("created", 0)) for m in self.markets)


async def _active_markets(session: AsyncSession, market_id: str | None) -> list[Market]:
    if market_id is not None:
        market = await session.get(Market, uuid.UUID(market_id))
        return [market] if market is not None else []
    stmt = select(Market).where(Market.tier == MarketTier.ACTIVE).order_by(Market.name)
    return list((await session.execute(stmt)).scalars().all())


async def start_seller_sequence(
    ctx: SellerContext,
    lead: SellerLead,
    *,
    contact_email: str,
    market: str,
    honor_send_window: bool = True,
) -> str:
    """Create the ``sequences`` row (committed) and start ``SellerSequenceWorkflow``. Returns
    the workflow id. Mirrors ``sequence_starter.start_investor_sequence``."""
    intervals = [int(d) for d in get_tunables().sequences.seller_followup_days]
    sequence_id = uuid.uuid4()
    workflow_id = seller_sequence_workflow_id(str(sequence_id))
    async with ctx.session_factory() as session:
        repo = SequenceRepository(session)
        row, _created = await repo.create(
            kind=SequenceKind.SELLER,
            seller_lead_id=lead.id,
            temporal_workflow_id=workflow_id,
            total_steps=len(intervals) + 1,
            sequence_id=sequence_id,
        )
        row.created_at = ctx.clock()
        fresh = await SellerRepository(session).require(lead.id)
        await SellerRepository(session).set_status(fresh, SellerLeadStatus.IN_SEQUENCE)
        await AuditRepository(session).record(
            actor=Actor.SYSTEM,
            actor_id="seller_sequence_starter",
            event_type=EVENT_SEQUENCE_STARTED,
            entity_type="sequence",
            entity_id=row.id,
            payload={
                "seller_lead_id": str(lead.id),
                "market": market,
                "email": contact_email,
                "workflow_id": workflow_id,
                "intervals_days": intervals,
                "signal_score": lead.signal_score,
            },
            correlation_id=workflow_id,
            idempotency_key=f"seq:{row.id}:started",
        )
        await session.commit()
    try:
        from wholesaling.workflows.seller_sequence import SellerSequenceInput

        await ctx.temporal.start_workflow(
            SELLER_SEQUENCE_WORKFLOW_NAME,
            [
                SellerSequenceInput(
                    sequence_id=str(sequence_id),
                    seller_lead_id=str(lead.id),
                    contact_email=contact_email,
                    market=market,
                    intervals_days=intervals,
                    honor_send_window=honor_send_window,
                )
            ],
            workflow_id=workflow_id,
        )
    except Exception as exc:
        log.error(
            "seller_sequence.start_failed", sequence_id=str(sequence_id), error=str(exc)[:300]
        )
        async with ctx.session_factory() as session:
            await SequenceRepository(session).cancel(
                sequence_id, START_FAILED_REASON, at=ctx.clock()
            )
            fresh = await SellerRepository(session).require(lead.id)
            await SellerRepository(session).set_status(fresh, SellerLeadStatus.QUEUED)
            await session.commit()
        raise
    return workflow_id


async def refresh_market(
    ctx: SellerContext, market: Market, input: SellerListRefreshInput
) -> MarketRefreshResult:
    """S1 (commit) -> S2 for queued leads without a trace (commit) -> start sequences."""
    async with ctx.session_factory() as session:
        fresh = await session.get(Market, market.id)
        assert fresh is not None
        s1: S1Result = await S1SellerListBuilder().run(
            session, ctx.providers, fresh, as_of=ctx.clock()
        )
        await session.commit()
    out = MarketRefreshResult(market=s1.market, market_id=str(market.id), s1=s1.to_json())
    if s1.refused:
        return out

    s2 = S2SkipTrace()
    candidates: list[tuple[uuid.UUID, str]] = []
    skipped: dict[str, int] = {}
    async with ctx.session_factory() as session:
        repo = SellerRepository(session)
        leads = await repo.list_by_status(market.id, SellerLeadStatus.QUEUED)
        messages = MessageRepository(session)
        for lead in leads:
            if "skip_trace" not in (lead.source_refs or {}):
                result: S2Result = await s2.run(session, ctx.providers, lead, now=ctx.clock())
                out.traced += 1
                if not result.reachable:
                    out.unreachable += 1
                    await AuditRepository(session).record(
                        actor=Actor.AGENT,
                        actor_id="S2",
                        event_type=EVENT_LEAD_UNREACHABLE,
                        entity_type="seller_lead",
                        entity_id=lead.id,
                        payload={"reasons": result.to_json()},
                        correlation_id=str(lead.id),
                        idempotency_key=f"s2:{lead.id}:unreachable",
                    )
                else:
                    out.reachable += 1
            email = SellerRepository.primary_email(lead)
            if email is None:
                skipped["no_email"] = skipped.get("no_email", 0) + 1
                continue
            if await messages.is_suppressed(email):
                skipped["suppressed"] = skipped.get("suppressed", 0) + 1
                continue
            if await repo.has_sequence_on_record(lead.id, ignore_reason=START_FAILED_REASON):
                skipped["has_sequence"] = skipped.get("has_sequence", 0) + 1
                continue
            candidates.append((lead.id, email))
        await session.commit()
    out.skipped = skipped
    if not input.start_sequences:
        return out
    for lead_id, email in candidates:
        if input.limit is not None and out.sequences_started >= input.limit:
            out.skipped["limit"] = out.skipped.get("limit", 0) + 1
            continue
        async with ctx.session_factory() as session:
            lead = await SellerRepository(session).require(lead_id)
            market_name = await _market_name(session, lead)
        await start_seller_sequence(
            ctx,
            lead,
            contact_email=email,
            market=market_name,
            honor_send_window=input.honor_send_window,
        )
        out.sequences_started += 1
    return out


@activity.defn(name="seller_list_refresh")
async def seller_list_refresh(input: SellerListRefreshInput) -> SellerListRefreshResult:
    """WF-10 weekly refresh: S1 per Active market, S2 on new leads, sequences for reachable ones.
    A Watch / Excluded market is refused by S1 and reported, never raised."""
    _bind()
    ctx = get_seller_context()
    try:
        async with ctx.session_factory() as session:
            markets = await _active_markets(session, input.market_id)
        result = SellerListRefreshResult()
        for market in markets:
            result.markets.append(await refresh_market(ctx, market, input))
        log.info(
            "seller_list_refresh.completed",
            markets=len(result.markets),
            created=result.created,
            sequences_started=result.sequences_started,
        )
        return result
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# S3 sequence steps
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SellerPrepareStepInput:
    sequence_id: str
    step: int
    contact_email: str
    market: str


async def _load_sequence(session: AsyncSession, sequence_id: str) -> Sequence:
    row = await SequenceRepository(session).get(uuid.UUID(sequence_id))
    if row is None:
        msg = f"sequence {sequence_id} not found"
        raise LookupError(msg)
    return row


@activity.defn(name="seller_prepare_step")
async def seller_prepare_step(input: SellerPrepareStepInput) -> PrepareStepResult:
    """Render the S3 template (with the SEL-05 block) into a ``messages`` draft row."""
    _bind()
    ctx = get_seller_context()
    try:
        key = idempotency_key(input.sequence_id, input.step)
        async with ctx.session_factory() as session:
            seq = await _load_sequence(session, input.sequence_id)
            if seq.seller_lead_id is None:
                msg = f"sequence {seq.id} has no seller lead"
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
            repo = SellerRepository(session)
            lead = await repo.require(seq.seller_lead_id)
            s3 = S3SellerOutreach(_sender(ctx.settings))
            draft: SellerOutreachDraft = s3.draft(
                step=input.step,
                seller_lead_id=lead.id,
                contact_email=input.contact_email,
                property_address=_property_address(lead),
                market=input.market,
                owner_first_name=owner_first_name(lead.owner_name or ""),
            )
            thread: Thread | None = None
            if seq.thread_id is not None:
                thread = await messages.get_thread(seq.thread_id)
            if thread is None:
                thread = await repo.create_thread(
                    lead=lead,
                    channel=Channel.EMAIL,
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
                    "template_legal_review": draft.legal_review,
                    "template_source_row": draft.source_row,
                    "subject_variant": draft.subject_variant,
                    "thread_strategy": draft.thread_strategy,
                    "market": input.market,
                    "seller_lead_id": str(lead.id),
                    "property_address": _property_address(lead),
                },
            )
            await AuditRepository(session).record(
                actor=Actor.AGENT,
                actor_id="S3",
                event_type="outreach.step_prepared",
                entity_type="message",
                entity_id=row.id,
                payload={
                    "sequence_id": str(seq.id),
                    "seller_lead_id": str(lead.id),
                    "step": input.step,
                    "template_key": draft.template_key,
                    "approval_status": draft.approval_status,
                    "legal_review": draft.legal_review,
                },
                correlation_id=str(seq.id),
                idempotency_key=f"out:{key}:prepared",
            )
            await session.commit()
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


async def _seller_policy_context(
    session: AsyncSession, row: Message, *, action: PolicyAction, tz: str, now: datetime
) -> PolicyContext:
    messages = MessageRepository(session)
    to = row.to_addresses[0] if row.to_addresses else ""
    review = await messages.latest_qa_verdict(row.id)
    return PolicyContext(
        qa_verdict=review.verdict if review is not None else None,
        qa_confidence=review.confidence if review is not None else None,
        qa_flags=list(review.flags) if review is not None else [],
        recipient_suppressed=await messages.is_suppressed(to) if to else True,
        sends_today=await messages.count_sends_today(action, tz, now=now),
        now=now,
    )


@activity.defn(name="seller_decide_and_send")
async def seller_decide_and_send(input: DecideAndSendInput) -> DecideAndSendResult:
    """A5 QA -> PolicyEngine(seller_outreach_email) -> send | queue_for_human | blocked.

    Refuses (raises) a draft that lacks any SEL-05 block: a template bug must stop the sequence,
    not be patched at send time.
    """
    _bind()
    ctx = get_seller_context()
    graph_ctx = get_graph_context()
    action = SELLER_OUTREACH_ACTION
    try:
        message_id = uuid.UUID(input.message_id)
        async with ctx.session_factory() as session:
            messages = MessageRepository(session)
            row = await messages.get(message_id)
            if row is None:
                msg = f"message {input.message_id} not found"
                raise LookupError(msg)
            if row.sent_at is not None:
                return DecideAndSendResult(
                    message_id=input.message_id,
                    decision=PolicyDecision.SEND.value,
                    reason="already sent",
                    level=str(row.meta.get("policy_level", "")),
                    action=action.value,
                    sent=True,
                )
            missing = missing_compliance_blocks(row.body, _sender(ctx.settings))
            if missing:
                msg = f"seller draft {row.id} lacks SEL-05 block(s) {missing}; refusing to send"
                raise RuntimeError(msg)
            now = ctx.clock()
            await qa_review_draft(graph_ctx, session, row, now=now)
            pctx = await _seller_policy_context(
                session, row, action=action, tz=input.timezone, now=now
            )
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
                },
                correlation_id=str(row.sequence_id) if row.sequence_id else None,
                idempotency_key=f"out:{key}:decided",
            )
            if decision.sends:
                await _ensure_graph_draft(graph_ctx, session, row)
            await session.commit()
        sent = False
        if decision.sends:
            sent = await _send(graph_ctx, message_id)
        log.info(
            "seller_outreach.policy_decided",
            message_id=input.message_id,
            step=input.step,
            decision=decision.decision.value,
            reason=decision.reason,
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


@activity.defn(name="seller_send_approved")
async def seller_send_approved(input: SendApprovedInput) -> SendApprovedResult:
    """A human approved a queued seller step: apply the edit (re-appending the SEL-05 block
    when the edit dropped it), create the Graph draft, send. Suppression still blocks."""
    _bind()
    ctx = get_seller_context()
    graph_ctx = get_graph_context()
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
                new_body = ensure_compliance_block(input.edited_body, _sender(ctx.settings))
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
            await _ensure_graph_draft(graph_ctx, session, row)
            await session.commit()
        sent = await _send(graph_ctx, message_id)
        return SendApprovedResult(message_id=input.message_id, sent=sent, body_edited=edited)
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# Inbound: shared helpers
# --------------------------------------------------------------------------------------------


async def _load_message(session: AsyncSession, message_id: str) -> Message:
    row = await session.get(Message, uuid.UUID(message_id))
    if row is None:
        msg = f"message {message_id} not found"
        raise ValueError(msg)
    return row


async def _resolve_lead(
    session: AsyncSession, thread: Thread, from_address: str | None
) -> SellerLead | None:
    repo = SellerRepository(session)
    if thread.seller_lead_id is not None:
        return await repo.get(thread.seller_lead_id)
    lead = await repo.find_by_email(from_address)
    if lead is not None:
        thread.seller_lead_id = lead.id
        await session.flush()
    return lead


async def _signal_sequence(
    ctx: SellerContext,
    session: AsyncSession,
    *,
    sequence: Sequence | None,
    signal: str,
    payload: Any,
    entity_id: uuid.UUID,
    idempotency_key: str,
    actor_id: str = "S4",
) -> str | None:
    if sequence is None:
        return None
    audit = AuditRepository(session)
    if await audit.get_by_idempotency_key(idempotency_key) is not None:
        return sequence.temporal_workflow_id
    await ctx.temporal.signal(sequence.temporal_workflow_id, signal, payload)
    await audit.record(
        actor=Actor.AGENT,
        actor_id=actor_id,
        event_type="sequence.signalled",
        entity_type="sequence",
        entity_id=sequence.id,
        payload={
            "signal": signal,
            "workflow_id": sequence.temporal_workflow_id,
            "entity_id": str(entity_id),
        },
        correlation_id=_correlation_id(),
        idempotency_key=idempotency_key,
    )
    return sequence.temporal_workflow_id


async def _suppress_email(
    session: AsyncSession, audit: AuditRepository, message: Message, address: str | None
) -> bool:
    if not address:
        return False
    try:
        normalized = normalize_email(address)
    except ValueError:
        return False
    created = False
    if not await _is_suppressed(session, normalized):
        session.add(
            Suppression(
                channel=Channel.EMAIL,
                normalized_value=normalized,
                reason=SUPPRESSION_REASON_OPT_OUT,
                source_message_id=message.id,
            )
        )
        try:
            async with session.begin_nested():
                await session.flush()
            created = True
        except IntegrityError:
            created = False
    await audit.record(
        actor=Actor.AGENT,
        actor_id="S4",
        event_type="suppression.added",
        entity_type="suppression",
        payload={
            "address": normalized,
            "reason": SUPPRESSION_REASON_OPT_OUT,
            "policy_action": PolicyAction.OPT_OUT_SUPPRESSION.value,
            "created": created,
        },
        correlation_id=str(message.id),
        idempotency_key=f"s4:{message.id}:opt_out",
    )
    return created


def _s4(ctx: SellerContext, session: AsyncSession, correlation: str) -> S4IntakeAgent:
    return S4IntakeAgent(
        _runner(ctx), session, correlation_id=correlation, prompt_values=ctx.prompt_values
    )


# --------------------------------------------------------------------------------------------
# seller_classify_inbound
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SellerClassifyInput:
    message_id: str


@dataclass(slots=True)
class SellerClassifyResult:
    message_id: str
    source: str = SOURCE_EMAIL
    classification: str | None = None  # SellerClassification value
    confidence: float | None = None
    is_auto_reply: bool = False
    asks_offer: bool = False
    quoted_text_stripped: bool = False
    already_classified: bool = False
    skipped_reason: str | None = None  # outbound | bounce | not_seller
    suppression_created: bool = False
    signalled: str | None = None
    sequence_workflow_id: str | None = None
    seller_lead_id: str | None = None

    @property
    def warrants_intake(self) -> bool:
        return (
            not self.is_auto_reply
            and self.classification == SellerClassification.REPLY.value
            and self.skipped_reason is None
        )


def _stored_classification(message: Message) -> dict[str, Any] | None:
    stored = (message.meta or {}).get("s4")
    return stored if isinstance(stored, dict) and stored.get("classification") else None


@activity.defn(name="seller_classify_inbound")
async def seller_classify_inbound(input: SellerClassifyInput) -> SellerClassifyResult:
    """S4 classification of one inbound seller message; opt-out -> suppression (POL-03l) and
    signals to the seller sequence (``reply`` / ``opt_out``). A web-form submission is a
    ``reply`` by construction (no model call)."""
    _bind()
    ctx = get_seller_context()
    try:
        async with ctx.session_factory() as session:
            message = await _load_message(session, input.message_id)
            source = SOURCE_WEB_FORM if message.channel is Channel.WEB_FORM else SOURCE_EMAIL
            if message.direction != Direction.IN:
                return SellerClassifyResult(input.message_id, source, skipped_reason="outbound")
            if (message.meta or {}).get("is_bounce"):
                return SellerClassifyResult(input.message_id, source, skipped_reason="bounce")
            thread = await session.get(Thread, message.thread_id)
            if thread is None:  # pragma: no cover - FK guarantees it
                msg = f"thread {message.thread_id} missing"
                raise ValueError(msg)
            lead = await _resolve_lead(session, thread, message.from_address)
            if lead is None:
                await session.commit()
                return SellerClassifyResult(input.message_id, source, skipped_reason="not_seller")

            stored = _stored_classification(message)
            if stored is not None:
                await session.commit()
                return SellerClassifyResult(
                    message_id=input.message_id,
                    source=source,
                    classification=str(stored["classification"]),
                    confidence=stored.get("confidence"),
                    is_auto_reply=bool(stored.get("is_auto_reply")),
                    asks_offer=bool(stored.get("asks_offer")),
                    quoted_text_stripped=bool(stored.get("quoted_text_stripped")),
                    already_classified=True,
                    signalled=stored.get("signalled"),
                    sequence_workflow_id=stored.get("sequence_workflow_id"),
                    seller_lead_id=str(lead.id),
                )

            correlation = _correlation_id() or str(message.id)
            if source == SOURCE_WEB_FORM:
                result = S4Classification(
                    classification=SellerClassification.REPLY,
                    confidence=1.0,
                    rationale="web form submission",
                    is_auto_reply=False,
                    quoted_text_stripped=False,
                )
            else:
                rows = await _thread_rows(session, thread.id)
                result = await _s4(ctx, session, correlation).classify(
                    _to_thread_messages(rows, message)
                )

            message.classification = result.classification.message_classification
            message.classification_confidence = result.confidence
            audit = AuditRepository(session)
            await audit.record(
                actor=Actor.AGENT,
                actor_id="S4",
                event_type="message.classified",
                entity_type="message",
                entity_id=message.id,
                payload={**result.to_json(), "seller_lead_id": str(lead.id)},
                correlation_id=_correlation_id(),
                idempotency_key=f"s4:{message.id}:classified",
            )
            repo = SellerRepository(session)
            suppression_created = False
            signalled: str | None = None
            workflow_id: str | None = None
            sequence = await repo.active_sequence(lead.id)
            if result.classification is SellerClassification.OPT_OUT and not result.is_auto_reply:
                suppression_created = await _suppress_email(
                    session, audit, message, message.from_address
                )
                await repo.set_status(lead, SellerLeadStatus.SUPPRESSED)
                signalled = SIGNAL_OPT_OUT
                workflow_id = await _signal_sequence(
                    ctx,
                    session,
                    sequence=sequence,
                    signal=SIGNAL_OPT_OUT,
                    payload=OptOutSignal(message_id=str(message.id)),
                    entity_id=message.id,
                    idempotency_key=f"s4:{message.id}:signal:{SIGNAL_OPT_OUT}",
                )
            elif result.is_substantive:
                if result.classification is SellerClassification.NOT_INTERESTED:
                    await repo.set_status(lead, SellerLeadStatus.DEAD)
                elif lead.status in {SellerLeadStatus.QUEUED, SellerLeadStatus.IN_SEQUENCE}:
                    await repo.set_status(lead, SellerLeadStatus.REPLIED)
                signalled = SIGNAL_REPLY
                workflow_id = await _signal_sequence(
                    ctx,
                    session,
                    sequence=sequence,
                    signal=SIGNAL_REPLY,
                    payload=ReplySignal(
                        message_id=str(message.id),
                        classification=result.classification.message_classification.value,
                    ),
                    entity_id=message.id,
                    idempotency_key=f"s4:{message.id}:signal:{SIGNAL_REPLY}",
                )
            if workflow_id is None:
                signalled = None
            meta = {
                **(message.meta or {}),
                "s4": {
                    **result.to_json(),
                    "signalled": signalled,
                    "sequence_workflow_id": workflow_id,
                    "seller_lead_id": str(lead.id),
                },
            }
            if result.is_auto_reply:
                # ``MessageRepository.thread_cancel_reason`` reads this key (INV-22): an
                # automatic reply must never cancel the seller sequence either.
                meta["a4"] = {**(meta.get("a4") or {}), "is_auto_reply": True}
            message.meta = meta
            await session.commit()
            log.info(
                "seller_inbound.classified",
                message_id=str(message.id),
                classification=result.classification.value,
                is_auto_reply=result.is_auto_reply,
                signalled=signalled,
                seller_lead_id=str(lead.id),
            )
            return SellerClassifyResult(
                message_id=input.message_id,
                source=source,
                classification=result.classification.value,
                confidence=result.confidence,
                is_auto_reply=result.is_auto_reply,
                asks_offer=result.asks_offer,
                quoted_text_stripped=result.quoted_text_stripped,
                suppression_created=suppression_created,
                signalled=signalled,
                sequence_workflow_id=workflow_id,
                seller_lead_id=str(lead.id),
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# seller_intake (S4 extract -> merge -> complete | reply)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SellerIntakeInput:
    message_id: str
    workflow_id: str | None = None


@dataclass(slots=True)
class SellerIntakeResult:
    message_id: str
    seller_lead_id: str | None = None
    intake_id: str | None = None
    source: str = SOURCE_EMAIL
    extracted: dict[str, Any] = field(default_factory=dict)
    changed_fields: list[str] = field(default_factory=list)
    parcel_verified: bool = False
    missing_required: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    intake_complete: bool = False
    completed_now: bool = False
    offer_workflow_id: str | None = None
    signalled_intake_complete: bool = False
    photo_link_sent: bool = False
    draft_message_id: str | None = None
    subject: str | None = None
    body: str | None = None
    contains_sensitive: bool = False
    qa_verdict: str | None = None
    qa_confidence: float | None = None
    qa_flags: list[str] = field(default_factory=list)
    policy_decision: str | None = None
    policy_reason: str | None = None
    policy_level: str | None = None
    sent: bool = False
    already_processed: bool = False
    skipped_reason: str | None = None  # not_seller | no_email

    @property
    def queued(self) -> bool:
        return self.policy_decision == PolicyDecision.QUEUE_FOR_HUMAN.value

    @property
    def blocked(self) -> bool:
        return self.policy_decision == PolicyDecision.BLOCKED.value


def _known(lead: SellerLead, intake: Intake) -> dict[str, Any]:
    known: dict[str, Any] = {
        "property_address": _property_address(lead),
        "parcel_verified": lead.parcel_verified,
        "motivation": intake.motivation,
        "condition_class": intake.condition_class.value if intake.condition_class else None,
        "disclosed_issues": list(intake.disclosed_issues or []),
        "timeline": intake.timeline.value if intake.timeline else None,
        "asking_price": None if intake.asking_price is None else str(intake.asking_price),
        "photos_on_record": len(intake.photos or []),
    }
    for name in ("co_owners", "occupancy", "liens", "hoa"):
        value = getattr(intake, name)
        known[name] = (
            value if value else ("unknown" if getattr(intake, f"{name}_unknown") else None)
        )
    return {k: v for k, v in known.items() if v not in (None, [], "")}


async def _verify_parcel(
    ctx: SellerContext, session: AsyncSession, lead: SellerLead, address: str | None
) -> bool:
    """Resolve ``address`` (or the lead's own) to a county parcel and stamp the lead."""
    candidate = (address or "").strip() or _property_address(lead)
    if not candidate:
        return lead.parcel_verified
    if lead.parcel_verified and (not address or address.strip() == _property_address(lead)):
        return True
    parcel = await ctx.providers.deeds.resolve_parcel(candidate)
    if parcel is None:
        # An S1 lead stays verified when the seller merely restates the address loosely; a lead
        # that was never verified (web form, typo) is asked to confirm it.
        return lead.parcel_verified
    changed = lead.parcel_id != parcel.parcel_id or not lead.parcel_verified
    lead.parcel_id = parcel.parcel_id
    lead.parcel_verified = True
    lead.property_address_line1 = parcel.address
    lead.property_city = parcel.city
    lead.property_state = parcel.state
    lead.property_postal_code = parcel.zip
    lead.address_key = normalize_address(parcel.address, parcel.city, parcel.state, parcel.zip)
    if changed:
        await AuditRepository(session).record(
            actor=Actor.AGENT,
            actor_id="S4",
            event_type=EVENT_PARCEL_VERIFIED,
            entity_type="seller_lead",
            entity_id=lead.id,
            payload={"parcel_id": parcel.parcel_id, "address": candidate, "source": parcel.source},
            correlation_id=_correlation_id(),
            idempotency_key=f"s4:{lead.id}:parcel:{parcel.parcel_id}",
        )
    await session.flush()
    return True


async def _find_reply_row(session: AsyncSession, inbound_message_id: str) -> Message | None:
    stmt = select(Message).where(
        Message.channel == Channel.EMAIL,
        Message.external_message_id == seller_reply_idempotency_key(inbound_message_id),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _result_from_stored(inbound: Message, draft_row: Message | None) -> SellerIntakeResult:
    stored = dict((inbound.meta or {}).get("s4_intake") or {})
    result = SellerIntakeResult(
        message_id=str(inbound.id),
        seller_lead_id=stored.get("seller_lead_id"),
        intake_id=stored.get("intake_id"),
        source=str(stored.get("source", SOURCE_EMAIL)),
        extracted=dict(stored.get("extracted") or {}),
        changed_fields=list(stored.get("changed_fields") or []),
        parcel_verified=bool(stored.get("parcel_verified")),
        missing_required=list(stored.get("missing_required") or []),
        asked=list(stored.get("asked") or []),
        intake_complete=bool(stored.get("intake_complete")),
        completed_now=False,
        offer_workflow_id=stored.get("offer_workflow_id"),
        signalled_intake_complete=bool(stored.get("signalled_intake_complete")),
        photo_link_sent=bool(stored.get("photo_link_sent")),
        skipped_reason=stored.get("skipped_reason"),
        already_processed=True,
    )
    if draft_row is not None:
        qa = dict(stored.get("qa") or {})
        result.draft_message_id = str(draft_row.id)
        result.subject = draft_row.subject
        result.body = draft_row.body
        result.contains_sensitive = bool((draft_row.meta or {}).get("contains_sensitive"))
        result.qa_verdict = qa.get("verdict")
        result.qa_confidence = qa.get("confidence")
        result.qa_flags = list(qa.get("flags") or [])
        result.policy_decision = (
            draft_row.policy_decision.value if draft_row.policy_decision else None
        )
        result.policy_reason = stored.get("policy_reason")
        result.policy_level = stored.get("policy_level")
        result.sent = draft_row.sent_at is not None
    return result


async def _complete_intake(
    ctx: SellerContext,
    session: AsyncSession,
    *,
    lead: SellerLead,
    intake: Intake,
    message: Message,
    result: SellerIntakeResult,
) -> None:
    """SEL-07: mark complete, audit, start ``OfferWorkflow`` (``offer.requested``) and tell the
    sequence. Each step is keyed so a retry repeats none of them."""
    now = ctx.clock()
    repo = SellerRepository(session)
    audit = AuditRepository(session)
    result.completed_now = await repo.complete(intake, at=now)
    result.intake_complete = True
    await audit.record(
        actor=Actor.AGENT,
        actor_id="S4",
        event_type=EVENT_INTAKE_COMPLETED,
        entity_type="intake",
        entity_id=intake.id,
        payload={
            "seller_lead_id": str(lead.id),
            "message_id": str(message.id),
            "photos": len(intake.photos or []),
            "photos_missing": not intake.photos,
        },
        correlation_id=_correlation_id(),
        idempotency_key=f"intake:{intake.id}:completed",
    )
    offer_id = offer_workflow_id(str(intake.id))
    offer_key = f"intake:{intake.id}:offer_requested"
    if await audit.get_by_idempotency_key(offer_key) is None:
        await ctx.temporal.start_workflow(
            OFFER_WORKFLOW_NAME,
            [OfferWorkflowInput(intake_id=str(intake.id), seller_lead_id=str(lead.id))],
            workflow_id=offer_id,
        )
        await audit.record(
            actor=Actor.AGENT,
            actor_id="S4",
            event_type=EVENT_OFFER_REQUESTED,
            entity_type="intake",
            entity_id=intake.id,
            payload={
                "seller_lead_id": str(lead.id),
                "workflow": OFFER_WORKFLOW_NAME,
                "workflow_id": offer_id,
            },
            correlation_id=_correlation_id(),
            idempotency_key=offer_key,
        )
    result.offer_workflow_id = offer_id
    sequence = await repo.active_sequence(lead.id)
    signalled = await _signal_sequence(
        ctx,
        session,
        sequence=sequence,
        signal=SIGNAL_INTAKE_COMPLETE,
        payload=IntakeCompleteSignal(intake_id=str(intake.id), seller_lead_id=str(lead.id)),
        entity_id=intake.id,
        idempotency_key=f"intake:{intake.id}:signal:{SIGNAL_INTAKE_COMPLETE}",
    )
    result.signalled_intake_complete = signalled is not None


async def _sends_today(session: AsyncSession, now: datetime) -> int:
    return await MessageRepository(session).count_sends_today(INTAKE_REPLY_ACTION, "UTC", now=now)


def _thread_messages_for_s4(rows: list[Message], latest: Message) -> list[ThreadMessage]:
    return _to_thread_messages(rows, latest)


@activity.defn(name="seller_intake")
async def seller_intake(input: SellerIntakeInput) -> SellerIntakeResult:
    """S4 extract -> merge into the intake -> parcel check -> complete (A7) or ask for at most
    two missing fields through A5 QA and ``PolicyEngine(seller_intake_reply)`` (XC-01)."""
    _bind()
    ctx = get_seller_context()
    try:
        async with ctx.session_factory() as session:
            inbound = await _load_message(session, input.message_id)
            if (inbound.meta or {}).get("s4_intake") is not None:
                return _result_from_stored(
                    inbound, await _find_reply_row(session, input.message_id)
                )
            if inbound.direction != Direction.IN:
                msg = f"message {input.message_id} is not inbound"
                raise ValueError(msg)
            thread = await session.get(Thread, inbound.thread_id)
            if thread is None:  # pragma: no cover
                msg = f"thread {inbound.thread_id} missing"
                raise ValueError(msg)
            source = SOURCE_WEB_FORM if inbound.channel is Channel.WEB_FORM else SOURCE_EMAIL
            lead = await _resolve_lead(session, thread, inbound.from_address)
            result = SellerIntakeResult(message_id=input.message_id, source=source)
            if lead is None:
                result.skipped_reason = "not_seller"
                inbound.meta = {
                    **(inbound.meta or {}),
                    "s4_intake": {"skipped_reason": "not_seller"},
                }
                await session.commit()
                return result
            repo = SellerRepository(session)
            intake = await repo.get_or_create_intake(lead)
            result.seller_lead_id = str(lead.id)
            result.intake_id = str(intake.id)
            correlation = _correlation_id() or str(inbound.id)
            agent = _s4(ctx, session, correlation)
            rows = await _thread_rows(session, thread.id)
            thread_messages = _thread_messages_for_s4(rows, inbound)

            if source == SOURCE_WEB_FORM:
                extraction: S4Extraction = agent.parse_form((inbound.meta or {}).get("form") or {})
            else:
                extraction = await agent.extract(thread_messages, _known(lead, intake))
            fields = extraction.fields
            result.extracted = extraction.to_json()

            result.parcel_verified = await _verify_parcel(
                ctx, session, lead, fields.property_address
            )
            result.changed_fields = await repo.merge_intake(intake, fields)
            if lead.status in {
                SellerLeadStatus.QUEUED,
                SellerLeadStatus.IN_SEQUENCE,
                SellerLeadStatus.REPLIED,
            }:
                await repo.set_status(lead, SellerLeadStatus.INTAKE)
            missing = missing_required(lead, intake)
            await repo.set_missing(intake, missing)
            result.missing_required = list(missing)

            if not missing:
                await _complete_intake(
                    ctx, session, lead=lead, intake=intake, message=inbound, result=result
                )
                inbound.meta = {**(inbound.meta or {}), "s4_intake": _stored_payload(result)}
                await session.commit()
                log.info(
                    "seller_intake.completed",
                    seller_lead_id=str(lead.id),
                    intake_id=str(intake.id),
                    offer_workflow_id=result.offer_workflow_id,
                )
                return result

            # -- incomplete: ask for at most two fields -----------------------------------------
            asks = choose_asks(missing, unasked_optional(intake))
            result.asked = asks
            # Reply to the address the seller wrote from (same thread); the traced primary
            # email is the fallback for a lead whose message carried no sender.
            recipient = inbound.from_address or SellerRepository.primary_email(lead)
            if not recipient:
                result.skipped_reason = "no_email"
                await AuditRepository(session).record(
                    actor=Actor.AGENT,
                    actor_id="S4",
                    event_type=EVENT_LEAD_UNREACHABLE,
                    entity_type="seller_lead",
                    entity_id=lead.id,
                    payload={"missing_required": missing, "reason": "no email to ask"},
                    correlation_id=_correlation_id(),
                    idempotency_key=f"s4:{inbound.id}:unreachable",
                )
                inbound.meta = {**(inbound.meta or {}), "s4_intake": _stored_payload(result)}
                await session.commit()
                return result

            photo_link = (
                None if intake.photos else photo_upload_link(ctx.settings.public_base_url, lead.id)
            )
            stored_class = (inbound.meta or {}).get("s4") or {}
            asks_offer = bool(stored_class.get("asks_offer"))
            facts: dict[str, Any] = {
                "our_mailbox": ctx.settings.graph_mailbox,
                "seller_name": lead.owner_name,
                "first_name": owner_first_name(lead.owner_name or ""),
                "property_address": _property_address(lead),
                "market": await _market_name(session, lead),
            }
            draft: S4ReplyDraft = await agent.draft_reply(
                thread_messages,
                known=_known(lead, intake),
                asks=asks,
                photo_link=photo_link,
                asks_offer=asks_offer,
                facts=facts,
            )
            sender = _sender(ctx.settings)
            body_text = assemble_reply_body(
                draft.body, sender=sender, photo_link=photo_link, first_name=facts["first_name"]
            )
            now = ctx.clock()
            suppressed = await _is_suppressed(session, recipient)
            sent_out = [r for r in rows if r.direction == Direction.OUT and r.sent_at is not None]
            last_out = sent_out[-1] if sent_out else None
            a5 = A5QaAgent(
                _runner(ctx), session, correlation_id=correlation, prompt_values=ctx.prompt_values
            )
            qa: QaResult = await a5.review(
                QaInput(
                    draft=QaDraft(
                        to=[recipient],
                        subject=draft.subject,
                        body=body_text,
                        asks_for=draft.asks_for,
                        contains_sensitive=draft.contains_sensitive,
                    ),
                    thread=thread_messages,
                    facts=facts,
                    recipient_suppressed=suppressed,
                    thread_participant=thread.counterpart_address or recipient,
                    last_outbound_asks_for=list((last_out.meta or {}).get("asks_for") or [])
                    if last_out
                    else (),
                    last_outbound_at=_aware(last_out.sent_at) if last_out else None,
                    now=now,
                )
            )
            decision = await PolicyEngine(session).decide(
                INTAKE_REPLY_ACTION,
                PolicyContext(
                    qa_verdict=qa.verdict,
                    qa_confidence=Decimal(str(qa.confidence)),
                    qa_flags=list(qa.flags),
                    recipient_suppressed=suppressed,
                    sends_today=await _sends_today(session, now),
                    sensitive_content=draft.contains_sensitive,
                    now=now,
                ),
            )
            policy_decision = decision.decision
            policy_reason = decision.reason
            if policy_decision is PolicyDecision.SEND and draft.contains_sensitive:
                # POL-06 / POL-09 hard floor: price, contract and legal content stay human at
                # every level, whatever the seller_intake_reply row says.
                policy_decision = PolicyDecision.QUEUE_FOR_HUMAN
                policy_reason = "hard floor: price/contract/legal content needs a human (POL-09)"
            final_body = body_text
            if qa.verdict is QaVerdict.REWRITE and qa.rewrite_body:
                final_body = ensure_compliance_block(qa.rewrite_body, sender)
            graph_id = (inbound.meta or {}).get("graph_id")
            conversation_id = (inbound.meta or {}).get(
                "conversation_id"
            ) or thread.external_thread_id
            draft_meta: dict[str, Any] = {
                "asks_for": list(draft.asks_for),
                "contains_sensitive": draft.contains_sensitive,
                "reply_to_message_id": str(inbound.id),
                "seller_lead_id": str(lead.id),
                "intake_id": str(intake.id),
                "s4_body": draft.body,
                "photo_link": photo_link,
            }
            await repo.note_asked(intake, [a for a in asks if a not in missing])
            if (
                photo_link
                and intake.photo_link_sent_at is None
                and policy_decision is not PolicyDecision.BLOCKED
            ):
                intake.photo_link_sent_at = now
                result.photo_link_sent = True
            result.contains_sensitive = draft.contains_sensitive
            result.subject = draft.subject
            result.body = final_body
            result.qa_verdict = qa.verdict.value
            result.qa_confidence = qa.confidence
            result.qa_flags = list(qa.flags)
            result.policy_decision = policy_decision.value
            result.policy_reason = policy_reason
            result.policy_level = decision.level.value
            await session.commit()  # agent runs + intake state are now durable

        # -- phase 2: the draft row (Graph draft unless blocked) ---------------------------------
        if policy_decision is PolicyDecision.BLOCKED:
            async with ctx.session_factory() as session:
                draft_row_id = await _record_blocked_reply(
                    session,
                    inbound_id=inbound.id,
                    thread_id=thread.id,
                    to=[recipient],
                    subject=draft.subject,
                    body=final_body,
                    mailbox=ctx.settings.graph_mailbox,
                    agent_run_id=draft.run_id,
                    meta={**draft_meta, "blocked": True, "conversation_id": conversation_id},
                )
                await session.commit()
        else:
            created = await create_draft(
                CreateDraftInput(
                    idempotency_key=seller_reply_idempotency_key(str(inbound.id)),
                    to=[recipient],
                    subject=draft.subject,
                    body_text=final_body,
                    reply_to_graph_message_id=str(graph_id) if graph_id else None,
                    conversation_id=str(conversation_id) if conversation_id else None,
                    thread_id=str(thread.id),
                    agent_run_id=str(draft.run_id),
                )
            )
            draft_row_id = uuid.UUID(created.message_id)

        # -- phase 3: QA review + policy stamped on the draft row -------------------------------
        async with ctx.session_factory() as session:
            draft_row = await session.get(Message, draft_row_id)
            if draft_row is None:  # pragma: no cover - just created
                msg = f"draft row {draft_row_id} vanished"
                raise RuntimeError(msg)
            review = await QaReviewRepository(session).record(
                message_id=draft_row.id,
                verdict=qa.verdict,
                confidence=qa.confidence,
                flags=qa.flags,
                rationale=qa.rationale,
                rewrite_body=qa.rewrite_body,
                model=qa.model,
                agent_run_id=qa.run_id,
            )
            draft_row.policy_action = INTAKE_REPLY_ACTION_VALUE
            draft_row.policy_decision = policy_decision
            workflow_id = _correlation_id() or input.workflow_id
            draft_row.meta = {
                **(draft_row.meta or {}),
                **draft_meta,
                "graph_draft_body": final_body,
                "policy_reason": policy_reason,
                "policy_level": decision.level.value,
                **({"temporal_workflow_id": workflow_id} if workflow_id else {}),
            }
            inbound_row = await _load_message(session, input.message_id)
            result.draft_message_id = str(draft_row.id)
            payload = _stored_payload(result)
            payload["qa"] = qa.to_json()
            payload["qa_review_id"] = str(review.id)
            inbound_row.meta = {**(inbound_row.meta or {}), "s4_intake": payload}
            await AuditRepository(session).record(
                actor=Actor.AGENT,
                actor_id="A5",
                event_type=EVENT_INTAKE_REPLY_DECIDED,
                entity_type="message",
                entity_id=draft_row.id,
                payload={
                    "inbound_message_id": str(inbound.id),
                    "seller_lead_id": str(lead.id),
                    "intake_id": str(intake.id),
                    "asked": asks,
                    "missing_required": missing,
                    "qa_verdict": qa.verdict.value,
                    "qa_confidence": qa.confidence,
                    "qa_flags": list(qa.flags),
                    "policy_action": INTAKE_REPLY_ACTION_VALUE,
                    "policy_level": decision.level.value,
                    "policy_decision": policy_decision.value,
                    "policy_reason": policy_reason,
                    "contains_sensitive": draft.contains_sensitive,
                    "photo_link_sent": result.photo_link_sent,
                },
                correlation_id=_correlation_id(),
                idempotency_key=f"s4:{inbound.id}:decided",
            )
            await session.commit()

        # -- phase 4: send when the policy says so ---------------------------------------------
        sent = False
        if policy_decision is PolicyDecision.SEND:
            sent_result = await send_draft(SendDraftInput(message_id=str(draft_row_id)))
            sent = sent_result.sent or sent_result.already_sent
        result.sent = sent
        log.info(
            "seller_intake.reply_decided",
            message_id=str(inbound.id),
            draft_message_id=str(draft_row_id),
            asked=asks,
            missing=missing,
            qa_verdict=qa.verdict.value,
            policy_decision=policy_decision.value,
            sent=sent,
        )
        return result
    finally:
        clear_context()


def _stored_payload(result: SellerIntakeResult) -> dict[str, Any]:
    keep = (
        "seller_lead_id",
        "intake_id",
        "source",
        "extracted",
        "changed_fields",
        "parcel_verified",
        "missing_required",
        "asked",
        "intake_complete",
        "offer_workflow_id",
        "signalled_intake_complete",
        "photo_link_sent",
        "skipped_reason",
        "policy_reason",
        "policy_level",
        "draft_message_id",
    )
    data = asdict(result)
    return {k: data[k] for k in keep}


async def _record_blocked_reply(
    session: AsyncSession,
    *,
    inbound_id: uuid.UUID,
    thread_id: uuid.UUID,
    to: list[str],
    subject: str,
    body: str,
    mailbox: str | None,
    agent_run_id: uuid.UUID,
    meta: dict[str, Any],
) -> uuid.UUID:
    existing = await _find_reply_row(session, str(inbound_id))
    if existing is not None:
        return existing.id
    row = Message(
        thread_id=thread_id,
        direction=Direction.OUT,
        channel=Channel.EMAIL,
        external_message_id=seller_reply_idempotency_key(str(inbound_id)),
        from_address=mailbox,
        to_addresses=list(to),
        subject=subject,
        body=body,
        draft=True,
        policy_action=INTAKE_REPLY_ACTION_VALUE,
        policy_decision=PolicyDecision.BLOCKED,
        agent_run_id=agent_run_id,
        meta=dict(meta),
    )
    session.add(row)
    await session.flush()
    return row.id


# --------------------------------------------------------------------------------------------
# seller_apply_human_decision
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SellerHumanDecisionInput:
    message_id: str  # the INBOUND message whose reply was queued
    approved: bool
    edited_body: str | None = None
    decided_by: str | None = None


@dataclass(slots=True)
class SellerHumanDecisionResult:
    message_id: str
    draft_message_id: str | None
    sent: bool
    discarded: bool
    edited: bool
    already_decided: bool = False
    reason: str | None = None


@activity.defn(name="seller_apply_human_decision")
async def seller_apply_human_decision(input: SellerHumanDecisionInput) -> SellerHumanDecisionResult:
    """Send or discard a queued S4 reply on the human's say-so. An edit becomes a second draft
    (``seller-reply:<id>:edited``) with the SEL-05 block re-appended; the first decision wins."""
    _bind()
    ctx = get_seller_context()
    try:
        async with ctx.session_factory() as session:
            inbound = await _load_message(session, input.message_id)
            draft_row = await _find_reply_row(session, input.message_id)
            if draft_row is None:
                return SellerHumanDecisionResult(
                    input.message_id, None, False, False, False, reason="no_draft"
                )
            meta = dict(draft_row.meta or {})
            applied_before = meta.get("human_decision_applied")
            if applied_before is not None:
                return SellerHumanDecisionResult(
                    input.message_id,
                    str(meta.get("sent_message_id") or draft_row.id),
                    sent=bool(meta.get("human_sent")),
                    discarded=applied_before == "rejected",
                    edited=bool(meta.get("edited")),
                    already_decided=True,
                )
            if meta.get("blocked") is True:
                return SellerHumanDecisionResult(
                    input.message_id, str(draft_row.id), False, True, False, reason="blocked"
                )
            to = list(draft_row.to_addresses)
            if not to or await _is_suppressed(session, to[0]):
                draft_row.meta = {
                    **meta,
                    "human_decision_applied": "blocked",
                    "policy_reason": "recipient is suppressed",
                }
                draft_row.policy_decision = PolicyDecision.BLOCKED
                await session.commit()
                return SellerHumanDecisionResult(
                    input.message_id, str(draft_row.id), False, True, False, reason="suppressed"
                )
            graph_body = str(meta.get("graph_draft_body") or draft_row.body)
            edited_body = (
                ensure_compliance_block(input.edited_body, _sender(ctx.settings))
                if input.edited_body and input.edited_body.strip()
                else None
            )
            edited = edited_body is not None and edited_body.strip() != graph_body.strip()
            thread_id = draft_row.thread_id
            graph_id = (inbound.meta or {}).get("graph_id")
            conversation_id = (inbound.meta or {}).get("conversation_id")
            subject = draft_row.subject or ""
            original_id = draft_row.id
            if not input.approved:
                draft_row.meta = {
                    **meta,
                    "human_decision_applied": "rejected",
                    "decided_by": input.decided_by,
                    "discarded": True,
                }
                await AuditRepository(session).record(
                    actor=Actor.HUMAN,
                    actor_id=input.decided_by,
                    event_type="reply.rejected",
                    entity_type="message",
                    entity_id=draft_row.id,
                    payload={"inbound_message_id": str(inbound.id)},
                    correlation_id=_correlation_id(),
                    idempotency_key=f"human:{inbound.id}:decision",
                )
                await session.commit()
                return SellerHumanDecisionResult(
                    input.message_id, str(original_id), sent=False, discarded=True, edited=False
                )
            await session.commit()

        send_id = original_id
        if edited and edited_body is not None:
            created = await create_draft(
                CreateDraftInput(
                    idempotency_key=f"{seller_reply_idempotency_key(str(inbound.id))}:edited",
                    to=to,
                    subject=subject,
                    body_text=edited_body.strip(),
                    reply_to_graph_message_id=str(graph_id) if graph_id else None,
                    conversation_id=str(conversation_id) if conversation_id else None,
                    thread_id=str(thread_id),
                )
            )
            send_id = uuid.UUID(created.message_id)
        sent_result = await send_draft(SendDraftInput(message_id=str(send_id)))
        sent = sent_result.sent or sent_result.already_sent

        async with ctx.session_factory() as session:
            original = await session.get(Message, original_id)
            if original is not None:
                original.meta = {
                    **(original.meta or {}),
                    "human_decision_applied": "approved",
                    "decided_by": input.decided_by,
                    "human_sent": sent,
                    "edited": edited,
                    "sent_message_id": str(send_id),
                    **({"superseded_by": str(send_id)} if edited else {}),
                }
            if edited:
                sent_row = await session.get(Message, send_id)
                if sent_row is not None:
                    sent_row.policy_action = INTAKE_REPLY_ACTION_VALUE
                    sent_row.policy_decision = PolicyDecision.QUEUE_FOR_HUMAN
                    sent_row.qa_review_id = original.qa_review_id if original else None
                    sent_row.meta = {
                        **(sent_row.meta or {}),
                        "reply_to_message_id": str(inbound.id),
                        "edited_by_human": True,
                        "supersedes": str(original_id),
                    }
            await AuditRepository(session).record(
                actor=Actor.HUMAN,
                actor_id=input.decided_by,
                event_type="reply.approved",
                entity_type="message",
                entity_id=send_id,
                payload={
                    "inbound_message_id": str(inbound.id),
                    "edited": edited,
                    "original_draft_id": str(original_id),
                },
                correlation_id=_correlation_id(),
                idempotency_key=f"human:{inbound.id}:decision",
            )
            await session.commit()
        return SellerHumanDecisionResult(
            input.message_id, str(send_id), sent=sent, discarded=False, edited=edited
        )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# seller_notify_bounce
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SellerBounceInput:
    message_id: str
    bounced_address: str | None = None


@dataclass(slots=True)
class SellerBounceResult:
    message_id: str
    signalled: bool
    sequence_workflow_id: str | None = None
    seller_lead_id: str | None = None


@activity.defn(name="seller_notify_bounce")
async def seller_notify_bounce(input: SellerBounceInput) -> SellerBounceResult:
    """Signal ``bounce`` once to the seller sequence behind the bounced address."""
    _bind()
    ctx = get_seller_context()
    try:
        async with ctx.session_factory() as session:
            message = await _load_message(session, input.message_id)
            address = input.bounced_address or (message.meta or {}).get("bounced_address")
            thread = await session.get(Thread, message.thread_id)
            repo = SellerRepository(session)
            lead: SellerLead | None = None
            if thread is not None and thread.seller_lead_id is not None:
                lead = await repo.get(thread.seller_lead_id)
            if lead is None and address:
                lead = await repo.find_by_email(str(address))
            if lead is None:
                return SellerBounceResult(input.message_id, signalled=False)
            sequence = await repo.active_sequence(lead.id)
            workflow_id = await _signal_sequence(
                ctx,
                session,
                sequence=sequence,
                signal=SIGNAL_BOUNCE,
                payload=BounceSignal(message_id=str(message.id), bounced_address=address),
                entity_id=message.id,
                idempotency_key=f"bounce:{message.id}:seller_signal",
            )
            await session.commit()
            return SellerBounceResult(
                input.message_id,
                signalled=workflow_id is not None,
                sequence_workflow_id=workflow_id,
                seller_lead_id=str(lead.id),
            )
    finally:
        clear_context()


__all__ = [
    "EVENT_INTAKE_COMPLETED",
    "EVENT_INTAKE_REPLY_DECIDED",
    "EVENT_LEAD_UNREACHABLE",
    "EVENT_OFFER_REQUESTED",
    "EVENT_PARCEL_VERIFIED",
    "EVENT_SEQUENCE_STARTED",
    "INTAKE_REPLY_ACTION",
    "OFFER_WORKFLOW_ID_PREFIX",
    "OFFER_WORKFLOW_NAME",
    "SELLER_INBOUND_WORKFLOW_ID_PREFIX",
    "SELLER_INBOUND_WORKFLOW_NAME",
    "SELLER_OUTREACH_ACTION",
    "SELLER_SEQUENCE_WORKFLOW_ID_PREFIX",
    "SELLER_SEQUENCE_WORKFLOW_NAME",
    "SIGNAL_INTAKE_COMPLETE",
    "SOURCE_EMAIL",
    "SOURCE_WEB_FORM",
    "START_FAILED_REASON",
    "IntakeCompleteSignal",
    "MarketRefreshResult",
    "OfferWorkflowInput",
    "SellerBounceInput",
    "SellerBounceResult",
    "SellerClassifyInput",
    "SellerClassifyResult",
    "SellerContext",
    "SellerHumanDecisionInput",
    "SellerHumanDecisionResult",
    "SellerIntakeInput",
    "SellerIntakeResult",
    "SellerListRefreshInput",
    "SellerListRefreshResult",
    "SellerPrepareStepInput",
    "get_seller_context",
    "offer_workflow_id",
    "refresh_market",
    "seller_apply_human_decision",
    "seller_classify_inbound",
    "seller_decide_and_send",
    "seller_inbound_workflow_id",
    "seller_intake",
    "seller_list_refresh",
    "seller_notify_bounce",
    "seller_prepare_step",
    "seller_reply_idempotency_key",
    "seller_send_approved",
    "seller_sequence_workflow_id",
    "set_seller_context",
    "start_seller_sequence",
]
