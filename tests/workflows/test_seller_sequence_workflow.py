"""SellerSequenceWorkflow end to end under the Temporal time-skipping test server with the mock
Graph gateway, the fixture providers and SQLite (REQUIREMENTS.md SEL-04, SEL-05, WF-03, WF-21).

Marked ``temporal``: skips when the test-server binary cannot be downloaded (sandbox); runs in CI.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from wholesaling.activities import graph as gacts
from wholesaling.activities import seller as sacts
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.agents.templates.seller_outreach import missing_compliance_blocks
from wholesaling.config.settings import Settings
from wholesaling.db.models import Message
from wholesaling.db.models.enums import (
    Direction,
    MarketTier,
    SellerLeadStatus,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Sequence
from wholesaling.db.models.sellers import SellerLead
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.workers.main import build_worker
from wholesaling.workflows.seller_sequence import (
    HumanDecision,
    SellerSequenceInput,
    SellerSequenceResult,
    SellerSequenceWorkflow,
)

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-seller-sequence"
MAILBOX = "deals@example.test"
CONTACT = "dolores.ramirez@example.test"
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)  # Tuesday 10:00 Chicago
SENDER = SenderIdentity(
    name="Aaron",
    company="NorthStack",
    postal_address="123 Main St, Houston, TX 77002",
    opt_out_text=(
        'If you would rather not hear from us, reply with "unsubscribe" and we will remove you.'
    ),
)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        graph_mailbox=MAILBOX,
        sender_postal_address="123 Main St, Houston, TX 77002",
    )


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
def gw() -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=lambda: NOW)


@pytest.fixture
async def ctx(
    gw: MockGraphGateway, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[sacts.SellerContext]:
    settings = _settings()
    gacts.set_graph_context(
        gacts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=settings, clock=lambda: NOW
        )
    )
    seller_ctx = sacts.SellerContext(
        session_factory=session_factory,
        providers=mock_data_providers(),
        temporal=MockTemporalGateway(),
        settings=settings,
        clock=lambda: NOW,
    )
    sacts.set_seller_context(seller_ctx)
    try:
        yield seller_ctx
    finally:
        sacts.set_seller_context(None)
        gacts.set_graph_context(None)


@pytest.fixture
async def worker(env: WorkflowEnvironment) -> AsyncIterator[Worker]:
    w = build_worker(env.client, TASK_QUEUE)
    async with w:
        yield w


@pytest.fixture
async def seq(session: AsyncSession) -> Sequence:
    market = Market(name="Houston", state="TX", tier=MarketTier.ACTIVE)
    session.add(market)
    await session.flush()
    lead = SellerLead(
        market_id=market.id,
        status=SellerLeadStatus.QUEUED,
        owner_name="Dolores Ramirez",
        property_address_line1="1420 Pecan Grove Dr",
        property_city="Houston",
        property_state="TX",
        property_postal_code="77009",
        parcel_id="HCAD-D-0000001",
        parcel_verified=True,
        signals=["absentee", "tax_delinquent"],
        signal_score=40,
        assessed_value=Decimal("165000"),
        contact_channels=[
            {"channel": "email", "value": CONTACT, "normalized_value": CONTACT, "is_primary": True}
        ],
    )
    session.add(lead)
    await session.flush()
    seq_id = uuid.uuid4()
    row, _ = await SequenceRepository(session).create(
        kind=SequenceKind.SELLER,
        seller_lead_id=lead.id,
        temporal_workflow_id=sacts.seller_sequence_workflow_id(str(seq_id)),
        sequence_id=seq_id,
    )
    await session.commit()
    return row


async def _level(session: AsyncSession, level: AutonomyLevel) -> None:
    await PolicyRepository(session).set_level(
        PolicyAction.SELLER_OUTREACH_EMAIL, level, updated_by="test", daily_cap=200
    )
    await session.commit()


def _input(seq: Sequence) -> SellerSequenceInput:
    assert seq.seller_lead_id is not None
    return SellerSequenceInput(
        sequence_id=str(seq.id),
        seller_lead_id=str(seq.seller_lead_id),
        contact_email=CONTACT,
        market="Houston",
    )


async def _start(
    env: WorkflowEnvironment, seq: Sequence
) -> WorkflowHandle[SellerSequenceWorkflow, SellerSequenceResult]:
    return await env.client.start_workflow(
        SellerSequenceWorkflow.run, _input(seq), id=seq.temporal_workflow_id, task_queue=TASK_QUEUE
    )


async def _wait_for(
    handle: WorkflowHandle[Any, Any],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    max_wait: float = 30.0,
) -> dict[str, Any]:
    async with asyncio.timeout(max_wait):
        while True:
            status: dict[str, Any] = await handle.query(SellerSequenceWorkflow.status)
            if predicate(status):
                return status
            await asyncio.sleep(0.05)


def _awaiting(step: int) -> Callable[[dict[str, Any]], bool]:
    return lambda s: s["state"] == "awaiting_human" and s["pending_human_step"] == step


def _sent(*steps: int) -> Callable[[dict[str, Any]], bool]:
    return lambda s: s["steps_sent"] == list(steps) and s["state"] in ("sleeping", "done")


async def _outbound(session: AsyncSession) -> list[Message]:
    stmt = select(Message).where(Message.direction == Direction.OUT).order_by(Message.sequence_step)
    return list((await session.execute(stmt)).scalars().all())


async def test_four_steps_one_conversation_at_3_7_14_days_with_l0_approvals(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: sacts.SellerContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    await _level(session, AutonomyLevel.L0)
    handle = await _start(env, seq)
    for step, days in zip(range(4), (3, 7, 14, None), strict=True):
        await _wait_for(handle, _awaiting(step))
        await handle.signal(
            SellerSequenceWorkflow.human_decision,
            HumanDecision(step=step, approved=True, actor_id="aaron"),
        )
        if days is not None:
            await _wait_for(handle, _sent(*range(step + 1)))
            await env.sleep(timedelta(days=days, minutes=1))
    result = await handle.result()
    assert result.completed and result.steps_sent == [0, 1, 2, 3]
    assert len(gw.sent) == 4
    assert len({d["conversation_id"] for d in gw.drafts.values()}) == 1
    rows = await _outbound(session)
    assert [r.sequence_step for r in rows] == [0, 1, 2, 3]
    assert all(missing_compliance_blocks(r.body, SENDER) == [] for r in rows)
    assert all(r.policy_action == "seller_outreach_email" for r in rows)
    await session.refresh(seq)
    assert seq.status is SequenceStatus.COMPLETED and seq.step == 4


async def test_intake_complete_signal_cancels_remaining_steps_at_l2(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: sacts.SellerContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    await _level(session, AutonomyLevel.L2)
    handle = await _start(env, seq)
    await _wait_for(handle, _sent(0))
    await env.sleep(timedelta(days=3, minutes=1))
    await _wait_for(handle, _sent(0, 1))
    await handle.signal(
        SellerSequenceWorkflow.intake_complete,
        sacts.IntakeCompleteSignal(intake_id="i1", seller_lead_id=str(seq.seller_lead_id)),
    )
    result = await handle.result()
    assert (
        not result.completed
        and result.cancel_reason == "intake_complete"
        and result.steps_sent == [0, 1]
    )
    assert len(gw.sent) == 2
    await session.refresh(seq)
    assert seq.status is SequenceStatus.CANCELLED and seq.cancel_reason == "intake_complete"


async def test_opt_out_while_awaiting_human_cancels_and_a_late_approval_is_ignored(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: sacts.SellerContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    await _level(session, AutonomyLevel.L0)
    handle = await _start(env, seq)
    await _wait_for(handle, _awaiting(0))
    await handle.signal(SellerSequenceWorkflow.opt_out)
    result = await handle.result()
    assert result.cancel_reason == "opt_out" and result.steps_sent == [] and gw.sent == []
    rows = await _outbound(session)
    assert len(rows) == 1 and rows[0].sent_at is None  # the queued draft stays a local row
