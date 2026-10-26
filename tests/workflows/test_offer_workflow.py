"""OfferWorkflow and BuyBoxSearch end to end under the Temporal time-skipping test server with
the mock Graph / model / property provider and SQLite (WF-04, WF-07, A7-10, POL-03g).

Marked ``temporal``: skips when the test-server binary cannot be downloaded (sandbox); runs in CI.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.activities.offers_fixtures import (
    OfferHarness,
    seed_market,
    seed_seller,
    set_level,
)
from tests.db.conftest import MakeInvestor
from wholesaling.db.models.deals import Match
from wholesaling.db.models.enums import OfferStatus
from wholesaling.db.models.ops import AuditEvent
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria, BuyBoxRepository
from wholesaling.db.repositories.offers import OfferRepository
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.workers.main import build_worker
from wholesaling.workflows.buy_box_search import BuyBoxSearch, BuyBoxSearchInput, BuyBoxSearchResult
from wholesaling.workflows.offer import (
    HumanDecision,
    OfferWorkflow,
    OfferWorkflowInput,
    OfferWorkflowResult,
    offer_workflow_id,
)

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-offer"


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
async def worker(env: WorkflowEnvironment) -> AsyncIterator[Worker]:
    w = build_worker(env.client, TASK_QUEUE)
    async with w:
        yield w


async def _wait_for(
    handle: WorkflowHandle[Any, Any],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    max_wait: float = 60.0,
) -> dict[str, Any]:
    async with asyncio.timeout(max_wait):
        while True:
            status: dict[str, Any] = await handle.query(OfferWorkflow.status)
            if predicate(status):
                return status
            await asyncio.sleep(0.05)


async def test_offer_workflow_l1_sends_and_hands_off(
    env: WorkflowEnvironment,
    worker: Worker,
    offer_harness: OfferHarness,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_level(session, PolicyAction.OFFER_SEND, AutonomyLevel.L1)
    market = await seed_market(session)
    lead, intake = await seed_seller(session, market=market)
    await session.commit()
    result: OfferWorkflowResult = await env.client.execute_workflow(
        OfferWorkflow.run,
        OfferWorkflowInput(intake_id=str(intake.id), seller_lead_id=str(lead.id)),
        id=offer_workflow_id(str(intake.id)),
        task_queue=TASK_QUEUE,
    )
    assert (
        result.outcome == "sent"
        and result.offer_price == "88500"
        and result.arv_confidence == "High"
    )
    assert result.negotiation_workflow_id == f"negotiation:{result.offer_id}"
    assert len(offer_harness.graph.sent) == 1
    async with session_factory() as s:
        offer = await OfferRepository(s).get(uuid.UUID(result.offer_id or ""))
        assert offer is not None and offer.status is OfferStatus.SENT
        rows = await OfferRepository(s).list_verifications(offer.id)
        assert [r.pass_id.value for r in rows] == ["V1", "V3", "V2", "V4", "V5", "V6"]
        events = [
            e.event_type
            for e in (await s.execute(select(AuditEvent).where(AuditEvent.entity_id == offer.id)))
            .scalars()
            .all()
        ]
        assert "offer.sent" in events and "offer.handed_to_s5" in events
    starts = [c for c in offer_harness.temporal.calls if c.method == "start_workflow"]
    assert starts and starts[-1].args["workflow"] == "NegotiationWorkflow"


async def test_offer_workflow_l0_queues_until_the_human_approves(
    env: WorkflowEnvironment,
    worker: Worker,
    offer_harness: OfferHarness,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    market = await seed_market(session)
    lead, intake = await seed_seller(session, market=market)
    await session.commit()
    handle = await env.client.start_workflow(
        OfferWorkflow.run,
        OfferWorkflowInput(intake_id=str(intake.id), seller_lead_id=str(lead.id)),
        id=offer_workflow_id(str(intake.id)),
        task_queue=TASK_QUEUE,
    )
    status = await _wait_for(handle, lambda s: s["state"] == "awaiting_human")
    assert status["pending_message_id"] is not None and offer_harness.graph.sent == []
    async with session_factory() as s:
        offer = await OfferRepository(s).get(uuid.UUID(status["offer_id"]))
        assert offer is not None and offer.status is OfferStatus.PENDING_REVIEW
    await handle.signal(
        OfferWorkflow.human_decision,
        HumanDecision(
            step=None, approved=True, actor_id="aaron", message_id=status["pending_message_id"]
        ),
    )
    result: OfferWorkflowResult = await handle.result()
    assert result.outcome == "queued_then_sent" and result.human_rounds == 1
    assert len(offer_harness.graph.sent) == 1
    async with session_factory() as s:
        offer = await OfferRepository(s).get(uuid.UUID(result.offer_id or ""))
        assert (
            offer is not None
            and offer.status is OfferStatus.SENT
            and offer.policy_level_at_send is AutonomyLevel.L0
        )
        rows = await OfferRepository(s).list_verifications(offer.id)
        assert [r.iteration for r in rows if r.pass_id.value == "V6"] == [
            1,
            2,
        ]  # before decision and before send


async def test_buy_box_search_runs_d2_and_writes_pending_matches(
    env: WorkflowEnvironment,
    worker: Worker,
    offer_harness: OfferHarness,
    session: AsyncSession,
    make_investor: MakeInvestor,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from wholesaling.activities import deals as dacts

    inv = await make_investor("E2E Buyers LLC")
    res = await BuyBoxRepository(session).create_version(
        inv.id,
        BuyBoxCriteria(
            geography={"markets": ["Houston, TX"], "zips": ["77008", "77018"]},
            asset_types=["sfr"],
            price_max=Decimal(250000),
            condition="any",
        ),
    )
    await session.commit()
    d1 = await dacts.run_d1(
        dacts.RunD1Input(str(res.buy_box.id), str(inv.id), 1, "buy_box.created")
    )
    assert d1.created and d1.search_workflow_id
    result: BuyBoxSearchResult = await env.client.execute_workflow(
        BuyBoxSearch.run,
        BuyBoxSearchInput(
            search_profile_id=d1.search_profile_id,
            buy_box_id=str(res.buy_box.id),
            profile_version=1,
        ),
        id=d1.search_workflow_id,
        task_queue=TASK_QUEUE,
    )
    assert result.searched and result.matches_written >= 2 and not result.superseded
    async with session_factory() as s:
        matches = (
            (
                await s.execute(
                    select(Match).where(Match.search_profile_id == uuid.UUID(d1.search_profile_id))
                )
            )
            .scalars()
            .all()
        )
        assert len(matches) == result.matches_written and all(
            m.review_status.value == "pending" for m in matches
        )
