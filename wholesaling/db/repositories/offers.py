"""Offers and their verification records (REQUIREMENTS.md A7-01, A7-08, A7-18, SEL-09, SEL-11).

``offers`` holds one row per A7 run with the full calculation record; ``offer_verifications``
holds one row per (pass, iteration). Writes are idempotent on the workflow-supplied
``idempotency_key`` (a retried ``compute_offer`` activity finds its row) and on
``(offer_id, pass_id, iteration)`` for verification rows (a re-run replaces, never duplicates).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import (
    ArvConfidence,
    AutonomyLevel,
    OfferStatus,
    VerificationPass,
    VerificationResult,
)
from wholesaling.db.models.offers import Offer, OfferVerification
from wholesaling.db.models.sellers import SellerLead

# Statuses that count as "an active offer on this parcel" for the A7-08 gate.
ACTIVE_OFFER_STATUSES: frozenset[OfferStatus] = frozenset(
    {
        OfferStatus.DRAFT,
        OfferStatus.PENDING_REVIEW,
        OfferStatus.SENT,
        OfferStatus.COUNTERED,
    }
)


class OfferRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # -- reads -----------------------------------------------------------------------------------

    async def get(self, offer_id: uuid.UUID) -> Offer | None:
        return await self._s.get(Offer, offer_id)

    async def get_by_idempotency_key(self, key: str) -> Offer | None:
        stmt = select(Offer).where(Offer.idempotency_key == key)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_by_intake(self, intake_id: uuid.UUID) -> Offer | None:
        stmt = (
            select(Offer)
            .where(Offer.intake_id == intake_id)
            .order_by(Offer.created_at.desc(), Offer.id.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_for_seller(self, seller_lead_id: uuid.UUID) -> list[Offer]:
        stmt = (
            select(Offer)
            .where(Offer.seller_lead_id == seller_lead_id)
            .order_by(Offer.created_at, Offer.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def active_parcel_ids(
        self, *, exclude_offer_id: uuid.UUID | None = None
    ) -> frozenset[str]:
        """Parcel ids with an offer in an active status (gate ``no_active_offer_on_parcel``)."""
        stmt = select(Offer.parcel_id, Offer.id).where(
            Offer.status.in_(list(ACTIVE_OFFER_STATUSES)), Offer.parcel_id.is_not(None)
        )
        rows = (await self._s.execute(stmt)).all()
        return frozenset(
            str(pid)
            for pid, oid in rows
            if pid and (exclude_offer_id is None or oid != exclude_offer_id)
        )

    async def list_by_status(self, statuses: Iterable[OfferStatus]) -> list[Offer]:
        stmt = (
            select(Offer)
            .where(Offer.status.in_(list(statuses)))
            .order_by(Offer.created_at, Offer.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_accepted_with_leads(self) -> list[tuple[Offer, SellerLead]]:
        """Accepted offers joined to their seller lead: D2's off-market source (BB-12)."""
        stmt = (
            select(Offer, SellerLead)
            .join(SellerLead, SellerLead.id == Offer.seller_lead_id)
            .where(Offer.status == OfferStatus.ACCEPTED)
            .order_by(Offer.created_at, Offer.id)
        )
        return [(o, s) for o, s in (await self._s.execute(stmt)).all()]

    # -- writes ----------------------------------------------------------------------------------

    async def create(
        self,
        *,
        seller_lead_id: uuid.UUID,
        intake_id: uuid.UUID,
        idempotency_key: str,
        parcel_id: str | None = None,
    ) -> tuple[Offer, bool]:
        """The offer row for one A7 run; a replay with the same key returns the existing row."""
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing, False
        row = Offer(
            seller_lead_id=seller_lead_id,
            intake_id=intake_id,
            parcel_id=parcel_id,
            status=OfferStatus.DRAFT,
            idempotency_key=idempotency_key,
            gates=[],
            open_flags=[],
            calc_record={},
        )
        self._s.add(row)
        await self._s.flush()
        return row, True

    async def record_calculation(
        self,
        offer: Offer,
        *,
        parcel_id: str | None,
        arv: Decimal | None,
        arv_confidence: ArvConfidence | None,
        arv_avm: Decimal | None,
        comp_tier: int | None,
        repairs: Decimal | None,
        market_pct: Decimal | None,
        assignment_fee: Decimal | None,
        closing_costs: Decimal | None,
        mao: Decimal | None,
        negotiation_room_pct: Decimal | None,
        offer_price: Decimal | None,
        gates: Sequence[dict[str, Any]],
        open_flags: Sequence[str],
        calc_record: dict[str, Any],
        draft_body: str | None,
        status: OfferStatus | None = None,
    ) -> Offer:
        offer.parcel_id = parcel_id
        offer.arv = arv
        offer.arv_confidence = arv_confidence
        offer.arv_avm = arv_avm
        offer.comp_tier = comp_tier
        offer.repairs = repairs
        offer.market_pct = market_pct
        offer.assignment_fee = assignment_fee
        offer.closing_costs = closing_costs
        offer.mao = mao
        offer.negotiation_room_pct = negotiation_room_pct
        offer.offer_price = offer_price
        offer.gates = [dict(g) for g in gates]
        offer.open_flags = list(open_flags)
        offer.calc_record = dict(calc_record)
        offer.draft_body = draft_body
        if status is not None:
            offer.status = status
        await self._s.flush()
        return offer

    async def set_status(
        self,
        offer_id: uuid.UUID,
        status: OfferStatus,
        *,
        reason: str | None = None,
        at: datetime | None = None,
    ) -> Offer:
        offer = await self._require(offer_id)
        offer.status = status
        record = dict(offer.calc_record)
        history = list(record.get("status_history") or [])
        history.append(
            {
                "status": status.value,
                "reason": reason,
                "at": at.isoformat() if at else None,
            }
        )
        record["status_history"] = history
        offer.calc_record = record
        await self._s.flush()
        return offer

    async def mark_sent(
        self,
        offer_id: uuid.UUID,
        *,
        sent_at: datetime,
        expires_at: datetime | None,
        message_id: uuid.UUID | None,
        level: AutonomyLevel,
    ) -> Offer:
        offer = await self._require(offer_id)
        if offer.sent_at is None:
            offer.sent_at = sent_at
            offer.expires_at = expires_at
            offer.sent_message_id = message_id
            offer.policy_level_at_send = level
            offer.status = OfferStatus.SENT
            await self._s.flush()
        return offer

    async def set_accepted_price(self, offer_id: uuid.UUID, price: Decimal) -> Offer:
        """Record the agreed number (a counter inside the room) on the calc record."""
        offer = await self._require(offer_id)
        record = dict(offer.calc_record)
        record["accepted_price"] = str(price)
        offer.calc_record = record
        await self._s.flush()
        return offer

    async def write_verification(
        self,
        offer_id: uuid.UUID,
        *,
        pass_id: VerificationPass,
        iteration: int,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        result: VerificationResult,
        reason: str | None,
        agent_run_id: uuid.UUID | None = None,
    ) -> tuple[OfferVerification, bool]:
        """Upsert on ``(offer_id, pass_id, iteration)``: a re-run overwrites the same row."""
        stmt = select(OfferVerification).where(
            OfferVerification.offer_id == offer_id,
            OfferVerification.pass_id == pass_id,
            OfferVerification.iteration == iteration,
        )
        existing = (await self._s.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.inputs = dict(inputs)
            existing.outputs = dict(outputs)
            existing.result = result
            existing.reason = reason
            existing.agent_run_id = agent_run_id
            await self._s.flush()
            return existing, False
        row = OfferVerification(
            offer_id=offer_id,
            pass_id=pass_id,
            iteration=iteration,
            inputs=dict(inputs),
            outputs=dict(outputs),
            result=result,
            reason=reason,
            agent_run_id=agent_run_id,
        )
        self._s.add(row)
        await self._s.flush()
        return row, True

    async def list_verifications(self, offer_id: uuid.UUID) -> list[OfferVerification]:
        stmt = (
            select(OfferVerification)
            .where(OfferVerification.offer_id == offer_id)
            .order_by(
                OfferVerification.created_at, OfferVerification.pass_id, OfferVerification.iteration
            )
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def _require(self, offer_id: uuid.UUID) -> Offer:
        offer = await self.get(offer_id)
        if offer is None:
            msg = f"offer {offer_id} not found"
            raise LookupError(msg)
        return offer


__all__ = ["ACTIVE_OFFER_STATUSES", "OfferRepository"]
