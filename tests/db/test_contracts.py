"""``contracts`` table + ``ContractRepository`` (REQUIREMENTS.md SEL-10, SEL-11, POL-03i/j,
POL-06, XC-05, BB-11): transitions, audit events, idempotency, and the hard rule that no
automated actor can sign, move earnest money, pick the buyer, record a deposit, close or kill a
deal."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import wholesaling.db.models as models
from tests.disposition_seed import (
    NOW,
    OPERATOR,
    seed_accepted_offer,
    seed_investor,
    seed_psa,
)
from wholesaling.db.models.deals import (
    BuyerResponse,
    Contract,
    ContractStatus,
    ContractType,
    EarnestMoneyStatus,
)
from wholesaling.db.models.enums import Actor, OfferStatus
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.contracts import (
    EVENT_BUYER_SELECTED,
    EVENT_CLOSED,
    EVENT_CREATED,
    EVENT_DEAD,
    EVENT_EARNEST_MONEY,
    EVENT_SIGNED,
    ContractNotFound,
    ContractRepository,
    HumanActionRequired,
    InvalidContractTransition,
    contract_address,
)


def test_contracts_table_is_registered() -> None:
    assert "contracts" in models.TABLE_NAMES
    assert Contract.__tablename__ == "contracts"
    assert models.Contract is Contract


async def _events(session: AsyncSession, row: Contract) -> list[str]:
    return [e.event_type for e in await AuditRepository(session).for_entity("contract", row.id)]


# -- create ---------------------------------------------------------------------------------------


async def test_awaiting_contract_lists_accepted_offers_without_a_psa(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    await seed_accepted_offer(
        session, market=deal.market, address="9 Other St", status=OfferStatus.SENT
    )
    repo = ContractRepository(session)
    awaiting = await repo.awaiting_contract()
    assert [a.offer_id for a in awaiting] == [deal.offer.id]
    assert awaiting[0].property_address == "1427 Pecan Grove Dr, Houston, TX 77092"
    await repo.create_from_accepted_offer(deal.offer.id, actor=Actor.SYSTEM, actor_id="test")
    assert await repo.awaiting_contract() == []


async def test_create_from_accepted_offer_copies_the_deal_and_is_idempotent(
    session: AsyncSession,
) -> None:
    deal = await seed_accepted_offer(session)
    repo = ContractRepository(session)
    row, created = await repo.create_from_accepted_offer(
        deal.offer.id, actor=Actor.HUMAN, actor_id=OPERATOR
    )
    again, created2 = await repo.create_from_accepted_offer(
        deal.offer.id, actor=Actor.HUMAN, actor_id=OPERATOR
    )
    assert created and not created2 and again.id == row.id
    assert row.type is ContractType.PSA and row.status is ContractStatus.REQUESTED
    assert row.purchase_price == Decimal("185000") and row.assignment_fee == Decimal("15000")
    assert row.property_postal_code == "77092"
    assert row.earnest_money_status is EarnestMoneyStatus.NONE
    assert contract_address(row) == "1427 Pecan Grove Dr, Houston, TX 77092"
    assert await _events(session, row) == [EVENT_CREATED]


async def test_create_refuses_unaccepted_offer_and_unknown_offer(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session, status=OfferStatus.COUNTERED)
    repo = ContractRepository(session)
    with pytest.raises(InvalidContractTransition, match="not accepted"):
        await repo.create_from_accepted_offer(deal.offer.id, actor=Actor.HUMAN, actor_id=OPERATOR)
    with pytest.raises(ContractNotFound):
        await repo.create_from_accepted_offer(uuid.uuid4(), actor=Actor.HUMAN, actor_id=OPERATOR)


# -- hard floor: automated actors are refused ------------------------------------------------------


@pytest.mark.parametrize("actor", [Actor.AGENT, Actor.SYSTEM])
async def test_automated_actor_cannot_sign_or_touch_money(
    session: AsyncSession, actor: Actor
) -> None:
    """POL-06: the repository, not only the UI, refuses every hard-floor transition unless the
    actor is a human. The row is untouched and no audit event is written."""
    deal = await seed_accepted_offer(session)
    repo = ContractRepository(session)
    row, _ = await repo.create_from_accepted_offer(deal.offer.id, actor=actor, actor_id="S6")
    investor = await seed_investor(session, name="Buyer LLC", email="b@x.com", market=deal.market)

    with pytest.raises(HumanActionRequired):
        await repo.mark_sent_for_signature(
            row.id, envelope_id="e", envelope_status="created", actor=actor, actor_id="S6", at=NOW
        )
    with pytest.raises(HumanActionRequired):
        await repo.mark_signed(
            row.id, signed_by="S6", signed_at=NOW, envelope_id="e", actor=actor, actor_id="S6"
        )
    with pytest.raises(HumanActionRequired):
        await repo.set_earnest_money(
            row.id,
            status=EarnestMoneyStatus.REQUESTED,
            amount=Decimal("1000"),
            actor=actor,
            actor_id="S6",
        )
    with pytest.raises(HumanActionRequired):
        await repo.set_deadlines(row.id, actor=actor, actor_id="S6", closing_date=NOW)
    with pytest.raises(HumanActionRequired):
        await repo.select_buyer(row.id, investor_id=investor.id, actor=actor, actor_id="S6")
    with pytest.raises(HumanActionRequired):
        await repo.record_deposit(
            row.id, amount=Decimal("5000"), received_at=NOW, actor=actor, actor_id="S6"
        )
    with pytest.raises(HumanActionRequired):
        await repo.mark_closed(
            row.id, realized_assignment_fee=Decimal("1"), closed_at=NOW, actor=actor, actor_id="S6"
        )
    with pytest.raises(HumanActionRequired):
        await repo.mark_dead(row.id, reason="x", actor=actor, actor_id="S6", at=NOW)
    with pytest.raises(HumanActionRequired):
        await repo.create_assignment(row.id, actor=actor, actor_id="S6")
    with pytest.raises(HumanActionRequired):
        await repo.update_checklist(
            row.id, items={"title_opened": True}, actor=actor, actor_id="S6"
        )

    fresh = await repo.require(row.id)
    assert fresh.status is ContractStatus.REQUESTED
    assert fresh.signed_at is None and fresh.earnest_money_status is EarnestMoneyStatus.NONE
    assert await _events(session, row) == [EVENT_CREATED]


async def test_automated_actor_may_record_a_buyer_response(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    row = await seed_psa(session, deal, signed=True)
    investor = await seed_investor(session, name="Buyer LLC", email="b@x.com", market=deal.market)
    row = await ContractRepository(session).record_buyer_response(
        row.id,
        investor_id=investor.id,
        response=BuyerResponse.INTERESTED,
        proof_of_funds=True,
        deposit_terms="$5k non-refundable within 24h",
        actor=Actor.AGENT,
        actor_id="S6",
        at=NOW,
    )
    entry = row.buyer_responses[str(investor.id)]
    assert entry["response"] == "interested" and entry["proof_of_funds"] is True
    assert entry["deposit_terms"] == "$5k non-refundable within 24h"
    assert row.buyer_investor_id is None  # never selects


# -- signing ---------------------------------------------------------------------------------------


async def test_mark_signed_records_human_and_is_idempotent(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    repo = ContractRepository(session)
    row, _ = await repo.create_from_accepted_offer(
        deal.offer.id, actor=Actor.HUMAN, actor_id=OPERATOR
    )
    row = await repo.mark_sent_for_signature(
        row.id,
        envelope_id="env-1",
        envelope_status="created",
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
        at=NOW,
    )
    assert row.status is ContractStatus.SENT_FOR_SIGNATURE and row.sent_for_signature_at == NOW
    row, changed = await repo.mark_signed(
        row.id,
        signed_by=OPERATOR,
        signed_at=NOW,
        envelope_id=None,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    assert changed and row.status is ContractStatus.SIGNED
    assert row.signed_by == OPERATOR and row.signed_at == NOW
    assert row.docusign_envelope_id == "env-1" and row.envelope_status == "completed"
    _, changed2 = await repo.mark_signed(
        row.id,
        signed_by="someone else",
        signed_at=NOW,
        envelope_id=None,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    assert not changed2 and row.signed_by == OPERATOR
    events = await AuditRepository(session).for_entity("contract", row.id)
    signed = [e for e in events if e.event_type == EVENT_SIGNED]
    assert len(signed) == 1
    assert signed[0].actor is Actor.HUMAN and signed[0].actor_id == OPERATOR
    assert signed[0].payload["signed_by"] == OPERATOR
    assert signed[0].correlation_id == f"disposition:{row.id}"


async def test_mark_signed_needs_a_name_and_a_signable_status(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    repo = ContractRepository(session)
    row, _ = await repo.create_from_accepted_offer(
        deal.offer.id, actor=Actor.HUMAN, actor_id=OPERATOR
    )
    with pytest.raises(InvalidContractTransition):
        await repo.mark_signed(
            row.id,
            signed_by="  ",
            signed_at=NOW,
            envelope_id=None,
            actor=Actor.HUMAN,
            actor_id=OPERATOR,
        )
    await repo.mark_dead(
        row.id, reason="seller walked", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW
    )
    with pytest.raises(InvalidContractTransition):
        await repo.mark_signed(
            row.id,
            signed_by=OPERATOR,
            signed_at=NOW,
            envelope_id=None,
            actor=Actor.HUMAN,
            actor_id=OPERATOR,
        )


# -- earnest money ---------------------------------------------------------------------------------


async def test_earnest_money_transitions_in_order_only(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    row = await seed_psa(session, deal, signed=True)
    repo = ContractRepository(session)
    with pytest.raises(InvalidContractTransition):
        await repo.set_earnest_money(
            row.id,
            status=EarnestMoneyStatus.DEPOSITED,
            amount=None,
            actor=Actor.HUMAN,
            actor_id=OPERATOR,
        )
    row = await repo.set_earnest_money(
        row.id,
        status=EarnestMoneyStatus.REQUESTED,
        amount=Decimal("2500"),
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    row = await repo.set_earnest_money(
        row.id,
        status=EarnestMoneyStatus.DEPOSITED,
        amount=None,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    assert row.earnest_money_amount == Decimal("2500")
    with pytest.raises(InvalidContractTransition):
        await repo.set_earnest_money(
            row.id,
            status=EarnestMoneyStatus.REQUESTED,
            amount=None,
            actor=Actor.HUMAN,
            actor_id=OPERATOR,
        )
    row = await repo.set_earnest_money(
        row.id,
        status=EarnestMoneyStatus.RELEASED,
        amount=None,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
        note="deal dead",
    )
    assert row.earnest_money_status is EarnestMoneyStatus.RELEASED
    money_events = [
        e
        for e in await AuditRepository(session).for_entity("contract", row.id)
        if e.event_type == EVENT_EARNEST_MONEY
    ]
    assert [(e.payload["from"], e.payload["to"]) for e in money_events] == [
        ("none", "requested"),
        ("requested", "deposited"),
        ("deposited", "released"),
    ]
    assert all(e.actor is Actor.HUMAN for e in money_events)


# -- buyer, deposit, assignment, closing -----------------------------------------------------------


async def test_buyer_selection_deposit_assignment_and_closing(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    repo = ContractRepository(session)
    row, _ = await repo.create_from_accepted_offer(
        deal.offer.id, actor=Actor.HUMAN, actor_id=OPERATOR
    )
    investor = await seed_investor(session, name="Buyer LLC", email="b@x.com", market=deal.market)
    with pytest.raises(InvalidContractTransition):  # not signed yet
        await repo.select_buyer(
            row.id, investor_id=investor.id, actor=Actor.HUMAN, actor_id=OPERATOR
        )
    await repo.mark_signed(
        row.id,
        signed_by=OPERATOR,
        signed_at=NOW,
        envelope_id="e",
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    with pytest.raises(InvalidContractTransition):  # no buyer yet
        await repo.record_deposit(
            row.id, amount=Decimal("5000"), received_at=NOW, actor=Actor.HUMAN, actor_id=OPERATOR
        )
    with pytest.raises(InvalidContractTransition):
        await repo.create_assignment(row.id, actor=Actor.HUMAN, actor_id=OPERATOR)
    row = await repo.select_buyer(
        row.id,
        investor_id=investor.id,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
        assignment_fee=Decimal("17500"),
        deposit_amount=Decimal("5000"),
    )
    assert row.buyer_investor_id == investor.id and row.assignment_fee == Decimal("17500")
    with pytest.raises(InvalidContractTransition):
        await repo.record_deposit(
            row.id, amount=Decimal("0"), received_at=NOW, actor=Actor.HUMAN, actor_id=OPERATOR
        )
    row = await repo.record_deposit(
        row.id,
        amount=Decimal("5000"),
        received_at=NOW + timedelta(days=1),
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    assert row.deposit_received_at == NOW + timedelta(days=1)

    assignment, created = await repo.create_assignment(row.id, actor=Actor.HUMAN, actor_id=OPERATOR)
    assert created and assignment.type is ContractType.ASSIGNMENT
    assert assignment.parent_contract_id == row.id and assignment.offer_id == row.offer_id
    assert assignment.buyer_investor_id == investor.id and assignment.assignment_fee == Decimal(
        "17500"
    )
    again, created2 = await repo.create_assignment(row.id, actor=Actor.HUMAN, actor_id=OPERATOR)
    assert not created2 and again.id == assignment.id
    assert (await repo.assignment_for(row.id)) is not None

    row, changed = await repo.mark_closed(
        row.id,
        realized_assignment_fee=Decimal("17000"),
        closed_at=NOW + timedelta(days=20),
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    assert (
        changed
        and row.status is ContractStatus.CLOSED
        and row.realized_assignment_fee == Decimal("17000")
    )
    _, changed2 = await repo.mark_closed(
        row.id,
        realized_assignment_fee=Decimal("1"),
        closed_at=NOW,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    assert not changed2 and row.realized_assignment_fee == Decimal("17000")
    with pytest.raises(InvalidContractTransition):
        await repo.mark_dead(row.id, reason="oops", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW)
    events = await _events(session, row)
    assert EVENT_BUYER_SELECTED in events and EVENT_CLOSED in events


async def test_mark_dead_keeps_earnest_money_and_flags_release(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    row = await seed_psa(session, deal, signed=True)
    repo = ContractRepository(session)
    await repo.set_earnest_money(
        row.id,
        status=EarnestMoneyStatus.REQUESTED,
        amount=Decimal("2500"),
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    await repo.set_earnest_money(
        row.id,
        status=EarnestMoneyStatus.DEPOSITED,
        amount=None,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    with pytest.raises(InvalidContractTransition):
        await repo.mark_dead(row.id, reason="  ", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW)
    row, changed = await repo.mark_dead(
        row.id, reason="failed inspection", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW
    )
    assert (
        changed
        and row.status is ContractStatus.CANCELLED
        and row.deal_dead_reason == "failed inspection"
    )
    assert row.earnest_money_status is EarnestMoneyStatus.DEPOSITED  # not released automatically
    dead = [
        e
        for e in await AuditRepository(session).for_entity("contract", row.id)
        if e.event_type == EVENT_DEAD
    ]
    assert len(dead) == 1 and dead[0].payload["release_earnest_money_reminder"] is True
    _, changed2 = await repo.mark_dead(
        row.id, reason="again", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW
    )
    assert not changed2


async def test_set_deadlines_and_checklist(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    row = await seed_psa(session, deal, signed=True)
    repo = ContractRepository(session)
    row = await repo.set_deadlines(
        row.id,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
        option_period_end=NOW + timedelta(days=7),
        inspection_deadline=NOW + timedelta(days=10),
        closing_date=NOW + timedelta(days=30),
        title_company="  Lone Star Title  ",
    )
    assert row.closing_date == NOW + timedelta(days=30) and row.title_company == "Lone Star Title"
    row = await repo.update_checklist(
        row.id, items={"title_opened": True}, actor=Actor.HUMAN, actor_id=OPERATOR
    )
    assert row.checklist == {"title_opened": True}


async def test_list_helpers(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    row = await seed_psa(session, deal, signed=True)
    repo = ContractRepository(session)
    assert [c.id for c in await repo.list_psa()] == [row.id]
    assert [c.id for c in await repo.list_all(status=ContractStatus.SIGNED)] == [row.id]
    assert await repo.list_all(status=ContractStatus.CLOSED) == []
    assert (await repo.for_offer(deal.offer.id, ContractType.PSA)) is not None
    with pytest.raises(ContractNotFound):
        await repo.require(uuid.uuid4())


@pytest.mark.postgres
async def test_contracts_round_trip_on_postgres(session: AsyncSession) -> None:
    deal = await seed_accepted_offer(session)
    row = await seed_psa(session, deal, signed=True)
    await session.commit()
    fresh = await ContractRepository(session).require(row.id)
    assert fresh.status is ContractStatus.SIGNED and fresh.signed_at is not None
