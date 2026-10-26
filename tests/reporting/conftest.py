"""Seeded KPI fixtures for reporting tests. Uses the shared ``engine``/``session`` fixtures
(SQLite in memory, or Postgres when TEST_DATABASE_URL is set)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.buy_boxes import BuyBox, SearchProfile
from wholesaling.db.models.deals import Match, PropertyCandidate
from wholesaling.db.models.enums import (
    Actor,
    AgentId,
    Channel,
    ContactChannel,
    Direction,
    InvestorType,
    MarketTier,
    MatchReviewStatus,
    MessageClassification,
    OfferStatus,
    PolicyDecision,
    QaVerdict,
)
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Thread
from wholesaling.db.models.offers import Offer
from wholesaling.db.models.ops import AgentRun, AuditEvent, QaReview
from wholesaling.db.models.sellers import Intake, SellerLead

TZ = "America/Chicago"
# A fixed "today": 2026-10-20 local. 09:00 Chicago (CDT, UTC-5) = 14:00 UTC.
DAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)


def at(day_offset: int, hour: int = 10) -> datetime:
    """UTC instant inside local day DAY+offset (local hour -> UTC via CDT -5)."""
    base = datetime(DAY.year, DAY.month, DAY.day, hour + 5, 0, tzinfo=UTC)
    return base + timedelta(days=day_offset)


@dataclass(slots=True)
class KpiSeed:
    market: Market
    investor: Investor
    thread: Thread


async def seed_kpis(session: AsyncSession) -> KpiSeed:
    """Deterministic data: known counts today vs. earlier in the week vs. outside the window."""
    market = Market(name="Houston", state="TX", tier=MarketTier.ACTIVE)
    session.add(market)
    await session.flush()
    investor = Investor(
        canonical_name="Acme Buyers LLC",
        entity_key="acme-buyers-llc",
        investor_type=InvestorType.FLIPPER,
        market_id=market.id,
    )
    session.add(investor)
    await session.flush()

    # Contacts: 2 today, 1 three days ago, 1 ten days ago (outside 7d).
    for i, when in enumerate([at(0), at(0, 11), at(-3), at(-10)]):
        c = Contact(
            investor_id=investor.id,
            channel=ContactChannel.EMAIL,
            value=f"c{i}@example.com",
            normalized_value=f"c{i}@example.com",
        )
        c.created_at = when
        session.add(c)

    thread = Thread(
        channel=Channel.EMAIL,
        external_thread_id="conv-1",
        investor_id=investor.id,
        counterpart_address="c0@example.com",
    )
    session.add(thread)
    await session.flush()

    def msg(**kw: object) -> Message:
        m = Message(thread_id=thread.id, channel=Channel.EMAIL, **kw)  # type: ignore[arg-type]
        m.external_message_id = f"m-{uuid.uuid4().hex}"
        return m

    # Sent: 3 today (one auto-sent by policy), 2 two days ago, 1 nine days ago.
    for when, decision in [
        (at(0), PolicyDecision.SEND),
        (at(0), PolicyDecision.SEND),
        (at(0, 12), PolicyDecision.SEND),
        (at(-2), PolicyDecision.SEND),
        (at(-2), PolicyDecision.SEND),
        (at(-9), PolicyDecision.SEND),
    ]:
        m = msg(
            direction=Direction.OUT,
            draft=False,
            sent_at=when,
            policy_action="investor_initial_outreach",
            policy_decision=decision,
        )
        m.created_at = when
        session.add(m)
    # Drafts queued today: 2 (live queue); one older queued draft from 2 days ago.
    for when in [at(0), at(0, 11), at(-2)]:
        m = msg(
            direction=Direction.OUT,
            draft=True,
            policy_action="investor_initial_outreach",
            policy_decision=PolicyDecision.QUEUE_FOR_HUMAN,
        )
        m.created_at = when
        session.add(m)
    # Inbound: today 1 interested, 1 question, 1 opt-out; four days ago 1 buy_box_supplied.
    for when, cls in [
        (at(0), MessageClassification.INTERESTED),
        (at(0), MessageClassification.QUESTION),
        (at(0, 13), MessageClassification.OPT_OUT),
        (at(-4), MessageClassification.BUY_BOX_SUPPLIED),
    ]:
        m = msg(direction=Direction.IN, received_at=when, classification=cls)
        m.created_at = when
        session.add(m)
    await session.flush()

    # QA reviews: 2 today (one block), 1 five days ago.
    for when, verdict in [
        (at(0), QaVerdict.APPROVE),
        (at(0), QaVerdict.BLOCK),
        (at(-5), QaVerdict.APPROVE),
    ]:
        q = QaReview(verdict=verdict, confidence=Decimal("0.9"), flags=[], model="mock")
        q.created_at = when
        session.add(q)

    # Buy boxes: v1 (superseded) and v2 (current) today, plus one active 6 days ago, one inactive.
    bb1 = BuyBox(investor_id=investor.id, buy_box_key="default", version=1, is_current=False)
    bb1.created_at = at(0)
    bb2 = BuyBox(investor_id=investor.id, buy_box_key="default", version=2, is_current=True)
    bb2.created_at = at(0, 11)
    bb3 = BuyBox(investor_id=investor.id, buy_box_key="rentals", version=1, is_current=True)
    bb3.created_at = at(-6)
    bb4 = BuyBox(
        investor_id=investor.id, buy_box_key="old", version=1, is_current=True, is_active=False
    )
    bb4.created_at = at(0)
    session.add_all([bb1, bb2, bb3, bb4])
    await session.flush()

    profile = SearchProfile(buy_box_id=bb2.id, version=1)
    session.add(profile)
    await session.flush()
    # Matches: 2 today (1 reviewed today), 1 three days ago reviewed yesterday.
    for i, (created, reviewed) in enumerate([(at(0), at(0, 12)), (at(0), None), (at(-3), at(-1))]):
        pc = PropertyCandidate(
            market_id=market.id,
            source="mock",
            external_id=f"p{i}",
            retrieved_at=created,
            address_line1=f"{i} Main St",
        )
        session.add(pc)
        await session.flush()
        m = Match(
            search_profile_id=profile.id,
            property_candidate_id=pc.id,
            review_status=MatchReviewStatus.APPROVED if reviewed else MatchReviewStatus.PENDING,
            reviewed_at=reviewed,
        )
        m.created_at = created
        session.add(m)

    # Offers: one accepted today with a $12,000 fee; one accepted 8 days ago; one sent today.
    lead = SellerLead(market_id=market.id, property_address_line1="5 Oak St")
    session.add(lead)
    await session.flush()
    intake = Intake(seller_lead_id=lead.id)
    session.add(intake)
    await session.flush()
    for i, (when, status_, fee) in enumerate(
        [
            (at(0), OfferStatus.ACCEPTED, Decimal("12000")),
            (at(-8), OfferStatus.ACCEPTED, Decimal("9000")),
            (at(0), OfferStatus.SENT, Decimal("7000")),
        ]
    ):
        o = Offer(
            seller_lead_id=lead.id,
            intake_id=intake.id,
            status=status_,
            assignment_fee=fee,
            idempotency_key=f"offer-{i}",
        )
        o.created_at = when
        o.updated_at = when
        session.add(o)

    # Qualified-but-unreachable investors (GAP-07): 1 today, 1 three days ago, 1 twenty days ago.
    for i, when in enumerate([at(0, 10), at(-3), at(-20)]):
        ev = AuditEvent(
            actor=Actor.AGENT,
            actor_id="A2",
            event_type="investor.unreachable",
            entity_type="investor",
            entity_id=uuid.uuid4(),
            payload={"investor_id": "x", "reasons": {}},
            idempotency_key=f"unreachable-{i}",
        )
        ev.created_at = when
        session.add(ev)

    # Agent runs: $0.50 + $0.25 today, $1.00 three days ago, $5 twenty days ago.
    for when, cost in [(at(0), "0.5"), (at(0, 12), "0.25"), (at(-3), "1.0"), (at(-20), "5")]:
        r = AgentRun(agent=AgentId.A5, model="mock", cost_usd=Decimal(cost))
        r.created_at = when
        session.add(r)

    await session.commit()
    return KpiSeed(market=market, investor=investor, thread=thread)


@pytest.fixture
async def seeded(session: AsyncSession) -> KpiSeed:
    return await seed_kpis(session)
