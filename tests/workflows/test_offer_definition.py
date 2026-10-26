"""Contract checks for OfferWorkflow, NegotiationWorkflow and BuyBoxSearch (no Temporal server).

The S4 builder starts ``OfferWorkflow`` by name with ``OfferWorkflowInput(intake_id,
seller_lead_id)`` and id ``offer:{intake_id}``; the review queue signals ``human_decision`` with
the shared ``HumanDecision`` dataclass; D1 starts ``BuyBoxSearch`` and signals ``superseded``.
"""

from __future__ import annotations

import dataclasses
import inspect

from temporalio import activity, workflow

from wholesaling.activities import deals as dacts
from wholesaling.activities import offers as oacts
from wholesaling.db.repositories.review import HumanDecision as ReviewHumanDecision
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows import buy_box_search as wbs
from wholesaling.workflows import negotiation as wn
from wholesaling.workflows import offer as wo
from wholesaling.workflows.buy_box_search import BuyBoxSearch, BuyBoxSearchInput, SupersededSignal
from wholesaling.workflows.negotiation import (
    CounterSignal,
    HumanOverride,
    NegotiationInput,
    NegotiationWorkflow,
)
from wholesaling.workflows.offer import (
    HumanDecision,
    OfferWorkflow,
    OfferWorkflowInput,
    offer_workflow_id,
)


def _defn(cls: type) -> workflow._Definition:
    d = workflow._Definition.from_class(cls)
    assert d is not None
    return d


def test_offer_workflow_contract_matches_the_s4_builder() -> None:
    assert _defn(OfferWorkflow).name == "OfferWorkflow" == wo.WORKFLOW_NAME
    assert offer_workflow_id("abc") == "offer:abc"
    assert list(inspect.signature(OfferWorkflow.run).parameters) == ["self", "input"]
    assert [f.name for f in dataclasses.fields(OfferWorkflowInput)] == [
        "intake_id",
        "seller_lead_id",
    ]
    # The S4 side declares its own copy of the input; field names must match ours.
    from wholesaling.activities.seller import OfferWorkflowInput as S4Input

    assert [f.name for f in dataclasses.fields(S4Input)] == ["intake_id", "seller_lead_id"]


def test_offer_workflow_signals_and_human_decision_payload() -> None:
    d = _defn(OfferWorkflow)
    assert set(d.signals) == {"human_decision"} and set(d.queries) == {"status"}
    ours = {f.name for f in dataclasses.fields(HumanDecision)}
    theirs = {f.name for f in dataclasses.fields(ReviewHumanDecision)}
    assert ours == theirs == {"step", "approved", "edited_body", "actor_id", "message_id"}


def test_negotiation_workflow_contract() -> None:
    d = _defn(NegotiationWorkflow)
    assert d.name == "NegotiationWorkflow" == wn.WORKFLOW_NAME == oacts.NEGOTIATION_WORKFLOW_NAME
    assert set(d.signals) == {"counter", "accept", "decline", "human_override"} and set(
        d.queries
    ) == {"status"}
    assert (
        [f.name for f in dataclasses.fields(NegotiationInput)]
        == [f.name for f in dataclasses.fields(oacts.NegotiationStartInput)]
        == ["offer_id", "seller_lead_id"]
    )
    assert [f.name for f in dataclasses.fields(CounterSignal)] == ["amount", "message_id"]
    assert [f.name for f in dataclasses.fields(HumanOverride)] == [
        "action",
        "amount",
        "reason",
        "actor_id",
    ]
    assert wn.negotiation_workflow_id("o") == oacts.negotiation_workflow_id("o") == "negotiation:o"


def test_buy_box_search_contract() -> None:
    d = _defn(BuyBoxSearch)
    assert d.name == "BuyBoxSearch" == wbs.WORKFLOW_NAME == dacts.BUY_BOX_SEARCH_WORKFLOW_NAME
    assert set(d.signals) == {"superseded"} == {dacts.SUPERSEDED_SIGNAL} and set(d.queries) == {
        "status"
    }
    assert [f.name for f in dataclasses.fields(BuyBoxSearchInput)] == [
        f.name for f in dataclasses.fields(dacts.BuyBoxSearchStartInput)
    ]
    assert (
        [f.name for f in dataclasses.fields(SupersededSignal)]
        == [f.name for f in dataclasses.fields(dacts.SupersededPayload)]
        == ["new_version", "new_search_profile_id"]
    )
    assert (
        wbs.buy_box_search_workflow_id("b", 2)
        == dacts.buy_box_search_workflow_id("b", 2)
        == "buy-box-search:b:v2"
    )


def test_everything_is_registered_on_the_worker() -> None:
    assert {OfferWorkflow, NegotiationWorkflow, BuyBoxSearch} <= set(WORKFLOWS)
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    expected = {
        "offer_compute",
        "offer_qa_draft",
        "offer_pre_send_check",
        "offer_decide_and_send",
        "offer_send_approved",
        "offer_withdraw",
        "offer_handoff_to_s5",
        "negotiation_load",
        "negotiation_evaluate_counter",
        "negotiation_accept",
        "negotiation_decline",
        "negotiation_expire",
        "negotiation_send_message",
        "negotiation_requeue_seller",
        "deals_run_d1",
        "deals_run_d2",
    }
    assert expected <= names


def test_workflows_have_no_sleeps_except_negotiation() -> None:
    for mod in (wo, wbs):
        src = inspect.getsource(mod)
        assert (
            "asyncio.sleep" not in src and "workflow.sleep" not in src
        )  # WF-04 / WF-07: no sleeps
    assert "wait_condition" in inspect.getsource(wn)  # WF-05 durable timers
