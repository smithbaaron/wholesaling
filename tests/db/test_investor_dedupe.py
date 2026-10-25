"""INV-06: LLC variants collapse to one investor; every source deed stays as evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models import Contact, Investor, SourceEvidence
from wholesaling.db.models.enums import ContactChannel, InvestorType
from wholesaling.db.normalize import normalize_email
from wholesaling.db.repositories.investors import (
    EvidenceInput,
    InvestorRepository,
    MailingAddress,
)

VARIANTS = [
    "Acme Holdings LLC",
    "ACME HOLDINGS, L.L.C.",
    "Acme Holdings Limited Liability Company",
    "The Acme Holdings LLC.",
    "acme holdings llc",
]


async def _investor_count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(Investor))).scalar_one())


async def test_llc_variants_collapse_to_one_investor_with_all_evidence(
    session: AsyncSession,
) -> None:
    repo = InvestorRepository(session)
    results = []
    for i, name in enumerate(VARIANTS):
        results.append(
            await repo.upsert_by_entity_key(
                name,
                evidence=[
                    EvidenceInput(
                        source="harris_county_deeds",
                        ref=f"deed-{i}",
                        url=f"https://example.test/deeds/{i}",
                        retrieved_at=datetime(2026, 10, 1 + i, tzinfo=UTC),
                        confidence=Decimal("0.9"),
                        raw={"grantee": name},
                    )
                ],
            )
        )

    assert results[0].created is True
    assert all(r.created is False for r in results[1:])
    assert len({r.investor.id for r in results}) == 1
    assert await _investor_count(session) == 1

    inv = results[0].investor
    assert inv.entity_key == "ACME HOLDINGS LLC"
    assert inv.canonical_name == "Acme Holdings LLC", "first spelling is kept as canonical"
    assert set(inv.aliases) == set(VARIANTS)
    assert inv.is_entity is True

    evidence = await repo.list_evidence(inv.id)
    assert len(evidence) == 5
    assert [e.raw["grantee"] for e in evidence] == VARIANTS
    assert all(e.source == "harris_county_deeds" for e in evidence)


async def test_duplicate_evidence_ref_is_not_double_counted(session: AsyncSession) -> None:
    repo = InvestorRepository(session)
    ev = EvidenceInput(source="deeds", ref="deed-1", raw={"a": 1})
    first = await repo.upsert_by_entity_key("Acme Holdings LLC", evidence=[ev])
    second = await repo.upsert_by_entity_key("ACME HOLDINGS L.L.C.", evidence=[ev])
    assert first.evidence_added == 1 and second.evidence_added == 0
    assert len(await repo.list_evidence(first.investor.id)) == 1

    # Evidence without a ref can't be deduped, so it is always kept (never lost).
    third = await repo.upsert_by_entity_key(
        "Acme Holdings LLC", evidence=[EvidenceInput(source="web", raw={"page": 1})]
    )
    assert third.evidence_added == 1
    assert len(await repo.list_evidence(first.investor.id)) == 2


async def test_merge_is_conservative(session: AsyncSession) -> None:
    repo = InvestorRepository(session)
    first = await repo.upsert_by_entity_key(
        "Acme Holdings LLC",
        investor_type=InvestorType.FLIPPER,
        mailing_address=MailingAddress("123 Main St", "Houston", "TX", "77002"),
        score=62,
        purchase_count_24m=3,
        last_purchase_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_refs={"deeds": ["d1"]},
    )
    second = await repo.upsert_by_entity_key(
        "Acme Holdings, L.L.C.",
        investor_type=InvestorType.WHOLESALER,  # must NOT overwrite a known type
        mailing_address=MailingAddress("999 Other Rd", "Dallas", "TX", "75201"),  # keep first
        score=70,  # latest score wins
        purchase_count_24m=2,  # max wins
        last_purchase_at=datetime(2025, 6, 1, tzinfo=UTC),  # older: keep newer
        first_name="Pat",  # fills a blank
        source_refs={"deeds": ["d2", "d1"], "sos": ["tx-1"]},  # union
    )
    assert second.created is False
    inv = second.investor
    assert inv.id == first.investor.id
    assert inv.investor_type is InvestorType.FLIPPER
    assert inv.mailing_address_line1 == "123 Main St"
    assert inv.mailing_address_key == "123 MAIN ST|HOUSTON|TX|77002"
    assert inv.score == 70
    assert inv.purchase_count_24m == 3
    assert inv.last_purchase_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert inv.first_name == "Pat"
    assert inv.source_refs == {"deeds": ["d1", "d2"], "sos": ["tx-1"]}


async def test_unknown_type_is_upgraded_but_known_type_is_not(session: AsyncSession) -> None:
    repo = InvestorRepository(session)
    r1 = await repo.upsert_by_entity_key("Beta Props LLC")
    assert r1.investor.investor_type is InvestorType.UNKNOWN
    r2 = await repo.upsert_by_entity_key("Beta Props LLC", investor_type=InvestorType.BUY_AND_HOLD)
    assert r2.investor.investor_type is InvestorType.BUY_AND_HOLD
    r3 = await repo.upsert_by_entity_key("Beta Props LLC", investor_type=InvestorType.FLIPPER)
    assert r3.investor.investor_type is InvestorType.BUY_AND_HOLD


async def test_different_entities_stay_separate(session: AsyncSession) -> None:
    repo = InvestorRepository(session)
    a = await repo.upsert_by_entity_key("Acme Holdings LLC")
    b = await repo.upsert_by_entity_key("Acme Holdings Trust")
    c = await repo.upsert_by_entity_key("Acme Holdings II LLC")
    d = await repo.upsert_by_entity_key("John Smith")
    assert len({a.investor.id, b.investor.id, c.investor.id, d.investor.id}) == 4
    assert d.investor.is_entity is False
    assert await _investor_count(session) == 4


async def test_entity_key_unique_constraint_is_the_backstop(session: AsyncSession) -> None:
    session.add(Investor(canonical_name="X LLC", entity_key="X LLC"))
    await session.flush()
    session.add(Investor(canonical_name="x llc", entity_key="X LLC"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_contact_email_unique_per_normalized_value(session: AsyncSession) -> None:
    """DM-02 unique email normalisation: two spellings of one Gmail box cannot both be stored."""
    repo = InvestorRepository(session)
    inv = (await repo.upsert_by_entity_key("Acme Holdings LLC")).investor
    raw1, raw2 = "Pat.Acme+deals@gmail.com", "patacme@gmail.com"
    assert normalize_email(raw1) == normalize_email(raw2)
    session.add(
        Contact(
            investor_id=inv.id,
            channel=ContactChannel.EMAIL,
            value=raw1,
            normalized_value=normalize_email(raw1),
        )
    )
    await session.flush()
    session.add(
        Contact(
            investor_id=inv.id,
            channel=ContactChannel.EMAIL,
            value=raw2,
            normalized_value=normalize_email(raw2),
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_evidence_requires_a_subject(session: AsyncSession) -> None:
    session.add(SourceEvidence(source="deeds", retrieved_at=datetime.now(tz=UTC)))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
