"""Inbound conversation activities: A4 classify -> buy box -> A4 draft -> A5 QA -> policy.

REQUIREMENTS.md INV-13..18, INV-21, INV-24, WF-19, WF-21, POL-03c, POL-03l, XC-01, XC-04.

Four activities, each idempotent per ``message_id`` (a retry or a duplicate notification is a
no-op; the first thing every activity does is look for the rows a previous run left):

- ``classify_inbound``             A4 classification on the ``messages`` row; opt-out ->
                                   suppression (auto at every level, POL-03l); signals the
                                   investor sequence (``reply`` / ``opt_out``)
- ``extract_and_upsert_buy_box``   A4 extraction -> ``BuyBoxRepository.create_version`` per
                                   stated buy box (replay-safe by ``source_message_id``);
                                   audit ``buy_box.created`` / ``buy_box.updated`` for D1
- ``draft_and_qa_reply``           A4 draft -> A5 QA -> ``PolicyEngine.decide(INVESTOR_REPLY)``
                                   -> send (same conversation) / queue for human / blocked
- ``apply_human_decision``         the review queue's verdict on a queued draft: send (with an
                                   optional edited body) or discard
- ``notify_bounce``                signals ``bounce`` to the investor sequence behind an NDR

Graph calls go through the ``activities.graph`` functions (``create_draft`` / ``send_draft``)
so drafting and sending keep their own idempotency contract and their own session; this module
never holds a DB transaction open across a Graph call.

Sequence signalling contract (A3 workflow): signal names ``reply``, ``opt_out``, ``bounce``;
payloads ``ReplySignal``, ``OptOutSignal``, ``BounceSignal`` from ``workflows/signals.py``
(the one module both sides import, so the handler signatures and the payloads cannot drift);
target workflow id = ``sequences.temporal_workflow_id`` of the active investor sequence on the
thread (fallback: the investor's active sequence). Signals are at-least-once: an activity retry
after a crash may repeat one, so the sequence workflow must treat duplicates as no-ops.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from collections.abc import Sequence as Seq
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.activities.graph import (
    CreateDraftInput,
    SendDraftInput,
    create_draft,
    get_graph_context,
    send_draft,
)
from wholesaling.agents.a4 import (
    MODEL_OVERRIDES,
    TRACKED_BUY_BOX_FIELDS,
    A4Classification,
    A4ReplyAgent,
    ReplyDraft,
    ThreadMessage,
    tracked_unknowns,
)
from wholesaling.agents.a5 import A5QaAgent, QaDraft, QaInput, QaResult
from wholesaling.agents.llm.factory import build_runner
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.enums import (
    Actor,
    Channel,
    ContactChannel,
    Direction,
    MessageClassification,
    PolicyDecision,
    QaVerdict,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.messaging import Message, Sequence, Suppression, Thread
from wholesaling.db.normalize import normalize_email
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria, BuyBoxRepository
from wholesaling.db.repositories.qa_reviews import QaReviewRepository
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

SUPPRESSION_REASON_OPT_OUT = "opt_out"
REPLY_POLICY_ACTION = PolicyAction.INVESTOR_REPLY.value


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def reply_idempotency_key(inbound_message_id: str) -> str:
    """``messages.external_message_id`` of the reply drafted for one inbound message."""
    return f"reply:{inbound_message_id}"


# --------------------------------------------------------------------------------------------
# Dependency context
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ConversationContext:
    session_factory: async_sessionmaker[AsyncSession]
    runner: AgentRunner
    temporal: TemporalGateway
    settings: Settings
    clock: Clock = _utcnow
    # Rendered into the A4/A5 system prompts (``{company}``); the operator's identity.
    prompt_values: dict[str, Any] = field(default_factory=lambda: {"company": "our team"})


_context: ConversationContext | None = None


def set_conversation_context(ctx: ConversationContext | None) -> None:
    """Install (or clear) the dependency context. Tests inject the mock runner and gateway."""
    global _context
    _context = ctx


def get_conversation_context() -> ConversationContext:
    """The active context. Built on first use from the Graph activity context (same session
    factory, settings and clock) plus the settings-driven model runner and Temporal gateway."""
    global _context
    if _context is None:
        graph_ctx = get_graph_context()
        settings = graph_ctx.settings
        _context = ConversationContext(
            session_factory=graph_ctx.session_factory,
            runner=build_runner(settings, overrides=MODEL_OVERRIDES),
            temporal=get_temporal(settings),
            settings=settings,
            clock=graph_ctx.clock,
        )
    return _context


def _bind() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


def _correlation_id() -> str | None:
    return activity.info().workflow_id if activity.in_activity() else None


# --------------------------------------------------------------------------------------------
# Shared loaders
# --------------------------------------------------------------------------------------------


async def _load_message(session: AsyncSession, message_id: str) -> Message:
    row = await session.get(Message, uuid.UUID(message_id))
    if row is None:
        msg = f"message {message_id} not found"
        raise ValueError(msg)
    return row


async def _thread_rows(session: AsyncSession, thread_id: uuid.UUID) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.thread_id == thread_id)
        .order_by(
            func.coalesce(Message.received_at, Message.sent_at, Message.created_at), Message.id
        )
    )
    return list((await session.execute(stmt)).scalars().all())


def _to_thread_messages(rows: Seq[Message], latest: Message) -> list[ThreadMessage]:
    out: list[ThreadMessage] = []
    for r in rows:
        if r.direction == Direction.OUT and r.draft and r.sent_at is None:
            continue  # unsent drafts are not part of the conversation the investor saw
        out.append(
            ThreadMessage(
                direction=r.direction.value,
                body=r.body,
                from_address=r.from_address,
                subject=r.subject,
                at=_aware(r.received_at or r.sent_at or r.created_at),
                headers=dict((r.meta or {}).get("headers") or {}),
                is_latest_inbound=(r.id == latest.id),
            )
        )
    if not any(m.is_latest_inbound for m in out):
        out.append(
            ThreadMessage(
                direction="in",
                body=latest.body,
                from_address=latest.from_address,
                subject=latest.subject,
                at=_aware(latest.received_at or latest.created_at),
                headers=dict((latest.meta or {}).get("headers") or {}),
                is_latest_inbound=True,
            )
        )
    return out


async def _resolve_investor(
    session: AsyncSession, thread: Thread, from_address: str | None
) -> Investor | None:
    """The investor behind the thread; links ``threads.investor_id`` from the sender's contact
    when the thread was created by an inbound message we had not matched yet."""
    if thread.investor_id is not None:
        return await session.get(Investor, thread.investor_id)
    if not from_address:
        return None
    try:
        normalized = normalize_email(from_address)
    except ValueError:
        return None
    stmt = select(Contact).where(
        Contact.channel == ContactChannel.EMAIL, Contact.normalized_value == normalized
    )
    contact = (await session.execute(stmt)).scalars().first()
    if contact is None:
        return None
    thread.investor_id = contact.investor_id
    await session.flush()
    return await session.get(Investor, contact.investor_id)


async def _active_sequence(
    session: AsyncSession, thread: Thread, investor_id: uuid.UUID | None
) -> Sequence | None:
    conditions = [Sequence.thread_id == thread.id]
    if investor_id is not None:
        conditions.append(Sequence.investor_id == investor_id)
    stmt = (
        select(Sequence)
        .where(
            Sequence.kind == SequenceKind.INVESTOR,
            Sequence.status.in_([SequenceStatus.ACTIVE, SequenceStatus.PAUSED]),
            or_(*conditions),
        )
        .order_by(Sequence.created_at.desc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for r in rows:  # prefer the sequence on this very thread
        if r.thread_id == thread.id:
            return r
    return rows[0] if rows else None


async def _is_suppressed(session: AsyncSession, address: str | None) -> bool:
    if not address:
        return False
    try:
        normalized = normalize_email(address)
    except ValueError:
        return False
    stmt = select(Suppression).where(
        Suppression.channel == Channel.EMAIL, Suppression.normalized_value == normalized
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _find_reply_row(session: AsyncSession, inbound_message_id: str) -> Message | None:
    stmt = select(Message).where(
        Message.channel == Channel.EMAIL,
        Message.external_message_id == reply_idempotency_key(inbound_message_id),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _signal_sequence(
    ctx: ConversationContext,
    session: AsyncSession,
    *,
    sequence: Sequence | None,
    signal: str,
    payload: Any,
    message: Message,
    idempotency_key: str,
) -> str | None:
    """Signal the sequence workflow once per (message, signal); returns the workflow id."""
    if sequence is None:
        return None
    audit = AuditRepository(session)
    if await audit.get_by_idempotency_key(idempotency_key) is not None:
        return sequence.temporal_workflow_id  # already signalled by an earlier attempt
    await ctx.temporal.signal(sequence.temporal_workflow_id, signal, payload)
    await audit.record(
        actor=Actor.AGENT,
        actor_id="A4",
        event_type="sequence.signalled",
        entity_type="sequence",
        entity_id=sequence.id,
        payload={
            "signal": signal,
            "workflow_id": sequence.temporal_workflow_id,
            "message_id": str(message.id),
        },
        correlation_id=_correlation_id(),
        idempotency_key=idempotency_key,
    )
    return sequence.temporal_workflow_id


def _investor_facts(
    investor: Investor | None, thread: Thread, mailbox: str | None
) -> dict[str, Any]:
    facts: dict[str, Any] = {"our_mailbox": mailbox, "thread_subject": thread.subject}
    if investor is not None:
        facts.update(
            {
                "investor_name": investor.canonical_name,
                "first_name": investor.first_name,
                "investor_type": investor.investor_type.value,
            }
        )
    return facts


# --------------------------------------------------------------------------------------------
# classify_inbound
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ClassifyInboundInput:
    message_id: str


@dataclass(slots=True)
class ClassifyInboundResult:
    message_id: str
    classification: str | None = None
    confidence: float | None = None
    is_auto_reply: bool = False
    reconnect_date: str | None = None
    quoted_text_stripped: bool = False
    already_classified: bool = False
    skipped_reason: str | None = None  # "outbound" | "bounce"
    suppression_created: bool = False
    signalled: str | None = None  # "reply" | "opt_out"
    sequence_workflow_id: str | None = None
    investor_id: str | None = None

    @property
    def classification_enum(self) -> MessageClassification | None:
        return MessageClassification(self.classification) if self.classification else None


@activity.defn(name="conversation_classify_inbound")
async def classify_inbound(input: ClassifyInboundInput) -> ClassifyInboundResult:
    """A4 classification of one inbound message, stored on the row (INV-13).

    Opt-outs add a ``suppression`` row immediately, at every autonomy level (POL-03l, WF-21).
    Substantive replies (every class but ``unrelated``; auto-replies never) signal the
    investor sequence so it cancels its remaining sends (INV-10, WF-19).
    """
    _bind()
    ctx = get_conversation_context()
    try:
        async with ctx.session_factory() as session:
            message = await _load_message(session, input.message_id)
            if message.direction != Direction.IN:
                return ClassifyInboundResult(message_id=input.message_id, skipped_reason="outbound")
            if (message.meta or {}).get("is_bounce"):
                return ClassifyInboundResult(message_id=input.message_id, skipped_reason="bounce")
            thread = await session.get(Thread, message.thread_id)
            if thread is None:  # pragma: no cover - FK guarantees it
                msg = f"thread {message.thread_id} missing"
                raise ValueError(msg)
            investor = await _resolve_investor(session, thread, message.from_address)

            stored = (message.meta or {}).get("a4")
            if message.classification is not None and stored:
                await session.commit()
                return ClassifyInboundResult(
                    message_id=input.message_id,
                    classification=message.classification.value,
                    confidence=message.classification_confidence,
                    is_auto_reply=bool(stored.get("is_auto_reply")),
                    reconnect_date=stored.get("reconnect_date"),
                    quoted_text_stripped=bool(stored.get("quoted_text_stripped")),
                    already_classified=True,
                    suppression_created=False,
                    signalled=stored.get("signalled"),
                    sequence_workflow_id=stored.get("sequence_workflow_id"),
                    investor_id=str(investor.id) if investor else None,
                )

            rows = await _thread_rows(session, thread.id)
            thread_messages = _to_thread_messages(rows, message)
            agent = A4ReplyAgent(
                ctx.runner,
                session,
                correlation_id=_correlation_id() or str(message.id),
                prompt_values=ctx.prompt_values,
            )
            result: A4Classification = await agent.classify(thread_messages)

            message.classification = result.classification
            message.classification_confidence = result.confidence
            audit = AuditRepository(session)
            await audit.record(
                actor=Actor.AGENT,
                actor_id="A4",
                event_type="message.classified",
                entity_type="message",
                entity_id=message.id,
                payload={**result.to_json(), "investor_id": str(investor.id) if investor else None},
                correlation_id=_correlation_id(),
                idempotency_key=f"a4:{message.id}:classified",
            )

            suppression_created = False
            if result.classification is MessageClassification.OPT_OUT:
                suppression_created = await _suppress_opt_out(session, audit, message)

            sequence = await _active_sequence(session, thread, investor.id if investor else None)
            signalled: str | None = None
            workflow_id: str | None = None
            if result.classification is MessageClassification.OPT_OUT:
                signalled = SIGNAL_OPT_OUT
                workflow_id = await _signal_sequence(
                    ctx,
                    session,
                    sequence=sequence,
                    signal=SIGNAL_OPT_OUT,
                    payload=OptOutSignal(message_id=str(message.id)),
                    message=message,
                    idempotency_key=f"a4:{message.id}:signal:{SIGNAL_OPT_OUT}",
                )
            elif result.is_substantive:
                signalled = SIGNAL_REPLY
                workflow_id = await _signal_sequence(
                    ctx,
                    session,
                    sequence=sequence,
                    signal=SIGNAL_REPLY,
                    payload=ReplySignal(
                        message_id=str(message.id), classification=result.classification.value
                    ),
                    message=message,
                    idempotency_key=f"a4:{message.id}:signal:{SIGNAL_REPLY}",
                )
            if workflow_id is None:
                signalled = None

            message.meta = {
                **(message.meta or {}),
                "a4": {
                    **result.to_json(),
                    "signalled": signalled,
                    "sequence_workflow_id": workflow_id,
                },
            }
            await session.commit()
            log.info(
                "conversation.classified",
                message_id=str(message.id),
                classification=result.classification.value,
                confidence=result.confidence,
                is_auto_reply=result.is_auto_reply,
                suppression_created=suppression_created,
                signalled=signalled,
                workflow_id=workflow_id,
            )
            return ClassifyInboundResult(
                message_id=input.message_id,
                classification=result.classification.value,
                confidence=result.confidence,
                is_auto_reply=result.is_auto_reply,
                reconnect_date=result.reconnect_date.isoformat() if result.reconnect_date else None,
                quoted_text_stripped=result.quoted_text_stripped,
                suppression_created=suppression_created,
                signalled=signalled,
                sequence_workflow_id=workflow_id,
                investor_id=str(investor.id) if investor else None,
            )
    finally:
        clear_context()


async def _suppress_opt_out(
    session: AsyncSession, audit: AuditRepository, message: Message
) -> bool:
    if not message.from_address:
        return False
    try:
        normalized = normalize_email(message.from_address)
    except ValueError:
        log.warning("conversation.opt_out_address_invalid", address=message.from_address)
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
        actor_id="A4",
        event_type="suppression.added",
        entity_type="suppression",
        payload={
            "address": normalized,
            "reason": SUPPRESSION_REASON_OPT_OUT,
            "policy_action": PolicyAction.OPT_OUT_SUPPRESSION.value,
            "created": created,
        },
        correlation_id=str(message.id),
        idempotency_key=f"a4:{message.id}:opt_out",
    )
    if created:
        log.warning(
            "conversation.opt_out_suppressed", address=normalized, message_id=str(message.id)
        )
    return created


# --------------------------------------------------------------------------------------------
# extract_and_upsert_buy_box
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ExtractBuyBoxInput:
    message_id: str


@dataclass(slots=True)
class BuyBoxVersionInfo:
    buy_box_id: str
    buy_box_key: str
    version: int
    created: bool
    reason: str  # created | replay | duplicate
    changed_fields: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    corrections: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ExtractBuyBoxResult:
    message_id: str
    investor_id: str | None = None
    versions: list[BuyBoxVersionInfo] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    skipped_reason: str | None = None  # "no_investor" | "nothing_stated" | "replay"

    @property
    def created_any(self) -> bool:
        return any(v.created for v in self.versions)


def _version_info(
    buy_box: BuyBox,
    *,
    created: bool,
    reason: str,
    changed_fields: Seq[str] = (),
    corrections: Seq[dict[str, Any]] = (),
) -> BuyBoxVersionInfo:
    return BuyBoxVersionInfo(
        buy_box_id=str(buy_box.id),
        buy_box_key=buy_box.buy_box_key,
        version=buy_box.version,
        created=created,
        reason=reason,
        changed_fields=list(changed_fields),
        unknowns=list(buy_box.unknown_fields),
        corrections=list(corrections),
    )


@activity.defn(name="conversation_extract_buy_box")
async def extract_and_upsert_buy_box(input: ExtractBuyBoxInput) -> ExtractBuyBoxResult:
    """A4 extraction -> one versioned upsert per stated buy box (INV-14, INV-21, BB-02/03).

    Replaying the same message is a no-op: the previous versions are returned from
    ``buy_boxes.source_message_id`` and nothing is written. Each new version emits one
    ``buy_box.created`` (version 1) or ``buy_box.updated`` audit event, which BO-08 / D1 consume.
    """
    _bind()
    ctx = get_conversation_context()
    try:
        async with ctx.session_factory() as session:
            message = await _load_message(session, input.message_id)
            thread = await session.get(Thread, message.thread_id)
            if thread is None:  # pragma: no cover - FK guarantees it
                msg = f"thread {message.thread_id} missing"
                raise ValueError(msg)
            investor = await _resolve_investor(session, thread, message.from_address)
            if investor is None:
                await session.commit()
                return ExtractBuyBoxResult(
                    message_id=input.message_id, skipped_reason="no_investor"
                )

            repo = BuyBoxRepository(session)
            previous = await repo.list_by_source_message(message.id)
            if previous:
                stored = (message.meta or {}).get("buy_box_extraction") or {}
                return ExtractBuyBoxResult(
                    message_id=input.message_id,
                    investor_id=str(investor.id),
                    versions=[_version_info(b, created=False, reason="replay") for b in previous],
                    unknowns=list(stored.get("unknowns") or []),
                    corrections=list(stored.get("corrections") or []),
                    skipped_reason="replay",
                )
            stored_meta = (message.meta or {}).get("buy_box_extraction")
            if stored_meta is not None and not stored_meta.get("buy_boxes"):
                return ExtractBuyBoxResult(
                    message_id=input.message_id,
                    investor_id=str(investor.id),
                    skipped_reason="nothing_stated",
                )

            current_rows = await repo.list_current(investor.id)
            current = {b.buy_box_key: BuyBoxCriteria.from_row(b) for b in current_rows}
            rows = await _thread_rows(session, thread.id)
            agent = A4ReplyAgent(
                ctx.runner,
                session,
                correlation_id=_correlation_id() or str(message.id),
                prompt_values=ctx.prompt_values,
            )
            extraction = await agent.extract_buy_box(_to_thread_messages(rows, message), current)

            audit = AuditRepository(session)
            versions: list[BuyBoxVersionInfo] = []
            for box in extraction.buy_boxes:
                note = (
                    "correction: " + ", ".join(c.field for c in box.corrections)
                    if box.corrections
                    else f"extracted from message {message.id}"
                )
                result = await repo.create_version(
                    investor.id,
                    box.criteria,
                    buy_box_key=box.buy_box_key,
                    source_message_id=message.id,
                    source_ref=f"message:{message.id}",
                    change_note=note[:255],
                    mode="merge",
                )
                corrections = [c.to_json() for c in box.corrections]
                versions.append(
                    _version_info(
                        result.buy_box,
                        created=result.created,
                        reason=result.reason,
                        changed_fields=result.changed_fields,
                        corrections=corrections,
                    )
                )
                if result.created:
                    event = "buy_box.created" if result.buy_box.version == 1 else "buy_box.updated"
                    await audit.record(
                        actor=Actor.AGENT,
                        actor_id="A4",
                        event_type=event,
                        entity_type="buy_box",
                        entity_id=result.buy_box.id,
                        payload={
                            "investor_id": str(investor.id),
                            "buy_box_key": result.buy_box.buy_box_key,
                            "version": result.buy_box.version,
                            "changed_fields": list(result.changed_fields),
                            "corrections": corrections,
                            "unknown_fields": list(result.buy_box.unknown_fields),
                            "source_message_id": str(message.id),
                            "superseded_version": (
                                result.superseded.version if result.superseded else None
                            ),
                        },
                        correlation_id=_correlation_id(),
                        idempotency_key=f"a4:{message.id}:buybox:{result.buy_box.buy_box_key}",
                    )
            message.meta = {**(message.meta or {}), "buy_box_extraction": extraction.to_json()}
            await session.commit()
            log.info(
                "conversation.buy_box_extracted",
                message_id=str(message.id),
                investor_id=str(investor.id),
                versions=[(v.buy_box_key, v.version, v.reason) for v in versions],
                unknowns=extraction.unknowns,
                corrections=len(extraction.corrections),
            )
            return ExtractBuyBoxResult(
                message_id=input.message_id,
                investor_id=str(investor.id),
                versions=versions,
                unknowns=list(extraction.unknowns),
                corrections=[c.to_json() for c in extraction.corrections],
                skipped_reason=None if extraction.buy_boxes else "nothing_stated",
            )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# draft_and_qa_reply
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DraftReplyInput:
    message_id: str
    #: The ``GraphInboundMessageWorkflow`` id that owns this inbound message, passed by the
    #: workflow (``workflow.info().workflow_id``). Inside a Temporal activity the id is also
    #: available from ``activity.info()``; outside one (tests, the synthetic pilot's simulator)
    #: this is the only way the review route can find the workflow to signal (pilot GAP-05).
    workflow_id: str | None = None


@dataclass(slots=True)
class DraftReplyResult:
    message_id: str
    draft_message_id: str | None = None
    subject: str | None = None
    body: str | None = None
    asks_for: list[str] = field(default_factory=list)
    contains_sensitive: bool = False
    qa_verdict: str | None = None
    qa_confidence: float | None = None
    qa_flags: list[str] = field(default_factory=list)
    qa_review_id: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    policy_level: str | None = None
    sent: bool = False
    already_processed: bool = False

    @property
    def queued(self) -> bool:
        return self.policy_decision == PolicyDecision.QUEUE_FOR_HUMAN.value

    @property
    def blocked(self) -> bool:
        return self.policy_decision == PolicyDecision.BLOCKED.value


async def _sends_today(session: AsyncSession, now: datetime) -> int:
    start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    stmt = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.OUT,
            Message.policy_action == REPLY_POLICY_ACTION,
            Message.sent_at.is_not(None),
            Message.sent_at >= start,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


def _last_outbound(rows: Seq[Message]) -> Message | None:
    sent = [r for r in rows if r.direction == Direction.OUT and r.sent_at is not None]
    return sent[-1] if sent else None


def _result_from_existing(inbound: Message, draft_row: Message) -> DraftReplyResult:
    stored = (inbound.meta or {}).get("reply") or {}
    qa = stored.get("qa") or {}
    return DraftReplyResult(
        message_id=str(inbound.id),
        draft_message_id=str(draft_row.id),
        subject=draft_row.subject,
        body=draft_row.body,
        asks_for=list((draft_row.meta or {}).get("asks_for") or []),
        contains_sensitive=bool((draft_row.meta or {}).get("contains_sensitive")),
        qa_verdict=qa.get("verdict"),
        qa_confidence=qa.get("confidence"),
        qa_flags=list(qa.get("flags") or []),
        qa_review_id=str(draft_row.qa_review_id) if draft_row.qa_review_id else None,
        policy_decision=draft_row.policy_decision.value if draft_row.policy_decision else None,
        policy_reason=stored.get("policy_reason"),
        policy_level=stored.get("policy_level"),
        sent=draft_row.sent_at is not None,
        already_processed=True,
    )


@activity.defn(name="conversation_draft_and_qa_reply")
async def draft_and_qa_reply(input: DraftReplyInput) -> DraftReplyResult:
    """A4 draft -> A5 QA -> policy -> send / queue / block, all on the same code path (XC-01).

    Phase 1 (one session): load thread, draft, QA, decide, commit (agent runs are logged even
    if a later phase fails). Phase 2: the Graph draft through ``create_draft`` (its own
    idempotency key ``reply:<message_id>``) unless the decision is ``blocked``, in which case a
    local ``messages`` row records the blocked draft and nothing reaches the mailbox. Phase 3:
    QA review row linked to the draft row, policy stamped. Phase 4: ``send_draft`` on ``send``.
    """
    _bind()
    ctx = get_conversation_context()
    try:
        async with ctx.session_factory() as session:
            inbound = await _load_message(session, input.message_id)
            existing = await _find_reply_row(session, input.message_id)
            if existing is not None:
                return _result_from_existing(inbound, existing)
            if inbound.direction != Direction.IN:
                msg = f"message {input.message_id} is not inbound"
                raise ValueError(msg)
            thread = await session.get(Thread, inbound.thread_id)
            if thread is None:  # pragma: no cover
                msg = f"thread {inbound.thread_id} missing"
                raise ValueError(msg)
            investor = await _resolve_investor(session, thread, inbound.from_address)
            rows = await _thread_rows(session, thread.id)
            thread_messages = _to_thread_messages(rows, inbound)
            classification = _classification_from_row(inbound)

            current_box: BuyBox | None = None
            if investor is not None:
                current_rows = await BuyBoxRepository(session).list_current(investor.id)
                current_box = current_rows[0] if current_rows else None
            criteria = BuyBoxCriteria.from_row(current_box) if current_box else None
            missing = (
                tracked_unknowns(criteria) if criteria is not None else list(TRACKED_BUY_BOX_FIELDS)
            )
            facts = _investor_facts(investor, thread, ctx.settings.graph_mailbox)
            correlation = _correlation_id() or str(inbound.id)

            a4 = A4ReplyAgent(
                ctx.runner, session, correlation_id=correlation, prompt_values=ctx.prompt_values
            )
            draft: ReplyDraft = await a4.draft_reply(
                thread_messages, classification, criteria, missing, facts=facts
            )

            to = [inbound.from_address] if inbound.from_address else []
            suppressed = await _is_suppressed(session, inbound.from_address)
            last_out = _last_outbound(rows)
            now = ctx.clock()
            a5 = A5QaAgent(
                ctx.runner, session, correlation_id=correlation, prompt_values=ctx.prompt_values
            )
            qa: QaResult = await a5.review(
                QaInput(
                    draft=QaDraft(
                        to=to,
                        subject=draft.subject,
                        body=draft.body,
                        asks_for=draft.asks_for,
                        contains_sensitive=draft.contains_sensitive,
                    ),
                    thread=thread_messages,
                    facts=facts,
                    current_buy_box=_criteria_doc(criteria),
                    recipient_suppressed=suppressed,
                    thread_participant=thread.counterpart_address or inbound.from_address,
                    last_outbound_asks_for=list((last_out.meta or {}).get("asks_for") or [])
                    if last_out
                    else (),
                    last_outbound_at=_aware(last_out.sent_at) if last_out else None,
                    now=now,
                )
            )
            decision = await PolicyEngine(session).decide(
                PolicyAction.INVESTOR_REPLY,
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
            body_to_send = (
                qa.rewrite_body
                if qa.verdict is QaVerdict.REWRITE and qa.rewrite_body
                else draft.body
            )
            graph_id = (inbound.meta or {}).get("graph_id")
            conversation_id = (inbound.meta or {}).get(
                "conversation_id"
            ) or thread.external_thread_id
            draft_meta = {
                "asks_for": list(draft.asks_for),
                "contains_sensitive": draft.contains_sensitive,
                "reply_to_message_id": str(inbound.id),
                "classification": classification.classification.value,
                "a4_body": draft.body,
            }
            await session.commit()  # agent runs are now durable

        # -- phase 2: the draft row (Graph draft unless blocked) -------------------------------
        if decision.decision is PolicyDecision.BLOCKED:
            async with ctx.session_factory() as session:
                draft_row_id = await _record_blocked_draft(
                    session,
                    inbound_id=inbound.id,
                    thread_id=thread.id,
                    to=to,
                    subject=draft.subject,
                    body=body_to_send,
                    mailbox=ctx.settings.graph_mailbox,
                    agent_run_id=draft.run_id,
                    meta={**draft_meta, "blocked": True, "conversation_id": conversation_id},
                )
                await session.commit()
        else:
            created = await create_draft(
                CreateDraftInput(
                    idempotency_key=reply_idempotency_key(str(inbound.id)),
                    to=to,
                    subject=draft.subject,
                    body_text=body_to_send,
                    reply_to_graph_message_id=str(graph_id) if graph_id else None,
                    conversation_id=str(conversation_id) if conversation_id else None,
                    thread_id=str(thread.id),
                    agent_run_id=str(draft.run_id),
                )
            )
            draft_row_id = uuid.UUID(created.message_id)

        # -- phase 3: QA review + policy on the draft row ---------------------------------------
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
            draft_row.policy_action = REPLY_POLICY_ACTION
            draft_row.policy_decision = decision.decision
            workflow_id = _correlation_id() or input.workflow_id
            draft_row.meta = {
                **(draft_row.meta or {}),
                **draft_meta,
                # The body that is actually in the Graph draft (a QA rewrite may differ from A4's).
                "graph_draft_body": body_to_send,
                # The review queue resolves the workflow to signal from this key
                # (``db.repositories.review.META_WORKFLOW_ID_KEY``).
                **({"temporal_workflow_id": workflow_id} if workflow_id else {}),
            }
            inbound_row = await _load_message(session, input.message_id)
            inbound_row.meta = {
                **(inbound_row.meta or {}),
                "reply": {
                    "draft_message_id": str(draft_row.id),
                    "qa": qa.to_json(),
                    "qa_review_id": str(review.id),
                    "policy_decision": decision.decision.value,
                    "policy_reason": decision.reason,
                    "policy_level": decision.level.value,
                },
            }
            await AuditRepository(session).record(
                actor=Actor.AGENT,
                actor_id="A5",
                event_type="reply.decided",
                entity_type="message",
                entity_id=draft_row.id,
                payload={
                    "inbound_message_id": str(inbound.id),
                    "qa_verdict": qa.verdict.value,
                    "qa_confidence": qa.confidence,
                    "qa_flags": list(qa.flags),
                    "qa_precheck_only": qa.precheck_only,
                    "policy_action": REPLY_POLICY_ACTION,
                    "policy_level": decision.level.value,
                    "policy_decision": decision.decision.value,
                    "policy_reason": decision.reason,
                    "contains_sensitive": draft.contains_sensitive,
                },
                correlation_id=_correlation_id(),
                idempotency_key=f"a5:{inbound.id}:decided",
            )
            await session.commit()
            review_id = review.id

        # -- phase 4: send when the policy says so ---------------------------------------------
        sent = False
        if decision.decision is PolicyDecision.SEND:
            sent_result = await send_draft(SendDraftInput(message_id=str(draft_row_id)))
            sent = sent_result.sent or sent_result.already_sent

        log.info(
            "conversation.reply_decided",
            message_id=str(inbound.id),
            draft_message_id=str(draft_row_id),
            qa_verdict=qa.verdict.value,
            qa_confidence=qa.confidence,
            qa_flags=qa.flags,
            policy_level=decision.level.value,
            policy_decision=decision.decision.value,
            policy_reason=decision.reason,
            sent=sent,
        )
        return DraftReplyResult(
            message_id=input.message_id,
            draft_message_id=str(draft_row_id),
            subject=draft.subject,
            body=body_to_send,
            asks_for=list(draft.asks_for),
            contains_sensitive=draft.contains_sensitive,
            qa_verdict=qa.verdict.value,
            qa_confidence=qa.confidence,
            qa_flags=list(qa.flags),
            qa_review_id=str(review_id),
            policy_decision=decision.decision.value,
            policy_reason=decision.reason,
            policy_level=decision.level.value,
            sent=sent,
        )
    finally:
        clear_context()


def _classification_from_row(inbound: Message) -> A4Classification:
    stored = (inbound.meta or {}).get("a4") or {}
    classification = inbound.classification or MessageClassification.AMBIGUOUS
    reconnect = stored.get("reconnect_date")
    parsed_date = None
    if reconnect:
        try:
            parsed_date = datetime.fromisoformat(str(reconnect)).date()
        except ValueError:
            parsed_date = None
    return A4Classification(
        classification=classification,
        confidence=float(inbound.classification_confidence or 0.0),
        rationale=str(stored.get("rationale", "")),
        is_auto_reply=bool(stored.get("is_auto_reply", False)),
        quoted_text_stripped=bool(stored.get("quoted_text_stripped", False)),
        reconnect_date=parsed_date,
    )


def _criteria_doc(criteria: BuyBoxCriteria | None) -> dict[str, Any] | None:
    if criteria is None:
        return None
    doc: dict[str, Any] = {}
    for key in (
        "geography",
        "asset_types",
        "price_min",
        "price_max",
        "condition",
        "strategy",
        "financing",
        "closing_preferences",
        "other_criteria",
        "unknown_fields",
    ):
        value = getattr(criteria, key)
        if value in (None, [], {}):
            continue
        doc[key] = str(value) if isinstance(value, Decimal) else value
    return doc


async def _record_blocked_draft(
    session: AsyncSession,
    *,
    inbound_id: uuid.UUID,
    thread_id: uuid.UUID,
    to: Seq[str],
    subject: str,
    body: str,
    mailbox: str | None,
    agent_run_id: uuid.UUID,
    meta: Mapping[str, Any],
) -> uuid.UUID:
    existing = await _find_reply_row(session, str(inbound_id))
    if existing is not None:
        return existing.id
    row = Message(
        thread_id=thread_id,
        direction=Direction.OUT,
        channel=Channel.EMAIL,
        external_message_id=reply_idempotency_key(str(inbound_id)),
        from_address=mailbox,
        to_addresses=list(to),
        subject=subject,
        body=body,
        draft=True,
        policy_action=REPLY_POLICY_ACTION,
        policy_decision=PolicyDecision.BLOCKED,
        agent_run_id=agent_run_id,
        meta=dict(meta),
    )
    session.add(row)
    await session.flush()
    return row.id


# --------------------------------------------------------------------------------------------
# apply_human_decision (review queue -> workflow signal -> here)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ApplyHumanDecisionInput:
    message_id: str  # the INBOUND message whose reply was queued
    approved: bool
    edited_body: str | None = None
    decided_by: str | None = None


@dataclass(slots=True)
class ApplyHumanDecisionResult:
    message_id: str
    draft_message_id: str | None
    sent: bool
    discarded: bool
    edited: bool
    already_decided: bool = False
    reason: str | None = None


@activity.defn(name="conversation_apply_human_decision")
async def apply_human_decision(input: ApplyHumanDecisionInput) -> ApplyHumanDecisionResult:
    """Send or discard a queued reply on the human's say-so (POL-03c L0 "human sends").

    An edited body cannot be patched into the existing Graph draft (the gateway has no update
    operation), so an edit creates a second reply draft (key ``reply:<id>:edited``), sends that
    and marks the original row ``superseded_by``. Idempotent: a repeated decision is a no-op.
    """
    _bind()
    ctx = get_conversation_context()
    try:
        async with ctx.session_factory() as session:
            inbound = await _load_message(session, input.message_id)
            draft_row = await _find_reply_row(session, input.message_id)
            if draft_row is None:
                return ApplyHumanDecisionResult(
                    input.message_id, None, False, False, False, reason="no_draft"
                )
            meta = dict(draft_row.meta or {})
            # ``meta["human_decision"]`` is the review queue's own record (written before it
            # signals); this activity keeps its marker under ``human_decision_applied``.
            applied_before = meta.get("human_decision_applied")
            if applied_before is not None:
                return ApplyHumanDecisionResult(
                    input.message_id,
                    str(meta.get("sent_message_id") or draft_row.id),
                    sent=bool(meta.get("human_sent")),
                    discarded=applied_before == "rejected",
                    edited=bool(meta.get("edited")),
                    already_decided=True,
                )
            if meta.get("blocked") is True:
                # QA / policy blocked it: a human approval cannot revive a blocked draft.
                return ApplyHumanDecisionResult(
                    input.message_id, str(draft_row.id), False, True, False, reason="blocked"
                )
            graph_body = str(meta.get("graph_draft_body") or draft_row.body)
            edited = (
                bool(input.edited_body) and (input.edited_body or "").strip() != graph_body.strip()
            )
            thread_id = draft_row.thread_id
            graph_id = (inbound.meta or {}).get("graph_id")
            conversation_id = (inbound.meta or {}).get("conversation_id")
            to = list(draft_row.to_addresses)
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
                return ApplyHumanDecisionResult(
                    input.message_id, str(original_id), sent=False, discarded=True, edited=False
                )
            await session.commit()

        send_id = original_id
        if edited:
            created = await create_draft(
                CreateDraftInput(
                    idempotency_key=f"{reply_idempotency_key(str(inbound.id))}:edited",
                    to=to,
                    subject=subject,
                    body_text=(input.edited_body or "").strip(),
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
                    sent_row.policy_action = REPLY_POLICY_ACTION
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
        log.info(
            "conversation.human_decision",
            message_id=str(inbound.id),
            approved=True,
            edited=edited,
            sent=sent,
        )
        return ApplyHumanDecisionResult(
            input.message_id, str(send_id), sent=sent, discarded=False, edited=edited
        )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# notify_bounce
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class NotifyBounceInput:
    message_id: str
    bounced_address: str | None = None


@dataclass(slots=True)
class NotifyBounceResult:
    message_id: str
    signalled: bool
    sequence_workflow_id: str | None = None


@activity.defn(name="conversation_notify_bounce")
async def notify_bounce(input: NotifyBounceInput) -> NotifyBounceResult:
    """Find the investor sequence behind a bounced address and signal ``bounce`` once."""
    _bind()
    ctx = get_conversation_context()
    try:
        async with ctx.session_factory() as session:
            message = await _load_message(session, input.message_id)
            address = input.bounced_address or (message.meta or {}).get("bounced_address")
            investor_id: uuid.UUID | None = None
            if address:
                try:
                    normalized = normalize_email(str(address))
                except ValueError:
                    normalized = None
                if normalized:
                    stmt = select(Contact).where(
                        Contact.channel == ContactChannel.EMAIL,
                        Contact.normalized_value == normalized,
                    )
                    contact = (await session.execute(stmt)).scalars().first()
                    investor_id = contact.investor_id if contact else None
            thread = await session.get(Thread, message.thread_id)
            if thread is None:  # pragma: no cover
                return NotifyBounceResult(input.message_id, signalled=False)
            sequence = await _active_sequence(session, thread, investor_id)
            workflow_id = await _signal_sequence(
                ctx,
                session,
                sequence=sequence,
                signal=SIGNAL_BOUNCE,
                payload=BounceSignal(message_id=str(message.id), bounced_address=address),
                message=message,
                idempotency_key=f"bounce:{message.id}:signal",
            )
            await session.commit()
            return NotifyBounceResult(
                input.message_id,
                signalled=workflow_id is not None,
                sequence_workflow_id=workflow_id,
            )
    finally:
        clear_context()


__all__ = [
    "REPLY_POLICY_ACTION",
    "SIGNAL_BOUNCE",
    "SIGNAL_OPT_OUT",
    "SIGNAL_REPLY",
    "SUPPRESSION_REASON_OPT_OUT",
    "ApplyHumanDecisionInput",
    "ApplyHumanDecisionResult",
    "BounceSignal",
    "BuyBoxVersionInfo",
    "ClassifyInboundInput",
    "ClassifyInboundResult",
    "ConversationContext",
    "DraftReplyInput",
    "DraftReplyResult",
    "ExtractBuyBoxInput",
    "ExtractBuyBoxResult",
    "NotifyBounceInput",
    "NotifyBounceResult",
    "OptOutSignal",
    "ReplySignal",
    "apply_human_decision",
    "classify_inbound",
    "draft_and_qa_reply",
    "extract_and_upsert_buy_box",
    "get_conversation_context",
    "notify_bounce",
    "reply_idempotency_key",
    "set_conversation_context",
]
