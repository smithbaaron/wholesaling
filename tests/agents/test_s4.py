"""S4 Seller Intake: schemas, form parsing, golden set, draft post-processing, reply assembly
(REQUIREMENTS.md SEL-06, SEL-07, POL-09, XC-07)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.a4 import ThreadMessage
from wholesaling.agents.llm.mock import MockModelProvider, placeholder_for_schema
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.prompts import s4 as prompts
from wholesaling.agents.s4 import (
    CLASSIFY_SCHEMA,
    DRAFT_SCHEMA,
    EXTRACT_SCHEMA,
    MAX_ASKS_PER_MESSAGE,
    S4IntakeAgent,
    SellerClassification,
    assemble_reply_body,
    choose_asks,
    contains_sensitive_content,
    fields_from_parsed,
    parse_form,
    photo_upload_link,
)
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.agents.templates.seller_outreach import missing_compliance_blocks
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import (
    AgentId,
    ConditionClass,
    IntakeTimeline,
    MessageClassification,
)
from wholesaling.db.models.ops import AgentRun
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.repositories.sellers import SellerRepository, missing_required, unasked_optional

GOLDEN = Path(__file__).parent / "golden" / "seller_intake.json"
SENDER = SenderIdentity(
    name="Aaron",
    company="NorthStack",
    postal_address="123 Main St, Houston, TX 77002",
    opt_out_text=(
        'If you would rather not hear from us, reply with "unsubscribe" and we will remove you.'
    ),
)


def load_golden() -> list[dict[str, Any]]:
    doc = json.loads(GOLDEN.read_text())
    assert doc["_meta"]["version"] >= 1
    cases: list[dict[str, Any]] = doc["cases"]
    return cases


GOLDEN_CASES = load_golden()


def golden_thread(case: dict[str, Any]) -> list[ThreadMessage]:
    at = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    out: list[ThreadMessage] = [
        ThreadMessage(
            direction=h["direction"],
            body=h["body"],
            from_address="deals@example.test" if h["direction"] == "out" else case["from"],
            at=at,
        )
        for h in case["history"]
    ]
    out.append(
        ThreadMessage(
            direction="in",
            body=case["body"],
            from_address=case["from"],
            subject=case["subject"],
            headers=case.get("headers") or {},
            at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
            is_latest_inbound=True,
        )
    )
    return out


@pytest.fixture
def provider() -> MockModelProvider:
    return MockModelProvider()


@pytest.fixture
def runner(provider: MockModelProvider, settings: Settings) -> AgentRunner:
    return AgentRunner(provider, ModelRouter.from_settings(settings))


@pytest.fixture
def agent(runner: AgentRunner, session: AsyncSession) -> S4IntakeAgent:
    return S4IntakeAgent(
        runner, session, correlation_id="test", prompt_values={"company": "NorthStack"}
    )


# -- schemas and deterministic helpers -------------------------------------------------------------


def test_extract_schema_invents_nothing_when_unscripted() -> None:
    parsed = placeholder_for_schema(EXTRACT_SCHEMA)
    fields = fields_from_parsed(parsed)
    assert fields.motivation is None and fields.condition_class is None and fields.timeline is None
    assert (
        fields.asking_price is None and fields.disclosed_issues == [] and fields.seller_unsure == []
    )
    assert set(EXTRACT_SCHEMA["required"]) == set(EXTRACT_SCHEMA["properties"])


def test_classify_schema_lists_the_four_classes() -> None:
    assert CLASSIFY_SCHEMA["properties"]["classification"]["enum"] == list(prompts.CLASSES)
    assert {c.value for c in SellerClassification} == set(prompts.CLASSES)
    assert SellerClassification.REPLY.message_classification is MessageClassification.INTERESTED
    assert SellerClassification.OPT_OUT.message_classification is MessageClassification.OPT_OUT


def test_fields_from_parsed_validates_enums_and_prices() -> None:
    f = fields_from_parsed(
        {
            "condition_class": "heavy",
            "timeline": "ASAP",
            "asking_price": "185k",
            "seller_unsure": ["liens", "price"],
        }
    )
    assert f.condition_class is ConditionClass.HEAVY and f.timeline is IntakeTimeline.ASAP
    assert f.asking_price == Decimal(185000) and f.seller_unsure == ["liens"]
    bad = fields_from_parsed(
        {"condition_class": "Ruined", "timeline": "unknown", "asking_price": -5}
    )
    assert bad.condition_class is None and bad.timeline is None and bad.asking_price is None


def test_parse_form_maps_fields_without_a_model() -> None:
    f = parse_form(
        {
            "address_line1": "1420 Pecan Grove Dr",
            "city": "Houston",
            "state": "TX",
            "postal_code": "77009",
            "motivation": "Relocating",
            "condition_class": "Medium",
            "disclosed_issues": "roof\nAC",
            "timeline": "within_30_days",
            "asking_price": "185,000",
            "photos": ["sell/x/01.jpg"],
        }
    )
    assert f.property_address == "1420 Pecan Grove Dr, Houston, TX 77009"
    assert f.condition_class is ConditionClass.MEDIUM and f.disclosed_issues == ["roof", "AC"]
    assert f.timeline is IntakeTimeline.WITHIN_30_DAYS and f.asking_price == Decimal(185000)
    assert f.photos_mentioned is True
    empty = parse_form({"address_line1": "1 A St"})
    assert (
        empty.property_address == "1 A St"
        and empty.motivation is None
        and not empty.photos_mentioned
    )


def test_choose_asks_limits_to_two_required_first() -> None:
    assert choose_asks(["motivation", "condition", "timeline"], ["liens"]) == [
        "motivation",
        "condition",
    ]
    assert choose_asks(["timeline"], ["co_owners", "liens"]) == ["timeline", "co_owners"]
    assert choose_asks([], ["co_owners", "liens", "hoa"]) == ["co_owners", "liens"]
    assert len(choose_asks(["a", "b", "c"], ["d"])) == MAX_ASKS_PER_MESSAGE == 2


@pytest.mark.parametrize(
    ("text", "sensitive"),
    [
        ("Once I have those details I can put together a written offer.", False),
        ("We could offer around $150,000.", True),
        ("Probably 150k to 160k.", True),
        ("Our attorney will send the contract.", True),
        ("The house is worth more than that.", True),
        ("Thanks for the photos, how soon would you want to close?", False),
    ],
)
def test_seller_sensitivity_flags_numbers_and_legal_terms_not_the_word_offer(
    text: str, sensitive: bool
) -> None:
    assert contains_sensitive_content(text) is sensitive


def test_assemble_reply_body_adds_link_and_compliance_block() -> None:
    link = photo_upload_link("https://sell.example.com/", "abc")
    assert link == "https://sell.example.com/sell?lead=abc"
    body = assemble_reply_body(
        "Thanks for the details.", sender=SENDER, photo_link=link, first_name="Dolores"
    )
    assert body.startswith("Hi Dolores,\n\nThanks for the details.")
    assert link in body and missing_compliance_blocks(body, SENDER) == []
    no_link = assemble_reply_body(
        "Hi Sam, thanks.", sender=SENDER, photo_link=None, first_name="Sam"
    )
    assert "upload" not in no_link and no_link.startswith("Hi Sam, thanks.")


def test_prompts_carry_the_rules() -> None:
    system = S4IntakeAgent.SYSTEM_PROMPT.render(company="NorthStack")
    assert "NorthStack" in system and "Never invent" in system
    assert "Never state, estimate or hint at an offer amount" in prompts.DRAFT_INSTRUCTIONS
    assert "at most two" in prompts.DRAFT_INSTRUCTIONS
    assert "seller_unsure" in prompts.EXTRACT_INSTRUCTIONS


# -- golden set (SEL-06 / SEL-07) ------------------------------------------------------------------


def _script(provider: MockModelProvider, case: dict[str, Any]) -> None:
    model = case["model"]
    if model.get("classify"):
        provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=model["classify"])
    if model.get("extract"):
        provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=model["extract"])


async def _lead_and_intake(session: AsyncSession, verified: bool) -> tuple[SellerLead, Intake]:
    lead = SellerLead(
        owner_name="Dolores Ramirez",
        property_address_line1="1420 Pecan Grove Dr",
        property_city="Houston",
        property_state="TX",
        property_postal_code="77009",
        parcel_id="HCAD-D-0000001" if verified else None,
        parcel_verified=verified,
    )
    session.add(lead)
    await session.flush()
    intake = await SellerRepository(session).get_or_create_intake(lead)
    return lead, intake


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c["id"])
async def test_golden_classification(
    case: dict[str, Any], agent: S4IntakeAgent, provider: MockModelProvider, session: AsyncSession
) -> None:
    _script(provider, case)
    expected = case["expected"]
    result = await agent.classify(golden_thread(case))
    assert result.classification.value == expected["classification"]
    assert result.is_auto_reply is expected["is_auto_reply"]
    assert result.asks_offer is expected["asks_offer"]
    assert result.quoted_text_stripped is expected["quoted_text_stripped"]
    classify_calls = [r for r in provider.requests if r.metadata.task_kind is TaskKind.CLASSIFY]
    assert bool(classify_calls) is expected["model_called"]
    runs = (await session.execute(select(func.count()).select_from(AgentRun))).scalar_one()
    assert runs == (1 if expected["model_called"] else 0)
    if classify_calls:
        prompt = classify_calls[0].messages[-1].content
        if expected.get("stripped_text_contains"):
            assert expected["stripped_text_contains"] in prompt
        if expected.get("stripped_text_excludes"):
            latest = prompt.split("LATEST INBOUND MESSAGE")[-1]
            assert expected["stripped_text_excludes"] not in latest


EXTRACT_CASES = [c for c in GOLDEN_CASES if c["expected"].get("extract_called")]


@pytest.mark.parametrize("case", EXTRACT_CASES, ids=lambda c: c["id"])
async def test_golden_extraction_and_missing_fields(
    case: dict[str, Any], agent: S4IntakeAgent, provider: MockModelProvider, session: AsyncSession
) -> None:
    _script(provider, case)
    expected = case["expected"]
    lead, intake = await _lead_and_intake(session, bool(case["known"].get("parcel_verified", True)))
    extraction = await agent.extract(
        golden_thread(case), {"property_address": "1420 Pecan Grove Dr"}
    )
    got = extraction.fields.to_json()
    for name, value in expected["fields"].items():
        assert got[name] == value, name
    # Everything the seller did not state stays null (never invented).
    for name in (
        "motivation",
        "condition_class",
        "timeline",
        "asking_price",
        "co_owners",
        "occupancy",
        "liens",
        "hoa",
    ):
        if name not in expected["fields"]:
            assert got[name] is None, name
    repo = SellerRepository(session)
    await repo.merge_intake(intake, extraction.fields)
    if extraction.fields.property_address and not lead.parcel_verified:
        pass  # the activity resolves the parcel; here the address stays unverified
    assert missing_required(lead, intake) == expected["missing_required"]
    assert (intake.property_facts or {}).get("seller_unsure", []) == expected["seller_unsure"]
    for name in ("co_owners", "occupancy", "liens", "hoa"):
        stated = name in expected["fields"]
        assert getattr(intake, f"{name}_unknown") is (not stated), name
    unasked = unasked_optional(intake)
    for name in expected["seller_unsure"]:
        assert name not in unasked


def test_golden_set_covers_the_required_scenarios() -> None:
    ids = {c["id"] for c in GOLDEN_CASES}
    assert len(GOLDEN_CASES) >= 16
    for required in (
        "full_info",
        "partial_motivation_only",
        "whats_your_offer",
        "hostile",
        "asking_price_only",
        "photos_attached_mention",
        "co_owner_mention",
        "probate",
        "tenant_occupied",
        "unknown_liens",
        "opt_out",
        "auto_reply",
        "address_typo",
        "sms_style_text",
    ):
        assert required in ids, required
    assert {c["expected"]["classification"] for c in GOLDEN_CASES} == set(prompts.CLASSES)


# -- draft post-processing -------------------------------------------------------------------------


async def test_draft_reply_clamps_asks_and_flags_sensitive(
    agent: S4IntakeAgent, provider: MockModelProvider
) -> None:
    provider.script(
        AgentId.S4,
        TaskKind.DRAFT,
        parsed={
            "subject": "Question about 1420 Pecan Grove Dr",
            "body": (
                "Thanks Dolores. Could you tell me about the condition and when you would want "
                "to close? Also any liens?"
            ),
            "asks_for": ["condition", "timeline", "liens", "hoa"],
            "contains_sensitive": False,
        },
    )
    draft = await agent.draft_reply(
        golden_thread(GOLDEN_CASES[1]),
        known={"motivation": "taxes"},
        asks=["condition", "timeline"],
        photo_link="https://x/sell?lead=1",
    )
    assert draft.asks_for == ["condition", "timeline"]
    assert draft.subject == "RE: Question about 1420 Pecan Grove Dr"
    assert draft.contains_sensitive is False
    request = next(r for r in provider.requests if r.metadata.task_kind is TaskKind.DRAFT)
    assert "ASK FOR (at most 2)" in request.messages[-1].content
    assert "will be appended" in request.messages[-1].content
    assert request.json_schema == DRAFT_SCHEMA


async def test_draft_reply_marks_price_talk_sensitive(
    agent: S4IntakeAgent, provider: MockModelProvider
) -> None:
    provider.script(
        AgentId.S4,
        TaskKind.DRAFT,
        parsed={
            "subject": "RE: x",
            "body": "We could probably do around $150,000.",
            "asks_for": [],
            "contains_sensitive": False,
        },
    )
    draft = await agent.draft_reply(
        golden_thread(GOLDEN_CASES[2]), known={}, asks=["motivation"], photo_link=None
    )
    assert draft.contains_sensitive is True
    asked = await agent.draft_reply(
        golden_thread(GOLDEN_CASES[2]),
        known={},
        asks=["motivation"],
        photo_link=None,
        asks_offer=True,
    )
    assert asked.contains_sensitive is True


async def test_routing_classify_and_extract_on_haiku_draft_on_sonnet(
    agent: S4IntakeAgent, provider: MockModelProvider, settings: Settings
) -> None:
    case = GOLDEN_CASES[0]
    _script(provider, case)
    provider.script(
        AgentId.S4,
        TaskKind.DRAFT,
        parsed={"subject": "s", "body": "b", "asks_for": [], "contains_sensitive": False},
    )
    await agent.classify(golden_thread(case))
    await agent.extract(golden_thread(case))
    await agent.draft_reply(golden_thread(case), known={}, asks=[], photo_link=None)
    models = {r.metadata.task_kind: r.model for r in provider.requests}
    assert models[TaskKind.CLASSIFY] == settings.model_classify
    assert models[TaskKind.EXTRACT] == settings.model_extract
    assert models[TaskKind.DRAFT] == settings.model_draft
    assert settings.model_classify != settings.model_draft
