"""A4 Reply & Buy Box: quote stripper, auto-reply detection, schemas, golden set (INV-22),
prompt content (INV-14 no-invention, thread context), routing override (section 5)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents import a4
from wholesaling.agents.a4 import (
    CLASSIFY_SCHEMA,
    DRAFT_SCHEMA,
    EXTRACT_SCHEMA,
    MODEL_OVERRIDES,
    A4Classification,
    A4ReplyAgent,
    Correction,
    ThreadMessage,
    compute_corrections,
    contains_sensitive_content,
    detect_auto_reply,
    render_thread,
    strip_quoted_text,
    tracked_unknowns,
)
from wholesaling.agents.llm.mock import MockModelProvider, placeholder_for_schema
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.prompts import a4 as prompts
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId, MessageClassification
from wholesaling.db.models.ops import AgentRun
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria

GOLDEN = Path(__file__).parent / "golden" / "inbound_replies.json"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def load_golden() -> list[dict[str, Any]]:
    data = json.loads(GOLDEN.read_text())
    cases: list[dict[str, Any]] = data["cases"]
    return cases


GOLDEN_CASES = load_golden()


def golden_thread(case: dict[str, Any]) -> list[ThreadMessage]:
    out: list[ThreadMessage] = []
    for i, h in enumerate(case.get("history", [])):
        out.append(
            ThreadMessage(
                direction=h["direction"],
                body=h["body"],
                from_address="deals@example.test" if h["direction"] == "out" else "jane@acme.com",
                subject=case["subject"],
                at=NOW.replace(day=20 + i),
            )
        )
    out.append(
        ThreadMessage(
            direction="in",
            body=case["body"],
            from_address="jane@acme.com",
            subject=case["subject"],
            at=NOW,
            headers=case.get("headers", {}),
            is_latest_inbound=True,
        )
    )
    return out


def criteria_from_golden(doc: dict[str, Any]) -> BuyBoxCriteria:
    return BuyBoxCriteria(
        geography=doc.get("geography"),
        asset_types=doc.get("asset_types"),
        price_min=Decimal(doc["price_min"]) if doc.get("price_min") else None,
        price_max=Decimal(doc["price_max"]) if doc.get("price_max") else None,
        condition=doc.get("condition"),
        strategy=doc.get("strategy"),
        financing=doc.get("financing"),
        closing_preferences=doc.get("closing_preferences"),
        other_criteria=doc.get("other_criteria"),
    )


@pytest.fixture
def provider() -> MockModelProvider:
    return MockModelProvider()


@pytest.fixture
def runner(provider: MockModelProvider, settings: Settings) -> AgentRunner:
    return AgentRunner(provider, ModelRouter.from_settings(settings, MODEL_OVERRIDES))


@pytest.fixture
def agent(runner: AgentRunner, session: AsyncSession) -> A4ReplyAgent:
    return A4ReplyAgent(runner, session, correlation_id="test", prompt_values={"company": "Acme"})


# -- quote stripper ----------------------------------------------------------------------------


def test_strip_outlook_original_message_marker() -> None:
    body = "Yes.\n\n-----Original Message-----\nFrom: a@b.c\nSent: Monday\nTo: x\nSubject: y\n\nold"
    r = strip_quoted_text(body)
    assert r.stripped and r.text == "Yes." and r.marker == "original_message"


def test_strip_outlook_header_block_without_dashes() -> None:
    body = "Reply here\n\nFrom: Deals <deals@example.test>\nSent: Mon\nTo: Jane\nSubject: Hi\n\nold"
    r = strip_quoted_text(body)
    assert r.stripped and r.text == "Reply here" and r.marker == "outlook_header"


def test_strip_outlook_underscore_separator() -> None:
    body = "Top\n________________________________\nFrom: x@y.z\nSent: today\n\nold body"
    r = strip_quoted_text(body)
    assert r.stripped and r.text == "Top" and r.marker == "outlook_separator"


def test_from_line_alone_is_not_a_quote_marker() -> None:
    # A sentence starting with "From:" inside the reply must survive without a Sent:/Date: line.
    body = "From: my side, the deal looks fine.\nLet's talk."
    r = strip_quoted_text(body)
    assert not r.stripped and r.text == body


def test_strip_gmail_wrote_single_line() -> None:
    body = "Sure.\n\nOn Mon, Oct 20, 2026 at 9:00 AM Deals <deals@example.test> wrote:\n> hi"
    r = strip_quoted_text(body)
    assert r.stripped and r.text == "Sure." and r.marker == "gmail_wrote"


def test_strip_gmail_wrote_wrapped_over_two_lines() -> None:
    body = (
        "Sure.\n\nOn Mon, Oct 20, 2026 at 9:00 AM Deals Team <deals@example.test>\nwrote:\n\n> hi"
    )
    r = strip_quoted_text(body)
    assert r.stripped and r.text == "Sure."


def test_on_sentence_without_wrote_is_kept() -> None:
    body = "On balance we like the north side.\nSend deals."
    r = strip_quoted_text(body)
    assert not r.stripped and "north side" in r.text


def test_strip_angle_bracket_quotes() -> None:
    r = strip_quoted_text("Yes please.\n> old line one\n> old line two")
    assert r.stripped and r.text == "Yes please." and r.marker == "angle_quote"


def test_strip_never_returns_empty_when_reply_is_only_quote() -> None:
    body = "> only quoted text\n> nothing new"
    r = strip_quoted_text(body)
    assert not r.stripped and r.text == body


def test_strip_normalizes_crlf_and_keeps_signature() -> None:
    body = "Yes.\r\n\r\nBest,\r\nJane\r\nSent from my iPhone\r\n\r\n> quoted"
    r = strip_quoted_text(body)
    assert r.stripped and r.text == "Yes.\n\nBest,\nJane\nSent from my iPhone"


def test_strip_no_markers_is_identity_modulo_whitespace() -> None:
    r = strip_quoted_text("  Plain reply.  \n")
    assert not r.stripped and r.text == "Plain reply." and r.marker is None


# -- auto-reply detection ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "headers", "expected"),
    [
        ("Automatic reply: Deals", "Hi", {}, True),
        ("Out of Office: Deals", "Hi", {}, True),
        ("RE: Deals", "I am out of the office until Monday.", {}, True),
        ("RE: Deals", "Yes, send them.", {"auto-submitted": "auto-replied"}, True),
        ("RE: Deals", "Yes, send them.", {"X-Auto-Response-Suppress": "All"}, True),
        ("RE: Deals", "Yes, send them.", {"auto-submitted": "no"}, False),
        ("RE: Deals", "We buy SFR up to 200k.", {}, False),
        ("RE: Deals", "Our office is at 1 Main St.", {}, False),
    ],
)
def test_detect_auto_reply(
    subject: str, body: str, headers: dict[str, str], expected: bool
) -> None:
    assert detect_auto_reply(subject=subject, body=body, headers=headers) is expected


# -- schemas -----------------------------------------------------------------------------------


def test_classify_schema_offers_exactly_the_inv13_classes() -> None:
    assert CLASSIFY_SCHEMA["properties"]["classification"]["enum"] == [
        c.value for c in MessageClassification
    ]
    assert CLASSIFY_SCHEMA["additionalProperties"] is False


def test_extract_schema_has_no_defaults_and_nullable_first() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert "default" not in node, "schema must not supply defaults (INV-14)"
            if "anyOf" in node:
                assert node["anyOf"][0] == {"type": "null"}, "null must be the first option"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(EXTRACT_SCHEMA)
    walk(DRAFT_SCHEMA)


def test_unscripted_mock_invents_no_criteria() -> None:
    doc = placeholder_for_schema(EXTRACT_SCHEMA)
    assert doc == {"buy_boxes": [], "rationale": "mock"}
    item = placeholder_for_schema(a4.BUY_BOX_ITEM_SCHEMA)
    for key in ("geography", "asset_types", "price_min", "price_max", "condition", "financing"):
        assert item[key] is None


# -- routing override --------------------------------------------------------------------------


def test_a4_classify_routes_to_sonnet_via_override(settings: Settings) -> None:
    router = ModelRouter.from_settings(settings, MODEL_OVERRIDES)
    assert router.route(AgentId.A4, TaskKind.CLASSIFY) == "claude-sonnet-5-5"
    assert router.route(AgentId.A4, TaskKind.EXTRACT) == settings.model_extract
    assert router.route(AgentId.A4, TaskKind.DRAFT) == settings.model_draft
    assert router.route(AgentId.A5, TaskKind.CLASSIFY) == settings.model_classify


# -- golden set: classification ----------------------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c["id"])
async def test_golden_classification(
    case: dict[str, Any], agent: A4ReplyAgent, provider: MockModelProvider, session: AsyncSession
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=case["model"])
    result = await agent.classify(golden_thread(case))
    expected = case["expected"]

    assert result.classification.value == expected["classification"]
    assert result.is_auto_reply is expected.get("is_auto_reply", False)
    if "quoted_text_stripped" in expected:
        assert result.quoted_text_stripped is expected["quoted_text_stripped"]
    if "reconnect_date" in expected:
        want = expected["reconnect_date"]
        assert result.reconnect_date == (date.fromisoformat(want) if want else None)

    if expected["model_called"]:
        assert len(provider.requests) == 1 and result.run_id is not None
        request = provider.requests[0]
        assert request.model == "claude-sonnet-5-5"
        sent = request.messages[-1].content
        if "stripped_text_contains" in expected:
            assert expected["stripped_text_contains"] in sent
        if "stripped_text_excludes" in expected:
            latest_part = sent.split("LATEST INBOUND MESSAGE", 1)[1]
            assert expected["stripped_text_excludes"] not in latest_part
        runs = (await session.execute(select(func.count()).select_from(AgentRun))).scalar_one()
        assert runs == 1
    else:
        assert provider.requests == [] and result.run_id is None
        assert result.confidence == 1.0


def test_golden_set_covers_every_inv22_scenario_and_every_class() -> None:
    ids = {c["id"] for c in GOLDEN_CASES}
    assert len(GOLDEN_CASES) >= 24
    for needle in (
        "interested",
        "buy_box_two_in_one_email",
        "question",
        "not_now_with_date",
        "not_interested",
        "opt_out_unsubscribe",
        "opt_out_stop_emailing",
        "opt_out_remove_me",
        "sarcasm_love_spam",
        "correction_price_max",
        "quoted_outlook_original_message",
        "quoted_gmail_wrote",
        "out_of_office_subject",
        "ambiguous_one_word",
    ):
        assert any(needle in i for i in ids), needle
    classes = {c["expected"]["classification"] for c in GOLDEN_CASES}
    assert classes == {c.value for c in MessageClassification}


# -- golden set: extraction (unknowns, corrections, multiple boxes) -------------------------------


EXTRACTION_CASES = [c for c in GOLDEN_CASES if "extraction" in c]


@pytest.mark.parametrize("case", EXTRACTION_CASES, ids=lambda c: c["id"])
async def test_golden_extraction(
    case: dict[str, Any], agent: A4ReplyAgent, provider: MockModelProvider
) -> None:
    spec = case["extraction"]
    provider.script(AgentId.A4, TaskKind.EXTRACT, parsed=spec["model"])
    current = {k: criteria_from_golden(v) for k, v in spec["current"].items()}
    result = await agent.extract_buy_box(golden_thread(case), current)

    expected_boxes = spec["expected"]["buy_boxes"]
    assert [b.buy_box_key for b in result.buy_boxes] == [e["buy_box_key"] for e in expected_boxes]
    for box, want in zip(result.buy_boxes, expected_boxes, strict=True):
        if "price_max" in want:
            assert box.criteria.price_max == Decimal(want["price_max"])
        if "price_min" in want:
            assert box.criteria.price_min == Decimal(want["price_min"])
        if "asset_types" in want:
            assert box.criteria.asset_types == want["asset_types"]
        if "zips" in want:
            assert (box.criteria.geography or {}).get("zips") == want["zips"]
        if "financing" in want:
            assert box.criteria.financing == want["financing"]
        if "unknowns" in want:
            assert box.unknowns == want["unknowns"]
            assert box.criteria.unknown_fields == want["unknowns"]
        if "corrections" in want:
            assert [c.to_json() for c in box.corrections] == want["corrections"]
    assert provider.requests[-1].model == "claude-haiku-5-5"


async def test_extraction_merged_price_max_reflects_correction(
    agent: A4ReplyAgent, provider: MockModelProvider
) -> None:
    case = next(c for c in GOLDEN_CASES if c["id"] == "correction_price_max")
    spec = case["extraction"]
    provider.script(AgentId.A4, TaskKind.EXTRACT, parsed=spec["model"])
    current = {k: criteria_from_golden(v) for k, v in spec["current"].items()}
    result = await agent.extract_buy_box(golden_thread(case), current)
    # The stated (partial) criteria only carry the corrected field; the merge is the repo's job.
    assert result.buy_boxes[0].criteria.asset_types is None
    assert result.corrections[0].field == "price_max"
    assert result.unknowns == ["condition", "closing_timeline"]


async def test_extraction_drops_empty_boxes_and_accepts_single_current(
    agent: A4ReplyAgent, provider: MockModelProvider
) -> None:
    empty = placeholder_for_schema(a4.BUY_BOX_ITEM_SCHEMA)
    provider.script(
        AgentId.A4,
        TaskKind.EXTRACT,
        parsed={"buy_boxes": [empty, {**empty, "buy_box_key": "x", "condition": "any"}], "r": ""},
    )
    thread = [ThreadMessage(direction="in", body="any condition", is_latest_inbound=True)]
    result = await agent.extract_buy_box(thread, BuyBoxCriteria(price_max=Decimal(100)))
    assert len(result.buy_boxes) == 1
    assert result.buy_boxes[0].buy_box_key == "x"
    assert result.buy_boxes[0].criteria.condition == "any"


def test_tracked_unknowns_and_compute_corrections() -> None:
    c = BuyBoxCriteria(price_max=Decimal(1), closing_preferences={"timeline": "30 days"})
    assert tracked_unknowns(c) == ["geography", "asset_types", "condition", "financing"]
    base = BuyBoxCriteria(price_max=Decimal(250000), asset_types=["sfr"], financing="cash")
    stated = BuyBoxCriteria(price_max=Decimal(150000), asset_types=["SFR"], condition="any")
    corrections = compute_corrections(base, stated)
    assert corrections == [Correction("price_max", Decimal(250000), Decimal(150000))]


# -- prompts -----------------------------------------------------------------------------------


def test_prompts_carry_thread_context_and_no_invention_rule(agent: A4ReplyAgent) -> None:
    thread = [
        ThreadMessage(direction="out", body="Are you buying in Houston?", at=NOW),
        ThreadMessage(
            direction="in", body="Yes, in 77009.\n> Are you buying", is_latest_inbound=True
        ),
    ]
    assert "Never invent criteria" in agent.system_prompt()
    assert "Acme" in agent.system_prompt()
    classify_text, stripped = agent.classify_prompt(thread)
    assert stripped.stripped and "Are you buying in Houston?" in classify_text
    assert "THREAD HISTORY" in classify_text and "LATEST INBOUND MESSAGE" in classify_text
    extract_text = agent.extract_prompt(thread, {"default": BuyBoxCriteria(price_max=Decimal(5))})
    assert "Never invent criteria" in extract_text and '"price_max": "5"' in extract_text
    assert "CURRENT BUY BOX(ES) ON RECORD" in extract_text
    classification = A4Classification(
        MessageClassification.NOT_NOW, 0.9, "", False, False, reconnect_date=date(2027, 1, 15)
    )
    draft_text = agent.draft_prompt(thread, classification, None, ["financing"], facts={"x": 1})
    assert "reconnect date 2027-01-15" in draft_text
    assert "MISSING FIELDS YOU MAY ASK ABOUT: financing" in draft_text
    assert "Never assert" in draft_text and "Are you buying in Houston?" in draft_text
    assert prompts.PROMPT_VERSION == A4ReplyAgent.PROMPT_VERSION


def test_render_thread_strips_quotes_from_inbound_history_only() -> None:
    thread = [
        ThreadMessage(direction="in", body="Older\n> quoted", at=NOW),
        ThreadMessage(direction="out", body="Ours > not a quote marker"),
        ThreadMessage(direction="in", body="latest", is_latest_inbound=True),
    ]
    text = render_thread(thread)
    assert "Older" in text and "quoted" not in text
    assert "Ours > not a quote marker" in text and "latest" not in text
    assert render_thread(thread, exclude_latest=False).endswith("latest")


# -- draft_reply -------------------------------------------------------------------------------


async def test_draft_reply_clamps_asks_and_flags_sensitive(
    agent: A4ReplyAgent, provider: MockModelProvider
) -> None:
    provider.script(
        AgentId.A4,
        TaskKind.DRAFT,
        parsed={
            "subject": "Off-market deals",
            "body": "Thanks Jane. What price range and which ZIPs do you target?",
            "asks_for": ["price_max", "geography", "asset_types"],
            "contains_sensitive": False,
        },
    )
    thread = [
        ThreadMessage(direction="in", body="Interested.", subject="Deals", is_latest_inbound=True)
    ]
    classification = A4Classification(MessageClassification.INTERESTED, 0.9, "", False, False)
    draft = await agent.draft_reply(thread, classification, None, ["price_max", "geography"])
    assert draft.subject == "RE: Off-market deals"
    assert draft.asks_for == ["price_max", "geography"], "never asks beyond the missing fields"
    assert draft.contains_sensitive is True, "'price range' is price talk"
    assert provider.requests[-1].model == "claude-sonnet-5-5"


async def test_draft_reply_without_price_talk_is_not_sensitive(
    agent: A4ReplyAgent, provider: MockModelProvider
) -> None:
    provider.script(
        AgentId.A4,
        TaskKind.DRAFT,
        parsed={
            "subject": "RE: Deals",
            "body": "Thanks Jane, which ZIP codes and property types do you focus on?",
            "asks_for": ["geography"],
            "contains_sensitive": False,
        },
    )
    thread = [
        ThreadMessage(direction="in", body="Interested.", subject="Deals", is_latest_inbound=True)
    ]
    classification = A4Classification(MessageClassification.INTERESTED, 0.9, "", False, False)
    draft = await agent.draft_reply(thread, classification, None, ["geography"])
    assert draft.contains_sensitive is False and draft.subject == "RE: Deals"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Our offer would be $150,000", True),
        ("we can go to 200k", True),
        ("the contract terms", True),
        ("talk to your attorney", True),
        ("Which ZIP codes do you like?", False),
        ("Thanks, I will send matches.", False),
    ],
)
def test_contains_sensitive_content(text: str, expected: bool) -> None:
    assert contains_sensitive_content(text) is expected


def test_classification_properties() -> None:
    auto = A4Classification(MessageClassification.AMBIGUOUS, 1.0, "", True, False)
    assert not auto.is_substantive and not auto.warrants_reply and not auto.may_contain_buy_box
    unrelated = A4Classification(MessageClassification.UNRELATED, 0.9, "", False, False)
    assert not unrelated.is_substantive and not unrelated.warrants_reply
    interested = A4Classification(MessageClassification.INTERESTED, 0.9, "", False, False)
    assert (
        interested.is_substantive and interested.warrants_reply and interested.may_contain_buy_box
    )
    opt_out = A4Classification(MessageClassification.OPT_OUT, 0.9, "", False, False)
    assert opt_out.is_substantive and not opt_out.warrants_reply
    assert interested.to_json()["classification"] == "interested"
