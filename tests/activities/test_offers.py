"""A7 and S5 activities on SQLite with the mock Graph / model / Temporal / property provider."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.activities.offers_fixtures import (
    SELLER_EMAIL,
    OfferHarness,
    new_key,
    seed_market,
    seed_seller,
    set_level,
)
from wholesaling.activities import offers as acts
from wholesaling.activities.graph import ALERT_EVENT
from wholesaling.agents.a7 import mao as mao_mod
from wholesaling.db.models.enums import (
    Channel,
    Direction,
    OfferStatus,
    PolicyDecision,
    QaVerdict,
    SellerLeadStatus,
    VerificationPass,
)
from wholesaling.db.models.messaging import Message, Suppression
from wholesaling.db.models.ops import AuditEvent, QaReview
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.repositories.offers import OfferRepository
from wholesaling.integrations.data.property import DEFAULT_TODAY, PropertyAddress, SoldComp
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

pytestmark = pytest.mark.usefixtures("offer_harness")


async def _events(session: AsyncSession, entity_id: uuid.UUID) -> list[str]:
    stmt = (
        select(AuditEvent.event_type)
        .where(AuditEvent.entity_id == entity_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


async def _compute(
    session: AsyncSession, **seed: object
) -> tuple[acts.ComputeOfferResult, SellerLead, Intake]:
    market = await seed_market(session)
    lead, intake = await seed_seller(session, market=market, **seed)  # type: ignore[arg-type]
    await session.commit()
    result = await acts.compute_offer(
        acts.ComputeOfferInput(
            intake_id=str(intake.id), seller_lead_id=str(lead.id), idempotency_key=new_key()
        )
    )
    return result, lead, intake


# -- compute_offer ---------------------------------------------------------------------------------


async def test_compute_offer_persists_row_verifications_and_calc_record(
    session: AsyncSession,
) -> None:
    result, _lead, intake = await _compute(session)
    assert result.status == "ready" and result.gates_passed and result.offer_price == "88500"
    assert result.arv == "240161" and result.arv_confidence == "High" and result.verifications == 5
    offers = OfferRepository(session)
    offer = await offers.get(uuid.UUID(result.offer_id))
    assert offer is not None and offer.status is OfferStatus.DRAFT
    assert offer.offer_price == Decimal(88500) and offer.mao == Decimal("95169.32")
    assert (
        offer.arv == Decimal(240161) and offer.arv_avm == Decimal(245000) and offer.comp_tier == 1
    )
    assert offer.repairs == Decimal(60000) and offer.closing_costs == Decimal("2943.38")
    assert offer.parcel_id == "HC-0001" and offer.market_pct == Decimal("0.7")
    assert len(offer.gates) == 7 and all(g["passed"] for g in offer.gates)
    assert (
        offer.calc_record["status"] == "ready"
        and offer.draft_body
        and "$88,500" in offer.draft_body
    )
    rows = await offers.list_verifications(offer.id)
    assert [(r.pass_id.value, r.iteration, r.result.value) for r in rows] == [
        ("V1", 1, "pass"),
        ("V3", 1, "pass"),
        ("V2", 1, "pass"),
        ("V4", 1, "pass"),
        ("V5", 1, "pass"),
    ]
    assert rows[1].agent_run_id is not None  # V3 used the model (Sonnet route)
    await session.refresh(intake)
    assert intake.property_facts["a7"]["sqft"] == 1600
    assert await _events(session, offer.id) == ["offer.computed"]


async def test_compute_offer_is_idempotent_on_the_key(session: AsyncSession) -> None:
    market = await seed_market(session)
    lead, intake = await seed_seller(session, market=market)
    await session.commit()
    key = new_key()
    first = await acts.compute_offer(acts.ComputeOfferInput(str(intake.id), str(lead.id), key))
    second = await acts.compute_offer(acts.ComputeOfferInput(str(intake.id), str(lead.id), key))
    assert first.offer_id == second.offer_id and second.created is False
    assert second.offer_price == first.offer_price == "88500"
    rows = await OfferRepository(session).list_verifications(uuid.UUID(first.offer_id))
    assert len(rows) == 5


async def test_compute_offer_v5_mismatch_blocks_and_alerts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wholesaling.agents.a7.engine as engine_mod

    real = mao_mod.compute_mao

    def buggy(inputs: mao_mod.MaoInputs) -> mao_mod.MaoResult:
        res = real(inputs)
        return mao_mod.MaoResult(
            res.inputs,
            res.arv_times_pct,
            res.pre_closing,
            res.closing_costs,
            res.mao,
            res.offer_raw,
            res.offer + 500,
        )

    monkeypatch.setattr(engine_mod, "compute_mao", buggy)
    result, _, _ = await _compute(session)
    assert (
        result.status == "blocked"
        and result.blocked_reason
        and result.blocked_reason.startswith("V5")
    )
    offer = await OfferRepository(session).get(uuid.UUID(result.offer_id))
    assert offer is not None and offer.status is OfferStatus.BLOCKED
    events = await _events(session, offer.id)
    assert ALERT_EVENT in events and "offer.computed" in events
    alert = (
        (await session.execute(select(AuditEvent).where(AuditEvent.event_type == ALERT_EVENT)))
        .scalars()
        .one()
    )
    assert alert.payload["kind"] == "a7.recompute_mismatch"


async def test_compute_offer_low_confidence_escalates(session: AsyncSession) -> None:
    result, _, _ = await _compute(
        session, line1="61 Elm Loop", postal_code="77022", disclosed_issues=[], asking_price=None
    )
    assert result.status == "escalate" and not result.gates_passed
    assert result.gates_failed == ["arv_confidence"] and result.arv_confidence == "Low"
    assert result.escalations and result.escalations[0].startswith("V1:")


async def test_compute_offer_market_hard_cap_from_markets_table(session: AsyncSession) -> None:
    market = await seed_market(session, hard_cap=Decimal(80000))
    lead, intake = await seed_seller(session, market=market)
    await session.commit()
    result = await acts.compute_offer(
        acts.ComputeOfferInput(str(intake.id), str(lead.id), new_key())
    )
    assert result.gates_failed == ["market_hard_cap"]


async def test_compute_offer_active_offer_on_same_parcel_fails_gate(session: AsyncSession) -> None:
    market = await seed_market(session)
    lead1, intake1 = await seed_seller(session, market=market)
    lead2, intake2 = await seed_seller(session, market=market, email="other@example.test")
    await session.commit()
    first = await acts.compute_offer(
        acts.ComputeOfferInput(str(intake1.id), str(lead1.id), new_key())
    )
    assert first.gates_passed
    second = await acts.compute_offer(
        acts.ComputeOfferInput(str(intake2.id), str(lead2.id), new_key())
    )
    assert second.gates_failed == ["no_active_offer_on_parcel"]


async def test_compute_offer_requires_a_condition_class(session: AsyncSession) -> None:
    market = await seed_market(session)
    lead, intake = await seed_seller(session, market=market, condition_class=None)
    await session.commit()
    with pytest.raises(ValueError, match="condition class"):
        await acts.compute_offer(acts.ComputeOfferInput(str(intake.id), str(lead.id), new_key()))


# -- qa_offer_draft -------------------------------------------------------------------------------


async def test_qa_offer_draft_records_review_and_reuses_it(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    result, _, _ = await _compute(session)
    qa = await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    assert qa.verdict == "approve" and qa.confidence == 0.95 and not qa.reused
    again = await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    assert again.reused
    reviews = (
        (
            await session.execute(
                select(QaReview).where(QaReview.offer_id == uuid.UUID(result.offer_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(reviews) == 1 and reviews[0].verdict is QaVerdict.APPROVE
    forced = await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id, force=True))
    assert not forced.reused
    request = offer_harness.model.requests[-1]
    assert (
        "never_disclose" in request.messages[-1].content
        and "$88,500" in request.messages[-1].content
    )


async def test_qa_offer_draft_blocks_a_tampered_template(session: AsyncSession) -> None:
    result, _, _ = await _compute(session)
    offer = await OfferRepository(session).get(uuid.UUID(result.offer_id))
    assert offer is not None and offer.draft_body
    offer.draft_body = offer.draft_body + "\nWe already have a buyer lined up and our fee is small."
    await session.commit()
    qa = await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    assert qa.verdict == "block" and qa.precheck_only
    assert "template:fee_disclosure" in qa.flags and "template:buyer_lined_up" in qa.flags


# -- pre_send_check (V6) ---------------------------------------------------------------------------


async def test_pre_send_check_writes_v6_row_and_recomputes_on_drift(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    result, _, _ = await _compute(
        session, line1="717 Willow Way", postal_code="77009", disclosed_issues=[], asking_price=None
    )
    assert result.offer_price == "92500"
    pre = await acts.pre_send_check(acts.PreSendInput(offer_id=result.offer_id))
    assert not pre.changed and pre.result == "pass" and pre.gates_passed
    addr = PropertyAddress("717 Willow Way", "Houston", "TX", "77009")
    for cid in ("N1", "N2", "N3"):
        offer_harness.property_provider.inject_comp(
            addr,
            SoldComp(
                cid,
                f"P-{cid}",
                f"{cid} St",
                "77009",
                Decimal(170 * 1500),
                DEFAULT_TODAY,
                1500,
                Decimal("0.1"),
                year_built=1980,
            ),
        )
    pre2 = await acts.pre_send_check(acts.PreSendInput(offer_id=result.offer_id))
    assert pre2.changed and pre2.result == "adjusted" and pre2.gates_passed
    assert pre2.offer_price is not None and Decimal(pre2.offer_price) > Decimal(92500)
    offers = OfferRepository(session)
    offer = await offers.get(uuid.UUID(result.offer_id))
    assert offer is not None and offer.offer_price == Decimal(pre2.offer_price)
    assert offer.draft_body and f"${int(offer.offer_price):,}" in offer.draft_body
    rows = await offers.list_verifications(offer.id)
    v6 = [r for r in rows if r.pass_id is VerificationPass.V6_PRE_SEND_DRIFT]
    assert [r.iteration for r in v6] == [1, 2] and [r.result.value for r in v6] == [
        "pass",
        "adjusted",
    ]
    assert any(
        r.pass_id is VerificationPass.V5_INDEPENDENT_RECOMPUTE and r.iteration == 2 for r in rows
    )


# -- decide_and_send_offer -------------------------------------------------------------------------


async def test_decide_l0_queues_for_human_and_writes_draft_row(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    result, _lead, _ = await _compute(session)
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    decided = await acts.decide_and_send_offer(
        acts.DecideOfferInput(offer_id=result.offer_id, workflow_id="offer:test")
    )
    assert decided.decision == PolicyDecision.QUEUE_FOR_HUMAN.value and not decided.sent
    assert decided.level == "L0" and decided.offer_status == OfferStatus.PENDING_REVIEW.value
    row = await session.get(Message, uuid.UUID(decided.message_id or ""))
    assert row is not None and row.direction is Direction.OUT and row.draft
    assert row.to_addresses == [SELLER_EMAIL] and row.policy_action == PolicyAction.OFFER_SEND.value
    assert row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
    assert (
        row.meta["temporal_workflow_id"] == "offer:test" and row.meta["offer_id"] == result.offer_id
    )
    assert row.external_message_id == acts.offer_draft_key(result.offer_id)
    assert offer_harness.graph.sent == [] and offer_harness.graph.drafts == {}
    offer = await OfferRepository(session).get(uuid.UUID(result.offer_id))
    assert (
        offer is not None and offer.status is OfferStatus.PENDING_REVIEW and offer.sent_at is None
    )
    assert "offer.policy_decided" in await _events(session, offer.id)


async def test_decide_l1_sends_when_gates_and_qa_pass(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    await set_level(session, PolicyAction.OFFER_SEND, AutonomyLevel.L1)
    result, _lead, _ = await _compute(session)
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    decided = await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    assert decided.decision == "send" and decided.sent and decided.level == "L1"
    assert len(offer_harness.graph.sent) == 1
    offer = await OfferRepository(session).get(uuid.UUID(result.offer_id))
    assert offer is not None and offer.status is OfferStatus.SENT and offer.sent_at is not None
    assert offer.policy_level_at_send is AutonomyLevel.L1 and offer.sent_message_id is not None
    assert offer.expires_at is not None and (offer.expires_at - offer.sent_at).days == 14
    events = await _events(session, offer.id)
    assert events[-1] == "offer.sent"
    # A retry is a no-op.
    again = await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    assert again.reason == "already sent" and len(offer_harness.graph.sent) == 1


async def test_decide_l1_queues_when_a_gate_fails(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    await set_level(session, PolicyAction.OFFER_SEND, AutonomyLevel.L1)
    result, _, _ = await _compute(
        session, line1="61 Elm Loop", postal_code="77022", disclosed_issues=[], asking_price=None
    )
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    decided = await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    assert decided.decision == "queue_for_human" and "A7 gates not all passed" in decided.reason
    assert offer_harness.graph.sent == []


async def test_decide_blocks_a_suppressed_seller(session: AsyncSession) -> None:
    await set_level(session, PolicyAction.OFFER_SEND, AutonomyLevel.L1)
    result, _, _ = await _compute(session)
    session.add(Suppression(channel=Channel.EMAIL, normalized_value=SELLER_EMAIL, reason="opt_out"))
    await session.commit()
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    decided = await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    assert decided.decision == "blocked" and decided.offer_status == OfferStatus.BLOCKED.value


async def test_decide_queues_without_a_seller_email(session: AsyncSession) -> None:
    await set_level(session, PolicyAction.OFFER_SEND, AutonomyLevel.L1)
    result, _, _ = await _compute(session, email=None)
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    decided = await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    assert decided.decision == "queue_for_human" and decided.reason == "no seller email on record"


# -- send_approved_offer / withdraw / handoff ------------------------------------------------------


async def test_send_approved_offer_sends_and_marks_l0(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    result, lead, _ = await _compute(session)
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    approved = await acts.send_approved_offer(
        acts.SendApprovedOfferInput(offer_id=result.offer_id, actor_id="aaron")
    )
    assert approved.sent and not approved.body_edited and len(offer_harness.graph.sent) == 1
    offer = await OfferRepository(session).get(uuid.UUID(result.offer_id))
    assert (
        offer is not None
        and offer.status is OfferStatus.SENT
        and offer.policy_level_at_send is AutonomyLevel.L0
    )
    events = await _events(session, offer.id)
    assert "offer.human_approved" in events and events[-1] == "offer.sent"
    handed = await acts.handoff_to_s5(
        acts.HandoffInput(offer_id=result.offer_id, seller_lead_id=str(lead.id))
    )
    assert handed.started and handed.workflow_id == f"negotiation:{result.offer_id}"
    start = [c for c in offer_harness.temporal.calls if c.method == "start_workflow"][-1]
    assert (
        start.args["workflow"] == "NegotiationWorkflow"
        and start.args["workflow_id"] == handed.workflow_id
    )
    assert start.args["args"][0].offer_id == result.offer_id
    await session.refresh(lead)
    assert lead.status is SellerLeadStatus.OFFERED


async def test_send_approved_offer_rejects_an_edit_that_breaks_the_template(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    result, _, _ = await _compute(session)
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    bad = await acts.send_approved_offer(
        acts.SendApprovedOfferInput(
            offer_id=result.offer_id, edited_body="Take $95,000, close in 21 days."
        )
    )
    assert not bad.sent and bad.blocked_reason and "template guard" in bad.blocked_reason
    assert offer_harness.graph.sent == []


async def test_withdraw_offer(session: AsyncSession) -> None:
    result, _, _ = await _compute(session)
    out = await acts.withdraw_offer(
        acts.WithdrawOfferInput(offer_id=result.offer_id, reason="rejected", actor_id="aaron")
    )
    assert out.status == OfferStatus.WITHDRAWN.value
    offer = await OfferRepository(session).get(uuid.UUID(result.offer_id))
    assert offer is not None and offer.calc_record["status_history"][-1]["reason"] == "rejected"


async def test_handoff_refuses_an_unsent_offer(session: AsyncSession) -> None:
    result, lead, _ = await _compute(session)
    with pytest.raises(ValueError, match="not sent"):
        await acts.handoff_to_s5(
            acts.HandoffInput(offer_id=result.offer_id, seller_lead_id=str(lead.id))
        )


# -- S5 --------------------------------------------------------------------------------------------


async def _sent_offer(session: AsyncSession) -> tuple[acts.ComputeOfferResult, SellerLead]:
    await set_level(session, PolicyAction.OFFER_SEND, AutonomyLevel.L1)
    result, lead, _ = await _compute(session)
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    decided = await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    assert decided.sent
    return result, lead


async def test_load_negotiation_terms(session: AsyncSession) -> None:
    result, _ = await _sent_offer(session)
    terms = await acts.load_negotiation(acts.LoadNegotiationInput(offer_id=result.offer_id))
    assert terms.offer_price == "88500.00" and terms.mao == "95169.32" and terms.status == "sent"
    assert terms.seller_email == SELLER_EMAIL and terms.schedule["reminder_days"] == [2, 5, 10]


async def test_counter_within_room_at_l1_auto_accepts_and_requests_contract(
    session: AsyncSession,
) -> None:
    await set_level(session, PolicyAction.COUNTER_ACCEPTANCE, AutonomyLevel.L1)
    result, lead = await _sent_offer(session)
    decision = await acts.evaluate_counter(
        acts.EvaluateCounterInput(offer_id=result.offer_id, amount="92000", message_id="m1")
    )
    assert decision.within_room and decision.accepted and decision.decision == "send"
    assert decision.offer_status == OfferStatus.ACCEPTED.value
    offer = await OfferRepository(session).get(uuid.UUID(result.offer_id))
    assert offer is not None and offer.calc_record["accepted_price"] == "92000"
    events = await _events(session, offer.id)
    assert "offer.countered" in events and acts.EVENT_CONTRACT_REQUESTED in events
    contract = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.event_type == acts.EVENT_CONTRACT_REQUESTED)
            )
        )
        .scalars()
        .one()
    )
    assert contract.payload["offer_id"] == result.offer_id and contract.payload[
        "seller_lead_id"
    ] == str(lead.id)
    assert contract.payload["price"] == "92000" and "human signs" in contract.payload["next"]


async def test_counter_at_l0_waits_for_a_human(session: AsyncSession) -> None:
    result, _ = await _sent_offer(session)
    decision = await acts.evaluate_counter(
        acts.EvaluateCounterInput(offer_id=result.offer_id, amount="92000")
    )
    assert decision.within_room and not decision.accepted and decision.decision == "queue_for_human"
    assert decision.offer_status == OfferStatus.COUNTERED.value


async def test_counter_above_mao_goes_to_human_even_at_l2(session: AsyncSession) -> None:
    await set_level(session, PolicyAction.COUNTER_ACCEPTANCE, AutonomyLevel.L2)
    result, _ = await _sent_offer(session)
    decision = await acts.evaluate_counter(
        acts.EvaluateCounterInput(offer_id=result.offer_id, amount="99000")
    )
    assert decision.classification == "above_mao" and not decision.accepted
    assert decision.decision == "queue_for_human"


async def test_counter_on_an_expired_offer_needs_a_human(session: AsyncSession) -> None:
    await set_level(session, PolicyAction.COUNTER_ACCEPTANCE, AutonomyLevel.L1)
    result, _ = await _sent_offer(session)
    expired = await acts.expire_offer(acts.ExpireOfferInput(offer_id=result.offer_id))
    assert expired.expired
    decision = await acts.evaluate_counter(
        acts.EvaluateCounterInput(offer_id=result.offer_id, amount="90000")
    )
    assert (
        not decision.accepted
        and "expired" in decision.reason
        and decision.offer_status == "expired"
    )


async def test_accept_decline_expire_and_requeue(session: AsyncSession) -> None:
    result, _lead = await _sent_offer(session)
    accepted = await acts.accept_offer(
        acts.AcceptOfferInput(offer_id=result.offer_id, source="seller")
    )
    assert accepted.status == "accepted" and accepted.price == "88500.00"
    assert acts.EVENT_CONTRACT_REQUESTED in await _events(session, uuid.UUID(result.offer_id))
    # a second accept is idempotent
    await acts.accept_offer(
        acts.AcceptOfferInput(offer_id=result.offer_id, source="human", actor_id="aaron")
    )
    events = await _events(session, uuid.UUID(result.offer_id))
    assert events.count(acts.EVENT_CONTRACT_REQUESTED) == 1

    result2, lead2 = await _sent_offer(session)
    await acts.handoff_to_s5(
        acts.HandoffInput(offer_id=result2.offer_id, seller_lead_id=str(lead2.id))
    )
    declined = await acts.decline_offer(
        acts.DeclineOfferInput(offer_id=result2.offer_id, reason="too low")
    )
    assert declined.status == "declined" and declined.requeue_at.startswith("2027-01-23")
    await session.refresh(lead2)
    assert lead2.status is SellerLeadStatus.REPLIED
    requeued = await acts.requeue_seller(
        acts.RequeueInput(offer_id=result2.offer_id, seller_lead_id=str(lead2.id))
    )
    assert requeued.requeued and requeued.status == SellerLeadStatus.QUEUED.value
    again = await acts.requeue_seller(
        acts.RequeueInput(offer_id=result2.offer_id, seller_lead_id=str(lead2.id))
    )
    assert not again.requeued
    not_expired = await acts.expire_offer(acts.ExpireOfferInput(offer_id=result2.offer_id))
    assert not not_expired.expired and not_expired.status == "declined"


async def test_reminder_goes_through_qa_and_policy(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    result, _ = await _sent_offer(session)
    sent = await acts.send_negotiation_message(
        acts.NegotiationMessageInput(
            offer_id=result.offer_id, kind="reminder", day=2, workflow_id="negotiation:x"
        )
    )
    assert sent.decision == "send" and sent.sent and sent.message_id
    assert len(offer_harness.graph.sent) == 2  # offer + reminder
    row = await session.get(Message, uuid.UUID(sent.message_id))
    assert row is not None and row.qa_review_id is not None and "$88,500" in row.body
    assert row.external_message_id == acts.offer_draft_key(result.offer_id, "reminder", 2)
    again = await acts.send_negotiation_message(
        acts.NegotiationMessageInput(offer_id=result.offer_id, kind="reminder", day=2)
    )
    assert again.reason == "already sent" and len(offer_harness.graph.sent) == 2
    # Re-engagement after expiry carries no number.
    await acts.expire_offer(acts.ExpireOfferInput(offer_id=result.offer_id))
    skipped = await acts.send_negotiation_message(
        acts.NegotiationMessageInput(offer_id=result.offer_id, kind="reminder", day=5)
    )
    assert skipped.skipped_reason == "status"
    re_ = await acts.send_negotiation_message(
        acts.NegotiationMessageInput(offer_id=result.offer_id, kind="reengage", day=60)
    )
    assert re_.sent
    row2 = await session.get(Message, uuid.UUID(re_.message_id or ""))
    assert row2 is not None and "$" not in row2.body


async def test_reminder_at_l0_queues_and_does_not_send(
    session: AsyncSession, offer_harness: OfferHarness
) -> None:
    result, _, _ = await _compute(session)
    await acts.qa_offer_draft(acts.QaOfferInput(offer_id=result.offer_id))
    await acts.decide_and_send_offer(acts.DecideOfferInput(offer_id=result.offer_id))
    await acts.send_approved_offer(
        acts.SendApprovedOfferInput(offer_id=result.offer_id, actor_id="aaron")
    )
    out = await acts.send_negotiation_message(
        acts.NegotiationMessageInput(offer_id=result.offer_id, kind="reminder", day=2)
    )
    assert out.decision == "queue_for_human" and not out.sent and len(offer_harness.graph.sent) == 1
