"""Investor entity resolution (REQUIREMENTS.md INV-06, INV-07).

``upsert_by_entity_key`` collapses name variants (``Acme Holdings, L.L.C.`` / ``ACME HOLDINGS
LLC``) onto one investor keyed by ``normalize_entity_name``. Merges are conservative: existing
non-null facts are kept, blanks are filled, lists are unioned, and every source record is kept
as a ``source_evidence`` row. Nothing is ever deleted here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.base import utcnow
from wholesaling.db.models.enums import InvestorType
from wholesaling.db.models.investors import Investor, SourceEvidence
from wholesaling.db.normalize import normalize_address, normalize_entity_name


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    """One source record supporting an investor (a deed, a provider hit, a web page)."""

    source: str
    ref: str | None = None
    url: str | None = None
    retrieved_at: datetime | None = None
    confidence: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MailingAddress:
    line1: str
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    line2: str | None = None


@dataclass(frozen=True, slots=True)
class InvestorUpsertResult:
    investor: Investor
    created: bool
    evidence_added: int


class InvestorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, investor_id: uuid.UUID) -> Investor | None:
        return await self._s.get(Investor, investor_id)

    async def get_by_entity_key(self, entity_key: str) -> Investor | None:
        stmt = select(Investor).where(Investor.entity_key == entity_key)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_by_name(self, name: str) -> Investor | None:
        return await self.get_by_entity_key(normalize_entity_name(name))

    async def list_evidence(self, investor_id: uuid.UUID) -> list[SourceEvidence]:
        stmt = (
            select(SourceEvidence)
            .where(SourceEvidence.investor_id == investor_id)
            .order_by(SourceEvidence.retrieved_at, SourceEvidence.created_at)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def upsert_by_entity_key(
        self,
        name: str,
        *,
        investor_type: InvestorType | None = None,
        is_entity: bool | None = None,
        score: int | None = None,
        score_breakdown: dict[str, Any] | None = None,
        market_id: uuid.UUID | None = None,
        mailing_address: MailingAddress | None = None,
        first_name: str | None = None,
        purchase_count_24m: int | None = None,
        last_purchase_at: datetime | None = None,
        source_refs: dict[str, list[str]] | None = None,
        evidence: list[EvidenceInput] | None = None,
    ) -> InvestorUpsertResult:
        """Create or conservatively merge an investor identified by its normalised name.

        Merge rules for an existing row:
        - ``canonical_name`` keeps its first spelling; new spellings go to ``aliases``.
        - Scalars (``market_id``, mailing address, ``first_name``) fill blanks only.
        - ``investor_type`` is replaced only when the stored type is ``unknown``.
        - ``score`` / ``score_breakdown`` take the latest value when given (A1 rescoring).
        - ``purchase_count_24m`` and ``last_purchase_at`` keep the maximum.
        - ``source_refs`` are unioned per source.
        - ``evidence`` rows are appended; a row with the same ``(source, ref)`` already attached
          to this investor is not duplicated. Evidence is never removed.
        """
        entity_key = normalize_entity_name(name)
        if not entity_key:
            msg = f"name normalises to an empty entity key: {name!r}"
            raise ValueError(msg)

        stmt = select(Investor).where(Investor.entity_key == entity_key).with_for_update()
        existing = (await self._s.execute(stmt)).scalar_one_or_none()
        display_name = " ".join(name.split())

        if existing is None:
            investor = Investor(
                canonical_name=display_name,
                entity_key=entity_key,
                aliases=[display_name],
                investor_type=investor_type or InvestorType.UNKNOWN,
                is_entity=_looks_like_entity(entity_key) if is_entity is None else is_entity,
                score=score,
                score_breakdown=dict(score_breakdown or {}),
                market_id=market_id,
                first_name=first_name,
                purchase_count_24m=purchase_count_24m,
                last_purchase_at=last_purchase_at,
                source_refs={k: sorted(set(v)) for k, v in (source_refs or {}).items()},
            )
            _apply_address(investor, mailing_address, fill_only=False)
            self._s.add(investor)
            created = True
        else:
            investor = existing
            created = False
            if display_name not in investor.aliases:
                investor.aliases = [*investor.aliases, display_name]
            if investor_type is not None and investor.investor_type is InvestorType.UNKNOWN:
                investor.investor_type = investor_type
            if is_entity is not None and not investor.is_entity:
                investor.is_entity = is_entity
            if score is not None:
                investor.score = score
                if score_breakdown is not None:
                    investor.score_breakdown = dict(score_breakdown)
            if investor.market_id is None and market_id is not None:
                investor.market_id = market_id
            if investor.first_name is None and first_name:
                investor.first_name = first_name
            if purchase_count_24m is not None:
                investor.purchase_count_24m = max(
                    investor.purchase_count_24m or 0, purchase_count_24m
                )
            if last_purchase_at is not None and (
                investor.last_purchase_at is None or last_purchase_at > investor.last_purchase_at
            ):
                investor.last_purchase_at = last_purchase_at
            if source_refs:
                merged = {k: list(v) for k, v in investor.source_refs.items()}
                for src, refs in source_refs.items():
                    merged[src] = sorted(set(merged.get(src, [])) | set(refs))
                investor.source_refs = merged
            _apply_address(investor, mailing_address, fill_only=True)

        await self._s.flush()

        added = 0
        if evidence:
            seen: set[tuple[str, str]] = set()
            if not created:
                for row in await self.list_evidence(investor.id):
                    if row.ref is not None:
                        seen.add((row.source, row.ref))
            for ev in evidence:
                key = (ev.source, ev.ref) if ev.ref is not None else None
                if key is not None and key in seen:
                    continue
                self._s.add(
                    SourceEvidence(
                        investor_id=investor.id,
                        source=ev.source,
                        ref=ev.ref,
                        url=ev.url,
                        retrieved_at=ev.retrieved_at or utcnow(),
                        confidence=ev.confidence,
                        raw=dict(ev.raw),
                    )
                )
                if key is not None:
                    seen.add(key)
                added += 1
            await self._s.flush()

        return InvestorUpsertResult(investor=investor, created=created, evidence_added=added)


_ENTITY_MARKERS = frozenset({"LLC", "INC", "CORP", "LP", "LLP", "LTD", "TRUST", "CO"})


def _looks_like_entity(entity_key: str) -> bool:
    return bool(entity_key) and entity_key.split()[-1] in _ENTITY_MARKERS


def _apply_address(investor: Investor, address: MailingAddress | None, *, fill_only: bool) -> None:
    if address is None:
        return
    if fill_only and investor.mailing_address_line1 is not None:
        return
    investor.mailing_address_line1 = address.line1
    investor.mailing_address_line2 = address.line2
    investor.mailing_city = address.city
    investor.mailing_state = address.state
    investor.mailing_postal_code = address.postal_code
    investor.mailing_address_key = normalize_address(
        address.line1, address.city, address.state, address.postal_code
    )
