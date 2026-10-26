"""S2 Skip Trace: provenance, dedupe, suppression, DNC scrub recorded (REQUIREMENTS.md SEL-03,
CMP-02, RUL-06)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.s2 import PHONE_LIMITATION, S2SkipTrace, dedupe_candidates
from wholesaling.db.models.enums import Channel, SellerLeadStatus
from wholesaling.db.models.messaging import Suppression
from wholesaling.db.models.sellers import SellerLead
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.data.models import ContactCandidate

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def providers() -> DataProviders:
    return mock_data_providers()


async def _lead(session: AsyncSession, owner: str, address: str = "1 Test St") -> SellerLead:
    lead = SellerLead(
        status=SellerLeadStatus.QUEUED,
        owner_name=owner,
        property_address_line1=address,
        property_city="Houston",
        property_state="TX",
        property_postal_code="77009",
        signals=["absentee", "vacant"],
        signal_score=30,
        assessed_value=Decimal("150000"),
        source="test",
        source_refs={"owner_mailing_address": "PO Box 1"},
    )
    session.add(lead)
    await session.flush()
    return lead


def cand(channel: str, value: str, conf: float = 0.5) -> ContactCandidate:
    return ContactCandidate(
        channel=channel, value=value, source="v", provenance="p", confidence=conf
    )  # type: ignore[arg-type]


def test_dedupe_normalises_drops_junk_and_keeps_highest_confidence() -> None:
    kept, junk, dupes = dedupe_candidates(
        [
            cand("email", "A@Example.test", 0.6),
            cand("email", "a@example.test", 0.9),
            cand("email", "n/a"),
            cand("email", "not an email"),
            cand("phone", "(713) 555-0101", 0.7),
            cand("phone", "713-555-0101", 0.4),
            cand("address", "PO Box 1, Katy TX"),
        ]
    )
    assert junk == 2 and dupes == 2
    by_channel = {c.channel: c for c in kept}
    assert (
        by_channel["email"].confidence == 0.9
        and by_channel["email"].normalized_value == "a@example.test"
    )
    assert (
        by_channel["phone"].normalized_value == "+17135550101"
        and by_channel["phone"].confidence == 0.7
    )
    assert by_channel["phone"].use_limitations == PHONE_LIMITATION
    assert by_channel["address"].normalized_value == "PO BOX 1, KATY TX"


async def test_trace_records_contacts_with_provenance_and_primary_email(
    session: AsyncSession, providers: DataProviders
) -> None:
    lead = await _lead(session, "Thomas Nguyen", "915 Linden Ct")
    result = await S2SkipTrace().run(session, providers, lead, now=NOW)
    assert result.vendor == "skip_trace" and result.candidates == 4
    assert result.dropped_junk == 1 and result.dropped_duplicate == 1
    assert result.primary_email == "t.nguyen@example.test" and result.reachable
    channels = lead.contact_channels
    assert [c["channel"] for c in channels] == ["email", "phone"]
    assert channels[0]["is_primary"] is True and channels[0]["confidence"] == 0.78
    assert channels[0]["provenance"] == "vendor match: mailing address"
    assert channels[1]["dnc_listed"] is True  # 281-555-0142 is on the DNC fixture
    assert channels[1]["use_limitations"] == PHONE_LIMITATION
    meta = lead.source_refs["skip_trace"]
    assert meta["dnc_listed"] is True and meta["dnc_checked"] == 1
    assert meta["sms_allowed"] is False and meta["preferred_channel"] == "email"
    assert meta["traced_at"] == NOW.isoformat()
    assert result.to_json()["seller_lead_id"] == str(lead.id)


async def test_dnc_scrub_is_recorded_even_when_the_phone_is_clean(
    session: AsyncSession, providers: DataProviders
) -> None:
    lead = await _lead(session, "Gerald Pike")
    result = await S2SkipTrace().run(session, providers, lead, now=NOW)
    assert result.dnc_checked == 1 and result.dnc_listed is False
    phone = next(c for c in lead.contact_channels if c["channel"] == "phone")
    assert phone["dnc_listed"] is False
    assert lead.source_refs["skip_trace"]["dnc_listed"] is False


async def test_phone_only_owner_is_unreachable_by_email(
    session: AsyncSession, providers: DataProviders
) -> None:
    lead = await _lead(session, "Estate of Walter Boyd")
    result = await S2SkipTrace().run(session, providers, lead, now=NOW)
    assert result.kept == 1 and result.primary_email is None and not result.reachable
    assert result.dnc_listed is True


async def test_unknown_owner_yields_no_contacts(
    session: AsyncSession, providers: DataProviders
) -> None:
    lead = await _lead(session, "Nobody Known")
    result = await S2SkipTrace().run(session, providers, lead, now=NOW)
    assert result.candidates == 0 and result.kept == 0 and not result.reachable
    assert lead.contact_channels == [] and "skip_trace" in lead.source_refs


async def test_suppressed_email_is_kept_in_the_record_but_never_primary(
    session: AsyncSession, providers: DataProviders
) -> None:
    session.add(
        Suppression(channel=Channel.EMAIL, normalized_value="gpike@example.test", reason="opt_out")
    )
    await session.flush()
    lead = await _lead(session, "Gerald Pike")
    result = await S2SkipTrace().run(session, providers, lead, now=NOW)
    assert result.suppressed_emails == 1 and result.primary_email is None
    email = next(c for c in lead.contact_channels if c["channel"] == "email")
    assert email["suppressed"] is True and email["is_primary"] is False


async def test_missing_provider_or_owner_is_skipped(session: AsyncSession) -> None:
    bundle = mock_data_providers()
    without = DataProviders(
        deeds=bundle.deeds,
        signals=bundle.signals,
        contacts=bundle.contacts,
        reference_lists=bundle.reference_lists,
    )
    lead = await _lead(session, "Gerald Pike")
    assert (
        await S2SkipTrace().run(session, without, lead)
    ).skipped_reason == "no_skiptrace_provider"
    nameless = await _lead(session, "")
    assert (await S2SkipTrace().run(session, bundle, nameless)).skipped_reason == "no_owner_name"
