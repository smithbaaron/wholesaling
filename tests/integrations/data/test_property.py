"""Fixture-backed property data provider (A7-02/03/12/13/15, BB-12): records, filters, hooks."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.base import HealthStatus
from wholesaling.integrations.data import get_property_data_provider
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    ActiveListing,
    CompQuery,
    ListingInfo,
    ListingSearch,
    MockPropertyDataProvider,
    ParcelFacts,
    PropertyAddress,
    PropertyDataProvider,
    SoldComp,
    address_key,
    mock_property_provider,
)

CLEAN = PropertyAddress("1204 Oak Hollow Dr", "Houston", "TX", "77008")


@pytest.fixture
def provider() -> MockPropertyDataProvider:
    return mock_property_provider()


def test_mock_satisfies_the_protocol_and_loads_fixtures(provider: MockPropertyDataProvider) -> None:
    assert isinstance(provider, PropertyDataProvider)
    assert provider.today == DEFAULT_TODAY == date(2026, 10, 25)
    assert len(provider.subjects) == 7 and len(provider.listings) == 10
    assert set(provider.area_comps) == {"77008", "77018", "77007", "77055"}


async def test_healthcheck(provider: MockPropertyDataProvider) -> None:
    status = await provider.healthcheck()
    assert status == HealthStatus(
        name="property_data", mode="mock", ok=True, detail="7 fixture subjects, 10 listings"
    )


def test_factory_follows_data_provider_mode(settings: Settings) -> None:
    assert isinstance(get_property_data_provider(settings), MockPropertyDataProvider)
    live = settings.model_copy(update={"data_provider_mode": "live"})
    with pytest.raises(NotImplementedError, match="INT-03"):
        get_property_data_provider(live)


def test_address_key_is_the_normalised_dedupe_key() -> None:
    assert CLEAN.key == address_key("1204 Oak Hollow Drive", "houston", "tx", "77008")
    assert CLEAN.display == "1204 Oak Hollow Dr, Houston, TX 77008"
    assert PropertyAddress("1 Main St").display == "1 Main St"


async def test_facts_from_three_sources(provider: MockPropertyDataProvider) -> None:
    county = await provider.county_facts(CLEAN)
    prov = await provider.provider_facts(CLEAN)
    third = await provider.third_source_facts(CLEAN)
    assert county is not None and prov is not None and third is not None
    assert county.source == "county" and prov.source == "provider" and third.source == "third"
    assert county.sqft == prov.sqft == 1600 and third.lot_sqft == 7150
    assert county.last_sale_date == date(2015, 6, 12) and county.last_sale_price == Decimal(165000)
    assert (
        await provider.county_facts(PropertyAddress("9 Nowhere", "Houston", "TX", "77001")) is None
    )
    assert [c["method"] for c in provider.calls] == [
        "county_facts",
        "provider_facts",
        "third_facts",
        "county_facts",
    ]


def test_records_round_trip_through_json(provider: MockPropertyDataProvider) -> None:
    subject = provider.subject(CLEAN)
    assert subject is not None and subject.county is not None and subject.listing is not None
    assert ParcelFacts.from_json(subject.county.to_json()) == subject.county
    assert ListingInfo.from_json(subject.listing.to_json()) == subject.listing
    comp = subject.comps[0]
    assert SoldComp.from_json(comp.to_json()) == comp
    assert comp.price_per_sqft == Decimal("150.0000")
    listing = provider.listings[0]
    assert ActiveListing.from_json(listing.to_json()) == listing


async def test_sold_comps_filters_and_orders(provider: MockPropertyDataProvider) -> None:
    query = CompQuery("77008", 1600, 1985, Decimal("0.75"), 180, DEFAULT_TODAY, parcel_id="HC-0001")
    comps = await provider.sold_comps(query)
    assert [c.comp_id for c in comps] == ["CL7", "CL3", "CL1", "CL5", "CL2", "CL6", "CL4"]
    narrow = await provider.sold_comps(
        CompQuery("77008", 1600, 1985, Decimal("0.35"), 60, DEFAULT_TODAY, parcel_id="HC-0001")
    )
    assert [c.comp_id for c in narrow] == ["CL7", "CL3", "CL1"]


async def test_sold_comps_fall_back_to_the_zip_pool_for_unknown_parcels(
    provider: MockPropertyDataProvider,
) -> None:
    comps = await provider.sold_comps(
        CompQuery("77008", 1550, 1985, Decimal("0.75"), 180, DEFAULT_TODAY, parcel_id="L-999")
    )
    assert comps and all(c.comp_id.startswith("A8-") for c in comps)
    assert (
        await provider.sold_comps(
            CompQuery("00000", 1550, 1985, Decimal("0.75"), 180, DEFAULT_TODAY)
        )
        == []
    )


async def test_avm_and_listing_info_by_parcel(provider: MockPropertyDataProvider) -> None:
    assert await provider.avm("HC-0001") == Decimal(245000)
    assert await provider.avm("nope") is None
    info = await provider.listing_info("HC-0005")
    assert info is not None and "Foundation" in (info.remarks or "") and len(info.photo_urls) == 3
    assert await provider.listing_info("nope") is None


async def test_inject_comp_and_advance_clock(provider: MockPropertyDataProvider) -> None:
    new = SoldComp(
        "NEW",
        "P-NEW",
        "1 New",
        "77008",
        Decimal(320000),
        DEFAULT_TODAY,
        1600,
        Decimal("0.1"),
        year_built=1985,
    )
    provider.inject_comp(CLEAN, new)
    comps = await provider.sold_comps(
        CompQuery("77008", 1600, 1985, Decimal("0.75"), 180, DEFAULT_TODAY, parcel_id="HC-0001")
    )
    assert [c.comp_id for c in comps][:2] == ["CL7", "NEW"] and len(
        comps
    ) == 8  # 0.1 mi ties: older first
    assert provider.advance_clock(400) == DEFAULT_TODAY + timedelta(days=400)
    assert (
        await provider.sold_comps(
            CompQuery("77008", 1600, 1985, Decimal("0.75"), 180, DEFAULT_TODAY, parcel_id="HC-0001")
        )
        == []
    )
    with pytest.raises(KeyError):
        provider.inject_comp(PropertyAddress("9 Nowhere", "Houston", "TX", "77001"), new)


async def test_active_listings_search(provider: MockPropertyDataProvider) -> None:
    by_zip = await provider.active_listings(ListingSearch(postal_codes=("77008",)))
    assert sorted(li.listing_id for li in by_zip) == [
        "L-001",
        "L-002",
        "L-009",
    ]  # pending L-010 excluded
    assert all(li.retrieved_at == DEFAULT_TODAY for li in by_zip)
    by_city = await provider.active_listings(ListingSearch(cities=("houston",), state="TX"))
    assert len(by_city) == 8 and all(li.address.city == "Houston" for li in by_city)
    by_market = await provider.active_listings(ListingSearch(markets=("Dallas, TX",)))
    assert [li.listing_id for li in by_market] == ["L-008"]
    everything = await provider.active_listings(ListingSearch())
    assert len(everything) == 9
    wrong_state = await provider.active_listings(ListingSearch(cities=("Houston",), state="OK"))
    assert wrong_state == []


def test_fixture_scenarios_cover_the_spec_list(provider: MockPropertyDataProvider) -> None:
    """The fixture file must keep: clean >=5 tier-1 comps; a tier-2 subject; a >10% sqft gap; the
    four dirty comp kinds; an AVM diverging >10%; a drift subject (comps injected by tests)."""
    clean = provider.subject(CLEAN)
    assert clean is not None and len(clean.comps) >= 5
    gap = provider.subject(PropertyAddress("4510 Maple Ct", "Houston", "TX", "77092"))
    assert gap is not None and gap.county and gap.provider and gap.third
    assert abs(gap.county.sqft - gap.provider.sqft) / gap.provider.sqft > 0.10  # type: ignore[operator]
    dirty = provider.subject(PropertyAddress("930 Cedar Bend", "Houston", "TX", "77055"))
    assert dirty is not None and {c.comp_id for c in dirty.comps} >= {
        "DC-NAL",
        "DC-NEW",
        "DC-FLIP",
        "DC-LOT",
    }
    avm = provider.subject(PropertyAddress("2201 Birch St", "Houston", "TX", "77007"))
    assert avm is not None and avm.avm == Decimal(262500)
