"""Zapier-side contract: the parser template mirrors A6's labels; the reference parser inverts
``format``; INT-22 acceptance in miniature (two emails -> one row key; replay -> same key)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from tests.agents.test_a6 import buy_box, investor
from wholesaling.agents.a6 import RAW_TEXT_LABEL, STRUCTURED_LABELS, A6ParserFormatter
from wholesaling.integrations.zapier import (
    REPLAY_KEY_LABELS,
    ROW_KEY_LABEL,
    parse_parser_email,
    parser_template,
)


def test_template_has_one_placeholder_per_label_in_a6_order() -> None:
    template = parser_template()
    lines = template.splitlines()
    assert lines[0] == "BEGIN_BUY_BOX" and lines[-1] == "END_BUY_BOX"
    for label in STRUCTURED_LABELS:
        assert f"{label}: {{{{{label.lower()}}}}}" in template
    assert "RAW_TEXT:\n{{raw_text}}\n" in template
    assert template.count("{{") == len(STRUCTURED_LABELS) + 1
    assert "---" in lines


def test_reference_parser_inverts_a6_format() -> None:
    bb, inv = buy_box(), investor()
    email = A6ParserFormatter().format(bb, inv)
    parsed = parse_parser_email(email.body)
    fields = A6ParserFormatter().fields(bb, inv)
    assert {k: v for k, v in parsed.fields.items() if k != RAW_TEXT_LABEL} == fields
    assert parsed.fields[RAW_TEXT_LABEL] == (bb.raw_text or "").strip()
    assert parsed.row_key == f"{inv.id}|default" == parsed.fields[ROW_KEY_LABEL]
    assert parsed.replay_key == (f"{inv.id}|default", "2")
    assert parsed.is_supported and parsed.format_version == "1"


def test_parser_ignores_text_outside_markers() -> None:
    email = A6ParserFormatter().format(buy_box(), investor())
    wrapped = "Forwarded by Outlook\n\n" + email.body + "\n-- \nsignature\nVERSION: 99\n"
    parsed = parse_parser_email(wrapped)
    assert parsed.fields["VERSION"] == "2"
    assert "signature" not in parsed.fields[RAW_TEXT_LABEL]


def test_int_22_two_emails_for_one_buy_box_resolve_to_the_same_row() -> None:
    inv = investor()
    # Two versions are two ``buy_boxes`` rows with different ids; the Zap keys on ROW_KEY.
    v1 = A6ParserFormatter().format(
        buy_box(id=uuid.uuid4(), version=1, price_max=Decimal("300000")), inv
    )
    v2 = A6ParserFormatter().format(buy_box(version=2), inv)
    p1, p2 = parse_parser_email(v1.body), parse_parser_email(v2.body)
    assert p1.fields["BUY_BOX_ID"] != p2.fields["BUY_BOX_ID"]
    assert p1.row_key == p2.row_key, "Zap looks up by row_key -> update branch, not create"
    assert p1.replay_key != p2.replay_key
    assert p1.fields["PRICE_MAX"] == "300000" and p2.fields["PRICE_MAX"] == "250000.50"


def test_int_22_replay_has_identical_replay_key_and_payload() -> None:
    email = A6ParserFormatter().format(buy_box(), investor())
    first, replay = parse_parser_email(email.body), parse_parser_email(email.body)
    assert first.replay_key == replay.replay_key
    assert first.fields == replay.fields
    assert REPLAY_KEY_LABELS == ("ROW_KEY", "VERSION")


def test_different_buy_boxes_never_share_a_row_key() -> None:
    inv = investor()
    a = A6ParserFormatter().format(buy_box(), inv)
    b = A6ParserFormatter().format(buy_box(id=uuid.uuid4(), buy_box_key="multifamily"), inv)
    assert parse_parser_email(a.body).row_key != parse_parser_email(b.body).row_key
