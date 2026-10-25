"""A1 Investor Discovery (REQUIREMENTS.md INV-01..07; AGT-A1 "Haiku + code", the code half).

Pulls deeds for the 24-month window in one market, resolves buyers into investor candidates
(``entity_resolution``), applies INV-05 exclusions and INV-02 qualification, classifies the
investor type (INV-03), scores 0..100 (INV-04) and writes investors plus every source deed as
``source_evidence`` through ``InvestorRepository.upsert_by_entity_key``. Rerunning the same
window is idempotent: the repository merges onto the entity key and skips evidence already
attached by ``(source, ref)``.

Write policy (not spelled out in V2; decided here):
- Qualified candidates are written with score, type and evidence.
- INV-05 exclusions are written with ``excluded_reason`` and evidence but no score, so the
  exclusion is auditable and the entity is not re-researched.
- Candidates that fail INV-02 (single-purchase individuals, mortgage buyers, out-of-band,
  stale) are counted by reason and **not** written: they may qualify on a later run and a row
  for every retail homebuyer in a metro would bloat the table.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.entity_resolution import InvestorCandidate, resolve_entities
from wholesaling.agents.scoring import (
    PURCHASE_WINDOW_MONTHS,
    EntityCandidate,
    InvestorFeatures,
    PurchaseEvent,
    RecordSet,
    classify_investor_type,
    in_band,
    investor_score,
    is_cash_like,
    is_excluded,
    qualifies,
)
from wholesaling.config.tunables import Tunables, get_tunables
from wholesaling.db.base import utcnow
from wholesaling.db.models.enums import InvestorType
from wholesaling.db.models.investors import Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.repositories.investors import EvidenceInput, InvestorRepository
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.models import DeedRecord, MarketRef
from wholesaling.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class A1Result:
    market: str
    market_id: uuid.UUID
    window_since: str
    window_until: str
    deeds_pulled: int = 0
    found: int = 0  # resolved buyer candidates
    qualified: int = 0  # passed INV-02 and not excluded
    prospects: int = 0  # score >= 50 (INV-04)
    priority: int = 0  # score >= 75 (INV-04)
    excluded_by_reason: dict[str, int] = field(default_factory=dict)  # INV-05 + INV-02 failures
    by_type: dict[str, int] = field(default_factory=dict)
    investors_created: int = 0
    investors_updated: int = 0
    evidence_added: int = 0
    prospect_ids: list[uuid.UUID] = field(default_factory=list)  # ordered by score desc

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["market_id"] = str(self.market_id)
        d["prospect_ids"] = [str(i) for i in self.prospect_ids]
        return d


def _evidence(rec: DeedRecord, role: str) -> EvidenceInput:
    return EvidenceInput(
        source=rec.source,
        ref=rec.source_ref,
        url=rec.source_url,
        retrieved_at=rec.retrieved_at,
        confidence=Decimal("0.95"),
        raw={"role": role, **rec.to_json()},
    )


def purchase_events(cand: InvestorCandidate) -> list[PurchaseEvent]:
    """Pair each purchase with the investor's later sale of the same parcel, if seen."""
    events: list[PurchaseEvent] = []
    for p in cand.purchases:
        resale = next(
            (
                r
                for r in sorted(cand.resales, key=lambda r: r.sale_date)
                if r.parcel_id == p.parcel_id and r.sale_date > p.sale_date
            ),
            None,
        )
        events.append(
            PurchaseEvent(
                purchase_date=p.sale_date,
                price=p.sale_price,
                zip=p.zip,
                resale_date=resale.sale_date if resale else None,
                resale_price=resale.sale_price if resale else None,
            )
        )
    return events


def _first_name(cand: InvestorCandidate) -> str | None:
    if cand.is_entity:
        return None
    tokens = cand.display_name.split()
    return tokens[0].title() if tokens else None


class A1InvestorDiscovery:
    def __init__(self, tunables: Tunables | None = None) -> None:
        self._tunables = tunables or get_tunables()

    async def run(
        self,
        session: AsyncSession,
        providers: DataProviders,
        market: Market,
        *,
        as_of: datetime | None = None,
        suppressed_entity_keys: frozenset[str] = frozenset(),
    ) -> A1Result:
        as_of = as_of or utcnow()
        until = as_of.date()
        since = (as_of - timedelta(days=round(PURCHASE_WINDOW_MONTHS * 30.4375))).date()
        band_cfg = self._tunables.deal_band
        band = (
            market.deal_band_min if market.deal_band_min is not None else band_cfg.min_purchase,
            market.deal_band_max if market.deal_band_max is not None else band_cfg.max_purchase,
        )
        ref = MarketRef(market.name, market.state)
        result = A1Result(
            market=ref.label,
            market_id=market.id,
            window_since=since.isoformat(),
            window_until=until.isoformat(),
        )
        repo = InvestorRepository(session)
        lists = providers.reference_lists

        records = await providers.deeds.purchases(ref, since, until)
        result.deeds_pulled = len(records)
        candidates = resolve_entities(records)
        result.found = len(candidates)
        reasons: Counter[str] = Counter()
        types: Counter[str] = Counter()
        scored: list[tuple[int, uuid.UUID]] = []

        for cand in candidates:
            entity = EntityCandidate(
                entity_keys=frozenset(cand.entity_keys), purchases=cand.purchases
            )
            excluded = is_excluded(entity, lists, suppressed_entity_keys)
            if excluded is not None:
                reasons[excluded] += 1
                inv_type = (
                    InvestorType.IBUYER_INSTITUTIONAL
                    if excluded == "institutional"
                    else InvestorType.UNKNOWN
                )
                investor, created, added = await self._write(
                    repo, cand, market, investor_type=inv_type, score=None, breakdown=None
                )
                investor.excluded_reason = excluded
                self._count_write(result, created, added)
                continue

            qual = qualifies(RecordSet(cand.is_entity, cand.purchases, band, until))
            if not qual.qualified:
                reasons[qual.reason or "not_qualified"] += 1
                continue

            events = purchase_events(cand)
            inv_type = classify_investor_type(
                events,
                until,
                known_institutional=bool(cand.entity_keys & lists.institutional),
                licensed_agent=bool(cand.entity_keys & lists.licensed_agents),
            )
            n = len(cand.purchases)
            in_market = sum(
                1 for p in cand.purchases if p.city == market.name and p.state == market.state
            )
            features = InvestorFeatures(
                purchase_count_24m=n,
                months_since_last=qual.months_since_last or 0.0,
                cash_rate=sum(1 for p in cand.purchases if is_cash_like(p)) / n,
                band_fit_rate=sum(1 for p in cand.purchases if in_band(p.sale_price, band)) / n,
                market_concentration=in_market / n,
                investor_type=inv_type,
            )
            score = investor_score(features)
            investor, created, added = await self._write(
                repo,
                cand,
                market,
                investor_type=inv_type,
                score=score.total,
                breakdown={
                    **score.breakdown,
                    "features": asdict(features) | {"investor_type": inv_type.value},
                },
            )
            investor.excluded_reason = None
            self._count_write(result, created, added)
            result.qualified += 1
            types[inv_type.value] += 1
            if score.is_prospect:
                result.prospects += 1
                scored.append((score.total, investor.id))
            if score.is_priority:
                result.priority += 1

        await session.flush()
        result.excluded_by_reason = dict(sorted(reasons.items()))
        result.by_type = dict(sorted(types.items()))
        result.prospect_ids = [i for _s, i in sorted(scored, key=lambda t: (-t[0], str(t[1])))]
        log.info(
            "a1.run_complete",
            market=ref.label,
            deeds=result.deeds_pulled,
            found=result.found,
            qualified=result.qualified,
            prospects=result.prospects,
            priority=result.priority,
            excluded=result.excluded_by_reason,
        )
        return result

    @staticmethod
    def _count_write(result: A1Result, created: bool, added: int) -> None:
        if created:
            result.investors_created += 1
        else:
            result.investors_updated += 1
        result.evidence_added += added

    @staticmethod
    async def _write(
        repo: InvestorRepository,
        cand: InvestorCandidate,
        market: Market,
        *,
        investor_type: InvestorType,
        score: int | None,
        breakdown: dict[str, Any] | None,
    ) -> tuple[Investor, bool, int]:
        """Upsert the candidate onto one investor row.

        A candidate merged across several entity keys (shared mailing address, INV-06) is
        keyed by whichever of its keys already has a row, else by its first-seen name, and the
        other spellings become aliases. Every purchase and resale is evidence.
        """
        name = cand.display_name
        for key in sorted(cand.entity_keys):
            existing = await repo.get_by_entity_key(key)
            if existing is not None:
                name = existing.canonical_name
                # SQLite returns DateTime(timezone=True) columns naive; the repository compares
                # against the aware value we pass. Postgres is unaffected.
                if (
                    existing.last_purchase_at is not None
                    and existing.last_purchase_at.tzinfo is None
                ):
                    existing.last_purchase_at = existing.last_purchase_at.replace(tzinfo=UTC)
                break
        last = max(p.sale_date for p in cand.purchases)
        evidence = [_evidence(p, "purchase") for p in cand.purchases] + [
            _evidence(r, "resale") for r in cand.resales
        ]
        refs: dict[str, list[str]] = {}
        for rec in [*cand.purchases, *cand.resales]:
            refs.setdefault(rec.source, []).append(rec.source_ref)
        res = await repo.upsert_by_entity_key(
            name,
            investor_type=investor_type,
            is_entity=cand.is_entity,
            score=score,
            score_breakdown=breakdown,
            market_id=market.id,
            mailing_address=cand.mailing_address(),
            first_name=_first_name(cand),
            purchase_count_24m=len(cand.purchases),
            last_purchase_at=datetime(last.year, last.month, last.day, tzinfo=UTC),
            source_refs=refs,
            evidence=evidence,
        )
        investor = res.investor
        # A1 is the authoritative classifier: the repository only fills an unknown type.
        if investor.investor_type is not investor_type:
            investor.investor_type = investor_type
        for alias in cand.names:
            if alias not in investor.aliases:
                investor.aliases = [*investor.aliases, alias]
        return investor, res.created, res.evidence_added


__all__ = ["A1InvestorDiscovery", "A1Result", "purchase_events"]
