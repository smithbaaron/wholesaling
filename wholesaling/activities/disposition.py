"""Disposition activities (REQUIREMENTS.md SEL-10, SEL-11, BB-09, BB-11, POL-03k, WF-06, XC-05).

Called by ``workflows/disposition.py`` (and, for ``record_buyer_response``, by the ``/deals``
page and later the inbound pipeline). One contract runs through:

    load_contract            snapshot of the row for the workflow (status, deadlines, buyer)
    run_blast                S6 selects buyers, drafts, A5 QA, PolicyEngine(BUYER_DISPOSITION_BLAST)
                             -> send (Graph draft + send) | queue_for_human | blocked, per buyer
    send_approved_blast      a human approved a queued blast draft in the review queue
    record_buyer_response    interested / pass + proof of funds + deposit terms on the row
    raise_deadline_alert     ``alert.raised`` audit + log (+ Slack when a webhook is configured)
    record_workflow_state    the workflow's state and next alert on ``contracts.meta`` for /deals
    deal_dead_followup       reminder audit to release earnest money (a human does it)

Hard floors (POL-03i/j, POL-06, RUL-06): **nothing in this module imports the DocuSign gateway
or changes contract status, earnest money, deposits, buyer selection or closing.** Those are
human actions on ``/deals``. The blast policy context sets ``deal_approved`` only when the row
is ``signed``; the template itself refuses to render otherwise.

Idempotency: the draft row for buyer ``i`` on contract ``c`` is keyed
``disp:{contract_id}:{investor_id}``; a retried ``run_blast`` returns the existing rows and never
re-sends; audit keys derive from the same value (XC-03, RUL-05).

Dependencies come from ``DispositionContext`` (session factory, settings, clock, Graph gateway
for the sends, optional Slack client); built from ``GraphActivityContext`` on first use so one
``set_graph_context`` wires both for tests. The A5 model runner comes from
``ConversationContext`` as it does for outreach.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.activities.conversation import get_conversation_context
from wholesaling.activities.graph import (
    ALERT_EVENT,
    GraphActivityContext,
    SendDraftInput,
    get_graph_context,
    send_draft,
)
from wholesaling.agents.a5 import QaDraft, QaInput
from wholesaling.agents.s6 import BuyerCandidate, DealFacts, S6Disposition, S6QaAgent
from wholesaling.agents.templates.investor_outreach import SenderIdentity, footer
from wholesaling.config.settings import Settings
from wholesaling.db.models.deals import BuyerResponse, Contract, ContractStatus, EarnestMoneyStatus
from wholesaling.db.models.enums import Actor, PolicyDecision
from wholesaling.db.models.messaging import Message
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.contracts import ContractRepository, contract_address
from wholesaling.db.repositories.messages import MessageRepository
from wholesaling.db.repositories.qa_reviews import QaReviewRepository
from wholesaling.db.repositories.review import META_WORKFLOW_ID_KEY
from wholesaling.integrations.graph import GraphGateway, OutboundDraft
from wholesaling.logging import bind_correlation_id, clear_context, get_logger
from wholesaling.policy.actions import PolicyAction
from wholesaling.policy.engine import PolicyContext, PolicyEngine

log = get_logger(__name__)

Clock = Callable[[], datetime]
AGENT_ACTOR_ID = "S6"
BLAST_ACTION = PolicyAction.BUYER_DISPOSITION_BLAST
WORKFLOW_ID_PREFIX = "disposition:"
EVENT_BLAST_PREPARED = "disposition.blast_prepared"
EVENT_BLAST_QA = "disposition.qa_reviewed"
EVENT_BLAST_DECIDED = "disposition.policy_decided"
EVENT_BLAST_HUMAN_APPROVED = "disposition.human_approved"
EVENT_DEAL_DEAD_FOLLOWUP = "contract.dead_followup"
EVENT_WORKFLOW_STATE = "disposition.workflow_state"
SLACK_TIMEOUT_SECONDS = 10.0


def disposition_workflow_id(contract_id: str) -> str:
    return f"{WORKFLOW_ID_PREFIX}{contract_id}"


def blast_idempotency_key(contract_id: str, investor_id: str) -> str:
    return f"disp:{contract_id}:{investor_id}"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _bind(contract_id: str) -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)
    else:
        bind_correlation_id(disposition_workflow_id(contract_id))


# --------------------------------------------------------------------------------------------
# Dependency context
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DispositionContext:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    gateway: GraphGateway
    clock: Clock = _utcnow
    slack_client: httpx.AsyncClient | None = None


_context: DispositionContext | None = None


def set_disposition_context(ctx: DispositionContext | None) -> None:
    global _context
    _context = ctx


def get_disposition_context() -> DispositionContext:
    """The active context; derived from the Graph activity context on first use."""
    global _context
    if _context is None:
        g: GraphActivityContext = get_graph_context()
        _context = DispositionContext(
            session_factory=g.session_factory, settings=g.settings, gateway=g.gateway, clock=g.clock
        )
    return _context


# --------------------------------------------------------------------------------------------
# load_contract
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class LoadContractInput:
    contract_id: str


@dataclass(slots=True)
class ContractSnapshot:
    """Plain values for the workflow (all timestamps ISO-8601 so the payload is JSON-safe)."""

    contract_id: str
    status: str
    signed_at: str | None
    option_period_end: str | None
    inspection_deadline: str | None
    closing_date: str | None
    buyer_investor_id: str | None
    deposit_received_at: str | None
    earnest_money_status: str
    deal_dead_reason: str | None
    closed_at: str | None
    raised_alerts: list[str] = field(default_factory=list)

    @property
    def deadlines(self) -> dict[str, str | None]:
        return {
            "option_period_end": self.option_period_end,
            "inspection_deadline": self.inspection_deadline,
            "closing_date": self.closing_date,
        }


async def _snapshot(session: AsyncSession, row: Contract) -> ContractSnapshot:
    raised = [
        str(e.payload.get("alert_key"))
        for e in await AuditRepository(session).for_entity("contract", row.id)
        if e.event_type == ALERT_EVENT and e.payload.get("alert_key")
    ]
    return ContractSnapshot(
        contract_id=str(row.id),
        status=row.status.value,
        signed_at=_iso(row.signed_at),
        option_period_end=_iso(row.option_period_end),
        inspection_deadline=_iso(row.inspection_deadline),
        closing_date=_iso(row.closing_date),
        buyer_investor_id=str(row.buyer_investor_id) if row.buyer_investor_id else None,
        deposit_received_at=_iso(row.deposit_received_at),
        earnest_money_status=row.earnest_money_status.value,
        deal_dead_reason=row.deal_dead_reason,
        closed_at=_iso(row.closed_at),
        raised_alerts=raised,
    )


@activity.defn(name="disposition_load_contract")
async def load_contract(input: LoadContractInput) -> ContractSnapshot:
    _bind(input.contract_id)
    ctx = get_disposition_context()
    try:
        async with ctx.session_factory() as session:
            row = await ContractRepository(session).require(uuid.UUID(input.contract_id))
            return await _snapshot(session, row)
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# run_blast
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RunBlastInput:
    contract_id: str
    limit: int | None = None
    include_fallback: bool = True


@dataclass(slots=True)
class BlastRecipient:
    investor_id: str
    email: str
    tier: int
    message_id: str
    decision: str  # PolicyDecision value
    reason: str
    qa_verdict: str | None
    sent: bool
    created: bool


@dataclass(slots=True)
class BlastResult:
    contract_id: str
    recipients: list[BlastRecipient]
    skipped_suppressed: list[str]
    level: str
    deal_approved: bool

    @property
    def sent(self) -> int:
        return sum(1 for r in self.recipients if r.sent)

    @property
    def queued(self) -> int:
        return sum(1 for r in self.recipients if r.decision == PolicyDecision.QUEUE_FOR_HUMAN.value)

    @property
    def blocked(self) -> int:
        return sum(1 for r in self.recipients if r.decision == PolicyDecision.BLOCKED.value)


def _s6(ctx: DispositionContext) -> S6Disposition:
    return S6Disposition(SenderIdentity.from_settings(ctx.settings))


async def _qa(
    ctx: DispositionContext,
    session: AsyncSession,
    row: Message,
    *,
    facts: DealFacts,
    buyer: BuyerCandidate,
    now: datetime,
) -> Any:
    messages = MessageRepository(session)
    existing = await messages.latest_qa_verdict(row.id)
    if existing is not None:
        return existing
    conv = get_conversation_context()
    agent = S6QaAgent(
        conv.runner,
        session,
        correlation_id=disposition_workflow_id(str(facts.contract_id)),
        prompt_values=conv.prompt_values,
        contract_signed=facts.contract_signed,
    )
    qa = await agent.review(
        QaInput(
            draft=QaDraft(to=list(row.to_addresses), subject=row.subject or "", body=row.body),
            thread=[],
            facts={
                "contract_status": "signed" if facts.contract_signed else "not signed",
                "contract_id": str(facts.contract_id),
                "property_address": facts.property_address,
                "market": facts.market,
                "assignment_price": str(facts.asking_price) if facts.asking_price else None,
                "arv": str(facts.arv) if facts.arv else None,
                "arv_confidence": facts.arv_confidence,
                "repairs": str(facts.repairs) if facts.repairs else None,
                "open_flags": list(facts.open_flags),
                "buyer_name": buyer.name,
                "buyer_selection_reason": buyer.reason,
                "our_mailbox": ctx.settings.graph_mailbox,
            },
            recipient_suppressed=await messages.is_suppressed(buyer.email),
            thread_participant=buyer.email,
            now=now,
            scheduled_sequence=True,  # the blast is a one-shot; no cadence/repeat rules apply
        )
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
    await AuditRepository(session).record(
        actor=Actor.AGENT,
        actor_id="A5",
        event_type=EVENT_BLAST_QA,
        entity_type="message",
        entity_id=row.id,
        payload={
            "contract_id": str(facts.contract_id),
            "investor_id": str(buyer.investor_id),
            "qa_verdict": qa.verdict.value,
            "qa_confidence": qa.confidence,
            "qa_flags": list(qa.flags),
            "under_contract_exemption": facts.contract_signed,
        },
        correlation_id=disposition_workflow_id(str(facts.contract_id)),
        idempotency_key=f"{row.external_message_id}:qa",
    )
    return review


async def _graph_draft(ctx: DispositionContext, session: AsyncSession, row: Message) -> str:
    existing = row.meta.get("graph_draft_id")
    if existing:
        return str(existing)
    info = await ctx.gateway.create_draft(
        OutboundDraft(to=list(row.to_addresses), subject=row.subject or "", body_text=row.body)
    )
    row.meta = {
        **row.meta,
        "graph_draft_id": info.draft_id,
        "internet_message_id": info.internet_message_id,
        "conversation_id": info.conversation_id,
    }
    await session.flush()
    return info.draft_id


async def _send(message_id: uuid.UUID) -> bool:
    result = await send_draft(SendDraftInput(message_id=str(message_id)))
    return result.sent or result.already_sent


@activity.defn(name="disposition_run_blast")
async def run_blast(input: RunBlastInput) -> BlastResult:
    """S6 blast for one signed contract: one draft row per buyer, QA, policy, send or queue."""
    _bind(input.contract_id)
    ctx = get_disposition_context()
    try:
        contract_id = uuid.UUID(input.contract_id)
        to_send: list[uuid.UUID] = []
        recipients: list[BlastRecipient] = []
        skipped: list[str] = []
        level = ""
        async with ctx.session_factory() as session:
            contracts = ContractRepository(session)
            row = await contracts.require(contract_id)
            s6 = _s6(ctx)
            facts = await s6.load_facts(session, row)
            deal_approved = row.status is ContractStatus.SIGNED
            if not deal_approved:
                # BB-09: only approved (signed) deals are described to buyers. Nothing drafted.
                log.warning(
                    "disposition.blast_refused", contract_id=input.contract_id, status=row.status
                )
                return BlastResult(
                    contract_id=input.contract_id,
                    recipients=[],
                    skipped_suppressed=[],
                    level="",
                    deal_approved=False,
                )
            buyers = await s6.select_buyers(
                session,
                row,
                facts=facts,
                include_fallback=input.include_fallback,
                limit=input.limit,
            )
            messages = MessageRepository(session)
            engine = PolicyEngine(session)
            now = ctx.clock()
            tz = ctx.settings.operator_timezone
            workflow_id = disposition_workflow_id(input.contract_id)
            for buyer in buyers:
                if await messages.is_suppressed(buyer.email):
                    skipped.append(str(buyer.investor_id))
                    continue
                key = blast_idempotency_key(input.contract_id, str(buyer.investor_id))
                existing = await messages.get_by_external_id(key)
                if existing is not None and existing.sent_at is not None:
                    recipients.append(
                        BlastRecipient(
                            investor_id=str(buyer.investor_id),
                            email=buyer.email,
                            tier=buyer.tier,
                            message_id=str(existing.id),
                            decision=PolicyDecision.SEND.value,
                            reason="already sent",
                            qa_verdict=None,
                            sent=True,
                            created=False,
                        )
                    )
                    continue
                copy = s6.draft(facts, buyer)
                thread = await messages.get_thread_for_investor(buyer.investor_id)
                if thread is None:
                    thread = await messages.create_thread(
                        investor_id=buyer.investor_id,
                        counterpart_address=buyer.email,
                        subject=copy.subject,
                    )
                draft, created = await messages.create_outbound_draft(
                    thread=thread,
                    to=buyer.email,
                    subject=copy.subject,
                    body=copy.body,
                    policy_action=BLAST_ACTION,
                    idempotency_key=key,
                    from_address=ctx.settings.graph_mailbox,
                    meta={
                        "template_key": copy.template_key,
                        "template_approval_status": copy.approval_status,
                        "contract_id": input.contract_id,
                        "investor_id": str(buyer.investor_id),
                        "tier": buyer.tier,
                        "selection_reason": buyer.reason,
                        "market": facts.market,
                        META_WORKFLOW_ID_KEY: workflow_id,
                    },
                )
                if created:
                    await AuditRepository(session).record(
                        actor=Actor.AGENT,
                        actor_id=AGENT_ACTOR_ID,
                        event_type=EVENT_BLAST_PREPARED,
                        entity_type="message",
                        entity_id=draft.id,
                        payload={
                            "contract_id": input.contract_id,
                            "investor_id": str(buyer.investor_id),
                            "tier": buyer.tier,
                            "reason": buyer.reason,
                        },
                        correlation_id=workflow_id,
                        idempotency_key=f"{key}:prepared",
                    )
                review = await _qa(ctx, session, draft, facts=facts, buyer=buyer, now=now)
                pctx = PolicyContext(
                    qa_verdict=review.verdict,
                    qa_confidence=review.confidence,
                    qa_flags=list(review.flags),
                    recipient_suppressed=False,
                    sends_today=await messages.count_sends_today(BLAST_ACTION, tz, now=now),
                    deal_approved=deal_approved,
                    now=now,
                )
                decision = await engine.decide(BLAST_ACTION, pctx)
                level = decision.level.value
                draft.policy_action = BLAST_ACTION.value
                draft.policy_decision = decision.decision
                draft.meta = {
                    **draft.meta,
                    "policy_reason": decision.reason,
                    "policy_level": decision.level.value,
                    "policy_behaviour": decision.behaviour.value,
                }
                await AuditRepository(session).record(
                    actor=Actor.SYSTEM,
                    actor_id="policy",
                    event_type=EVENT_BLAST_DECIDED,
                    entity_type="message",
                    entity_id=draft.id,
                    payload={
                        "contract_id": input.contract_id,
                        "action": BLAST_ACTION.value,
                        "level": decision.level.value,
                        "decision": decision.decision.value,
                        "reason": decision.reason,
                        "deal_approved": deal_approved,
                        "qa_verdict": review.verdict.value,
                    },
                    correlation_id=workflow_id,
                    idempotency_key=f"{key}:decided",
                )
                if decision.sends:
                    await _graph_draft(ctx, session, draft)
                    to_send.append(draft.id)
                recipients.append(
                    BlastRecipient(
                        investor_id=str(buyer.investor_id),
                        email=buyer.email,
                        tier=buyer.tier,
                        message_id=str(draft.id),
                        decision=decision.decision.value,
                        reason=decision.reason,
                        qa_verdict=review.verdict.value,
                        sent=False,
                        created=created,
                    )
                )
            await session.commit()

        for message_id in to_send:
            ok = await _send(message_id)
            for r in recipients:
                if r.message_id == str(message_id):
                    r.sent = ok
        result = BlastResult(
            contract_id=input.contract_id,
            recipients=recipients,
            skipped_suppressed=skipped,
            level=level,
            deal_approved=True,
        )
        log.info(
            "disposition.blast_done",
            contract_id=input.contract_id,
            recipients=len(recipients),
            sent=result.sent,
            queued=result.queued,
            blocked=result.blocked,
            suppressed=len(skipped),
        )
        return result
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# send_approved_blast (human decision from the review queue)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SendApprovedBlastInput:
    message_id: str
    actor_id: str | None = None
    edited_body: str | None = None


@dataclass(slots=True)
class SendApprovedBlastResult:
    message_id: str
    sent: bool
    blocked_reason: str | None = None


@activity.defn(name="disposition_send_approved_blast")
async def send_approved_blast(input: SendApprovedBlastInput) -> SendApprovedBlastResult:
    """A human approved a queued blast draft: keep the footer, honour suppression, send."""
    ctx = get_disposition_context()
    message_id = uuid.UUID(input.message_id)
    try:
        async with ctx.session_factory() as session:
            messages = MessageRepository(session)
            row = await messages.get(message_id)
            if row is None:
                raise LookupError(f"message {input.message_id} not found")
            _bind(str(row.meta.get("contract_id", "")))
            if row.sent_at is not None:
                return SendApprovedBlastResult(message_id=input.message_id, sent=True)
            to = row.to_addresses[0] if row.to_addresses else ""
            if not to or await messages.is_suppressed(to):
                row.policy_decision = PolicyDecision.BLOCKED
                row.meta = {**row.meta, "policy_reason": "recipient is suppressed"}
                await session.commit()
                return SendApprovedBlastResult(
                    message_id=input.message_id,
                    sent=False,
                    blocked_reason="recipient is suppressed",
                )
            if input.edited_body and input.edited_body.strip():
                body = input.edited_body
                foot = footer(SenderIdentity.from_settings(ctx.settings))
                if foot not in body:
                    body = body.rstrip() + "\n\n" + foot
                if body != row.body:
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
                event_type=EVENT_BLAST_HUMAN_APPROVED,
                entity_type="message",
                entity_id=row.id,
                payload={"contract_id": row.meta.get("contract_id")},
                correlation_id=str(row.meta.get(META_WORKFLOW_ID_KEY) or ""),
                idempotency_key=f"{row.external_message_id}:approved",
            )
            await _graph_draft(ctx, session, row)
            await session.commit()
        sent = await _send(message_id)
        return SendApprovedBlastResult(message_id=input.message_id, sent=sent)
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# record_buyer_response
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RecordBuyerResponseInput:
    contract_id: str
    investor_id: str
    response: str  # "interested" | "pass"
    proof_of_funds: bool = False
    deposit_terms: str | None = None
    actor: str = Actor.AGENT.value
    actor_id: str | None = None


@dataclass(slots=True)
class RecordBuyerResponseResult:
    contract_id: str
    investor_id: str
    response: str
    interested_count: int


@activity.defn(name="disposition_record_buyer_response")
async def record_buyer_response(input: RecordBuyerResponseInput) -> RecordBuyerResponseResult:
    """Interested / pass (+ proof of funds, deposit terms) from a buyer. Any actor may call it;
    it never selects the buyer - that stays a human click on /deals."""
    _bind(input.contract_id)
    ctx = get_disposition_context()
    try:
        async with ctx.session_factory() as session:
            row = await ContractRepository(session).record_buyer_response(
                uuid.UUID(input.contract_id),
                investor_id=uuid.UUID(input.investor_id),
                response=BuyerResponse(input.response),
                proof_of_funds=input.proof_of_funds,
                deposit_terms=input.deposit_terms,
                actor=Actor(input.actor),
                actor_id=input.actor_id,
                at=ctx.clock(),
            )
            await session.commit()
            interested = sum(
                1
                for v in row.buyer_responses.values()
                if v.get("response") == BuyerResponse.INTERESTED.value
            )
            return RecordBuyerResponseResult(
                contract_id=input.contract_id,
                investor_id=input.investor_id,
                response=input.response,
                interested_count=interested,
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# raise_deadline_alert
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DeadlineAlertInput:
    contract_id: str
    deadline: str  # option_period_end | inspection_deadline | closing_date
    label: str  # T-3d | T-1d | T-0
    alert_key: str  # "{deadline}:{label}"
    deadline_at: str  # ISO-8601


@dataclass(slots=True)
class DeadlineAlertResult:
    created: bool
    notified: list[str]


async def _notify_slack(ctx: DispositionContext, text: str) -> bool:
    url = ctx.settings.slack_webhook_url
    if url is None or not url.get_secret_value().strip():
        return False
    payload = {"text": text}
    try:
        if ctx.slack_client is not None:
            response = await ctx.slack_client.post(
                url.get_secret_value(), json=payload, timeout=SLACK_TIMEOUT_SECONDS
            )
        else:
            async with httpx.AsyncClient(timeout=SLACK_TIMEOUT_SECONDS) as client:
                response = await client.post(url.get_secret_value(), json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:  # the audit row is the record; Slack is best effort
        log.warning("disposition.slack_failed", error=str(exc)[:200])
        return False
    return True


@activity.defn(name="disposition_raise_deadline_alert")
async def raise_deadline_alert(input: DeadlineAlertInput) -> DeadlineAlertResult:
    """``alert.raised`` on the contract (idempotent per alert key) + log + Slack if configured."""
    _bind(input.contract_id)
    ctx = get_disposition_context()
    try:
        async with ctx.session_factory() as session:
            row = await ContractRepository(session).require(uuid.UUID(input.contract_id))
            address = contract_address(row)
            detail = (
                f"{input.label} {input.deadline.replace('_', ' ')} for {address} "
                f"(due {input.deadline_at}); status {row.status.value}, "
                f"earnest money {row.earnest_money_status.value}"
            )
            _, created = await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="disposition",
                event_type=ALERT_EVENT,
                entity_type="contract",
                entity_id=row.id,
                payload={
                    "kind": "deal_deadline",
                    "detail": detail,
                    "alert_key": input.alert_key,
                    "deadline": input.deadline,
                    "label": input.label,
                    "deadline_at": input.deadline_at,
                    "contract_id": input.contract_id,
                },
                correlation_id=disposition_workflow_id(input.contract_id),
                idempotency_key=f"disp:{input.contract_id}:alert:{input.alert_key}",
            )
            await session.commit()
        notified = ["log"]
        log.warning("alert", kind="deal_deadline", detail=detail, contract_id=input.contract_id)
        if created and await _notify_slack(ctx, f"Deal deadline: {detail}"):
            notified.append("slack")
        return DeadlineAlertResult(created=created, notified=notified)
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# record_workflow_state / deal_dead_followup
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class WorkflowStateInput:
    contract_id: str
    state: str
    next_alert_key: str | None = None
    next_alert_at: str | None = None
    blast_sent: int | None = None
    blast_queued: int | None = None


@activity.defn(name="disposition_record_workflow_state")
async def record_workflow_state(input: WorkflowStateInput) -> bool:
    """Mirror the workflow's state onto ``contracts.meta`` so /deals can show "next alert"."""
    _bind(input.contract_id)
    ctx = get_disposition_context()
    try:
        async with ctx.session_factory() as session:
            row = await ContractRepository(session).require(uuid.UUID(input.contract_id))
            wf: dict[str, Any] = {
                "state": input.state,
                "next_alert_key": input.next_alert_key,
                "next_alert_at": input.next_alert_at,
                "updated_at": ctx.clock().isoformat(),
            }
            if input.blast_sent is not None:
                wf["blast_sent"] = input.blast_sent
            if input.blast_queued is not None:
                wf["blast_queued"] = input.blast_queued
            row.meta = {**row.meta, "workflow": {**dict(row.meta.get("workflow") or {}), **wf}}
            await session.commit()
            return True
    finally:
        clear_context()


@dataclass(slots=True)
class DealDeadFollowupInput:
    contract_id: str
    reason: str


@dataclass(slots=True)
class DealDeadFollowupResult:
    created: bool
    earnest_money_status: str
    release_reminder: bool


@activity.defn(name="disposition_deal_dead_followup")
async def deal_dead_followup(input: DealDeadFollowupInput) -> DealDeadFollowupResult:
    """After a human marked the deal dead: audit the follow-up and, when earnest money is on
    deposit, raise the release reminder. The release itself is a human action (POL-03j)."""
    _bind(input.contract_id)
    ctx = get_disposition_context()
    try:
        async with ctx.session_factory() as session:
            row = await ContractRepository(session).require(uuid.UUID(input.contract_id))
            remind = row.earnest_money_status is EarnestMoneyStatus.DEPOSITED
            _, created = await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="disposition",
                event_type=EVENT_DEAL_DEAD_FOLLOWUP,
                entity_type="contract",
                entity_id=row.id,
                payload={
                    "reason": input.reason,
                    "timers_cancelled": True,
                    "earnest_money_status": row.earnest_money_status.value,
                    "release_earnest_money_reminder": remind,
                },
                correlation_id=disposition_workflow_id(input.contract_id),
                idempotency_key=f"disp:{input.contract_id}:dead_followup",
            )
            if remind and created:
                await AuditRepository(session).record(
                    actor=Actor.SYSTEM,
                    actor_id="disposition",
                    event_type=ALERT_EVENT,
                    entity_type="contract",
                    entity_id=row.id,
                    payload={
                        "kind": "earnest_money_release",
                        "detail": (
                            f"Deal dead ({input.reason}): earnest money "
                            f"{row.earnest_money_amount} is on deposit - a human must request "
                            "its release (POL-03j)"
                        ),
                        "alert_key": "earnest_money:release",
                        "contract_id": input.contract_id,
                    },
                    correlation_id=disposition_workflow_id(input.contract_id),
                    idempotency_key=f"disp:{input.contract_id}:alert:earnest_money:release",
                )
                await _notify_slack(
                    ctx, f"Deal dead ({contract_address(row)}): release earnest money (human)"
                )
            await session.commit()
            return DealDeadFollowupResult(
                created=created,
                earnest_money_status=row.earnest_money_status.value,
                release_reminder=remind,
            )
    finally:
        clear_context()


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "AGENT_ACTOR_ID",
    "BLAST_ACTION",
    "EVENT_BLAST_DECIDED",
    "EVENT_BLAST_HUMAN_APPROVED",
    "EVENT_BLAST_PREPARED",
    "EVENT_BLAST_QA",
    "EVENT_DEAL_DEAD_FOLLOWUP",
    "WORKFLOW_ID_PREFIX",
    "BlastRecipient",
    "BlastResult",
    "ContractSnapshot",
    "DeadlineAlertInput",
    "DeadlineAlertResult",
    "DealDeadFollowupInput",
    "DealDeadFollowupResult",
    "DispositionContext",
    "LoadContractInput",
    "RecordBuyerResponseInput",
    "RecordBuyerResponseResult",
    "RunBlastInput",
    "SendApprovedBlastInput",
    "SendApprovedBlastResult",
    "WorkflowStateInput",
    "blast_idempotency_key",
    "deal_dead_followup",
    "disposition_workflow_id",
    "get_disposition_context",
    "load_contract",
    "raise_deadline_alert",
    "record_buyer_response",
    "record_workflow_state",
    "run_blast",
    "send_approved_blast",
    "set_disposition_context",
]
