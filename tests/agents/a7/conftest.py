"""Shared A7 fixtures: the fixture property provider, config, identity and subject factory."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from wholesaling.agents.a7.engine import A7OfferEngine, SubjectInput
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.config.tunables import A7Config, DealBand, Tunables, get_tunables
from wholesaling.db.models.enums import ConditionClass
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    MockPropertyDataProvider,
    PropertyAddress,
    mock_property_provider,
)

IDENTITY = SenderIdentity(
    name="Aaron",
    company="NorthStack",
    postal_address="123 Main St, Houston, TX 77002",
    opt_out_text='Reply "unsubscribe" to opt out.',
)

CLEAN = PropertyAddress("1204 Oak Hollow Dr", "Houston", "TX", "77008")
TIER2 = PropertyAddress("88 Pine Ridge Ln", "Houston", "TX", "77018")
SQFT_GAP = PropertyAddress("4510 Maple Ct", "Houston", "TX", "77092")
CONFLICT = PropertyAddress("61 Elm Loop", "Houston", "TX", "77022")
DIRTY = PropertyAddress("930 Cedar Bend", "Houston", "TX", "77055")
AVM_GAP = PropertyAddress("2201 Birch St", "Houston", "TX", "77007")
DRIFT = PropertyAddress("717 Willow Way", "Houston", "TX", "77009")
UNKNOWN = PropertyAddress("1 Nowhere Rd", "Houston", "TX", "77001")


@pytest.fixture
def tunables() -> Tunables:
    return get_tunables()


@pytest.fixture
def a7_config(tunables: Tunables) -> A7Config:
    return tunables.a7


@pytest.fixture
def deal_band(tunables: Tunables) -> DealBand:
    return tunables.deal_band


@pytest.fixture
def provider() -> MockPropertyDataProvider:
    return mock_property_provider()


@pytest.fixture
def a7_engine(provider: MockPropertyDataProvider, tunables: Tunables) -> A7OfferEngine:
    return A7OfferEngine(
        provider,
        tunables.a7,
        deal_band=tunables.deal_band,
        identity=IDENTITY,
        today=DEFAULT_TODAY,
    )


MakeSubject = Callable[..., SubjectInput]


@pytest.fixture
def make_subject() -> MakeSubject:
    def _make(address: PropertyAddress = CLEAN, **overrides: Any) -> SubjectInput:
        base: dict[str, Any] = {
            "address": address,
            "market_name": "Houston",
            "condition_class": ConditionClass.MEDIUM,
            "disclosed_issues": ["roof"],
            "asking_price": Decimal("100000"),
            "photo_count": 2,
            "seller_name": "Pat Doe",
            "seller_email": "pat@example.test",
            "motivation": "relocating for work",
            "liens": "none",
            "liens_unknown": False,
            "hoa": "no",
            "hoa_unknown": False,
            "co_owners": "none",
            "co_owners_unknown": False,
        }
        base.update(overrides)
        return SubjectInput(**base)

    return _make
