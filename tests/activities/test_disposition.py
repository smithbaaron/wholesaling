"""Disposition activities on SQLite with the mock Graph gateway and a scripted A5 model
(REQUIREMENTS.md SEL-10, BB-09, POL-03k, WF-06, WF-23, XC-03, XC-05).

L0: every blast draft is queued for the review queue, nothing is sent. L1: a QA-approved draft
for a *signed* contract sends; an unsigned contract never drafts anything. Retries never create a
second draft, review, send or alert. Nothing here touches DocuSign or contract status.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.disposition_seed import (
    OPERATOR,
    seed_accepted_offer,
    seed_approved_match,
    seed_investor,
    seed_psa,
)
from wholesaling.activities import conversation as conv
from wholesaling.activities import disposition as acts
from wholesaling.activities import graph as gacts
from wholesaling.activities.graph import ALERT_EVENT
from wholesaling.agents.a4 import MODEL_OVERRIDES
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, Message, QaReview, Suppression
from wholesaling.db.models.deals import ContractStatus, EarnestMoneyStatus
from wholesaling.db.models.enums import Actor, AgentId, Channel, PolicyDecision
from wholesaling.db.repositories.contracts import ContractRepository
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.db.repositories.review import META_WORKFLOW_ID_KEY
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

MAILBOX = "deals@example.test"
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
BOX = {
    "price_min": Decimal("100000"),
    "price_max": Decimal("250000"),
    "geography": {"zips": ["77092"]},
}


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
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gw(clock: FakeClock) -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=clock)


@pytest.fixture
def provider() -> MockModelProvider:
    p = MockModelProvider()
    p.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "approve",
            "confidence": 0.95,
            "flags": [],
            "rewrite_body": None,
            "rationale": "ok",
        },
    )
    return p


@pytest.fixture
async def ctx(
    gw: MockGraphGateway,
    clock: FakeClock,
    provider: MockModelProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[acts.DispositionContext]:
    settings = _settings()
    gctx = gacts.GraphActivityContext(
        gateway=gw, session_factory=session_factory, settings=settings, clock=clock
    )
    gacts.set_graph_context(gctx)
    conv.set_conversation_context(
        conv.ConversationContext(
            session_factory=session_factory,
            runner=AgentRunner(provider, ModelRouter.from_settings(settings, MODEL_OVERRIDES)),
            temporal=MockTemporalGateway(),
            settings=settings,
            clock=clock,
        )
    )
    acts.set_disposition_context(None)  # derive from the graph context on first use
    try:
        yield acts.get_disposition_context()
    finally:
        acts.set_disposition_context(None)
        conv.set_conversation_context(None)
        gacts.set_graph_context(None)


async def _seed_signed_deal(
    session: AsyncSession, *, buyers: int = 2, signed: bool = True
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    deal = await seed_accepted_offer(session)
    psa = await seed_psa(session, deal, signed=signed, closing_date=NOW + timedelta(days=30))
    ids: list[uuid.UUID] = []
    for i in range(buyers):
        inv = await seed_investor(
            session,
            name=f"Buyer {i} LLC",
            email=f"buyer{i}@x.com",
            market=deal.market,
            score=90 - i,
            first_name="Jane",
            buy_box=BOX,
        )
        if i == 0:
            await seed_approved_match(session, investor=inv, deal=deal)
        ids.append(inv.id)
    await session.commit()
    return psa.id, ids


async def _set_level(session: AsyncSession, level: AutonomyLevel) -> None:
    await PolicyRepository(session).set_level(
        PolicyAction.BUYER_DISPOSITION_BLAST, level, updated_by="test"
    )
    await session.commit()


async def _count(session: AsyncSession, model: type, **where: object) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int((await session.execute(stmt)).scalar_one())


# -- run_blast ------------------------------------------------------------------------------------


async def test_l0_blast_queues_every_draft_and_sends_nothing(
    ctx: acts.DispositionContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    contract_id, investors = await _seed_signed_deal(session)
    result = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    assert result.deal_approved and result.level == "L0"
    assert len(result.recipients) == 2 and result.queued == 2 and result.sent == 0
    assert [r.tier for r in result.recipients] == [1, 2]  # approved match first
    assert gw.sent == []
    rows = (await session.execute(select(Message).order_by(Message.created_at))).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert row.draft and row.sent_at is None
        assert row.policy_action == PolicyAction.BUYER_DISPOSITION_BLAST.value
        assert row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
        assert row.meta[META_WORKFLOW_ID_KEY] == f"disposition:{contract_id}"
        assert row.meta["contract_id"] == str(contract_id)
        assert row.external_message_id == acts.blast_idempotency_key(
            str(contract_id), row.meta["investor_id"]
        )
        assert "under contract" in row.body and "unsubscribe" in row.body
        assert "seller has agreed to" not in row.body.lower().replace("agreed only", "")
    assert await _count(session, QaReview) == 2
    events = {e.event_type for e in (await session.execute(select(AuditEvent))).scalars().all()}
    assert {acts.EVENT_BLAST_PREPARED, acts.EVENT_BLAST_QA, acts.EVENT_BLAST_DECIDED} <= events
    assert set(investors) == {uuid.UUID(r.investor_id) for r in result.recipients}


async def test_l1_blast_sends_qa_approved_drafts_for_a_signed_deal(
    ctx: acts.DispositionContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    await _set_level(session, AutonomyLevel.L1)
    contract_id, _ = await _seed_signed_deal(session)
    result = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    assert result.sent == 2 and result.queued == 0
    assert len(gw.sent) == 2
    rows = (await session.execute(select(Message))).scalars().all()
    assert all(r.sent_at is not None and not r.draft for r in rows)
    sent_events = await _count(session, AuditEvent, event_type="message.sent")
    assert sent_events == 2


async def test_blast_is_idempotent_on_retry(
    ctx: acts.DispositionContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    await _set_level(session, AutonomyLevel.L1)
    contract_id, _ = await _seed_signed_deal(session)
    first = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    second = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    assert first.sent == 2 and second.sent == 2
    assert all(r.reason == "already sent" and not r.created for r in second.recipients)
    assert len(gw.sent) == 2
    assert await _count(session, Message) == 2 and await _count(session, QaReview) == 2


async def test_unsigned_contract_drafts_nothing(
    ctx: acts.DispositionContext, session: AsyncSession
) -> None:
    """BB-09: only approved (signed) deals are described to buyers."""
    await _set_level(session, AutonomyLevel.L2)
    contract_id, _ = await _seed_signed_deal(session, signed=False)
    result = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    assert not result.deal_approved and result.recipients == []
    assert await _count(session, Message) == 0


async def test_suppressed_buyer_is_skipped(
    ctx: acts.DispositionContext, session: AsyncSession
) -> None:
    contract_id, investors = await _seed_signed_deal(session)
    session.add(
        Suppression(channel=Channel.EMAIL, normalized_value="buyer1@x.com", reason="opt_out")
    )
    await session.commit()
    result = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    assert len(result.recipients) == 1 and result.skipped_suppressed == [str(investors[1])]


async def test_qa_block_blocks_the_draft_even_at_l2(
    ctx: acts.DispositionContext,
    provider: MockModelProvider,
    gw: MockGraphGateway,
    session: AsyncSession,
) -> None:
    provider.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "block",
            "confidence": 0.9,
            "flags": ["invented_deal"],
            "rewrite_body": None,
            "rationale": "no",
        },
    )
    await _set_level(session, AutonomyLevel.L2)
    contract_id, _ = await _seed_signed_deal(session, buyers=1)
    result = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    assert result.blocked == 1 and gw.sent == []


async def test_blast_limit_and_match_only(
    ctx: acts.DispositionContext, session: AsyncSession
) -> None:
    contract_id, investors = await _seed_signed_deal(session, buyers=3)
    result = await acts.run_blast(
        acts.RunBlastInput(contract_id=str(contract_id), include_fallback=False)
    )
    assert [r.investor_id for r in result.recipients] == [str(investors[0])]


# -- send_approved_blast --------------------------------------------------------------------------


async def test_send_approved_blast_sends_queued_draft_and_keeps_footer(
    ctx: acts.DispositionContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    contract_id, _ = await _seed_signed_deal(session, buyers=1)
    blast = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    message_id = blast.recipients[0].message_id
    out = await acts.send_approved_blast(
        acts.SendApprovedBlastInput(
            message_id=message_id, actor_id=OPERATOR, edited_body="Shorter pitch, same deal."
        )
    )
    assert out.sent and len(gw.sent) == 1
    row = await session.get(Message, uuid.UUID(message_id))
    assert row is not None and row.sent_at is not None
    assert row.body.startswith("Shorter pitch") and "unsubscribe" in row.body
    assert row.meta["approved_by"] == OPERATOR and "original_body" in row.meta
    again = await acts.send_approved_blast(
        acts.SendApprovedBlastInput(message_id=message_id, actor_id=OPERATOR)
    )
    assert again.sent and len(gw.sent) == 1
    assert await _count(session, AuditEvent, event_type=acts.EVENT_BLAST_HUMAN_APPROVED) == 1


async def test_send_approved_blast_honours_suppression(
    ctx: acts.DispositionContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    contract_id, _ = await _seed_signed_deal(session, buyers=1)
    blast = await acts.run_blast(acts.RunBlastInput(contract_id=str(contract_id)))
    session.add(
        Suppression(channel=Channel.EMAIL, normalized_value="buyer0@x.com", reason="opt_out")
    )
    await session.commit()
    out = await acts.send_approved_blast(
        acts.SendApprovedBlastInput(message_id=blast.recipients[0].message_id, actor_id=OPERATOR)
    )
    assert not out.sent and out.blocked_reason == "recipient is suppressed" and gw.sent == []


# -- record_buyer_response -------------------------------------------------------------------------


async def test_record_buyer_response_activity(
    ctx: acts.DispositionContext, session: AsyncSession
) -> None:
    contract_id, investors = await _seed_signed_deal(session)
    r1 = await acts.record_buyer_response(
        acts.RecordBuyerResponseInput(
            contract_id=str(contract_id),
            investor_id=str(investors[0]),
            response="interested",
            proof_of_funds=True,
            deposit_terms="5k/24h",
        )
    )
    r2 = await acts.record_buyer_response(
        acts.RecordBuyerResponseInput(
            contract_id=str(contract_id), investor_id=str(investors[1]), response="pass"
        )
    )
    assert r1.interested_count == 1 and r2.interested_count == 1
    row = await ContractRepository(session).require(contract_id)
    assert row.buyer_responses[str(investors[0])]["deposit_terms"] == "5k/24h"
    assert row.buyer_investor_id is None and row.status is ContractStatus.SIGNED


# -- alerts ----------------------------------------------------------------------------------------


async def test_raise_deadline_alert_is_idempotent_and_audited(
    ctx: acts.DispositionContext, session: AsyncSession
) -> None:
    contract_id, _ = await _seed_signed_deal(session, buyers=0)
    inp = acts.DeadlineAlertInput(
        contract_id=str(contract_id),
        deadline="closing_date",
        label="T-3d",
        alert_key="closing_date:T-3d",
        deadline_at=(NOW + timedelta(days=30)).isoformat(),
    )
    first = await acts.raise_deadline_alert(inp)
    second = await acts.raise_deadline_alert(inp)
    assert first.created and not second.created and first.notified == ["log"]
    events = [
        e
        for e in (
            await session.execute(select(AuditEvent).where(AuditEvent.event_type == ALERT_EVENT))
        )
        .scalars()
        .all()
    ]
    assert len(events) == 1
    assert (
        events[0].entity_type == "contract"
        and events[0].payload["alert_key"] == "closing_date:T-3d"
    )
    assert (
        events[0].payload["kind"] == "deal_deadline"
        and "T-3d closing date" in events[0].payload["detail"]
    )
    snap = await acts.load_contract(acts.LoadContractInput(contract_id=str(contract_id)))
    assert snap.raised_alerts == ["closing_date:T-3d"] and snap.status == "signed"
    assert snap.deadlines["closing_date"] == (NOW + timedelta(days=30)).isoformat()


async def test_deadline_alert_posts_to_slack_when_configured(
    gw: MockGraphGateway,
    clock: FakeClock,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, text="ok")

    settings = _settings(slack_webhook_url="https://hooks.slack.test/abc")
    acts.set_disposition_context(
        acts.DispositionContext(
            session_factory=session_factory,
            settings=settings,
            gateway=gw,
            clock=clock,
            slack_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )
    try:
        contract_id, _ = await _seed_signed_deal(session, buyers=0)
        out = await acts.raise_deadline_alert(
            acts.DeadlineAlertInput(
                contract_id=str(contract_id),
                deadline="closing_date",
                label="T-0",
                alert_key="closing_date:T-0",
                deadline_at=NOW.isoformat(),
            )
        )
        assert out.notified == ["log", "slack"] and len(posted) == 1
        assert b"Deal deadline" in posted[0].content
    finally:
        acts.set_disposition_context(None)


async def test_deal_dead_followup_reminds_to_release_deposited_earnest_money(
    ctx: acts.DispositionContext, session: AsyncSession
) -> None:
    contract_id, _ = await _seed_signed_deal(session, buyers=0)
    repo = ContractRepository(session)
    await repo.set_earnest_money(
        contract_id,
        status=EarnestMoneyStatus.REQUESTED,
        amount=Decimal("2500"),
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    await repo.set_earnest_money(
        contract_id,
        status=EarnestMoneyStatus.DEPOSITED,
        amount=None,
        actor=Actor.HUMAN,
        actor_id=OPERATOR,
    )
    await repo.mark_dead(
        contract_id, reason="inspection", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW
    )
    await session.commit()
    out = await acts.deal_dead_followup(
        acts.DealDeadFollowupInput(contract_id=str(contract_id), reason="inspection")
    )
    assert out.created and out.release_reminder and out.earnest_money_status == "deposited"
    alerts = (
        (await session.execute(select(AuditEvent).where(AuditEvent.event_type == ALERT_EVENT)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1 and alerts[0].payload["kind"] == "earnest_money_release"
    again = await acts.deal_dead_followup(
        acts.DealDeadFollowupInput(contract_id=str(contract_id), reason="inspection")
    )
    assert not again.created
    # The activity never released the money itself (POL-03j).
    assert (await repo.require(contract_id)).earnest_money_status is EarnestMoneyStatus.DEPOSITED


async def test_record_workflow_state_mirrors_onto_meta(
    ctx: acts.DispositionContext, session: AsyncSession
) -> None:
    contract_id, _ = await _seed_signed_deal(session, buyers=0)
    assert await acts.record_workflow_state(
        acts.WorkflowStateInput(
            contract_id=str(contract_id),
            state="marketing",
            next_alert_key="closing_date:T-3d",
            next_alert_at=NOW.isoformat(),
            blast_sent=0,
            blast_queued=2,
        )
    )
    row = await ContractRepository(session).require(contract_id)
    await session.refresh(row)
    assert (
        row.meta["workflow"]["state"] == "marketing" and row.meta["workflow"]["blast_queued"] == 2
    )
    assert row.status is ContractStatus.SIGNED  # state mirror never changes status


async def test_workflow_id_helpers() -> None:
    assert acts.disposition_workflow_id("abc") == "disposition:abc"
    assert acts.blast_idempotency_key("c", "i") == "disp:c:i"
