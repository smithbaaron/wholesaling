"""Fixtures for the discovery-agent tests: mock providers and the fixture anchor date."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from tests.db.conftest import MakeInvestor, make_investor
from wholesaling.integrations.data.generate_fixtures import AS_OF
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.data.models import DeedRecord

AS_OF_DATE: date = AS_OF.date()

__all__ = ["AS_OF_DATE", "MakeInvestor", "make_deed", "make_investor"]


@pytest.fixture
def providers() -> DataProviders:
    return mock_data_providers()


@pytest.fixture
def as_of() -> datetime:
    return AS_OF


def make_deed(
    buyer: str,
    *,
    months_ago: float = 1.0,
    price: int = 150_000,
    financing: str = "cash",
    property_type: str = "sfr",
    units: int = 1,
    zip_code: str = "77009",
    seller: str = "Prior Owner",
    parcel_id: str | None = None,
    ref: str | None = None,
    city: str = "Houston",
    state: str = "TX",
    mailing: tuple[str, str, str, str] | None = None,
    entity_type: str = "llc",
) -> DeedRecord:
    """Hand-built deed for unit tests (dates relative to the fixture anchor)."""
    from datetime import timedelta

    sale_date = (AS_OF - timedelta(days=round(months_ago * 30.4375))).date()
    ref = ref or f"T-{buyer}-{months_ago}-{price}"
    return DeedRecord(
        parcel_id=parcel_id or f"P-{ref}",
        address="1 Test St",
        city=city,
        state=state,
        zip=zip_code,
        sale_date=sale_date,
        sale_price=Decimal(price),
        buyer_name=buyer,
        buyer_entity_type=entity_type,  # type: ignore[arg-type]
        seller_name=seller,
        financing=financing,  # type: ignore[arg-type]
        property_type=property_type,  # type: ignore[arg-type]
        units=units,
        source="test",
        source_ref=ref,
        source_url=None,
        retrieved_at=AS_OF,
        buyer_mailing_address=mailing[0] if mailing else None,
        buyer_mailing_city=mailing[1] if mailing else None,
        buyer_mailing_state=mailing[2] if mailing else None,
        buyer_mailing_zip=mailing[3] if mailing else None,
    )
