"""DM-04 / BB-03 acceptance: duplicate, update, correction, conflicting update, replay.

Every scenario asserts both the current state and that history is preserved.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import MakeInvestor, MakeMessage
from wholesaling.db.models import BuyBox
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria, BuyBoxRepository

HOUSTON_SFR = BuyBoxCriteria(
    geography={"markets": ["Houston"], "zips": ["77008", "77009"]},
    asset_types=["sfr"],
    price_min=Decimal("100000"),
    price_max=Decimal("250000"),
    condition="any",
    strategy="flip",
    financing="cash",
    closing_preferences={"days": 14},
    raw_text="Looking for SFRs in the Heights, 100-250k, cash, can close in two weeks.",
)


async def _count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(BuyBox))).scalar_one())


async def test_new_buy_box_creates_version_one(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    inv = await make_investor()
    msg = await make_message(inv, body=HOUSTON_SFR.raw_text or "")
    repo = BuyBoxRepository(session)

    result = await repo.create_version(inv.id, HOUSTON_SFR, source_message_id=msg.id)

    assert result.created and result.reason == "created"
    assert result.buy_box.version == 1
    assert result.buy_box.is_current is True
    assert result.buy_box.raw_text == HOUSTON_SFR.raw_text
    assert result.buy_box.price_max == Decimal("250000")
    assert result.buy_box.geography["zips"] == ["77008", "77009"]
    assert await _count(session) == 1


async def test_duplicate_criteria_from_a_new_message_does_not_create_a_version(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    """Same criteria restated in different words (or a quoted old email) is not a change."""
    inv = await make_investor()
    repo = BuyBoxRepository(session)
    m1 = await make_message(inv)
    m2 = await make_message(inv)
    first = await repo.create_version(inv.id, HOUSTON_SFR, source_message_id=m1.id)

    restated = replace(HOUSTON_SFR, raw_text="As I said: Heights SFRs, 100k to 250k, cash.")
    second = await repo.create_version(inv.id, restated, source_message_id=m2.id)

    assert second.created is False and second.reason == "duplicate"
    assert second.buy_box.id == first.buy_box.id
    assert await _count(session) == 1
    current = await repo.get_current(inv.id)
    assert current is not None and current.version == 1


async def test_update_creates_new_version_and_flips_is_current(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    inv = await make_investor()
    repo = BuyBoxRepository(session)
    m1 = await make_message(inv)
    m2 = await make_message(inv)
    v1 = (await repo.create_version(inv.id, HOUSTON_SFR, source_message_id=m1.id)).buy_box

    updated = replace(HOUSTON_SFR, price_max=Decimal("300000"), raw_text="Bumping my max to 300k.")
    result = await repo.create_version(inv.id, updated, source_message_id=m2.id)

    assert result.created and result.buy_box.version == 2
    assert result.superseded is not None and result.superseded.id == v1.id
    assert result.changed_fields == ["price_max"]
    await session.refresh(v1)
    assert v1.is_current is False
    assert v1.price_max == Decimal("250000"), "history must be preserved verbatim"
    current = await repo.get_current(inv.id)
    assert current is not None and current.version == 2 and current.price_max == Decimal("300000")
    versions = await repo.list_versions(inv.id)
    assert [v.version for v in versions] == [1, 2]
    assert sum(v.is_current for v in versions) == 1


async def test_correction_in_merge_mode_keeps_unmentioned_fields(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    """INV-14: "corrections overwrite with history". A one-field correction keeps the rest."""
    inv = await make_investor()
    repo = BuyBoxRepository(session)
    m1 = await make_message(inv)
    m2 = await make_message(inv)
    await repo.create_version(inv.id, HOUSTON_SFR, source_message_id=m1.id)

    correction = BuyBoxCriteria(
        price_max=Decimal("225000"), raw_text="Sorry, typo: max is 225k not 250k."
    )
    result = await repo.create_version(
        inv.id, correction, source_message_id=m2.id, mode="merge", change_note="typo correction"
    )

    assert result.created and result.buy_box.version == 2
    bb = result.buy_box
    assert bb.price_max == Decimal("225000")
    assert bb.price_min == Decimal("100000")
    assert bb.geography == HOUSTON_SFR.geography
    assert bb.asset_types == ["sfr"]
    assert bb.financing == "cash"
    assert bb.raw_text == "Sorry, typo: max is 225k not 250k."
    assert bb.change_note == "typo correction"
    assert result.changed_fields == ["price_max"]


async def test_conflicting_updates_both_kept_latest_wins(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    """Two messages disagree. Neither is lost; the later one is current; order is auditable."""
    inv = await make_investor()
    repo = BuyBoxRepository(session)
    m1, m2, m3 = [await make_message(inv) for _ in range(3)]
    await repo.create_version(inv.id, HOUSTON_SFR, source_message_id=m1.id)

    says_cash = BuyBoxCriteria(financing="cash", strategy="flip")
    says_hard_money = BuyBoxCriteria(financing="hard_money", strategy="buy_and_hold")
    r2 = await repo.create_version(inv.id, says_hard_money, source_message_id=m2.id, mode="merge")
    r3 = await repo.create_version(inv.id, says_cash, source_message_id=m3.id, mode="merge")

    assert r2.created and r2.buy_box.version == 2
    assert r3.created and r3.buy_box.version == 3
    versions = await repo.list_versions(inv.id)
    assert [(v.version, v.financing, v.strategy, v.is_current) for v in versions] == [
        (1, "cash", "flip", False),
        (2, "hard_money", "buy_and_hold", False),
        (3, "cash", "flip", True),
    ]
    assert [v.source_message_id for v in versions] == [m1.id, m2.id, m3.id]


async def test_replaying_same_source_message_is_a_noop(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    """A redelivered webhook or retried activity must not create a duplicate version."""
    inv = await make_investor()
    repo = BuyBoxRepository(session)
    msg = await make_message(inv)
    first = await repo.create_version(inv.id, HOUSTON_SFR, source_message_id=msg.id)

    # Even if the extractor produced slightly different output on the retry.
    changed = replace(HOUSTON_SFR, price_max=Decimal("999999"))
    replay = await repo.create_version(inv.id, changed, source_message_id=msg.id)

    assert replay.created is False and replay.reason == "replay"
    assert replay.buy_box.id == first.buy_box.id
    assert await _count(session) == 1
    current = await repo.get_current(inv.id)
    assert current is not None and current.price_max == Decimal("250000")


async def test_multiple_distinct_buy_boxes_per_investor(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    """BB-03: one investor, two criteria sets, each versioned independently."""
    inv = await make_investor()
    repo = BuyBoxRepository(session)
    dallas = BuyBoxCriteria(
        geography={"markets": ["Dallas"]},
        asset_types=["duplex"],
        price_max=Decimal("400000"),
        raw_text="Also buying duplexes in Dallas under 400k.",
    )

    a = await repo.create_version(inv.id, HOUSTON_SFR, buy_box_key="houston_sfr")
    b = await repo.create_version(inv.id, dallas, buy_box_key="dallas_duplex")
    b2 = await repo.create_version(
        inv.id,
        BuyBoxCriteria(price_max=Decimal("350000")),
        buy_box_key="dallas_duplex",
        mode="merge",
    )

    assert a.buy_box.version == 1 and b.buy_box.version == 1 and b2.buy_box.version == 2
    current = await repo.list_current(inv.id)
    assert {c.buy_box_key: c.version for c in current} == {"houston_sfr": 1, "dallas_duplex": 2}
    houston = await repo.get_current(inv.id, "houston_sfr")
    assert houston is not None and houston.is_current is True
    assert await _count(session) == 3


async def test_unique_constraint_backstops_duplicate_versions(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    """ADR 0002: the DB itself refuses a second (investor, key, version) row."""
    inv = await make_investor()
    session.add(BuyBox(investor_id=inv.id, buy_box_key="default", version=1))
    await session.flush()
    session.add(BuyBox(investor_id=inv.id, buy_box_key="default", version=1, is_current=False))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_deactivate_keeps_history(session: AsyncSession, make_investor: MakeInvestor) -> None:
    inv = await make_investor()
    repo = BuyBoxRepository(session)
    await repo.create_version(inv.id, HOUSTON_SFR)
    row = await repo.deactivate(inv.id)
    assert row is not None and row.is_active is False and row.is_current is True
    assert await _count(session) == 1
