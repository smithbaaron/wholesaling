"""A6 Parser Formatter: golden string, stable labels, delimiters, raw text last, idempotency key."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from wholesaling.agents.a6 import (
    BEGIN_MARKER,
    END_MARKER,
    PARSER_FORMAT_VERSION,
    SEPARATOR,
    STRUCTURED_LABELS,
    A6ParserFormatter,
    ParserEmail,
    parser_idempotency_key,
    parser_row_key,
)
from wholesaling.db.models import BuyBox, Investor
from wholesaling.db.models.enums import AgentId

INVESTOR_ID = uuid.UUID("00000000-0000-4000-8000-000000000010")
BB_ID = uuid.UUID("00000000-0000-4000-8000-000000000041")
MSG_ID = uuid.UUID("00000000-0000-4000-8000-000000000031")
T1 = datetime(2026, 10, 24, 15, 45, tzinfo=UTC)


def investor() -> Investor:
    return Investor(
        id=INVESTOR_ID, canonical_name="Acme  Holdings\nLLC", entity_key="ACME HOLDINGS LLC"
    )


def buy_box(**overrides: object) -> BuyBox:
    values: dict[str, object] = {
        "id": BB_ID,
        "investor_id": INVESTOR_ID,
        "buy_box_key": "default",
        "version": 2,
        "is_current": True,
        "is_active": True,
        "geography": {"markets": ["Houston"], "cities": ["Houston", "Pasadena"], "zips": ["77009"]},
        "asset_types": ["sfr", "duplex"],
        "price_min": Decimal("100000.00"),
        "price_max": Decimal("250000.50"),
        "condition": "light",
        "strategy": "flip",
        "financing": "cash",
        "closing_preferences": {"days": 14},
        "other_criteria": {"no_hoa": True},
        "unknown_fields": ["financing_proof"],
        "raw_text": "SFR in 77009 under 250k, cash, light rehab\nActually min 100k.\n",
        "source_message_id": MSG_ID,
        "source_ref": f"message:{MSG_ID}",
        "change_note": "correction: price_max",
        "updated_at": T1,
    }
    values.update(overrides)
    return BuyBox(**values)  # type: ignore[arg-type]


GOLDEN_BODY = f"""BEGIN_BUY_BOX
FORMAT_VERSION: 1
ROW_KEY: {INVESTOR_ID}|default
INVESTOR_ID: {INVESTOR_ID}
INVESTOR_NAME: Acme Holdings LLC
BUY_BOX_ID: {BB_ID}
BUY_BOX_KEY: default
VERSION: 2
STATUS: current
SOURCE_MESSAGE_ID: {MSG_ID}
SOURCE_REF: message:{MSG_ID}
MARKETS: Houston
CITIES: Houston; Pasadena
ZIPS: 77009
NEIGHBORHOODS:
ASSET_TYPES: sfr; duplex
PRICE_MIN: 100000
PRICE_MAX: 250000.50
CONDITION: light
STRATEGY: flip
FINANCING: cash
CLOSING_PREFERENCES: {{"days":14}}
OTHER_CRITERIA: {{"no_hoa":true}}
UNKNOWN_FIELDS: financing_proof
CHANGE_NOTE: correction: price_max
UPDATED_AT: 2026-10-24T15:45:00Z
---
RAW_TEXT:
SFR in 77009 under 250k, cash, light rehab
Actually min 100k.
END_BUY_BOX
"""


def test_golden_string() -> None:
    email = A6ParserFormatter().format(buy_box(), investor())
    assert isinstance(email, ParserEmail)
    assert email.body == GOLDEN_BODY
    assert email.subject == f"[BUYBOX] {INVESTOR_ID} default v2"
    assert email.buy_box_id == str(BB_ID) and email.version == 2


def test_format_is_deterministic_and_versioned() -> None:
    f = A6ParserFormatter()
    assert f.format(buy_box(), investor()) == f.format(buy_box(), investor())
    assert f.agent_id == AgentId.A6
    assert PARSER_FORMAT_VERSION == "1" and "FORMAT_VERSION: 1" in GOLDEN_BODY


def test_every_structured_field_present_in_stable_order_with_delimiters() -> None:
    body = A6ParserFormatter().format(buy_box(), investor()).body
    lines = body.splitlines()
    assert lines[0] == BEGIN_MARKER and lines[-1] == END_MARKER
    labels = [line.split(":")[0] for line in lines[1 : 1 + len(STRUCTURED_LABELS)]]
    assert tuple(labels) == STRUCTURED_LABELS
    assert lines[1 + len(STRUCTURED_LABELS)] == SEPARATOR
    assert lines[2 + len(STRUCTURED_LABELS)] == "RAW_TEXT:"
    assert all(
        line.startswith(f"{label}: ") or line == f"{label}:"
        for label, line in zip(labels, lines[1:], strict=False)
    )
    for required in ("ROW_KEY", "INVESTOR_ID", "BUY_BOX_ID", "VERSION", "SOURCE_MESSAGE_ID"):
        assert required in labels


def test_row_key_is_one_per_logical_buy_box_across_versions() -> None:
    f = A6ParserFormatter()
    v2 = f.fields(buy_box(), investor())
    v3 = f.fields(buy_box(id=uuid.uuid4(), version=3), investor())
    assert v2["ROW_KEY"] == v3["ROW_KEY"] == parser_row_key(INVESTOR_ID, "default")
    assert v2["BUY_BOX_ID"] != v3["BUY_BOX_ID"]
    other = f.fields(buy_box(id=uuid.uuid4(), buy_box_key="multifamily"), investor())
    assert other["ROW_KEY"] == f"{INVESTOR_ID}|multifamily"


def test_raw_text_is_last_and_keeps_newlines_while_other_values_are_one_line() -> None:
    body = A6ParserFormatter().format(buy_box(change_note="line1\nline2  spaced"), investor()).body
    assert "CHANGE_NOTE: line1 line2 spaced\n" in body
    assert "INVESTOR_NAME: Acme Holdings LLC\n" in body
    assert body.index("RAW_TEXT:") > body.index("UPDATED_AT:")
    assert body.endswith("Actually min 100k.\nEND_BUY_BOX\n")


def test_unknowns_and_empty_fields_render_as_empty_values_not_invented() -> None:
    bare = buy_box(
        geography={},
        asset_types=[],
        price_min=None,
        price_max=None,
        condition=None,
        strategy=None,
        financing=None,
        closing_preferences={},
        other_criteria={},
        unknown_fields=["price_min", "price_max"],
        raw_text=None,
        source_message_id=None,
        source_ref=None,
        change_note=None,
        is_active=False,
    )
    fields = A6ParserFormatter().fields(bare, investor())
    assert fields["PRICE_MIN"] == "" and fields["PRICE_MAX"] == ""
    assert fields["MARKETS"] == "" and fields["CLOSING_PREFERENCES"] == ""
    assert fields["UNKNOWN_FIELDS"] == "price_min; price_max"
    assert fields["STATUS"] == "inactive"
    assert fields["SOURCE_MESSAGE_ID"] == ""
    body = A6ParserFormatter().format(bare, investor()).body
    assert "RAW_TEXT:\n\nEND_BUY_BOX" in body


def test_idempotency_key_is_per_buy_box_version() -> None:
    email_v2 = A6ParserFormatter().format(buy_box(), investor())
    email_v3 = A6ParserFormatter().format(buy_box(version=3), investor())
    assert email_v2.idempotency_key == parser_idempotency_key(str(BB_ID), 2)
    assert email_v2.idempotency_key == f"zapier-parser:{BB_ID}:v2"
    assert email_v2.idempotency_key != email_v3.idempotency_key
