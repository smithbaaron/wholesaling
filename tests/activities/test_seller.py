"""Seller activities against SQLite with the mock Graph / model / Temporal gateways and the
fixture data providers (REQUIREMENTS.md SEL-01..08, SEL-12, WF-03, WF-10, WF-21, POL-03d/f,
POL-03l, POL-09, XC-01, XC-03).
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
from wholesaling.activities import graph as gacts
from wholesaling.activities import outreach as oacts
from wholesaling.activities import seller as acts
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.agents.templates.seller_outreach import disclosure, missing_compliance_blocks
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import (
    AgentId,
    Channel,
    Direction,
    MarketTier,
    PolicyDecision,
    SellerLeadStatus,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Sequence, Suppression, Thread
from wholesaling.db.models.ops import AgentRun, AuditEvent, QaReview
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.db.repositories.sellers import SellerRepository
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

MAILBOX = "deals@example.test"
SELLER = "dolores.ramirez@example.test"
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)  # Tuesday 10:00 Chicago
SENDER = SenderIdentity(
    name="Aaron",
    company="NorthStack",
    postal_address="123 Main St, Houston, TX 77002",
    opt_out_text=(
        'If you would rather not hear from us, reply with "unsubscribe" and we will remove you.'
    ),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "graph_mode": "mock",
        "graph_mailbox": MAILBOX,
        "sender_name": "Aaron",
        "sender_company": "NorthStack",
        "sender_postal_address": "123 Main St, Houston, TX 77002",
        "public_base_url": "https://sell.example.test",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def qa(verdict: str = "approve", confidence: float = 0.95) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "confidence": confidence,
        "flags": [],
        "rewrite_body": "Shorter." if verdict == "rewrite" else None,
        "rationale": "scripted",
    }


def classify(label: str, *, asks_offer: bool = False, auto: bool = False) -> dict[str, Any]:
    return {
        "classification": label,
        "confidence": 0.9,
        "rationale": "scripted",
        "is_auto_reply": auto,
        "asks_offer": asks_offer,
    }


def extract(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "property_address": None,
        "motivation": None,
        "condition_class": None,
        "disclosed_issues": [],
        "timeline": None,
        "timeline_text": None,
        "asking_price": None,
        "co_owners": None,
        "occupancy": None,
        "liens": None,
        "hoa": None,
        "seller_unsure": [],
        "photos_mentioned": False,
        "rationale": "scripted",
    }
    base.update(kw)
    return base


def draft(
    body: str = "Thanks for the details. Could you tell me about the condition and timeline?",
) -> dict[str, Any]:
    return {
        "subject": "",
        "body": body,
        "asks_for": ["condition", "timeline"],
        "contains_sensitive": False,
    }


# --------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gw(clock: FakeClock) -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=clock)


@pytest.fixture
def provider() -> MockModelProvider:
    p = MockModelProvider()
    p.script(AgentId.A5, TaskKind.QA, parsed=qa())
    p.script(AgentId.S4, TaskKind.DRAFT, parsed=draft())
    return p


@pytest.fixture
def temporal() -> MockTemporalGateway:
    return MockTemporalGateway()


@pytest.fixture
def providers() -> DataProviders:
    return mock_data_providers()


@pytest.fixture(autouse=True)
async def contexts(
    gw: MockGraphGateway,
    clock: FakeClock,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    providers: DataProviders,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[acts.SellerContext]:
    settings = _settings()
    runner = AgentRunner(provider, ModelRouter.from_settings(settings))
    gacts.set_graph_context(
        gacts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=settings, clock=clock
        )
    )
    conv.set_conversation_context(
        conv.ConversationContext(
            session_factory=session_factory,
            runner=runner,
            temporal=temporal,
            settings=settings,
            clock=clock,
        )
    )
    ctx = acts.SellerContext(
        session_factory=session_factory,
        providers=providers,
        temporal=temporal,
        settings=settings,
        clock=clock,
        runner=runner,
        prompt_values={"company": "NorthStack"},
    )
    acts.set_seller_context(ctx)
    try:
        yield ctx
    finally:
        acts.set_seller_context(None)
        conv.set_conversation_context(None)
        gacts.set_graph_context(None)


async def _market(session: AsyncSession, tier: MarketTier = MarketTier.ACTIVE) -> Market:
    market = Market(name="Houston", state="TX", tier=tier)
    session.add(market)
    await session.commit()
    return market


async def _lead(
    session: AsyncSession,
    market: Market,
    *,
    owner: str = "Dolores Ramirez",
    email: str | None = SELLER,
    verified: bool = True,
    status: SellerLeadStatus = SellerLeadStatus.QUEUED,
) -> SellerLead:
    lead = SellerLead(
        market_id=market.id,
        status=status,
        owner_name=owner,
        property_address_line1="1420 Pecan Grove Dr",
        property_city="Houston",
        property_state="TX",
        property_postal_code="77009",
        address_key="1420 PECAN GROVE DR|HOUSTON|TX|77009",
        parcel_id="HCAD-D-0000001" if verified else None,
        parcel_verified=verified,
        signals=["absentee", "tax_delinquent"],
        signal_score=40,
        assessed_value=Decimal("165000"),
        source="test",
        contact_channels=(
            [{"channel": "email", "value": email, "normalized_value": email, "is_primary": True}]
            if email
            else []
        ),
    )
    session.add(lead)
    await session.commit()
    return lead


async def _sequence(
    session: AsyncSession, lead: SellerLead, *, with_thread: bool = True
) -> tuple[Sequence, Thread | None]:
    seq_id = uuid.uuid4()
    row, _ = await SequenceRepository(session).create(
        kind=SequenceKind.SELLER,
        seller_lead_id=lead.id,
        temporal_workflow_id=acts.seller_sequence_workflow_id(str(seq_id)),
        sequence_id=seq_id,
    )
    thread: Thread | None = None
    if with_thread:
        thread = Thread(
            channel=Channel.EMAIL,
            external_thread_id=f"conv-{uuid.uuid4().hex[:8]}",
            seller_lead_id=lead.id,
            counterpart_address=SELLER,
            subject="Question about 1420 Pecan Grove Dr",
        )
        session.add(thread)
        await session.flush()
        row.thread_id = thread.id
    lead.status = SellerLeadStatus.IN_SEQUENCE
    await session.commit()
    return row, thread


async def _inbound(
    session: AsyncSession,
    thread: Thread,
    body: str,
    *,
    from_address: str = SELLER,
    headers: dict[str, str] | None = None,
    gw: MockGraphGateway | None = None,
) -> Message:
    """An inbound row on ``thread``; when ``gw`` is given the item also exists in the mock
    mailbox so a reply draft can be filed against it (as ``fetch_message`` would leave it)."""
    graph_id = f"AAMk-{uuid.uuid4().hex[:6]}"
    external_id = f"<{uuid.uuid4().hex}@example.test>"
    if gw is not None:
        injected = gw.inject_inbound(
            from_address=from_address,
            subject="RE: Question about 1420 Pecan Grove Dr",
            body=body,
            conversation_id=thread.external_thread_id,
            internet_message_id=external_id,
            graph_id=graph_id,
            headers=headers,
        )
        graph_id = injected.graph_id
    msg = Message(
        thread_id=thread.id,
        direction=Direction.IN,
        channel=Channel.EMAIL,
        external_message_id=external_id,
        from_address=from_address,
        to_addresses=[MAILBOX],
        subject="RE: Question about 1420 Pecan Grove Dr",
        body=body,
        received_at=NOW,
        meta={
            "graph_id": graph_id,
            "conversation_id": thread.external_thread_id,
            "headers": headers or {},
        },
    )
    session.add(msg)
    await session.commit()
    return msg


async def _count(session: AsyncSession, model: type, **where: object) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int((await session.execute(stmt)).scalar_one())


async def _set_level(
    session: AsyncSession,
    action: PolicyAction,
    level: AutonomyLevel,
    *,
    daily_cap: int | None = None,
) -> None:
    await PolicyRepository(session).set_level(action, level, updated_by="test", daily_cap=daily_cap)
    await session.commit()


def _sent_bodies(gw: MockGraphGateway) -> list[dict[str, Any]]:
    return [gw.drafts[r.draft_id] for r in gw.sent]


def _starts(temporal: MockTemporalGateway, workflow: str) -> list[Any]:
    return [
        c for c in temporal.calls if c.method == "start_workflow" and c.args["workflow"] == workflow
    ]


# --------------------------------------------------------------------------------------------
# S1 + S2 + sequence start (WF-10)
# --------------------------------------------------------------------------------------------


async def test_list_refresh_builds_traces_and_starts_sequences(
    session: AsyncSession, temporal: MockTemporalGateway
) -> None:
    market = await _market(session)
    result = await acts.seller_list_refresh(acts.SellerListRefreshInput(market_id=str(market.id)))
    assert len(result.markets) == 1
    m = result.markets[0]
    assert m.s1["created"] == 5 and m.traced == 5
    assert m.reachable == 4 and m.unreachable == 1  # the estate has only a DNC-listed phone
    assert m.sequences_started == 4 and result.sequences_started == 4
    starts = _starts(temporal, "SellerSequenceWorkflow")
    assert len(starts) == 4
    for call in starts:
        assert call.args["workflow_id"].startswith("seller-sequence:")
        inp = call.args["args"][0]
        assert inp.intervals_days == [3, 7, 14] and inp.market == "Houston"
        assert inp.contact_email.endswith("@example.test")
    leads = list((await session.execute(select(SellerLead))).scalars())
    for lead in leads:
        await session.refresh(lead)
    statuses = {lead.owner_name: lead.status for lead in leads}
    assert statuses["Dolores Ramirez"] is SellerLeadStatus.IN_SEQUENCE
    assert statuses["Estate of Walter Boyd"] is SellerLeadStatus.QUEUED
    assert await _count(session, Sequence, kind=SequenceKind.SELLER) == 4
    assert await _count(session, AuditEvent, event_type=acts.EVENT_SEQUENCE_STARTED) == 4
    assert await _count(session, AuditEvent, event_type=acts.EVENT_LEAD_UNREACHABLE) == 1
    # Every traced lead carries the DNC scrub record (SEL-03), SMS allowed on none.
    for lead in leads:
        trace = lead.source_refs["skip_trace"]
        assert "dnc_listed" in trace and trace["sms_allowed"] is False


async def test_list_refresh_is_idempotent(
    session: AsyncSession, temporal: MockTemporalGateway
) -> None:
    market = await _market(session)
    first = await acts.seller_list_refresh(acts.SellerListRefreshInput(market_id=str(market.id)))
    second = await acts.seller_list_refresh(acts.SellerListRefreshInput(market_id=str(market.id)))
    assert first.sequences_started == 4 and second.sequences_started == 0
    assert second.markets[0].s1["created"] == 0 and second.markets[0].s1["refreshed"] == 5
    assert second.markets[0].traced == 0  # already traced
    assert second.markets[0].skipped == {"no_email": 1}  # in-sequence leads are no longer queued
    assert len(_starts(temporal, "SellerSequenceWorkflow")) == 4
    assert await _count(session, SellerLead) == 5


async def test_list_refresh_refuses_watch_market_and_runs_every_active_market(
    session: AsyncSession, temporal: MockTemporalGateway
) -> None:
    watch = Market(name="Columbus", state="OH", tier=MarketTier.WATCH)
    active = Market(name="Houston", state="TX", tier=MarketTier.ACTIVE)
    session.add_all([watch, active])
    await session.commit()
    refused = await acts.seller_list_refresh(acts.SellerListRefreshInput(market_id=str(watch.id)))
    assert refused.markets[0].s1["refused_reason"] == "market_not_active"
    assert refused.sequences_started == 0 and await _count(session, SellerLead) == 0
    everything = await acts.seller_list_refresh(acts.SellerListRefreshInput(limit=2))
    assert [m.market for m in everything.markets] == ["Houston, TX"]  # Watch is not scheduled
    assert everything.sequences_started == 2 and everything.markets[0].skipped["limit"] == 2


async def test_sequence_start_failure_rolls_the_lead_back(
    session: AsyncSession, contexts: acts.SellerContext
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)

    class Failing(MockTemporalGateway):
        async def start_workflow(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("temporal down")

    contexts.temporal = Failing()
    with pytest.raises(RuntimeError, match="temporal down"):
        await acts.start_seller_sequence(contexts, lead, contact_email=SELLER, market="Houston")
    seq = (await session.execute(select(Sequence))).scalar_one()
    assert seq.status is SequenceStatus.CANCELLED and seq.cancel_reason == "start_failed"
    await session.refresh(lead)
    assert lead.status is SellerLeadStatus.QUEUED
    assert not await SellerRepository(session).has_sequence_on_record(
        lead.id, ignore_reason="start_failed"
    )


# --------------------------------------------------------------------------------------------
# S3 sequence steps (POL-03d, SEL-05)
# --------------------------------------------------------------------------------------------


async def _prepare(seq: Sequence, step: int) -> oacts.PrepareStepResult:
    return await acts.seller_prepare_step(
        acts.SellerPrepareStepInput(
            sequence_id=str(seq.id), step=step, contact_email=SELLER, market="Houston"
        )
    )


async def _decide(prepared: oacts.PrepareStepResult, step: int) -> oacts.DecideAndSendResult:
    return await acts.seller_decide_and_send(
        oacts.DecideAndSendInput(message_id=prepared.message_id, step=step)
    )


async def test_prepare_step_writes_a_compliant_draft_once(session: AsyncSession) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, _ = await _sequence(session, lead, with_thread=False)
    first = await _prepare(seq, 0)
    again = await _prepare(seq, 0)
    assert first.created and not again.created and first.message_id == again.message_id
    assert first.idempotency_key == f"seq:{seq.id}:step:0"
    assert first.policy_action == "seller_outreach_email" and first.template_key == "seller_initial"
    row = await session.get(Message, uuid.UUID(first.message_id))
    assert row is not None and row.draft and row.sent_at is None
    assert missing_compliance_blocks(row.body, SENDER) == []
    assert "Hi Dolores," in row.body and "1420 Pecan Grove Dr" in row.body
    assert row.meta["template_legal_review"] == "pending"
    assert row.meta["seller_lead_id"] == str(lead.id)
    thread = await session.get(Thread, row.thread_id)
    assert (
        thread is not None
        and thread.seller_lead_id == lead.id
        and thread.counterpart_address == SELLER
    )
    await session.refresh(seq)
    assert seq.thread_id == thread.id
    assert await _count(session, AuditEvent, event_type="outreach.step_prepared") == 1


async def test_l0_queues_every_step_with_a_qa_review_and_nothing_in_graph(
    session: AsyncSession, gw: MockGraphGateway
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, _ = await _sequence(session, lead, with_thread=False)
    prepared = await _prepare(seq, 0)
    decided = await _decide(prepared, 0)
    assert decided.decision == "queue_for_human" and not decided.sent and decided.level == "L0"
    assert decided.action == "seller_outreach_email" and decided.qa_verdict == "approve"
    assert await _count(session, QaReview) == 1
    assert gw.sent == [] and gw.drafts == {}
    row = await session.get(Message, uuid.UUID(prepared.message_id))
    assert row is not None and row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
    assert row.qa_review_id is not None
    # Retry: no second review.
    await _decide(prepared, 0)
    assert await _count(session, QaReview) == 1


async def test_l1_qa_approved_steps_send_in_one_conversation(
    session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(session, PolicyAction.SELLER_OUTREACH_EMAIL, AutonomyLevel.L1, daily_cap=200)
    market = await _market(session)
    lead = await _lead(session, market)
    seq, _ = await _sequence(session, lead, with_thread=False)
    conversations = set()
    for step in range(4):
        prepared = await _prepare(seq, step)
        decided = await _decide(prepared, step)
        assert decided.decision == "send" and decided.sent, step
        row = await session.get(Message, uuid.UUID(prepared.message_id))
        assert row is not None and row.sent_at is not None
        conversations.add(row.meta["conversation_id"])
        assert missing_compliance_blocks(row.body, SENDER) == []
    assert len(conversations) == 1 and len(gw.sent) == 4
    assert all(disclosure("NorthStack") in d["body"] for d in _sent_bodies(gw))
    assert await _count(session, QaReview) == 4


async def test_l1_daily_cap_counts_seller_sends_only(session: AsyncSession) -> None:
    await _set_level(session, PolicyAction.SELLER_OUTREACH_EMAIL, AutonomyLevel.L1, daily_cap=1)
    market = await _market(session)
    lead_a = await _lead(session, market)
    lead_b = await _lead(session, market, owner="Gerald Pike", email="gpike@example.test")
    seq_a, _ = await _sequence(session, lead_a, with_thread=False)
    seq_b, _ = await _sequence(session, lead_b, with_thread=False)
    first = await _decide(await _prepare(seq_a, 0), 0)
    assert first.decision == "send"
    second = await acts.seller_decide_and_send(
        oacts.DecideAndSendInput(message_id=(await _prepare(seq_b, 0)).message_id, step=0)
    )
    assert second.decision == "queue_for_human" and "daily cap 1 reached" in second.reason
    assert second.sends_today == 1


async def test_decide_refuses_a_draft_without_the_disclosure(session: AsyncSession) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, _ = await _sequence(session, lead, with_thread=False)
    prepared = await _prepare(seq, 0)
    row = await session.get(Message, uuid.UUID(prepared.message_id))
    assert row is not None
    row.body = "Hi Dolores, want to sell?"  # a template regression
    await session.commit()
    with pytest.raises(RuntimeError, match="SEL-05"):
        await _decide(prepared, 0)


async def test_qa_block_or_suppression_blocks(
    session: AsyncSession, provider: MockModelProvider
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, _ = await _sequence(session, lead, with_thread=False)
    provider.script(AgentId.A5, TaskKind.QA, parsed=qa("block", 0.99))
    blocked = await _decide(await _prepare(seq, 0), 0)
    assert blocked.decision == "blocked" and "QA blocked" in blocked.reason
    session.add(Suppression(channel=Channel.EMAIL, normalized_value=SELLER, reason="opt_out"))
    await session.commit()
    provider.script(AgentId.A5, TaskKind.QA, parsed=qa())
    suppressed = await _decide(await _prepare(seq, 1), 1)
    assert suppressed.decision == "blocked" and "suppressed" in suppressed.reason


async def test_send_approved_reappends_the_compliance_block_to_an_edit(
    session: AsyncSession, gw: MockGraphGateway
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, _ = await _sequence(session, lead, with_thread=False)
    prepared = await _prepare(seq, 0)
    await _decide(prepared, 0)
    result = await acts.seller_send_approved(
        oacts.SendApprovedInput(
            message_id=prepared.message_id,
            step=0,
            edited_body="Dolores - happy to talk about the house.",
            actor_id="aaron",
        )
    )
    assert result.sent and result.body_edited
    row = await session.get(Message, uuid.UUID(prepared.message_id))
    assert row is not None and row.body.startswith("Dolores - happy to talk")
    assert missing_compliance_blocks(row.body, SENDER) == []
    assert row.meta["human_approved"] is True and row.meta["approved_by"] == "aaron"
    assert len(gw.sent) == 1
    again = await acts.seller_send_approved(
        oacts.SendApprovedInput(message_id=prepared.message_id, step=0)
    )
    assert again.sent and not again.body_edited and len(gw.sent) == 1


async def test_send_approved_blocks_a_recipient_suppressed_meanwhile(session: AsyncSession) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, _ = await _sequence(session, lead, with_thread=False)
    prepared = await _prepare(seq, 0)
    await _decide(prepared, 0)
    session.add(Suppression(channel=Channel.EMAIL, normalized_value=SELLER, reason="opt_out"))
    await session.commit()
    result = await acts.seller_send_approved(
        oacts.SendApprovedInput(message_id=prepared.message_id, step=0)
    )
    assert not result.sent and result.blocked_reason == "recipient is suppressed"


async def test_shared_cancel_check_sees_seller_replies(session: AsyncSession) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, thread = await _sequence(session, lead)
    assert thread is not None
    clear = await oacts.check_cancel_conditions(
        oacts.CheckCancelInput(sequence_id=str(seq.id), contact_email=SELLER)
    )
    assert clear.cancel_reason is None
    await _inbound(session, thread, "Yes, let's talk.")
    found = await oacts.check_cancel_conditions(
        oacts.CheckCancelInput(sequence_id=str(seq.id), contact_email=SELLER)
    )
    assert found.cancel_reason == "reply"


# --------------------------------------------------------------------------------------------
# Inbound: classify (POL-03l, WF-21)
# --------------------------------------------------------------------------------------------


async def test_opt_out_suppresses_signals_and_marks_the_lead(
    session: AsyncSession, provider: MockModelProvider, temporal: MockTemporalGateway
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, thread = await _sequence(session, lead)
    assert thread is not None
    msg = await _inbound(session, thread, "Please remove me from your list.")
    provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=classify("opt_out"))
    result = await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    assert result.classification == "opt_out" and result.suppression_created
    assert result.signalled == "opt_out" and result.sequence_workflow_id == seq.temporal_workflow_id
    assert temporal.signals[seq.temporal_workflow_id][0][0] == "opt_out"
    assert await _count(session, Suppression, normalized_value=SELLER) == 1
    await session.refresh(lead)
    assert lead.status is SellerLeadStatus.SUPPRESSED
    await session.refresh(msg)
    assert msg.classification is not None and msg.classification.value == "opt_out"
    assert msg.meta["s4"]["signalled"] == "opt_out"
    # Replay: no second model call, no second signal.
    again = await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    assert again.already_classified
    assert len([r for r in provider.requests if r.metadata.task_kind is TaskKind.CLASSIFY]) == 1
    assert len(temporal.signals[seq.temporal_workflow_id]) == 1


async def test_reply_signals_the_sequence_and_marks_replied(
    session: AsyncSession, provider: MockModelProvider, temporal: MockTemporalGateway
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, thread = await _sequence(session, lead)
    assert thread is not None
    msg = await _inbound(session, thread, "Maybe, what would you pay?")
    provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=classify("reply", asks_offer=True))
    result = await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    assert result.classification == "reply" and result.asks_offer and result.warrants_intake
    signal, payload = temporal.signals[seq.temporal_workflow_id][0]
    assert signal == "reply" and payload[0].message_id == str(msg.id)
    await session.refresh(lead)
    assert lead.status is SellerLeadStatus.REPLIED
    assert await _count(session, AuditEvent, event_type="sequence.signalled") == 1


async def test_not_interested_kills_the_lead_without_a_draft(
    session: AsyncSession, provider: MockModelProvider
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    _, thread = await _sequence(session, lead)
    assert thread is not None
    msg = await _inbound(session, thread, "No thanks.")
    provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=classify("not_interested"))
    result = await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    assert result.classification == "not_interested" and not result.warrants_intake
    assert result.signalled == "reply"  # a real answer still stops the follow-ups
    await session.refresh(lead)
    assert lead.status is SellerLeadStatus.DEAD


async def test_auto_reply_never_cancels_and_calls_no_model(
    session: AsyncSession, provider: MockModelProvider, temporal: MockTemporalGateway
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, thread = await _sequence(session, lead)
    assert thread is not None
    msg = await _inbound(
        session,
        thread,
        "I am out of the office until Monday.",
        headers={"auto-submitted": "auto-replied"},
    )
    result = await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    assert result.is_auto_reply and result.signalled is None and not result.warrants_intake
    assert temporal.signals == {} and provider.requests == []
    check = await oacts.check_cancel_conditions(
        oacts.CheckCancelInput(sequence_id=str(seq.id), contact_email=SELLER)
    )
    assert check.cancel_reason is None  # INV-22 for sellers too


async def test_unknown_sender_is_not_a_seller(
    session: AsyncSession, provider: MockModelProvider
) -> None:
    thread = Thread(
        channel=Channel.EMAIL, external_thread_id="conv-x", counterpart_address="who@example.test"
    )
    session.add(thread)
    await session.commit()
    msg = await _inbound(session, thread, "Hello?", from_address="who@example.test")
    result = await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    assert result.skipped_reason == "not_seller" and provider.requests == []


async def test_sender_matched_to_lead_by_email_when_thread_is_new(
    session: AsyncSession, provider: MockModelProvider
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    await _sequence(session, lead)  # creates the thread with the seller as counterpart
    fresh = Thread(channel=Channel.EMAIL, external_thread_id="conv-new", counterpart_address=SELLER)
    session.add(fresh)
    await session.commit()
    msg = await _inbound(session, fresh, "Wrote you from my phone, yes interested.")
    provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=classify("reply"))
    result = await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    assert result.seller_lead_id == str(lead.id)
    await session.refresh(fresh)
    assert fresh.seller_lead_id == lead.id


# --------------------------------------------------------------------------------------------
# Inbound: intake (SEL-06, SEL-07, POL-03f, POL-09)
# --------------------------------------------------------------------------------------------


async def _classified_reply(
    session: AsyncSession,
    provider: MockModelProvider,
    gw: MockGraphGateway,
    *,
    body: str,
    asks_offer: bool = False,
    verified: bool = True,
) -> tuple[SellerLead, Sequence, Message]:
    market = await _market(session)
    lead = await _lead(session, market, verified=verified)
    seq, thread = await _sequence(session, lead)
    assert thread is not None
    msg = await _inbound(session, thread, body, gw=gw)
    provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=classify("reply", asks_offer=asks_offer))
    await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    return lead, seq, msg


async def test_partial_intake_asks_two_fields_with_photo_link_at_l0(
    session: AsyncSession,
    provider: MockModelProvider,
    gw: MockGraphGateway,
    temporal: MockTemporalGateway,
) -> None:
    lead, _seq, msg = await _classified_reply(session, provider, gw, body="Taxes are killing me.")
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract(motivation="behind on taxes"))
    result = await acts.seller_intake(
        acts.SellerIntakeInput(message_id=str(msg.id), workflow_id="seller-inbound:email:x")
    )
    assert result.missing_required == ["condition", "timeline"] and result.asked == [
        "condition",
        "timeline",
    ]
    assert result.changed_fields == ["motivation"] and not result.intake_complete
    assert (
        result.policy_decision == "queue_for_human" and not result.sent and result.photo_link_sent
    )
    assert result.qa_verdict == "approve" and result.draft_message_id is not None
    draft_row = await session.get(Message, uuid.UUID(result.draft_message_id))
    assert draft_row is not None and draft_row.policy_action == "seller_intake_reply"
    assert (
        draft_row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
        and draft_row.qa_review_id is not None
    )
    assert f"https://sell.example.test/sell?lead={lead.id}" in draft_row.body
    assert missing_compliance_blocks(draft_row.body, SENDER) == []
    assert draft_row.body.startswith("Hi Dolores,")
    assert draft_row.meta["temporal_workflow_id"] == "seller-inbound:email:x"
    assert draft_row.meta["reply_to_message_id"] == str(msg.id) and draft_row.meta["asks_for"] == [
        "condition",
        "timeline",
    ]
    assert (
        draft_row.meta["conversation_id"]
        == (await session.get(Thread, draft_row.thread_id)).external_thread_id
    )  # type: ignore[union-attr]
    assert gw.sent == [] and len(gw.drafts) == 1  # Graph draft left in Drafts for the human
    intake = (await session.execute(select(Intake))).scalar_one()
    assert intake.motivation == "behind on taxes" and intake.missing_required == [
        "condition",
        "timeline",
    ]
    assert intake.photo_link_sent_at is not None and not intake.intake_complete
    await session.refresh(lead)
    assert lead.status is SellerLeadStatus.INTAKE
    assert _starts(temporal, "OfferWorkflow") == []
    assert await _count(session, AuditEvent, event_type=acts.EVENT_INTAKE_REPLY_DECIDED) == 1
    # Replay: same draft, no new model calls.
    before = len(provider.requests)
    again = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert again.already_processed and again.draft_message_id == result.draft_message_id
    assert again.asked == ["condition", "timeline"] and len(provider.requests) == before
    assert len(gw.drafts) == 1


async def test_complete_intake_starts_offer_workflow_and_signals_the_sequence(
    session: AsyncSession,
    provider: MockModelProvider,
    gw: MockGraphGateway,
    temporal: MockTemporalGateway,
) -> None:
    lead, seq, msg = await _classified_reply(session, provider, gw, body="Everything you asked.")
    provider.script(
        AgentId.S4,
        TaskKind.EXTRACT,
        parsed=extract(
            motivation="inherited",
            condition_class="Medium",
            disclosed_issues=["roof"],
            timeline="within_30_days",
            asking_price=185000,
            co_owners="just me",
            occupancy="vacant",
            liens="none",
            hoa="none",
            photos_mentioned=True,
        ),
    )
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.intake_complete and result.completed_now and result.missing_required == []
    assert result.draft_message_id is None and gw.drafts == {}  # A7 answers, not S4
    intake = (await session.execute(select(Intake))).scalar_one()
    assert intake.intake_complete and intake.missing_required == []
    assert intake.completed_at is not None and intake.completed_at.replace(tzinfo=UTC) == NOW
    assert intake.condition_class.value == "Medium" and intake.asking_price == Decimal("185000")  # type: ignore[union-attr]
    assert intake.co_owners_unknown is False and intake.liens_unknown is False
    # offer.requested: OfferWorkflow started by the deterministic id with the agreed input.
    starts = _starts(temporal, "OfferWorkflow")
    assert len(starts) == 1
    assert starts[0].args["workflow_id"] == f"offer:{intake.id}" == result.offer_workflow_id
    payload = starts[0].args["args"][0]
    assert payload.intake_id == str(intake.id) and payload.seller_lead_id == str(lead.id)
    # intake_complete signal to the seller sequence (WF-03).
    assert result.signalled_intake_complete
    signals = [s for s in temporal.signals[seq.temporal_workflow_id] if s[0] == "intake_complete"]
    assert len(signals) == 1 and signals[0][1][0].intake_id == str(intake.id)
    assert await _count(session, AuditEvent, event_type=acts.EVENT_INTAKE_COMPLETED) == 1
    assert await _count(session, AuditEvent, event_type=acts.EVENT_OFFER_REQUESTED) == 1
    # Replay: nothing starts or signals twice.
    again = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert again.already_processed and again.intake_complete and not again.completed_now
    assert len(_starts(temporal, "OfferWorkflow")) == 1
    assert len(temporal.signals[seq.temporal_workflow_id]) == 2  # reply + intake_complete


async def test_completion_over_two_messages(
    session: AsyncSession,
    provider: MockModelProvider,
    gw: MockGraphGateway,
    temporal: MockTemporalGateway,
) -> None:
    _lead_row, _seq, first = await _classified_reply(session, provider, gw, body="Taxes.")
    provider.script(
        AgentId.S4, TaskKind.EXTRACT, parsed=extract(motivation="taxes", timeline="asap")
    )
    r1 = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(first.id)))
    assert r1.missing_required == ["condition"] and r1.asked == ["condition", "co_owners"]
    thread = await session.get(Thread, first.thread_id)
    assert thread is not None
    second = await _inbound(
        session, thread, "Needs a roof, otherwise fine. Just me on title.", gw=gw
    )
    provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=classify("reply"))
    await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(second.id)))
    provider.script(
        AgentId.S4,
        TaskKind.EXTRACT,
        parsed=extract(condition_class="Medium", disclosed_issues=["roof"], co_owners="just me"),
    )
    r2 = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(second.id)))
    assert r2.intake_complete and r2.missing_required == []
    intake = (await session.execute(select(Intake))).scalar_one()
    assert (
        intake.motivation == "taxes"
        and intake.disclosed_issues == ["roof"]
        and intake.co_owners == "just me"
    )
    assert (intake.property_facts or {}).get("asked_optional") == ["co_owners"]
    assert len(_starts(temporal, "OfferWorkflow")) == 1


async def test_price_question_queues_even_at_l1(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    await _set_level(session, PolicyAction.SELLER_INTAKE_REPLY, AutonomyLevel.L1)
    _, _, msg = await _classified_reply(
        session, provider, gw, body="What's your offer?", asks_offer=True
    )
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract())
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.contains_sensitive and result.policy_decision == "queue_for_human"
    assert "POL-09" in (result.policy_reason or "") and result.policy_level == "L1"
    assert gw.sent == []


async def test_l1_clean_intake_reply_auto_sends(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    await _set_level(session, PolicyAction.SELLER_INTAKE_REPLY, AutonomyLevel.L1)
    _, _, msg = await _classified_reply(session, provider, gw, body="Taxes.")
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract(motivation="taxes"))
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.policy_decision == "send" and result.sent
    assert len(gw.sent) == 1
    sent = _sent_bodies(gw)[0]
    assert sent["conversation_id"] == (await session.get(Thread, msg.thread_id)).external_thread_id  # type: ignore[union-attr]
    assert missing_compliance_blocks(sent["body"], SENDER) == []
    assert await _count(session, Message, policy_action="seller_intake_reply") == 1


async def test_qa_block_leaves_nothing_in_the_mailbox(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    _, _, msg = await _classified_reply(session, provider, gw, body="Taxes.")
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract(motivation="taxes"))
    provider.script(AgentId.A5, TaskKind.QA, parsed=qa("block", 0.99))
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.blocked and gw.drafts == {} and gw.sent == []
    row = await session.get(Message, uuid.UUID(result.draft_message_id))  # type: ignore[arg-type]
    assert (
        row is not None
        and row.meta["blocked"] is True
        and row.policy_decision is PolicyDecision.BLOCKED
    )
    assert result.photo_link_sent is False


async def test_qa_rewrite_keeps_the_compliance_block(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    _, _, msg = await _classified_reply(session, provider, gw, body="Taxes.")
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract(motivation="taxes"))
    provider.script(AgentId.A5, TaskKind.QA, parsed=qa("rewrite", 0.9))
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.qa_verdict == "rewrite" and result.policy_decision == "queue_for_human"
    assert result.body is not None and result.body.startswith("Shorter.")
    assert missing_compliance_blocks(result.body, SENDER) == []


async def test_unverified_address_is_asked_and_resolves_when_known(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    lead, _, msg = await _classified_reply(
        session, provider, gw, body="Yes, 1402 Pecan Grove Dr is mine.", verified=False
    )
    provider.script(
        AgentId.S4, TaskKind.EXTRACT, parsed=extract(property_address="1402 Pecan Grove Dr")
    )
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert not result.parcel_verified and result.missing_required[0] == "address"
    assert result.asked == ["address", "motivation"]
    thread = await session.get(Thread, msg.thread_id)
    assert thread is not None
    fix = await _inbound(session, thread, "Sorry, 1420 Pecan Grove Dr.", gw=gw)
    provider.script(AgentId.S4, TaskKind.CLASSIFY, parsed=classify("reply"))
    await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(fix.id)))
    provider.script(
        AgentId.S4,
        TaskKind.EXTRACT,
        parsed=extract(property_address="1420 Pecan Grove Dr, Houston"),
    )
    fixed = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(fix.id)))
    assert fixed.parcel_verified and "address" not in fixed.missing_required
    await session.refresh(lead)
    assert lead.parcel_id == "HCAD-D-0000001" and lead.parcel_verified
    assert await _count(session, AuditEvent, event_type=acts.EVENT_PARCEL_VERIFIED) == 1


async def test_intake_without_any_email_is_recorded_unreachable(
    session: AsyncSession, provider: MockModelProvider
) -> None:
    market = await _market(session)
    lead = await _lead(session, market, email=None)
    _, thread = await _sequence(session, lead)
    assert thread is not None
    msg = Message(
        thread_id=thread.id,
        direction=Direction.IN,
        channel=Channel.EMAIL,
        external_message_id="<nofrom@example.test>",
        from_address=None,
        to_addresses=[MAILBOX],
        body="hi",
        received_at=NOW,
        meta={"s4": {"classification": "reply"}},
    )
    session.add(msg)
    await session.commit()
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract())
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.skipped_reason == "no_email" and result.draft_message_id is None
    assert await _count(session, AuditEvent, event_type=acts.EVENT_LEAD_UNREACHABLE) == 1


# --------------------------------------------------------------------------------------------
# Web form source (SEL-08, SEL-12)
# --------------------------------------------------------------------------------------------


async def _form_message(
    session: AsyncSession, lead: SellerLead, payload: dict[str, Any]
) -> Message:
    repo = SellerRepository(session)
    thread = await repo.create_thread(
        lead=lead, channel=Channel.EMAIL, counterpart_address=payload["email"], subject="form"
    )
    msg, _ = await repo.create_form_message(
        thread=thread,
        submission_id=payload["submission_id"],
        from_address=payload["email"],
        body="form",
        payload=payload,
        received_at=NOW,
    )
    await session.commit()
    return msg


async def test_web_form_submission_runs_s4_without_an_extract_call(
    session: AsyncSession,
    provider: MockModelProvider,
    gw: MockGraphGateway,
    temporal: MockTemporalGateway,
) -> None:
    market = await _market(session)
    lead = await _lead(session, market, verified=False, status=SellerLeadStatus.INTAKE)
    payload = {
        "submission_id": "sub-1",
        "email": SELLER,
        "address_line1": "1420 Pecan Grove Dr",
        "city": "Houston",
        "state": "TX",
        "postal_code": "77009",
        "motivation": "Relocating",
        "condition_class": "Light",
        "disclosed_issues": "",
        "timeline": "",
        "asking_price": "",
        "photos": ["sell/sub-1/01.jpg"],
    }
    msg = await _form_message(session, lead, payload)
    classified = await acts.seller_classify_inbound(
        acts.SellerClassifyInput(message_id=str(msg.id))
    )
    assert (
        classified.source == "web_form"
        and classified.classification == "reply"
        and provider.requests == []
    )
    result = await acts.seller_intake(
        acts.SellerIntakeInput(message_id=str(msg.id), workflow_id="seller-inbound:form:sub-1")
    )
    assert (
        result.source == "web_form" and result.parcel_verified
    )  # resolved against the county fixture
    assert result.missing_required == ["timeline"] and result.asked == ["timeline", "co_owners"]
    assert not [r for r in provider.requests if r.metadata.task_kind is TaskKind.EXTRACT]
    assert result.policy_decision == "queue_for_human"
    draft_row = await session.get(Message, uuid.UUID(result.draft_message_id))  # type: ignore[arg-type]
    assert (
        draft_row is not None
        and draft_row.to_addresses == [SELLER]
        and draft_row.channel is Channel.EMAIL
    )
    assert (
        draft_row.meta.get("reply_to_graph_message_id") is None
    )  # a new conversation: no Graph item to reply to
    assert missing_compliance_blocks(draft_row.body, SENDER) == []
    assert "/sell?lead=" in draft_row.body  # the form had no photos on the intake row itself
    assert _starts(temporal, "OfferWorkflow") == []


async def test_complete_web_form_requests_an_offer(
    session: AsyncSession, temporal: MockTemporalGateway
) -> None:
    market = await _market(session)
    lead = await _lead(session, market, verified=False, status=SellerLeadStatus.INTAKE)
    payload = {
        "submission_id": "sub-2",
        "email": SELLER,
        "address_line1": "1420 Pecan Grove Dr",
        "city": "Houston",
        "state": "TX",
        "postal_code": "77009",
        "motivation": "Relocating",
        "condition_class": "Light",
        "disclosed_issues": "",
        "timeline": "asap",
        "asking_price": "190000",
        "photos": [],
    }
    msg = await _form_message(session, lead, payload)
    await acts.seller_classify_inbound(acts.SellerClassifyInput(message_id=str(msg.id)))
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.intake_complete and result.offer_workflow_id == f"offer:{result.intake_id}"
    assert (
        len(_starts(temporal, "OfferWorkflow")) == 1 and not result.signalled_intake_complete
    )  # no sequence to tell


# --------------------------------------------------------------------------------------------
# Human decision, bounce
# --------------------------------------------------------------------------------------------


async def _queued_reply(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> tuple[Message, acts.SellerIntakeResult]:
    _, _, msg = await _classified_reply(session, provider, gw, body="Taxes.")
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract(motivation="taxes"))
    result = await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    assert result.queued
    return msg, result


async def test_human_approval_sends_the_queued_reply(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    msg, queued = await _queued_reply(session, provider, gw)
    applied = await acts.seller_apply_human_decision(
        acts.SellerHumanDecisionInput(message_id=str(msg.id), approved=True, decided_by="aaron")
    )
    assert (
        applied.sent and not applied.edited and applied.draft_message_id == queued.draft_message_id
    )
    assert len(gw.sent) == 1
    again = await acts.seller_apply_human_decision(
        acts.SellerHumanDecisionInput(message_id=str(msg.id), approved=True)
    )
    assert again.already_decided and len(gw.sent) == 1
    assert await _count(session, AuditEvent, event_type="reply.approved") == 1


async def test_human_edit_creates_a_second_draft_with_the_compliance_block(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    msg, queued = await _queued_reply(session, provider, gw)
    applied = await acts.seller_apply_human_decision(
        acts.SellerHumanDecisionInput(
            message_id=str(msg.id),
            approved=True,
            edited_body="Dolores, thanks - how is the roof?",
            decided_by="aaron",
        )
    )
    assert applied.sent and applied.edited and applied.draft_message_id != queued.draft_message_id
    sent_row = await session.get(Message, uuid.UUID(applied.draft_message_id))  # type: ignore[arg-type]
    assert (
        sent_row is not None
        and sent_row.body.startswith("Dolores, thanks")
        and missing_compliance_blocks(sent_row.body, SENDER) == []
    )
    assert (
        sent_row.meta["edited_by_human"] is True and sent_row.policy_action == "seller_intake_reply"
    )
    original = await session.get(Message, uuid.UUID(queued.draft_message_id))  # type: ignore[arg-type]
    assert original is not None and original.meta["superseded_by"] == applied.draft_message_id


async def test_human_rejection_discards(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    msg, _ = await _queued_reply(session, provider, gw)
    applied = await acts.seller_apply_human_decision(
        acts.SellerHumanDecisionInput(message_id=str(msg.id), approved=False, decided_by="aaron")
    )
    assert applied.discarded and not applied.sent and gw.sent == []
    assert await _count(session, AuditEvent, event_type="reply.rejected") == 1


async def test_human_approval_respects_a_later_suppression(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    msg, _ = await _queued_reply(session, provider, gw)
    session.add(Suppression(channel=Channel.EMAIL, normalized_value=SELLER, reason="opt_out"))
    await session.commit()
    applied = await acts.seller_apply_human_decision(
        acts.SellerHumanDecisionInput(message_id=str(msg.id), approved=True)
    )
    assert applied.discarded and applied.reason == "suppressed" and gw.sent == []


async def test_bounce_signals_the_seller_sequence_once(
    session: AsyncSession, temporal: MockTemporalGateway
) -> None:
    market = await _market(session)
    lead = await _lead(session, market)
    seq, thread = await _sequence(session, lead)
    assert thread is not None
    ndr = Message(
        thread_id=thread.id,
        direction=Direction.IN,
        channel=Channel.EMAIL,
        external_message_id="<ndr@example.test>",
        from_address="postmaster@example.test",
        to_addresses=[MAILBOX],
        body="Undeliverable",
        received_at=NOW,
        meta={"is_bounce": True, "bounced_address": SELLER},
    )
    session.add(ndr)
    await session.commit()
    result = await acts.seller_notify_bounce(acts.SellerBounceInput(message_id=str(ndr.id)))
    assert result.signalled and result.sequence_workflow_id == seq.temporal_workflow_id
    again = await acts.seller_notify_bounce(acts.SellerBounceInput(message_id=str(ndr.id)))
    assert again.signalled and len(temporal.signals[seq.temporal_workflow_id]) == 1
    classified = await acts.seller_classify_inbound(
        acts.SellerClassifyInput(message_id=str(ndr.id))
    )
    assert classified.skipped_reason == "bounce"


async def test_every_model_call_is_logged(
    session: AsyncSession, provider: MockModelProvider, gw: MockGraphGateway
) -> None:
    _, _, msg = await _classified_reply(session, provider, gw, body="Taxes.")
    provider.script(AgentId.S4, TaskKind.EXTRACT, parsed=extract(motivation="taxes"))
    await acts.seller_intake(acts.SellerIntakeInput(message_id=str(msg.id)))
    agents = [r.agent for r in (await session.execute(select(AgentRun))).scalars()]
    assert sorted(a.value for a in agents) == [
        "A5",
        "S4",
        "S4",
        "S4",
    ]  # classify, extract, draft + QA
