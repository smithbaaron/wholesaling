"""Graph workflows: definitions, worker registration, schedule registry, and (when the
Temporal time-skipping server is available) an end-to-end run with the mock gateway."""

from __future__ import annotations

import inspect
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from wholesaling.activities import graph as acts
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, Message
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS, build_worker
from wholesaling.workflows import SCHEDULES
from wholesaling.workflows.graph_maintenance import (
    RENEWAL_ALERT_AFTER_FAILURES,
    DeltaPollWorkflowInput,
    GraphDeltaPollWorkflow,
    GraphInboundMessageWorkflow,
    GraphSubscriptionRenewalWorkflow,
    InboundMessageInput,
    SubscriptionRenewalInput,
    inbound_workflow_id,
)
from wholesaling.workflows.schedules import get_schedule

GRAPH_WORKFLOWS = [
    GraphSubscriptionRenewalWorkflow,
    GraphDeltaPollWorkflow,
    GraphInboundMessageWorkflow,
]


@pytest.mark.parametrize("wf", GRAPH_WORKFLOWS, ids=lambda w: w.__name__)
def test_workflow_definitions_take_single_dataclass_input(wf: type) -> None:
    defn = workflow._Definition.from_class(wf)
    assert defn is not None and defn.name == wf.__name__
    assert "status" in defn.queries
    params = list(inspect.signature(wf.run).parameters)
    assert params == ["self", "input"], "milestone-1 decision 8: one dataclass input"


def test_graph_workflows_and_activities_are_registered_in_worker() -> None:
    for wf in GRAPH_WORKFLOWS:
        assert wf in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {
        "graph_fetch_message",
        "graph_delta_poll",
        "graph_renew_subscriptions",
        "graph_record_renewal_outcome",
        "graph_raise_alert",
        "graph_create_draft",
        "graph_send_draft",
    } <= names


def test_schedule_registry_matches_requirements() -> None:
    renewal = get_schedule("graph-subscription-renewal")
    assert renewal.workflow == "GraphSubscriptionRenewalWorkflow"
    assert renewal.interval == timedelta(hours=48), "WF-11"
    assert renewal.args == (SubscriptionRenewalInput(),)
    assert renewal.overlap == "skip"
    poll = get_schedule("graph-delta-poll")
    assert poll.workflow == "GraphDeltaPollWorkflow"
    assert poll.interval == timedelta(minutes=15), "WF-12"
    assert poll.args == (DeltaPollWorkflowInput(),)
    registered = {workflow._Definition.from_class(w).name for w in WORKFLOWS}  # type: ignore[union-attr]
    for spec in SCHEDULES:
        assert spec.workflow in registered, f"{spec.id} points at an unregistered workflow"
    assert len({s.id for s in SCHEDULES}) == len(SCHEDULES)
    with pytest.raises(KeyError):
        get_schedule("nope")


def test_renewal_lifetime_outlives_the_renewal_cadence() -> None:
    renewal = get_schedule("graph-subscription-renewal")
    lifetime = timedelta(hours=SubscriptionRenewalInput().lifetime_hours)
    assert lifetime > renewal.interval, "one missed run must not lapse the subscription"
    assert lifetime <= timedelta(minutes=10_080), "Graph's Outlook message ceiling"


def test_inbound_workflow_id_is_deterministic_and_distinct() -> None:
    a = inbound_workflow_id("sub-1", "AAMk1")
    assert a == inbound_workflow_id("sub-1", "AAMk1")
    assert a != inbound_workflow_id("sub-1", "AAMk2")
    assert a != inbound_workflow_id("sub-2", "AAMk1")
    assert a.startswith("graph-inbound-") and len(a) < 64


def test_status_queries_before_run() -> None:
    assert GraphSubscriptionRenewalWorkflow().status() == {"state": "pending", "outcome": None}
    assert GraphDeltaPollWorkflow().status() == {"state": "pending", "result": None}
    assert GraphInboundMessageWorkflow().status() == {"state": "pending", "result": None}
    assert RENEWAL_ALERT_AFTER_FAILURES == 2


# -- end to end under the time-skipping test server ---------------------------------------------

pytest_temporal = pytest.mark.temporal
TASK_QUEUE = "test-graph"


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
async def graph_ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[acts.GraphActivityContext]:
    gw = MockGraphGateway(mailbox="deals@example.test")
    ctx = acts.GraphActivityContext(
        gateway=gw,
        session_factory=session_factory,
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="test",
            graph_mailbox="deals@example.test",
            graph_webhook_url="https://api.example/webhooks/graph",
            graph_webhook_client_state="s",
        ),
    )
    acts.set_graph_context(ctx)
    try:
        yield ctx
    finally:
        acts.set_graph_context(None)


@pytest.fixture
async def worker(env: WorkflowEnvironment) -> AsyncIterator[Worker]:
    w = build_worker(env.client, TASK_QUEUE)
    async with w:
        yield w


@pytest_temporal
async def test_inbound_workflow_persists_message_once(
    env: WorkflowEnvironment,
    worker: Worker,
    graph_ctx: acts.GraphActivityContext,
    session: AsyncSession,
) -> None:
    gw = graph_ctx.gateway
    assert isinstance(gw, MockGraphGateway)
    inbound = gw.inject_inbound(from_address="jane@acme.com", subject="Deals", body="Yes")
    wf_id = inbound_workflow_id("sub-1", inbound.graph_id)
    result = await env.client.execute_workflow(
        GraphInboundMessageWorkflow.run,
        InboundMessageInput(graph_id=inbound.graph_id, subscription_id="sub-1"),
        id=wf_id,
        task_queue=TASK_QUEUE,
    )
    assert result.found and result.created
    count = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
    assert count == 1


@pytest_temporal
async def test_renewal_workflow_alerts_after_two_failures(
    env: WorkflowEnvironment,
    worker: Worker,
    graph_ctx: acts.GraphActivityContext,
    session: AsyncSession,
) -> None:
    gw = graph_ctx.gateway
    assert isinstance(gw, MockGraphGateway)
    outcomes = []
    for _ in range(2):
        for _attempt in range(3):  # the activity retry policy allows 3 attempts
            gw.fail_next("list_subscriptions", RuntimeError("graph down"))
        outcomes.append(
            await env.client.execute_workflow(
                GraphSubscriptionRenewalWorkflow.run,
                SubscriptionRenewalInput(),
                id=f"renewal-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
        )
    assert [o.ok for o in outcomes] == [False, False]
    assert [o.consecutive_failures for o in outcomes] == [1, 2]
    assert [o.alerted for o in outcomes] == [False, True]
    alerts = (
        await session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == acts.ALERT_EVENT)
        )
    ).scalar_one()
    assert alerts == 1
    ok = await env.client.execute_workflow(
        GraphSubscriptionRenewalWorkflow.run,
        SubscriptionRenewalInput(),
        id=f"renewal-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    assert ok.ok and ok.consecutive_failures == 0 and len(ok.created) == 1
