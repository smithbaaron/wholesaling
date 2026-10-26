"""SellerSequenceWorkflow / SellerListRefreshWorkflow contract checks (no Temporal server)."""

from __future__ import annotations

import dataclasses
import inspect

from temporalio import activity, workflow

from wholesaling.activities import seller as acts
from wholesaling.db.repositories.review import HumanDecision as ApiHumanDecision
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows import seller_sequence as ws
from wholesaling.workflows.schedules import get_schedule
from wholesaling.workflows.seller_sequence import (
    HumanDecision,
    SellerListRefreshWorkflow,
    SellerSequenceInput,
    SellerSequenceWorkflow,
    seller_sequence_workflow_id,
)


def _defn(cls: type) -> workflow._Definition:
    d = workflow._Definition.from_class(cls)
    assert d is not None
    return d


def test_workflow_type_names_are_stable() -> None:
    assert _defn(SellerSequenceWorkflow).name == "SellerSequenceWorkflow" == ws.WORKFLOW_NAME
    assert _defn(SellerListRefreshWorkflow).name == "SellerListRefreshWorkflow"
    assert acts.SELLER_SEQUENCE_WORKFLOW_NAME == ws.WORKFLOW_NAME


def test_run_takes_single_dataclass_input_with_3_7_14_default() -> None:
    assert list(inspect.signature(SellerSequenceWorkflow.run).parameters) == ["self", "input"]
    names = [f.name for f in dataclasses.fields(SellerSequenceInput)]
    assert names == [
        "sequence_id",
        "seller_lead_id",
        "contact_email",
        "market",
        "intervals_days",
        "honor_send_window",
    ]
    assert SellerSequenceInput("s", "l", "e", "m").intervals_days == [3, 7, 14]  # OPN-05


def test_signal_contract_includes_intake_complete() -> None:
    d = _defn(SellerSequenceWorkflow)
    assert set(d.signals) == {
        "human_decision",
        "reply",
        "opt_out",
        "bounce",
        "intake_complete",
        "pause",
        "resume",
    }
    assert set(d.queries) == {"status"}
    assert acts.SIGNAL_INTAKE_COMPLETE == "intake_complete"
    assert [f.name for f in dataclasses.fields(acts.IntakeCompleteSignal)] == [
        "intake_id",
        "seller_lead_id",
    ]


def test_human_decision_is_the_investor_payload_and_compatible_with_the_review_queue() -> None:
    from wholesaling.workflows.investor_sequence import HumanDecision as InvestorHumanDecision

    assert HumanDecision is InvestorHumanDecision
    ours = {f.name for f in dataclasses.fields(HumanDecision)}
    theirs = {f.name for f in dataclasses.fields(ApiHumanDecision)}
    assert theirs <= ours


def test_workflow_and_activities_are_registered() -> None:
    assert SellerSequenceWorkflow in WORKFLOWS and SellerListRefreshWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {
        "seller_list_refresh",
        "seller_prepare_step",
        "seller_decide_and_send",
        "seller_send_approved",
        "outreach_check_cancel_conditions",
        "outreach_send_window",
        "outreach_update_sequence",
    } <= names


def test_weekly_refresh_schedule_exists() -> None:
    spec = get_schedule("seller-list-refresh")
    assert spec.workflow == "SellerListRefreshWorkflow" and spec.interval.days == 7
    assert spec.overlap == "skip" and "WF-10" in spec.requirement
    assert isinstance(spec.args[0], acts.SellerListRefreshInput) and spec.args[0].market_id is None


def test_ids_are_deterministic() -> None:
    assert seller_sequence_workflow_id("abc") == "seller-sequence:abc"
    assert acts.offer_workflow_id("i1") == "offer:i1"
    assert acts.seller_inbound_workflow_id("form", "s1") == "seller-inbound:form:s1"
    assert acts.seller_reply_idempotency_key("m") == "seller-reply:m"


def test_offer_workflow_contract() -> None:
    assert acts.OFFER_WORKFLOW_NAME == "OfferWorkflow"
    assert [f.name for f in dataclasses.fields(acts.OfferWorkflowInput)] == [
        "intake_id",
        "seller_lead_id",
    ]


def test_status_before_run() -> None:
    wf = SellerSequenceWorkflow()
    status = wf.status()
    assert (
        status["state"] == "pending" and status["decisions"] == {} and status["last_signal"] is None
    )
    wf.intake_complete(acts.IntakeCompleteSignal(intake_id="i", seller_lead_id="l"))
    wf.reply()  # a later cancelling signal is a no-op
    assert wf.status()["cancel_reason"] == "intake_complete"
    assert wf.status()["last_signal"] == {
        "signal": "intake_complete",
        "intake_id": "i",
        "seller_lead_id": "l",
    }
