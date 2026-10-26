"""SellerInboundWorkflow: contract checks and a stage simulation with the activities stubbed
(no Temporal server) (REQUIREMENTS.md SEL-08, WF-19, WF-21, POL-03f)."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow

from wholesaling.activities import graph as gacts
from wholesaling.activities import seller as acts
from wholesaling.db.repositories.review import HumanDecision as ApiHumanDecision
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows import seller_inbound as wi
from wholesaling.workflows.seller_inbound import (
    HumanDecision,
    SellerInboundInput,
    SellerInboundWorkflow,
    seller_inbound_workflow_id,
)

# -- definition ----------------------------------------------------------------------------------


def _defn() -> workflow._Definition:
    d = workflow._Definition.from_class(SellerInboundWorkflow)
    assert d is not None
    return d


def test_type_name_signals_and_queries() -> None:
    assert (
        _defn().name
        == "SellerInboundWorkflow"
        == wi.WORKFLOW_NAME
        == acts.SELLER_INBOUND_WORKFLOW_NAME
    )
    assert set(_defn().signals) == {"human_decision"} and set(_defn().queries) == {"status"}
    assert timedelta(days=14) == wi.HUMAN_DECISION_TIMEOUT


def test_input_shape() -> None:
    names = [f.name for f in dataclasses.fields(SellerInboundInput)]
    assert names == [
        "source",
        "graph_id",
        "subscription_id",
        "change_type",
        "message_id",
        "submission_id",
    ]
    assert SellerInboundInput().source == "email"


def test_human_decision_payload_matches_the_review_queue() -> None:
    from wholesaling.workflows.graph_maintenance import HumanDecision as GraphHumanDecision

    assert HumanDecision is GraphHumanDecision
    theirs = {f.name for f in dataclasses.fields(ApiHumanDecision)}
    assert theirs <= {f.name for f in dataclasses.fields(HumanDecision)}


def test_registered() -> None:
    assert SellerInboundWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {
        "seller_classify_inbound",
        "seller_intake",
        "seller_apply_human_decision",
        "seller_notify_bounce",
        "graph_fetch_message",
    } <= names
    assert seller_inbound_workflow_id("email", "AAMk1") == "seller-inbound:email:AAMk1"


# -- simulation ------------------------------------------------------------------------------------


@dataclass
class Call:
    name: str
    arg: Any


@dataclass
class FakeWorkflowModule:
    wf: SellerInboundWorkflow
    handlers: dict[str, Callable[[Any], Any]]
    events: list[tuple[Callable[[SellerInboundWorkflow], bool], Callable[[], Any]]] = field(
        default_factory=list
    )
    calls: list[Call] = field(default_factory=list)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("sim"))

    class unsafe:
        @staticmethod
        def imports_passed_through() -> Any:  # pragma: no cover
            raise AssertionError("not used after import")

    def info(self) -> Any:
        return type("Info", (), {"workflow_id": "seller-inbound:sim"})()

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        self.calls.append(Call(fn.__name__, arg))
        return self.handlers[fn.__name__](arg)

    async def _fire(self) -> None:
        remaining = []
        for pred, action in self.events:
            if pred(self.wf):
                result = action()
                if hasattr(result, "__await__"):
                    await result
            else:
                remaining.append((pred, action))
        self.events = remaining

    async def wait_condition(
        self,
        pred: Callable[[], bool],
        timeout: timedelta | None = None,  # noqa: ASYNC109 - mirrors workflow.wait_condition
    ) -> None:
        await self._fire()
        if pred():
            return
        if timeout is None:
            raise RuntimeError("would wait forever")
        raise TimeoutError


def _fetch(found: bool = True, bounce: bool = False) -> Callable[[Any], gacts.FetchMessageResult]:
    def _h(i: gacts.FetchMessageInput) -> gacts.FetchMessageResult:
        return gacts.FetchMessageResult(
            found=found,
            message_id="m1" if found else None,
            thread_id="t1",
            external_message_id="<x>",
            created=True,
            is_bounce=bounce,
            bounced_address="s@x.test" if bounce else None,
            from_address="s@x.test",
        )

    return _h


def _classify(label: str | None = "reply", **kw: Any) -> Callable[[Any], acts.SellerClassifyResult]:
    def _h(i: acts.SellerClassifyInput) -> acts.SellerClassifyResult:
        return acts.SellerClassifyResult(
            message_id=i.message_id,
            classification=label,
            confidence=0.9,
            seller_lead_id="lead",
            **kw,
        )

    return _h


def _intake(**kw: Any) -> Callable[[Any], acts.SellerIntakeResult]:
    def _h(i: acts.SellerIntakeInput) -> acts.SellerIntakeResult:
        base: dict[str, Any] = {
            "seller_lead_id": "lead",
            "intake_id": "i1",
            "missing_required": ["condition"],
            "asked": ["condition"],
            "draft_message_id": "d1",
            "policy_decision": "queue_for_human",
            "qa_verdict": "approve",
        }
        base.update(kw)
        return acts.SellerIntakeResult(message_id=i.message_id, **base)

    return _h


def _human(i: acts.SellerHumanDecisionInput) -> acts.SellerHumanDecisionResult:
    return acts.SellerHumanDecisionResult(
        i.message_id, "d1", sent=i.approved, discarded=not i.approved, edited=bool(i.edited_body)
    )


def _handlers(**over: Callable[[Any], Any]) -> dict[str, Callable[[Any], Any]]:
    base: dict[str, Callable[[Any], Any]] = {
        "fetch_message": _fetch(),
        "seller_notify_bounce": lambda i: acts.SellerBounceResult(i.message_id, signalled=True),
        "seller_classify_inbound": _classify(),
        "seller_intake": _intake(),
        "seller_apply_human_decision": _human,
    }
    base.update(over)
    return base


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWorkflowModule]:
    def _make(**over: Callable[[Any], Any]) -> FakeWorkflowModule:
        wf = SellerInboundWorkflow()
        fake = FakeWorkflowModule(wf=wf, handlers=_handlers(**over))
        monkeypatch.setattr(wi, "workflow", fake)
        return fake

    return _make


def _names(fake: FakeWorkflowModule) -> list[str]:
    return [c.name for c in fake.calls]


async def test_email_reply_queued_then_approved(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim()
    fake.events.append(
        (
            lambda wf: wf._state == "awaiting_human",
            lambda: fake.wf.human_decision(
                HumanDecision(approved=True, edited_body="edit", actor_id="aaron")
            ),
        )
    )
    result = await fake.wf.run(SellerInboundInput(graph_id="AAMk1"))
    assert _names(fake) == [
        "fetch_message",
        "seller_classify_inbound",
        "seller_intake",
        "seller_apply_human_decision",
    ]
    assert result.found and result.classification == "reply" and result.asked == ["condition"]
    assert result.human_decision == "approved" and result.sent and result.edited_by_human
    fetch_arg = fake.calls[0].arg
    assert fetch_arg.workflow_id == "seller-inbound:sim" and fetch_arg.graph_id == "AAMk1"
    assert fake.calls[2].arg.workflow_id == "seller-inbound:sim"
    applied = fake.calls[3].arg
    assert (
        applied.message_id == "m1"
        and applied.decided_by == "aaron"
        and applied.edited_body == "edit"
    )
    assert fake.wf.status()["state"] == "done"


async def test_web_form_source_skips_fetch(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(
        seller_intake=_intake(
            intake_complete=True,
            offer_workflow_id="offer:i1",
            missing_required=[],
            asked=[],
            draft_message_id=None,
            policy_decision=None,
        )
    )
    result = await fake.wf.run(
        SellerInboundInput(source="web_form", message_id="form-1", submission_id="s1")
    )
    assert _names(fake) == ["seller_classify_inbound", "seller_intake"]
    assert (
        result.intake_complete
        and result.offer_workflow_id == "offer:i1"
        and result.human_decision is None
    )
    assert fake.calls[0].arg.message_id == "form-1"


async def test_web_form_without_message_id_is_skipped(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    result = await fake.wf.run(SellerInboundInput(source="web_form"))
    assert result.skipped_reason == "no_message_id" and fake.calls == []


async def test_bounce_notifies_and_stops(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(fetch_message=_fetch(bounce=True))
    result = await fake.wf.run(SellerInboundInput(graph_id="g"))
    assert (
        _names(fake) == ["fetch_message", "seller_notify_bounce"]
        and result.skipped_reason == "bounce"
    )
    assert fake.calls[1].arg.bounced_address == "s@x.test"


async def test_not_found_stops(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(fetch_message=_fetch(found=False))
    result = await fake.wf.run(SellerInboundInput(graph_id="g"))
    assert result.skipped_reason == "not_found" and _names(fake) == ["fetch_message"]


@pytest.mark.parametrize("label", ["opt_out", "not_interested", "unrelated"])
async def test_no_intake_for_non_replies(
    sim: Callable[..., FakeWorkflowModule], label: str
) -> None:
    fake = sim(
        seller_classify_inbound=_classify(
            label, signalled="opt_out" if label == "opt_out" else None
        )
    )
    result = await fake.wf.run(SellerInboundInput(graph_id="g"))
    assert result.skipped_reason == f"no_intake:{label}" and "seller_intake" not in _names(fake)


async def test_auto_reply_and_not_seller_stop_early(sim: Callable[..., FakeWorkflowModule]) -> None:
    auto = sim(seller_classify_inbound=_classify("unrelated", is_auto_reply=True))
    assert (
        await auto.wf.run(SellerInboundInput(graph_id="g"))
    ).skipped_reason == "no_intake:unrelated"
    stranger = sim(seller_classify_inbound=_classify(None, skipped_reason="not_seller"))
    result = await stranger.wf.run(SellerInboundInput(graph_id="g"))
    assert result.skipped_reason == "not_seller" and "seller_intake" not in _names(stranger)


async def test_sent_or_blocked_reply_needs_no_human(sim: Callable[..., FakeWorkflowModule]) -> None:
    sent = sim(seller_intake=_intake(policy_decision="send", sent=True))
    result = await sent.wf.run(SellerInboundInput(graph_id="g"))
    assert (
        result.sent
        and result.human_decision is None
        and "seller_apply_human_decision" not in _names(sent)
    )
    blocked = sim(seller_intake=_intake(policy_decision="blocked"))
    result = await blocked.wf.run(SellerInboundInput(graph_id="g"))
    assert result.policy_decision == "blocked" and result.human_decision is None


async def test_human_timeout_leaves_the_draft_queued(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    result = await fake.wf.run(SellerInboundInput(graph_id="g"))
    assert result.human_decision == "timeout" and result.policy_decision == "queue_for_human"
    assert "seller_apply_human_decision" not in _names(fake)


async def test_rejection(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim()
    fake.events.append(
        (
            lambda wf: wf._state == "awaiting_human",
            lambda: fake.wf.human_decision(HumanDecision(approved=False)),
        )
    )
    result = await fake.wf.run(SellerInboundInput(graph_id="g"))
    assert result.human_decision == "rejected" and not result.sent


async def test_first_human_decision_wins() -> None:
    wf = SellerInboundWorkflow()
    await wf.human_decision(HumanDecision(approved=False))
    await wf.human_decision(HumanDecision(approved=True))
    assert wf._human_decision is not None and wf._human_decision.approved is False


async def test_intake_unreachable_stops(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(
        seller_intake=_intake(
            skipped_reason="no_email", draft_message_id=None, policy_decision=None
        )
    )
    result = await fake.wf.run(SellerInboundInput(graph_id="g"))
    assert result.skipped_reason == "no_email" and result.human_decision is None
