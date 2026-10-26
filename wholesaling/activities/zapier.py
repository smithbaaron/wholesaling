"""``send_parser_email``: the optional Zapier parser path (REQUIREMENTS.md INV-20, INT-20..22,
RUL-07).

Behaviour:

- ``ZAPIER_PARSER_ENABLED=false`` (the default): nothing is formatted or sent. One
  ``zapier.parser_skipped`` audit note is recorded per (buy box, version) so the trail shows
  the path was consulted and declined.
- flag on: A6 formats the current row of the buy box, then the email goes through the Graph
  draft -> send activities (``integrations/zapier/send.py``) to ``ZAPIER_PARSER_ADDRESS``. In mock
  mode the mock Graph gateway records the draft and the send; in live mode ``send_draft`` is
  still subject to ``GRAPH_ALLOW_LIVE_SEND`` (RUL-06).
- idempotent per (buy_box_id, version): a retry or a replayed buy-box event finds the existing
  ``messages`` row and sends nothing.

Dependencies: the DB session factory and settings come from ``ZapierActivityContext``; the Graph
gateway is whatever ``activities.graph`` has installed, so tests wire one mock gateway for both.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.agents.a6 import A6ParserFormatter, parser_idempotency_key
from wholesaling.config.settings import Settings, get_settings
from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.enums import Actor
from wholesaling.db.models.investors import Investor
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.integrations.zapier.send import send_parser_email_via_graph
from wholesaling.logging import bind_correlation_id, clear_context, get_logger

log = get_logger(__name__)

Clock = Callable[[], datetime]
PARSER_SENT_EVENT = "zapier.parser_sent"
PARSER_SKIPPED_EVENT = "zapier.parser_skipped"
REASON_FLAG_OFF = "flag_off"
REASON_NO_MAILBOX = "no_parser_mailbox"
REASON_REPLAY = "replay"
REASON_NOT_FOUND = "buy_box_not_found"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class ZapierActivityContext:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    clock: Clock = _utcnow


_context: ZapierActivityContext | None = None


def set_zapier_context(ctx: ZapierActivityContext | None) -> None:
    global _context
    _context = ctx


def get_zapier_context() -> ZapierActivityContext:
    global _context
    if _context is None:
        from wholesaling.db.engine import get_session_factory

        _context = ZapierActivityContext(
            session_factory=get_session_factory(), settings=get_settings()
        )
    return _context


def _bind() -> str | None:
    if activity.in_activity():
        workflow_id = activity.info().workflow_id
        bind_correlation_id(workflow_id)
        return workflow_id
    return None


@dataclass(slots=True)
class SendParserEmailInput:
    buy_box_id: str


@dataclass(slots=True)
class SendParserEmailResult:
    buy_box_id: str
    version: int | None
    enabled: bool
    sent: bool
    skipped_reason: str | None = None
    message_id: str | None = None
    subject: str | None = None
    idempotency_key: str | None = None
    audit_created: bool = False

    @property
    def replayed(self) -> bool:
        return self.skipped_reason == REASON_REPLAY


@activity.defn(name="zapier_send_parser_email")
async def send_parser_email(input: SendParserEmailInput) -> SendParserEmailResult:
    workflow_id = _bind()
    ctx = get_zapier_context()
    try:
        settings = ctx.settings
        async with ctx.session_factory() as session:
            box = await session.get(BuyBox, uuid.UUID(input.buy_box_id))
            if box is None:
                log.warning("zapier.buy_box_missing", buy_box_id=input.buy_box_id)
                return SendParserEmailResult(
                    buy_box_id=input.buy_box_id,
                    version=None,
                    enabled=settings.zapier_parser_enabled,
                    sent=False,
                    skipped_reason=REASON_NOT_FOUND,
                )
            investor = await session.get(Investor, box.investor_id)
            if investor is None:  # pragma: no cover - FK guarantees it
                msg = f"investor {box.investor_id} missing for buy box {box.id}"
                raise ValueError(msg)
            key = parser_idempotency_key(str(box.id), box.version)
            audit = AuditRepository(session)

            if not settings.zapier_parser_enabled or not settings.zapier_parser_address:
                reason = (
                    REASON_FLAG_OFF if not settings.zapier_parser_enabled else REASON_NO_MAILBOX
                )
                _, created = await audit.record(
                    actor=Actor.AGENT,
                    actor_id="A6",
                    event_type=PARSER_SKIPPED_EVENT,
                    entity_type="buy_box",
                    entity_id=box.id,
                    payload={
                        "investor_id": str(investor.id),
                        "buy_box_key": box.buy_box_key,
                        "version": box.version,
                        "reason": reason,
                        "note": (
                            "ZAPIER_PARSER_ENABLED is false (RUL-07); parser email not sent"
                            if reason == REASON_FLAG_OFF
                            else "ZAPIER_PARSER_ADDRESS is not set; parser email not sent"
                        ),
                    },
                    correlation_id=workflow_id,
                    idempotency_key=f"{key}:skipped",
                )
                await session.commit()
                log.info("zapier.parser_skipped", buy_box_id=str(box.id), reason=reason)
                return SendParserEmailResult(
                    buy_box_id=str(box.id),
                    version=box.version,
                    enabled=settings.zapier_parser_enabled,
                    sent=False,
                    skipped_reason=reason,
                    idempotency_key=key,
                    audit_created=created,
                )

            email = A6ParserFormatter().format(box, investor)
            mailbox = settings.zapier_parser_address

        # Graph draft + send run in their own sessions (activities.graph owns those writes).
        outcome = await send_parser_email_via_graph(email, mailbox=mailbox)

        async with ctx.session_factory() as session:
            _, created = await AuditRepository(session).record(
                actor=Actor.AGENT,
                actor_id="A6",
                event_type=PARSER_SENT_EVENT,
                entity_type="buy_box",
                entity_id=uuid.UUID(email.buy_box_id),
                payload={
                    "investor_id": str(investor.id),
                    "buy_box_key": box.buy_box_key,
                    "version": email.version,
                    "message_id": outcome.message_id,
                    "draft_id": outcome.draft_id,
                    "to": mailbox,
                    "subject": email.subject,
                    "graph_mode": settings.graph_mode,
                },
                correlation_id=workflow_id,
                idempotency_key=f"{key}:sent",
            )
            await session.commit()
        log.info(
            "zapier.parser_email",
            buy_box_id=email.buy_box_id,
            version=email.version,
            sent=outcome.sent,
            replay=outcome.replay,
            message_id=outcome.message_id,
        )
        return SendParserEmailResult(
            buy_box_id=email.buy_box_id,
            version=email.version,
            enabled=True,
            sent=outcome.sent,
            skipped_reason=REASON_REPLAY if outcome.replay else None,
            message_id=outcome.message_id,
            subject=email.subject,
            idempotency_key=key,
            audit_created=created,
        )
    finally:
        clear_context()


__all__ = [
    "PARSER_SENT_EVENT",
    "PARSER_SKIPPED_EVENT",
    "REASON_FLAG_OFF",
    "REASON_NOT_FOUND",
    "REASON_NO_MAILBOX",
    "REASON_REPLAY",
    "SendParserEmailInput",
    "SendParserEmailResult",
    "ZapierActivityContext",
    "get_zapier_context",
    "send_parser_email",
    "set_zapier_context",
]
