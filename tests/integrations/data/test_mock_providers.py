"""Mock data providers: fixtures are committed and deterministic; factory honours the mode."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.data import get_data_providers
from wholesaling.integrations.data.generate_fixtures import AS_OF, build
from wholesaling.integrations.data.interface import (
    ContactEnrichmentProvider,
    DeedRecordsProvider,
    MarketSignalsProvider,
)
from wholesaling.integrations.data.mock import FIXTURES_DIR, mock_data_providers
from wholesaling.integrations.data.models import (
    ContactCandidate,
    DeedRecord,
    MarketRef,
    MarketSignals,
)

HOUSTON = MarketRef("Houston", "TX")
SINCE = (AS_OF - timedelta(days=730)).date()
UNTIL = AS_OF.date()


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_committed_fixtures_match_the_generator() -> None:
    """Regenerating must be a no-op; a diff here means the generator changed without a rerun."""
    generated = build()
    for name, payload in generated.items():
        on_disk = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert on_disk == payload, f"fixtures/{name}.json is stale; rerun the generator"


def test_generator_is_deterministic() -> None:
    assert build() == build()


def test_fixtures_cover_houston_and_the_twenty_seed_metros() -> None:
    from wholesaling.config.tunables import get_tunables

    providers = mock_data_providers()
    seeds = {MarketRef.parse(s) for s in get_tunables().m1.seed_markets}
    assert len(seeds) == 21
    assert set(providers.signals.by_market) == seeds  # type: ignore[attr-defined]
    deeds_markets = {MarketRef(r.city, r.state) for r in providers.deeds.records}  # type: ignore[attr-defined]
    assert deeds_markets == seeds


@pytest.mark.asyncio
async def test_deeds_filter_by_market_and_window() -> None:
    providers = mock_data_providers()
    houston = await providers.deeds.purchases(HOUSTON, SINCE, UNTIL)
    assert len(houston) == 47
    assert all(r.city == "Houston" and r.state == "TX" for r in houston)
    assert all(SINCE <= r.sale_date <= UNTIL for r in houston)
    recent = await providers.deeds.purchases(HOUSTON, UNTIL - timedelta(days=45), UNTIL)
    assert 0 < len(recent) < len(houston)
    none = await providers.deeds.purchases(MarketRef("Nowhere", "ZZ"), SINCE, UNTIL)
    assert none == []
    assert providers.deeds.calls[-1]["market"] == "Nowhere, ZZ"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_houston_fixture_contains_every_named_scenario() -> None:
    providers = mock_data_providers()
    deeds = await providers.deeds.purchases(HOUSTON, SINCE, UNTIL)
    buyers = {r.buyer_name for r in deeds}
    assert {
        "Bayou Capital Partners LLC",
        "BAYOU CAPITAL PARTNERS, L.L.C.",
        "Bayou Capital Partners Limited Liability Company",
    } <= buyers  # LLC variants
    assert {
        "Gulf Coast Rentals LLC",
        "Gulf Coast Rentals II LLC",
    } <= buyers  # shared mailing address
    assert sum(1 for r in deeds if r.buyer_name == "Maria Hernandez") == 2  # repeat individual
    assert "Opendoor Property Trust I" in buyers  # iBuyer on the exclusion list
    assert all(r.property_type == "land" for r in deeds if r.buyer_name.startswith("Lennar"))
    fast = [r for r in deeds if r.seller_name == "Fast Close Houston LLC"]  # competitor resales
    assert len(fast) == 2
    assert all(
        r.financing == "conventional" for r in deeds if r.buyer_name == "Westside Living LLC"
    )
    assert all(r.sale_price > 350_000 for r in deeds if r.buyer_name == "Luxury Estates Group LLC")
    stale = [r for r in deeds if r.buyer_name == "Old Money Investments LLC"]
    assert all((UNTIL - r.sale_date).days > 18 * 30 for r in stale)


@pytest.mark.asyncio
async def test_signals_for_every_market_and_data_gate_case() -> None:
    providers = mock_data_providers()
    houston = await providers.signals.signals(HOUSTON)
    assert houston.data_available and houston.median_price is not None
    birmingham = await providers.signals.signals(MarketRef("Birmingham", "AL"))
    assert birmingham.data_available is False
    unknown = await providers.signals.signals(MarketRef("Nowhere", "ZZ"))
    assert unknown.data_available is False and unknown.market.state == "ZZ"


@pytest.mark.asyncio
async def test_contact_lookup_is_keyed_by_normalised_entity_name() -> None:
    providers = mock_data_providers()
    a = await providers.contacts.lookup("Bayou Capital Partners LLC", None)
    b = await providers.contacts.lookup("BAYOU CAPITAL PARTNERS, L.L.C.", "4400 Post Oak Pkwy")
    assert a == b and len(a) == 7
    assert await providers.contacts.lookup("Nobody LLC", None) == []


def test_reference_lists_are_normalised_keys() -> None:
    lists = mock_data_providers().reference_lists
    assert "OPENDOOR PROPERTY TRUST I" in lists.institutional
    assert lists.suppressed == {"SUPPRESSED HOLDINGS LLC"}
    assert lists.licensed_agents == {"KELLER REALTY INVESTMENTS LLC"}
    assert all(k == k.upper() for k in lists.institutional)


@pytest.mark.asyncio
async def test_healthchecks_report_mock_mode() -> None:
    statuses = await mock_data_providers().healthcheck()
    assert [s.name for s in statuses] == ["deed_records", "market_signals", "contact_enrichment"]
    assert all(s.ok and s.mode == "mock" for s in statuses)


def test_providers_satisfy_the_protocols() -> None:
    p = mock_data_providers()
    assert isinstance(p.deeds, DeedRecordsProvider)
    assert isinstance(p.signals, MarketSignalsProvider)
    assert isinstance(p.contacts, ContactEnrichmentProvider)


def test_factory_defaults_to_mock_and_refuses_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_PROVIDER_MODE", raising=False)
    assert _settings().data_provider_mode == "mock"
    assert get_data_providers(_settings()).deeds.mode == "mock"
    with pytest.raises(NotImplementedError, match="INT-03"):
        get_data_providers(_settings(data_provider_mode="live"))


def test_models_round_trip_through_json() -> None:
    providers = mock_data_providers()
    deed: DeedRecord = providers.deeds.records[0]  # type: ignore[attr-defined]
    assert DeedRecord.from_json(json.loads(json.dumps(deed.to_json()))) == deed
    sig: MarketSignals = next(iter(providers.signals.by_market.values()))  # type: ignore[attr-defined]
    assert MarketSignals.from_json(json.loads(json.dumps(sig.to_json()))) == sig
    cand = ContactCandidate("email", "a@b.co", "s", "p", 0.5, "limits", "https://x")
    assert ContactCandidate.from_json(cand.to_json()) == cand


def test_market_ref_parse() -> None:
    assert MarketRef.parse("Dallas-Fort Worth, TX") == MarketRef("Dallas-Fort Worth", "TX")
    assert MarketRef.parse(" St. Louis , mo ").label == "St. Louis, MO"
    with pytest.raises(ValueError, match="City, ST"):
        MarketRef.parse("Houston")


def test_fixture_files_are_small_enough_to_commit() -> None:
    total = sum(p.stat().st_size for p in Path(FIXTURES_DIR).glob("*.json"))
    assert total < 2_000_000


def test_fixture_dates_are_anchored_not_clock_based() -> None:
    deeds = mock_data_providers().deeds.records  # type: ignore[attr-defined]
    assert max(r.sale_date for r in deeds) <= UNTIL
    assert min(r.sale_date for r in deeds) >= date(2024, 9, 1)
