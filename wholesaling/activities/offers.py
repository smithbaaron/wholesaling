"""Offer (A7) and negotiation (S5) activities (REQUIREMENTS.md A7-01..A7-18, SEL-09, SEL-11,
POL-03g/h, WF-04, WF-05).

A7, called by ``workflows/offer.py``::

    compute_offer          steps 1-7 + the step-8 draft; persists the offer row, every
                           verification row and the calc record (A7-18)
    qa_offer_draft         A5 QA of the draft (A7-09), persisted on ``qa_reviews.offer_id``
    pre_send_check         V6: re-pull comps, re-run gates (and V5 / draft when the ARV moved)
    decide_and_send_offer  PolicyEngine(OFFER_SEND) -> Graph draft + send | queue | blocked
    send_approved_offer    a human approved the queued offer in the review queue
    withdraw_offer         a human rejected it, or the workflow gave up
    handoff_to_s5          start ``NegotiationWorkflow`` (step 10)

S5, called by ``workflows/negotiation.py``::

    load_negotiation           offer price / MAO / sent_at the workflow needs
    evaluate_counter           classify + PolicyEngine(COUNTER_ACCEPTANCE) -> accept | human
    accept_offer               status accepted + audit ``contract.requested`` (SEL-11)
    decline_offer              status declined, reason captured, requeue date recorded
    expire_offer               status expired (day 14)
    send_negotiation_message   reminder / re-engage through A5 QA + OFFER_SEND policy
    requeue_seller             the 90-day requeue after a decline

Idempotency: the offer row is keyed on the workflow-supplied ``idempotency_key``; verification
rows on ``(offer, pass, iteration)``; draft message rows on ``offer:{offer_id}:<kind>``; every
audit event on a key derived from those. A retried activity therefore finds its own work.

Dependencies come from ``OfferActivityContext`` (property provider, model runner for A7/A5,
tunables) on top of the Graph context (gateway, session factory, settings, clock) and the
conversation context (Temporal gateway); ``set_offer_context`` wires tests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from wholesaling.activities.conversation import get_conversation_context
from wholesaling.activities.graph import (
    ALERT_EVENT,
    GraphActivityContext,
    SendDraftInput,
    get_graph_context,
    send_draft,
)
from wholesaling.activities.outreach import _ensure_graph_draft
from wholesaling.agents.a5 import A5QaAgent, QaDraft, QaInput, QaResult
from wholesaling.agents.a7.audit import MODEL_OVERRIDES as A7_MODEL_OVERRIDES
from wholesaling.agents.a7.audit import A7CompAuditAgent
from wholesaling.agents.a7.engine import (
    STATUS_BLOCKED,
    A7OfferEngine,
    OfferComputation,
    SubjectInput,
)
from wholesaling.agents.a7.gates import failed_names
from wholesaling.agents.a7.templates import (
    RenderedDraft,
    check_offer_body,
    render_reengage,
    render_reminder,
)
from wholesaling.agents.llm.factory import build_runner
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.s5 import (
    NegotiationSchedule,
    classify_counter,
    counter_policy_context,
)
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.config.tunables import Tunables, get_tunables
from wholesaling.db.models.enums import (
    Actor,
    AutonomyLevel,
    Channel,
    OfferStatus,
    PolicyDecision,
    QaVerdict,
    SellerLeadStatus,
)
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Thread
from wholesaling.db.models.ops import QaReview
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.messages import MessageRepository
from wholesaling.db.repositories.offers import OfferRepository
from wholesaling.db.repositories.qa_reviews import QaReviewRepository
from wholesaling.db.repositories.review import META_WORKFLOW_ID_KEY
from wholesaling.integrations.data.property import (
    PropertyAddress,
    PropertyDataProvider,
    mock_property_provider,
)
from wholesaling.logging import bind_correlation_id, clear_context, get_logger
from wholesaling.policy.actions import PolicyAction
from wholesaling.policy.engine import PolicyContext, PolicyEngine

log = get_logger(__name__)

ACTOR_A7 = "A7"
ACTOR_S5 = "S5"
EVENT_CONTRACT_REQUESTED = "contract.requested"
NEGOTIATION_WORKFLOW_NAME = "NegotiationWorkflow"
NEGOTIATION_WORKFLOW_ID_PREFIX = "negotiation:"
DEFAULT_TIMEZONE = "America/Chicago"


@dataclass(slots=True)
class NegotiationStartInput:
    """What ``NegotiationWorkflow`` is started with (``workflows/negotiation.py`` declares the
    same two fields; Temporal's converter maps by field name)."""

    offer_id: str
    seller_lead_id: str


def negotiation_workflow_id(offer_id: str) -> str:
    return f"{NEGOTIATION_WORKFLOW_ID_PREFIX}{offer_id}"


def offer_draft_key(offer_id: str, kind: str = "draft", day: int | None = None) -> str:
    """``messages.external_message_id`` for the offer email and its follow-ups."""
    suffix = f":{kind}" if day is None else f":{kind}:{day}"
    return f"offer:{offer_id}{suffix}"


def _bind() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------------------------
# Dependency context
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class OfferActivityContext:
    property_provider: PropertyDataProvider
    runner: AgentRunner
    tunables: Tunables
    prompt_values: dict[str, Any] = field(default_factory=lambda: {"company": "our team"})


_context: OfferActivityContext | None = None


def set_offer_context(ctx: OfferActivityContext | None) -> None:
    global _context
    _context = ctx


def get_offer_context() -> OfferActivityContext:
    """Built on first use: the fixture property provider (``DATA_PROVIDER_MODE=mock``), a runner
    over the conversation context's model provider with A7's Sonnet comp-audit override, and
    the YAML tunables."""
    global _context
    if _context is None:
        graph_ctx = get_graph_context()
        conv = get_conversation_context()
        settings = graph_ctx.settings
        if settings.data_provider_mode != "mock":
            msg = (
                "DATA_PROVIDER_MODE=live has no property data provider yet (REQUIREMENTS.md "
                "INT-03); use mock"
            )
            raise NotImplementedError(msg)
        _context = OfferActivityContext(
            property_provider=mock_property_provider(),
            runner=build_runner(
                settings, provider=conv.runner.provider, overrides=A7_MODEL_OVERRIDES
            ),
            tunables=get_tunables(),
            prompt_values=dict(conv.prompt_values),
        )
    return _context


def _engine(
    ctx: GraphActivityContext,
    octx: OfferActivityContext,
    session: AsyncSession,
    *,
    correlation_id: str,
) -> A7OfferEngine:
    auditor = A7CompAuditAgent(
        octx.runner, session, correlation_id=correlation_id, prompt_values=octx.prompt_values
    )
    return A7OfferEngine(
        octx.property_provider,
        octx.tunables.a7,
        deal_band=octx.tunables.deal_band,
        identity=SenderIdentity.from_settings(ctx.settings),
        auditor=auditor,
        today=ctx.clock().date(),
    )


# --------------------------------------------------------------------------------------------
# Loading the subject
# --------------------------------------------------------------------------------------------


def seller_email(lead: SellerLead) -> str | None:
    """First primary email contact channel, else the first email channel."""
    channels = [
        c
        for c in (lead.contact_channels or [])
        if str(c.get("channel", c.get("type", ""))).lower() == "email" and c.get("value")
    ]
    for c in channels:
        if c.get("is_primary"):
            return str(c["value"])
    return str(channels[0]["value"]) if channels else None


@dataclass(slots=True)
class _Loaded:
    lead: SellerLead
    intake: Intake
    market: Market | None
    subject: SubjectInput
    email: str | None


async def _load_subject(
    session: AsyncSession, *, intake_id: uuid.UUID, exclude_offer_id: uuid.UUID | None
) -> _Loaded:
    intake = await session.get(Intake, intake_id)
    if intake is None:
        msg = f"intake {intake_id} not found"
        raise LookupError(msg)
    lead = await session.get(SellerLead, intake.seller_lead_id)
    if lead is None:
        msg = f"seller lead {intake.seller_lead_id} not found"
        raise LookupError(msg)
    market = await session.get(Market, lead.market_id) if lead.market_id else None
    email = seller_email(lead)
    messages = MessageRepository(session)
    suppressed = await messages.is_suppressed(email) if email else False
    if lead.status is SellerLeadStatus.SUPPRESSED:
        suppressed = True
    active_parcels = await OfferRepository(session).active_parcel_ids(
        exclude_offer_id=exclude_offer_id
    )
    if intake.condition_class is None:
        msg = f"intake {intake_id} has no condition class; S4 must complete before A7 runs"
        raise ValueError(msg)
    subject = SubjectInput(
        address=PropertyAddress(
            line1=lead.property_address_line1,
            city=lead.property_city,
            state=lead.property_state,
            postal_code=lead.property_postal_code,
        ),
        market_name=market.name if market else (lead.property_city or ""),
        condition_class=intake.condition_class,
        disclosed_issues=[str(x) for x in (intake.disclosed_issues or [])],
        asking_price=intake.asking_price,
        photo_count=len(intake.photos or []),
        seller_name=lead.owner_name,
        seller_email=email,
        motivation=intake.motivation,
        liens=intake.liens,
        liens_unknown=intake.liens_unknown,
        hoa=intake.hoa,
        hoa_unknown=intake.hoa_unknown,
        co_owners=intake.co_owners,
        co_owners_unknown=intake.co_owners_unknown,
        seller_suppressed=suppressed,
        active_offer_parcel_ids=active_parcels,
        market_hard_cap=market.deal_band_max if market and market.deal_band_max else None,
    )
    return _Loaded(lead=lead, intake=intake, market=market, subject=subject, email=email)


async def _persist(
    session: AsyncSession, offers: OfferRepository, offer_id: uuid.UUID, comp: OfferComputation
) -> None:
    offer = await offers.get(offer_id)
    assert offer is not None
    mao = comp.mao
    await offers.record_calculation(
        offer,
        parcel_id=comp.parcel_id,
        arv=comp.arv.arv if comp.arv else None,
        arv_confidence=comp.arv_confidence,
        arv_avm=comp.avm,
        comp_tier=comp.pull.tier.tier if comp.pull else None,
        repairs=comp.repairs.total if comp.repairs else None,
        market_pct=mao.inputs.market_pct if mao else None,
        assignment_fee=mao.inputs.assignment_fee if mao else None,
        closing_costs=mao.closing_costs if mao else None,
        mao=mao.mao if mao else None,
        negotiation_room_pct=mao.inputs.negotiation_room_pct if mao else None,
        offer_price=mao.offer if mao else None,
        gates=[g.to_json() for g in comp.gates],
        open_flags=comp.open_flags,
        calc_record={**offer.calc_record, **comp.calc_record()},
        draft_body=comp.draft.body if comp.draft else None,
    )
    for rec in comp.verifications:
        await offers.write_verification(
            offer_id,
            pass_id=rec.pass_id,
            iteration=rec.iteration,
            inputs=rec.inputs,
            outputs=rec.outputs,
            result=rec.result,
            reason=rec.reason,
            agent_run_id=rec.agent_run_id,
        )


# --------------------------------------------------------------------------------------------
# compute_offer (steps 1-7 + draft)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ComputeOfferInput:
    intake_id: str
    seller_lead_id: str
    idempotency_key: str


@dataclass(slots=True)
class ComputeOfferResult:
    offer_id: str
    status: str  # engine status: ready | escalate | blocked
    offer_status: str  # OfferStatus value
    offer_price: str | None
    arv: str | None
    arv_confidence: str
    gates_passed: bool
    gates_failed: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    verifications: int = 0
    seller_email: str | None = None
    created: bool = True


def _summary(
    offer_id: uuid.UUID,
    comp: OfferComputation,
    *,
    offer_status: OfferStatus,
    email: str | None,
    created: bool,
) -> ComputeOfferResult:
    return ComputeOfferResult(
        offer_id=str(offer_id),
        status=comp.status,
        offer_status=offer_status.value,
        offer_price=str(comp.offer_price) if comp.offer_price is not None else None,
        arv=str(comp.arv.arv) if comp.arv else None,
        arv_confidence=comp.arv_confidence.value,
        gates_passed=comp.gates_passed,
        gates_failed=failed_names(comp.gates),
        escalations=list(comp.escalations),
        blocked_reason=comp.blocked_reason,
        verifications=len(comp.verifications),
        seller_email=email,
        created=created,
    )


@activity.defn(name="offer_compute")
async def compute_offer(input: ComputeOfferInput) -> ComputeOfferResult:
    """Run A7 steps 1-7 and draft the offer; persist everything (A7-18)."""
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        intake_id = uuid.UUID(input.intake_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            existing = await offers.get_by_idempotency_key(input.idempotency_key)
            if existing is not None and existing.calc_record.get("status"):
                loaded = await _load_subject(
                    session, intake_id=intake_id, exclude_offer_id=existing.id
                )
                comp = OfferComputation.restore(loaded.subject, existing.calc_record)
                return _summary(
                    existing.id,
                    comp,
                    offer_status=existing.status,
                    email=loaded.email,
                    created=False,
                )
            offer, created = await offers.create(
                seller_lead_id=uuid.UUID(input.seller_lead_id),
                intake_id=intake_id,
                idempotency_key=input.idempotency_key,
            )
            loaded = await _load_subject(session, intake_id=intake_id, exclude_offer_id=offer.id)
            engine = _engine(ctx, octx, session, correlation_id=str(offer.id))
            comp = await engine.compute(loaded.subject)
            await _persist(session, offers, offer.id, comp)
            if comp.facts is not None:
                loaded.intake.property_facts = {
                    **(loaded.intake.property_facts or {}),
                    "a7": comp.facts.to_json(),
                }
            audit = AuditRepository(session)
            offer_status = OfferStatus.DRAFT
            if comp.status == STATUS_BLOCKED:
                offer_status = OfferStatus.BLOCKED
                await offers.set_status(
                    offer.id, OfferStatus.BLOCKED, reason=comp.blocked_reason, at=ctx.clock()
                )
                if comp.blocked_reason and comp.blocked_reason.startswith("V5"):
                    await audit.record(
                        actor=Actor.SYSTEM,
                        actor_id=ACTOR_A7,
                        event_type=ALERT_EVENT,
                        entity_type="alert",
                        entity_id=offer.id,
                        payload={
                            "kind": "a7.recompute_mismatch",
                            "detail": comp.blocked_reason,
                            "offer_id": str(offer.id),
                        },
                        correlation_id=str(offer.id),
                        idempotency_key=f"offer:{offer.id}:recompute_mismatch",
                    )
                    log.error("alert", kind="a7.recompute_mismatch", offer_id=str(offer.id))
            await audit.record(
                actor=Actor.AGENT,
                actor_id=ACTOR_A7,
                event_type="offer.computed",
                entity_type="offer",
                entity_id=offer.id,
                payload={
                    "intake_id": input.intake_id,
                    "seller_lead_id": input.seller_lead_id,
                    "status": comp.status,
                    "offer_price": str(comp.offer_price) if comp.offer_price is not None else None,
                    "arv": str(comp.arv.arv) if comp.arv else None,
                    "arv_confidence": comp.arv_confidence.value,
                    "comp_tier": comp.pull.tier.tier if comp.pull else None,
                    "gates_passed": comp.gates_passed,
                    "escalations": list(comp.escalations),
                    "blocked_reason": comp.blocked_reason,
                },
                correlation_id=str(offer.id),
                idempotency_key=f"offer:{offer.id}:computed",
            )
            await session.commit()
            log.info(
                "offer.computed",
                offer_id=str(offer.id),
                status=comp.status,
                offer_price=str(comp.offer_price),
                confidence=comp.arv_confidence.value,
            )
            return _summary(
                offer.id, comp, offer_status=offer_status, email=loaded.email, created=created
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# qa_offer_draft (A7-09: A5 checks facts and prohibited claims)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class QaOfferInput:
    offer_id: str
    force: bool = False  # re-review after V6 changed the draft


@dataclass(slots=True)
class QaOfferResult:
    offer_id: str
    verdict: str
    confidence: float
    flags: list[str] = field(default_factory=list)
    precheck_only: bool = False
    reused: bool = False


async def _latest_offer_qa(session: AsyncSession, offer_id: uuid.UUID) -> QaReview | None:
    stmt = (
        select(QaReview)
        .where(QaReview.offer_id == offer_id)
        .order_by(QaReview.created_at.desc(), QaReview.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _qa_draft(
    ctx: GraphActivityContext,
    octx: OfferActivityContext,
    session: AsyncSession,
    *,
    offer_id: uuid.UUID,
    to: str | None,
    subject: str,
    body: str,
    facts: dict[str, Any],
    now: datetime,
) -> QaResult:
    messages = MessageRepository(session)
    agent = A5QaAgent(
        octx.runner, session, correlation_id=str(offer_id), prompt_values=octx.prompt_values
    )
    return await agent.review(
        QaInput(
            draft=QaDraft(to=[to] if to else [], subject=subject, body=body),
            thread=[],
            facts=facts,
            recipient_suppressed=(await messages.is_suppressed(to)) if to else False,
            now=now,
            scheduled_sequence=True,  # the workflow owns cadence; no prior outbound to compare
        )
    )


@activity.defn(name="offer_qa_draft")
async def qa_offer_draft(input: QaOfferInput) -> QaOfferResult:
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offer = await OfferRepository(session).get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            if not input.force:
                existing = await _latest_offer_qa(session, offer_id)
                if existing is not None:
                    return QaOfferResult(
                        offer_id=input.offer_id,
                        verdict=existing.verdict.value,
                        confidence=float(existing.confidence or 0),
                        flags=list(existing.flags),
                        reused=True,
                    )
            record = offer.calc_record
            draft = record.get("draft") or {}
            body = offer.draft_body or str(draft.get("body") or "")
            lead = await session.get(SellerLead, offer.seller_lead_id)
            to = seller_email(lead) if lead else None
            if not body:
                qa = QaResult(
                    QaVerdict.BLOCK, 1.0, ["no_draft"], "offer has no draft body to review"
                )
            else:
                violations = check_offer_body(
                    body,
                    offer=Decimal(str(offer.offer_price or 0)),
                    cash_close_days=int(octx.tunables.a7.cash_close_days),
                )
                if violations:
                    qa = QaResult(
                        QaVerdict.BLOCK,
                        1.0,
                        [f"template:{v}" for v in violations],
                        "offer template guard failed",
                    )
                else:
                    qa = await _qa_draft(
                        ctx,
                        octx,
                        session,
                        offer_id=offer_id,
                        to=to,
                        subject=str(draft.get("subject") or ""),
                        body=body,
                        facts={
                            "address": record.get("subject", {}).get("address"),
                            "offer_price": str(offer.offer_price),
                            "arv": str(offer.arv),
                            "arv_confidence": offer.arv_confidence.value
                            if offer.arv_confidence
                            else None,
                            "cash_close_days": int(octx.tunables.a7.cash_close_days),
                            "gates_passed": record.get("gates_passed"),
                            "template": (
                                "locked A7-09 offer template; the number and the days come "
                                "from the calculation"
                            ),
                            "never_disclose": ["assignment fee", "spread", "any buyer lined up"],
                        },
                        now=ctx.clock(),
                    )
            await QaReviewRepository(session).record(
                offer_id=offer_id,
                verdict=qa.verdict,
                confidence=qa.confidence,
                flags=qa.flags,
                rationale=qa.rationale,
                rewrite_body=qa.rewrite_body,
                model=qa.model,
                agent_run_id=qa.run_id,
            )
            await AuditRepository(session).record(
                actor=Actor.AGENT,
                actor_id="A5",
                event_type="offer.qa_reviewed",
                entity_type="offer",
                entity_id=offer_id,
                payload={
                    "verdict": qa.verdict.value,
                    "confidence": qa.confidence,
                    "flags": list(qa.flags),
                    "forced": input.force,
                },
                correlation_id=str(offer_id),
                idempotency_key=(
                    f"offer:{offer_id}:qa:{uuid.uuid4().hex if input.force else 'first'}"
                ),
            )
            await session.commit()
            return QaOfferResult(
                offer_id=input.offer_id,
                verdict=qa.verdict.value,
                confidence=qa.confidence,
                flags=list(qa.flags),
                precheck_only=qa.precheck_only,
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# pre_send_check (V6)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class PreSendInput:
    offer_id: str


@dataclass(slots=True)
class PreSendResult:
    offer_id: str
    changed: bool
    gates_passed: bool
    offer_price: str | None
    arv: str | None
    blocked_reason: str | None = None
    result: str = "pass"


@activity.defn(name="offer_pre_send_check")
async def pre_send_check(input: PreSendInput) -> PreSendResult:
    """A7-17: immediately before send, re-pull comps and re-run the gates."""
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            loaded = await _load_subject(
                session, intake_id=offer.intake_id, exclude_offer_id=offer.id
            )
            comp = OfferComputation.restore(loaded.subject, offer.calc_record)
            before_offer = comp.offer_price
            engine = _engine(ctx, octx, session, correlation_id=str(offer.id))
            comp = await engine.pre_send_check(comp)
            v6 = comp.verifications[-1] if comp.verifications else None
            changed = v6 is not None and v6.result.value == "adjusted"
            await _persist(session, offers, offer.id, comp)
            if comp.blocked_reason and offer.status is not OfferStatus.BLOCKED:
                await offers.set_status(
                    offer.id, OfferStatus.BLOCKED, reason=comp.blocked_reason, at=ctx.clock()
                )
            await AuditRepository(session).record(
                actor=Actor.AGENT,
                actor_id=ACTOR_A7,
                event_type="offer.pre_send_checked",
                entity_type="offer",
                entity_id=offer.id,
                payload={
                    "result": v6.result.value if v6 else None,
                    "reason": v6.reason if v6 else None,
                    "offer_before": str(before_offer) if before_offer is not None else None,
                    "offer_after": str(comp.offer_price) if comp.offer_price is not None else None,
                    "gates_passed": comp.gates_passed,
                },
                correlation_id=str(offer.id),
                idempotency_key=f"offer:{offer.id}:v6:{v6.iteration if v6 else 0}",
            )
            await session.commit()
            return PreSendResult(
                offer_id=input.offer_id,
                changed=changed,
                gates_passed=comp.gates_passed,
                offer_price=str(comp.offer_price) if comp.offer_price is not None else None,
                arv=str(comp.arv.arv) if comp.arv else None,
                blocked_reason=comp.blocked_reason,
                result=v6.result.value if v6 else "pass",
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# decide_and_send_offer (step 9, POL-03g)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DecideOfferInput:
    offer_id: str
    workflow_id: str | None = None
    timezone: str = DEFAULT_TIMEZONE


@dataclass(slots=True)
class DecideOfferResult:
    offer_id: str
    decision: str
    reason: str
    level: str
    sent: bool
    message_id: str | None = None
    offer_status: str = OfferStatus.DRAFT.value


async def _offer_thread(
    session: AsyncSession, lead: SellerLead, *, subject: str, to: str
) -> Thread:
    stmt = (
        select(Thread).where(Thread.seller_lead_id == lead.id).order_by(Thread.created_at).limit(1)
    )
    thread = (await session.execute(stmt)).scalar_one_or_none()
    if thread is None:
        thread = Thread(
            channel=Channel.EMAIL, subject=subject, seller_lead_id=lead.id, counterpart_address=to
        )
        session.add(thread)
        await session.flush()
    return thread


async def _draft_row(
    ctx: GraphActivityContext,
    session: AsyncSession,
    *,
    offer_id: uuid.UUID,
    lead: SellerLead,
    to: str,
    subject: str,
    body: str,
    key: str,
    workflow_id: str | None,
    kind: str,
) -> tuple[Message, bool]:
    messages = MessageRepository(session)
    thread = await _offer_thread(session, lead, subject=subject, to=to)
    meta: dict[str, object] = {"offer_id": str(offer_id), "offer_message_kind": kind}
    if workflow_id:
        meta[META_WORKFLOW_ID_KEY] = workflow_id
    return await messages.create_outbound_draft(
        thread=thread,
        to=to,
        subject=subject,
        body=body,
        policy_action=PolicyAction.OFFER_SEND,
        idempotency_key=key,
        from_address=ctx.settings.graph_mailbox,
        meta=meta,
    )


def _gates_passed(offer: Any) -> bool:
    record = offer.calc_record or {}
    return bool(record.get("gates_passed")) and record.get("status") == "ready"


@activity.defn(name="offer_decide_and_send")
async def decide_and_send_offer(input: DecideOfferInput) -> DecideOfferResult:
    """PolicyEngine(OFFER_SEND) with ``a7_gates_passed`` + the A5 verdict + suppression; then
    send (Graph draft + send), queue for a human, or block."""
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            if offer.sent_at is not None:
                return DecideOfferResult(
                    input.offer_id,
                    PolicyDecision.SEND.value,
                    "already sent",
                    offer.policy_level_at_send.value if offer.policy_level_at_send else "",
                    True,
                    str(offer.sent_message_id) if offer.sent_message_id else None,
                    offer.status.value,
                )
            lead = await session.get(SellerLead, offer.seller_lead_id)
            assert lead is not None
            to = seller_email(lead)
            messages = MessageRepository(session)
            now = ctx.clock()
            qa = await _latest_offer_qa(session, offer_id)
            # No address is not "suppressed": the human can add one in the review queue.
            suppressed = (await messages.is_suppressed(to)) if to else False
            pctx = PolicyContext(
                qa_verdict=qa.verdict if qa else None,
                qa_confidence=qa.confidence if qa else None,
                qa_flags=list(qa.flags) if qa else [],
                recipient_suppressed=suppressed,
                sends_today=await messages.count_sends_today(
                    PolicyAction.OFFER_SEND, input.timezone, now=now
                ),
                a7_gates_passed=_gates_passed(offer),
                now=now,
            )
            decision = await PolicyEngine(session).decide(PolicyAction.OFFER_SEND, pctx)
            if (
                offer.status is OfferStatus.BLOCKED
                and decision.decision is not PolicyDecision.BLOCKED
            ):
                # A V5 mismatch already blocked the row: the policy cannot un-block a code bug.
                decision = replace(
                    decision,
                    decision=PolicyDecision.BLOCKED,
                    reason=f"offer blocked: {offer.calc_record.get('blocked_reason')}",
                )
            draft = offer.calc_record.get("draft") or {}
            row: Message | None = None
            if to and offer.draft_body:
                row, _ = await _draft_row(
                    ctx,
                    session,
                    offer_id=offer_id,
                    lead=lead,
                    to=to,
                    subject=str(
                        draft.get("subject") or f"Cash offer for {lead.property_address_line1}"
                    ),
                    body=offer.draft_body,
                    key=offer_draft_key(input.offer_id),
                    workflow_id=input.workflow_id,
                    kind="offer",
                )
                row.policy_decision = decision.decision
                row.meta = {
                    **row.meta,
                    "policy_reason": decision.reason,
                    "policy_level": decision.level.value,
                    "policy_behaviour": decision.behaviour.value,
                }
            elif decision.decision is PolicyDecision.SEND:
                decision = replace(
                    decision,
                    decision=PolicyDecision.QUEUE_FOR_HUMAN,
                    reason="no seller email on record" if not to else "no draft body",
                )
            new_status = {
                PolicyDecision.SEND: OfferStatus.SENT,
                PolicyDecision.QUEUE_FOR_HUMAN: OfferStatus.PENDING_REVIEW,
                PolicyDecision.BLOCKED: OfferStatus.BLOCKED,
            }[decision.decision]
            if decision.decision is not PolicyDecision.SEND:
                await offers.set_status(offer_id, new_status, reason=decision.reason, at=now)
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="policy",
                event_type="offer.policy_decided",
                entity_type="offer",
                entity_id=offer_id,
                payload={
                    "action": PolicyAction.OFFER_SEND.value,
                    "level": decision.level.value,
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "a7_gates_passed": pctx.a7_gates_passed,
                    "qa_verdict": pctx.qa_verdict.value if pctx.qa_verdict else None,
                    "recipient_suppressed": suppressed,
                    "message_id": str(row.id) if row else None,
                },
                correlation_id=str(offer_id),
                idempotency_key=f"offer:{offer_id}:decided:{now.isoformat()}",
            )
            if decision.sends and row is not None:
                await _ensure_graph_draft(ctx, session, row)
            await session.commit()
            row_id = row.id if row else None

        sent = False
        if decision.sends and row_id is not None:
            result = await send_draft(SendDraftInput(message_id=str(row_id)))
            sent = result.sent or result.already_sent
            async with ctx.session_factory() as session:
                offers = OfferRepository(session)
                sched = NegotiationSchedule.from_config(octx.tunables.sequences)
                sent_at = ctx.clock()
                await offers.mark_sent(
                    offer_id,
                    sent_at=sent_at,
                    expires_at=sent_at + timedelta(days=sched.expire_day),
                    message_id=row_id,
                    level=decision.level,
                )
                await AuditRepository(session).record(
                    actor=Actor.SYSTEM,
                    actor_id=ACTOR_A7,
                    event_type="offer.sent",
                    entity_type="offer",
                    entity_id=offer_id,
                    payload={
                        "message_id": str(row_id),
                        "level": decision.level.value,
                        "auto": True,
                    },
                    correlation_id=str(offer_id),
                    idempotency_key=f"offer:{offer_id}:sent",
                )
                await session.commit()
        log.info(
            "offer.policy_decided",
            offer_id=input.offer_id,
            decision=decision.decision.value,
            reason=decision.reason,
            sent=sent,
        )
        return DecideOfferResult(
            offer_id=input.offer_id,
            decision=decision.decision.value,
            reason=decision.reason,
            level=decision.level.value,
            sent=sent,
            message_id=str(row_id) if row_id else None,
            offer_status=new_status.value,
        )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# send_approved_offer / withdraw_offer (human decisions from the review queue)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SendApprovedOfferInput:
    offer_id: str
    actor_id: str | None = None
    edited_body: str | None = None


@dataclass(slots=True)
class SendApprovedOfferResult:
    offer_id: str
    sent: bool
    blocked_reason: str | None = None
    body_edited: bool = False


@activity.defn(name="offer_send_approved")
async def send_approved_offer(input: SendApprovedOfferInput) -> SendApprovedOfferResult:
    """A human approved the queued offer. Suppression still applies; an edited body must still
    pass the A7-09 template guard (the number and the terms are not editable)."""
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            if offer.sent_at is not None:
                return SendApprovedOfferResult(input.offer_id, sent=True)
            if offer.status is OfferStatus.BLOCKED:
                return SendApprovedOfferResult(
                    input.offer_id,
                    sent=False,
                    blocked_reason=str(
                        offer.calc_record.get("blocked_reason") or "offer is blocked"
                    ),
                )
            lead = await session.get(SellerLead, offer.seller_lead_id)
            assert lead is not None
            to = seller_email(lead)
            messages = MessageRepository(session)
            if not to or await messages.is_suppressed(to):
                await offers.set_status(
                    offer_id, OfferStatus.BLOCKED, reason="recipient is suppressed", at=ctx.clock()
                )
                await session.commit()
                return SendApprovedOfferResult(
                    input.offer_id, sent=False, blocked_reason="recipient is suppressed"
                )
            edited = False
            body = offer.draft_body or ""
            if (
                input.edited_body is not None
                and input.edited_body.strip()
                and input.edited_body != body
            ):
                violations = check_offer_body(
                    input.edited_body,
                    offer=Decimal(str(offer.offer_price or 0)),
                    cash_close_days=int(octx.tunables.a7.cash_close_days),
                )
                if violations:
                    return SendApprovedOfferResult(
                        input.offer_id,
                        sent=False,
                        blocked_reason="edited body fails the offer template guard: "
                        + ", ".join(violations),
                    )
                body = input.edited_body
                edited = True
                offer.draft_body = body
            draft = offer.calc_record.get("draft") or {}
            row, _ = await _draft_row(
                ctx,
                session,
                offer_id=offer_id,
                lead=lead,
                to=to,
                subject=str(
                    draft.get("subject") or f"Cash offer for {lead.property_address_line1}"
                ),
                body=body,
                key=offer_draft_key(input.offer_id),
                workflow_id=None,
                kind="offer",
            )
            if edited:
                row.meta = {**row.meta, "original_body": row.body}
                row.body = body
            row.policy_decision = PolicyDecision.SEND
            row.meta = {
                **row.meta,
                "human_approved": True,
                "approved_by": input.actor_id,
                "policy_reason": "approved by human in review queue",
            }
            await AuditRepository(session).record(
                actor=Actor.HUMAN,
                actor_id=input.actor_id,
                event_type="offer.human_approved",
                entity_type="offer",
                entity_id=offer_id,
                payload={"body_edited": edited, "message_id": str(row.id)},
                correlation_id=str(offer_id),
                idempotency_key=f"offer:{offer_id}:approved",
            )
            await _ensure_graph_draft(ctx, session, row)
            await session.commit()
            row_id = row.id
        result = await send_draft(SendDraftInput(message_id=str(row_id)))
        sent = result.sent or result.already_sent
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            sched = NegotiationSchedule.from_config(octx.tunables.sequences)
            sent_at = ctx.clock()
            await offers.mark_sent(
                offer_id,
                sent_at=sent_at,
                expires_at=sent_at + timedelta(days=sched.expire_day),
                message_id=row_id,
                level=AutonomyLevel.L0,
            )
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id=ACTOR_A7,
                event_type="offer.sent",
                entity_type="offer",
                entity_id=offer_id,
                payload={"message_id": str(row_id), "auto": False, "approved_by": input.actor_id},
                correlation_id=str(offer_id),
                idempotency_key=f"offer:{offer_id}:sent",
            )
            await session.commit()
        return SendApprovedOfferResult(input.offer_id, sent=sent, body_edited=edited)
    finally:
        clear_context()


@dataclass(slots=True)
class WithdrawOfferInput:
    offer_id: str
    reason: str
    actor_id: str | None = None


@dataclass(slots=True)
class WithdrawOfferResult:
    offer_id: str
    status: str


@activity.defn(name="offer_withdraw")
async def withdraw_offer(input: WithdrawOfferInput) -> WithdrawOfferResult:
    _bind()
    ctx = get_graph_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            if offer.status in (OfferStatus.ACCEPTED, OfferStatus.WITHDRAWN):
                return WithdrawOfferResult(input.offer_id, offer.status.value)
            await offers.set_status(
                offer_id, OfferStatus.WITHDRAWN, reason=input.reason, at=ctx.clock()
            )
            await AuditRepository(session).record(
                actor=Actor.HUMAN if input.actor_id else Actor.SYSTEM,
                actor_id=input.actor_id or ACTOR_A7,
                event_type="offer.withdrawn",
                entity_type="offer",
                entity_id=offer_id,
                payload={"reason": input.reason},
                correlation_id=str(offer_id),
                idempotency_key=f"offer:{offer_id}:withdrawn",
            )
            await session.commit()
            return WithdrawOfferResult(input.offer_id, OfferStatus.WITHDRAWN.value)
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# handoff_to_s5 (step 10)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class HandoffInput:
    offer_id: str
    seller_lead_id: str


@dataclass(slots=True)
class HandoffResult:
    offer_id: str
    workflow_id: str
    started: bool


@activity.defn(name="offer_handoff_to_s5")
async def handoff_to_s5(input: HandoffInput) -> HandoffResult:
    """Start ``NegotiationWorkflow`` (id ``negotiation:{offer_id}``) and mark the lead offered."""
    _bind()
    ctx = get_graph_context()
    conv = get_conversation_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        wf_id = negotiation_workflow_id(input.offer_id)
        async with ctx.session_factory() as session:
            offer = await OfferRepository(session).get(offer_id)
            if offer is None or offer.sent_at is None:
                msg = f"offer {input.offer_id} is not sent; nothing to negotiate"
                raise ValueError(msg)
            lead = await session.get(SellerLead, offer.seller_lead_id)
            if lead is not None and lead.status not in (
                SellerLeadStatus.SUPPRESSED,
                SellerLeadStatus.UNDER_CONTRACT,
                SellerLeadStatus.CLOSED,
            ):
                lead.status = SellerLeadStatus.OFFERED
            await conv.temporal.start_workflow(
                NEGOTIATION_WORKFLOW_NAME,
                [
                    NegotiationStartInput(
                        offer_id=input.offer_id, seller_lead_id=input.seller_lead_id
                    )
                ],
                workflow_id=wf_id,
            )
            _, created = await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id=ACTOR_A7,
                event_type="offer.handed_to_s5",
                entity_type="offer",
                entity_id=offer_id,
                payload={"workflow_id": wf_id, "seller_lead_id": input.seller_lead_id},
                correlation_id=str(offer_id),
                idempotency_key=f"offer:{offer_id}:handoff",
            )
            await session.commit()
            return HandoffResult(input.offer_id, wf_id, started=created)
    finally:
        clear_context()


# ============================================================================================
# S5 activities
# ============================================================================================


@dataclass(slots=True)
class LoadNegotiationInput:
    offer_id: str


@dataclass(slots=True)
class NegotiationTerms:
    offer_id: str
    status: str
    offer_price: str | None
    mao: str | None
    sent_at: str | None
    seller_email: str | None
    address: str
    schedule: dict[str, Any] = field(default_factory=dict)


@activity.defn(name="negotiation_load")
async def load_negotiation(input: LoadNegotiationInput) -> NegotiationTerms:
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        async with ctx.session_factory() as session:
            offer = await OfferRepository(session).get(uuid.UUID(input.offer_id))
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            lead = await session.get(SellerLead, offer.seller_lead_id)
            sent_at = _aware(offer.sent_at)
            return NegotiationTerms(
                offer_id=input.offer_id,
                status=offer.status.value,
                offer_price=str(offer.offer_price) if offer.offer_price is not None else None,
                mao=str(offer.mao) if offer.mao is not None else None,
                sent_at=sent_at.isoformat() if sent_at else None,
                seller_email=seller_email(lead) if lead else None,
                address=lead.property_address_line1 if lead else "",
                schedule=NegotiationSchedule.from_config(octx.tunables.sequences).to_json(),
            )
    finally:
        clear_context()


@dataclass(slots=True)
class EvaluateCounterInput:
    offer_id: str
    amount: str
    message_id: str | None = None
    workflow_id: str | None = None


@dataclass(slots=True)
class CounterDecision:
    offer_id: str
    amount: str
    classification: str
    within_room: bool
    decision: str  # PolicyDecision value
    reason: str
    level: str
    accepted: bool
    offer_status: str


@activity.defn(name="negotiation_evaluate_counter")
async def evaluate_counter(input: EvaluateCounterInput) -> CounterDecision:
    """SEL-09 / POL-03h: inside the room -> PolicyEngine(COUNTER_ACCEPTANCE) may accept
    automatically (L1/L2); at L0 or above MAO a human decides."""
    _bind()
    ctx = get_graph_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        amount = Decimal(input.amount)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None or offer.offer_price is None or offer.mao is None:
                msg = f"offer {input.offer_id} has no price to negotiate against"
                raise ValueError(msg)
            now = ctx.clock()
            evaluation = classify_counter(amount, offer=offer.offer_price, mao=offer.mao)
            lead = await session.get(SellerLead, offer.seller_lead_id)
            to = seller_email(lead) if lead else None
            suppressed = (await MessageRepository(session).is_suppressed(to)) if to else False
            if offer.status in (
                OfferStatus.EXPIRED,
                OfferStatus.DECLINED,
                OfferStatus.WITHDRAWN,
                OfferStatus.BLOCKED,
            ):
                # The number is stale once the offer lapsed: a human must re-open it.
                pctx = counter_policy_context(evaluation, seller_suppressed=suppressed)
                pctx.within_negotiation_room = False
                stale_reason = f"offer is {offer.status.value}; counter needs human review"
            else:
                pctx = counter_policy_context(evaluation, seller_suppressed=suppressed)
                stale_reason = None
            decision = await PolicyEngine(session).decide(PolicyAction.COUNTER_ACCEPTANCE, pctx)
            record = dict(offer.calc_record)
            counters = list(record.get("counters") or [])
            counters.append(
                {
                    **evaluation.to_json(),
                    "at": now.isoformat(),
                    "message_id": input.message_id,
                    "decision": decision.decision.value,
                    "reason": stale_reason or decision.reason,
                }
            )
            record["counters"] = counters
            offer.calc_record = record
            accepted = decision.sends and stale_reason is None
            if accepted:
                await offers.set_accepted_price(offer_id, amount)
                await offers.set_status(
                    offer_id,
                    OfferStatus.ACCEPTED,
                    reason=f"counter {amount} accepted automatically ({decision.reason})",
                    at=now,
                )
                await _contract_requested(
                    session,
                    offer_id,
                    offer.seller_lead_id,
                    price=amount,
                    actor=Actor.AGENT,
                    actor_id=ACTOR_S5,
                    how="counter_auto_accept",
                )
            elif offer.status not in (
                OfferStatus.EXPIRED,
                OfferStatus.DECLINED,
                OfferStatus.WITHDRAWN,
                OfferStatus.BLOCKED,
                OfferStatus.ACCEPTED,
            ):
                await offers.set_status(
                    offer_id, OfferStatus.COUNTERED, reason=stale_reason or decision.reason, at=now
                )
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="policy",
                event_type="offer.countered",
                entity_type="offer",
                entity_id=offer_id,
                payload={
                    "amount": str(amount),
                    **evaluation.to_json(),
                    "decision": decision.decision.value,
                    "level": decision.level.value,
                    "reason": stale_reason or decision.reason,
                    "accepted": accepted,
                },
                correlation_id=str(offer_id),
                idempotency_key=f"offer:{offer_id}:counter:{input.message_id or now.isoformat()}",
            )
            await session.commit()
            status = (await offers.get(offer_id)).status  # type: ignore[union-attr]
            return CounterDecision(
                offer_id=input.offer_id,
                amount=str(amount),
                classification=evaluation.classification,
                within_room=evaluation.within_room,
                decision=decision.decision.value,
                reason=stale_reason or decision.reason,
                level=decision.level.value,
                accepted=accepted,
                offer_status=status.value,
            )
    finally:
        clear_context()


async def _contract_requested(
    session: AsyncSession,
    offer_id: uuid.UUID,
    seller_lead_id: uuid.UUID,
    *,
    price: Decimal,
    actor: Actor,
    actor_id: str | None,
    how: str,
) -> None:
    """SEL-11: ``contract.requested``; a human signs (POL-03i); S6 starts from
    ``contract.signed``."""
    await AuditRepository(session).record(
        actor=actor,
        actor_id=actor_id,
        event_type=EVENT_CONTRACT_REQUESTED,
        entity_type="offer",
        entity_id=offer_id,
        payload={
            "offer_id": str(offer_id),
            "seller_lead_id": str(seller_lead_id),
            "price": str(price),
            "how": how,
            "next": (
                "human signs the purchase contract (POL-03i hard floor); "
                "S6 starts on contract.signed"
            ),
        },
        correlation_id=str(offer_id),
        idempotency_key=f"offer:{offer_id}:contract_requested",
    )


@dataclass(slots=True)
class AcceptOfferInput:
    offer_id: str
    amount: str | None = None  # None = the offer price
    actor_id: str | None = None
    source: str = "seller"  # seller | human


@dataclass(slots=True)
class AcceptOfferResult:
    offer_id: str
    status: str
    price: str


@activity.defn(name="negotiation_accept")
async def accept_offer(input: AcceptOfferInput) -> AcceptOfferResult:
    _bind()
    ctx = get_graph_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None or offer.offer_price is None:
                msg = f"offer {input.offer_id} has no price"
                raise ValueError(msg)
            price = Decimal(input.amount) if input.amount else offer.offer_price
            if offer.status is not OfferStatus.ACCEPTED:
                await offers.set_accepted_price(offer_id, price)
                await offers.set_status(
                    offer_id,
                    OfferStatus.ACCEPTED,
                    reason=f"accepted ({input.source})",
                    at=ctx.clock(),
                )
                await _contract_requested(
                    session,
                    offer_id,
                    offer.seller_lead_id,
                    price=price,
                    actor=Actor.HUMAN if input.source == "human" else Actor.AGENT,
                    actor_id=input.actor_id or ACTOR_S5,
                    how=input.source,
                )
                await session.commit()
            return AcceptOfferResult(input.offer_id, OfferStatus.ACCEPTED.value, str(price))
    finally:
        clear_context()


@dataclass(slots=True)
class DeclineOfferInput:
    offer_id: str
    reason: str
    actor_id: str | None = None


@dataclass(slots=True)
class DeclineOfferResult:
    offer_id: str
    status: str
    requeue_at: str


@activity.defn(name="negotiation_decline")
async def decline_offer(input: DeclineOfferInput) -> DeclineOfferResult:
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            now = ctx.clock()
            days = NegotiationSchedule.from_config(octx.tunables.sequences).decline_requeue_days
            requeue_at = now + timedelta(days=days)
            if offer.status is not OfferStatus.DECLINED:
                record = dict(offer.calc_record)
                record["decline"] = {
                    "reason": input.reason,
                    "at": now.isoformat(),
                    "requeue_at": requeue_at.isoformat(),
                }
                offer.calc_record = record
                await offers.set_status(offer_id, OfferStatus.DECLINED, reason=input.reason, at=now)
                lead = await session.get(SellerLead, offer.seller_lead_id)
                if lead is not None and lead.status is SellerLeadStatus.OFFERED:
                    lead.status = SellerLeadStatus.REPLIED
                await AuditRepository(session).record(
                    actor=Actor.HUMAN if input.actor_id else Actor.AGENT,
                    actor_id=input.actor_id or ACTOR_S5,
                    event_type="offer.declined",
                    entity_type="offer",
                    entity_id=offer_id,
                    payload={
                        "reason": input.reason,
                        "requeue_at": requeue_at.isoformat(),
                        "requeue_days": days,
                    },
                    correlation_id=str(offer_id),
                    idempotency_key=f"offer:{offer_id}:declined",
                )
                await session.commit()
            return DeclineOfferResult(
                input.offer_id, OfferStatus.DECLINED.value, requeue_at.isoformat()
            )
    finally:
        clear_context()


@dataclass(slots=True)
class ExpireOfferInput:
    offer_id: str


@dataclass(slots=True)
class ExpireOfferResult:
    offer_id: str
    status: str
    expired: bool


@activity.defn(name="negotiation_expire")
async def expire_offer(input: ExpireOfferInput) -> ExpireOfferResult:
    _bind()
    ctx = get_graph_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            if offer.status not in (OfferStatus.SENT, OfferStatus.COUNTERED):
                return ExpireOfferResult(input.offer_id, offer.status.value, expired=False)
            await offers.set_status(
                offer_id, OfferStatus.EXPIRED, reason="no response by expiry day", at=ctx.clock()
            )
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id=ACTOR_S5,
                event_type="offer.expired",
                entity_type="offer",
                entity_id=offer_id,
                payload={},
                correlation_id=str(offer_id),
                idempotency_key=f"offer:{offer_id}:expired",
            )
            await session.commit()
            return ExpireOfferResult(input.offer_id, OfferStatus.EXPIRED.value, expired=True)
    finally:
        clear_context()


@dataclass(slots=True)
class NegotiationMessageInput:
    offer_id: str
    kind: str  # reminder | reengage
    day: int
    workflow_id: str | None = None
    timezone: str = DEFAULT_TIMEZONE


@dataclass(slots=True)
class NegotiationMessageResult:
    offer_id: str
    kind: str
    day: int
    decision: str
    reason: str
    sent: bool
    message_id: str | None = None
    skipped_reason: str | None = None


@activity.defn(name="negotiation_send_message")
async def send_negotiation_message(input: NegotiationMessageInput) -> NegotiationMessageResult:
    """Reminder (days 2/5/10) or re-engagement (days 60/90) from the locked templates, through
    A5 QA and the OFFER_SEND policy. Queued messages wait in the review queue; the workflow
    does not wait for them."""
    _bind()
    ctx = get_graph_context()
    octx = get_offer_context()
    try:
        offer_id = uuid.UUID(input.offer_id)
        async with ctx.session_factory() as session:
            offers = OfferRepository(session)
            offer = await offers.get(offer_id)
            if offer is None:
                msg = f"offer {input.offer_id} not found"
                raise LookupError(msg)
            lead = await session.get(SellerLead, offer.seller_lead_id)
            assert lead is not None
            to = seller_email(lead)
            messages = MessageRepository(session)
            key = offer_draft_key(input.offer_id, input.kind, input.day)
            existing = await messages.get_by_external_id(key)
            if existing is not None and existing.sent_at is not None:
                return NegotiationMessageResult(
                    input.offer_id,
                    input.kind,
                    input.day,
                    "send",
                    "already sent",
                    True,
                    str(existing.id),
                )
            if not to:
                return NegotiationMessageResult(
                    input.offer_id,
                    input.kind,
                    input.day,
                    "blocked",
                    "no seller email",
                    False,
                    skipped_reason="no_email",
                )
            if input.kind == "reminder" and offer.status not in (
                OfferStatus.SENT,
                OfferStatus.COUNTERED,
            ):
                return NegotiationMessageResult(
                    input.offer_id,
                    input.kind,
                    input.day,
                    "skipped",
                    f"offer is {offer.status.value}",
                    False,
                    skipped_reason="status",
                )
            if input.kind == "reengage" and offer.status not in (
                OfferStatus.EXPIRED,
                OfferStatus.SENT,
                OfferStatus.COUNTERED,
            ):
                return NegotiationMessageResult(
                    input.offer_id,
                    input.kind,
                    input.day,
                    "skipped",
                    f"offer is {offer.status.value}",
                    False,
                    skipped_reason="status",
                )
            identity = SenderIdentity.from_settings(ctx.settings)
            address = PropertyAddress(
                lead.property_address_line1,
                lead.property_city,
                lead.property_state,
                lead.property_postal_code,
            ).display
            sent_at = _aware(offer.sent_at)
            draft: RenderedDraft
            if input.kind == "reminder":
                draft = render_reminder(
                    seller_name=lead.owner_name,
                    address=address,
                    offer=Decimal(str(offer.offer_price or 0)),
                    sent_on=sent_at.date().isoformat() if sent_at else "recently",
                    cash_close_days=int(octx.tunables.a7.cash_close_days),
                    identity=identity,
                )
            elif input.kind == "reengage":
                draft = render_reengage(
                    seller_name=lead.owner_name, address=address, identity=identity
                )
            else:
                msg = f"unknown negotiation message kind {input.kind!r}"
                raise ValueError(msg)
            row, _ = await _draft_row(
                ctx,
                session,
                offer_id=offer_id,
                lead=lead,
                to=to,
                subject=draft.subject,
                body=draft.body,
                key=key,
                workflow_id=input.workflow_id,
                kind=input.kind,
            )
            now = ctx.clock()
            review = await messages.latest_qa_verdict(row.id)
            if review is None:
                qa = await _qa_draft(
                    ctx,
                    octx,
                    session,
                    offer_id=offer_id,
                    to=to,
                    subject=draft.subject,
                    body=draft.body,
                    facts={
                        "offer_price": str(offer.offer_price),
                        "kind": input.kind,
                        "day": input.day,
                    },
                    now=now,
                )
                review = await QaReviewRepository(session).record(
                    message_id=row.id,
                    verdict=qa.verdict,
                    confidence=qa.confidence,
                    flags=qa.flags,
                    rationale=qa.rationale,
                    rewrite_body=qa.rewrite_body,
                    model=qa.model,
                    agent_run_id=qa.run_id,
                )
            pctx = PolicyContext(
                qa_verdict=review.verdict,
                qa_confidence=review.confidence,
                qa_flags=list(review.flags),
                recipient_suppressed=await messages.is_suppressed(to),
                sends_today=await messages.count_sends_today(
                    PolicyAction.OFFER_SEND, input.timezone, now=now
                ),
                a7_gates_passed=_gates_passed(offer),
                now=now,
            )
            decision = await PolicyEngine(session).decide(PolicyAction.OFFER_SEND, pctx)
            row.policy_decision = decision.decision
            row.meta = {
                **row.meta,
                "policy_reason": decision.reason,
                "policy_level": decision.level.value,
            }
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="policy",
                event_type=f"offer.{input.kind}_decided",
                entity_type="offer",
                entity_id=offer_id,
                payload={
                    "day": input.day,
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "level": decision.level.value,
                    "message_id": str(row.id),
                },
                correlation_id=str(offer_id),
                idempotency_key=f"{key}:decided",
            )
            if decision.sends:
                await _ensure_graph_draft(ctx, session, row)
            await session.commit()
            row_id = row.id
        sent = False
        if decision.sends:
            result = await send_draft(SendDraftInput(message_id=str(row_id)))
            sent = result.sent or result.already_sent
        return NegotiationMessageResult(
            input.offer_id,
            input.kind,
            input.day,
            decision.decision.value,
            decision.reason,
            sent,
            str(row_id),
        )
    finally:
        clear_context()


@dataclass(slots=True)
class RequeueInput:
    offer_id: str
    seller_lead_id: str


@dataclass(slots=True)
class RequeueResult:
    seller_lead_id: str
    status: str
    requeued: bool


@activity.defn(name="negotiation_requeue_seller")
async def requeue_seller(input: RequeueInput) -> RequeueResult:
    """SEL-09: a declined seller goes back to the outreach queue after the configured days."""
    _bind()
    ctx = get_graph_context()
    try:
        lead_id = uuid.UUID(input.seller_lead_id)
        async with ctx.session_factory() as session:
            lead = await session.get(SellerLead, lead_id)
            if lead is None:
                msg = f"seller lead {input.seller_lead_id} not found"
                raise LookupError(msg)
            if lead.status in (
                SellerLeadStatus.SUPPRESSED,
                SellerLeadStatus.UNDER_CONTRACT,
                SellerLeadStatus.CLOSED,
                SellerLeadStatus.DEAD,
            ):
                return RequeueResult(input.seller_lead_id, lead.status.value, requeued=False)
            lead.status = SellerLeadStatus.QUEUED
            _, created = await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id=ACTOR_S5,
                event_type="seller.requeued",
                entity_type="seller_lead",
                entity_id=lead_id,
                payload={"offer_id": input.offer_id, "reason": "declined offer requeue (SEL-09)"},
                correlation_id=input.offer_id,
                idempotency_key=f"offer:{input.offer_id}:requeued",
            )
            await session.commit()
            return RequeueResult(
                input.seller_lead_id, SellerLeadStatus.QUEUED.value, requeued=created
            )
    finally:
        clear_context()


__all__ = [
    "ACTOR_A7",
    "ACTOR_S5",
    "EVENT_CONTRACT_REQUESTED",
    "NEGOTIATION_WORKFLOW_ID_PREFIX",
    "NEGOTIATION_WORKFLOW_NAME",
    "AcceptOfferInput",
    "AcceptOfferResult",
    "ComputeOfferInput",
    "ComputeOfferResult",
    "CounterDecision",
    "DecideOfferInput",
    "DecideOfferResult",
    "DeclineOfferInput",
    "DeclineOfferResult",
    "EvaluateCounterInput",
    "ExpireOfferInput",
    "ExpireOfferResult",
    "HandoffInput",
    "HandoffResult",
    "LoadNegotiationInput",
    "NegotiationMessageInput",
    "NegotiationMessageResult",
    "NegotiationStartInput",
    "NegotiationTerms",
    "OfferActivityContext",
    "PreSendInput",
    "PreSendResult",
    "QaOfferInput",
    "QaOfferResult",
    "RequeueInput",
    "RequeueResult",
    "SendApprovedOfferInput",
    "SendApprovedOfferResult",
    "WithdrawOfferInput",
    "WithdrawOfferResult",
    "accept_offer",
    "compute_offer",
    "decide_and_send_offer",
    "decline_offer",
    "evaluate_counter",
    "expire_offer",
    "get_offer_context",
    "handoff_to_s5",
    "load_negotiation",
    "negotiation_workflow_id",
    "offer_draft_key",
    "pre_send_check",
    "qa_offer_draft",
    "requeue_seller",
    "seller_email",
    "send_approved_offer",
    "send_negotiation_message",
    "set_offer_context",
    "withdraw_offer",
]
