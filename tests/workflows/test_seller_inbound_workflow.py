"""SellerInboundWorkflow end to end under the Temporal time-skipping test server (web form and
email sources) with mocks and SQLite (REQUIREMENTS.md SEL-07, SEL-08, SEL-12, POL-03f, POL-03l).

Marked ``temporal``: skips when the test-server binary cannot be downloaded (sandbox); runs in CI.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from wholesaling.activities import conversation as conv
from wholesaling.activities import graph as gacts
from wholesaling.activities import seller as sacts
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId, Channel, Direction, MarketTier, SellerLeadStatus
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Suppression, Thread
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.repositories.sellers import SellerRepository
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.workers.main import build_worker
from wholesaling.workflows.seller_inbound import (
    HumanDecision,
    SellerInboundInput,
    SellerInboundWorkflow,
)

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-seller-inbound"
MAILBOX = "deals@example.test"
SELLER = "dolores.ramirez@example.test"
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
    p.script(
        AgentId.S4,
        TaskKind.DRAFT,
        parsed={
            "subject": "",
            "body": "Thanks - how soon would you want to close?",
            "asks_for": ["timeline"],
            "contains_sensitive": False,
        },
    )
    return p


@pytest.fixture
def temporal() -> MockTemporalGateway:
    return MockTemporalGateway()


@pytest.fixture
async def ctx(
    gw: MockGraphGateway,
    provider: MockModelProvider,
    temporal: MockTemporalGateway,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[sacts.SellerContext]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        graph_mailbox=MAILBOX,
        sender_postal_address="123 Main St, Houston, TX 77002",
        public_base_url="https://sell.example.test",
    )
    runner = AgentRunner(provider, ModelRouter.from_settings(settings))
    gacts.set_graph_context(
        gacts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=settings, clock=lambda: NOW
        )
    )
    conv.set_conversation_context(
        conv.ConversationContext(
            session_factory=session_factory,
            runner=runner,
            temporal=temporal,
            settings=settings,
            clock=lambda: NOW,
        )
    )
    seller_ctx = sacts.SellerContext(
        session_factory=session_factory,
        providers=mock_data_providers(),
        temporal=temporal,
        settings=settings,
        clock=lambda: NOW,
        runner=runner,
    )
    sacts.set_seller_context(seller_ctx)
    try:
        yield seller_ctx
    finally:
        sacts.set_seller_context(None)
        conv.set_conversation_context(None)
        gacts.set_graph_context(None)


@pytest.fixture
async def worker(env: WorkflowEnvironment) -> AsyncIterator[Worker]:
    w = build_worker(env.client, TASK_QUEUE)
    async with w:
        yield w


async def _lead(session: AsyncSession) -> SellerLead:
    market = Market(name="Houston", state="TX", tier=MarketTier.ACTIVE)
    session.add(market)
    await session.flush()
    lead = SellerLead(
        market_id=market.id,
        status=SellerLeadStatus.INTAKE,
        owner_name="Dolores Ramirez",
        property_address_line1="1420 Pecan Grove Dr",
        property_city="Houston",
        property_state="TX",
        property_postal_code="77009",
        source="web_form",
        contact_channels=[
            {"channel": "email", "value": SELLER, "normalized_value": SELLER, "is_primary": True}
        ],
    )
    session.add(lead)
    await session.commit()
    return lead


async def _count(session: AsyncSession, model: type, **where: object) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int((await session.execute(stmt)).scalar_one())


async def test_web_form_submission_queues_an_s4_reply_and_a_human_approves_it(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: sacts.SellerContext,
    gw: MockGraphGateway,
    session: AsyncSession,
) -> None:
    lead = await _lead(session)
    repo = SellerRepository(session)
    thread = await repo.create_thread(
        lead=lead, channel=Channel.EMAIL, counterpart_address=SELLER, subject="form"
    )
    payload: dict[str, Any] = {
        "submission_id": "sub-e2e",
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
        "photos": [],
    }
    msg, _ = await repo.create_form_message(
        thread=thread,
        submission_id="sub-e2e",
        from_address=SELLER,
        body="form",
        payload=payload,
        received_at=NOW,
    )
    await session.commit()
    handle = await env.client.start_workflow(
        SellerInboundWorkflow.run,
        SellerInboundInput(source="web_form", message_id=str(msg.id), submission_id="sub-e2e"),
        id=sacts.seller_inbound_workflow_id("form", "sub-e2e"),
        task_queue=TASK_QUEUE,
    )
    while (await handle.query(SellerInboundWorkflow.status))["state"] != "awaiting_human":
        await env.sleep(1)
    await handle.signal(
        SellerInboundWorkflow.human_decision, HumanDecision(approved=True, actor_id="aaron")
    )
    result = await handle.result()
    assert result.found and result.classification == "reply" and result.source == "web_form"
    assert result.missing_required == ["timeline"] and result.asked == ["timeline", "co_owners"]
    assert (
        result.policy_decision == "queue_for_human"
        and result.human_decision == "approved"
        and result.sent
    )
    assert len(gw.sent) == 1
    sent_body = gw.drafts[gw.sent[0].draft_id]["body"]
    assert "Disclosure:" in sent_body and f"/sell?lead={lead.id}" in sent_body
    intake = (await session.execute(select(Intake))).scalar_one()
    assert intake.motivation == "Relocating" and intake.missing_required == ["timeline"]
    await session.refresh(lead)
    assert lead.parcel_verified and lead.parcel_id == "HCAD-D-0000001"


async def test_email_opt_out_suppresses_and_ends(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: sacts.SellerContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    session: AsyncSession,
) -> None:
    lead = await _lead(session)
    thread = Thread(
        channel=Channel.EMAIL,
        external_thread_id="conv-e2e",
        seller_lead_id=lead.id,
        counterpart_address=SELLER,
    )
    session.add(thread)
    await session.commit()
    inbound = gw.inject_inbound(
        from_address=SELLER,
        subject="RE: Your property",
        body="Remove me from your list.",
        conversation_id="conv-e2e",
    )
    provider.script(
        AgentId.S4,
        TaskKind.CLASSIFY,
        parsed={
            "classification": "opt_out",
            "confidence": 0.98,
            "rationale": "x",
            "is_auto_reply": False,
            "asks_offer": False,
        },
    )
    result = await env.client.execute_workflow(
        SellerInboundWorkflow.run,
        SellerInboundInput(graph_id=inbound.graph_id, subscription_id="sub"),
        id=sacts.seller_inbound_workflow_id("email", inbound.graph_id),
        task_queue=TASK_QUEUE,
    )
    assert result.found and result.created and result.classification == "opt_out"
    assert result.skipped_reason == "no_intake:opt_out" and result.draft_message_id is None
    assert await _count(session, Suppression, normalized_value=SELLER) == 1
    assert await _count(session, Message, direction=Direction.IN) == 1
    await session.refresh(lead)
    assert lead.status is SellerLeadStatus.SUPPRESSED
    assert gw.sent == []


async def test_replayed_notification_repeats_nothing(
    env: WorkflowEnvironment,
    worker: Worker,
    ctx: sacts.SellerContext,
    gw: MockGraphGateway,
    provider: MockModelProvider,
    session: AsyncSession,
) -> None:
    """A duplicate notification after the first run completed re-runs the pipeline (Temporal's
    default id-reuse policy allows it); every activity finds the rows of the first run."""
    lead = await _lead(session)
    thread = Thread(
        channel=Channel.EMAIL,
        external_thread_id=f"conv-{uuid.uuid4().hex[:6]}",
        seller_lead_id=lead.id,
        counterpart_address=SELLER,
    )
    session.add(thread)
    await session.commit()
    inbound = gw.inject_inbound(
        from_address=SELLER,
        subject="RE: Your property",
        body="Not selling.",
        conversation_id=thread.external_thread_id,
    )
    provider.script(
        AgentId.S4,
        TaskKind.CLASSIFY,
        parsed={
            "classification": "not_interested",
            "confidence": 0.9,
            "rationale": "x",
            "is_auto_reply": False,
            "asks_offer": False,
        },
    )
    wid = sacts.seller_inbound_workflow_id("email", inbound.graph_id)
    first = await env.client.execute_workflow(
        SellerInboundWorkflow.run,
        SellerInboundInput(graph_id=inbound.graph_id),
        id=wid,
        task_queue=TASK_QUEUE,
    )
    second = await env.client.execute_workflow(
        SellerInboundWorkflow.run,
        SellerInboundInput(graph_id=inbound.graph_id),
        id=wid,
        task_queue=TASK_QUEUE,
    )
    assert first.created and not second.created and second.classification == "not_interested"
    assert await _count(session, Message, direction=Direction.IN) == 1
    assert len([r for r in provider.requests if r.metadata.task_kind is TaskKind.CLASSIFY]) == 1
