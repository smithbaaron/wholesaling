"""Generate the committed JSON fixtures behind the mock data providers.

Run with ``uv run python -m wholesaling.integrations.data.generate_fixtures`` and commit the
result. The output is deterministic (fixed seed, fixed anchor date), so a rerun that changes a
fixture means the generator changed, never the clock.

Houston is hand-designed so every A1 rule has a named scenario with an exact expected outcome
(see ``docs/agents/discovery.md`` for the table); the other 20 seed metros are drawn from a
seeded RNG for volume. Dates are relative to ``AS_OF`` so tests pass ``as_of=AS_OF``.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from wholesaling.config.tunables import get_tunables
from wholesaling.db.normalize import normalize_entity_name
from wholesaling.integrations.data.models import (
    ContactCandidate,
    DeedRecord,
    DistressSignals,
    MarketRef,
    MarketSignals,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SEED = 20261025
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
SOURCE = "mock_county_deeds"
RETRIEVED_AT = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------------------------
# Houston scenarios (every A1 rule, named)
# ---------------------------------------------------------------------------------------------

HOUSTON = MarketRef("Houston", "TX")
_POST_OAK = ("4400 Post Oak Pkwy Ste 200", "Houston", "TX", "77027")
_PO_BOX = ("PO Box 8821", "Houston", "TX", "77288")
_REG_AGENT = ("1999 Bryan St Ste 900", "Dallas", "TX", "75201")  # shared registered agent


def _months_ago(months: float) -> date:
    return (AS_OF - timedelta(days=round(months * 30.4375))).date()


class _Houston:
    def __init__(self) -> None:
        self.records: list[DeedRecord] = []
        self._n = 0

    def deed(
        self,
        buyer: str,
        months_ago: float,
        price: int,
        *,
        zip_code: str,
        financing: str = "cash",
        property_type: str = "sfr",
        units: int = 1,
        seller: str = "Estate of a prior owner",
        buyer_entity_type: str = "llc",
        mailing: tuple[str, str, str, str] | None = None,
        street: str | None = None,
        parcel_id: str | None = None,
    ) -> DeedRecord:
        self._n += 1
        ref = f"HC-2026-{self._n:05d}"
        rec = DeedRecord(
            parcel_id=parcel_id or f"HCAD-{self._n:07d}",
            address=street or f"{1000 + self._n * 7} Example St",
            city="Houston",
            state="TX",
            zip=zip_code,
            sale_date=_months_ago(months_ago),
            sale_price=Decimal(price),
            buyer_name=buyer,
            buyer_entity_type=buyer_entity_type,  # type: ignore[arg-type]
            seller_name=seller,
            financing=financing,  # type: ignore[arg-type]
            property_type=property_type,  # type: ignore[arg-type]
            units=units,
            source=SOURCE,
            source_ref=ref,
            source_url=f"https://mock.example/deeds/{ref}",
            retrieved_at=RETRIEVED_AT,
            buyer_mailing_address=mailing[0] if mailing else None,
            buyer_mailing_city=mailing[1] if mailing else None,
            buyer_mailing_state=mailing[2] if mailing else None,
            buyer_mailing_zip=mailing[3] if mailing else None,
        )
        self.records.append(rec)
        return rec

    def resale(self, purchase: DeedRecord, months_ago: float, price: int, buyer: str) -> DeedRecord:
        """The investor sells ``purchase`` on to a retail buyer with a mortgage."""
        return self.deed(
            buyer,
            months_ago,
            price,
            zip_code=purchase.zip,
            financing="conventional",
            seller=purchase.buyer_name,
            buyer_entity_type="individual",
            street=purchase.address,
            parcel_id=purchase.parcel_id,
        )


def houston_records() -> list[DeedRecord]:
    h = _Houston()

    # A. Flipper, three LLC spellings, 4 cash buys, 2 resales held 7-8 months (INV-03 Flipper).
    a1 = h.deed("Bayou Capital Partners LLC", 14, 185_000, zip_code="77009", mailing=_POST_OAK)
    a2 = h.deed("BAYOU CAPITAL PARTNERS, L.L.C.", 11, 142_000, zip_code="77022", mailing=_POST_OAK)
    h.deed(
        "Bayou Capital Partners Limited Liability Company",
        5,
        210_000,
        zip_code="77008",
        mailing=_POST_OAK,
    )
    h.deed("Bayou Capital Partners LLC", 1, 168_000, zip_code="77018", mailing=_POST_OAK)
    h.resale(a1, 7, 265_000, "Daniel Rao")
    h.resale(a2, 3, 219_000, "Priya Natarajan")

    # B. Buy-and-hold: two differently named LLCs at one PO box (INV-06 shared mailing address),
    #    5 cash buys clustered in ZIP 77021, no resales, 3 held > 12 months.
    for name, months, price in (
        ("Gulf Coast Rentals LLC", 22, 95_000),
        ("Gulf Coast Rentals LLC", 19, 110_000),
        ("Gulf Coast Rentals II LLC", 16, 120_000),
        ("Gulf Coast Rentals II LLC", 9, 135_000),
        ("Gulf Coast Rentals LLC", 4, 128_000),
    ):
        h.deed(name, months, price, zip_code="77021", mailing=_PO_BOX)

    # C. Repeat individual buyer: 2 cash buys in 24 months qualifies (INV-02).
    h.deed("Maria Hernandez", 15, 120_000, zip_code="77087", buyer_entity_type="individual")
    h.deed("Maria Hernandez", 6, 145_000, zip_code="77017", buyer_entity_type="individual")

    # D. Individual with one purchase: not qualified (INV-02).
    h.deed("John Carter", 3, 150_000, zip_code="77092", buyer_entity_type="individual")

    # E. iBuyer on the known-entity list: Exclude (INV-03, INV-05).
    for months, price, z in ((8, 230_000, "77084"), (4, 215_000, "77095"), (1, 240_000, "77449")):
        h.deed("Opendoor Property Trust I", months, price, zip_code=z, buyer_entity_type="trust")

    # F. Builder buying lots: Exclude (INV-05).
    for months, z in ((9, "77493"), (6, "77493"), (2, "77494")):
        h.deed(
            "Lennar Homes of Texas LLC", months, 60_000, zip_code=z, property_type="land", units=0
        )

    # G. Wholesaler / competitor: cash buys resold within a month (INV-03 Deprioritize).
    g1 = h.deed("Fast Close Houston LLC", 10, 95_000, zip_code="77026", mailing=_REG_AGENT)
    g2 = h.deed("Fast Close Houston LLC", 6, 130_000, zip_code="77028", mailing=_REG_AGENT)
    h.deed("Fast Close Houston LLC", 2, 140_000, zip_code="77016", mailing=_REG_AGENT)
    h.resale(g1, 9, 118_000, "Kevin Oduya")
    h.resale(g2, 5, 152_000, "Lauren Whitfield")

    # H. Conventional-mortgage buyer: no cash purchase, not qualified (INV-02).
    for months, price, z in ((7, 190_000, "77040"), (4, 205_000, "77041"), (1, 199_000, "77043")):
        h.deed("Westside Living LLC", months, price, zip_code=z, financing="conventional")

    # I. Cash buys outside the deal band: not qualified (INV-02, M1-06).
    h.deed("Luxury Estates Group LLC", 5, 900_000, zip_code="77024")
    h.deed("Luxury Estates Group LLC", 2, 1_200_000, zip_code="77019")

    # J. Stale: last purchase > 18 months ago, not qualified (INV-02).
    for months, price, z in (
        (23, 140_000, "77033"),
        (21, 125_000, "77033"),
        (20, 150_000, "77051"),
    ):
        h.deed("Old Money Investments LLC", months, price, zip_code=z)

    # K. Government: Exclude (INV-05).
    h.deed("Harris County", 3, 180_000, zip_code="77002", buyer_entity_type="government")

    # L. Nonprofit: Exclude (INV-05).
    h.deed(
        "Habitat for Humanity Houston Inc", 6, 110_000, zip_code="77026", buyer_entity_type="corp"
    )
    h.deed(
        "Habitat for Humanity Houston Inc", 2, 105_000, zip_code="77026", buyer_entity_type="corp"
    )

    # M. Lender taking REO at a trustee sale: Exclude (INV-05).
    h.deed(
        "Wells Fargo Bank NA",
        4,
        160_000,
        zip_code="77045",
        financing="unknown",
        seller="Substitute Trustee",
        buyer_entity_type="corp",
    )

    # N. On the suppression list: Exclude (INV-05).
    h.deed("Suppressed Holdings LLC", 3, 170_000, zip_code="77055")
    h.deed("Suppressed Holdings LLC", 1, 160_000, zip_code="77055")

    # O. Hard-money flipper: qualifies; one flip held 7 months (INV-02 hard money, INV-03).
    o1 = h.deed("Lone Star Flips LLC", 13, 160_000, zip_code="77012", financing="hard_money")
    h.deed("Lone Star Flips LLC", 3, 175_000, zip_code="77023", financing="hard_money")
    h.resale(o1, 6, 240_000, "Thomas Beaumont")

    # P. Agent-investor (licence match): Flag, include (INV-03).
    h.deed("Keller Realty Investments LLC", 8, 130_000, zip_code="77076")
    h.deed("Keller Realty Investments LLC", 2, 155_000, zip_code="77037")

    # Q. Mixed band fit: 2 of 3 buys inside the band; same registered agent as G (must NOT
    #    merge with G: different name, shared address alone is not enough).
    h.deed("Heights Holdings LLC", 9, 200_000, zip_code="77008", mailing=_REG_AGENT)
    h.deed("Heights Holdings LLC", 5, 250_000, zip_code="77007", mailing=_REG_AGENT)
    h.deed("Heights Holdings LLC", 2, 480_000, zip_code="77007", mailing=_REG_AGENT)

    return h.records


def houston_signals() -> MarketSignals:
    return MarketSignals(
        market=HOUSTON,
        data_available=True,
        entity_purchase_share=0.27,
        cash_share=0.38,
        repeat_buyer_count=160,
        i2i_resale_share=0.09,
        cash_vs_retail_spread=0.24,
        flip_profit_median=Decimal("52000"),
        distress=DistressSignals(
            pre_foreclosure=6.5, tax_delinquent=9.0, probate=3.2, absentee=14.0, vacancy=5.5
        ),
        days_on_market=38,
        months_inventory=3.1,
        sold_trend=0.04,
        share_in_band=0.58,
        median_price=Decimal("262000"),
        source="mock_market_signals",
        retrieved_at=RETRIEVED_AT,
    )


def houston_contacts() -> dict[str, list[ContactCandidate]]:
    def c(
        channel: str,
        value: str,
        source: str,
        provenance: str,
        confidence: float,
        limits: str | None = "Business contact; email outreach only, no SMS or calls",
        url: str | None = None,
    ) -> ContactCandidate:
        return ContactCandidate(
            channel=channel,  # type: ignore[arg-type]
            value=value,
            source=source,
            provenance=provenance,
            confidence=confidence,
            use_limitations=limits,
            provenance_url=url,
        )

    return {
        normalize_entity_name("Bayou Capital Partners LLC"): [
            c(
                "email",
                "deals@bayoucapitalpartners.com",
                "company_website",
                "Contact page of bayoucapitalpartners.com",
                0.9,
                url="https://bayoucapitalpartners.example/contact",
            ),
            # Same address, different casing and source: must dedupe onto one row.
            c("email", "Deals@BayouCapitalPartners.com", "tx_sos_filing", "TX SOS filing", 0.7),
            c("email", "info@bayoucapitalpartners.com", "company_website", "Footer", 0.6),
            c("email", "deals@bayou", "web_scrape", "Broken mailto", 0.2),  # junk, dropped
            c("phone", "(713) 555-0142", "company_website", "Contact page", 0.8),
            c("phone", "713-555-0142", "tx_sos_filing", "TX SOS filing", 0.6),  # dup phone
            c("address", "4400 Post Oak Pkwy Ste 200, Houston, TX 77027", "deed", "Deed", 0.9),
        ],
        normalize_entity_name("Gulf Coast Rentals LLC"): [
            c("phone", "832-555-0190", "tx_sos_filing", "TX SOS filing", 0.6),
            c("address", "PO Box 8821, Houston, TX 77288", "deed", "Deed mailing address", 0.9),
        ],
        normalize_entity_name("Maria Hernandez"): [
            c("email", "maria.hernandez.htx@gmail.com", "licensed_enrichment", "Vendor", 0.55),
            c("email", "mariahernandezhtx@gmail.com", "web_scrape", "Forum", 0.3),  # Gmail dup
        ],
        normalize_entity_name("Fast Close Houston LLC"): [
            c("email", "offers@fastclosehouston.com", "company_website", "Contact page", 0.85),
        ],
        normalize_entity_name("Lone Star Flips LLC"): [
            c("email", "acquisitions@lonestarflips.com", "company_website", "Contact page", 0.85),
            c("phone", "281-555-0101", "company_website", "Contact page", 0.8),
        ],
        normalize_entity_name("Keller Realty Investments LLC"): [
            c("email", "invest@kellerrealtyinv.com", "trec_license", "TREC licence record", 0.8),
        ],
        normalize_entity_name("Heights Holdings LLC"): [
            c("email", "hello@heightsholdings.com", "company_website", "Contact page", 0.75),
        ],
    }


def houston_reference_lists() -> dict[str, list[str]]:
    return {
        "institutional": sorted(
            normalize_entity_name(n)
            for n in (
                "Opendoor Property Trust I",
                "Offerpad SPE Borrower A LLC",
                "Zillow Homes Property Trust",
                "Invitation Homes",
                "American Homes 4 Rent",
                "Progress Residential",
                "FirstKey Homes",
            )
        ),
        "licensed_agents": sorted(
            normalize_entity_name(n) for n in ("Keller Realty Investments LLC",)
        ),
        "suppressed": sorted(normalize_entity_name(n) for n in ("Suppressed Holdings LLC",)),
    }


# ---------------------------------------------------------------------------------------------
# Other seed metros (seeded RNG)
# ---------------------------------------------------------------------------------------------

_WORDS_A = ["Red", "Blue", "Oak", "Pine", "River", "Summit", "Harbor", "Prairie", "Cedar", "Iron"]
_WORDS_B = ["Capital", "Holdings", "Property", "Equity", "Ventures", "Homes", "Realty", "Assets"]
_SUFFIX = ["LLC", "LLC", "LLC", "LP", "Trust", "Inc"]
_FIRST = ["James", "Linda", "Robert", "Patricia", "Michael", "Barbara", "David", "Susan"]
_LAST = ["Nguyen", "Walker", "Patel", "Brown", "Garcia", "Kim", "Robinson", "Lopez"]

_ZIP_PREFIX = {
    "Dallas-Fort Worth": "752",
    "San Antonio": "782",
    "Austin": "787",
    "Atlanta": "303",
    "Phoenix": "850",
    "Tampa": "336",
    "Orlando": "328",
    "Jacksonville": "322",
    "Charlotte": "282",
    "Nashville": "372",
    "Memphis": "381",
    "Birmingham": "352",
    "Oklahoma City": "731",
    "Kansas City": "641",
    "St. Louis": "631",
    "Indianapolis": "462",
    "Columbus": "432",
    "Cleveland": "441",
    "Cincinnati": "452",
    "Detroit": "482",
}
_MEDIAN = {
    "Dallas-Fort Worth": 340_000,
    "San Antonio": 265_000,
    "Austin": 430_000,
    "Atlanta": 350_000,
    "Phoenix": 420_000,
    "Tampa": 360_000,
    "Orlando": 365_000,
    "Jacksonville": 300_000,
    "Charlotte": 370_000,
    "Nashville": 410_000,
    "Memphis": 190_000,
    "Birmingham": 205_000,
    "Oklahoma City": 215_000,
    "Kansas City": 250_000,
    "St. Louis": 220_000,
    "Indianapolis": 240_000,
    "Columbus": 275_000,
    "Cleveland": 160_000,
    "Cincinnati": 235_000,
    "Detroit": 120_000,
}
# Birmingham stands in for "provider has no coverage here" so the M1 data gate is exercised.
_NO_DATA = {"Birmingham"}


def _metro_records(market: MarketRef, rng: random.Random) -> list[DeedRecord]:
    records: list[DeedRecord] = []
    prefix = _ZIP_PREFIX[market.name]
    median = _MEDIAN[market.name]
    n_buyers = rng.randint(7, 10)
    counter = 0
    for b in range(n_buyers):
        is_entity = rng.random() < 0.65
        if is_entity:
            name = f"{rng.choice(_WORDS_A)} {rng.choice(_WORDS_B)} {rng.choice(_SUFFIX)}"
            etype = name.split()[-1].lower()
            if etype not in {"llc", "lp", "trust"}:
                etype = "corp"
        else:
            name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
            etype = "individual"
        name = f"{name} {b}" if is_entity else name  # keep generated entity names distinct
        n_purchases = rng.choice([1, 1, 2, 2, 3, 4]) if is_entity else rng.choice([1, 1, 2])
        mailing = (
            (
                f"{rng.randint(100, 9999)} Main St",
                market.name,
                market.state,
                f"{prefix}{rng.randint(10, 99)}",
            )
            if is_entity
            else None
        )
        for _ in range(n_purchases):
            counter += 1
            months = rng.uniform(0.5, 23.5)
            price = int(median * rng.uniform(0.35, 1.1) / 1000) * 1000
            financing = rng.choices(
                ["cash", "hard_money", "conventional", "unknown"], weights=[55, 15, 25, 5]
            )[0]
            ref = f"{market.state}-{market.name[:3].upper()}-{counter:04d}"
            records.append(
                DeedRecord(
                    parcel_id=f"{market.state}{prefix}-{counter:06d}",
                    address=f"{rng.randint(100, 9999)} {rng.choice(_WORDS_A)} St",
                    city=market.name,
                    state=market.state,
                    zip=f"{prefix}{rng.randint(10, 99)}",
                    sale_date=_months_ago(months),
                    sale_price=Decimal(price),
                    buyer_name=name,
                    buyer_entity_type=etype,  # type: ignore[arg-type]
                    seller_name=f"{rng.choice(_FIRST)} {rng.choice(_LAST)}",
                    financing=financing,  # type: ignore[arg-type]
                    property_type=rng.choices(["sfr", "duplex", "land"], weights=[85, 10, 5])[0],  # type: ignore[arg-type]
                    units=1,
                    source=SOURCE,
                    source_ref=ref,
                    source_url=f"https://mock.example/deeds/{ref}",
                    retrieved_at=RETRIEVED_AT,
                    buyer_mailing_address=mailing[0] if mailing else None,
                    buyer_mailing_city=mailing[1] if mailing else None,
                    buyer_mailing_state=mailing[2] if mailing else None,
                    buyer_mailing_zip=mailing[3] if mailing else None,
                )
            )
    return records


def _metro_signals(market: MarketRef, rng: random.Random) -> MarketSignals:
    if market.name in _NO_DATA:
        return MarketSignals(
            market=market,
            data_available=False,
            source="mock_market_signals",
            retrieved_at=RETRIEVED_AT,
        )
    median = _MEDIAN[market.name]
    return MarketSignals(
        market=market,
        data_available=True,
        entity_purchase_share=round(rng.uniform(0.10, 0.30), 3),
        cash_share=round(rng.uniform(0.20, 0.45), 3),
        repeat_buyer_count=rng.randint(20, 180),
        i2i_resale_share=round(rng.uniform(0.02, 0.12), 3),
        cash_vs_retail_spread=round(rng.uniform(0.08, 0.30), 3),
        flip_profit_median=Decimal(rng.randint(20, 70) * 1000) if rng.random() < 0.7 else None,
        distress=DistressSignals(
            pre_foreclosure=round(rng.uniform(1.0, 9.0), 1),
            tax_delinquent=round(rng.uniform(2.0, 14.0), 1),
            probate=round(rng.uniform(0.5, 4.0), 1),
            absentee=round(rng.uniform(5.0, 20.0), 1),
            vacancy=round(rng.uniform(1.0, 9.0), 1),
        ),
        days_on_market=rng.randint(25, 80),
        months_inventory=round(rng.uniform(1.5, 6.5), 1),
        sold_trend=round(rng.uniform(-0.15, 0.12), 3),
        share_in_band=round(max(0.05, min(0.9, 1.1 - median / 400_000)), 3),
        median_price=Decimal(median),
        source="mock_market_signals",
        retrieved_at=RETRIEVED_AT,
    )


def _metro_contacts(
    records: list[DeedRecord], rng: random.Random
) -> dict[str, list[ContactCandidate]]:
    out: dict[str, list[ContactCandidate]] = {}
    for rec in records:
        if rec.buyer_entity_type == "individual":
            continue
        key = normalize_entity_name(rec.buyer_name)
        if key in out or rng.random() < 0.25:  # a quarter of entities have no findable email
            continue
        slug = "".join(ch for ch in rec.buyer_name.lower() if ch.isalnum())[:24]
        out[key] = [
            ContactCandidate(
                channel="email",
                value=f"contact@{slug}.example",
                source="company_website",
                provenance="Contact page",
                confidence=round(rng.uniform(0.5, 0.9), 2),
                use_limitations="Business contact; email outreach only, no SMS or calls",
                provenance_url=f"https://{slug}.example/contact",
            )
        ]
    return out


# ---------------------------------------------------------------------------------------------


def build() -> dict[str, Any]:
    tunables = get_tunables()
    markets = [MarketRef.parse(label) for label in tunables.m1.seed_markets]
    deeds: list[DeedRecord] = houston_records()
    signals: list[MarketSignals] = [houston_signals()]
    contacts: dict[str, list[ContactCandidate]] = houston_contacts()
    for market in markets:
        if market == HOUSTON:
            continue
        rng = random.Random(f"{SEED}:{market.label}")
        recs = _metro_records(market, rng)
        deeds.extend(recs)
        signals.append(_metro_signals(market, rng))
        contacts.update(_metro_contacts(recs, rng))
    return {
        "deeds": [d.to_json() for d in deeds],
        "signals": [s.to_json() for s in signals],
        "contacts": {k: [c.to_json() for c in v] for k, v in sorted(contacts.items())},
        "reference_lists": houston_reference_lists(),
    }


def write(out_dir: Path = FIXTURES_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build()
    written: list[Path] = []
    for name, payload in data.items():
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for p in write():
        print(p)  # noqa: T201 - CLI output
