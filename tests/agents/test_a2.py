"""A2 contact research: dedupe, junk, provenance, suppression, primary choice (INV-08, INV-09)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.agents.conftest import MakeInvestor
from wholesaling.activities import discovery
from wholesaling.agents.a2 import PHONE_CAPTURE_ONLY, A2ContactResearch, dedupe_candidates
from wholesaling.db.models import Contact, Investor, Suppression
from wholesaling.db.models.enums import Channel, ContactChannel
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.models import ContactCandidate


def _c(channel: str, value: str, confidence: float = 0.5, source: str = "s") -> ContactCandidate:
    return ContactCandidate(
        channel=channel, value=value, source=source, provenance="p", confidence=confidence
    )  # type: ignore[arg-type]


def test_dedupe_folds_case_dots_and_plus_tags_and_drops_junk() -> None:
    kept, junk, dups = dedupe_candidates(
        [
            _c("email", "Deals@Example.com", 0.9),
            _c("email", "deals@example.com", 0.7),
            _c("email", "maria.h+deals@gmail.com", 0.4),
            _c("email", "mariah@gmail.com", 0.6),
            _c("email", "not-an-email", 0.9),
            _c("email", "n/a"),
            _c("phone", "(713) 555-0142", 0.8),
            _c("phone", "713-555-0142", 0.6),
            _c("phone", "12345", 0.9),
        ]
    )
    assert junk == 3 and dups == 3
    by_key = {(k.channel, k.normalized): k.candidate.confidence for k in kept}
    assert by_key == {
        (ContactChannel.EMAIL, "deals@example.com"): 0.9,
        (ContactChannel.EMAIL, "mariah@gmail.com"): 0.6,
        (ContactChannel.PHONE, "+17135550142"): 0.8,
    }


def test_dedupe_keeps_highest_confidence_candidate() -> None:
    kept, _, _ = dedupe_candidates(
        [_c("email", "a@b.co", 0.3, "web"), _c("email", "A@B.CO", 0.8, "sos")]
    )
    assert len(kept) == 1 and kept[0].candidate.source == "sos"


async def test_bayou_gets_primary_email_with_provenance_and_limits(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    inv = await make_investor(
        "Bayou Capital Partners LLC", mailing_address_line1="4400 Post Oak Pkwy Ste 200"
    )
    res = await A2ContactResearch().run_detailed(session, providers, inv)
    assert res.primary_email == "deals@bayoucapitalpartners.com"
    assert res.candidates == 7 and res.junk_dropped == 1 and res.duplicates_folded == 2
    assert res.contacts_created == 4 and res.by_channel == {"email": 2, "phone": 1, "address": 1}
    rows = list(
        (await session.execute(select(Contact).where(Contact.investor_id == inv.id))).scalars()
    )
    assert len(rows) == 4
    primary = next(r for r in rows if r.is_primary)
    assert primary.normalized_value == "deals@bayoucapitalpartners.com"
    assert primary.confidence == Decimal("0.9")
    assert primary.provenance == "company_website: Contact page of bayoucapitalpartners.com"
    assert primary.provenance_url == "https://bayoucapitalpartners.example/contact"
    assert primary.use_limitations and "no SMS" in primary.use_limitations
    others = [r for r in rows if r.channel is ContactChannel.EMAIL and not r.is_primary]
    assert [o.normalized_value for o in others] == ["info@bayoucapitalpartners.com"]


async def test_phone_is_captured_with_capture_only_limitation_never_primary(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    inv = await make_investor("Bayou Capital Partners LLC")
    await A2ContactResearch().run(session, providers, inv)
    phone = (
        await session.execute(
            select(Contact).where(
                Contact.investor_id == inv.id, Contact.channel == ContactChannel.PHONE
            )
        )
    ).scalar_one()
    assert phone.normalized_value == "+17135550142"
    assert phone.is_primary is False
    assert phone.use_limitations is not None and PHONE_CAPTURE_ONLY in phone.use_limitations
    assert phone.confidence == Decimal("0.8")  # the duplicate at 0.6 was folded


async def test_no_email_returns_none_but_still_stores_phone_and_address(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    inv = await make_investor("Gulf Coast Rentals LLC")
    assert await A2ContactResearch().run(session, providers, inv) is None
    rows = list(
        (await session.execute(select(Contact).where(Contact.investor_id == inv.id))).scalars()
    )
    assert {r.channel for r in rows} == {ContactChannel.PHONE, ContactChannel.ADDRESS}
    assert not any(r.is_primary for r in rows)


async def test_gmail_dot_variants_dedupe_to_one_row(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    inv = await make_investor("Maria Hernandez", is_entity=False)
    res = await A2ContactResearch().run_detailed(session, providers, inv)
    assert res.primary_email == "mariahernandezhtx@gmail.com"
    assert res.duplicates_folded == 1 and res.contacts_created == 1
    row = (await session.execute(select(Contact).where(Contact.investor_id == inv.id))).scalar_one()
    assert row.value == "maria.hernandez.htx@gmail.com"  # raw kept beside the normalised key
    assert row.confidence == Decimal("0.55")


async def test_rerun_updates_instead_of_duplicating(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    inv = await make_investor("Bayou Capital Partners LLC")
    a2 = A2ContactResearch()
    await a2.run(session, providers, inv)
    res = await a2.run_detailed(session, providers, inv)
    assert res.contacts_created == 0 and res.contacts_updated == 4
    rows = list(
        (await session.execute(select(Contact).where(Contact.investor_id == inv.id))).scalars()
    )
    assert len(rows) == 4 and sum(r.is_primary for r in rows) == 1


async def test_suppressed_email_is_skipped_and_next_best_becomes_primary(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    session.add(
        Suppression(
            channel=Channel.EMAIL,
            normalized_value="deals@bayoucapitalpartners.com",
            reason="opt_out",
        )
    )
    await session.flush()
    inv = await make_investor("Bayou Capital Partners LLC")
    res = await A2ContactResearch().run_detailed(session, providers, inv)
    assert res.suppressed == 1
    assert res.primary_email == "info@bayoucapitalpartners.com"
    values = {r.normalized_value for r in (await session.execute(select(Contact))).scalars()}
    assert "deals@bayoucapitalpartners.com" not in values


async def test_contact_already_owned_by_another_investor_is_not_reassigned(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    other = await make_investor("Some Other LLC")
    session.add(
        Contact(
            investor_id=other.id,
            channel=ContactChannel.EMAIL,
            value="deals@bayoucapitalpartners.com",
            normalized_value="deals@bayoucapitalpartners.com",
            confidence=Decimal("0.5"),
        )
    )
    await session.flush()
    inv = await make_investor("Bayou Capital Partners LLC")
    res = await A2ContactResearch().run_detailed(session, providers, inv)
    assert res.owned_by_other_investor == 1
    assert res.primary_email == "info@bayoucapitalpartners.com"
    owner = (
        await session.execute(
            select(Contact).where(Contact.normalized_value == "deals@bayoucapitalpartners.com")
        )
    ).scalar_one()
    assert owner.investor_id == other.id


async def test_unknown_entity_yields_no_contacts(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    inv = await make_investor("Nobody Knows This LLC")
    res = await A2ContactResearch().run_detailed(session, providers, inv)
    assert res.candidates == 0 and res.primary_email is None and res.contacts_created == 0


async def test_lookup_receives_mailing_address(
    session: AsyncSession, providers: DataProviders, make_investor: MakeInvestor
) -> None:
    inv = await make_investor(
        "Bayou Capital Partners LLC",
        mailing_address_line1="4400 Post Oak Pkwy",
        mailing_address_line2="Ste 200",
        mailing_city="Houston",
        mailing_state="TX",
        mailing_postal_code="77027",
    )
    await A2ContactResearch().run(session, providers, inv)
    call = providers.contacts.calls[-1]  # type: ignore[attr-defined]
    assert call["entity_name"] == "Bayou Capital Partners LLC"
    assert call["mailing_address"] == "4400 Post Oak Pkwy, Ste 200, Houston, TX 77027"


async def test_run_a2_activity(
    session_factory: async_sessionmaker[AsyncSession], providers: DataProviders
) -> None:
    from wholesaling.db.normalize import normalize_entity_name

    async with session_factory() as s, s.begin():
        inv = Investor(
            canonical_name="Lone Star Flips LLC",
            entity_key=normalize_entity_name("Lone Star Flips LLC"),
            aliases=["Lone Star Flips LLC"],
        )
        s.add(inv)
        await s.flush()
        investor_id = inv.id
    discovery.configure(session_factory, providers)
    try:
        payload = await discovery.run_a2(str(investor_id))
        assert payload["primary_email"] == "acquisitions@lonestarflips.com"
        assert payload["by_channel"] == {"email": 1, "phone": 1}
        with pytest.raises(LookupError):
            await discovery.run_a2("00000000-0000-0000-0000-000000000000")
    finally:
        discovery.reset()
