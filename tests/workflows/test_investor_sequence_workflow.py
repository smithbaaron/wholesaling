"""InvestorSequenceWorkflow end to end under the Temporal time-skipping test server with the
mock Graph gateway and SQLite (REQUIREMENTS.md INV-10, INV-23, WF-02, WF-21, WF-22).

Marked ``temporal``: skips when the test-server binary cannot be downloaded (sandbox); runs in CI.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.db.conftest import MakeInvestor, make_investor
from wholesaling.activities import graph as gacts
from wholesaling.config.settings import Settings
from wholesaling.db.models import Investor, Message
from wholesaling.db.models.enums import (
    Channel,
    Direction,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.messaging import Sequence
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.workers.main import build_worker
from wholesaling.workflows.investor_sequence import (
    HumanDecision,
    InvestorSequenceInput,
    InvestorSequenceResult,
    InvestorSequenceWorkflow,
)

__all__ = ["make_investor"]

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-investor-sequence"
MAILBOX = "deals@example.test"
CONTACT = "jane@acme.com"
FOUR_DAYS = timedelta(days=4, minutes=1)
# Tuesday 10:00 America/Chicago: inside the default send window.
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
def gw() -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=lambda: NOW)


@pytest.fixture
async def ctx(
    gw: MockGraphGateway, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[gacts.GraphActivityContext]:
    context = gacts.GraphActivityContext(
        gateway=gw,
        session_factory=session_factory,
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="test",
            graph_mailbox=MAILBOX,
            sender_postal_address="123 Main St, Houston, TX 77002",
        ),
        clock=lambda: NOW,
    )
    gacts.set_graph_context(context)
    try:
        yield context
    finally:
        gacts.set_graph_context(None)


@pytest.fixture
async def worker(env: WorkflowEnvironment) -> AsyncIterator[Worker]:
    w = build_worker(env.client, TASK_QUEUE)
    async with w:
        yield w


@pytest.fixture
async def seq(session: AsyncSession, make_investor: MakeInvestor) -> Sequence:
    inv = await make_investor("Acme Holdings LLC", first_name="Jane")
    row, _ = await SequenceRepository(session).create(
        kind=SequenceKind.INVESTOR,
        investor_id=inv.id,
        temporal_workflow_id=f"investor-sequence:{uuid.uuid4()}",
    )
    await session.commit()
    return row


async def _levels(session: AsyncSession, level: AutonomyLevel) -> None:
    repo = PolicyRepository(session)
    await repo.set_level(
        PolicyAction.INVESTOR_INITIAL_OUTREACH, level, updated_by="test", daily_cap=50
    )
    await repo.set_level(PolicyAction.INVESTOR_FOLLOWUP, level, updated_by="test")
    await session.commit()


def _input(seq: Sequence) -> InvestorSequenceInput:
    assert seq.investor_id is not None
    return InvestorSequenceInput(
        sequence_id=str(seq.id),
        investor_id=str(seq.investor_id),
        contact_email=CONTACT,
        market="Dallas",
        intervals_days=[4, 4, 4],
    )


async def _start(
    env: WorkflowEnvironment, seq: Sequence
) -> WorkflowHandle[InvestorSequenceWorkflow, InvestorSequenceResult]:
    return await env.client.start_workflow(
        InvestorSequenceWorkflow.run,
        _input(seq),
        id=seq.temporal_workflow_id,
        task_queue=TASK_QUEUE,
    )


async def _wait_for(
    handle: WorkflowHandle[Any, Any],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    max_wait: float = 30.0,
) -> dict[str, Any]:
    """Poll ``status`` (real time; activities run at wall-clock speed) until ``predicate``."""

    async with asyncio.timeout(max_wait):
        while True:
            status: dict[str, Any] = await handle.query(InvestorSequenceWorkflow.status)
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


Approve = Callable[[int], Awaitable[None]]


async def test_four_messages_one_conversation_with_scripted_approvals_at_l1(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: gacts.GraphActivityContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    """INV-23: at L0 every step queues (the policy table, whatever QA says); a human approves
    each one in the review queue (the ``human_decision`` signal) and all four land in one
    conversation."""
    await _levels(session, AutonomyLevel.L0)
    handle = await _start(env, seq)
    for step in range(4):
        await _wait_for(handle, _awaiting(step))
        await handle.signal(
            InvestorSequenceWorkflow.human_decision,
            HumanDecision(step=step, approved=True, actor_id="aaron"),
        )
        if step < 3:
            await _wait_for(handle, _sent(*range(step + 1)))
            await env.sleep(FOUR_DAYS)
    result = await handle.result()
    assert result.completed and result.cancel_reason is None
    assert result.steps_sent == [0, 1, 2, 3]
    assert result.decisions == {str(s): "send" for s in range(4)}

    assert len(gw.sent) == 4
    assert len({d["conversation_id"] for d in gw.drafts.values()}) == 1, "one Outlook conversation"
    rows = await _outbound(session)
    assert [r.sequence_step for r in rows] == [0, 1, 2, 3]
    assert all(r.sent_at is not None and not r.draft for r in rows)
    assert len({r.thread_id for r in rows}) == 1
    assert {r.external_message_id for r in rows} == {f"seq:{seq.id}:step:{s}" for s in range(4)}
    assert all(r.meta["human_approved"] is True for r in rows)
    await session.refresh(seq)
    assert seq.status is SequenceStatus.COMPLETED and seq.step == 4
    status = await handle.query(InvestorSequenceWorkflow.status)
    assert status["state"] == "done"


async def test_reply_signal_after_step_1_cancels_remaining_steps(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: gacts.GraphActivityContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    """INV-23 acceptance: a reply cancels every future send."""
    await _levels(session, AutonomyLevel.L2)
    handle = await _start(env, seq)
    await _wait_for(handle, _sent(0))
    await env.sleep(FOUR_DAYS)
    await _wait_for(handle, _sent(0, 1))
    await handle.signal(InvestorSequenceWorkflow.reply)
    result = await handle.result()
    assert not result.completed and result.cancel_reason == "reply"
    assert result.steps_sent == [0, 1]
    assert len(gw.sent) == 2
    rows = await _outbound(session)
    assert [r.sequence_step for r in rows] == [0, 1]
    await session.refresh(seq)
    assert seq.status is SequenceStatus.CANCELLED and seq.cancel_reason == "reply"
    assert seq.next_send_at is None


async def test_inbound_reply_on_thread_cancels_without_a_signal(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: gacts.GraphActivityContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    """The check before each step reads the thread, so a reply persisted by the inbound path
    (webhook / delta poll) stops the sequence even if nobody signalled the workflow."""
    await _levels(session, AutonomyLevel.L2)
    handle = await _start(env, seq)
    await _wait_for(handle, _sent(0))
    await session.refresh(seq)
    assert seq.thread_id is not None
    session.add(
        Message(
            thread_id=seq.thread_id,
            direction=Direction.IN,
            channel=Channel.EMAIL,
            external_message_id=f"<{uuid.uuid4().hex}@acme.com>",
            from_address=CONTACT,
            body="Yes, we buy in Dallas. Send me what fits.",
        )
    )
    await session.commit()
    await env.sleep(FOUR_DAYS)
    result = await handle.result()
    assert result.cancel_reason == "reply" and result.steps_sent == [0]
    assert len(gw.sent) == 1


async def test_opt_out_during_review_cancels_and_pause_holds(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: gacts.GraphActivityContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    await _levels(session, AutonomyLevel.L2)
    handle = await _start(env, seq)
    await _wait_for(handle, _sent(0))
    await handle.signal(InvestorSequenceWorkflow.pause)
    with env.auto_time_skipping_disabled():
        await env.sleep(timedelta(days=10))
    status = await handle.query(InvestorSequenceWorkflow.status)
    assert status["paused"] is True and status["steps_sent"] == [0]
    assert len(gw.sent) == 1, "a paused sequence sends nothing"
    await handle.signal(InvestorSequenceWorkflow.opt_out)
    result = await handle.result()
    assert result.cancel_reason == "opt_out" and result.steps_sent == [0]
    await session.refresh(seq)
    assert seq.status is SequenceStatus.CANCELLED and seq.cancel_reason == "opt_out"


async def test_human_rejection_cancels_the_sequence(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: gacts.GraphActivityContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    handle = await _start(env, seq)  # L0 everywhere: step 0 queues
    await _wait_for(handle, _awaiting(0))
    assert len(gw.sent) == 0
    await handle.signal(
        InvestorSequenceWorkflow.human_decision, HumanDecision(step=0, approved=False)
    )
    result = await handle.result()
    assert result.cancel_reason == "human_rejected:step:0" and result.steps_sent == []
    assert result.decisions == {"0": "queue_for_human"}
    rows = await _outbound(session)
    assert len(rows) == 1 and rows[0].sent_at is None and rows[0].draft


async def test_edited_body_is_sent_with_footer(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: gacts.GraphActivityContext,
    gw: MockGraphGateway,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    handle = await _start(env, seq)
    await _wait_for(handle, _awaiting(0))
    await handle.signal(
        InvestorSequenceWorkflow.human_decision,
        HumanDecision(step=0, approved=True, edited_body="Hi Jane - quick note.", actor_id="a"),
    )
    await _wait_for(handle, _sent(0))
    sent = next(iter(gw.drafts.values()))
    assert sent["body"].startswith("Hi Jane - quick note.")
    assert "123 Main St" in sent["body"] and "unsubscribe" in sent["body"]
    await handle.signal(InvestorSequenceWorkflow.bounce)
    result = await handle.result()
    assert result.cancel_reason == "bounce"


async def test_workflow_started_by_name(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: gacts.GraphActivityContext,
    session: AsyncSession,
    seq: Sequence,
) -> None:
    handle = await env.client.start_workflow(
        "InvestorSequenceWorkflow",
        _input(seq),
        id=seq.temporal_workflow_id,
        task_queue=TASK_QUEUE,
    )
    await _wait_for(handle, _awaiting(0))
    await handle.signal("reply")
    result = await handle.result()
    assert result["cancel_reason"] == "reply"
    inv = await session.get(Investor, seq.investor_id)
    assert inv is not None
