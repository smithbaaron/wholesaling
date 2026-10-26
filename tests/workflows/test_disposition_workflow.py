"""DispositionWorkflow end to end under the Temporal time-skipping test server with SQLite, the
mock Graph gateway and a scripted A5 (REQUIREMENTS.md WF-06, SEL-10, WF-22).

Marked ``temporal``: skips when the test-server binary cannot be downloaded (sandbox); runs in CI.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment

from tests.disposition_seed import OPERATOR, seed_accepted_offer, seed_investor, seed_psa
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
from wholesaling.db.models import AuditEvent, Message
from wholesaling.db.models.deals import ContractStatus
from wholesaling.db.models.enums import Actor, AgentId
from wholesaling.db.repositories.contracts import ContractRepository
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.signals import DealClosedSignal, DealDeadSignal
from wholesaling.workers.main import build_worker
from wholesaling.workflows.disposition import (
    DispositionInput,
    DispositionResult,
    DispositionWorkflow,
    disposition_workflow_id,
)

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-disposition"
MAILBOX = "deals@example.test"
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    existing = os.environ.get("TEMPORAL_TEST_SERVER_PATH")
    try:
        environment = await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=existing
        )
    except Exception as exc:
        pytest.skip(f"Temporal time-skipping test server unavailable: {exc!r}")
    async with environment:
        yield environment


@pytest.fixture
async def ctx(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        graph_mailbox=MAILBOX,
        sender_postal_address="123 Main St, Houston, TX 77002",
    )
    gw = MockGraphGateway(mailbox=MAILBOX, clock=lambda: NOW)
    gacts.set_graph_context(
        gacts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=settings, clock=lambda: NOW
        )
    )
    provider = MockModelProvider()
    provider.script(
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
    conv.set_conversation_context(
        conv.ConversationContext(
            session_factory=session_factory,
            runner=AgentRunner(provider, ModelRouter.from_settings(settings, MODEL_OVERRIDES)),
            temporal=MockTemporalGateway(),
            settings=settings,
            clock=lambda: NOW,
        )
    )
    acts.set_disposition_context(None)
    try:
        yield
    finally:
        acts.set_disposition_context(None)
        conv.set_conversation_context(None)
        gacts.set_graph_context(None)


async def _seed(session: AsyncSession, *, closing_in_days: int) -> uuid.UUID:
    await PolicyRepository(session).set_level(
        PolicyAction.BUYER_DISPOSITION_BLAST, AutonomyLevel.L1, updated_by="test"
    )
    deal = await seed_accepted_offer(session)
    psa = await seed_psa(
        session, deal, signed=True, closing_date=NOW + timedelta(days=closing_in_days)
    )
    await seed_investor(
        session,
        name="Buyer LLC",
        email="buyer@x.com",
        market=deal.market,
        first_name="Jane",
        buy_box={"price_max": Decimal("250000"), "geography": {"zips": ["77092"]}},
    )
    await session.commit()
    return psa.id


async def test_blast_alerts_and_close(
    env: WorkflowEnvironment, ctx: None, session: AsyncSession
) -> None:
    contract_id = await _seed(session, closing_in_days=10)
    workflow_id = disposition_workflow_id(str(contract_id))
    async with build_worker(env.client, TASK_QUEUE, workflows=[DispositionWorkflow]):
        handle = await env.client.start_workflow(
            DispositionWorkflow.run,
            DispositionInput(contract_id=str(contract_id)),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        for _ in range(40):  # blast + state recorded
            status = await handle.query(DispositionWorkflow.status)
            if status["blast_sent"] == 1:
                break
            await asyncio.sleep(0.2)
        assert status["blast_sent"] == 1 and status["state"] == "marketing"
        await env.sleep(timedelta(days=8))  # past T-3d and T-1d? (T-3d at day 7, T-1d at day 9)
        for _ in range(40):
            status = await handle.query(DispositionWorkflow.status)
            if "closing_date:T-3d" in status["alerts_raised"]:
                break
            await asyncio.sleep(0.2)
        assert "closing_date:T-3d" in status["alerts_raised"]
        await env.sleep(timedelta(days=3))
        for _ in range(40):
            status = await handle.query(DispositionWorkflow.status)
            if "closing_date:T-0" in status["alerts_raised"]:
                break
            await asyncio.sleep(0.2)
        assert set(status["alerts_raised"]) == {
            "closing_date:T-3d",
            "closing_date:T-1d",
            "closing_date:T-0",
        }
        # The human closes on /deals (writes the row) and signals.
        repo = ContractRepository(session)
        inv = (await session.execute(select(Message))).scalars().first()
        assert inv is not None and inv.sent_at is not None
        await handle.signal(
            DispositionWorkflow.deal_closed, DealClosedSignal("15000", NOW.isoformat(), OPERATOR)
        )
        result: DispositionResult = await handle.result()
    assert result.outcome == "closed" and result.blast_sent == 1
    assert sorted(result.alerts_raised) == [
        "closing_date:T-0",
        "closing_date:T-1d",
        "closing_date:T-3d",
    ]
    alerts = (
        (await session.execute(select(AuditEvent).where(AuditEvent.event_type == ALERT_EVENT)))
        .scalars()
        .all()
    )
    assert len(alerts) == 3
    row = await repo.require(contract_id)
    await session.refresh(row)
    assert row.meta["workflow"]["state"] == "closed"
    assert (
        row.status is ContractStatus.SIGNED
    )  # the workflow never changes status; the human route does


async def test_deal_dead_path(env: WorkflowEnvironment, ctx: None, session: AsyncSession) -> None:
    contract_id = await _seed(session, closing_in_days=30)
    async with build_worker(env.client, TASK_QUEUE, workflows=[DispositionWorkflow]):
        handle = await env.client.start_workflow(
            DispositionWorkflow.run,
            DispositionInput(contract_id=str(contract_id)),
            id=disposition_workflow_id(str(contract_id)),
            task_queue=TASK_QUEUE,
        )
        for _ in range(40):
            status = await handle.query(DispositionWorkflow.status)
            if status["blast_sent"] == 1:
                break
            await asyncio.sleep(0.2)
        repo = ContractRepository(session)
        await repo.mark_dead(
            contract_id, reason="title defect", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW
        )
        await session.commit()
        await handle.signal(DispositionWorkflow.deal_dead, DealDeadSignal("title defect", OPERATOR))
        result: DispositionResult = await handle.result()
    assert (
        result.outcome == "dead" and result.reason == "title defect" and result.alerts_raised == []
    )
    followups = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.event_type == acts.EVENT_DEAL_DEAD_FOLLOWUP)
            )
        )
        .scalars()
        .all()
    )
    assert len(followups) == 1 and followups[0].payload["timers_cancelled"] is True
