"""The human review queue (REQUIREMENTS.md BO-07 "review queue page", POL-01, INV-17/18).

A queued item is a ``messages`` row with ``draft = True`` and ``policy_decision =
'queue_for_human'``. The policy engine put it there; the owning Temporal workflow is parked on
the ``human_decision`` signal waiting for the operator. This repository reads the queue (with its
QA review, thread, investor and market) and resolves which workflow to signal:

- sequence steps: ``sequences.temporal_workflow_id`` via ``messages.sequence_id``;
- inbound-reply drafts (no sequence): ``messages.meta["temporal_workflow_id"]`` when the A4
  builder stored it there, otherwise the ``audit_events`` row with ``entity_type = 'message'``,
  ``entity_id = <message id>`` and ``event_type = 'inbound.workflow_started'`` whose payload
  carries ``workflow_id`` (or ``temporal_workflow_id``).

Flipping the row (``send`` / ``blocked``) is the API route's job and happens only *after* the
signal succeeded.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import PolicyDecision, QaVerdict
from wholesaling.db.models.investors import Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Sequence, Thread
from wholesaling.db.models.ops import AuditEvent, QaReview

INBOUND_WORKFLOW_STARTED = "inbound.workflow_started"
HUMAN_DECISION_SIGNAL = "human_decision"
META_WORKFLOW_ID_KEY = "temporal_workflow_id"


@dataclass(slots=True)
class HumanDecision:
    """Payload of the ``human_decision`` signal (contract shared with the workflow builders).

    Temporal's default converter serialises this as a JSON object keyed by field name, so the
    workflow side may declare its own dataclass with the same fields.
    """

    approved: bool
    edited_body: str | None
    step: int | None
    message_id: str
    actor_id: str | None = None  # operator username; optional so the 4-field contract still holds


@dataclass(slots=True)
class QueuedItem:
    """One review-queue row with everything the page shows."""

    message_id: uuid.UUID
    thread_id: uuid.UUID
    action: str
    recipient: str
    subject: str | None
    body: str
    policy_reason: str | None
    created_at: datetime
    sequence_id: uuid.UUID | None
    sequence_step: int | None
    sequence_workflow_id: str | None
    qa_verdict: QaVerdict | None
    qa_confidence: Decimal | None
    qa_flags: list[str] = field(default_factory=list)
    qa_rationale: str | None = None
    investor_name: str | None = None
    market_id: uuid.UUID | None = None
    market_name: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(tz=UTC)
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return max((now - created).total_seconds(), 0.0)

    @property
    def body_preview(self) -> str:
        text = " ".join(self.body.split())
        return text if len(text) <= 240 else text[:237] + "..."

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "message_id": str(self.message_id),
            "thread_id": str(self.thread_id),
            "action": self.action,
            "recipient": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "body_preview": self.body_preview,
            "policy_reason": self.policy_reason,
            "created_at": self.created_at.isoformat(),
            "age_seconds": int(self.age_seconds(now)),
            "sequence_id": str(self.sequence_id) if self.sequence_id else None,
            "sequence_step": self.sequence_step,
            "workflow_id": self.sequence_workflow_id,
            "qa": {
                "verdict": self.qa_verdict.value if self.qa_verdict else None,
                "confidence": float(self.qa_confidence) if self.qa_confidence is not None else None,
                "flags": list(self.qa_flags),
                "rationale": self.qa_rationale,
            },
            "investor_name": self.investor_name,
            "market_id": str(self.market_id) if self.market_id else None,
            "market_name": self.market_name,
        }


def _recipient(message: Message, thread: Thread) -> str:
    if message.to_addresses:
        return ", ".join(message.to_addresses)
    return thread.counterpart_address or "(unknown recipient)"


class ReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def _queue_stmt(self) -> Any:
        return (
            select(Message, Thread, QaReview, Sequence, Investor, Market)
            .join(Thread, Thread.id == Message.thread_id)
            .outerjoin(QaReview, QaReview.id == Message.qa_review_id)
            .outerjoin(Sequence, Sequence.id == Message.sequence_id)
            .outerjoin(Investor, Investor.id == Thread.investor_id)
            .outerjoin(Market, Market.id == Investor.market_id)
            .where(
                Message.draft.is_(True),
                Message.policy_decision == PolicyDecision.QUEUE_FOR_HUMAN,
            )
        )

    @staticmethod
    def _to_item(row: Any) -> QueuedItem:
        message, thread, qa, sequence, investor, market = row
        return QueuedItem(
            message_id=message.id,
            thread_id=message.thread_id,
            action=message.policy_action or "unknown",
            recipient=_recipient(message, thread),
            subject=message.subject or thread.subject,
            body=message.body,
            policy_reason=(message.meta or {}).get("policy_reason"),
            created_at=message.created_at,
            sequence_id=message.sequence_id,
            sequence_step=message.sequence_step,
            sequence_workflow_id=sequence.temporal_workflow_id if sequence else None,
            qa_verdict=qa.verdict if qa else None,
            qa_confidence=qa.confidence if qa else None,
            qa_flags=list(qa.flags or []) if qa else [],
            qa_rationale=qa.rationale if qa else None,
            investor_name=investor.canonical_name if investor else None,
            market_id=market.id if market else None,
            market_name=f"{market.name}, {market.state}" if market else None,
            meta=dict(message.meta or {}),
        )

    async def list_queue(
        self, *, action: str | None = None, market_id: uuid.UUID | None = None
    ) -> list[QueuedItem]:
        stmt = self._queue_stmt().order_by(Message.created_at, Message.id)
        if action:
            stmt = stmt.where(Message.policy_action == action)
        if market_id is not None:
            stmt = stmt.where(Market.id == market_id)
        rows = (await self._s.execute(stmt)).all()
        return [self._to_item(r) for r in rows]

    async def get_item(self, message_id: uuid.UUID) -> QueuedItem | None:
        stmt = self._queue_stmt().where(Message.id == message_id)
        row = (await self._s.execute(stmt)).first()
        return self._to_item(row) if row is not None else None

    async def get_message(self, message_id: uuid.UUID) -> Message | None:
        return await self._s.get(Message, message_id)

    async def queue_depth(self) -> int:
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(
                Message.draft.is_(True), Message.policy_decision == PolicyDecision.QUEUE_FOR_HUMAN
            )
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def oldest_queued_at(self) -> datetime | None:
        stmt = select(func.min(Message.created_at)).where(
            Message.draft.is_(True), Message.policy_decision == PolicyDecision.QUEUE_FOR_HUMAN
        )
        value = (await self._s.execute(stmt)).scalar_one_or_none()
        if value is None:
            return None
        if isinstance(value, str):  # SQLite min() over a datetime column returns text
            value = datetime.fromisoformat(value)
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def markets_in_queue(self) -> list[Market]:
        """Markets that currently have at least one queued item (for the filter dropdown)."""
        stmt = (
            select(Market)
            .join(Investor, Investor.market_id == Market.id)
            .join(Thread, Thread.investor_id == Investor.id)
            .join(Message, Message.thread_id == Thread.id)
            .where(
                Message.draft.is_(True),
                Message.policy_decision == PolicyDecision.QUEUE_FOR_HUMAN,
            )
            .distinct()
            .order_by(Market.name)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def resolve_workflow_id(self, message: Message) -> str | None:
        """The Temporal workflow waiting on this message (see module docstring)."""
        if message.sequence_id is not None:
            sequence = await self._s.get(Sequence, message.sequence_id)
            if sequence is not None and sequence.temporal_workflow_id:
                return sequence.temporal_workflow_id
        meta_id = (message.meta or {}).get(META_WORKFLOW_ID_KEY)
        if isinstance(meta_id, str) and meta_id:
            return meta_id
        stmt = (
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "message",
                AuditEvent.entity_id == message.id,
                AuditEvent.event_type == INBOUND_WORKFLOW_STARTED,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        )
        event = (await self._s.execute(stmt)).scalars().first()
        if event is None:
            return None
        payload = event.payload or {}
        for key in ("workflow_id", META_WORKFLOW_ID_KEY):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    async def apply_decision(
        self,
        message: Message,
        *,
        approved: bool,
        edited_body: str | None,
        operator: str,
        reason: str | None,
        decided_at: datetime,
    ) -> Message:
        """Flip the row after the signal succeeded. Never call this before signalling."""
        message.policy_decision = PolicyDecision.SEND if approved else PolicyDecision.BLOCKED
        if approved and edited_body is not None:
            message.body = edited_body
        meta = dict(message.meta or {})
        meta["human_decision"] = {
            "approved": approved,
            "edited": bool(approved and edited_body is not None),
            "operator": operator,
            "reason": reason,
            "decided_at": decided_at.isoformat(),
        }
        message.meta = meta
        await self._s.flush()
        return message


__all__ = [
    "HUMAN_DECISION_SIGNAL",
    "INBOUND_WORKFLOW_STARTED",
    "META_WORKFLOW_ID_KEY",
    "HumanDecision",
    "QueuedItem",
    "ReviewRepository",
]
