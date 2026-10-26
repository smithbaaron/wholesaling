"""BB-11 contract KPIs (append-only additions to reporting/kpis.py): signed, closed, dead,
realised fees, contract-to-close rate. The legacy ``contracts()`` proxy is untouched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from tests.disposition_seed import OPERATOR, seed_accepted_offer, seed_investor, seed_psa
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.contracts import ContractRepository
from wholesaling.reporting.kpis import (
    ContractKpis,
    contract_kpis,
    contract_to_close_totals,
    contracts,
    contracts_closed,
    contracts_dead,
    contracts_signed,
)

# ``contracts_dead`` keys on ``updated_at`` (the human's click, wall clock), so the window is
# anchored on the real clock here.
NOW = datetime.now(tz=UTC).replace(microsecond=0)
START = NOW - timedelta(days=1)
END = NOW + timedelta(days=1)


async def test_empty_database(session: AsyncSession) -> None:
    k = await contract_kpis(session, START, END)
    assert k == ContractKpis(window_start=START, window_end=END)
    assert k.contract_to_close_rate is None and k.average_realized_fee_usd is None
    assert k.as_dict()["contract_to_close_rate"] is None


async def test_signed_closed_dead_and_rate(session: AsyncSession) -> None:
    deal1 = await seed_accepted_offer(session)
    market = deal1.market
    deal2 = await seed_accepted_offer(session, market=market, address="2 Second St")
    deal3 = await seed_accepted_offer(session, market=market, address="3 Third St")
    deal4 = await seed_accepted_offer(session, market=market, address="4 Fourth St")
    repo = ContractRepository(session)
    inv = await seed_investor(session, name="Buyer LLC", email="b@x.com", market=market)

    # 1: signed in window and closed in window with a realised fee.
    c1 = await seed_psa(session, deal1, signed=True, signed_at=NOW)
    await repo.select_buyer(c1.id, investor_id=inv.id, actor=Actor.HUMAN, actor_id=OPERATOR)
    await repo.mark_closed(
        c1.id,
        realized_assignment_fee=Decimal("17000"),
        closed_at=NOW,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    # 2: signed in window, still open.
    await seed_psa(session, deal2, signed=True, signed_at=NOW)
    # 3: signed long ago (outside the window), dead now.
    c3 = await seed_psa(session, deal3, signed=True, signed_at=NOW - timedelta(days=40))
    await repo.mark_dead(c3.id, reason="inspection", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW)
    # 4: requested only (never signed).
    await seed_psa(session, deal4)
    await session.commit()

    assert await contracts_signed(session, START, END) == 2
    assert await contracts_closed(session, START, END) == (1, Decimal("17000"))
    assert await contracts_dead(session, START, END) == 1
    assert await contract_to_close_totals(session) == (3, 1)
    k = await contract_kpis(session, START, END)
    assert k.contracts_signed == 2 and k.contracts_closed == 1 and k.contracts_dead == 1
    assert k.realized_assignment_fees_usd == Decimal("17000")
    assert k.contract_to_close_rate == 1 / 3
    assert k.average_realized_fee_usd == Decimal("17000.00")
    d = k.as_dict()
    assert d["realized_assignment_fees_usd"] == "17000.00" and d["signed_total"] == 3

    # Window with nothing in it.
    far = await contract_kpis(session, NOW + timedelta(days=10), NOW + timedelta(days=11))
    assert far.contracts_signed == 0 and far.signed_total == 3  # totals are all-time

    # The existing accepted-offer proxy still works and is independent of the new table.
    count, fees = await contracts(session, START, END)
    assert count == 4 and fees == Decimal("60000")
