"""``threads`` / ``messages`` repository for outbound sequences (REQUIREMENTS.md INV-10, INV-12,
INV-23, XC-03).

Outbound idempotency: the caller's key (``seq:{sequence_id}:step:{n}``) is stored as the draft
row's ``external_message_id``; the same key always yields the same row. The Graph send path in
``activities/graph.py`` keys its audit events on the same value.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import (
    Channel,
    Direction,
    MessageClassification,
    PolicyDecision,
    QaVerdict,
)
from wholesaling.db.models.messaging import Message, Suppression, Thread
from wholesaling.db.models.ops import QaReview
from wholesaling.db.normalize import normalize_email
from wholesaling.policy.actions import PolicyAction

# A4 classifications that do not count as a substantive reply (INV-10 "any substantive reply").
NON_SUBSTANTIVE: frozenset[MessageClassification] = frozenset({MessageClassification.UNRELATED})


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for ``DateTime(timezone=True)``; treat them as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def local_day_start_utc(now: datetime, tz: str) -> datetime:
    """UTC instant at which the current local calendar day began in ``tz``."""
    zone = ZoneInfo(tz)
    local = (_aware(now) or now).astimezone(zone)
    return datetime.combine(local.date(), time.min, tzinfo=zone).astimezone(UTC)


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # -- lookups -------------------------------------------------------------------------------

    async def get(self, message_id: uuid.UUID) -> Message | None:
        return await self._s.get(Message, message_id)

    async def get_by_external_id(self, external_message_id: str) -> Message | None:
        stmt = select(Message).where(
            Message.channel == Channel.EMAIL, Message.external_message_id == external_message_id
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_thread(self, thread_id: uuid.UUID) -> Thread | None:
        return await self._s.get(Thread, thread_id)

    async def get_thread_for_investor(self, investor_id: uuid.UUID) -> Thread | None:
        """The investor's most recent email thread, if any."""
        stmt = (
            select(Thread)
            .where(Thread.channel == Channel.EMAIL, Thread.investor_id == investor_id)
            .order_by(Thread.created_at.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_step_message(self, sequence_id: uuid.UUID, step: int) -> Message | None:
        stmt = select(Message).where(
            Message.sequence_id == sequence_id,
            Message.sequence_step == step,
            Message.direction == Direction.OUT,
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_thread_messages(self, thread_id: uuid.UUID) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.created_at, Message.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def inbound_messages(self, thread_id: uuid.UUID) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.thread_id == thread_id, Message.direction == Direction.IN)
            .order_by(Message.created_at, Message.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    # -- threads -------------------------------------------------------------------------------

    async def create_thread(
        self,
        *,
        investor_id: uuid.UUID | None,
        counterpart_address: str,
        subject: str | None,
        external_thread_id: str | None = None,
    ) -> Thread:
        thread = Thread(
            channel=Channel.EMAIL,
            external_thread_id=external_thread_id,
            subject=subject,
            investor_id=investor_id,
            counterpart_address=counterpart_address,
        )
        self._s.add(thread)
        await self._s.flush()
        return thread

    # -- outbound drafts -----------------------------------------------------------------------

    async def create_outbound_draft(
        self,
        *,
        thread: Thread,
        to: str,
        subject: str,
        body: str,
        policy_action: PolicyAction,
        idempotency_key: str,
        from_address: str | None,
        sequence_id: uuid.UUID | None = None,
        sequence_step: int | None = None,
        meta: dict[str, object] | None = None,
    ) -> tuple[Message, bool]:
        """Write the draft row (``direction=out``, ``draft=True``) or return the existing one.

        No Graph call happens here; the draft exists in our database only until the policy
        decision creates (and possibly sends) the mailbox draft.
        """
        existing = await self.get_by_external_id(idempotency_key)
        if existing is not None:
            return existing, False
        row = Message(
            thread_id=thread.id,
            direction=Direction.OUT,
            channel=Channel.EMAIL,
            external_message_id=idempotency_key,
            from_address=from_address,
            to_addresses=[to],
            subject=subject,
            body=body,
            draft=True,
            policy_action=policy_action.value,
            sequence_id=sequence_id,
            sequence_step=sequence_step,
            meta={"idempotency_key": idempotency_key, **(meta or {})},
        )
        self._s.add(row)
        await self._s.flush()
        return row, True

    async def set_policy_decision(
        self, message_id: uuid.UUID, decision: PolicyDecision, reason: str
    ) -> Message:
        row = await self._require(message_id)
        row.policy_decision = decision
        row.meta = {**row.meta, "policy_reason": reason}
        await self._s.flush()
        return row

    async def mark_sent(
        self,
        message_id: uuid.UUID,
        *,
        sent_at: datetime,
        internet_message_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Message:
        """Stamp ``sent_at`` (idempotent: a sent row is untouched) and refresh the thread."""
        row = await self._require(message_id)
        if row.sent_at is not None:
            return row
        row.sent_at = sent_at
        row.draft = False
        extra = {
            k: v
            for k, v in {
                "internet_message_id": internet_message_id,
                "conversation_id": conversation_id,
            }.items()
            if v
        }
        row.meta = {**row.meta, **extra}
        thread = await self.get_thread(row.thread_id)
        if thread is not None:
            thread.last_message_at = sent_at
            if thread.external_thread_id is None and conversation_id:
                thread.external_thread_id = conversation_id
        await self._s.flush()
        return row

    # -- caps and gates ------------------------------------------------------------------------

    async def count_sends_today(self, action: PolicyAction, tz: str, *, now: datetime) -> int:
        """Outbound messages for ``action`` sent since local midnight in ``tz`` (daily caps).

        The day boundary is the market's local day, so a cap of 50 means 50 per local calendar
        day, not per rolling 24 hours.
        """
        start = local_day_start_utc(now, tz)
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(
                Message.direction == Direction.OUT,
                Message.policy_action == action.value,
                Message.sent_at.is_not(None),
                Message.sent_at >= start,
            )
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def is_suppressed(self, address: str) -> bool:
        try:
            normalized = normalize_email(address)
        except ValueError:
            return True  # an address we cannot normalise is never mailed
        stmt = select(Suppression).where(
            Suppression.channel == Channel.EMAIL, Suppression.normalized_value == normalized
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    async def latest_qa_verdict(self, message_id: uuid.UUID) -> QaReview | None:
        row = await self.get(message_id)
        if row is not None and row.qa_review_id is not None:
            by_id = await self._s.get(QaReview, row.qa_review_id)
            if by_id is not None:
                return by_id
        stmt = (
            select(QaReview)
            .where(QaReview.message_id == message_id)
            .order_by(QaReview.created_at.desc(), QaReview.id.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def qa_verdict_for(self, message_id: uuid.UUID) -> QaVerdict | None:
        review = await self.latest_qa_verdict(message_id)
        return review.verdict if review is not None else None

    async def thread_cancel_reason(self, thread_id: uuid.UUID) -> str | None:
        """``"opt_out"`` / ``"bounce"`` / ``"reply"`` when an inbound message on the thread
        requires the sequence to stop (INV-10, INV-23), else ``None``.

        Conservative: an inbound message A4 has not classified yet counts as a reply.
        """
        inbound = await self.inbound_messages(thread_id)
        reason: str | None = None
        for msg in inbound:
            if msg.classification is MessageClassification.OPT_OUT:
                return "opt_out"
            if msg.meta.get("is_bounce"):
                reason = reason or "bounce"
            elif msg.classification not in NON_SUBSTANTIVE:
                reason = "reply"  # a real reply outranks an earlier bounce
        return reason

    async def _require(self, message_id: uuid.UUID) -> Message:
        row = await self.get(message_id)
        if row is None:
            msg = f"message {message_id} not found"
            raise LookupError(msg)
        return row


__all__ = ["NON_SUBSTANTIVE", "MessageRepository", "local_day_start_utc"]
