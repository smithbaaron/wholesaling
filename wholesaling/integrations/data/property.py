"""Property data for the A7 offer engine and D2 matching (REQUIREMENTS.md A7-02, A7-03, A7-12,
A7-13, A7-15, A7-17, BB-07, BB-12; INT-01..03).

What A7 needs from the outside world, as one Protocol:

- parcel facts from two independent sources (county, provider) plus a third fallback (V1);
- sold comps with radius / recency / sqft / age filters (step 2, V2 tier expansion, V6 re-pull);
- a provider AVM (V2 dual-method ARV);
- listing remarks and photos (V4 repair cross-check; a stub until a photo model exists);
- active listings for D2 (BB-12: the same sources as comps; off-market candidates come from
  A7 contracts in the database, not from here).

No provider has been selected (INT-03 is OPEN), so only the fixture-backed mock exists and is
selected by ``DATA_PROVIDER_MODE=mock``. The mock is deterministic: every answer comes from
``fixtures/properties.json`` / ``fixtures/listings.json`` and a fixed ``today``; tests move time
with ``advance_clock`` and simulate a comp recorded between draft and send with ``inject_comp``.

Every record is a frozen dataclass with ``to_json`` / ``from_json`` so the verification record
(A7-18) can embed exactly what the engine saw.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from wholesaling.db.normalize import normalize_address
from wholesaling.integrations.base import HealthStatus, IntegrationMode

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEFAULT_TODAY = date(2026, 10, 25)

SOURCE_COUNTY = "county"
SOURCE_PROVIDER = "provider"
SOURCE_THIRD = "third"
# The five facts V1 compares across sources (A7-12).
FACT_FIELDS: tuple[str, ...] = ("sqft", "beds", "baths", "year_built", "lot_sqft")


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    return date.fromisoformat(str(value))


def _json_safe(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def address_key(line1: str, city: str | None, state: str | None, postal_code: str | None) -> str:
    """The dedupe key used by the mock and by D2 (``normalize.normalize_address``)."""
    return normalize_address(line1, city, state, postal_code)


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PropertyAddress:
    line1: str
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None

    @property
    def key(self) -> str:
        return address_key(self.line1, self.city, self.state, self.postal_code)

    @property
    def display(self) -> str:
        parts = [self.line1.strip()]
        locality = ", ".join(p for p in (self.city, self.state) if p)
        if locality:
            parts.append(locality)
        text = ", ".join(parts)
        return f"{text} {self.postal_code}".strip() if self.postal_code else text


@dataclass(frozen=True, slots=True)
class ParcelFacts:
    """Beds / baths / sqft / year / lot from one source (A7-02, A7-12)."""

    source: str
    parcel_id: str | None
    beds: int | None = None
    baths: Decimal | None = None
    sqft: int | None = None
    year_built: int | None = None
    lot_sqft: int | None = None
    property_type: str = "sfr"  # sfr | duplex | triplex | fourplex | condo | land | commercial
    units: int = 1
    last_sale_date: date | None = None
    last_sale_price: Decimal | None = None
    owner_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ParcelFacts:
        return cls(
            source=str(d["source"]),
            parcel_id=d.get("parcel_id"),
            beds=d.get("beds"),
            baths=_dec(d.get("baths")),
            sqft=d.get("sqft"),
            year_built=d.get("year_built"),
            lot_sqft=d.get("lot_sqft"),
            property_type=str(d.get("property_type", "sfr")),
            units=int(d.get("units", 1)),
            last_sale_date=_date(d.get("last_sale_date")),
            last_sale_price=_dec(d.get("last_sale_price")),
            owner_name=d.get("owner_name"),
        )


@dataclass(frozen=True, slots=True)
class SoldComp:
    """One recorded sale near the subject (A7-03). ``distance_miles`` is from the subject."""

    comp_id: str
    parcel_id: str | None
    address: str
    postal_code: str
    sale_price: Decimal
    sale_date: date
    sqft: int
    distance_miles: Decimal
    beds: int | None = None
    baths: Decimal | None = None
    year_built: int | None = None
    lot_sqft: int | None = None
    seller_name: str | None = None
    buyer_name: str | None = None
    prior_sale_date: date | None = None
    prior_sale_price: Decimal | None = None
    remarks: str | None = None
    source: str = "provider"

    @property
    def price_per_sqft(self) -> Decimal:
        return (self.sale_price / Decimal(self.sqft)).quantize(Decimal("0.0001"))

    def to_json(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SoldComp:
        sale_price = _dec(d["sale_price"])
        sale_date = _date(d["sale_date"])
        distance = _dec(d["distance_miles"])
        assert sale_price is not None and sale_date is not None and distance is not None
        return cls(
            comp_id=str(d["comp_id"]),
            parcel_id=d.get("parcel_id"),
            address=str(d["address"]),
            postal_code=str(d["postal_code"]),
            sale_price=sale_price,
            sale_date=sale_date,
            sqft=int(d["sqft"]),
            distance_miles=distance,
            beds=d.get("beds"),
            baths=_dec(d.get("baths")),
            year_built=d.get("year_built"),
            lot_sqft=d.get("lot_sqft"),
            seller_name=d.get("seller_name"),
            buyer_name=d.get("buyer_name"),
            prior_sale_date=_date(d.get("prior_sale_date")),
            prior_sale_price=_dec(d.get("prior_sale_price")),
            remarks=d.get("remarks"),
            source=str(d.get("source", "provider")),
        )


@dataclass(frozen=True, slots=True)
class CompQuery:
    """Step-2 filters. ``as_of`` is the day the engine runs (recency is measured from it)."""

    postal_code: str
    subject_sqft: int
    subject_year_built: int | None
    max_miles: Decimal
    max_days: int
    as_of: date
    parcel_id: str | None = None  # the subject; the mock keys its per-subject comp pools on it
    sqft_tolerance_pct: Decimal = Decimal("0.20")
    year_tolerance: int = 15

    def accepts(self, comp: SoldComp) -> bool:
        if comp.postal_code != self.postal_code:
            return False
        lo = Decimal(self.subject_sqft) * (Decimal(1) - self.sqft_tolerance_pct)
        hi = Decimal(self.subject_sqft) * (Decimal(1) + self.sqft_tolerance_pct)
        if not (lo <= Decimal(comp.sqft) <= hi):
            return False
        if (
            self.subject_year_built is not None
            and comp.year_built is not None
            and abs(comp.year_built - self.subject_year_built) > self.year_tolerance
        ):
            return False
        age_days = (self.as_of - comp.sale_date).days
        if age_days < 0 or age_days > self.max_days:
            return False
        return comp.distance_miles <= self.max_miles

    def to_json(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class ListingInfo:
    """Public listing remarks and photo references for the subject (A7-15). Photos are a stub:
    the engine counts them and records the URLs; no image model runs in this build."""

    parcel_id: str | None
    remarks: str | None = None
    photo_urls: tuple[str, ...] = ()
    list_price: Decimal | None = None
    status: str | None = None  # active | pending | expired | off_market
    days_on_market: int | None = None

    def to_json(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ListingInfo:
        return cls(
            parcel_id=d.get("parcel_id"),
            remarks=d.get("remarks"),
            photo_urls=tuple(d.get("photo_urls") or ()),
            list_price=_dec(d.get("list_price")),
            status=d.get("status"),
            days_on_market=d.get("days_on_market"),
        )


@dataclass(frozen=True, slots=True)
class ActiveListing:
    """A for-sale listing D2 may rank against a search profile (BB-07, BB-12)."""

    listing_id: str
    source: str
    address: PropertyAddress
    parcel_id: str | None
    list_price: Decimal | None
    property_type: str = "sfr"
    units: int = 1
    beds: int | None = None
    baths: Decimal | None = None
    sqft: int | None = None
    year_built: int | None = None
    lot_sqft: int | None = None
    condition_hint: str | None = None  # light | medium | heavy | gut | None (unknown)
    remarks: str | None = None
    hoa: bool | None = None
    financing_terms: str | None = None  # e.g. "cash only"
    title_notes: str | None = None
    status: str = "active"
    days_on_market: int | None = None
    source_url: str | None = None
    retrieved_at: date | None = None
    market: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["address"] = asdict(self.address)
        return _json_safe(d)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ActiveListing:
        addr = d["address"]
        return cls(
            listing_id=str(d["listing_id"]),
            source=str(d.get("source", "provider")),
            address=PropertyAddress(
                line1=str(addr["line1"]),
                city=addr.get("city"),
                state=addr.get("state"),
                postal_code=addr.get("postal_code"),
            ),
            parcel_id=d.get("parcel_id"),
            list_price=_dec(d.get("list_price")),
            property_type=str(d.get("property_type", "sfr")),
            units=int(d.get("units", 1)),
            beds=d.get("beds"),
            baths=_dec(d.get("baths")),
            sqft=d.get("sqft"),
            year_built=d.get("year_built"),
            lot_sqft=d.get("lot_sqft"),
            condition_hint=d.get("condition_hint"),
            remarks=d.get("remarks"),
            hoa=d.get("hoa"),
            financing_terms=d.get("financing_terms"),
            title_notes=d.get("title_notes"),
            status=str(d.get("status", "active")),
            days_on_market=d.get("days_on_market"),
            source_url=d.get("source_url"),
            retrieved_at=_date(d.get("retrieved_at")),
            market=d.get("market"),
        )


@dataclass(frozen=True, slots=True)
class ListingSearch:
    """D2 query: any of the ZIPs or cities, in ``state``; empty tuples mean "no filter"."""

    postal_codes: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    state: str | None = None
    markets: tuple[str, ...] = ()

    def accepts(self, listing: ActiveListing) -> bool:
        if listing.status != "active":
            return False
        if self.state and (listing.address.state or "").upper() != self.state.upper():
            return False
        if not (self.postal_codes or self.cities or self.markets):
            return True
        if self.postal_codes and listing.address.postal_code in self.postal_codes:
            return True
        cities = {c.strip().lower() for c in self.cities}
        if cities and (listing.address.city or "").strip().lower() in cities:
            return True
        markets = {m.strip().lower() for m in self.markets}
        return bool(markets and (listing.market or "").strip().lower() in markets)


# --------------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------------


@runtime_checkable
class PropertyDataProvider(Protocol):
    name: str
    mode: IntegrationMode

    async def county_facts(self, address: PropertyAddress) -> ParcelFacts | None:
        """Parcel facts from the county assessor / appraisal district (source 1)."""
        ...

    async def provider_facts(self, address: PropertyAddress) -> ParcelFacts | None:
        """Parcel facts from the data provider (source 2, independent of the county)."""
        ...

    async def third_source_facts(self, address: PropertyAddress) -> ParcelFacts | None:
        """A third opinion, pulled only when the first two disagree by >10% (A7-12)."""
        ...

    async def sold_comps(self, query: CompQuery) -> list[SoldComp]:
        """Closed sales that satisfy every filter in ``query``."""
        ...

    async def avm(self, parcel_id: str) -> Decimal | None:
        """Provider automated valuation, or ``None`` when the provider has none (A7-13)."""
        ...

    async def listing_info(self, parcel_id: str) -> ListingInfo | None:
        """Listing remarks / photos for the subject, if it was ever listed (A7-15)."""
        ...

    async def active_listings(self, search: ListingSearch) -> list[ActiveListing]:
        """Active for-sale listings inside ``search`` (BB-12)."""
        ...

    async def healthcheck(self) -> HealthStatus: ...


# --------------------------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SubjectFixture:
    """Everything the mock knows about one subject address."""

    key: str
    county: ParcelFacts | None
    provider: ParcelFacts | None
    third: ParcelFacts | None
    avm: Decimal | None
    listing: ListingInfo | None
    comps: list[SoldComp]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SubjectFixture:
        addr = d["address"]
        key = address_key(
            addr["line1"], addr.get("city"), addr.get("state"), addr.get("postal_code")
        )
        return cls(
            key=key,
            county=ParcelFacts.from_json(d["county"]) if d.get("county") else None,
            provider=ParcelFacts.from_json(d["provider"]) if d.get("provider") else None,
            third=ParcelFacts.from_json(d["third"]) if d.get("third") else None,
            avm=_dec(d.get("avm")),
            listing=ListingInfo.from_json(d["listing"]) if d.get("listing") else None,
            comps=[SoldComp.from_json(c) for c in d.get("comps", [])],
        )


@dataclass(slots=True)
class MockPropertyDataProvider:
    """Fixture-backed, deterministic ``PropertyDataProvider``.

    ``today`` is fixed (``DEFAULT_TODAY``) so comp ages never drift between runs. Two test
    hooks: ``advance_clock(days)`` moves ``today`` (comps age out of a tier); ``inject_comp``
    adds a sale to a subject's pool, simulating a comp recorded between draft and send (V6).
    """

    name: str = "property_data"
    mode: IntegrationMode = "mock"
    today: date = DEFAULT_TODAY
    subjects: dict[str, SubjectFixture] = field(default_factory=dict)
    area_comps: dict[str, list[SoldComp]] = field(default_factory=dict)  # zip -> pool
    listings: list[ActiveListing] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    healthy: bool = True

    @classmethod
    def from_fixtures(cls, fixtures_dir: Path = FIXTURES_DIR) -> MockPropertyDataProvider:
        with (fixtures_dir / "properties.json").open("r", encoding="utf-8") as fh:
            props = json.load(fh)
        with (fixtures_dir / "listings.json").open("r", encoding="utf-8") as fh:
            listings_raw = json.load(fh)
        subjects = [SubjectFixture.from_json(s) for s in props.get("subjects", [])]
        area = {
            str(zip_code): [SoldComp.from_json(c) for c in comps]
            for zip_code, comps in (props.get("area_comps") or {}).items()
        }
        return cls(
            today=_date(props.get("as_of")) or DEFAULT_TODAY,
            subjects={s.key: s for s in subjects},
            area_comps=area,
            listings=[ActiveListing.from_json(d) for d in listings_raw],
        )

    # -- test hooks --------------------------------------------------------------------------

    def advance_clock(self, days: int) -> date:
        self.today = self.today + timedelta(days=days)
        return self.today

    def inject_comp(self, address: PropertyAddress, comp: SoldComp) -> None:
        subject = self.subjects.get(address.key)
        if subject is None:
            msg = f"no fixture subject for {address.display}"
            raise KeyError(msg)
        subject.comps.append(comp)

    def subject(self, address: PropertyAddress) -> SubjectFixture | None:
        return self.subjects.get(address.key)

    # -- PropertyDataProvider ----------------------------------------------------------------

    def _facts(self, address: PropertyAddress, which: str) -> ParcelFacts | None:
        self.calls.append({"method": f"{which}_facts", "address": address.display})
        subject = self.subjects.get(address.key)
        if subject is None:
            return None
        found: ParcelFacts | None = getattr(subject, which)
        return found

    async def county_facts(self, address: PropertyAddress) -> ParcelFacts | None:
        return self._facts(address, "county")

    async def provider_facts(self, address: PropertyAddress) -> ParcelFacts | None:
        return self._facts(address, "provider")

    async def third_source_facts(self, address: PropertyAddress) -> ParcelFacts | None:
        return self._facts(address, "third")

    def _pool(self, query: CompQuery) -> list[SoldComp]:
        if query.parcel_id is not None:
            for subject in self.subjects.values():
                ids = {f.parcel_id for f in (subject.county, subject.provider, subject.third) if f}
                if query.parcel_id in ids:
                    return list(subject.comps)
        return list(self.area_comps.get(query.postal_code, []))

    async def sold_comps(self, query: CompQuery) -> list[SoldComp]:
        self.calls.append({"method": "sold_comps", "query": query.to_json()})
        effective = replace(query, as_of=self.today) if query.as_of != self.today else query
        found = [c for c in self._pool(query) if effective.accepts(c)]
        found.sort(key=lambda c: (c.distance_miles, c.sale_date, c.comp_id))
        return found

    async def avm(self, parcel_id: str) -> Decimal | None:
        self.calls.append({"method": "avm", "parcel_id": parcel_id})
        for subject in self.subjects.values():
            for facts in (subject.county, subject.provider, subject.third):
                if facts is not None and facts.parcel_id == parcel_id:
                    return subject.avm
        return None

    async def listing_info(self, parcel_id: str) -> ListingInfo | None:
        self.calls.append({"method": "listing_info", "parcel_id": parcel_id})
        for subject in self.subjects.values():
            for facts in (subject.county, subject.provider, subject.third):
                if facts is not None and facts.parcel_id == parcel_id:
                    return subject.listing
        return None

    async def active_listings(self, search: ListingSearch) -> list[ActiveListing]:
        self.calls.append({"method": "active_listings", "search": _json_safe(asdict(search))})
        return [
            replace(listing, retrieved_at=self.today)
            for listing in self.listings
            if search.accepts(listing)
        ]

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=self.healthy,
            detail=f"{len(self.subjects)} fixture subjects, {len(self.listings)} listings",
        )


def mock_property_provider(fixtures_dir: Path = FIXTURES_DIR) -> MockPropertyDataProvider:
    return MockPropertyDataProvider.from_fixtures(fixtures_dir)


__all__ = [
    "DEFAULT_TODAY",
    "FACT_FIELDS",
    "FIXTURES_DIR",
    "SOURCE_COUNTY",
    "SOURCE_PROVIDER",
    "SOURCE_THIRD",
    "ActiveListing",
    "CompQuery",
    "ListingInfo",
    "ListingSearch",
    "MockPropertyDataProvider",
    "ParcelFacts",
    "PropertyAddress",
    "PropertyDataProvider",
    "SoldComp",
    "SubjectFixture",
    "address_key",
    "mock_property_provider",
]
