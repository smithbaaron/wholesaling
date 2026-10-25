"""Graph (Outlook) activities: every Graph call and every ``messages`` write runs here.

Idempotency contract (REQUIREMENTS.md XC-03, INV-24, RUL-05):

- inbound: ``(channel, external_message_id)`` on ``messages`` is the key, where
  ``external_message_id`` is Graph's ``internetMessageId``. ``fetch_message`` and
  ``delta_poll`` share ``upsert_inbound``; a message delivered twice (webhook + delta, or an
  activity retry) yields exactly one row and exactly one ``audit_events`` row;
- outbound: the caller supplies an ``idempotency_key`` that becomes the draft row's
  ``external_message_id``; ``create_draft`` retried returns the same row, and ``send_draft``
  on a row with ``sent_at`` set never calls Graph again. The audit idempotency keys are
  derived from the same value.

Dependencies (gateway, session factory) come from ``GraphActivityContext`` so tests and the
synthetic pilot inject the mock gateway and the SQLite session factory.

Inputs and outputs are dataclasses: Temporal's default converter serialises them and the
workflow side needs no pydantic.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.config.settings import Settings, get_settings
from wholesaling.db.models.enums import Actor, Channel, Direction
from wholesaling.db.models.messaging import Message, Suppression, Thread
from wholesaling.db.normalize import normalize_email
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.integrations.graph import (
    DEFAULT_FOLDER,
    GraphGateway,
    GraphNotFound,
    InboundMessage,
    OutboundDraft,
    get_graph,
    subscription_expiry,
)
from wholesaling.logging import bind_correlation_id, clear_context, get_logger

log = get_logger(__name__)

Clock = Callable[[], datetime]
SUPPRESSION_REASON_BOUNCE = "bounce"
DEFAULT_DELTA_LOOKBACK_HOURS = 24


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for ``DateTime(timezone=True)``; treat them as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------------------------
# Dependency context
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class GraphActivityContext:
    gateway: GraphGateway
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    clock: Clock = _utcnow


_context: GraphActivityContext | None = None


def set_graph_context(ctx: GraphActivityContext | None) -> None:
    """Install (or clear) the dependency context. Tests call this with the mock gateway."""
    global _context
    _context = ctx


def get_graph_context() -> GraphActivityContext:
    """The active context, built from settings on first use inside a worker."""
    global _context
    if _context is None:
        from wholesaling.db.engine import get_session_factory

        settings = get_settings()
        _context = GraphActivityContext(
            gateway=get_graph(settings), session_factory=get_session_factory(), settings=settings
        )
    return _context


def _bind_workflow_context() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


# --------------------------------------------------------------------------------------------
# Inbound upsert (shared by fetch_message and delta_poll)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class UpsertOutcome:
    message_id: str
    thread_id: str
    external_message_id: str
    created: bool
    is_bounce: bool
    bounced_address: str | None
    suppression_created: bool


async def _get_or_create_thread(
    session: AsyncSession,
    *,
    conversation_id: str | None,
    subject: str | None,
    counterpart: str | None,
    at: datetime | None,
) -> Thread:
    if conversation_id is not None:
        stmt = select(Thread).where(
            Thread.channel == Channel.EMAIL, Thread.external_thread_id == conversation_id
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            last = _aware(existing.last_message_at)
            if at is not None and (last is None or at > last):
                existing.last_message_at = at
            return existing
    thread = Thread(
        channel=Channel.EMAIL,
        external_thread_id=conversation_id,
        subject=subject,
        counterpart_address=counterpart,
        last_message_at=at,
    )
    session.add(thread)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # Lost a race on (channel, external_thread_id); re-read the winner.
        stmt = select(Thread).where(
            Thread.channel == Channel.EMAIL, Thread.external_thread_id == conversation_id
        )
        thread = (await session.execute(stmt)).scalar_one()
    return thread


async def _find_message(session: AsyncSession, external_message_id: str) -> Message | None:
    stmt = select(Message).where(
        Message.channel == Channel.EMAIL, Message.external_message_id == external_message_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_inbound(
    session: AsyncSession, msg: InboundMessage, *, own_mailbox: str | None, source: str
) -> UpsertOutcome:
    """Insert the thread + message rows for one inbound message, or return the existing row.

    Exactly one ``messages`` row and one ``message.received`` audit event per
    ``internetMessageId`` no matter how many times this runs (INV-24). A bounce adds one
    ``suppression`` row for the failed address with reason ``bounce`` (INT-25).
    """
    audit = AuditRepository(session)
    existing = await _find_message(session, msg.external_message_id)
    created = False
    if existing is None:
        direction = (
            Direction.OUT
            if own_mailbox and msg.from_address and msg.from_address.lower() == own_mailbox.lower()
            else Direction.IN
        )
        thread = await _get_or_create_thread(
            session,
            conversation_id=msg.conversation_id,
            subject=msg.subject,
            counterpart=(
                msg.from_address if direction == Direction.IN else (msg.to[0] if msg.to else None)
            ),
            at=msg.received_at,
        )
        row = Message(
            thread_id=thread.id,
            direction=direction,
            channel=Channel.EMAIL,
            external_message_id=msg.external_message_id,
            in_reply_to_external_id=msg.in_reply_to,
            from_address=msg.from_address,
            to_addresses=list(msg.to),
            subject=msg.subject,
            body=msg.body_text,
            body_html=msg.body_html,
            draft=msg.is_draft,
            received_at=msg.received_at,
            meta={
                "graph_id": msg.graph_id,
                "conversation_id": msg.conversation_id,
                "headers": dict(msg.headers),
                "references": list(msg.references),
                "is_bounce": msg.bounce.is_bounce,
                "bounce_reason": msg.bounce.reason,
                "bounced_address": msg.bounce.bounced_address,
                "source": source,
            },
        )
        session.add(row)
        try:
            async with session.begin_nested():
                await session.flush()
            existing = row
            created = True
        except IntegrityError:
            # Concurrent insert of the same internetMessageId won: use that row.
            found = await _find_message(session, msg.external_message_id)
            if found is None:  # pragma: no cover - defensive
                raise
            existing = found

    _, audit_created = await audit.record(
        actor=Actor.SYSTEM,
        actor_id="graph",
        event_type="message.received",
        entity_type="message",
        entity_id=existing.id,
        payload={
            "external_message_id": msg.external_message_id,
            "graph_id": msg.graph_id,
            "conversation_id": msg.conversation_id,
            "from": msg.from_address,
            "subject": msg.subject,
            "is_bounce": msg.bounce.is_bounce,
            "source": source,
        },
        idempotency_key=f"graph:{msg.external_message_id}:received",
    )

    suppression_created = False
    if msg.bounce.is_bounce and msg.bounce.bounced_address:
        suppression_created = await _suppress_bounce(
            session, audit, msg.bounce.bounced_address, existing, msg.external_message_id
        )

    log.info(
        "graph.inbound_upserted",
        message_id=str(existing.id),
        created=created,
        audit_created=audit_created,
        is_bounce=msg.bounce.is_bounce,
        suppression_created=suppression_created,
        source=source,
    )
    return UpsertOutcome(
        message_id=str(existing.id),
        thread_id=str(existing.thread_id),
        external_message_id=msg.external_message_id,
        created=created,
        is_bounce=msg.bounce.is_bounce,
        bounced_address=msg.bounce.bounced_address,
        suppression_created=suppression_created,
    )


async def _suppress_bounce(
    session: AsyncSession,
    audit: AuditRepository,
    address: str,
    message: Message,
    external_message_id: str,
) -> bool:
    try:
        normalized = normalize_email(address)
    except ValueError:
        log.warning("graph.bounce_address_invalid", address=address)
        return False
    stmt = select(Suppression).where(
        Suppression.channel == Channel.EMAIL, Suppression.normalized_value == normalized
    )
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        return False
    session.add(
        Suppression(
            channel=Channel.EMAIL,
            normalized_value=normalized,
            reason=SUPPRESSION_REASON_BOUNCE,
            source_message_id=message.id,
        )
    )
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return False
    await audit.record(
        actor=Actor.SYSTEM,
        actor_id="graph",
        event_type="suppression.added",
        entity_type="suppression",
        entity_id=None,
        payload={"address": normalized, "reason": SUPPRESSION_REASON_BOUNCE},
        correlation_id=str(message.id),
        idempotency_key=f"graph:{external_message_id}:bounce",
    )
    log.warning("graph.bounce_suppressed", address=normalized, message_id=str(message.id))
    return True


# --------------------------------------------------------------------------------------------
# fetch_message (webhook path, WF-19 step 1)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class FetchMessageInput:
    graph_id: str
    subscription_id: str | None = None
    change_type: str | None = None


@dataclass(slots=True)
class FetchMessageResult:
    found: bool
    message_id: str | None = None
    thread_id: str | None = None
    external_message_id: str | None = None
    created: bool = False
    is_bounce: bool = False
    bounced_address: str | None = None
    suppression_created: bool = False
    is_reply: bool = False
    from_address: str | None = None


@activity.defn(name="graph_fetch_message")
async def fetch_message(input: FetchMessageInput) -> FetchMessageResult:
    """Fetch one message by Graph id and upsert thread + message rows."""
    _bind_workflow_context()
    ctx = get_graph_context()
    try:
        try:
            msg = await ctx.gateway.get_message(input.graph_id)
        except GraphNotFound:
            # Deleted between notification and fetch; nothing to record.
            log.info("graph.fetch_message.gone", graph_id=input.graph_id)
            return FetchMessageResult(found=False)
        async with ctx.session_factory() as session:
            outcome = await upsert_inbound(
                session, msg, own_mailbox=ctx.settings.graph_mailbox, source="webhook"
            )
            await session.commit()
        return FetchMessageResult(
            found=True,
            is_reply=msg.is_reply,
            from_address=msg.from_address,
            **asdict(outcome),
        )
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# delta_poll (WF-12 reconciliation)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DeltaPollInput:
    folder: str = DEFAULT_FOLDER
    delta_token: str | None = None
    lookback_hours: int = DEFAULT_DELTA_LOOKBACK_HOURS


@dataclass(slots=True)
class DeltaPollResult:
    fetched: int
    created: int
    bounces: int
    removed: int
    next_delta_token: str | None
    pages_fetched: int


@activity.defn(name="graph_delta_poll")
async def delta_poll(input: DeltaPollInput) -> DeltaPollResult:
    """Reconcile missed webhooks: delta-query the folder and run the same upsert.

    Without a ``delta_token`` the round is bounded to the last ``lookback_hours`` so a fresh
    scheduled run (which has no token) re-checks the window in which Graph may have dropped
    notifications (Graph retries for up to 4 h; 24 h is a wide margin). Every row write is
    idempotent, so overlapping windows are harmless.
    """
    _bind_workflow_context()
    ctx = get_graph_context()
    try:
        since = (
            None
            if input.delta_token
            else ctx.clock() - timedelta(hours=max(1, input.lookback_hours))
        )
        page = await ctx.gateway.delta_messages(input.folder, input.delta_token, since=since)
        created = 0
        bounces = 0
        async with ctx.session_factory() as session:
            for msg in page.messages:
                if msg.is_draft:
                    continue
                outcome = await upsert_inbound(
                    session, msg, own_mailbox=ctx.settings.graph_mailbox, source="delta"
                )
                created += int(outcome.created)
                bounces += int(outcome.is_bounce)
            await session.commit()
        result = DeltaPollResult(
            fetched=len(page.messages),
            created=created,
            bounces=bounces,
            removed=len(page.removed_ids),
            next_delta_token=page.next_delta_token,
            pages_fetched=page.pages_fetched,
        )
        summary = {k: v for k, v in asdict(result).items() if k != "next_delta_token"}
        log.info("graph.delta_poll", **summary)
        return result
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# renew_subscriptions (WF-11)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RenewSubscriptionsInput:
    notification_url: str | None = None  # defaults to GRAPH_WEBHOOK_URL
    resource: str | None = None  # defaults to the mailbox inbox resource
    lifetime_hours: int = 70


@dataclass(slots=True)
class RenewSubscriptionsResult:
    renewed: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    removed_stale: list[str] = field(default_factory=list)
    expires_at: str | None = None


def _default_resource(settings: Settings) -> str:
    mailbox = settings.graph_mailbox or "deals@example.test"
    return f"users/{mailbox}/mailFolders('{DEFAULT_FOLDER}')/messages"


@activity.defn(name="graph_renew_subscriptions")
async def renew_subscriptions(input: RenewSubscriptionsInput) -> RenewSubscriptionsResult:
    """Ensure exactly one live subscription for our inbox resource points at our webhook.

    Renews the matching subscription(s), creates one when none exists, and deletes stale ones
    for the same resource that point at a different (old) notification URL. Raises on failure
    so Temporal retries and the workflow can count consecutive failures.
    """
    _bind_workflow_context()
    ctx = get_graph_context()
    try:
        notification_url = input.notification_url or ctx.settings.graph_webhook_url
        if not notification_url:
            msg = "GRAPH_WEBHOOK_URL is not set; cannot create or renew a subscription"
            raise ValueError(msg)
        client_state = (
            ctx.settings.graph_webhook_client_state.get_secret_value()
            if ctx.settings.graph_webhook_client_state
            else ""
        )
        if not client_state:
            msg = "GRAPH_WEBHOOK_CLIENT_STATE is not set; refusing to subscribe without it"
            raise ValueError(msg)
        resource = input.resource or _default_resource(ctx.settings)
        expires_at = subscription_expiry(ctx.clock(), timedelta(hours=input.lifetime_hours))
        result = RenewSubscriptionsResult(expires_at=expires_at.isoformat())

        existing = await ctx.gateway.list_subscriptions()
        ours = [s for s in existing if s.resource == resource]
        for sub in ours:
            if sub.notification_url and sub.notification_url != notification_url:
                await ctx.gateway.delete_subscription(sub.id)
                result.removed_stale.append(sub.id)
                continue
            try:
                await ctx.gateway.renew_subscription(sub.id, expires_at)
                result.renewed.append(sub.id)
            except GraphNotFound:
                log.warning("graph.subscription_lapsed", subscription_id=sub.id)
        if not result.renewed:
            sub = await ctx.gateway.create_subscription(
                resource=resource,
                notification_url=notification_url,
                client_state=client_state,
                expires_at=expires_at,
                change_types=["created"],
                lifecycle_notification_url=notification_url,
            )
            result.created.append(sub.id)
        log.info("graph.subscriptions_renewed", **asdict(result))
        return result
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# Renewal outcome bookkeeping and alerting (WF-11 "alert if renewal fails twice", WF-23)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RecordRenewalOutcomeInput:
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class RecordRenewalOutcomeResult:
    consecutive_failures: int


RENEWAL_EVENT = "graph.subscription_renewal"
ALERT_EVENT = "alert.raised"


@activity.defn(name="graph_record_renewal_outcome")
async def record_renewal_outcome(input: RecordRenewalOutcomeInput) -> RecordRenewalOutcomeResult:
    """Append the renewal outcome to the audit log and return the trailing failure streak.

    The streak is derived from the audit log, so it survives the scheduled workflow runs that
    carry no state of their own.
    """
    _bind_workflow_context()
    ctx = get_graph_context()
    try:
        from wholesaling.db.models.ops import AuditEvent

        async with ctx.session_factory() as session:
            audit = AuditRepository(session)
            await audit.record(
                actor=Actor.SYSTEM,
                actor_id="graph",
                event_type=RENEWAL_EVENT,
                entity_type="graph_subscription",
                payload={"ok": input.ok, "detail": input.detail},
            )
            stmt = (
                select(AuditEvent.payload)
                .where(AuditEvent.event_type == RENEWAL_EVENT)
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .limit(10)
            )
            streak = 0
            for (payload,) in (await session.execute(stmt)).all():
                if payload.get("ok") is True:
                    break
                streak += 1
            await session.commit()
        return RecordRenewalOutcomeResult(consecutive_failures=streak)
    finally:
        clear_context()


@dataclass(slots=True)
class RaiseAlertInput:
    kind: str
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@activity.defn(name="graph_raise_alert")
async def raise_alert(input: RaiseAlertInput) -> bool:
    """Alert = structured log event + audit event (WF-23). A pager/Slack sink comes later."""
    _bind_workflow_context()
    ctx = get_graph_context()
    try:
        log.error("alert", kind=input.kind, detail=input.detail, **input.payload)
        async with ctx.session_factory() as session:
            _, created = await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="graph",
                event_type=ALERT_EVENT,
                entity_type="alert",
                payload={"kind": input.kind, "detail": input.detail, **input.payload},
                idempotency_key=input.idempotency_key,
            )
            await session.commit()
        return created
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# Drafts and sends (INT-13, RUL-05)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CreateDraftInput:
    idempotency_key: str
    to: list[str]
    subject: str
    body_text: str
    body_html: str | None = None
    cc: list[str] = field(default_factory=list)
    reply_to_graph_message_id: str | None = None
    conversation_id: str | None = None
    thread_id: str | None = None
    sequence_id: str | None = None
    sequence_step: int | None = None
    agent_run_id: str | None = None


@dataclass(slots=True)
class CreateDraftResult:
    message_id: str
    thread_id: str
    draft_id: str | None
    created: bool
    already_sent: bool


@activity.defn(name="graph_create_draft")
async def create_draft(input: CreateDraftInput) -> CreateDraftResult:
    """Create a Graph draft and its ``messages`` row (direction=out, draft=True). Never sends.

    Retried with the same ``idempotency_key`` it returns the existing row without creating a
    second draft in the mailbox.
    """
    _bind_workflow_context()
    ctx = get_graph_context()
    try:
        async with ctx.session_factory() as session:
            existing = await _find_message(session, input.idempotency_key)
            if existing is not None:
                return CreateDraftResult(
                    message_id=str(existing.id),
                    thread_id=str(existing.thread_id),
                    draft_id=existing.meta.get("graph_draft_id"),
                    created=False,
                    already_sent=existing.sent_at is not None,
                )

            info = await ctx.gateway.create_draft(
                OutboundDraft(
                    to=list(input.to),
                    cc=list(input.cc),
                    subject=input.subject,
                    body_text=input.body_text,
                    body_html=input.body_html,
                    reply_to_graph_message_id=input.reply_to_graph_message_id,
                    conversation_id=input.conversation_id,
                )
            )
            conversation_id = info.conversation_id or input.conversation_id
            thread: Thread | None = None
            if input.thread_id:
                thread = await session.get(Thread, uuid.UUID(input.thread_id))
                if thread is not None and thread.external_thread_id is None and conversation_id:
                    thread.external_thread_id = conversation_id
            if thread is None:
                thread = await _get_or_create_thread(
                    session,
                    conversation_id=conversation_id,
                    subject=input.subject,
                    counterpart=input.to[0] if input.to else None,
                    at=None,
                )
            row = Message(
                thread_id=thread.id,
                direction=Direction.OUT,
                channel=Channel.EMAIL,
                external_message_id=input.idempotency_key,
                from_address=ctx.settings.graph_mailbox,
                to_addresses=list(input.to),
                subject=input.subject,
                body=input.body_text,
                body_html=input.body_html,
                draft=True,
                sequence_id=uuid.UUID(input.sequence_id) if input.sequence_id else None,
                sequence_step=input.sequence_step,
                agent_run_id=uuid.UUID(input.agent_run_id) if input.agent_run_id else None,
                meta={
                    "graph_draft_id": info.draft_id,
                    "internet_message_id": info.internet_message_id,
                    "conversation_id": conversation_id,
                    "reply_to_graph_message_id": input.reply_to_graph_message_id,
                    "idempotency_key": input.idempotency_key,
                },
            )
            session.add(row)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError:
                # A concurrent retry created the row first; its draft wins.
                found = await _find_message(session, input.idempotency_key)
                if found is None:  # pragma: no cover - defensive
                    raise
                await session.commit()
                return CreateDraftResult(
                    message_id=str(found.id),
                    thread_id=str(found.thread_id),
                    draft_id=found.meta.get("graph_draft_id"),
                    created=False,
                    already_sent=found.sent_at is not None,
                )
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="graph",
                event_type="message.drafted",
                entity_type="message",
                entity_id=row.id,
                payload={"draft_id": info.draft_id, "to": list(input.to), "subject": input.subject},
                idempotency_key=f"out:{input.idempotency_key}:drafted",
            )
            await session.commit()
            log.info("graph.draft_recorded", message_id=str(row.id), draft_id=info.draft_id)
            return CreateDraftResult(
                message_id=str(row.id),
                thread_id=str(thread.id),
                draft_id=info.draft_id,
                created=True,
                already_sent=False,
            )
    finally:
        clear_context()


@dataclass(slots=True)
class SendDraftInput:
    message_id: str


@dataclass(slots=True)
class SendDraftResult:
    message_id: str
    sent: bool
    already_sent: bool
    sent_at: str | None


@activity.defn(name="graph_send_draft")
async def send_draft(input: SendDraftInput) -> SendDraftResult:
    """Send the Graph draft behind a ``messages`` row and stamp ``sent_at``.

    Idempotent (RUL-05): a row with ``sent_at`` set is returned untouched; a draft Graph no
    longer has (sent by a previous attempt that crashed before commit) is treated as sent.
    """
    _bind_workflow_context()
    ctx = get_graph_context()
    try:
        async with ctx.session_factory() as session:
            row = await session.get(Message, uuid.UUID(input.message_id))
            if row is None:
                msg = f"message {input.message_id} not found"
                raise ValueError(msg)
            if row.sent_at is not None:
                return SendDraftResult(
                    message_id=str(row.id),
                    sent=False,
                    already_sent=True,
                    sent_at=(_aware(row.sent_at) or row.sent_at).isoformat(),
                )
            if row.direction != Direction.OUT or not row.draft:
                msg = f"message {input.message_id} is not an outbound draft"
                raise ValueError(msg)
            draft_id = row.meta.get("graph_draft_id")
            if not draft_id:
                msg = f"message {input.message_id} has no graph_draft_id"
                raise ValueError(msg)

            try:
                result = await ctx.gateway.send_draft(str(draft_id))
                sent_at = result.sent_at or ctx.clock()
                new_meta = {
                    "internet_message_id": result.internet_message_id
                    or row.meta.get("internet_message_id"),
                    "conversation_id": result.conversation_id or row.meta.get("conversation_id"),
                }
            except GraphNotFound:
                # Sent on an earlier attempt that did not reach commit: record, do not resend.
                log.warning("graph.send_draft.already_gone", draft_id=draft_id)
                sent_at = ctx.clock()
                new_meta = {}

            row.sent_at = sent_at
            row.draft = False
            row.meta = {**row.meta, **{k: v for k, v in new_meta.items() if v}}
            thread = await session.get(Thread, row.thread_id)
            if thread is not None:
                thread.last_message_at = sent_at
                if thread.external_thread_id is None and row.meta.get("conversation_id"):
                    thread.external_thread_id = row.meta["conversation_id"]
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="graph",
                event_type="message.sent",
                entity_type="message",
                entity_id=row.id,
                payload={"draft_id": draft_id, "to": row.to_addresses},
                idempotency_key=f"out:{row.external_message_id}:sent",
            )
            await session.commit()
            log.info("graph.draft_sent_recorded", message_id=str(row.id), draft_id=draft_id)
            return SendDraftResult(
                message_id=str(row.id), sent=True, already_sent=False, sent_at=sent_at.isoformat()
            )
    finally:
        clear_context()


__all__ = [
    "ALERT_EVENT",
    "RENEWAL_EVENT",
    "CreateDraftInput",
    "CreateDraftResult",
    "DeltaPollInput",
    "DeltaPollResult",
    "FetchMessageInput",
    "FetchMessageResult",
    "GraphActivityContext",
    "RaiseAlertInput",
    "RecordRenewalOutcomeInput",
    "RecordRenewalOutcomeResult",
    "RenewSubscriptionsInput",
    "RenewSubscriptionsResult",
    "SendDraftInput",
    "SendDraftResult",
    "UpsertOutcome",
    "create_draft",
    "delta_poll",
    "fetch_message",
    "get_graph_context",
    "raise_alert",
    "record_renewal_outcome",
    "renew_subscriptions",
    "send_draft",
    "set_graph_context",
    "upsert_inbound",
]
