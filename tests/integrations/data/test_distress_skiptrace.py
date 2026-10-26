"""Distress-list, skip-trace and DNC mocks (REQUIREMENTS.md INT-04, INT-05, SEL-02, SEL-03)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.data import get_data_providers
from wholesaling.integrations.data.distress import (
    DISTRESS_SIGNALS,
    DistressListProvider,
    DistressRecord,
    MockDistressListProvider,
)
from wholesaling.integrations.data.interface import DataProviders, DeedRecordsProvider
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.data.models import MarketRef
from wholesaling.integrations.data.skiptrace import (
    DncListProvider,
    MockDncListProvider,
    MockSkipTraceProvider,
    SkipTraceProvider,
)

HOUSTON = MarketRef("Houston", "TX")


@pytest.fixture
def providers() -> DataProviders:
    return mock_data_providers()


def test_bundle_carries_the_seller_side_providers(providers: DataProviders) -> None:
    assert isinstance(providers.distress, DistressListProvider)
    assert isinstance(providers.skiptrace, SkipTraceProvider)
    assert isinstance(providers.dnc, DncListProvider)
    assert isinstance(providers.deeds, DeedRecordsProvider)


def test_factory_returns_mocks_and_refuses_live_modes() -> None:
    s = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    assert get_data_providers(s).distress is not None
    with pytest.raises(NotImplementedError, match="INT-04"):
        get_data_providers(Settings(_env_file=None, app_env="test", distress_provider_mode="live"))  # type: ignore[call-arg]
    with pytest.raises(NotImplementedError, match="INT-05"):
        get_data_providers(Settings(_env_file=None, app_env="test", skiptrace_provider_mode="live"))  # type: ignore[call-arg]


async def test_distress_fixture_is_market_scoped_and_records_calls(
    providers: DataProviders,
) -> None:
    assert isinstance(providers.distress, MockDistressListProvider)
    rows = await providers.distress.distress_records(HOUSTON)
    assert rows and all(r.city == "Houston" and r.state == "TX" for r in rows)
    assert providers.distress.calls == [{"method": "distress_records", "market": "Houston, TX"}]
    columbus = await providers.distress.distress_records(MarketRef("Columbus", "OH"))
    assert [r.parcel_id for r in columbus] == ["FRANK-D-0000011"]
    assert await providers.distress.distress_records(MarketRef("Nowhere", "ZZ")) == []


async def test_distress_fixture_has_one_duplicate_parcel_and_only_known_signals(
    providers: DataProviders,
) -> None:
    assert isinstance(providers.distress, MockDistressListProvider)
    rows = await providers.distress.distress_records(HOUSTON)
    ids = [r.parcel_id for r in rows]
    assert ids.count("HCAD-D-0000001") == 2  # two lists, one parcel: S1 merges them
    for r in rows:
        assert set(r.signals) <= set(DISTRESS_SIGNALS)
        assert r.signals, r.parcel_id


def test_distress_record_json_round_trip(providers: DataProviders) -> None:
    assert isinstance(providers.distress, MockDistressListProvider)
    rec = providers.distress.records[0]
    again = DistressRecord.from_json(rec.to_json())
    assert again == rec
    assert isinstance(again.assessed_value, Decimal)


def test_distress_record_rejects_unknown_signal(providers: DataProviders) -> None:
    assert isinstance(providers.distress, MockDistressListProvider)
    doc = providers.distress.records[0].to_json()
    doc["signals"] = ["absentee", "haunted"]
    with pytest.raises(ValueError, match="haunted"):
        DistressRecord.from_json(doc)


async def test_skip_trace_is_keyed_on_the_normalised_owner_name(providers: DataProviders) -> None:
    assert isinstance(providers.skiptrace, MockSkipTraceProvider)
    result = await providers.skiptrace.trace("DOLORES RAMIREZ", "1420 Pecan Grove Dr, Houston, TX")
    assert result.found_any and result.vendor == "skip_trace"
    assert {c.channel for c in result.contacts} == {"email", "phone", "address"}
    assert all(c.provenance and 0 < c.confidence <= 1 for c in result.contacts)
    empty = await providers.skiptrace.trace("Nobody Known", "1 Nowhere St")
    assert not empty.found_any and empty.vendor_ref is None
    assert providers.skiptrace.calls[0]["method"] == "trace"
    assert result.to_json()["contacts"][0]["channel"] == "email"


async def test_dnc_list_normalises_phones(providers: DataProviders) -> None:
    assert isinstance(providers.dnc, MockDncListProvider)
    assert await providers.dnc.is_listed("(713) 555-0199") is True
    assert await providers.dnc.is_listed("+17135550199") is True
    assert await providers.dnc.is_listed("713-555-0101") is False
    assert await providers.dnc.is_listed("not a phone") is False


async def test_resolve_parcel_matches_distress_and_deed_addresses(providers: DataProviders) -> None:
    parcel = await providers.deeds.resolve_parcel("1420 Pecan Grove Drive, Houston, TX 77009")
    assert parcel is not None and parcel.parcel_id == "HCAD-D-0000001"
    deed = await providers.deeds.resolve_parcel("1007 Example Street")
    assert deed is not None and deed.parcel_id == "HCAD-0000001"
    assert await providers.deeds.resolve_parcel("999 Nowhere Blvd") is None
    assert await providers.deeds.resolve_parcel("") is None


async def test_seller_provider_healthchecks(providers: DataProviders) -> None:
    assert providers.distress is not None and providers.skiptrace is not None
    assert providers.dnc is not None
    for p in (providers.distress, providers.skiptrace, providers.dnc):
        status = await p.healthcheck()
        assert status.ok and status.mode == "mock"
