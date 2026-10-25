"""POL-01/02/06, DM-10: 12 rows seeded at L0; hard floors cannot be raised."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models import Policy
from wholesaling.db.repositories.policies import HardFloorViolation, PolicyRepository
from wholesaling.policy.actions import (
    HARD_FLOOR_ACTIONS,
    POLICY_TABLE,
    AutonomyLevel,
    PolicyAction,
)


async def test_seed_creates_twelve_rows_at_l0(session: AsyncSession) -> None:
    repo = PolicyRepository(session)
    assert await repo.all() == {}
    inserted = await repo.ensure_seeded()
    assert inserted == 12
    rows = await repo.all()
    assert set(rows) == set(PolicyAction) == set(POLICY_TABLE)
    assert all(row.level is AutonomyLevel.L0 for row in rows.values())
    assert all(row.daily_cap is None and row.threshold is None for row in rows.values())
    # Seeding again is a no-op.
    assert await repo.ensure_seeded() == 0


async def test_missing_row_reads_as_l0(session: AsyncSession) -> None:
    repo = PolicyRepository(session)
    assert await repo.get_level(PolicyAction.OFFER_SEND) is AutonomyLevel.L0


async def test_set_level_and_caps(session: AsyncSession) -> None:
    repo = PolicyRepository(session)
    await repo.ensure_seeded()
    row = await repo.set_level(
        PolicyAction.INVESTOR_INITIAL_OUTREACH,
        AutonomyLevel.L1,
        updated_by="aaron",
        daily_cap=50,
        threshold=Decimal("0.85"),
        note="promotion after 50 approved drafts",
    )
    assert row.level is AutonomyLevel.L1
    assert await repo.get_level(PolicyAction.INVESTOR_INITIAL_OUTREACH) is AutonomyLevel.L1
    assert row.daily_cap == 50 and row.threshold == Decimal("0.85")
    assert row.updated_by == "aaron"
    # Demotion (POL-05) keeps caps unless given.
    row = await repo.set_level(
        PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L0, updated_by="system"
    )
    assert row.level is AutonomyLevel.L0 and row.daily_cap == 50


@pytest.mark.parametrize("action", sorted(HARD_FLOOR_ACTIONS))
@pytest.mark.parametrize("level", [AutonomyLevel.L1, AutonomyLevel.L2])
async def test_hard_floors_cannot_be_promoted(
    session: AsyncSession, action: PolicyAction, level: AutonomyLevel
) -> None:
    repo = PolicyRepository(session)
    await repo.ensure_seeded()
    with pytest.raises(HardFloorViolation):
        await repo.set_level(action, level, updated_by="aaron")
    assert await repo.get_level(action) is AutonomyLevel.L0


async def test_hard_floors_set_to_l0_is_allowed(session: AsyncSession) -> None:
    repo = PolicyRepository(session)
    for action in HARD_FLOOR_ACTIONS:
        row = await repo.set_level(action, AutonomyLevel.L0, updated_by="aaron")
        assert row.level is AutonomyLevel.L0


async def test_one_row_per_action(session: AsyncSession) -> None:
    session.add(Policy(action=PolicyAction.OFFER_SEND, level=AutonomyLevel.L0))
    await session.flush()
    session.add(Policy(action=PolicyAction.OFFER_SEND, level=AutonomyLevel.L1))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_invalid_level_rejected_by_check_constraint(session: AsyncSession) -> None:
    """The VARCHAR + CHECK enum (ADR 0002) is enforced by the database, not only by Python."""
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO policies (id, action, level, created_at, updated_at) "
                "VALUES (:id, :action, :level, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": "00000000-0000-0000-0000-000000000001", "action": "offer_send", "level": "L9"},
        )
    await session.rollback()
