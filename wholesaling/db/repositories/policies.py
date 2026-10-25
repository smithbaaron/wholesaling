"""Live autonomy levels (REQUIREMENTS.md POL-01..06, DM-10).

The table holds the *level* per action; what each level does is ``policy.actions.POLICY_TABLE``.
Hard-floor actions (contract execution, earnest money) are Human at every level by definition;
this repository also refuses to raise their stored level so a bad admin call cannot even record
an intent to automate them (POL-06).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.ops import Policy
from wholesaling.policy.actions import (
    HARD_FLOOR_ACTIONS,
    POLICY_TABLE,
    AutonomyLevel,
    PolicyAction,
    default_level,
)


class HardFloorViolation(ValueError):
    """Raised when a caller tries to promote a hard-floor action above L0."""


class PolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def all(self) -> dict[PolicyAction, Policy]:
        rows = (await self._s.execute(select(Policy).order_by(Policy.action))).scalars().all()
        return {row.action: row for row in rows}

    async def get(self, action: PolicyAction) -> Policy | None:
        stmt = select(Policy).where(Policy.action == action)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_level(self, action: PolicyAction) -> AutonomyLevel:
        """Level for ``action``; a missing row means L0 (POL-02), never an error."""
        row = await self.get(action)
        return row.level if row is not None else default_level()

    async def set_level(
        self,
        action: PolicyAction,
        level: AutonomyLevel,
        *,
        updated_by: str,
        daily_cap: int | None = None,
        threshold: Decimal | None = None,
        note: str | None = None,
    ) -> Policy:
        """Set the level (and optional cap/threshold) for one action, creating the row if absent."""
        if action in HARD_FLOOR_ACTIONS and level is not AutonomyLevel.L0:
            msg = f"{action.value} is a hard floor (POL-06); it stays at L0 / Human"
            raise HardFloorViolation(msg)
        if action not in POLICY_TABLE:  # pragma: no cover - enum guards this
            msg = f"unknown policy action {action!r}"
            raise ValueError(msg)
        row = await self.get(action)
        if row is None:
            row = Policy(action=action, level=level)
            self._s.add(row)
        row.level = level
        row.updated_by = updated_by
        if daily_cap is not None:
            row.daily_cap = daily_cap
        if threshold is not None:
            row.threshold = threshold
        if note is not None:
            row.note = note
        await self._s.flush()
        return row

    async def ensure_seeded(self, *, updated_by: str = "system") -> int:
        """Insert any missing action at L0. Migration 0001 already does this; kept for tests
        and for new actions added later. Returns the number of rows inserted."""
        existing = await self.all()
        inserted = 0
        for action in PolicyAction:
            if action not in existing:
                self._s.add(Policy(action=action, level=default_level(), updated_by=updated_by))
                inserted += 1
        if inserted:
            await self._s.flush()
        return inserted
