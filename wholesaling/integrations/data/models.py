"""Provider-neutral records exchanged with data providers (REQUIREMENTS.md INT-03, INV-02..08).

These are the *only* shapes M1, A1 and A2 see. A real provider (ATTOM, PropStream, BatchData,
a county API) is adapted into these dataclasses; the agents never learn provider-specific
fields. Everything is a frozen dataclass so provider output can be hashed, compared and
serialised into fixtures.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

Financing = Literal["cash", "hard_money", "conventional", "unknown"]
PropertyType = Literal["sfr", "duplex", "triplex", "fourplex", "multifamily", "land", "other"]
BuyerEntityType = Literal["llc", "lp", "trust", "corp", "individual", "government", "unknown"]

# Residential 1 to 4 unit property types (INV-02). ``multifamily`` (5+) and ``land`` are
# "configurable later" per V2 and stay out of this set.
RESIDENTIAL_1_4: frozenset[str] = frozenset({"sfr", "duplex", "triplex", "fourplex"})
# Financing types that satisfy INV-02 "cash or hard-money purchase (no conventional mortgage)".
QUALIFYING_FINANCING: frozenset[str] = frozenset({"cash", "hard_money"})


@dataclass(frozen=True, slots=True)
class MarketRef:
    """A seed market as named in ``config/m1.yaml`` (``"Houston, TX"``)."""

    name: str
    state: str

    @classmethod
    def parse(cls, label: str) -> MarketRef:
        name, _, state = label.rpartition(",")
        name = name.strip()
        state = state.strip().upper()
        if not name or len(state) != 2:
            msg = f"market label must look like 'City, ST': {label!r}"
            raise ValueError(msg)
        return cls(name=name, state=state)

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


@dataclass(frozen=True, slots=True)
class DeedRecord:
    """One recorded purchase (grantor to grantee) in a market.

    ``buyer_entity_type`` is the provider's *guess*; A1 re-derives entity-ness from the name
    with ``normalize_entity_name`` and only uses this field as a tie-breaker. ``financing`` is
    ``unknown`` when no mortgage search was possible; A1 treats unknown as *not* cash.
    """

    parcel_id: str
    address: str
    city: str
    state: str
    zip: str
    sale_date: date
    sale_price: Decimal
    buyer_name: str
    buyer_entity_type: BuyerEntityType
    seller_name: str
    financing: Financing
    property_type: PropertyType
    units: int
    source: str
    source_ref: str
    source_url: str | None
    retrieved_at: datetime
    buyer_mailing_address: str | None = None
    buyer_mailing_city: str | None = None
    buyer_mailing_state: str | None = None
    buyer_mailing_zip: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["sale_date"] = self.sale_date.isoformat()
        d["sale_price"] = str(self.sale_price)
        d["retrieved_at"] = self.retrieved_at.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DeedRecord:
        return cls(
            parcel_id=d["parcel_id"],
            address=d["address"],
            city=d["city"],
            state=d["state"],
            zip=d["zip"],
            sale_date=date.fromisoformat(d["sale_date"]),
            sale_price=Decimal(d["sale_price"]),
            buyer_name=d["buyer_name"],
            buyer_entity_type=d["buyer_entity_type"],
            seller_name=d["seller_name"],
            financing=d["financing"],
            property_type=d["property_type"],
            units=int(d["units"]),
            source=d["source"],
            source_ref=d["source_ref"],
            source_url=d.get("source_url"),
            retrieved_at=datetime.fromisoformat(d["retrieved_at"]),
            buyer_mailing_address=d.get("buyer_mailing_address"),
            buyer_mailing_city=d.get("buyer_mailing_city"),
            buyer_mailing_state=d.get("buyer_mailing_state"),
            buyer_mailing_zip=d.get("buyer_mailing_zip"),
        )


@dataclass(frozen=True, slots=True)
class DistressSignals:
    """Distress supply per 1,000 parcels (M1-02 "Distress supply")."""

    pre_foreclosure: float = 0.0
    tax_delinquent: float = 0.0
    probate: float = 0.0
    absentee: float = 0.0
    vacancy: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.pre_foreclosure + self.tax_delinquent + self.probate + self.absentee + self.vacancy
        )


@dataclass(frozen=True, slots=True)
class MarketSignals:
    """Raw M1 inputs for one market (V2 M1 signal table; M1-02).

    Shares are fractions (0.18, not 18). ``data_available`` is the M1 gate: when false the
    other fields may be zero or stale and the market is Excluded regardless of score.
    """

    market: MarketRef
    data_available: bool
    # Investor demand (weight 0.30)
    entity_purchase_share: float = 0.0
    cash_share: float = 0.0
    repeat_buyer_count: int = 0  # buyers with >=3 purchases in 12 months
    i2i_resale_share: float = 0.0  # investor-to-investor resales as a share of sales
    # Spread (0.25)
    cash_vs_retail_spread: float = 0.0  # (retail - cash) / retail for similar sqft/age
    flip_profit_median: Decimal | None = None
    # Distress supply (0.20)
    distress: DistressSignals = field(default_factory=DistressSignals)
    # Liquidity (0.10)
    days_on_market: float = 0.0
    months_inventory: float = 0.0
    sold_trend: float = 0.0  # YoY change in sold count, fraction (+0.05 = up 5%)
    # Price band fit (0.10)
    share_in_band: float = 0.0
    median_price: Decimal | None = None
    source: str = "unknown"
    retrieved_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["market"] = {"name": self.market.name, "state": self.market.state}
        d["flip_profit_median"] = (
            None if self.flip_profit_median is None else str(self.flip_profit_median)
        )
        d["median_price"] = None if self.median_price is None else str(self.median_price)
        d["retrieved_at"] = None if self.retrieved_at is None else self.retrieved_at.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MarketSignals:
        distress = d.get("distress") or {}
        return cls(
            market=MarketRef(name=d["market"]["name"], state=d["market"]["state"]),
            data_available=bool(d["data_available"]),
            entity_purchase_share=float(d.get("entity_purchase_share", 0.0)),
            cash_share=float(d.get("cash_share", 0.0)),
            repeat_buyer_count=int(d.get("repeat_buyer_count", 0)),
            i2i_resale_share=float(d.get("i2i_resale_share", 0.0)),
            cash_vs_retail_spread=float(d.get("cash_vs_retail_spread", 0.0)),
            flip_profit_median=(
                None if d.get("flip_profit_median") is None else Decimal(d["flip_profit_median"])
            ),
            distress=DistressSignals(**{k: float(v) for k, v in distress.items()}),
            days_on_market=float(d.get("days_on_market", 0.0)),
            months_inventory=float(d.get("months_inventory", 0.0)),
            sold_trend=float(d.get("sold_trend", 0.0)),
            share_in_band=float(d.get("share_in_band", 0.0)),
            median_price=None if d.get("median_price") is None else Decimal(d["median_price"]),
            source=d.get("source", "unknown"),
            retrieved_at=(
                None if d.get("retrieved_at") is None else datetime.fromisoformat(d["retrieved_at"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ContactCandidate:
    """One contact channel found by an enrichment provider (INV-08, INV-09).

    ``use_limitations`` is free text copied from the provider licence (for example "B2B
    outreach only; no SMS"), stored on the ``contacts`` row so later agents can honour it.
    """

    channel: Literal["email", "phone", "address"]
    value: str
    source: str
    provenance: str
    confidence: float
    use_limitations: str | None = None
    provenance_url: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ContactCandidate:
        return cls(
            channel=d["channel"],
            value=d["value"],
            source=d["source"],
            provenance=d["provenance"],
            confidence=float(d["confidence"]),
            use_limitations=d.get("use_limitations"),
            provenance_url=d.get("provenance_url"),
        )


@dataclass(frozen=True, slots=True)
class ReferenceLists:
    """Known-entity lists used by A1 (INV-03 iBuyer list, INV-05 suppression, agent licences).

    Values are *normalised entity keys* (``normalize_entity_name``). In mock mode they come
    from the fixtures; a live deployment loads them from a versioned config file.
    """

    institutional: frozenset[str] = frozenset()
    licensed_agents: frozenset[str] = frozenset()
    suppressed: frozenset[str] = frozenset()


__all__ = [
    "QUALIFYING_FINANCING",
    "RESIDENTIAL_1_4",
    "BuyerEntityType",
    "ContactCandidate",
    "DeedRecord",
    "DistressSignals",
    "Financing",
    "MarketRef",
    "MarketSignals",
    "PropertyType",
    "ReferenceLists",
]
