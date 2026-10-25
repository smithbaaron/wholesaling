"""Conversation activities on SQLite with the mock Graph gateway, mock model and mock Temporal.

INV-15 / WF-21: opt-out -> suppression + ``opt_out`` signal. INV-10 / WF-19: substantive reply
-> ``reply`` signal; auto-replies never cancel a sequence (INV-22). INV-14 / INV-21: buy box
version 1, correction -> version 2 with history, replay -> no new version. XC-01 / POL-03c:
L0 queues, L1 sends only with approve + confidence + no sensitive content, blocks never reach
the mailbox. INV-24: the drafted reply stays in the inbound conversation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wholesaling.activities import conversation as conv
from wholesaling.activities import graph as graph_acts
from wholesaling.agents.a4 import MODEL_OVERRIDES
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, BuyBox, Investor, Message, Suppression, Thread
from wholesaling.db.models.enums import (
    AgentId,
    Channel,
    ContactChannel,
    Direction,
    MessageClassification,
    PolicyDecision,
    QaVerdict,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.investors import Contact
from wholesaling.db.models.messaging import Sequence
from wholesaling.db.models.ops import AgentRun, QaReview
from wholesaling.db.normalize import normalize_email, normalize_entity_name
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

MAILBOX = "deals@northstack-deals.com"
INVESTOR_EMAIL = "jane@acme.com"
SEQUENCE_WF = "investor-seq-acme-0001"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


# -- model scripts -----------------------------------------------------------------------------


def classify_as(label: str, confidence: float = 0.9, **extra: Any) -> dict[str, Any]:
    return {
        "classification": label,
        "confidence": confidence,
        "rationale": "scripted",
        "is_auto_reply": False,
        "reconnect_date": None,
        **extra,
    }


def box(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "buy_box_key": "default",
        "geography": None,
        "asset_types": None,
        "price_min": None,
        "price_max": None,
        "condition": None,
        "strategy": None,
        "financing": None,
        "closing_timeline": None,
        "other_criteria": [],
        "corrections": [],
        "raw_text": None,
    }
    base.update(fields)
    return base


def draft(body: str, asks: list[str] | None = None, sensitive: bool = False) -> dict[str, Any]:
    return {
        "subject": "RE: Deals",
        "body": body,
        "asks_for": asks or [],
        "contains_sensitive": sensitive,
    }


def qa(
    verdict: str = "approve", confidence: float = 0.92, flags: list[str] | None = None
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "flags": flags or [],
        "rewrite_body": None,
        "rationale": "scripted",
    }


CLEAN_BODY = "Thanks Jane. Which property types and what condition do you take on?"


# -- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gw(clock: FakeClock) -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=clock)


@pytest.fixture
def provider() -> MockModelProvider:
    p = MockModelProvider()
    p.script(AgentId.A4, TaskKind.DRAFT, parsed=draft(CLEAN_BODY, ["asset_types", "condition"]))
    p.script(AgentId.A5, TaskKind.QA, parsed=qa())
    return p


@pytest.fixture
def temporal() -> MockTemporalGateway:
    return MockTemporalGateway()


@pytest.fixture
def conv_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        graph_mode="mock",
        graph_mailbox=MAILBOX,
        graph_webhook_url="https://api.example/webhooks/graph",
        graph_webhook_client_state="state-secret",
    )


@pytest.fixture
async def ctx(
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    session_factory: async_sessionmaker[AsyncSession],
    conv_settings: Settings,
    clock: FakeClock,
) -> AsyncIterator[conv.ConversationContext]:
    graph_acts.set_graph_context(
        graph_acts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=conv_settings, clock=clock
        )
    )
    context = conv.ConversationContext(
        session_factory=session_factory,
        runner=AgentRunner(provider, ModelRouter.from_settings(conv_settings, MODEL_OVERRIDES)),
        temporal=temporal,
        settings=conv_settings,
        clock=clock,
        prompt_values={"company": "NorthStack"},
    )
    conv.set_conversation_context(context)
    try:
        yield context
    finally:
        conv.set_conversation_context(None)
        graph_acts.set_graph_context(None)


@pytest.fixture
async def investor(session_factory: async_sessionmaker[AsyncSession]) -> Investor:
    """An investor with a known email contact and an active investor sequence."""
    async with session_factory() as s:
        inv = Investor(
            canonical_name="Acme Holdings LLC",
            entity_key=normalize_entity_name("Acme Holdings LLC"),
            aliases=["Acme Holdings LLC"],
            first_name="Jane",
        )
        s.add(inv)
        await s.flush()
        s.add(
            Contact(
                investor_id=inv.id,
                channel=ContactChannel.EMAIL,
                value=INVESTOR_EMAIL,
                normalized_value=normalize_email(INVESTOR_EMAIL),
                is_primary=True,
            )
        )
        s.add(
            Sequence(
                kind=SequenceKind.INVESTOR,
                investor_id=inv.id,
                temporal_workflow_id=SEQUENCE_WF,
                status=SequenceStatus.ACTIVE,
                step=1,
            )
        )
        await s.commit()
        return inv


async def set_level(
    session_factory: async_sessionmaker[AsyncSession],
    level: AutonomyLevel,
    *,
    daily_cap: int | None = None,
) -> None:
    async with session_factory() as s:
        await PolicyRepository(s).set_level(
            PolicyAction.INVESTOR_REPLY, level, updated_by="test", daily_cap=daily_cap
        )
        await s.commit()


async def inbound(
    gw: MockGraphGateway,
    body: str,
    *,
    subject: str = "RE: Deals",
    from_address: str = INVESTOR_EMAIL,
    conversation_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Inject + fetch one inbound message. Returns ``(message_id, conversation_id)``."""
    msg = gw.inject_inbound(
        from_address=from_address,
        subject=subject,
        body=body,
        conversation_id=conversation_id,
        headers=headers,
    )
    fetched = await graph_acts.fetch_message(graph_acts.FetchMessageInput(graph_id=msg.graph_id))
    assert fetched.found and fetched.message_id is not None
    return fetched.message_id, msg.conversation_id or ""


async def count(sf: async_sessionmaker[AsyncSession], model: type[Any], **where: Any) -> int:
    async with sf() as s:
        stmt = select(func.count()).select_from(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return int((await s.execute(stmt)).scalar_one())


async def get_message(sf: async_sessionmaker[AsyncSession], message_id: str) -> Message:
    async with sf() as s:
        row = await s.get(Message, uuid.UUID(message_id))
        assert row is not None
        return row


async def reply_row(sf: async_sessionmaker[AsyncSession], inbound_id: str) -> Message:
    async with sf() as s:
        stmt = select(Message).where(
            Message.external_message_id == conv.reply_idempotency_key(inbound_id)
        )
        return (await s.execute(stmt)).scalar_one()


# -- classify_inbound --------------------------------------------------------------------------


async def test_opt_out_suppresses_signals_and_audits(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as("opt_out", 0.99))
    message_id, _ = await inbound(gw, "Unsubscribe.")
    result = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))

    assert result.classification == "opt_out" and result.suppression_created
    assert result.signalled == "opt_out" and result.sequence_workflow_id == SEQUENCE_WF
    assert result.investor_id == str(investor.id)
    async with session_factory() as s:
        sup = (await s.execute(select(Suppression))).scalar_one()
        assert sup.normalized_value == INVESTOR_EMAIL and sup.reason == "opt_out"
        assert sup.source_message_id == uuid.UUID(message_id)
    assert await count(session_factory, AuditEvent, event_type="suppression.added") == 1
    assert await count(session_factory, AuditEvent, event_type="message.classified") == 1
    assert await count(session_factory, AuditEvent, event_type="sequence.signalled") == 1
    signal, (payload,) = temporal.signals[SEQUENCE_WF][0]
    assert signal == "opt_out" and payload == conv.OptOutSignal(message_id=message_id)
    row = await get_message(session_factory, message_id)
    assert row.classification is MessageClassification.OPT_OUT
    assert row.classification_confidence == 0.99
    assert row.meta["a4"]["signalled"] == "opt_out"


async def test_interested_signals_reply_and_links_thread_to_investor(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as("interested", 0.93))
    message_id, _ = await inbound(gw, "Yes, send them over.")
    result = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    assert result.classification == "interested" and result.signalled == "reply"
    assert temporal.signals[SEQUENCE_WF] == [
        ("reply", (conv.ReplySignal(message_id=message_id, classification="interested"),))
    ]
    row = await get_message(session_factory, message_id)
    async with session_factory() as s:
        thread = await s.get(Thread, row.thread_id)
        assert thread is not None and thread.investor_id == investor.id
    assert await count(session_factory, Suppression) == 0
    assert provider.requests[-1].model == "claude-sonnet-5-5", "A4 classify override (section 5)"


async def test_classify_replay_is_a_no_op(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as("question"))
    message_id, _ = await inbound(gw, "How does this work?")
    first = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    calls = len(provider.requests)
    second = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    assert second.already_classified and second.classification == first.classification
    assert second.signalled == "reply" and second.sequence_workflow_id == SEQUENCE_WF
    assert len(provider.requests) == calls, "no second model call"
    assert len(temporal.signals[SEQUENCE_WF]) == 1, "no second signal"
    assert await count(session_factory, AgentRun) == 1


async def test_sequence_on_the_thread_wins_over_investor_fallback(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as("interested"))
    message_id, _conversation_id = await inbound(gw, "Yes.")
    row = await get_message(session_factory, message_id)
    async with session_factory() as s:
        s.add(
            Sequence(
                kind=SequenceKind.INVESTOR,
                investor_id=investor.id,
                thread_id=row.thread_id,
                temporal_workflow_id="investor-seq-thread",
                status=SequenceStatus.PAUSED,
            )
        )
        await s.commit()
    result = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    assert result.sequence_workflow_id == "investor-seq-thread"
    assert "investor-seq-thread" in temporal.signals and SEQUENCE_WF not in temporal.signals


async def test_auto_reply_is_ambiguous_and_never_cancels_the_sequence(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as("interested"))
    message_id, _ = await inbound(
        gw,
        "I am out of the office until Nov 3.",
        subject="Automatic reply: Deals",
        headers={"auto-submitted": "auto-replied"},
    )
    result = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    assert result.classification == "ambiguous" and result.is_auto_reply
    assert result.signalled is None and temporal.signals == {}
    assert provider.requests == [], "deterministic detection, no model call"
    assert await count(session_factory, AgentRun) == 0


async def test_unrelated_and_outbound_and_bounce_do_not_signal(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    investor: Investor,
) -> None:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as("unrelated"))
    message_id, _ = await inbound(gw, "Weekly market update", subject="Newsletter")
    result = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    assert result.classification == "unrelated" and result.signalled is None

    own_id, _ = await inbound(gw, "our own sent mail", from_address=MAILBOX)
    skipped = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=own_id))
    assert skipped.skipped_reason == "outbound" and skipped.classification is None

    bounce_id, _ = await inbound(
        gw,
        f"Delivery to {INVESTOR_EMAIL} failed.",
        subject="Undeliverable: Deals",
        from_address="postmaster@acme.com",
    )
    skipped = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=bounce_id))
    assert skipped.skipped_reason == "bounce"
    assert temporal.signals == {}


async def test_not_now_keeps_reconnect_date(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(
        AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as("not_now", reconnect_date="2027-01-15")
    )
    message_id, _ = await inbound(gw, "Circle back after January 15.")
    result = await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    assert result.reconnect_date == "2027-01-15"
    row = await get_message(session_factory, message_id)
    assert row.meta["a4"]["reconnect_date"] == "2027-01-15"


# -- extract_and_upsert_buy_box ------------------------------------------------------------------


async def test_buy_box_version_1_then_correction_then_replay(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(
        AgentId.A4,
        TaskKind.EXTRACT,
        parsed={
            "buy_boxes": [
                box(
                    geography={
                        "markets": ["Houston"],
                        "cities": None,
                        "zips": ["77009"],
                        "neighborhoods": None,
                    },
                    asset_types=["sfr"],
                    price_max=250000,
                    financing="cash",
                    raw_text="SFR in 77009, cash, up to 250k",
                )
            ],
            "rationale": "",
        },
    )
    first_id, conversation_id = await inbound(gw, "SFR in 77009, cash, up to 250k.")
    first = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=first_id))
    assert first.investor_id == str(investor.id) and first.created_any
    (v1,) = first.versions
    assert (v1.version, v1.reason, v1.buy_box_key) == (1, "created", "default")
    assert v1.unknowns == ["condition", "closing_timeline"] and first.corrections == []
    assert await count(session_factory, AuditEvent, event_type="buy_box.created") == 1

    # Correction in the same conversation: "I said 150k max, not 250k".
    provider.script(
        AgentId.A4,
        TaskKind.EXTRACT,
        parsed={
            "buy_boxes": [box(price_max=150000, raw_text="I said 150k max, not 250k")],
            "rationale": "",
        },
    )
    second_id, _ = await inbound(
        gw, "I said 150k max, not 250k. Everything else stands.", conversation_id=conversation_id
    )
    second = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=second_id))
    (v2,) = second.versions
    assert (v2.version, v2.reason) == (2, "created")
    assert v2.changed_fields == ["price_max"]
    assert second.corrections == [{"field": "price_max", "old": "250000", "new": "150000"}]
    async with session_factory() as s:
        rows = list((await s.execute(select(BuyBox).order_by(BuyBox.version))).scalars().all())
    assert [r.version for r in rows] == [1, 2]
    assert [r.is_current for r in rows] == [False, True]
    assert rows[0].price_max == Decimal(250000) and rows[1].price_max == Decimal(150000)
    assert rows[1].asset_types == ["sfr"] and rows[1].financing == "cash", "merge keeps the rest"
    assert rows[1].source_message_id == uuid.UUID(second_id)
    assert rows[1].change_note is not None and rows[1].change_note.startswith(
        "correction: price_max"
    )
    assert await count(session_factory, AuditEvent, event_type="buy_box.updated") == 1
    async with session_factory() as s:
        updated = (
            await s.execute(select(AuditEvent).where(AuditEvent.event_type == "buy_box.updated"))
        ).scalar_one()
    assert updated.payload["superseded_version"] == 1
    assert updated.payload["corrections"] == second.corrections

    # Replay of the correction message: nothing new.
    calls = len(provider.requests)
    replay = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=second_id))
    assert replay.skipped_reason == "replay" and not replay.created_any
    assert replay.versions[0].version == 2 and replay.versions[0].reason == "replay"
    assert replay.corrections == second.corrections
    assert len(provider.requests) == calls
    assert await count(session_factory, BuyBox) == 2
    assert await count(session_factory, AuditEvent, event_type="buy_box.updated") == 1


async def test_two_buy_boxes_in_one_email(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(
        AgentId.A4,
        TaskKind.EXTRACT,
        parsed={
            "buy_boxes": [
                box(buy_box_key="houston-flips", asset_types=["sfr"], price_max=300000),
                box(buy_box_key="dallas-rentals", asset_types=["duplex"], price_max=500000),
            ],
            "rationale": "",
        },
    )
    message_id, _ = await inbound(gw, "Two boxes...")
    result = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=message_id))
    assert [(v.buy_box_key, v.version, v.created) for v in result.versions] == [
        ("houston-flips", 1, True),
        ("dallas-rentals", 1, True),
    ]
    assert await count(session_factory, BuyBox) == 2
    assert await count(session_factory, AuditEvent, event_type="buy_box.created") == 2
    replay = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=message_id))
    assert replay.skipped_reason == "replay" and len(replay.versions) == 2


async def test_extract_without_investor_or_without_criteria_is_skipped(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stranger_id, _ = await inbound(gw, "SFR up to 200k", from_address="who@unknown.com")
    result = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=stranger_id))
    assert result.skipped_reason == "no_investor" and result.versions == []
    assert provider.requests == []

    # Unscripted mock: the schema makes it return no boxes, so nothing is invented or stored.
    message_id, _ = await inbound(gw, "Yes, interested.")
    result = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=message_id))
    assert result.skipped_reason == "nothing_stated" and result.versions == []
    assert await count(session_factory, BuyBox) == 0
    again = await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=message_id))
    assert again.skipped_reason == "nothing_stated"
    assert len(provider.requests) == 1, "the empty extraction is remembered, no second call"


# -- draft_and_qa_reply ------------------------------------------------------------------------


async def classified(
    gw: MockGraphGateway, provider: MockModelProvider, body: str, label: str = "interested"
) -> tuple[str, str]:
    provider.script(AgentId.A4, TaskKind.CLASSIFY, parsed=classify_as(label))
    message_id, conversation_id = await inbound(gw, body)
    await conv.classify_inbound(conv.ClassifyInboundInput(message_id=message_id))
    return message_id, conversation_id


async def test_l0_queues_draft_in_same_conversation_and_sends_nothing(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message_id, conversation_id = await classified(gw, provider, "Yes, interested.")
    result = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))

    assert result.queued and not result.sent and not result.blocked
    assert result.policy_level == "L0" and result.qa_verdict == "approve"
    assert result.asks_for == ["asset_types", "condition"]
    assert gw.sent == [] and len(gw.drafts) == 1
    graph_draft = next(iter(gw.drafts.values()))
    assert graph_draft["conversation_id"] == conversation_id, "INV-24: same conversation"
    assert graph_draft["to"] == [INVESTOR_EMAIL] and graph_draft["body"] == CLEAN_BODY
    assert graph_draft["sent"] is False

    row = await reply_row(session_factory, message_id)
    assert row.direction is Direction.OUT and row.draft and row.sent_at is None
    assert row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
    assert row.policy_action == "investor_reply"
    assert row.meta["asks_for"] == ["asset_types", "condition"]
    assert row.qa_review_id is not None
    async with session_factory() as s:
        review = await s.get(QaReview, row.qa_review_id)
        assert review is not None and review.message_id == row.id
        assert review.verdict is QaVerdict.APPROVE and review.confidence == Decimal("0.92")
    assert await count(session_factory, AuditEvent, event_type="reply.decided") == 1
    assert await count(session_factory, AuditEvent, event_type="message.drafted") == 1
    assert await count(session_factory, AuditEvent, event_type="message.sent") == 0
    inbound_row = await get_message(session_factory, message_id)
    assert inbound_row.meta["reply"]["policy_decision"] == "queue_for_human"

    # Replay: same draft, nothing new in the mailbox, no new model calls.
    calls = len(provider.requests)
    again = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    assert again.already_processed and again.draft_message_id == result.draft_message_id
    assert again.queued and len(gw.drafts) == 1 and len(provider.requests) == calls
    assert await count(session_factory, QaReview) == 1


async def test_l1_approved_confident_non_sensitive_sends_in_same_conversation(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_level(session_factory, AutonomyLevel.L1)
    provider.script(AgentId.A5, TaskKind.QA, parsed=qa("approve", 0.9))
    message_id, conversation_id = await classified(gw, provider, "Yes, interested.")
    result = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))

    assert result.sent and result.policy_decision == "send" and result.policy_level == "L1"
    assert not result.contains_sensitive
    assert len(gw.sent) == 1 and gw.sent[0].conversation_id == conversation_id
    row = await reply_row(session_factory, message_id)
    assert row.sent_at is not None and not row.draft
    assert row.policy_decision is PolicyDecision.SEND
    assert await count(session_factory, AuditEvent, event_type="message.sent") == 1
    assert await count(session_factory, AuditEvent, event_type="reply.decided") == 1

    again = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    assert again.already_processed and again.sent and len(gw.sent) == 1


async def test_l1_with_price_talk_is_queued(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_level(session_factory, AutonomyLevel.L1)
    provider.script(
        AgentId.A4,
        TaskKind.DRAFT,
        parsed=draft("Thanks Jane. What price range do you target, roughly $150k?", ["price_max"]),
    )
    provider.script(AgentId.A5, TaskKind.QA, parsed=qa("approve", 0.95))
    message_id, _ = await classified(gw, provider, "Yes, interested.")
    result = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    assert result.contains_sensitive and result.queued and not result.sent
    assert "price/contract/legal" in (result.policy_reason or "")
    assert gw.sent == [] and len(gw.drafts) == 1
    assert "sensitive_content" in result.qa_flags


async def test_l1_below_confidence_or_rewrite_is_queued(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_level(session_factory, AutonomyLevel.L1)
    provider.script(AgentId.A5, TaskKind.QA, parsed=qa("approve", 0.6))
    low_id, _ = await classified(gw, provider, "Yes.")
    low = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=low_id))
    assert low.queued and "confidence below" in (low.policy_reason or "")

    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={**qa("rewrite", 0.95), "rewrite_body": "Thanks Jane - which property types?"},
    )
    rewrite_id, _ = await classified(gw, provider, "Yes!")
    rewritten = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=rewrite_id))
    assert rewritten.queued and rewritten.body == "Thanks Jane - which property types?"
    row = await reply_row(session_factory, rewrite_id)
    assert row.body == "Thanks Jane - which property types?" and row.meta["a4_body"] == CLEAN_BODY
    assert gw.sent == []


async def test_l1_daily_cap_reached_is_queued(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_level(session_factory, AutonomyLevel.L1, daily_cap=1)
    first_id, _ = await classified(gw, provider, "Yes, interested.")
    first = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=first_id))
    assert first.sent
    second_id, _ = await classified(gw, provider, "Also interested.")
    second = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=second_id))
    assert second.queued and "daily cap 1 reached" in (second.policy_reason or "")
    assert len(gw.sent) == 1


async def test_qa_precheck_block_never_reaches_the_mailbox(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_level(session_factory, AutonomyLevel.L1)
    provider.script(
        AgentId.A4,
        TaskKind.DRAFT,
        parsed=draft("We have inventory under contract in 77009 right now."),
    )
    message_id, _ = await classified(gw, provider, "Yes, interested.")
    result = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))

    assert result.blocked and not result.sent
    assert result.qa_verdict == "block" and result.qa_confidence == 1.0
    assert {"prohibited_claim:inventory", "prohibited_claim:under_contract"} <= set(result.qa_flags)
    assert gw.drafts == {} and gw.sent == [], "blocked drafts stay out of Outlook"
    row = await reply_row(session_factory, message_id)
    assert row.policy_decision is PolicyDecision.BLOCKED and row.meta["blocked"] is True
    assert row.draft and row.sent_at is None and row.qa_review_id is not None
    async with session_factory() as s:
        review = await s.get(QaReview, row.qa_review_id)
        assert review is not None and review.verdict is QaVerdict.BLOCK
        assert review.agent_run_id is None, "pre-check: no model call to link"
    a5_runs = await count(session_factory, AgentRun, agent=AgentId.A5)
    assert a5_runs == 0
    again = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    assert again.already_processed and again.blocked


async def test_suppressed_recipient_is_blocked(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        s.add(
            Suppression(
                channel=Channel.EMAIL,
                normalized_value=normalize_email(INVESTOR_EMAIL),
                reason="opt_out",
            )
        )
        await s.commit()
    message_id, _ = await classified(gw, provider, "Actually, send me deals.")
    result = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    assert result.blocked and "opt_out_violation" in result.qa_flags
    assert gw.drafts == {} and gw.sent == []


async def test_draft_prompt_sees_thread_and_current_buy_box_and_missing_fields(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider.script(
        AgentId.A4,
        TaskKind.EXTRACT,
        parsed={"buy_boxes": [box(asset_types=["sfr"], price_max=200000)], "rationale": ""},
    )
    message_id, _conversation_id = await classified(
        gw, provider, "SFR up to 200k.", label="buy_box_supplied"
    )
    await conv.extract_and_upsert_buy_box(conv.ExtractBuyBoxInput(message_id=message_id))
    await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    draft_request = next(r for r in provider.requests if r.metadata.task_kind is TaskKind.DRAFT)
    content = draft_request.messages[-1].content
    assert "SFR up to 200k." in content
    assert (
        "MISSING FIELDS YOU MAY ASK ABOUT: geography, condition, financing, closing_timeline"
        in content
    )
    assert '"price_max": "200000"' in content and "Acme Holdings LLC" in content
    assert "NorthStack" in draft_request.system
    qa_request = next(r for r in provider.requests if r.metadata.task_kind is TaskKind.QA)
    assert "SFR up to 200k." in qa_request.messages[-1].content
    assert "NorthStack" in qa_request.system


# -- apply_human_decision ----------------------------------------------------------------------


async def test_human_approval_sends_queued_draft(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message_id, conversation_id = await classified(gw, provider, "Yes, interested.")
    queued = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    assert queued.queued
    applied = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(message_id=message_id, approved=True, decided_by="aaron")
    )
    assert applied.sent and not applied.discarded and not applied.edited
    assert applied.draft_message_id == queued.draft_message_id
    assert len(gw.sent) == 1 and gw.sent[0].conversation_id == conversation_id
    row = await reply_row(session_factory, message_id)
    assert row.sent_at is not None and row.meta["human_decision_applied"] == "approved"
    assert row.meta["decided_by"] == "aaron"
    async with session_factory() as s:
        approved = (
            await s.execute(select(AuditEvent).where(AuditEvent.event_type == "reply.approved"))
        ).scalar_one()
    assert approved.actor.value == "human" and approved.actor_id == "aaron"
    again = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(message_id=message_id, approved=True)
    )
    assert again.already_decided and again.sent and len(gw.sent) == 1


async def test_human_rejection_discards_and_edit_sends_new_draft(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rejected_id, _ = await classified(gw, provider, "Yes.")
    await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=rejected_id))
    rejected = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(message_id=rejected_id, approved=False, decided_by="aaron")
    )
    assert rejected.discarded and not rejected.sent and gw.sent == []
    row = await reply_row(session_factory, rejected_id)
    assert row.meta["human_decision_applied"] == "rejected" and row.sent_at is None
    assert await count(session_factory, AuditEvent, event_type="reply.rejected") == 1

    edited_id, conversation_id = await classified(gw, provider, "Yes please.")
    original = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=edited_id))
    applied = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(
            message_id=edited_id, approved=True, edited_body="Thanks Jane - which ZIPs?"
        )
    )
    assert applied.sent and applied.edited
    assert applied.draft_message_id != original.draft_message_id
    assert len(gw.sent) == 1 and gw.sent[0].conversation_id == conversation_id
    sent_draft = gw.drafts[gw.sent[0].draft_id]
    assert sent_draft["body"] == "Thanks Jane - which ZIPs?"
    original_row = await reply_row(session_factory, edited_id)
    assert original_row.sent_at is None
    assert original_row.meta["superseded_by"] == applied.draft_message_id
    async with session_factory() as s:
        new_row = await s.get(Message, uuid.UUID(applied.draft_message_id or ""))
        assert new_row is not None and new_row.sent_at is not None
        assert new_row.meta["edited_by_human"] and new_row.qa_review_id == original_row.qa_review_id
        assert new_row.policy_action == "investor_reply"


async def test_human_decision_without_draft_or_on_blocked_draft(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message_id, _ = await classified(gw, provider, "Yes.")
    none = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(message_id=message_id, approved=True)
    )
    assert none.reason == "no_draft" and not none.sent
    provider.script(AgentId.A4, TaskKind.DRAFT, parsed=draft("Guaranteed returns!"))
    await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    blocked = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(message_id=message_id, approved=True)
    )
    assert blocked.reason == "blocked" and blocked.discarded and gw.sent == []


# -- notify_bounce -----------------------------------------------------------------------------


async def test_notify_bounce_signals_sequence_once(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    temporal: MockTemporalGateway,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bounce_id, _ = await inbound(
        gw,
        f"Delivery has failed to these recipients: {INVESTOR_EMAIL}",
        subject="Undeliverable: Deals",
        from_address="postmaster@acme.com",
    )
    result = await conv.notify_bounce(
        conv.NotifyBounceInput(message_id=bounce_id, bounced_address=INVESTOR_EMAIL)
    )
    assert result.signalled and result.sequence_workflow_id == SEQUENCE_WF
    signal, (payload,) = temporal.signals[SEQUENCE_WF][0]
    assert signal == "bounce"
    assert payload == conv.BounceSignal(message_id=bounce_id, bounced_address=INVESTOR_EMAIL)
    again = await conv.notify_bounce(
        conv.NotifyBounceInput(message_id=bounce_id, bounced_address=INVESTOR_EMAIL)
    )
    assert again.signalled and len(temporal.signals[SEQUENCE_WF]) == 1
    assert await count(session_factory, Suppression, reason="bounce") == 1


async def test_notify_bounce_without_sequence_is_quiet(
    ctx: conv.ConversationContext, gw: MockGraphGateway, temporal: MockTemporalGateway
) -> None:
    bounce_id, _ = await inbound(
        gw,
        "Delivery failed to nobody@nowhere.com",
        subject="Undeliverable: Deals",
        from_address="postmaster@nowhere.com",
    )
    result = await conv.notify_bounce(conv.NotifyBounceInput(message_id=bounce_id))
    assert not result.signalled and temporal.signals == {}


# -- context -----------------------------------------------------------------------------------


async def test_default_context_is_built_from_the_graph_context(
    gw: MockGraphGateway, session_factory: async_sessionmaker[AsyncSession], conv_settings: Settings
) -> None:
    graph_acts.set_graph_context(
        graph_acts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=conv_settings
        )
    )
    conv.set_conversation_context(None)
    try:
        ctx = conv.get_conversation_context()
        assert ctx.session_factory is session_factory and ctx.settings is conv_settings
        assert isinstance(ctx.temporal, MockTemporalGateway)
        assert ctx.runner.router.route(AgentId.A4, TaskKind.CLASSIFY) == "claude-sonnet-5-5"
        assert conv.get_conversation_context() is ctx
    finally:
        conv.set_conversation_context(None)
        graph_acts.set_graph_context(None)


async def test_unscripted_mock_and_unknown_sender_still_queue_a_draft(
    gw: MockGraphGateway,
    temporal: MockTemporalGateway,
    session_factory: async_sessionmaker[AsyncSession],
    conv_settings: Settings,
    clock: FakeClock,
) -> None:
    """The default mock (no scripts) must carry an unknown sender through the whole pipeline
    without inventing anything: classify -> no buy box -> draft -> QA -> queued at L0."""
    graph_acts.set_graph_context(
        graph_acts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=conv_settings, clock=clock
        )
    )
    conv.set_conversation_context(
        conv.ConversationContext(
            session_factory=session_factory,
            runner=AgentRunner(MockModelProvider(), ModelRouter.from_settings(conv_settings)),
            temporal=temporal,
            settings=conv_settings,
            clock=clock,
        )
    )
    try:
        message_id, conversation_id = await inbound(gw, "Yes", from_address="who@unknown.com")
        classified_result = await conv.classify_inbound(
            conv.ClassifyInboundInput(message_id=message_id)
        )
        assert (
            classified_result.classification == "interested" and classified_result.signalled is None
        )
        extracted = await conv.extract_and_upsert_buy_box(
            conv.ExtractBuyBoxInput(message_id=message_id)
        )
        assert extracted.skipped_reason == "no_investor"
        drafted = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
        assert drafted.queued and not drafted.sent and drafted.qa_verdict == "approve"
        assert len(gw.drafts) == 1 and gw.sent == []
        assert next(iter(gw.drafts.values()))["conversation_id"] == conversation_id
        assert await count(session_factory, BuyBox) == 0
    finally:
        conv.set_conversation_context(None)
        graph_acts.set_graph_context(None)


async def test_review_queue_contract_workflow_id_and_decision_record(
    ctx: conv.ConversationContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    investor: Investor,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review queue (api/routes/review.py) resolves the workflow from
    ``meta["temporal_workflow_id"]``, flips the row and writes ``meta["human_decision"]`` *before*
    signalling; our activity must still send the edited body and must not mistake that record
    for its own idempotency marker."""
    from wholesaling.db.repositories.review import META_WORKFLOW_ID_KEY, ReviewRepository

    monkeypatch.setattr(conv, "_correlation_id", lambda: "graph-inbound-abc")
    message_id, conversation_id = await classified(gw, provider, "Yes, interested.")
    queued = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=message_id))
    row = await reply_row(session_factory, message_id)
    assert row.meta[META_WORKFLOW_ID_KEY] == "graph-inbound-abc"
    assert row.meta["graph_draft_body"] == CLEAN_BODY
    async with session_factory() as s:
        draft_row = await s.get(Message, uuid.UUID(queued.draft_message_id or ""))
        assert draft_row is not None
        assert await ReviewRepository(s).resolve_workflow_id(draft_row) == "graph-inbound-abc"
        await ReviewRepository(s).apply_decision(
            draft_row,
            approved=True,
            edited_body="Edited by the operator.",
            operator="aaron",
            reason=None,
            decided_at=NOW,
        )
        await s.commit()

    applied = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(
            message_id=message_id,
            approved=True,
            edited_body="Edited by the operator.",
            decided_by="aaron",
        )
    )
    assert applied.sent and applied.edited and not applied.already_decided
    assert len(gw.sent) == 1 and gw.drafts[gw.sent[0].draft_id]["body"] == "Edited by the operator."
    assert gw.sent[0].conversation_id == conversation_id

    # Rejection path: the queue marks the row blocked first; we record the discard, nothing sent.
    rejected_id, _ = await classified(gw, provider, "Yes again.")
    second = await conv.draft_and_qa_reply(conv.DraftReplyInput(message_id=rejected_id))
    async with session_factory() as s:
        draft_row = await s.get(Message, uuid.UUID(second.draft_message_id or ""))
        assert draft_row is not None
        await ReviewRepository(s).apply_decision(
            draft_row,
            approved=False,
            edited_body=None,
            operator="aaron",
            reason="no",
            decided_at=NOW,
        )
        await s.commit()
    rejected = await conv.apply_human_decision(
        conv.ApplyHumanDecisionInput(message_id=rejected_id, approved=False, decided_by="aaron")
    )
    assert rejected.discarded and not rejected.sent and len(gw.sent) == 1
    row = await reply_row(session_factory, rejected_id)
    assert row.meta["human_decision_applied"] == "rejected"
    assert row.meta["human_decision"]["approved"] is False, "the queue's own record is kept"
