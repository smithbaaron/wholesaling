"""S1 Seller List Builder (REQUIREMENTS.md AGT-S1, SEL-01, SEL-02, WF-10; build step BO-10).

Deterministic code over ``DistressRecord``s (V2 "Code"): no model is involved.

    distress lists for one Active market
      -> merge rows per parcel (one vendor list per signal is common)
      -> derive ``long_term_hold`` from the last sale date (>= 15 years)
      -> gate: residential 1-4 unit; a value we can band (assessed, else estimated) inside the
         market deal band; >= 2 stacked signals (SEL-02)
      -> suppression: owner on the reference suppression list, or a seller lead on record for
         the parcel / normalised address that is suppressed or dead
      -> dedupe: an existing lead for the parcel / address is *refreshed* (signals, score,
         values), never duplicated (WF-10)
      -> new leads written ``status=queued`` with the stacked signals and score

Markets that are not ``Active`` are refused with a clear result (SEL-01: seller outreach starts
only after a market is Active), not an exception, so a weekly schedule over every market is a
no-op for Watch / Excluded ones. Rerunning on the same lists changes nothing (idempotent weekly
refresh).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.config.tunables import Tunables, get_tunables
from wholesaling.db.base import utcnow
from wholesaling.db.models.enums import AgentId, MarketTier, SellerLeadStatus
from wholesaling.db.models.markets import Market
from wholesaling.db.models.sellers import SellerLead
from wholesaling.db.normalize import normalize_address, normalize_entity_name
from wholesaling.db.repositories.sellers import SellerRepository
from wholesaling.integrations.data.distress import DISTRESS_SIGNALS, DistressRecord
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.models import RESIDENTIAL_1_4, MarketRef
from wholesaling.logging import get_logger

log = get_logger(__name__)

# SEL-02: "score by stacked signals". Points per signal are documented defaults (not V2
# numbers): the strongest motivation signals first. Retune from realized reply rates.
SIGNAL_WEIGHTS: Final[dict[str, int]] = {
    "pre_foreclosure": 30,
    "tax_delinquent": 25,
    "probate": 25,
    "absentee": 15,
    "vacant": 15,
    "code_violation": 15,
    "long_term_hold": 10,
    "expired_listing": 10,
}
MIN_SIGNALS_FOR_QUEUE: Final[int] = 2  # SEL-02 ">= 2 signals -> outreach queue"
LONG_TERM_HOLD_YEARS: Final[int] = 15  # SEL-02 "long-term hold (>= 15 yrs)"

REFUSED_NOT_ACTIVE = "market_not_active"
REFUSED_NO_PROVIDER = "no_distress_provider"

REASON_NOT_RESIDENTIAL = "not_residential_1_4"
REASON_NO_VALUE = "no_value"
REASON_OUTSIDE_BAND = "outside_deal_band"
REASON_TOO_FEW_SIGNALS = "fewer_than_two_signals"
REASON_SUPPRESSED = "suppressed"
REASON_LEAD_SUPPRESSED = "lead_suppressed_or_dead"

# Leads in these states are final: a refresh never re-queues them.
FINAL_STATUSES: Final[frozenset[SellerLeadStatus]] = frozenset(
    {SellerLeadStatus.SUPPRESSED, SellerLeadStatus.DEAD}
)


@dataclass(frozen=True, slots=True)
class ScoredParcel:
    """One parcel after merging and scoring, before the gates."""

    record: DistressRecord
    signals: tuple[str, ...]
    score: int
    value: Decimal | None  # the value banded (assessed first, else estimated)

    @property
    def address_key(self) -> str:
        r = self.record
        return normalize_address(r.address, r.city, r.state, r.zip)


@dataclass(slots=True)
class S1Result:
    market: str
    market_id: uuid.UUID
    refused_reason: str | None = None
    records_pulled: int = 0
    parcels: int = 0  # distinct parcels after merging
    queued: int = 0  # passed every gate (new + refreshed)
    created: int = 0
    refreshed: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    by_signal: dict[str, int] = field(default_factory=dict)  # among queued parcels
    queued_lead_ids: list[uuid.UUID] = field(default_factory=list)  # score desc

    @property
    def refused(self) -> bool:
        return self.refused_reason is not None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["market_id"] = str(self.market_id)
        d["queued_lead_ids"] = [str(i) for i in self.queued_lead_ids]
        return d


# --------------------------------------------------------------------------------------------
# Pure rules
# --------------------------------------------------------------------------------------------


def merge_records(records: list[DistressRecord]) -> list[list[DistressRecord]]:
    """Group rows by parcel id (vendors return one row per list), first-seen order."""
    groups: dict[str, list[DistressRecord]] = {}
    for r in records:
        groups.setdefault(r.parcel_id, []).append(r)
    return list(groups.values())


def is_long_term_hold(last_sale: date | None, as_of: date) -> bool:
    if last_sale is None:
        return False
    years = (as_of - last_sale).days / 365.25
    return years >= LONG_TERM_HOLD_YEARS


def stacked_signals(rows: list[DistressRecord], as_of: date) -> tuple[str, ...]:
    """Union of the vendor signals plus the derived ``long_term_hold``, in SEL-02 order."""
    present: set[str] = set()
    for r in rows:
        present.update(s for s in r.signals if s in DISTRESS_SIGNALS)
        if is_long_term_hold(r.last_sale_date, as_of):
            present.add("long_term_hold")
    return tuple(s for s in DISTRESS_SIGNALS if s in present)


def signal_score(signals: tuple[str, ...]) -> int:
    return sum(SIGNAL_WEIGHTS.get(s, 0) for s in signals)


def banded_value(rows: list[DistressRecord]) -> Decimal | None:
    """Assessed value when any row has one, else estimated; ``None`` when neither exists."""
    for r in rows:
        if r.assessed_value is not None:
            return r.assessed_value
    for r in rows:
        if r.estimated_value is not None:
            return r.estimated_value
    return None


def is_residential_1_4(record: DistressRecord) -> bool:
    return record.property_type in RESIDENTIAL_1_4 and 1 <= record.units <= 4


def score_parcel(rows: list[DistressRecord], as_of: date) -> ScoredParcel:
    signals = stacked_signals(rows, as_of)
    return ScoredParcel(
        record=rows[0], signals=signals, score=signal_score(signals), value=banded_value(rows)
    )


def gate_reason(parcel: ScoredParcel, band: tuple[Decimal, Decimal]) -> str | None:
    """Why ``parcel`` is not queued (SEL-02), or ``None`` when it passes every gate."""
    if not is_residential_1_4(parcel.record):
        return REASON_NOT_RESIDENTIAL
    if parcel.value is None:
        return REASON_NO_VALUE
    low, high = band
    if not low <= parcel.value <= high:
        return REASON_OUTSIDE_BAND
    if len(parcel.signals) < MIN_SIGNALS_FOR_QUEUE:
        return REASON_TOO_FEW_SIGNALS
    return None


def owner_first_name(owner_name: str) -> str | None:
    """A plausible first name for the greeting; ``None`` for estates, entities and ``Last,
    First`` forms we cannot read safely."""
    name = owner_name.strip()
    if not name or "," in name or "&" in name:
        return None
    lowered = name.lower()
    if lowered.startswith(("estate of", "the estate", "trust", "heirs of")):
        return None
    tokens = name.split()
    if len(tokens) < 2:
        return None
    key = normalize_entity_name(name)
    entity_markers = {"LLC", "LP", "INC", "TRUST", "CORP", "CO", "LTD", "LLP", "HOLDINGS"}
    if set(key.split()) & entity_markers:
        return None
    return tokens[0].capitalize()


# --------------------------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------------------------


class S1SellerListBuilder:
    agent_id: Final = AgentId.S1

    def __init__(self, tunables: Tunables | None = None) -> None:
        self._tunables = tunables or get_tunables()

    def deal_band(self, market: Market) -> tuple[Decimal, Decimal]:
        cfg = self._tunables.deal_band
        return (
            market.deal_band_min if market.deal_band_min is not None else cfg.min_purchase,
            market.deal_band_max if market.deal_band_max is not None else cfg.max_purchase,
        )

    async def run(
        self,
        session: AsyncSession,
        providers: DataProviders,
        market: Market,
        *,
        as_of: datetime | None = None,
    ) -> S1Result:
        """Build / refresh the seller list for one market. Writes queued leads; commits nothing
        (the caller owns the transaction)."""
        ref = MarketRef(market.name, market.state)
        result = S1Result(market=ref.label, market_id=market.id)
        if market.tier is not MarketTier.ACTIVE:
            result.refused_reason = REFUSED_NOT_ACTIVE
            log.info("s1.refused", market=ref.label, tier=market.tier.value)
            return result
        if providers.distress is None:
            result.refused_reason = REFUSED_NO_PROVIDER
            return result

        as_of_date = (as_of or utcnow()).date()
        band = self.deal_band(market)
        records = await providers.distress.distress_records(ref)
        result.records_pulled = len(records)
        groups = merge_records(records)
        result.parcels = len(groups)

        repo = SellerRepository(session)
        suppressed_owners = providers.reference_lists.suppressed
        reasons: Counter[str] = Counter()
        by_signal: Counter[str] = Counter()
        scored: list[tuple[int, uuid.UUID]] = []

        for rows in groups:
            parcel = score_parcel(rows, as_of_date)
            reason = gate_reason(parcel, band)
            if reason is not None:
                reasons[reason] += 1
                continue
            rec = parcel.record
            if normalize_entity_name(rec.owner_name) in suppressed_owners:
                reasons[REASON_SUPPRESSED] += 1
                continue
            address_key = parcel.address_key
            existing = await repo.find_existing(parcel_id=rec.parcel_id, address_key=address_key)
            if existing is not None and existing.status in FINAL_STATUSES:
                reasons[REASON_LEAD_SUPPRESSED] += 1
                continue
            lead = await self._write(repo, market.id, existing, parcel, address_key, rows)
            if existing is None:
                result.created += 1
            else:
                result.refreshed += 1
            result.queued += 1
            by_signal.update(parcel.signals)
            scored.append((parcel.score, lead.id))

        scored.sort(key=lambda t: (-t[0], str(t[1])))
        result.queued_lead_ids = [i for _, i in scored]
        result.rejected_by_reason = dict(sorted(reasons.items()))
        result.by_signal = dict(sorted(by_signal.items()))
        log.info(
            "s1.completed", **{k: v for k, v in result.to_json().items() if k != "queued_lead_ids"}
        )
        return result

    async def _write(
        self,
        repo: SellerRepository,
        market_id: uuid.UUID,
        existing: SellerLead | None,
        parcel: ScoredParcel,
        address_key: str,
        rows: list[DistressRecord],
    ) -> SellerLead:
        rec = parcel.record
        source_refs = {
            "distress": [
                {
                    "source": r.source,
                    "source_ref": r.source_ref,
                    "source_url": r.source_url,
                    "signals": list(r.signals),
                    "retrieved_at": r.retrieved_at.isoformat(),
                }
                for r in rows
            ],
            "owner_mailing_address": rec.owner_mailing_address,
            "owner_mailing_city": rec.owner_mailing_city,
            "owner_mailing_state": rec.owner_mailing_state,
            "owner_mailing_zip": rec.owner_mailing_zip,
            "last_sale_date": rec.last_sale_date.isoformat() if rec.last_sale_date else None,
            "property_type": rec.property_type,
            "units": rec.units,
        }
        if existing is not None:
            return await repo.refresh_from_distress(
                existing,
                signals=list(parcel.signals),
                signal_score=parcel.score,
                assessed_value=banded_assessed(rows),
                estimated_value=banded_estimated(rows),
                source_refs=source_refs,
            )
        return await repo.create_from_distress(
            market_id=market_id,
            owner_name=rec.owner_name,
            address_line1=rec.address,
            city=rec.city,
            state=rec.state,
            postal_code=rec.zip,
            address_key=address_key,
            parcel_id=rec.parcel_id,
            signals=list(parcel.signals),
            signal_score=parcel.score,
            assessed_value=banded_assessed(rows),
            estimated_value=banded_estimated(rows),
            source=rec.source,
            source_refs=source_refs,
        )


def banded_assessed(rows: list[DistressRecord]) -> Decimal | None:
    return next((r.assessed_value for r in rows if r.assessed_value is not None), None)


def banded_estimated(rows: list[DistressRecord]) -> Decimal | None:
    return next((r.estimated_value for r in rows if r.estimated_value is not None), None)


__all__ = [
    "FINAL_STATUSES",
    "LONG_TERM_HOLD_YEARS",
    "MIN_SIGNALS_FOR_QUEUE",
    "REASON_LEAD_SUPPRESSED",
    "REASON_NOT_RESIDENTIAL",
    "REASON_NO_VALUE",
    "REASON_OUTSIDE_BAND",
    "REASON_SUPPRESSED",
    "REASON_TOO_FEW_SIGNALS",
    "REFUSED_NOT_ACTIVE",
    "REFUSED_NO_PROVIDER",
    "SIGNAL_WEIGHTS",
    "S1Result",
    "S1SellerListBuilder",
    "ScoredParcel",
    "banded_value",
    "gate_reason",
    "is_long_term_hold",
    "is_residential_1_4",
    "merge_records",
    "owner_first_name",
    "score_parcel",
    "signal_score",
    "stacked_signals",
]
