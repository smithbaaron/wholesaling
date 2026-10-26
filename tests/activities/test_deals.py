"""D1 / D2 activities on SQLite (BB-04..BB-09, BB-12, WF-07)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.activities.offers_fixtures import (
    OfferHarness,
    new_key,
    seed_market,
    seed_seller,
    set_level,
)
from tests.db.conftest import MakeInvestor
from wholesaling.activities import deals as dacts
from wholesaling.activities import offers as oacts
from wholesaling.agents.d2 import SOURCE_A7_CONTRACT
from wholesaling.db.models.deals import Match, PropertyCandidate
from wholesaling.db.models.enums import MatchReviewStatus, SearchProfileStatus
from wholesaling.db.models.ops import AuditEvent
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria, BuyBoxRepository
from wholesaling.db.repositories.deals import CandidateRepository, SearchProfileRepository
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

pytestmark = pytest.mark.usefixtures("offer_harness")

CRITERIA = BuyBoxCriteria(
    geography={"markets": ["Houston, TX"], "zips": ["77008", "77018", "77055", "77007"]},
    asset_types=["single family"],
    price_min=Decimal(100000),
    price_max=Decimal(250000),
    condition="heavy ok",
    raw_text="SFR in Houston 100-250k, heavy rehab fine",
)


async def _box(
    session: AsyncSession, make_investor: MakeInvestor, criteria: BuyBoxCriteria = CRITERIA
) -> tuple[uuid.UUID, uuid.UUID, int]:
    inv = await make_investor("Deal Hunters LLC")
    res = await BuyBoxRepository(session).create_version(inv.id, criteria)
    await session.commit()
    return res.buy_box.id, inv.id, res.buy_box.version


async def test_run_d1_creates_one_profile_and_queues_exactly_one_search(
    session: AsyncSession, make_investor: MakeInvestor, offer_harness: OfferHarness
) -> None:
    box_id, inv_id, version = await _box(session, make_investor)
    out = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), version, "buy_box.created"))
    assert out.created and not out.coalesced and out.profile_version == 1
    assert (
        out.search_workflow_id == f"buy-box-search:{box_id}:v1"
        and out.superseded_workflow_id is None
    )
    starts = [c for c in offer_harness.temporal.calls if c.method == "start_workflow"]
    assert len(starts) == 1 and starts[0].args["workflow"] == "BuyBoxSearch"
    assert starts[0].args["args"][0].search_profile_id == out.search_profile_id
    profiles = await SearchProfileRepository(session).list_versions(box_id)
    assert len(profiles) == 1 and profiles[0].status is SearchProfileStatus.ACTIVE
    assert (
        profiles[0].criteria["property_classes"] == ["sfr"]
        and profiles[0].criteria["condition_tolerance"] == "heavy"
    )
    events = [e.event_type for e in (await session.execute(select(AuditEvent))).scalars().all()]
    assert events.count("search_profile.created") == 1 and events.count("search.queued") == 1


async def test_run_d1_coalesces_an_unchanged_revision_and_supersedes_a_changed_one(
    session: AsyncSession, make_investor: MakeInvestor, offer_harness: OfferHarness
) -> None:
    box_id, inv_id, _ = await _box(session, make_investor)
    first = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1))
    # Same criteria restated (new buy-box version with different wording): coalesced, no search.
    boxes = BuyBoxRepository(session)
    reworded = await boxes.create_version(
        inv_id, BuyBoxCriteria(raw_text="different words"), mode="merge"
    )
    assert reworded.created is False  # structured equality -> no new buy-box version either
    again = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1, "buy_box.updated"))
    assert (
        again.coalesced
        and again.search_profile_id == first.search_profile_id
        and again.search_workflow_id is None
    )
    assert len([c for c in offer_harness.temporal.calls if c.method == "start_workflow"]) == 1
    # A meaningful change -> version 2, the v1 search is signalled superseded, one new search.
    changed = await boxes.create_version(
        inv_id, BuyBoxCriteria(price_max=Decimal(300000)), mode="merge"
    )
    await session.commit()
    assert changed.created
    out = await dacts.run_d1(
        dacts.RunD1Input(
            str(changed.buy_box.id), str(inv_id), changed.buy_box.version, "buy_box.updated"
        )
    )
    # The new buy-box version is a new row, so D1 keys profiles on it; the old row's profile stays
    # active for its own row (history), and this row gets its own v1 profile + one search.
    assert out.created and out.profile_version == 1
    starts = [c for c in offer_harness.temporal.calls if c.method == "start_workflow"]
    assert len(starts) == 2


async def test_run_d1_supersedes_within_the_same_buy_box_row(
    session: AsyncSession, make_investor: MakeInvestor, offer_harness: OfferHarness
) -> None:
    box_id, inv_id, _ = await _box(session, make_investor)
    first = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1))
    # Simulate a correction written onto the same row (a re-extraction updating price_max).
    from wholesaling.db.models.buy_boxes import BuyBox

    row = await session.get(BuyBox, box_id)
    assert row is not None
    row.price_max = Decimal(275000)
    await session.commit()
    out = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1, "buy_box.updated"))
    assert out.created and out.profile_version == 2
    assert out.superseded_workflow_id == f"buy-box-search:{box_id}:v1"
    assert out.search_workflow_id == f"buy-box-search:{box_id}:v2"
    signals = offer_harness.temporal.signals[f"buy-box-search:{box_id}:v1"]
    assert signals[0][0] == "superseded" and signals[0][1][0].new_version == 2
    profiles = await SearchProfileRepository(session).list_versions(box_id)
    assert [p.status for p in profiles] == [
        SearchProfileStatus.SUPERSEDED,
        SearchProfileStatus.ACTIVE,
    ]
    assert profiles[0].id == uuid.UUID(first.search_profile_id)


async def test_run_d2_writes_ranked_pending_matches_with_evidence(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    box_id, inv_id, _ = await _box(session, make_investor)
    d1 = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1))
    out = await dacts.run_d2(dacts.RunD2Input(search_profile_id=d1.search_profile_id))
    assert out.skipped_reason is None and out.candidates_seen == 8 and out.duplicates_dropped == 1
    assert out.matches_written == len(out.match_ids) >= 3
    matches = await CandidateRepository(session).list_matches(uuid.UUID(d1.search_profile_id))
    assert {str(m.id) for m in matches} == set(out.match_ids)  # ties in score may reorder
    assert all(m.review_status is MatchReviewStatus.PENDING for m in matches)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)  # type: ignore[type-var]
    top = next(m for m in matches if m.evidence["rank"] == 1)
    assert top.score == matches[0].score and top.evidence["review_status"] == "pending"
    for key in (
        "source",
        "retrieved_at",
        "match_logic",
        "missing_fields",
        "source_restrictions",
        "freshness",
        "valuation_uncertainty",
        "next_review_action",
        "reason",
    ):
        assert key in top.evidence
    candidate = await session.get(PropertyCandidate, top.property_candidate_id)
    assert (
        candidate is not None
        and candidate.est_arv is not None
        and candidate.est_fees == Decimal(10000)
    )
    assert candidate.liquidity_score is not None and candidate.parcel_id
    # Flags from the data: the HOA listing in 77018 is in profile and flagged.
    flagged = {
        c.external_id: c.flags
        for c in (await session.execute(select(PropertyCandidate))).scalars().all()
    }
    assert "hoa" in flagged["L-003"]
    assert "L-008" not in flagged  # Dallas never became a candidate row
    events = [e.event_type for e in (await session.execute(select(AuditEvent))).scalars().all()]
    assert events.count("match.created") == out.matches_written and "search.completed" in events


async def test_run_d2_is_idempotent_and_keeps_human_review_status(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    box_id, inv_id, _ = await _box(session, make_investor)
    d1 = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1))
    first = await dacts.run_d2(dacts.RunD2Input(d1.search_profile_id))
    repo = CandidateRepository(session)
    matches = await repo.list_matches(uuid.UUID(d1.search_profile_id))
    from datetime import UTC, datetime

    await repo.review(
        matches[0].id,
        status=MatchReviewStatus.APPROVED,
        reviewed_by="aaron",
        at=datetime.now(tz=UTC),
    )
    await session.commit()
    second = await dacts.run_d2(dacts.RunD2Input(d1.search_profile_id))
    assert second.match_ids == first.match_ids
    assert len((await session.execute(select(Match))).scalars().all()) == first.matches_written
    reviewed = await session.get(Match, matches[0].id)
    assert reviewed is not None and reviewed.review_status is MatchReviewStatus.APPROVED
    events = [e.event_type for e in (await session.execute(select(AuditEvent))).scalars().all()]
    assert events.count("match.created") == first.matches_written  # no duplicates on re-run


async def test_run_d2_skips_a_superseded_profile(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    box_id, inv_id, _ = await _box(session, make_investor)
    d1 = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1))
    profile = await SearchProfileRepository(session).get(uuid.UUID(d1.search_profile_id))
    assert profile is not None
    profile.status = SearchProfileStatus.SUPERSEDED
    await session.commit()
    out = await dacts.run_d2(dacts.RunD2Input(d1.search_profile_id))
    assert out.skipped_reason == "profile is superseded" and out.matches_written == 0


async def test_run_d2_includes_off_market_candidates_from_accepted_offers(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    # An accepted A7 offer on the clean subject becomes an off-market candidate (BB-12).
    await set_level(session, PolicyAction.OFFER_SEND, AutonomyLevel.L1)
    market = await seed_market(session)
    lead, intake = await seed_seller(session, market=market)
    await session.commit()
    computed = await oacts.compute_offer(
        oacts.ComputeOfferInput(str(intake.id), str(lead.id), new_key())
    )
    await oacts.qa_offer_draft(oacts.QaOfferInput(offer_id=computed.offer_id))
    await oacts.decide_and_send_offer(oacts.DecideOfferInput(offer_id=computed.offer_id))
    await oacts.accept_offer(oacts.AcceptOfferInput(offer_id=computed.offer_id, source="seller"))

    box_id, inv_id, _ = await _box(session, make_investor)
    d1 = await dacts.run_d1(dacts.RunD1Input(str(box_id), str(inv_id), 1))
    out = await dacts.run_d2(dacts.RunD2Input(d1.search_profile_id))
    assert out.candidates_seen == 9  # 8 listings in the profile area + the off-market offer
    rows = (
        (
            await session.execute(
                select(PropertyCandidate).where(PropertyCandidate.source == SOURCE_A7_CONTRACT)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1 and rows[0].seller_lead_id == lead.id and rows[0].parcel_id == "HC-0001"
    assert rows[0].est_arv == Decimal(240161) and rows[0].est_repairs == Decimal(60000)
    assert rows[0].list_price == Decimal(88500)
    match = (
        (await session.execute(select(Match).where(Match.property_candidate_id == rows[0].id)))
        .scalars()
        .one()
    )
    assert "human signature" in match.evidence["seller_intent"]
    assert match.evidence["estimates"]["mao"] == "95169.32"
