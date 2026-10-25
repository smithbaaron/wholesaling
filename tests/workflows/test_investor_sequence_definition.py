"""InvestorSequenceWorkflow contract checks that need no Temporal server.

The signal names and the ``HumanDecision`` payload are the contract the review-queue API codes
against (docs/agents/outreach.md); changing them is a breaking change.
"""

from __future__ import annotations

import dataclasses
import inspect

from temporalio import activity, workflow

from wholesaling.activities import outreach as acts
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows import investor_sequence as ws
from wholesaling.workflows.investor_sequence import (
    HumanDecision,
    InvestorSequenceInput,
    InvestorSequenceWorkflow,
    investor_sequence_workflow_id,
)


def _defn() -> workflow._Definition:
    d = workflow._Definition.from_class(InvestorSequenceWorkflow)
    assert d is not None
    return d


def test_workflow_type_name_is_stable() -> None:
    assert _defn().name == "InvestorSequenceWorkflow" == ws.WORKFLOW_NAME


def test_run_takes_single_dataclass_input() -> None:
    params = list(inspect.signature(InvestorSequenceWorkflow.run).parameters)
    assert params == ["self", "input"]
    assert dataclasses.is_dataclass(InvestorSequenceInput)
    names = [f.name for f in dataclasses.fields(InvestorSequenceInput)]
    assert names == [
        "sequence_id",
        "investor_id",
        "contact_email",
        "market",
        "intervals_days",
        "honor_send_window",
    ]
    assert InvestorSequenceInput("s", "i", "e", "m").intervals_days == [4, 4, 4]  # OPN-04


def test_signal_contract() -> None:
    d = _defn()
    assert set(d.signals) == {"human_decision", "reply", "opt_out", "bounce", "pause", "resume"}
    assert set(d.queries) == {"status"}
    fields = {f.name: f for f in dataclasses.fields(HumanDecision)}
    assert list(fields) == ["step", "approved", "edited_body", "actor_id", "message_id"]
    assert fields["edited_body"].default is None and fields["actor_id"].default is None
    assert fields["message_id"].default is None
    assert HumanDecision(step=0, approved=True) == HumanDecision(0, True, None, None, None)


def test_human_decision_payload_is_compatible_with_the_review_queue() -> None:
    """The API side declares its own dataclass; both must agree field for field."""
    from wholesaling.db.repositories.review import HUMAN_DECISION_SIGNAL
    from wholesaling.db.repositories.review import HumanDecision as ApiHumanDecision

    assert HUMAN_DECISION_SIGNAL == "human_decision"
    ours = {f.name for f in dataclasses.fields(HumanDecision)}
    theirs = {f.name for f in dataclasses.fields(ApiHumanDecision)}
    assert theirs <= ours, f"review queue sends fields the workflow does not know: {theirs - ours}"


def test_human_decision_without_step_maps_onto_the_pending_message() -> None:
    wf = InvestorSequenceWorkflow()
    wf._pending_message_id = "msg-2"
    wf._step = 2
    wf.human_decision(HumanDecision(step=None, approved=True, message_id="other"))
    assert wf._human == {} and wf.status()["unmatched_decisions"] == 1
    wf.human_decision(HumanDecision(step=None, approved=True, message_id="msg-2"))
    assert wf._human[2].approved is True


def test_workflow_and_activities_are_registered() -> None:
    assert InvestorSequenceWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {
        "outreach_prepare_step",
        "outreach_decide_and_send",
        "outreach_send_approved",
        "outreach_check_cancel_conditions",
        "outreach_send_window",
        "outreach_update_sequence",
    } <= names
    for fn in (
        acts.prepare_step,
        acts.decide_and_send,
        acts.send_approved,
        acts.check_cancel_conditions,
        acts.send_window,
        acts.update_sequence,
    ):
        assert fn in ACTIVITIES


def test_workflow_id_is_deterministic() -> None:
    assert investor_sequence_workflow_id("abc") == "investor-sequence:abc"
    assert investor_sequence_workflow_id("abc") == investor_sequence_workflow_id("abc")


def test_idempotency_key_format() -> None:
    assert acts.idempotency_key("abc", 2) == "seq:abc:step:2"


def test_status_before_run_and_after_signals() -> None:
    wf = InvestorSequenceWorkflow()
    status = wf.status()
    assert status == {
        "sequence_id": "",
        "state": "pending",
        "step": 0,
        "steps_sent": [],
        "paused": False,
        "cancel_reason": None,
        "remaining_seconds": 0.0,
        "decisions": {},
        "pending_human_step": None,
        "pending_message_id": None,
        "unmatched_decisions": 0,
    }
    wf.pause()
    assert wf.status()["paused"] is True
    wf.resume()
    assert wf.status()["paused"] is False
    wf.reply()
    wf.opt_out()  # first cancel reason wins
    assert wf.status()["cancel_reason"] == "reply"


def test_cancel_signals_each_set_their_reason() -> None:
    for name in ("reply", "opt_out", "bounce"):
        wf = InvestorSequenceWorkflow()
        getattr(wf, name)()
        assert wf.status()["cancel_reason"] == name


def test_human_decision_latest_wins_until_step_is_sent() -> None:
    wf = InvestorSequenceWorkflow()
    wf.human_decision(HumanDecision(step=1, approved=False))
    wf.human_decision(HumanDecision(step=1, approved=True, edited_body="hi"))
    assert wf._human[1].approved is True and wf._human[1].edited_body == "hi"
    wf._steps_sent.append(1)
    wf.human_decision(HumanDecision(step=1, approved=False))
    assert wf._human[1].approved is True


def test_activity_timeouts_and_retry_are_bounded() -> None:
    assert ws.ACTIVITY_TIMEOUT.total_seconds() <= 600
    assert ws.ACTIVITY_RETRY.maximum_attempts == 3
