"""``sequences`` repository (REQUIREMENTS.md INV-10, WF-02, DM-07).

The row is the durable *record* of a sequence; the timers live in the Temporal workflow whose id
is ``temporal_workflow_id`` (unique). ``create`` is idempotent on that id so a workflow start
retried by the caller never produces two rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.base import utcnow
from wholesaling.db.models.enums import SequenceKind, SequenceStatus
from wholesaling.db.models.messaging import Sequence

ACTIVE_STATUSES: frozenset[SequenceStatus] = frozenset(
    {SequenceStatus.ACTIVE, SequenceStatus.PAUSED}
)


class SequenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, sequence_id: uuid.UUID) -> Sequence | None:
        return await self._s.get(Sequence, sequence_id)

    async def get_by_workflow_id(self, temporal_workflow_id: str) -> Sequence | None:
        stmt = select(Sequence).where(Sequence.temporal_workflow_id == temporal_workflow_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        kind: SequenceKind,
        temporal_workflow_id: str,
        investor_id: uuid.UUID | None = None,
        seller_lead_id: uuid.UUID | None = None,
        total_steps: int = 4,
        thread_id: uuid.UUID | None = None,
        next_send_at: datetime | None = None,
    ) -> tuple[Sequence, bool]:
        """Create a sequence, or return the existing one for ``temporal_workflow_id``.

        Returns ``(sequence, created)``. The target must match the kind (investor sequences
        need ``investor_id``; seller sequences ``seller_lead_id``), mirroring the table's CHECK.
        """
        if kind is SequenceKind.INVESTOR and (investor_id is None or seller_lead_id is not None):
            msg = "an investor sequence needs investor_id and no seller_lead_id"
            raise ValueError(msg)
        if kind is SequenceKind.SELLER and (seller_lead_id is None or investor_id is not None):
            msg = "a seller sequence needs seller_lead_id and no investor_id"
            raise ValueError(msg)
        existing = await self.get_by_workflow_id(temporal_workflow_id)
        if existing is not None:
            return existing, False
        row = Sequence(
            kind=kind,
            investor_id=investor_id,
            seller_lead_id=seller_lead_id,
            thread_id=thread_id,
            temporal_workflow_id=temporal_workflow_id,
            status=SequenceStatus.ACTIVE,
            step=0,
            total_steps=total_steps,
            next_send_at=next_send_at,
        )
        self._s.add(row)
        await self._s.flush()
        return row, True

    async def get_active_for_investor(self, investor_id: uuid.UUID) -> Sequence | None:
        """The running (active or paused) investor sequence, if any. At most one is expected."""
        stmt = (
            select(Sequence)
            .where(
                Sequence.kind == SequenceKind.INVESTOR,
                Sequence.investor_id == investor_id,
                Sequence.status.in_(list(ACTIVE_STATUSES)),
            )
            .order_by(Sequence.created_at.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def set_thread(self, sequence_id: uuid.UUID, thread_id: uuid.UUID) -> Sequence:
        row = await self._require(sequence_id)
        if row.thread_id is None:
            row.thread_id = thread_id
            await self._s.flush()
        return row

    async def mark_step(
        self,
        sequence_id: uuid.UUID,
        step: int,
        *,
        next_send_at: datetime | None = None,
    ) -> Sequence:
        """Record that ``step`` (0-based) has been handled; ``step + 1`` steps are now done.

        Completes the sequence when the last step is marked. Never moves backwards, so an
        activity replay cannot regress the counter.
        """
        row = await self._require(sequence_id)
        done = step + 1
        if done > row.step:
            row.step = done
        row.next_send_at = next_send_at
        if row.step >= row.total_steps and row.status is SequenceStatus.ACTIVE:
            row.status = SequenceStatus.COMPLETED
            row.next_send_at = None
        await self._s.flush()
        return row

    async def set_paused(self, sequence_id: uuid.UUID, paused: bool) -> Sequence:
        row = await self._require(sequence_id)
        if row.status in ACTIVE_STATUSES:
            row.status = SequenceStatus.PAUSED if paused else SequenceStatus.ACTIVE
            await self._s.flush()
        return row

    async def complete(self, sequence_id: uuid.UUID) -> Sequence:
        row = await self._require(sequence_id)
        if row.status in ACTIVE_STATUSES:
            row.status = SequenceStatus.COMPLETED
            row.next_send_at = None
            await self._s.flush()
        return row

    async def cancel(
        self, sequence_id: uuid.UUID, reason: str, *, at: datetime | None = None
    ) -> Sequence:
        """Cancel every remaining send (INV-10, WF-21). Idempotent: a cancelled row keeps its
        first reason."""
        row = await self._require(sequence_id)
        if row.status is not SequenceStatus.CANCELLED:
            row.status = SequenceStatus.CANCELLED
            row.cancel_reason = reason[:255]
            row.cancelled_at = at or utcnow()
            row.next_send_at = None
            await self._s.flush()
        return row

    async def list_due(self, now: datetime, *, limit: int = 100) -> list[Sequence]:
        """Active sequences whose ``next_send_at`` has passed (operator/digest view; the
        workflow itself does not poll this)."""
        stmt = (
            select(Sequence)
            .where(
                Sequence.status == SequenceStatus.ACTIVE,
                Sequence.next_send_at.is_not(None),
                Sequence.next_send_at <= now,
            )
            .order_by(Sequence.next_send_at)
            .limit(limit)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def _require(self, sequence_id: uuid.UUID) -> Sequence:
        row = await self.get(sequence_id)
        if row is None:
            msg = f"sequence {sequence_id} not found"
            raise LookupError(msg)
        return row


__all__ = ["ACTIVE_STATUSES", "SequenceRepository"]
