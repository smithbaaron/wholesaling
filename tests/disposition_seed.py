"""Row factories for the BO-12 tests: an accepted offer with its seller lead and intake, a PSA
row, investors with buy boxes / approved D2 matches / email contacts.

Plain module (not a conftest) so the DB, agent, activity, workflow and API tests import the
same helpers. Everything writes through the session passed in and flushes; callers commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.buy_boxes import BuyBox, SearchProfile
from wholesaling.db.models.deals import (
    Contract,
    ContractStatus,
    ContractType,
    Match,
    PropertyCandidate,
)
from wholesaling.db.models.enums import (
    Actor,
    ArvConfidence,
    ConditionClass,
    ContactChannel,
    InvestorType,
    MarketTier,
    MatchReviewStatus,
    OfferStatus,
    SellerLeadStatus,
)
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.offers import Offer
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.normalize import normalize_email
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.contracts import EVENT_REQUESTED, ContractRepository

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
OPERATOR = "aaron"


@dataclass(slots=True)
class Deal:
    market: Market
    lead: SellerLead
    intake: Intake
    offer: Offer


async def seed_market(session: AsyncSession, name: str = "Houston", state: str = "TX") -> Market:
    market = Market(name=name, state=state, tier=MarketTier.ACTIVE)
    session.add(market)
    await session.flush()
    return market


async def seed_accepted_offer(
    session: AsyncSession,
    *,
    market: Market | None = None,
    address: str = "1427 Pecan Grove Dr",
    city: str = "Houston",
    postal_code: str = "77092",
    offer_price: Decimal = Decimal("185000"),
    assignment_fee: Decimal = Decimal("15000"),
    seller_email: str | None = "seller@example.com",
    status: OfferStatus = OfferStatus.ACCEPTED,
    write_requested_audit: bool = True,
) -> Deal:
    """Seller lead -> intake -> offer in ``status`` (+ the A7/S5 ``contract.requested`` audit)."""
    market = market or await seed_market(session)
    lead = SellerLead(
        market_id=market.id,
        status=SellerLeadStatus.OFFERED,
        owner_name="Maria Lopez",
        property_address_line1=address,
        property_city=city,
        property_state="TX",
        property_postal_code=postal_code,
        address_key=f"{address.lower()}|{postal_code}",
        parcel_id=f"P-{uuid.uuid4().hex[:8]}",
        contact_channels=(
            [{"channel": "email", "value": seller_email, "provenance": "web_form"}]
            if seller_email
            else []
        ),
    )
    session.add(lead)
    await session.flush()
    intake = Intake(
        seller_lead_id=lead.id,
        condition_class=ConditionClass.MEDIUM,
        property_facts={"beds": 3, "baths": 2, "sqft": 1640, "year_built": 1978},
        intake_complete=True,
    )
    session.add(intake)
    await session.flush()
    offer = Offer(
        seller_lead_id=lead.id,
        intake_id=intake.id,
        parcel_id=lead.parcel_id,
        status=status,
        arv=Decimal("310000"),
        arv_confidence=ArvConfidence.MEDIUM,
        comp_tier=1,
        repairs=Decimal("42000"),
        assignment_fee=assignment_fee,
        offer_price=offer_price,
        mao=Decimal("195000"),
        open_flags=["hoa_unknown"],
        idempotency_key=f"offer:{uuid.uuid4()}",
    )
    session.add(offer)
    await session.flush()
    if write_requested_audit and status is OfferStatus.ACCEPTED:
        await AuditRepository(session).record(
            actor=Actor.AGENT,
            actor_id="S5",
            event_type=EVENT_REQUESTED,
            entity_type="offer",
            entity_id=offer.id,
            payload={"offer_id": str(offer.id), "seller_lead_id": str(lead.id)},
            idempotency_key=f"offer:{offer.id}:contract_requested",
        )
    return Deal(market=market, lead=lead, intake=intake, offer=offer)


async def seed_psa(
    session: AsyncSession,
    deal: Deal,
    *,
    signed: bool = False,
    signed_at: datetime = NOW,
    envelope_id: str | None = "env-psa-1",
    option_period_end: datetime | None = None,
    inspection_deadline: datetime | None = None,
    closing_date: datetime | None = None,
    title_company: str | None = "Lone Star Title",
) -> Contract:
    repo = ContractRepository(session)
    row, _ = await repo.create_from_accepted_offer(
        deal.offer.id, actor=Actor.HUMAN, actor_id=OPERATOR, title_company=title_company
    )
    if signed:
        row, _ = await repo.mark_signed(
            row.id,
            signed_by=OPERATOR,
            signed_at=signed_at,
            envelope_id=envelope_id,
            actor=Actor.HUMAN,
            actor_id=OPERATOR,
        )
        await repo.set_deadlines(
            row.id,
            actor=Actor.HUMAN,
            actor_id=OPERATOR,
            option_period_end=option_period_end,
            inspection_deadline=inspection_deadline,
            closing_date=closing_date,
        )
    return row


async def seed_investor(
    session: AsyncSession,
    *,
    name: str,
    email: str | None,
    market: Market | None,
    score: int | None = 70,
    first_name: str | None = None,
    purchase_count_24m: int | None = 3,
    buy_box: dict[str, Any] | None = None,
) -> Investor:
    """An investor with an email contact and, optionally, a current buy box."""
    inv = Investor(
        canonical_name=name,
        entity_key=name.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:6],
        investor_type=InvestorType.FLIPPER,
        market_id=market.id if market else None,
        score=score,
        first_name=first_name,
        purchase_count_24m=purchase_count_24m,
    )
    session.add(inv)
    await session.flush()
    if email:
        session.add(
            Contact(
                investor_id=inv.id,
                channel=ContactChannel.EMAIL,
                value=email,
                normalized_value=normalize_email(email),
                is_primary=True,
                provenance="test",
            )
        )
        await session.flush()
    if buy_box is not None:
        box = BuyBox(
            investor_id=inv.id,
            version=1,
            is_current=True,
            is_active=True,
            geography=buy_box.get("geography", {}),
            asset_types=buy_box.get("asset_types", ["sfr"]),
            price_min=buy_box.get("price_min"),
            price_max=buy_box.get("price_max"),
            raw_text="test buy box",
        )
        session.add(box)
        await session.flush()
    return inv


async def current_buy_box(session: AsyncSession, investor: Investor) -> BuyBox:
    from sqlalchemy import select

    stmt = select(BuyBox).where(BuyBox.investor_id == investor.id, BuyBox.is_current.is_(True))
    return (await session.execute(stmt)).scalar_one()


async def seed_approved_match(
    session: AsyncSession,
    *,
    investor: Investor,
    deal: Deal,
    review_status: MatchReviewStatus = MatchReviewStatus.APPROVED,
    score: Decimal = Decimal("0.9"),
) -> Match:
    """A D2 match of the deal's property against the investor's current buy box."""
    box = await current_buy_box(session, investor)
    profile = SearchProfile(buy_box_id=box.id, version=1, criteria={"zips": ["77092"]})
    session.add(profile)
    await session.flush()
    candidate = PropertyCandidate(
        market_id=deal.market.id,
        source="a7_contract",
        external_id=f"lead:{deal.lead.id}:{uuid.uuid4().hex[:6]}",
        retrieved_at=NOW,
        address_line1=deal.lead.property_address_line1,
        city=deal.lead.property_city,
        state="TX",
        postal_code=deal.lead.property_postal_code,
        address_key=deal.lead.address_key,
        parcel_id=deal.lead.parcel_id,
        seller_lead_id=deal.lead.id,
        list_price=deal.offer.offer_price,
    )
    session.add(candidate)
    await session.flush()
    match = Match(
        search_profile_id=profile.id,
        property_candidate_id=candidate.id,
        score=score,
        confidence=Decimal("0.8"),
        evidence={"reason": "zip + price band", "uncertainty": "repairs unverified"},
        review_status=review_status,
        reviewed_by=OPERATOR if review_status is not MatchReviewStatus.PENDING else None,
        reviewed_at=NOW if review_status is not MatchReviewStatus.PENDING else None,
    )
    session.add(match)
    await session.flush()
    return match


async def seed_closed_deal_for(
    session: AsyncSession, investor: Investor, market: Market
) -> Contract:
    """A prior closed PSA with ``investor`` as the buyer (tier ordering: closed deals first)."""
    deal = await seed_accepted_offer(
        session,
        market=market,
        address=f"{uuid.uuid4().int % 9000 + 100} Old Deal St",
        postal_code="77008",
    )
    row = await seed_psa(session, deal, signed=True, envelope_id=None)
    repo = ContractRepository(session)
    await repo.select_buyer(row.id, investor_id=investor.id, actor=Actor.HUMAN, actor_id=OPERATOR)
    row, _ = await repo.mark_closed(
        row.id,
        realized_assignment_fee=Decimal("12000"),
        closed_at=NOW,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    assert row.status is ContractStatus.CLOSED and row.type is ContractType.PSA
    return row


__all__ = [
    "NOW",
    "OPERATOR",
    "Deal",
    "current_buy_box",
    "seed_accepted_offer",
    "seed_approved_match",
    "seed_closed_deal_for",
    "seed_investor",
    "seed_market",
    "seed_psa",
]
