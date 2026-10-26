"""D1 search-profile engine (BB-04..BB-06): normalisation, hashing, coalescing, superseding."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import MakeInvestor
from wholesaling.agents.d1 import (
    ORIGINAL_WORDING_KEY,
    D1SearchProfileEngine,
    condition_within,
    hashable_criteria,
    normalize_condition,
    normalize_market,
    normalize_property_class,
    normalize_zip,
)
from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.enums import SearchProfileStatus
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria, BuyBoxRepository
from wholesaling.db.repositories.deals import SearchProfileRepository


def _box(**overrides: object) -> BuyBox:
    base: dict[str, object] = {
        "investor_id": uuid.uuid4(),
        "buy_box_key": "default",
        "version": 1,
        "geography": {
            "markets": ["houston, tx"],
            "cities": ["spring", "The Woodlands"],
            "zips": ["77008", "77018-1234", "bad"],
        },
        "asset_types": ["Single family", "duplexes"],
        "price_min": Decimal("100000.00"),
        "price_max": Decimal("300000.00"),
        "condition": "any condition",
        "strategy": "Flip",
        "financing": "Cash",
        "closing_preferences": {"Close In Days": 21},
        "other_criteria": {"No Flood Zone": True},
        "unknown_fields": ["financing_terms"],
        "raw_text": "Looking for SFR or duplexes in Houston up to 300k, any condition",
    }
    base.update(overrides)
    return BuyBox(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SFR", "sfr"),
        ("single-family homes", "sfr"),
        ("Duplex", "small_multi"),
        ("2-4 units", "small_multi"),
        ("townhomes", "condo_townhome"),
        ("vacant land", "land"),
        ("retail strip", "commercial"),
        ("mobile homes", "mobile_homes"),
    ],
)
def test_normalize_property_class(raw: str, expected: str) -> None:
    assert normalize_property_class(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("turnkey", "light"),
        ("light cosmetic work ok", "light"),
        ("moderate", "medium"),
        ("heavy rehab fine", "heavy"),
        ("any condition", "gut"),
        ("light to heavy", "heavy"),
        ("pristine", None),
    ],
)
def test_normalize_condition(raw: str | None, expected: str | None) -> None:
    assert normalize_condition(raw) == expected


def test_normalize_zip_and_market() -> None:
    assert normalize_zip("77018-1234") == "77018" and normalize_zip("zip 77008 please") == "77008"
    assert normalize_zip("1234") is None
    assert normalize_market("houston, tx") == "Houston, TX"
    assert normalize_market("san antonio") == "San Antonio"


def test_condition_within() -> None:
    assert condition_within("heavy", "medium") is True
    assert condition_within("light", "heavy") is False
    assert condition_within(None, "heavy") is None and condition_within("gut", None) is None


def test_normalize_buy_box_into_criteria() -> None:
    profile = D1SearchProfileEngine().normalize(_box())
    c = profile.criteria
    assert c["geography"] == {
        "markets": ["Houston, TX"],
        "cities": ["Spring", "The Woodlands"],
        "zips": ["77008", "77018"],
        "neighborhoods": [],
    }
    assert c["property_classes"] == ["sfr", "small_multi"]
    assert c["price_min"] == "100000" and c["price_max"] == "300000"
    assert (
        c["condition_tolerance"] == "gut" and c["strategy"] == "flip" and c["financing"] == "cash"
    )
    assert c["closing"] == {"close_in_days": 21} and c["constraints"] == {"no_flood_zone": True}
    assert c[ORIGINAL_WORDING_KEY].startswith("Looking for SFR")
    assert profile.unknowns == ["financing_terms"]


def test_missing_required_fields_become_unknowns() -> None:
    profile = D1SearchProfileEngine().normalize(
        _box(geography={}, asset_types=[], price_max=None, condition=None, unknown_fields=[])
    )
    assert profile.unknowns == ["condition", "geography", "price_max", "property_classes"]
    assert profile.criteria["unknowns"] == profile.unknowns


def test_hash_ignores_wording_but_not_structure() -> None:
    engine = D1SearchProfileEngine()
    a = engine.normalize(_box(raw_text="first wording"))
    b = engine.normalize(_box(raw_text="totally different words", version=2))
    c = engine.normalize(_box(price_max=Decimal("350000"), version=3))
    assert a.hash == b.hash and a.hash != c.hash
    assert ORIGINAL_WORDING_KEY not in hashable_criteria(
        a.criteria
    ) and "profile_version" not in hashable_criteria(a.criteria)
    assert a.criteria[ORIGINAL_WORDING_KEY] != b.criteria[ORIGINAL_WORDING_KEY]


async def test_repository_one_active_profile_coalesce_and_supersede(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor("D1 Holdings LLC")
    boxes = BuyBoxRepository(session)
    v1 = await boxes.create_version(
        inv.id,
        BuyBoxCriteria(
            geography={"zips": ["77008"]},
            asset_types=["sfr"],
            price_max=Decimal(300000),
            condition="any",
            raw_text="a",
        ),
    )
    engine = D1SearchProfileEngine()
    repo = SearchProfileRepository(session)
    n1 = engine.normalize(v1.buy_box)
    p1, created, superseded = await repo.apply(
        v1.buy_box.id, n1.criteria, n1.unknowns, digest=n1.hash
    )
    assert (
        created
        and superseded is None
        and p1.version == 1
        and p1.status is SearchProfileStatus.ACTIVE
    )

    # Same structure, new wording -> coalesced: no new version, nothing superseded.
    v1.buy_box.raw_text = "different words"
    n1b = engine.normalize(v1.buy_box)
    p1b, created_b, superseded_b = await repo.apply(
        v1.buy_box.id, n1b.criteria, n1b.unknowns, digest=n1b.hash
    )
    assert not created_b and superseded_b is None and p1b.id == p1.id
    assert len(await repo.list_versions(v1.buy_box.id)) == 1

    # Meaningful change -> version 2 active, version 1 superseded.
    v1.buy_box.price_max = Decimal(350000)
    n2 = engine.normalize(v1.buy_box)
    p2, created_2, superseded_2 = await repo.apply(
        v1.buy_box.id, n2.criteria, n2.unknowns, digest=n2.hash
    )
    assert created_2 and superseded_2 is not None and superseded_2.id == p1.id
    assert p2.version == 2 and p2.status is SearchProfileStatus.ACTIVE
    await session.refresh(p1)
    assert p1.status is SearchProfileStatus.SUPERSEDED
    active = await repo.get_active(v1.buy_box.id)
    assert active is not None and active.id == p2.id
    assert [p.id for p in await repo.list_active()] == [p2.id]
    deactivated = await repo.deactivate(v1.buy_box.id)
    assert deactivated is not None and deactivated.status is SearchProfileStatus.INACTIVE
