"""GraphInboundMessageWorkflow after BO-07: definition, stage routing (with the Temporal calls
stubbed so it runs without a server), worker registration, and the time-skipping end-to-end
runs marked ``temporal`` (WF-19, WF-21, POL-03c, INV-24)."""

from __future__ import annotations

import dataclasses
import inspect
import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity, workflow
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from wholesaling.activities import conversation as conv
from wholesaling.activities import graph as graph_acts
from wholesaling.agents.a4 import MODEL_OVERRIDES, REPLY_WARRANTED_CLASSES
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.db.models import Message, Suppression
from wholesaling.db.models.enums import AgentId, MessageClassification
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS, build_worker
from wholesaling.workflows import graph_maintenance as gm
from wholesaling.workflows.graph_maintenance import (
    HUMAN_DECISION_SIGNAL,
    HUMAN_DECISION_TIMEOUT,
    GraphInboundMessageWorkflow,
    HumanDecision,
    InboundMessageInput,
    InboundPipelineResult,
    inbound_workflow_id,
)

MAILBOX = "deals@example.test"


# -- definition --------------------------------------------------------------------------------


def test_workflow_exposes_human_decision_signal_and_status_query() -> None:
    defn = workflow._Definition.from_class(GraphInboundMessageWorkflow)
    assert defn is not None and defn.name == "GraphInboundMessageWorkflow"
    assert HUMAN_DECISION_SIGNAL in defn.signals and HUMAN_DECISION_SIGNAL == "human_decision"
    assert "status" in defn.queries
    params = list(inspect.signature(GraphInboundMessageWorkflow.run).parameters)
    assert params == ["self", "input"]
    sig_params = inspect.signature(GraphInboundMessageWorkflow.human_decision).parameters
    assert list(sig_params) == ["self", "decision"]
    assert sig_params["decision"].annotation in ("HumanDecision", HumanDecision)


def test_human_decision_payload_contract() -> None:
    fields = {f.name: f for f in dataclasses.fields(HumanDecision)}
    assert set(fields) == {"approved", "edited_body", "step", "message_id", "actor_id"}
    assert fields["edited_body"].default is None and fields["actor_id"].default is None
    assert HumanDecision(approved=True) == HumanDecision(True, None, None, None, None)


def test_pipeline_result_keeps_fetch_fields_flat() -> None:
    names = {f.name for f in dataclasses.fields(InboundPipelineResult)}
    assert {"found", "created", "message_id", "thread_id", "is_bounce"} <= names
    assert {"stage", "classification", "policy_decision", "sent", "human_decision"} <= names
    assert InboundPipelineResult(found=False).stage == "done"
    assert GraphInboundMessageWorkflow().status() == {"state": "pending", "result": None}
    assert timedelta(days=14) == HUMAN_DECISION_TIMEOUT


def test_reply_routing_sets_match_a4() -> None:
    no_reply = {c.value for c in MessageClassification} - {c.value for c in REPLY_WARRANTED_CLASSES}
    assert no_reply == gm._NO_REPLY_CLASSES
    assert {"interested", "buy_box_supplied"} == gm._BUY_BOX_CLASSES


def test_conversation_activities_registered_in_worker() -> None:
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {
        "conversation_classify_inbound",
        "conversation_extract_buy_box",
        "conversation_draft_and_qa_reply",
        "conversation_apply_human_decision",
        "conversation_notify_bounce",
    } <= names
    assert GraphInboundMessageWorkflow in WORKFLOWS


# -- stage routing with the Temporal primitives stubbed ------------------------------------------

STUB_WORKFLOW_ID = "graph-inbound-stub"


class StubbedTemporal:
    """Replaces ``workflow.execute_activity`` / ``wait_condition`` inside the workflow module."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, decision: HumanDecision | None = None
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.responses: dict[str, Callable[[Any], Any]] = {}
        self.decision = decision
        self.timed_out = False
        monkeypatch.setattr(gm.workflow, "execute_activity", self.execute_activity)
        monkeypatch.setattr(gm.workflow, "wait_condition", self.wait_condition)
        monkeypatch.setattr(
            gm.workflow, "info", lambda: SimpleNamespace(workflow_id=STUB_WORKFLOW_ID)
        )

    def respond(self, fn: Any, response: Callable[[Any], Any]) -> None:
        self.responses[fn.__name__] = response

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        self.calls.append((fn.__name__, arg))
        return self.responses[fn.__name__](arg)

    async def wait_condition(
        self,
        predicate: Callable[[], bool],
        timeout: Any = None,  # noqa: ASYNC109 - mirrors workflow.wait_condition's signature
    ) -> None:
        self.calls.append(("wait_condition", timeout))
        if self.decision is None:
            self.timed_out = True
            raise TimeoutError
        wf = self._workflow
        await wf.human_decision(self.decision)
        assert predicate()

    def bind(self, wf: GraphInboundMessageWorkflow) -> None:
        self._workflow = wf

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def fetched(**kw: Any) -> graph_acts.FetchMessageResult:
    base: dict[str, Any] = {
        "found": True,
        "message_id": "m1",
        "thread_id": "t1",
        "external_message_id": "<x@y>",
        "created": True,
    }
    base.update(kw)
    return graph_acts.FetchMessageResult(**base)


def classified(label: str, **kw: Any) -> conv.ClassifyInboundResult:
    values: dict[str, Any] = {"classification": label, "confidence": 0.9, "signalled": "reply"}
    values.update(kw)
    return conv.ClassifyInboundResult(message_id="m1", **values)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubbedTemporal:
    s = StubbedTemporal(monkeypatch)
    s.respond(graph_acts.fetch_message, lambda _a: fetched())
    s.respond(conv.classify_inbound, lambda _a: classified("interested"))
    s.respond(
        conv.extract_and_upsert_buy_box,
        lambda _a: conv.ExtractBuyBoxResult(
            message_id="m1",
            versions=[conv.BuyBoxVersionInfo("b1", "default", 1, True, "created")],
        ),
    )
    s.respond(
        conv.draft_and_qa_reply,
        lambda _a: conv.DraftReplyResult(
            message_id="m1",
            draft_message_id="d1",
            qa_verdict="approve",
            qa_confidence=0.9,
            policy_decision="queue_for_human",
            policy_reason="draft at L0: human sends",
        ),
    )
    s.respond(
        conv.apply_human_decision,
        lambda a: conv.ApplyHumanDecisionResult(
            "m1", "d1", sent=a.approved, discarded=not a.approved, edited=bool(a.edited_body)
        ),
    )
    s.respond(conv.notify_bounce, lambda _a: conv.NotifyBounceResult("m1", signalled=True))
    return s


async def run_wf(
    stub: StubbedTemporal,
) -> tuple[InboundPipelineResult, GraphInboundMessageWorkflow]:
    wf = GraphInboundMessageWorkflow()
    stub.bind(wf)
    result = await wf.run(InboundMessageInput(graph_id="AAMk1", subscription_id="sub"))
    return result, wf


async def test_workflow_hands_its_id_to_fetch_and_draft(stub: StubbedTemporal) -> None:
    """GAP-05: the review route resolves an inbound workflow from the id the workflow passes
    into ``fetch_message`` (audit event) and ``draft_and_qa_reply`` (``meta``)."""
    await run_wf(stub)
    by_name = dict(stub.calls)
    assert by_name["fetch_message"].workflow_id == STUB_WORKFLOW_ID
    assert by_name["draft_and_qa_reply"] == conv.DraftReplyInput(
        message_id="m1", workflow_id=STUB_WORKFLOW_ID
    )


async def test_interested_at_l0_waits_for_human_and_sends_on_approval(
    stub: StubbedTemporal,
) -> None:
    stub.decision = HumanDecision(approved=True, edited_body="Edited.", actor_id="aaron")
    result, wf = await run_wf(stub)
    assert stub.names() == [
        "fetch_message",
        "classify_inbound",
        "extract_and_upsert_buy_box",
        "draft_and_qa_reply",
        "wait_condition",
        "apply_human_decision",
    ]
    assert stub.calls[4][1] == HUMAN_DECISION_TIMEOUT
    assert result.found and result.created and result.classification == "interested"
    assert result.buy_box_versions[0]["version"] == 1
    assert result.policy_decision == "queue_for_human" and result.sent and result.edited_by_human
    assert result.human_decision == "approved" and result.stage == "done"
    assert wf.status()["state"] == "done" and wf.status()["result"]["sent"] is True
    applied = stub.calls[5][1]
    assert applied == conv.ApplyHumanDecisionInput("m1", True, "Edited.", "aaron")


async def test_rejection_discards_and_timeout_leaves_draft_queued(stub: StubbedTemporal) -> None:
    stub.decision = HumanDecision(approved=False)
    result, _ = await run_wf(stub)
    assert result.human_decision == "rejected" and not result.sent

    stub.calls.clear()
    stub.decision = None
    result, _ = await run_wf(stub)
    assert stub.timed_out and result.human_decision == "timeout" and not result.sent
    assert result.policy_decision == "queue_for_human"
    assert "apply_human_decision" not in stub.names()


async def test_l1_send_skips_the_human_wait(stub: StubbedTemporal) -> None:
    stub.respond(
        conv.draft_and_qa_reply,
        lambda _a: conv.DraftReplyResult(
            message_id="m1", draft_message_id="d1", policy_decision="send", sent=True
        ),
    )
    result, _ = await run_wf(stub)
    assert "wait_condition" not in stub.names() and result.sent and result.human_decision is None


@pytest.mark.parametrize("label", ["unrelated", "not_interested", "opt_out"])
async def test_no_reply_classes_stop_after_classification(
    stub: StubbedTemporal, label: str
) -> None:
    stub.respond(
        conv.classify_inbound, lambda _a: classified(label, suppression_created=label == "opt_out")
    )
    result, _ = await run_wf(stub)
    assert stub.names() == ["fetch_message", "classify_inbound"]
    assert result.skipped_reason == f"no_reply:{label}" and result.draft_message_id is None
    assert result.suppression_created is (label == "opt_out")


async def test_auto_reply_gets_no_draft(stub: StubbedTemporal) -> None:
    stub.respond(
        conv.classify_inbound,
        lambda _a: classified("ambiguous", is_auto_reply=True, signalled=None),
    )
    result, _ = await run_wf(stub)
    assert stub.names() == ["fetch_message", "classify_inbound"]
    assert result.is_auto_reply and result.skipped_reason == "no_reply:ambiguous"


@pytest.mark.parametrize("label", ["question", "not_now", "ambiguous"])
async def test_reply_without_buy_box_extraction(stub: StubbedTemporal, label: str) -> None:
    stub.respond(conv.classify_inbound, lambda _a: classified(label, reconnect_date="2027-01-15"))
    stub.decision = HumanDecision(approved=True)
    result, _ = await run_wf(stub)
    assert "extract_and_upsert_buy_box" not in stub.names()
    assert "draft_and_qa_reply" in stub.names() and result.reconnect_date == "2027-01-15"


async def test_bounce_notifies_sequence_and_stops(stub: StubbedTemporal) -> None:
    stub.respond(
        graph_acts.fetch_message, lambda _a: fetched(is_bounce=True, bounced_address="j@a.c")
    )
    result, _ = await run_wf(stub)
    assert stub.names() == ["fetch_message", "notify_bounce"]
    assert stub.calls[1][1] == conv.NotifyBounceInput("m1", "j@a.c")
    assert result.skipped_reason == "bounce" and result.is_bounce


async def test_not_found_and_outbound_short_circuit(stub: StubbedTemporal) -> None:
    stub.respond(
        graph_acts.fetch_message, lambda _a: fetched(found=False, message_id=None, created=False)
    )
    result, _ = await run_wf(stub)
    assert stub.names() == ["fetch_message"] and result.skipped_reason == "not_found"

    stub.calls.clear()
    stub.respond(graph_acts.fetch_message, lambda _a: fetched())
    stub.respond(
        conv.classify_inbound,
        lambda _a: conv.ClassifyInboundResult(message_id="m1", skipped_reason="outbound"),
    )
    result, _ = await run_wf(stub)
    assert stub.names() == ["fetch_message", "classify_inbound"]
    assert result.skipped_reason == "outbound" and result.classification is None


async def test_first_human_decision_wins() -> None:
    wf = GraphInboundMessageWorkflow()
    await wf.human_decision(HumanDecision(approved=False))
    await wf.human_decision(HumanDecision(approved=True))
    assert wf._human_decision == HumanDecision(approved=False)


# -- end to end under the time-skipping test server ---------------------------------------------

pytest_temporal = pytest.mark.temporal
TASK_QUEUE = "test-inbound-pipeline"
INVESTOR = "jane@acme.com"


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
def provider() -> MockModelProvider:
    p = MockModelProvider()
    p.script(
        AgentId.A4,
        TaskKind.CLASSIFY,
        parsed={
            "classification": "interested",
            "confidence": 0.9,
            "rationale": "",
            "is_auto_reply": False,
            "reconnect_date": None,
        },
    )
    p.script(
        AgentId.A4,
        TaskKind.DRAFT,
        parsed={
            "subject": "RE: Deals",
            "body": "Thanks Jane. Which property types and what condition do you take on?",
            "asks_for": ["asset_types", "condition"],
            "contains_sensitive": False,
        },
    )
    p.script(
        AgentId.A5,
        TaskKind.QA,
        parsed={
            "verdict": "approve",
            "confidence": 0.95,
            "flags": [],
            "rewrite_body": None,
            "rationale": "",
        },
    )
    return p


@pytest.fixture
async def pipeline_ctx(
    session_factory: async_sessionmaker[AsyncSession], provider: MockModelProvider
) -> AsyncIterator[tuple[MockGraphGateway, MockTemporalGateway]]:
    gw = MockGraphGateway(mailbox=MAILBOX)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        graph_mailbox=MAILBOX,
        graph_webhook_url="https://api.example/webhooks/graph",
        graph_webhook_client_state="s",
    )
    graph_acts.set_graph_context(
        graph_acts.GraphActivityContext(
            gateway=gw, session_factory=session_factory, settings=settings
        )
    )
    temporal = MockTemporalGateway()
    conv.set_conversation_context(
        conv.ConversationContext(
            session_factory=session_factory,
            runner=AgentRunner(provider, ModelRouter.from_settings(settings, MODEL_OVERRIDES)),
            temporal=temporal,
            settings=settings,
        )
    )
    try:
        yield gw, temporal
    finally:
        conv.set_conversation_context(None)
        graph_acts.set_graph_context(None)


@pytest.fixture
async def worker(env: WorkflowEnvironment) -> AsyncIterator[Worker]:
    w = build_worker(env.client, TASK_QUEUE)
    async with w:
        yield w


async def start(
    env: WorkflowEnvironment, gw: MockGraphGateway, body: str, subject: str = "RE: Deals"
) -> tuple[WorkflowHandle[Any, InboundPipelineResult], str]:
    inbound = gw.inject_inbound(from_address=INVESTOR, subject=subject, body=body)
    handle = await env.client.start_workflow(
        GraphInboundMessageWorkflow.run,
        InboundMessageInput(graph_id=inbound.graph_id, subscription_id="sub-1"),
        id=inbound_workflow_id("sub-1", inbound.graph_id),
        task_queue=TASK_QUEUE,
    )
    return handle, inbound.conversation_id or ""


@pytest_temporal
async def test_e2e_l0_queues_then_human_approval_sends_in_conversation(
    env: WorkflowEnvironment,
    worker: Worker,
    pipeline_ctx: tuple[MockGraphGateway, MockTemporalGateway],
    session: AsyncSession,
) -> None:
    gw, _ = pipeline_ctx
    handle, conversation_id = await start(env, gw, "Yes, interested.")
    await handle.signal(
        GraphInboundMessageWorkflow.human_decision, HumanDecision(approved=True, actor_id="aaron")
    )
    result = await handle.result()
    assert result.found and result.created and result.classification == "interested"
    assert result.policy_decision == "queue_for_human" and result.human_decision == "approved"
    assert result.sent and result.stage == "done"
    assert len(gw.sent) == 1 and gw.sent[0].conversation_id == conversation_id
    status = await handle.query(GraphInboundMessageWorkflow.status)
    assert status["state"] == "done" and status["result"]["sent"] is True
    total = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
    assert total == 2, "one inbound row, one reply row (INV-24)"


@pytest_temporal
async def test_e2e_opt_out_suppresses_and_drafts_nothing(
    env: WorkflowEnvironment,
    worker: Worker,
    pipeline_ctx: tuple[MockGraphGateway, MockTemporalGateway],
    provider: MockModelProvider,
    session: AsyncSession,
) -> None:
    gw, _ = pipeline_ctx
    provider.script(
        AgentId.A4,
        TaskKind.CLASSIFY,
        parsed={
            "classification": "opt_out",
            "confidence": 0.99,
            "rationale": "",
            "is_auto_reply": False,
            "reconnect_date": None,
        },
    )
    handle, _ = await start(env, gw, "Unsubscribe.")
    result = await handle.result()
    assert result.classification == "opt_out" and result.suppression_created
    assert result.skipped_reason == "no_reply:opt_out" and result.draft_message_id is None
    assert gw.drafts == {} and gw.sent == []
    rows = (await session.execute(select(Suppression))).scalars().all()
    assert [r.normalized_value for r in rows] == [INVESTOR]


@pytest_temporal
async def test_e2e_l1_sends_without_waiting(
    env: WorkflowEnvironment,
    worker: Worker,
    pipeline_ctx: tuple[MockGraphGateway, MockTemporalGateway],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gw, _ = pipeline_ctx
    async with session_factory() as s:
        await PolicyRepository(s).set_level(
            PolicyAction.INVESTOR_REPLY, AutonomyLevel.L1, updated_by="test"
        )
        await s.commit()
    handle, conversation_id = await start(env, gw, "Yes, interested.")
    result = await handle.result()
    assert result.policy_decision == "send" and result.sent and result.human_decision is None
    assert len(gw.sent) == 1 and gw.sent[0].conversation_id == conversation_id


@pytest_temporal
async def test_e2e_no_human_decision_times_out_and_leaves_draft_queued(
    env: WorkflowEnvironment,
    worker: Worker,
    pipeline_ctx: tuple[MockGraphGateway, MockTemporalGateway],
) -> None:
    gw, _ = pipeline_ctx
    handle, _ = await start(env, gw, "Yes, interested.")
    result = await handle.result()  # time skipping runs the 14-day timer instantly
    assert result.human_decision == "timeout" and not result.sent
    assert len(gw.drafts) == 1 and gw.sent == []


def test_review_queue_payload_decodes_into_workflow_human_decision() -> None:
    """The review queue sends its own ``HumanDecision`` dataclass; Temporal maps by field name."""
    from temporalio.converter import default

    from wholesaling.db.repositories.review import HUMAN_DECISION_SIGNAL as THEIR_SIGNAL
    from wholesaling.db.repositories.review import HumanDecision as QueueDecision

    assert THEIR_SIGNAL == HUMAN_DECISION_SIGNAL
    payloads = default().payload_converter.to_payloads(
        [QueueDecision(approved=True, edited_body="x", step=None, message_id="m", actor_id="aaron")]
    )
    (decoded,) = default().payload_converter.from_payloads(payloads, [HumanDecision])
    assert decoded == HumanDecision(
        approved=True, edited_body="x", step=None, message_id="m", actor_id="aaron"
    )
