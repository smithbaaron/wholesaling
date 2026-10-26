"""A5 QA: every deterministic pre-check blocks without a model call, the model path parses
verdicts, and ``QaReviewRepository`` persists and links reviews (INV-17, INV-18, XC-07)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import MakeInvestor, MakeMessage, make_investor, make_message
from wholesaling.agents.a4 import ThreadMessage
from wholesaling.agents.a5 import (
    FLAG_CADENCE,
    FLAG_OPT_OUT,
    FLAG_PROHIBITED_PREFIX,
    FLAG_REPEATED_REQUEST,
    FLAG_WRONG_RECIPIENT,
    QA_SCHEMA,
    A5QaAgent,
    QaDraft,
    QaInput,
    prohibited_claim_flags,
    run_prechecks,
)
from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.prompts import a5 as prompts
from wholesaling.config.settings import Settings
from wholesaling.db.models import Message
from wholesaling.db.models.enums import AgentId, QaVerdict
from wholesaling.db.models.ops import AgentRun, QaReview
from wholesaling.db.repositories.qa_reviews import QaReviewRepository

__all__ = ["make_investor", "make_message"]

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
PARTICIPANT = "jane@acme.com"
THREAD = [
    ThreadMessage(direction="out", body="Are you buying in Houston?", at=NOW - timedelta(days=3)),
    ThreadMessage(
        direction="in", body="Yes, SFR in 77009.", from_address=PARTICIPANT, is_latest_inbound=True
    ),
]


def clean_draft(**overrides: object) -> QaDraft:
    values: dict[str, object] = {
        "to": [PARTICIPANT],
        "subject": "RE: Deals",
        "body": "Thanks Jane. Which property types and what condition do you take on?",
        "asks_for": ["asset_types", "condition"],
    }
    values.update(overrides)
    return QaDraft(**values)  # type: ignore[arg-type]


def qa_input(draft: QaDraft | None = None, **overrides: object) -> QaInput:
    values: dict[str, object] = {
        "draft": draft or clean_draft(),
        "thread": THREAD,
        "facts": {"investor_name": "Acme Holdings LLC"},
        "thread_participant": PARTICIPANT,
        "now": NOW,
    }
    values.update(overrides)
    return QaInput(**values)  # type: ignore[arg-type]


@pytest.fixture
def provider() -> MockModelProvider:
    return MockModelProvider()


@pytest.fixture
def agent(provider: MockModelProvider, settings: Settings, session: AsyncSession) -> A5QaAgent:
    runner = AgentRunner(provider, ModelRouter.from_settings(settings))
    return A5QaAgent(runner, session, correlation_id="test", prompt_values={"company": "Acme"})


# -- pre-checks (pure) -------------------------------------------------------------------------


def test_clean_draft_passes_every_precheck() -> None:
    assert run_prechecks(qa_input()) == []


@pytest.mark.parametrize(
    ("body", "term"),
    [
        ("We have inventory that fits.", "inventory"),
        ("Two houses already under contract for you.", "under_contract"),
        ("You get exclusive access.", "exclusive"),
        ("Returns are guaranteed.", "guaranteed"),
        ("Here are the deals we have.", "properties_available"),
    ],
)
def test_prohibited_claims_denylist(body: str, term: str) -> None:
    flags = run_prechecks(qa_input(clean_draft(body=body)))
    assert flags == [f"{FLAG_PROHIBITED_PREFIX}{term}"]
    assert prohibited_claim_flags(body) == flags


def test_prohibited_claim_in_subject_counts_too() -> None:
    flags = run_prechecks(qa_input(clean_draft(subject="Exclusive inventory for you")))
    assert set(flags) == {
        f"{FLAG_PROHIBITED_PREFIX}inventory",
        f"{FLAG_PROHIBITED_PREFIX}exclusive",
    }


def test_opt_out_violation_flag() -> None:
    assert run_prechecks(qa_input(recipient_suppressed=True)) == [FLAG_OPT_OUT]


def test_wrong_recipient_flag() -> None:
    assert run_prechecks(qa_input(clean_draft(to=["bob@other.com"]))) == [FLAG_WRONG_RECIPIENT]
    assert run_prechecks(qa_input(clean_draft(to=[]))) == [FLAG_WRONG_RECIPIENT]
    # Same mailbox spelled differently is fine (normalised comparison).
    assert run_prechecks(qa_input(clean_draft(to=["Jane@ACME.com"]))) == []
    # No known participant: nothing to compare against.
    assert run_prechecks(qa_input(thread_participant=None)) == []


def test_repeated_request_flag() -> None:
    flags = run_prechecks(qa_input(last_outbound_asks_for=["condition", "asset_types"]))
    assert flags == [FLAG_REPEATED_REQUEST]
    assert run_prechecks(qa_input(last_outbound_asks_for=["financing"])) == []
    # A draft that asks nothing can never be a repeated request.
    assert run_prechecks(qa_input(clean_draft(asks_for=[]), last_outbound_asks_for=[])) == []


def test_cadence_flag_within_24h_only() -> None:
    assert run_prechecks(qa_input(last_outbound_at=NOW - timedelta(hours=2))) == [FLAG_CADENCE]
    assert run_prechecks(qa_input(last_outbound_at=NOW - timedelta(hours=25))) == []
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert run_prechecks(qa_input(last_outbound_at=naive)) == [FLAG_CADENCE]


def test_multiple_failures_all_reported() -> None:
    flags = run_prechecks(
        qa_input(
            clean_draft(body="Guaranteed inventory.", to=["x@y.z"]),
            recipient_suppressed=True,
            last_outbound_at=NOW,
        )
    )
    assert {FLAG_OPT_OUT, FLAG_WRONG_RECIPIENT, FLAG_CADENCE} <= set(flags)
    assert any(f.startswith(FLAG_PROHIBITED_PREFIX) for f in flags)


# -- agent: pre-check path vs model path -------------------------------------------------------


async def test_precheck_failure_blocks_without_model_call(
    agent: A5QaAgent, provider: MockModelProvider, session: AsyncSession
) -> None:
    result = await agent.review(qa_input(clean_draft(body="We have inventory.")))
    assert result.verdict is QaVerdict.BLOCK and result.confidence == 1.0
    assert result.flags == [f"{FLAG_PROHIBITED_PREFIX}inventory"]
    assert result.precheck_only and result.run_id is None and result.model is None
    assert provider.requests == []
    assert (await session.execute(select(func.count()).select_from(AgentRun))).scalar_one() == 0


async def test_model_path_approve(
    agent: A5QaAgent, provider: MockModelProvider, session: AsyncSession
) -> None:
    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "approve",
            "confidence": 0.91,
            "flags": [],
            "rewrite_body": None,
            "rationale": "In context and compliant.",
        },
    )
    result = await agent.review(qa_input())
    assert result.verdict is QaVerdict.APPROVE and result.confidence == 0.91
    assert result.flags == [] and not result.precheck_only
    request = provider.requests[0]
    assert request.model == "claude-sonnet-5-5" and request.json_schema == QA_SCHEMA
    content = request.messages[-1].content
    for rule in prompts.MARKETING_RULES:
        assert rule in content
    assert "Are you buying in Houston?" in content and "Acme Holdings LLC" in content
    assert "DRAFT BODY:" in content
    assert (await session.execute(select(func.count()).select_from(AgentRun))).scalar_one() == 1


async def test_model_path_rewrite_and_block(agent: A5QaAgent, provider: MockModelProvider) -> None:
    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "rewrite",
            "confidence": 0.7,
            "flags": ["tone"],
            "rewrite_body": "Thanks Jane - which property types do you buy?",
            "rationale": "Too long.",
        },
    )
    result = await agent.review(qa_input())
    assert result.verdict is QaVerdict.REWRITE and result.rewrite_body is not None
    provider.reset()
    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "rewrite",
            "confidence": 0.7,
            "flags": [],
            "rewrite_body": None,
            "rationale": "",
        },
    )
    result = await agent.review(qa_input())
    assert "rewrite_without_body" in result.flags
    provider.reset()
    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "block",
            "confidence": 0.95,
            "flags": ["invented_deal"],
            "rewrite_body": None,
            "rationale": "Mentions a property we do not have.",
        },
    )
    result = await agent.review(qa_input())
    assert result.verdict is QaVerdict.BLOCK and result.flags == ["invented_deal"]


async def test_sensitive_draft_adds_flag_and_unknown_verdict_raises(
    agent: A5QaAgent, provider: MockModelProvider
) -> None:
    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "approve",
            "confidence": 0.9,
            "flags": [],
            "rewrite_body": None,
            "rationale": "",
        },
    )
    result = await agent.review(qa_input(clean_draft(contains_sensitive=True)))
    assert "sensitive_content" in result.flags
    provider.reset()
    provider.script(AgentId.A5, TaskKind.QA, parsed={"verdict": "maybe", "confidence": 1})
    with pytest.raises(StructuredOutputError):
        await agent.review(qa_input())


async def test_unscripted_mock_yields_a_valid_verdict(agent: A5QaAgent) -> None:
    result = await agent.review(qa_input())
    assert result.verdict is QaVerdict.APPROVE and result.confidence == 0.0


# -- QaReviewRepository ------------------------------------------------------------------------


async def test_qa_review_persisted_and_linked_to_message(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    investor = await make_investor()
    message = await make_message(investor, body="draft body")
    repo = QaReviewRepository(session)
    review = await repo.record(
        message_id=message.id,
        verdict=QaVerdict.BLOCK,
        confidence=0.875,
        flags=["prohibited_claim:inventory"],
        rationale="pre-check",
        model=None,
    )
    await session.commit()
    stored = await session.get(QaReview, review.id)
    assert stored is not None and stored.verdict is QaVerdict.BLOCK
    assert stored.confidence == Decimal("0.875") and stored.flags == ["prohibited_claim:inventory"]
    linked = await session.get(Message, message.id)
    assert linked is not None and linked.qa_review_id == review.id
    assert await repo.latest_for_message(message.id) is not None
    second = await repo.record(message_id=message.id, verdict=QaVerdict.APPROVE, confidence=None)
    assert [r.id for r in await repo.for_message(message.id)] == [review.id, second.id]
    assert (await session.get(Message, message.id)).qa_review_id == second.id  # type: ignore[union-attr]
    assert await repo.get(second.id) is second


async def test_qa_review_requires_a_target(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="message_id or an offer_id"):
        await QaReviewRepository(session).record(verdict=QaVerdict.APPROVE, confidence=1.0)


def test_scheduled_sequence_skips_cadence_and_repeated_request_prechecks() -> None:
    """A3 follow-ups repeat the ask by design and the workflow owns the cadence; only the
    content checks apply to them (pilot follow-up: CI e2e exposed wall-clock cadence)."""
    from datetime import UTC, datetime, timedelta

    from wholesaling.agents.a5 import (
        FLAG_CADENCE,
        FLAG_REPEATED_REQUEST,
        QaDraft,
        QaInput,
        run_prechecks,
    )

    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    base = {
        "draft": QaDraft(
            to=["inv@example.com"], subject="Deals", body="Any interest?", asks_for=["buy box"]
        ),
        "thread": (),
        "last_outbound_asks_for": ["buy box"],
        "last_outbound_at": now - timedelta(minutes=5),
        "now": now,
    }
    flags = run_prechecks(QaInput(**base))
    assert FLAG_CADENCE in flags and FLAG_REPEATED_REQUEST in flags
    flags = run_prechecks(QaInput(**base, scheduled_sequence=True))
    assert FLAG_CADENCE not in flags and FLAG_REPEATED_REQUEST not in flags
