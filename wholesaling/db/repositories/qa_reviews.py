"""``qa_reviews``: A5 verdicts on outbound drafts (REQUIREMENTS.md INV-17, INV-18).

One row per review. ``record`` also stamps ``messages.qa_review_id`` so the review queue and
the policy engine find the latest verdict without a join (the column is deliberately not a
DB-level FK, see ``db/models/messaging.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import QaVerdict
from wholesaling.db.models.messaging import Message
from wholesaling.db.models.ops import QaReview


class QaReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        *,
        verdict: QaVerdict,
        confidence: float | Decimal | None,
        flags: Sequence[str] = (),
        rationale: str | None = None,
        rewrite_body: str | None = None,
        model: str | None = None,
        agent_run_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        offer_id: uuid.UUID | None = None,
    ) -> QaReview:
        """Append one review and, for a message, link ``messages.qa_review_id`` to it."""
        if message_id is None and offer_id is None:
            msg = "a QA review needs a message_id or an offer_id"
            raise ValueError(msg)
        row = QaReview(
            message_id=message_id,
            offer_id=offer_id,
            verdict=verdict,
            confidence=(
                None
                if confidence is None
                else (confidence if isinstance(confidence, Decimal) else Decimal(str(confidence)))
            ),
            flags=list(flags),
            rewrite_body=rewrite_body,
            rationale=rationale,
            model=model,
            agent_run_id=agent_run_id,
        )
        self._s.add(row)
        await self._s.flush()
        if message_id is not None:
            message = await self._s.get(Message, message_id)
            if message is not None:
                message.qa_review_id = row.id
                await self._s.flush()
        return row

    async def get(self, review_id: uuid.UUID) -> QaReview | None:
        return await self._s.get(QaReview, review_id)

    async def for_message(self, message_id: uuid.UUID) -> list[QaReview]:
        stmt = (
            select(QaReview)
            .where(QaReview.message_id == message_id)
            .order_by(QaReview.created_at, QaReview.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def latest_for_message(self, message_id: uuid.UUID) -> QaReview | None:
        rows = await self.for_message(message_id)
        return rows[-1] if rows else None


__all__ = ["QaReviewRepository"]
