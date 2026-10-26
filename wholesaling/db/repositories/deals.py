"""Search profiles (D1), property candidates and matches (D2) (REQUIREMENTS.md BB-04..BB-09).

- ``SearchProfileRepository``: one *active* profile per buy box; a meaningful change (different
  ``criteria_hash``) supersedes it with ``version + 1``; an identical hash coalesces (no new row,
  no new search). This is the BB-05/BB-06 guarantee.
- ``CandidateRepository``: ``property_candidates`` deduped by ``(source, external_id)`` and
  cross-source by parcel id or address key (BB-07); ``matches`` unique per
  ``(search_profile_id, property_candidate_id)`` and always created ``pending`` (BB-09).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.buy_boxes import SearchProfile
from wholesaling.db.models.deals import Match, PropertyCandidate
from wholesaling.db.models.enums import MatchReviewStatus, SearchProfileStatus


def criteria_hash(criteria: dict[str, Any]) -> str:
    """Stable SHA-256 of the canonical JSON of the criteria."""
    canonical = json.dumps(criteria, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SearchProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return await self._s.get(SearchProfile, profile_id)

    async def get_active(self, buy_box_id: uuid.UUID) -> SearchProfile | None:
        stmt = select(SearchProfile).where(
            SearchProfile.buy_box_id == buy_box_id,
            SearchProfile.status == SearchProfileStatus.ACTIVE,
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_versions(self, buy_box_id: uuid.UUID) -> list[SearchProfile]:
        stmt = (
            select(SearchProfile)
            .where(SearchProfile.buy_box_id == buy_box_id)
            .order_by(SearchProfile.version)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_active(self) -> list[SearchProfile]:
        stmt = (
            select(SearchProfile)
            .where(SearchProfile.status == SearchProfileStatus.ACTIVE)
            .order_by(SearchProfile.created_at)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def apply(
        self,
        buy_box_id: uuid.UUID,
        criteria: dict[str, Any],
        unknowns: list[str],
        *,
        digest: str | None = None,
    ) -> tuple[SearchProfile, bool, SearchProfile | None]:
        """Create or coalesce. Returns ``(profile, created, superseded)``.

        Same hash as the active profile -> ``(active, False, None)``; anything else supersedes
        the active profile (status ``superseded``) and writes ``version + 1``. ``digest`` lets
        the caller hash a subset of the criteria (D1 excludes the original wording).
        """
        digest = digest or criteria_hash(criteria)
        active = await self.get_active(buy_box_id)
        if active is not None and active.criteria_hash == digest:
            return active, False, None
        max_version = (
            await self._s.execute(
                select(func.max(SearchProfile.version)).where(
                    SearchProfile.buy_box_id == buy_box_id
                )
            )
        ).scalar_one()
        if active is not None:
            active.status = SearchProfileStatus.SUPERSEDED
        row = SearchProfile(
            buy_box_id=buy_box_id,
            version=int(max_version or 0) + 1,
            criteria=dict(criteria),
            unknowns=list(unknowns),
            status=SearchProfileStatus.ACTIVE,
            criteria_hash=digest,
        )
        self._s.add(row)
        await self._s.flush()
        return row, True, active

    async def deactivate(self, buy_box_id: uuid.UUID) -> SearchProfile | None:
        active = await self.get_active(buy_box_id)
        if active is not None:
            active.status = SearchProfileStatus.INACTIVE
            await self._s.flush()
        return active


class CandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, candidate_id: uuid.UUID) -> PropertyCandidate | None:
        return await self._s.get(PropertyCandidate, candidate_id)

    async def find_duplicate(
        self, *, source: str, external_id: str, parcel_id: str | None, address_key: str | None
    ) -> PropertyCandidate | None:
        """Same source id first; otherwise the same parcel id or address key from any source."""
        stmt = select(PropertyCandidate).where(
            PropertyCandidate.source == source, PropertyCandidate.external_id == external_id
        )
        found = (await self._s.execute(stmt)).scalar_one_or_none()
        if found is not None:
            return found
        if parcel_id:
            stmt = select(PropertyCandidate).where(PropertyCandidate.parcel_id == parcel_id)
            found = (await self._s.execute(stmt)).scalars().first()
            if found is not None:
                return found
        if address_key:
            stmt = select(PropertyCandidate).where(PropertyCandidate.address_key == address_key)
            found = (await self._s.execute(stmt)).scalars().first()
        return found

    async def upsert(
        self,
        *,
        source: str,
        external_id: str,
        retrieved_at: datetime,
        address_line1: str,
        city: str | None,
        state: str | None,
        postal_code: str | None,
        address_key: str | None,
        parcel_id: str | None,
        market_id: uuid.UUID | None = None,
        seller_lead_id: uuid.UUID | None = None,
        source_url: str | None = None,
        list_price: Decimal | None = None,
        est_arv: Decimal | None = None,
        est_repairs: Decimal | None = None,
        est_fees: Decimal | None = None,
        est_assignment_spread: Decimal | None = None,
        liquidity_score: Decimal | None = None,
        flags: list[str] | None = None,
        facts: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> tuple[PropertyCandidate, bool]:
        """Insert or refresh. A duplicate by parcel / address keeps its first row and gets the
        newer estimates; the other source id is remembered in ``raw.also_seen``."""
        existing = await self.find_duplicate(
            source=source, external_id=external_id, parcel_id=parcel_id, address_key=address_key
        )
        if existing is not None:
            existing.retrieved_at = retrieved_at
            existing.list_price = list_price if list_price is not None else existing.list_price
            existing.est_arv = est_arv
            existing.est_repairs = est_repairs
            existing.est_fees = est_fees
            existing.est_assignment_spread = est_assignment_spread
            existing.liquidity_score = liquidity_score
            existing.flags = list(flags or [])
            existing.facts = dict(facts or {})
            if (existing.source, existing.external_id) != (source, external_id):
                seen = list(existing.raw.get("also_seen") or [])
                ref = {"source": source, "external_id": external_id}
                if ref not in seen:
                    seen.append(ref)
                existing.raw = {**existing.raw, "also_seen": seen}
            else:
                existing.raw = dict(raw or existing.raw)
            if seller_lead_id is not None:
                existing.seller_lead_id = seller_lead_id
            await self._s.flush()
            return existing, False
        row = PropertyCandidate(
            market_id=market_id,
            source=source,
            external_id=external_id,
            source_url=source_url,
            retrieved_at=retrieved_at,
            address_line1=address_line1,
            city=city,
            state=state,
            postal_code=postal_code,
            address_key=address_key,
            parcel_id=parcel_id,
            seller_lead_id=seller_lead_id,
            list_price=list_price,
            est_arv=est_arv,
            est_repairs=est_repairs,
            est_fees=est_fees,
            est_assignment_spread=est_assignment_spread,
            liquidity_score=liquidity_score,
            flags=list(flags or []),
            facts=dict(facts or {}),
            raw=dict(raw or {}),
        )
        self._s.add(row)
        await self._s.flush()
        return row, True

    async def upsert_match(
        self,
        *,
        search_profile_id: uuid.UUID,
        candidate_id: uuid.UUID,
        score: Decimal | None,
        confidence: Decimal | None,
        evidence: dict[str, Any],
        agent_run_id: uuid.UUID | None = None,
    ) -> tuple[Match, bool]:
        """One match per (profile, candidate). A re-run refreshes score / evidence but never
        touches a human's review status (BB-09)."""
        stmt = select(Match).where(
            Match.search_profile_id == search_profile_id,
            Match.property_candidate_id == candidate_id,
        )
        existing = (await self._s.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.score = score
            existing.confidence = confidence
            existing.evidence = dict(evidence)
            existing.agent_run_id = agent_run_id
            await self._s.flush()
            return existing, False
        row = Match(
            search_profile_id=search_profile_id,
            property_candidate_id=candidate_id,
            score=score,
            confidence=confidence,
            evidence=dict(evidence),
            review_status=MatchReviewStatus.PENDING,
            agent_run_id=agent_run_id,
        )
        self._s.add(row)
        await self._s.flush()
        return row, True

    async def list_matches(self, search_profile_id: uuid.UUID) -> list[Match]:
        stmt = (
            select(Match)
            .where(Match.search_profile_id == search_profile_id)
            .order_by(Match.score.desc(), Match.confidence.desc(), Match.created_at, Match.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def review(
        self,
        match_id: uuid.UUID,
        *,
        status: MatchReviewStatus,
        reviewed_by: str,
        at: datetime,
    ) -> Match:
        match = await self._s.get(Match, match_id)
        if match is None:
            msg = f"match {match_id} not found"
            raise LookupError(msg)
        match.review_status = status
        match.reviewed_by = reviewed_by
        match.reviewed_at = at
        await self._s.flush()
        return match


__all__ = ["CandidateRepository", "SearchProfileRepository", "criteria_hash"]
