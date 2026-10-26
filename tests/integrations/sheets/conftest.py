"""Seed data covering every projected table, shared by the projection, activity and workflow
tests. Everything is deterministic (fixed UUIDs and timestamps) so golden rows can be asserted."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models import (
    BuyBox,
    Contact,
    Investor,
    Market,
    Match,
    Message,
    PropertyCandidate,
    QaReview,
    SearchProfile,
    Suppression,
    Thread,
)
from wholesaling.db.models.enums import (
    Channel,
    ContactChannel,
    Direction,
    InvestorType,
    MarketTier,
    MatchReviewStatus,
    MessageClassification,
    PolicyDecision,
    QaVerdict,
    SearchProfileStatus,
)
from wholesaling.db.normalize import normalize_entity_name

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
T1 = datetime(2026, 10, 24, 15, 45, tzinfo=UTC)

MARKET_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
INVESTOR_ID = uuid.UUID("00000000-0000-4000-8000-000000000010")
INVESTOR2_ID = uuid.UUID("00000000-0000-4000-8000-000000000011")
CONTACT_ID = uuid.UUID("00000000-0000-4000-8000-000000000020")
THREAD_ID = uuid.UUID("00000000-0000-4000-8000-000000000030")
MSG_IN_ID = uuid.UUID("00000000-0000-4000-8000-000000000031")
MSG_OUT_ID = uuid.UUID("00000000-0000-4000-8000-000000000032")
MSG_DRAFT_ID = uuid.UUID("00000000-0000-4000-8000-000000000033")
BB_V1_ID = uuid.UUID("00000000-0000-4000-8000-000000000040")
BB_V2_ID = uuid.UUID("00000000-0000-4000-8000-000000000041")
BB_INACTIVE_ID = uuid.UUID("00000000-0000-4000-8000-000000000042")
PROFILE_ID = uuid.UUID("00000000-0000-4000-8000-000000000050")
PROPERTY_ID = uuid.UUID("00000000-0000-4000-8000-000000000060")
MATCH_ID = uuid.UUID("00000000-0000-4000-8000-000000000070")
SUPPRESSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000080")
QA_ID = uuid.UUID("00000000-0000-4000-8000-000000000090")

INBOUND_EXTERNAL_ID = "<inbound-1@investor.test>"


@dataclass(slots=True)
class Seed:
    investor: Investor
    investor2: Investor
    contact: Contact
    buy_box_v1: BuyBox
    buy_box_v2: BuyBox
    buy_box_inactive: BuyBox
    profile: SearchProfile
    prop: PropertyCandidate
    match: Match
    thread: Thread
    msg_in: Message
    msg_out: Message
    msg_draft: Message
    suppression: Suppression
    qa: QaReview


async def seed_projection(session: AsyncSession) -> Seed:
    market = Market(
        id=MARKET_ID,
        name="Houston",
        state="TX",
        tier=MarketTier.ACTIVE,
        timezone="America/Chicago",
        created_at=T0,
        updated_at=T0,
    )
    session.add(market)
    await session.flush()
    inv = Investor(
        id=INVESTOR_ID,
        canonical_name="Acme Holdings LLC",
        entity_key=normalize_entity_name("Acme Holdings LLC"),
        aliases=["Acme Holdings LLC", "ACME HOLDINGS"],
        investor_type=InvestorType.FLIPPER,
        is_entity=True,
        score=82,
        market_id=MARKET_ID,
        mailing_address_line1="1 Main St",
        mailing_city="Houston",
        mailing_state="TX",
        mailing_postal_code="77009",
        first_name="Alex",
        purchase_count_24m=4,
        last_purchase_at=T0,
        created_at=T0,
        updated_at=T1,
    )
    inv2 = Investor(
        id=INVESTOR2_ID,
        canonical_name="Blocked Buyers Inc",
        entity_key=normalize_entity_name("Blocked Buyers Inc"),
        aliases=["Blocked Buyers Inc"],
        excluded_reason="institutional",
        created_at=T0,
        updated_at=T0,
    )
    session.add_all([inv, inv2])
    await session.flush()

    contact = Contact(
        id=CONTACT_ID,
        investor_id=INVESTOR_ID,
        channel=ContactChannel.EMAIL,
        value="Alex@Acme.test",
        normalized_value="alex@acme.test",
        provenance="county_deed",
        confidence=Decimal("0.9000"),
        is_primary=True,
        created_at=T0,
        updated_at=T0,
    )
    thread = Thread(
        id=THREAD_ID,
        channel=Channel.EMAIL,
        external_thread_id="conv-1",
        subject="Houston deals",
        investor_id=INVESTOR_ID,
        counterpart_address="alex@acme.test",
        last_message_at=T1,
        created_at=T0,
        updated_at=T1,
    )
    session.add_all([contact, thread])
    await session.flush()

    msg_out = Message(
        id=MSG_OUT_ID,
        thread_id=THREAD_ID,
        direction=Direction.OUT,
        channel=Channel.EMAIL,
        external_message_id="seq:step0",
        from_address="deals@example.test",
        to_addresses=["alex@acme.test"],
        subject="Houston deals",
        body="Hi Alex",
        sent_at=T0,
        sequence_step=0,
        created_at=T0,
        updated_at=T0,
    )
    msg_in = Message(
        id=MSG_IN_ID,
        thread_id=THREAD_ID,
        direction=Direction.IN,
        channel=Channel.EMAIL,
        external_message_id=INBOUND_EXTERNAL_ID,
        from_address="alex@acme.test",
        to_addresses=["deals@example.test"],
        subject="RE: Houston deals",
        body="SFR in 77009 under 250k, cash, light rehab",
        classification=MessageClassification.BUY_BOX_SUPPLIED,
        received_at=T1,
        created_at=T1,
        updated_at=T1,
    )
    msg_draft = Message(
        id=MSG_DRAFT_ID,
        thread_id=THREAD_ID,
        direction=Direction.OUT,
        channel=Channel.EMAIL,
        external_message_id="reply:draft",
        from_address="deals@example.test",
        to_addresses=["alex@acme.test"],
        subject="RE: Houston deals",
        body="Thanks Alex",
        draft=True,
        policy_decision=PolicyDecision.QUEUE_FOR_HUMAN,
        created_at=T1,
        updated_at=T1,
    )
    session.add_all([msg_out, msg_in, msg_draft])
    await session.flush()

    bb1 = BuyBox(
        id=BB_V1_ID,
        investor_id=INVESTOR_ID,
        buy_box_key="default",
        version=1,
        is_current=False,
        is_active=True,
        geography={"markets": ["Houston"], "zips": ["77009"]},
        asset_types=["sfr"],
        price_max=Decimal("300000.00"),
        raw_text="SFR under 300k",
        created_at=T0,
        updated_at=T0,
    )
    bb2 = BuyBox(
        id=BB_V2_ID,
        investor_id=INVESTOR_ID,
        buy_box_key="default",
        version=2,
        is_current=True,
        is_active=True,
        geography={"markets": ["Houston"], "cities": ["Houston", "Pasadena"], "zips": ["77009"]},
        asset_types=["sfr", "duplex"],
        price_min=Decimal("100000.00"),
        price_max=Decimal("250000.00"),
        condition="light",
        strategy="flip",
        financing="cash",
        closing_preferences={"days": 14},
        other_criteria={"no_hoa": True},
        unknown_fields=["financing_proof"],
        raw_text="SFR in 77009 under 250k, cash, light rehab\nActually min 100k.",
        source_message_id=MSG_IN_ID,
        source_ref=f"message:{MSG_IN_ID}",
        change_note="correction: price_max",
        created_at=T1,
        updated_at=T1,
    )
    bb_inactive = BuyBox(
        id=BB_INACTIVE_ID,
        investor_id=INVESTOR_ID,
        buy_box_key="multifamily",
        version=1,
        is_current=True,
        is_active=False,
        geography={"markets": ["Dallas"]},
        asset_types=["multifamily"],
        created_at=T0,
        updated_at=T1,
    )
    session.add_all([bb1, bb2, bb_inactive])
    await session.flush()

    profile = SearchProfile(
        id=PROFILE_ID,
        buy_box_id=BB_V2_ID,
        version=1,
        criteria={"zips": ["77009"], "price_max": 250000},
        unknowns=["financing_proof"],
        status=SearchProfileStatus.ACTIVE,
        criteria_hash="abc123",
        created_at=T1,
        updated_at=T1,
    )
    prop = PropertyCandidate(
        id=PROPERTY_ID,
        market_id=MARKET_ID,
        source="mls_mock",
        external_id="L-1",
        source_url="https://example.test/L-1",
        retrieved_at=T1,
        address_line1="12 Elm St",
        city="Houston",
        state="TX",
        postal_code="77009",
        parcel_id="P-1",
        list_price=Decimal("199000.00"),
        est_arv=Decimal("280000.00"),
        est_repairs=Decimal("35000.50"),
        liquidity_score=Decimal("0.7500"),
        flags=["hoa_unknown"],
        created_at=T1,
        updated_at=T1,
    )
    session.add_all([profile, prop])
    await session.flush()

    match = Match(
        id=MATCH_ID,
        search_profile_id=PROFILE_ID,
        property_candidate_id=PROPERTY_ID,
        score=Decimal("0.8100"),
        confidence=Decimal("0.6000"),
        evidence={"reason": "zip and price match", "next_action": "pull comps"},
        review_status=MatchReviewStatus.PENDING,
        created_at=T1,
        updated_at=T1,
    )
    suppression = Suppression(
        id=SUPPRESSION_ID,
        channel=Channel.EMAIL,
        normalized_value="noreply@gone.test",
        reason="bounce",
        source_message_id=MSG_IN_ID,
        created_at=T1,
    )
    qa = QaReview(
        id=QA_ID,
        message_id=MSG_DRAFT_ID,
        verdict=QaVerdict.APPROVE,
        confidence=Decimal("0.9500"),
        flags=[],
        rationale="No prohibited claims.",
        model="mock-model",
        created_at=T1,
        updated_at=T1,
    )
    session.add_all([match, suppression, qa])
    await session.flush()
    await session.commit()
    return Seed(
        investor=inv,
        investor2=inv2,
        contact=contact,
        buy_box_v1=bb1,
        buy_box_v2=bb2,
        buy_box_inactive=bb_inactive,
        profile=profile,
        prop=prop,
        match=match,
        thread=thread,
        msg_in=msg_in,
        msg_out=msg_out,
        msg_draft=msg_draft,
        suppression=suppression,
        qa=qa,
    )


__all__ = ["NOW", "Seed", "seed_projection"]
