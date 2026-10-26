"""Projection contract: TAB_SPECS header contract, stable ids first, status column everywhere,
ISO UTC datetimes, money as numbers, and one golden row per tab from the shared seed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integrations.sheets.conftest import (
    BB_INACTIVE_ID,
    BB_V1_ID,
    BB_V2_ID,
    CONTACT_ID,
    INVESTOR2_ID,
    INVESTOR_ID,
    MARKET_ID,
    MATCH_ID,
    MSG_DRAFT_ID,
    MSG_IN_ID,
    MSG_OUT_ID,
    PROFILE_ID,
    PROPERTY_ID,
    QA_ID,
    SUPPRESSION_ID,
    THREAD_ID,
    seed_projection,
)
from wholesaling.activities.sheets import load_snapshot
from wholesaling.integrations.sheets import TAB_ORDER, TAB_SPECS, TabSpec, build_rows, row_as_dict
from wholesaling.integrations.sheets.interface import canon_cell, canon_row, iso_utc
from wholesaling.integrations.sheets.projection import (
    STATUS_CURRENT,
    STATUS_DRAFT,
    STATUS_EXCLUDED,
    STATUS_INACTIVE,
    STATUS_RECEIVED,
    STATUS_SENT,
    STATUS_SUPERSEDED,
    TAB_BUY_BOXES,
    TAB_CONTACTS,
    TAB_CONVERSATIONS,
    TAB_INVESTORS,
    TAB_MATCHES,
    TAB_OUTREACH_EVENTS,
    TAB_PROPERTIES,
    TAB_QA_LOG,
    TAB_SEARCH_PROFILES,
    TAB_SUPPRESSIONS,
    ProjectionSnapshot,
)

EXPECTED_TABS = (
    "Investors",
    "Contacts",
    "Buy Boxes",
    "Search Profiles",
    "Properties",
    "Matches",
    "Conversations",
    "Suppressions",
    "QA Log",
    "Outreach Events",
)


# --------------------------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------------------------


def test_tab_registry_matches_int_14_plus_outreach_events() -> None:
    assert TAB_ORDER == EXPECTED_TABS
    assert set(TAB_SPECS) == set(EXPECTED_TABS)


@pytest.mark.parametrize("tab", TAB_ORDER)
def test_every_tab_has_stable_id_key_first_and_a_status_column(tab: str) -> None:
    spec = TAB_SPECS[tab]
    assert spec.name == tab
    assert spec.key_col == 0
    assert spec.key_name.endswith("_id"), "first column is the stable Postgres id (INT-14)"
    assert "status" in spec.header
    assert len(set(spec.header)) == len(spec.header), "header names are unique"
    assert all(h == h.lower() and " " not in h for h in spec.header)


def test_buy_box_rows_carry_investor_and_buy_box_ids() -> None:
    header = TAB_SPECS[TAB_BUY_BOXES].header
    assert header[:2] == ("buy_box_id", "investor_id")
    assert header.index("version") < header.index("status")


def test_tab_spec_rejects_missing_status_or_bad_key() -> None:
    with pytest.raises(ValueError, match="status"):
        TabSpec(name="X", header=("x_id", "name"))
    with pytest.raises(ValueError, match="key_col"):
        TabSpec(name="X", header=("x_id", "status"), key_col=5)
    with pytest.raises(ValueError, match="header"):
        TabSpec(name="X", header=())


def test_build_rows_rejects_unknown_tab() -> None:
    with pytest.raises(KeyError, match="unknown tab"):
        build_rows("Nope", ProjectionSnapshot())


def test_empty_snapshot_builds_no_rows() -> None:
    for tab in TAB_ORDER:
        assert build_rows(tab, ProjectionSnapshot()) == []


# --------------------------------------------------------------------------------------------
# Cell helpers
# --------------------------------------------------------------------------------------------


def test_iso_utc_normalises_naive_and_offset_datetimes() -> None:
    assert iso_utc(None) is None
    assert iso_utc(datetime(2026, 10, 25, 12, 0)) == "2026-10-25T12:00:00Z"
    from datetime import timedelta, timezone

    cst = timezone(timedelta(hours=-5))
    assert iso_utc(datetime(2026, 10, 25, 7, 0, tzinfo=cst)) == "2026-10-25T12:00:00Z"


def test_canon_cell_makes_sheets_round_trip_values_equal() -> None:
    assert canon_cell(None) == canon_cell("") == ""
    assert canon_cell(True) == canon_cell("TRUE") == "TRUE"
    assert canon_cell(250000) == canon_cell(250000.0) == canon_cell(Decimal("250000.00"))
    assert canon_cell(Decimal("0.7500")) == canon_cell(0.75) == "0.75"
    assert canon_cell("77009") == canon_cell(77009)
    assert canon_cell("abc") == "abc"
    assert canon_cell(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"


def test_canon_row_pads_short_rows_from_the_api() -> None:
    assert canon_row(["a", 1], 4) == canon_row(["a", 1.0, None, ""], 4) == ("a", "1", "", "")
    assert canon_row(["a", 1, "x", "y", "extra"], 4) == ("a", "1", "x", "y")


# --------------------------------------------------------------------------------------------
# Golden rows from the seed
# --------------------------------------------------------------------------------------------


@pytest.fixture
async def snapshot(session: AsyncSession) -> ProjectionSnapshot:
    await seed_projection(session)
    return await load_snapshot(session, TAB_ORDER)


@pytest.mark.parametrize("tab", TAB_ORDER)
async def test_rows_match_header_width_and_are_sorted_by_key(
    tab: str, snapshot: ProjectionSnapshot
) -> None:
    rows = build_rows(tab, snapshot)
    assert rows, f"seed should populate {tab}"
    width = TAB_SPECS[tab].width
    for row in rows:
        assert len(row) == width
        assert isinstance(row[0], str) and len(row[0]) == 36, "key is a uuid string"
        for cell in row:
            assert cell is None or isinstance(cell, str | int | float | bool)
            assert not isinstance(cell, Decimal) and not isinstance(cell, datetime)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)


async def test_investors_golden(snapshot: ProjectionSnapshot) -> None:
    rows = {r[0]: row_as_dict(TAB_INVESTORS, r) for r in build_rows(TAB_INVESTORS, snapshot)}
    acme = rows[str(INVESTOR_ID)]
    assert acme["status"] == "active"
    assert acme["canonical_name"] == "Acme Holdings LLC"
    assert acme["investor_type"] == "flipper"
    assert acme["score"] == 82
    assert acme["market_id"] == str(MARKET_ID) and acme["market_name"] == "Houston"
    assert acme["is_entity"] is True
    assert acme["last_purchase_at"] == "2026-10-20T09:30:00Z"
    assert acme["aliases"] == "Acme Holdings LLC; ACME HOLDINGS"
    assert acme["updated_at"] == "2026-10-24T15:45:00Z"
    blocked = rows[str(INVESTOR2_ID)]
    assert blocked["status"] == STATUS_EXCLUDED
    assert blocked["excluded_reason"] == "institutional"
    assert blocked["market_name"] is None


async def test_contacts_golden(snapshot: ProjectionSnapshot) -> None:
    [row] = build_rows(TAB_CONTACTS, snapshot)
    d = row_as_dict(TAB_CONTACTS, row)
    assert d["contact_id"] == str(CONTACT_ID) and d["investor_id"] == str(INVESTOR_ID)
    assert d["channel"] == "email"
    assert d["value"] == "Alex@Acme.test" and d["normalized_value"] == "alex@acme.test"
    assert d["is_primary"] is True
    assert d["confidence"] == 0.9


async def test_buy_boxes_golden_with_status_per_version(snapshot: ProjectionSnapshot) -> None:
    rows = {r[0]: row_as_dict(TAB_BUY_BOXES, r) for r in build_rows(TAB_BUY_BOXES, snapshot)}
    assert set(rows) == {str(BB_V1_ID), str(BB_V2_ID), str(BB_INACTIVE_ID)}
    v1, v2, dead = rows[str(BB_V1_ID)], rows[str(BB_V2_ID)], rows[str(BB_INACTIVE_ID)]
    assert v1["status"] == STATUS_SUPERSEDED and v1["version"] == 1
    assert v2["status"] == STATUS_CURRENT and v2["version"] == 2
    assert dead["status"] == STATUS_INACTIVE and dead["is_active"] is False
    assert v2["investor_id"] == str(INVESTOR_ID)
    assert v2["markets"] == "Houston" and v2["cities"] == "Houston; Pasadena"
    assert v2["zips"] == "77009" and v2["neighborhoods"] is None
    assert v2["asset_types"] == "sfr; duplex"
    assert v2["price_min"] == 100000 and v2["price_max"] == 250000
    assert isinstance(v2["price_max"], int)
    assert v2["closing_preferences"] == '{"days":14}'
    assert json.loads(str(v2["other_criteria"])) == {"no_hoa": True}
    assert v2["unknown_fields"] == "financing_proof"
    assert v2["raw_text"] == "SFR in 77009 under 250k, cash, light rehab\nActually min 100k."
    assert v2["source_message_id"] == str(MSG_IN_ID)
    assert v2["change_note"] == "correction: price_max"
    assert v1["price_min"] is None and v1["closing_preferences"] is None


async def test_search_profiles_golden_resolves_investor_through_buy_box(
    snapshot: ProjectionSnapshot,
) -> None:
    [row] = build_rows(TAB_SEARCH_PROFILES, snapshot)
    d = row_as_dict(TAB_SEARCH_PROFILES, row)
    assert d["search_profile_id"] == str(PROFILE_ID)
    assert d["buy_box_id"] == str(BB_V2_ID) and d["investor_id"] == str(INVESTOR_ID)
    assert d["status"] == "active"
    assert json.loads(str(d["criteria"])) == {"zips": ["77009"], "price_max": 250000}
    assert d["unknowns"] == "financing_proof" and d["criteria_hash"] == "abc123"


async def test_properties_golden_money_as_numbers(snapshot: ProjectionSnapshot) -> None:
    [row] = build_rows(TAB_PROPERTIES, snapshot)
    d = row_as_dict(TAB_PROPERTIES, row)
    assert d["property_candidate_id"] == str(PROPERTY_ID) and d["status"] == "active"
    assert d["list_price"] == 199000 and d["est_arv"] == 280000
    assert d["est_repairs"] == 35000.5 and d["est_fees"] is None
    assert d["liquidity_score"] == 0.75
    assert d["flags"] == "hoa_unknown"
    assert d["retrieved_at"] == "2026-10-24T15:45:00Z"


async def test_matches_golden_resolves_buy_box_and_investor(snapshot: ProjectionSnapshot) -> None:
    [row] = build_rows(TAB_MATCHES, snapshot)
    d = row_as_dict(TAB_MATCHES, row)
    assert d["match_id"] == str(MATCH_ID)
    assert d["search_profile_id"] == str(PROFILE_ID)
    assert d["property_candidate_id"] == str(PROPERTY_ID)
    assert d["buy_box_id"] == str(BB_V2_ID) and d["investor_id"] == str(INVESTOR_ID)
    assert d["status"] == "pending" and d["score"] == 0.81 and d["confidence"] == 0.6
    assert d["reason"] == "zip and price match" and d["next_action"] == "pull comps"


async def test_conversations_golden_aggregates_messages(snapshot: ProjectionSnapshot) -> None:
    [row] = build_rows(TAB_CONVERSATIONS, snapshot)
    d = row_as_dict(TAB_CONVERSATIONS, row)
    assert d["thread_id"] == str(THREAD_ID) and d["investor_id"] == str(INVESTOR_ID)
    assert d["message_count"] == 3 and d["inbound_count"] == 1 and d["outbound_count"] == 2
    assert d["last_message_at"] == "2026-10-24T15:45:00Z"
    assert d["last_direction"] in ("in", "out")
    assert d["counterpart_address"] == "alex@acme.test"


async def test_suppressions_and_qa_log_golden(snapshot: ProjectionSnapshot) -> None:
    [s] = build_rows(TAB_SUPPRESSIONS, snapshot)
    ds = row_as_dict(TAB_SUPPRESSIONS, s)
    assert ds["suppression_id"] == str(SUPPRESSION_ID) and ds["status"] == "suppressed"
    assert ds["channel"] == "email" and ds["reason"] == "bounce"
    assert ds["source_message_id"] == str(MSG_IN_ID)
    [q] = build_rows(TAB_QA_LOG, snapshot)
    dq = row_as_dict(TAB_QA_LOG, q)
    assert dq["qa_review_id"] == str(QA_ID) and dq["message_id"] == str(MSG_DRAFT_ID)
    assert dq["status"] == dq["verdict"] == "approve"
    assert dq["confidence"] == 0.95 and dq["flags"] is None
    assert dq["rationale"] == "No prohibited claims."


async def test_outreach_events_golden_status_per_message(snapshot: ProjectionSnapshot) -> None:
    rows = {
        r[0]: row_as_dict(TAB_OUTREACH_EVENTS, r) for r in build_rows(TAB_OUTREACH_EVENTS, snapshot)
    }
    assert set(rows) == {str(MSG_IN_ID), str(MSG_OUT_ID), str(MSG_DRAFT_ID)}
    assert rows[str(MSG_OUT_ID)]["status"] == STATUS_SENT
    assert rows[str(MSG_OUT_ID)]["sent_at"] == "2026-10-20T09:30:00Z"
    assert rows[str(MSG_OUT_ID)]["sequence_step"] == 0
    assert rows[str(MSG_IN_ID)]["status"] == STATUS_RECEIVED
    assert rows[str(MSG_IN_ID)]["classification"] == "buy_box_supplied"
    assert rows[str(MSG_DRAFT_ID)]["status"] == STATUS_DRAFT
    assert rows[str(MSG_DRAFT_ID)]["policy_decision"] == "queue_for_human"
    for d in rows.values():
        assert d["investor_id"] == str(INVESTOR_ID) and d["thread_id"] == str(THREAD_ID)
        assert d["to_addresses"] in ("alex@acme.test", "deals@example.test")


async def test_build_rows_is_deterministic(snapshot: ProjectionSnapshot) -> None:
    for tab in TAB_ORDER:
        assert build_rows(tab, snapshot) == build_rows(tab, snapshot)
