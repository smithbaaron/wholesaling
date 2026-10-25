"""Versioned buy boxes (REQUIREMENTS.md BB-02, BB-03, INV-14, DM-04).

Every change to a buy box is a new ``version`` row; the previous row keeps its data with
``is_current=False`` so corrections "overwrite with history". Two guards keep upserts
deterministic and duplicate-free:

- replaying the same ``source_message_id`` returns the version it already produced;
- criteria identical to the current version produce no new version.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.buy_boxes import DEFAULT_BUY_BOX_KEY, BuyBox

VersionReason = Literal["created", "replay", "duplicate"]


@dataclass(frozen=True, slots=True)
class BuyBoxCriteria:
    """Structured buy-box fields (DM-03).

    ``None`` means "not stated" (or, in merge mode, "unchanged").
    """

    geography: dict[str, Any] | None = None
    asset_types: list[str] | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    condition: str | None = None
    strategy: str | None = None
    financing: str | None = None
    closing_preferences: dict[str, Any] | None = None
    other_criteria: dict[str, Any] | None = None
    unknown_fields: list[str] | None = None
    raw_text: str | None = None

    @classmethod
    def from_row(cls, row: BuyBox) -> BuyBoxCriteria:
        return cls(
            geography=dict(row.geography),
            asset_types=list(row.asset_types),
            price_min=row.price_min,
            price_max=row.price_max,
            condition=row.condition,
            strategy=row.strategy,
            financing=row.financing,
            closing_preferences=dict(row.closing_preferences),
            other_criteria=dict(row.other_criteria),
            unknown_fields=list(row.unknown_fields),
            raw_text=row.raw_text,
        )

    def merged_over(self, base: BuyBoxCriteria) -> BuyBoxCriteria:
        """Fields set here win; ``None`` fields inherit from ``base``."""
        values = asdict(base)
        for k, v in asdict(self).items():
            if v is not None:
                values[k] = v
        return BuyBoxCriteria(**values)

    def structured_equals(self, other: BuyBoxCriteria) -> bool:
        """Equality on the structured fields only; ``raw_text`` wording is not compared."""
        a, b = asdict(self), asdict(other)
        a.pop("raw_text")
        b.pop("raw_text")
        return _canon(a) == _canon(b)


def _canon(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None or v == {} or v == []:
            out[k] = None
        elif isinstance(v, Decimal):
            out[k] = str(v.normalize())
        elif isinstance(v, list):
            out[k] = sorted(str(x) for x in v)
        else:
            out[k] = v
    return out


@dataclass(frozen=True, slots=True)
class BuyBoxVersionResult:
    buy_box: BuyBox
    created: bool
    reason: VersionReason
    superseded: BuyBox | None = None
    changed_fields: list[str] = field(default_factory=list)


class BuyBoxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_current(
        self, investor_id: uuid.UUID, buy_box_key: str = DEFAULT_BUY_BOX_KEY
    ) -> BuyBox | None:
        stmt = (
            select(BuyBox)
            .where(
                BuyBox.investor_id == investor_id,
                BuyBox.buy_box_key == buy_box_key,
                BuyBox.is_current.is_(True),
            )
            .with_for_update()
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_current(self, investor_id: uuid.UUID) -> list[BuyBox]:
        stmt = (
            select(BuyBox)
            .where(BuyBox.investor_id == investor_id, BuyBox.is_current.is_(True))
            .order_by(BuyBox.buy_box_key)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_versions(
        self, investor_id: uuid.UUID, buy_box_key: str = DEFAULT_BUY_BOX_KEY
    ) -> list[BuyBox]:
        stmt = (
            select(BuyBox)
            .where(BuyBox.investor_id == investor_id, BuyBox.buy_box_key == buy_box_key)
            .order_by(BuyBox.version)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def get_by_source_message(self, source_message_id: uuid.UUID) -> BuyBox | None:
        stmt = select(BuyBox).where(BuyBox.source_message_id == source_message_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def create_version(
        self,
        investor_id: uuid.UUID,
        criteria: BuyBoxCriteria,
        *,
        buy_box_key: str = DEFAULT_BUY_BOX_KEY,
        source_message_id: uuid.UUID | None = None,
        source_ref: str | None = None,
        change_note: str | None = None,
        mode: Literal["replace", "merge"] = "replace",
    ) -> BuyBoxVersionResult:
        """Record a new or changed buy box.

        - ``mode="replace"``: ``criteria`` is the complete new state (A4 re-extracted the full box).
        - ``mode="merge"``: ``None`` fields inherit from the current version (a correction such
          as "actually my max is 300k" touches one field and keeps the rest).

        Returns the version that now represents the message. ``created`` is False when the
        message was already processed (``reason="replay"``) or when the structured criteria
        equal the current version (``reason="duplicate"``); no row is written in either case.
        """
        if source_message_id is not None:
            replayed = await self.get_by_source_message(source_message_id)
            if replayed is not None:
                return BuyBoxVersionResult(buy_box=replayed, created=False, reason="replay")

        current = await self.get_current(investor_id, buy_box_key)
        effective = criteria
        if current is not None and mode == "merge":
            effective = criteria.merged_over(BuyBoxCriteria.from_row(current))

        if current is not None and effective.structured_equals(BuyBoxCriteria.from_row(current)):
            return BuyBoxVersionResult(buy_box=current, created=False, reason="duplicate")

        max_version_stmt = select(func.max(BuyBox.version)).where(
            BuyBox.investor_id == investor_id, BuyBox.buy_box_key == buy_box_key
        )
        max_version = (await self._s.execute(max_version_stmt)).scalar_one()
        next_version = int(max_version or 0) + 1

        changed: list[str] = []
        if current is not None:
            before = _canon(asdict(BuyBoxCriteria.from_row(current)))
            after = _canon(asdict(effective))
            changed = [k for k in after if k != "raw_text" and before[k] != after[k]]
            await self._s.execute(
                update(BuyBox)
                .where(
                    BuyBox.investor_id == investor_id,
                    BuyBox.buy_box_key == buy_box_key,
                    BuyBox.is_current.is_(True),
                )
                .values(is_current=False)
            )
            current.is_current = False

        row = BuyBox(
            investor_id=investor_id,
            buy_box_key=buy_box_key,
            version=next_version,
            is_current=True,
            is_active=True,
            geography=dict(effective.geography or {}),
            asset_types=list(effective.asset_types or []),
            price_min=effective.price_min,
            price_max=effective.price_max,
            condition=effective.condition,
            strategy=effective.strategy,
            financing=effective.financing,
            closing_preferences=dict(effective.closing_preferences or {}),
            other_criteria=dict(effective.other_criteria or {}),
            unknown_fields=list(effective.unknown_fields or []),
            raw_text=effective.raw_text,
            source_message_id=source_message_id,
            source_ref=source_ref,
            change_note=change_note,
        )
        self._s.add(row)
        await self._s.flush()
        return BuyBoxVersionResult(
            buy_box=row,
            created=True,
            reason="created",
            superseded=current,
            changed_fields=changed,
        )

    async def deactivate(
        self, investor_id: uuid.UUID, buy_box_key: str = DEFAULT_BUY_BOX_KEY
    ) -> BuyBox | None:
        """Mark the current version inactive (BB-02 active/inactive status). History is kept."""
        current = await self.get_current(investor_id, buy_box_key)
        if current is not None:
            current.is_active = False
            await self._s.flush()
        return current
