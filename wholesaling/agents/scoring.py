"""Pure scoring and tiering rules for M1 and A1 (REQUIREMENTS.md sections 6 and 7).

Everything here is deterministic code over provider data: no I/O, no model calls, no clock
(callers pass ``as_of``). V2 routes M1 and A1 to "Haiku + code"; the *code* half is this
module, and any model use later is optional on top of it.

Constants and where they come from
----------------------------------
- Signal weights 0.30 / 0.25 / 0.20 / 0.10 / 0.10 / -0.05 ............ M1-02, M1-03 (config/m1.yaml)
- Legal friction 0..3 per state ..................................... M1-02 (config/m1.yaml)
- Data availability is a gate, legal gate at friction 3 ............. M1-02, M1-04 "Excluded"
- Active cap 3..5, Watch -> Active at >=10 qualified buy boxes ....... M1-04 (config/m1.yaml)
- Threshold crossed twice before promotion/demotion ................. M1-07, WF-08
- Deal band: global $75k..$350k; market median x0.5..x1.5 clamped ... M1-06 (config/deal_band.yaml)
- A1 qualification rules ............................................ INV-02
- Investor type rules ............................................... INV-03
- Score weights 30 / 25 / 15 / 15 / 10 / +-5; prospect 50; priority 75  INV-04
- Exclusion classes ................................................. INV-05

Normalisation scales (``SignalScales``) are *not* in V2: they turn raw measures into 0..1
components so the weighted sum is meaningful. They are a documented default to be "retuned from
realized results" (M1-03) and live in one place so a future config move is mechanical.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from wholesaling.config.tunables import DealBand, M1Weights
from wholesaling.db.models.enums import InvestorType, MarketTier
from wholesaling.integrations.data.models import (
    QUALIFYING_FINANCING,
    RESIDENTIAL_1_4,
    DeedRecord,
    MarketSignals,
    ReferenceLists,
)

# --------------------------------------------------------------------------------------------
# M1: market score (M1-02, M1-03)
# --------------------------------------------------------------------------------------------

#: Legal friction at or above this value fails the legal gate (M1-04 "failed legal ... gate").
LEGAL_GATE_FRICTION = 3
#: Legal friction scale maximum (M1-02 "scored 0 to 3").
FRICTION_MAX = 3


@dataclass(frozen=True, slots=True)
class SignalScales:
    """Raw-measure value that maps to a component score of 1.0 (clamped). Defaults are
    documented starting points, not V2 decisions (see module docstring)."""

    entity_purchase_share: float = 0.35
    cash_share: float = 0.50
    repeat_buyer_count: float = 200.0
    i2i_resale_share: float = 0.15
    cash_vs_retail_spread: float = 0.30
    flip_profit_median: float = 60_000.0
    distress_per_1000: float = 40.0
    days_on_market_worst: float = 90.0  # DOM at or above this scores 0
    months_inventory_worst: float = 6.0  # months of inventory at or above this scores 0
    sold_trend_range: float = 0.20  # +-20% YoY maps to 1 / 0


DEFAULT_SCALES = SignalScales()


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _ratio(value: float, scale: float) -> float:
    return _clamp01(value / scale) if scale > 0 else 0.0


@dataclass(frozen=True, slots=True)
class MarketScore:
    """Per-signal components (0..1), their weighted contributions, and gate results."""

    data_gate_passed: bool
    legal_gate_passed: bool
    legal_review_pending: bool
    friction: int | None
    components: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    score: float | None = None

    @property
    def gates_passed(self) -> bool:
        return self.data_gate_passed and self.legal_gate_passed


def market_components(
    signals: MarketSignals, scales: SignalScales = DEFAULT_SCALES
) -> dict[str, float]:
    """Turn raw signals into the five positive components of M1-03, each in 0..1."""
    demand = (
        _ratio(signals.entity_purchase_share, scales.entity_purchase_share)
        + _ratio(signals.cash_share, scales.cash_share)
        + _ratio(signals.repeat_buyer_count, scales.repeat_buyer_count)
        + _ratio(signals.i2i_resale_share, scales.i2i_resale_share)
    ) / 4.0
    spread_parts = [_ratio(signals.cash_vs_retail_spread, scales.cash_vs_retail_spread)]
    if signals.flip_profit_median is not None:
        spread_parts.append(_ratio(float(signals.flip_profit_median), scales.flip_profit_median))
    spread = sum(spread_parts) / len(spread_parts)
    distress = _ratio(signals.distress.total, scales.distress_per_1000)
    liquidity = (
        (1.0 - _ratio(signals.days_on_market, scales.days_on_market_worst))
        + (1.0 - _ratio(signals.months_inventory, scales.months_inventory_worst))
        + _clamp01((signals.sold_trend + scales.sold_trend_range) / (2 * scales.sold_trend_range))
    ) / 3.0
    price_fit = _clamp01(signals.share_in_band)
    return {
        "demand": demand,
        "spread": spread,
        "distress": distress,
        "liquidity": liquidity,
        "price_fit": price_fit,
    }


def market_score(
    signals: MarketSignals,
    weights: M1Weights,
    friction: int | None,
    scales: SignalScales = DEFAULT_SCALES,
) -> MarketScore:
    """M1-03: ``0.30 demand + 0.25 spread + 0.20 distress + 0.10 liquidity + 0.10 price_fit
    - 0.05 friction``, after the data gate.

    ``friction`` is the state's 0..3 score from config, or ``None`` when the state has no
    entry yet. A missing entry scores as 0 but sets ``legal_review_pending`` so the market can
    stay in Watch but never be promoted (config/m1.yaml: every entry must be confirmed by the
    CMP-04 legal review before a market leaves Watch). Friction 3 fails the legal gate.
    The data gate (``data_available=False``) yields ``score=None``.
    """
    legal_pending = friction is None
    effective_friction = 0 if friction is None else friction
    legal_ok = effective_friction < LEGAL_GATE_FRICTION
    if not signals.data_available:
        return MarketScore(
            data_gate_passed=False,
            legal_gate_passed=legal_ok,
            legal_review_pending=legal_pending,
            friction=friction,
        )
    comps = market_components(signals, scales)
    comps["friction"] = effective_friction / FRICTION_MAX
    contributions = {
        "demand": weights.demand * comps["demand"],
        "spread": weights.spread * comps["spread"],
        "distress": weights.distress * comps["distress"],
        "liquidity": weights.liquidity * comps["liquidity"],
        "price_fit": weights.price_fit * comps["price_fit"],
        "friction": weights.friction * comps["friction"],
    }
    return MarketScore(
        data_gate_passed=True,
        legal_gate_passed=legal_ok,
        legal_review_pending=legal_pending,
        friction=friction,
        components=comps,
        contributions=contributions,
        score=round(sum(contributions.values()), 6),
    )


# --------------------------------------------------------------------------------------------
# M1: tiers (M1-04, M1-07)
# --------------------------------------------------------------------------------------------

#: Number of consecutive rescoring runs the proposed tier must differ before it takes effect.
REQUIRED_CROSSINGS = 2


@dataclass(frozen=True, slots=True)
class TierDecision:
    market: str
    current: MarketTier | None
    proposed: MarketTier
    new: MarketTier
    crossings: int
    reason: str
    rank: int | None


def tier_markets(
    scores: Mapping[str, MarketScore],
    active_cap: int,
    promote_at_buy_boxes: int,
    current_tiers: Mapping[str, MarketTier | None],
    buy_box_counts: Mapping[str, int],
    crossings: Mapping[str, int],
) -> dict[str, TierDecision]:
    """Active / Watch / Excluded with the crossed-twice rule.

    Rules, in order, per market:

    1. Data gate or legal gate failed -> ``Excluded`` immediately (M1-04). Gates are facts, not
       thresholds, so the crossed-twice rule does not apply; the counter resets.
    2. Otherwise the *proposed* tier is ``Active`` when the market ranks inside ``active_cap``
       by score among gate-passing markets **and** has at least ``promote_at_buy_boxes``
       qualified buy boxes **and** its legal friction entry is confirmed (not pending); else
       ``Watch`` (M1-01, M1-04). A market that has never been tiered starts in ``Watch``
       regardless of score: nothing is Active on the first run (M1-11 is a human approval).
    3. A change between ``Active`` and ``Watch`` takes effect only when the proposal has
       differed from the current tier on ``REQUIRED_CROSSINGS`` consecutive runs (M1-07,
       WF-08). ``crossings`` carries the count from the previous run; the returned decision
       carries the new count (0 when proposal and current agree).
    4. An ``Excluded`` market whose gates now pass returns to ``Watch`` immediately: that is
       re-entry to the funnel's lowest tier, not a promotion.
    """
    passing = [(m, s) for m, s in scores.items() if s.gates_passed and s.score is not None]
    passing.sort(key=lambda ms: (-(ms[1].score or 0.0), ms[0]))
    ranks = {m: i + 1 for i, (m, _s) in enumerate(passing)}

    decisions: dict[str, TierDecision] = {}
    for market, score in scores.items():
        current = current_tiers.get(market)
        prior_crossings = crossings.get(market, 0)
        rank = ranks.get(market)

        if not score.gates_passed:
            reason = "data_gate_failed" if not score.data_gate_passed else "legal_gate_failed"
            decisions[market] = TierDecision(
                market, current, MarketTier.EXCLUDED, MarketTier.EXCLUDED, 0, reason, None
            )
            continue

        in_top = rank is not None and rank <= active_cap
        enough_buy_boxes = buy_box_counts.get(market, 0) >= promote_at_buy_boxes
        if current is None:
            proposed, reason = MarketTier.WATCH, "new_market_starts_in_watch"
        elif current is MarketTier.EXCLUDED:
            proposed, reason = MarketTier.WATCH, "gates_now_pass_reenters_watch"
        elif in_top and enough_buy_boxes and not score.legal_review_pending:
            proposed, reason = MarketTier.ACTIVE, "top_n_with_buy_boxes"
        elif score.legal_review_pending:
            proposed, reason = MarketTier.WATCH, "legal_review_pending"
        elif not enough_buy_boxes:
            proposed, reason = MarketTier.WATCH, "below_buy_box_threshold"
        else:
            proposed, reason = MarketTier.WATCH, "outside_active_cap"

        if current is None or current is MarketTier.EXCLUDED:
            new, count = proposed, 0
        elif proposed is current:
            new, count = current, 0
        else:
            count = prior_crossings + 1
            if count >= REQUIRED_CROSSINGS:
                new, count = proposed, 0
            else:
                new = current
                reason = f"{reason}_pending_second_crossing"
        decisions[market] = TierDecision(market, current, proposed, new, count, reason, rank)
    return decisions


# --------------------------------------------------------------------------------------------
# M1: deal band (M1-06)
# --------------------------------------------------------------------------------------------

BAND_LOW_FACTOR = Decimal("0.5")
BAND_HIGH_FACTOR = Decimal("1.5")


def deal_band_for_market(
    median_price: Decimal | None, global_band: DealBand
) -> tuple[Decimal, Decimal]:
    """M1-06: ``median x 0.5 .. median x 1.5``, clamped into the global purchase band. Without
    a median the global band applies unchanged."""
    lo, hi = global_band.min_purchase, global_band.max_purchase
    if median_price is None or median_price <= 0:
        return lo, hi
    band_lo = max(lo, min(hi, (median_price * BAND_LOW_FACTOR).quantize(Decimal("1"))))
    band_hi = min(hi, max(lo, (median_price * BAND_HIGH_FACTOR).quantize(Decimal("1"))))
    if band_lo > band_hi:  # degenerate median far outside the global band
        return lo, hi
    return band_lo, band_hi


# --------------------------------------------------------------------------------------------
# A1: qualification (INV-02)
# --------------------------------------------------------------------------------------------

#: INV-02 "individual with >=2 purchases in 24 months".
INDIVIDUAL_MIN_PURCHASES = 2
PURCHASE_WINDOW_MONTHS = 24
#: INV-02 "last purchase <=18 months".
MAX_MONTHS_SINCE_LAST = 18


def months_between(earlier: date, later: date) -> float:
    """Calendar-agnostic month distance (30.4375-day months), never negative."""
    return max(0.0, (later - earlier).days / 30.4375)


def is_cash_like(record: DeedRecord) -> bool:
    return record.financing in QUALIFYING_FINANCING


def is_residential_1_4(record: DeedRecord) -> bool:
    return record.property_type in RESIDENTIAL_1_4 and 1 <= record.units <= 4


def in_band(price: Decimal, band: tuple[Decimal, Decimal]) -> bool:
    return band[0] <= price <= band[1]


@dataclass(frozen=True, slots=True)
class RecordSet:
    """One resolved buyer's purchases in the window, as A1 sees them."""

    is_entity: bool
    purchases: Sequence[DeedRecord]
    band: tuple[Decimal, Decimal]
    as_of: date


@dataclass(frozen=True, slots=True)
class Qualification:
    qualified: bool
    reason: str | None  # first failing rule, in INV-02 order
    qualifying_purchases: tuple[DeedRecord, ...]  # cash-like, residential 1-4, in band
    months_since_last: float | None


def qualifies(record_set: RecordSet) -> Qualification:
    """INV-02, checked in the order written: entity-or-repeat-individual, cash/hard money,
    residential 1-4, price inside band, last purchase <=18 months.

    A record "qualifies" when it is cash-like **and** residential 1-4 **and** in band; the
    buyer needs at least one such record. Each rule reports the *first* failure so counts by
    reason are mutually exclusive.
    """
    purchases = list(record_set.purchases)
    if not purchases:
        return Qualification(False, "no_purchases", (), None)
    if not record_set.is_entity and len(purchases) < INDIVIDUAL_MIN_PURCHASES:
        return Qualification(False, "individual_below_two_purchases", (), None)
    cash = [p for p in purchases if is_cash_like(p)]
    if not cash:
        return Qualification(False, "no_cash_purchase", (), None)
    residential = [p for p in cash if is_residential_1_4(p)]
    if not residential:
        return Qualification(False, "not_residential_1_4", (), None)
    banded = [p for p in residential if in_band(p.sale_price, record_set.band)]
    if not banded:
        return Qualification(False, "outside_deal_band", (), None)
    last = max(p.sale_date for p in purchases)
    since = months_between(last, record_set.as_of)
    if since > MAX_MONTHS_SINCE_LAST:
        return Qualification(False, "stale_last_purchase", tuple(banded), since)
    return Qualification(True, None, tuple(banded), since)


# --------------------------------------------------------------------------------------------
# A1: investor type (INV-03)
# --------------------------------------------------------------------------------------------

FLIP_MIN_HOLD_MONTHS = 4.0
FLIP_MAX_HOLD_MONTHS = 12.0
HOLD_MIN_MONTHS = 12.0
#: Share of purchases resold inside ``FLIP_MIN_HOLD_MONTHS`` that marks a wholesaler
#: ("assignment or double-close patterns, high velocity"). Not in V2; documented default.
WHOLESALER_FAST_RESALE_SHARE = 0.5
WHOLESALER_MIN_FAST_RESALES = 2
#: Share of purchases in one ZIP that counts as "clustered in one ZIP" (buy-and-hold).
CLUSTER_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class PurchaseEvent:
    """A purchase and, when seen in the deed stream, the investor's later sale of it."""

    purchase_date: date
    price: Decimal
    zip: str
    resale_date: date | None = None
    resale_price: Decimal | None = None

    def hold_months(self, as_of: date) -> float:
        return months_between(self.purchase_date, self.resale_date or as_of)


def classify_investor_type(
    purchase_history: Sequence[PurchaseEvent],
    as_of: date,
    *,
    known_institutional: bool = False,
    licensed_agent: bool = False,
) -> InvestorType:
    """INV-03, evaluated in this order:

    1. known entity list -> ``ibuyer_institutional`` (Exclude; A1 never scores these)
    2. licence match -> ``agent_investor`` (Flag, include)
    3. >=2 resales held under 4 months making up >=50% of purchases -> ``wholesaler``
    4. any resale held 4..12 months at a higher price -> ``flipper``
    5. majority of purchases held >12 months, or (>=50% in one ZIP with >=2 purchases, at
       least one held >12 months, and no resales) -> ``buy_and_hold``
    6. otherwise ``unknown``
    """
    if known_institutional:
        return InvestorType.IBUYER_INSTITUTIONAL
    if licensed_agent:
        return InvestorType.AGENT_INVESTOR
    if not purchase_history:
        return InvestorType.UNKNOWN
    n = len(purchase_history)
    resold = [e for e in purchase_history if e.resale_date is not None]
    fast = [e for e in resold if e.hold_months(as_of) < FLIP_MIN_HOLD_MONTHS]
    if len(fast) >= WHOLESALER_MIN_FAST_RESALES and len(fast) / n >= WHOLESALER_FAST_RESALE_SHARE:
        return InvestorType.WHOLESALER
    flips = [
        e
        for e in resold
        if FLIP_MIN_HOLD_MONTHS <= e.hold_months(as_of) <= FLIP_MAX_HOLD_MONTHS
        and e.resale_price is not None
        and e.resale_price > e.price
    ]
    if flips:
        return InvestorType.FLIPPER
    held_long = [
        e
        for e in purchase_history
        if e.resale_date is None and e.hold_months(as_of) > HOLD_MIN_MONTHS
    ]
    zips: dict[str, int] = {}
    for e in purchase_history:
        zips[e.zip] = zips.get(e.zip, 0) + 1
    top_zip = max(zips.values()) if zips else 0
    clustered = n >= 2 and top_zip >= 2 and top_zip / n >= CLUSTER_SHARE
    if len(held_long) * 2 > n or (clustered and held_long and not resold):
        return InvestorType.BUY_AND_HOLD
    return InvestorType.UNKNOWN


# --------------------------------------------------------------------------------------------
# A1: score (INV-04)
# --------------------------------------------------------------------------------------------

W_VOLUME = 30
W_RECENCY = 25
W_CASH_RATE = 15
W_BAND_FIT = 15
W_CONCENTRATION = 10
W_TYPE_BONUS = 5
#: Purchases in 24 months at which the volume component saturates. Not in V2; documented
#: default (an investor buying 6+ homes in two years is at the top of the range we target).
VOLUME_SATURATION = 6
PROSPECT_THRESHOLD = 50
PRIORITY_THRESHOLD = 75

TYPE_BONUS: dict[InvestorType, int] = {
    InvestorType.FLIPPER: W_TYPE_BONUS,  # Priority
    InvestorType.BUY_AND_HOLD: W_TYPE_BONUS,  # Priority
    InvestorType.WHOLESALER: -W_TYPE_BONUS,  # Deprioritize
    InvestorType.IBUYER_INSTITUTIONAL: -W_TYPE_BONUS,  # Excluded before scoring anyway
    InvestorType.AGENT_INVESTOR: 0,  # Flag, include
    InvestorType.UNKNOWN: 0,
}


@dataclass(frozen=True, slots=True)
class InvestorFeatures:
    purchase_count_24m: int
    months_since_last: float
    cash_rate: float  # share of purchases that are cash or hard money
    band_fit_rate: float  # share of purchases inside the market deal band
    market_concentration: float  # share of the investor's purchases inside this market
    investor_type: InvestorType


@dataclass(frozen=True, slots=True)
class InvestorScore:
    total: int
    breakdown: dict[str, float]

    @property
    def is_prospect(self) -> bool:
        return self.total >= PROSPECT_THRESHOLD

    @property
    def is_priority(self) -> bool:
        return self.total >= PRIORITY_THRESHOLD


def investor_score(features: InvestorFeatures) -> InvestorScore:
    """INV-04: volume 30, recency 25, cash rate 15, price-band fit 15, concentration 10,
    type bonus +-5; clamped to 0..100 and rounded half-up.

    - volume   = 30 x min(count_24m, 6) / 6
    - recency  = 25 x (1 - months_since_last / 18), floored at 0
    - cash     = 15 x cash_rate
    - band fit = 15 x band_fit_rate
    - concentration = 10 x market_concentration
    - type bonus from ``TYPE_BONUS``
    """
    volume = W_VOLUME * min(features.purchase_count_24m, VOLUME_SATURATION) / VOLUME_SATURATION
    recency = W_RECENCY * _clamp01(1.0 - features.months_since_last / MAX_MONTHS_SINCE_LAST)
    cash = W_CASH_RATE * _clamp01(features.cash_rate)
    band = W_BAND_FIT * _clamp01(features.band_fit_rate)
    conc = W_CONCENTRATION * _clamp01(features.market_concentration)
    bonus = TYPE_BONUS.get(features.investor_type, 0)
    raw = volume + recency + cash + band + conc + bonus
    total = int(
        Decimal(str(min(100.0, max(0.0, raw)))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return InvestorScore(
        total=total,
        breakdown={
            "volume": round(volume, 3),
            "recency": round(recency, 3),
            "cash_rate": round(cash, 3),
            "band_fit": round(band, 3),
            "concentration": round(conc, 3),
            "type_bonus": float(bonus),
        },
    )


# --------------------------------------------------------------------------------------------
# A1: exclusions (INV-05)
# --------------------------------------------------------------------------------------------

GOVERNMENT_TOKENS = frozenset({"COUNTY", "CITY", "STATE", "AUTHORITY", "HUD", "USA", "FEDERAL"})
GOVERNMENT_PHRASES = (
    "CITY OF",
    "STATE OF",
    "COUNTY OF",
    "HOUSING AUTHORITY",
    "UNITED STATES",
    "SECRETARY OF",
)
NONPROFIT_PHRASES = (
    "HABITAT FOR HUMANITY",
    "FOUNDATION",
    "CHURCH",
    "MINISTRIES",
    "CHARITABLE",
    "NONPROFIT",
    "NON PROFIT",
    "COMMUNITY DEVELOPMENT",
)
LENDER_TOKENS = frozenset({"BANK", "MORTGAGE", "LENDING", "FSB", "BANCORP"})
LENDER_PHRASES = (
    "CREDIT UNION",
    "SAVINGS AND LOAN",
    "FANNIE MAE",
    "FREDDIE MAC",
    "FEDERAL NATIONAL MORTGAGE",
    "FEDERAL HOME LOAN",
)
BUILDER_TOKENS = frozenset({"HOMES", "BUILDERS", "HOMEBUILDERS", "CONSTRUCTION", "DEVELOPMENT"})
#: Share of a builder-named entity's purchases that are land/lots to call it "buying lots".
BUILDER_LOT_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class EntityCandidate:
    """The minimum A1 needs to apply INV-05 to a resolved buyer."""

    entity_keys: frozenset[str]  # every normalised name spelling merged into this candidate
    purchases: Sequence[DeedRecord]


def _tokens(key: str) -> set[str]:
    return set(key.split())


def is_excluded(
    entity: EntityCandidate,
    exclusion_lists: ReferenceLists,
    suppression: Iterable[str] = (),
) -> str | None:
    """INV-05: return the exclusion reason or ``None``. Order: suppression list, known
    institutional list (INV-03 iBuyer), government, nonprofit, lender taking REO, builder
    buying lots. Name rules use the normalised entity key's tokens, never substrings, so
    ``Riverbank Holdings LLC`` does not match ``BANK``."""
    suppressed = set(suppression) | set(exclusion_lists.suppressed)
    keys = set(entity.entity_keys)
    if keys & suppressed:
        return "suppressed"
    if keys & set(exclusion_lists.institutional):
        return "institutional"
    for key in keys:
        toks = _tokens(key)
        padded = f" {key} "
        if toks & GOVERNMENT_TOKENS and (
            any(f" {p} " in padded for p in GOVERNMENT_PHRASES)
            or toks & {"COUNTY", "HUD", "AUTHORITY"}
        ):
            return "government"
        if any(f" {p} " in padded for p in NONPROFIT_PHRASES):
            return "nonprofit"
        if toks & LENDER_TOKENS or any(f" {p} " in padded for p in LENDER_PHRASES):
            return "lender_reo"
        if toks & BUILDER_TOKENS and entity.purchases:
            lots = sum(1 for p in entity.purchases if p.property_type == "land")
            if lots / len(entity.purchases) >= BUILDER_LOT_SHARE:
                return "builder_lots"
    return None


__all__ = [
    "DEFAULT_SCALES",
    "INDIVIDUAL_MIN_PURCHASES",
    "LEGAL_GATE_FRICTION",
    "MAX_MONTHS_SINCE_LAST",
    "PRIORITY_THRESHOLD",
    "PROSPECT_THRESHOLD",
    "PURCHASE_WINDOW_MONTHS",
    "REQUIRED_CROSSINGS",
    "TYPE_BONUS",
    "EntityCandidate",
    "InvestorFeatures",
    "InvestorScore",
    "MarketScore",
    "PurchaseEvent",
    "Qualification",
    "RecordSet",
    "SignalScales",
    "TierDecision",
    "classify_investor_type",
    "deal_band_for_market",
    "in_band",
    "investor_score",
    "is_cash_like",
    "is_excluded",
    "is_residential_1_4",
    "market_components",
    "market_score",
    "months_between",
    "qualifies",
    "tier_markets",
]
