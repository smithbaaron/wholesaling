"""DispositionWorkflow contract checks that need no Temporal server (WF-06): type name, input
shape, the signal set the /deals routes and the review queue code against, registry membership,
and the pure deadline schedule arithmetic."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta

from temporalio import activity, workflow

from wholesaling.activities import disposition as acts
from wholesaling.signals import (
    SIGNAL_BUYER_RESPONSE,
    SIGNAL_BUYER_SELECTED,
    SIGNAL_DEADLINES_UPDATED,
    SIGNAL_DEAL_CLOSED,
    SIGNAL_DEAL_DEAD,
    SIGNAL_DEPOSIT_RECEIVED,
    SIGNAL_HUMAN_DECISION,
    BuyerResponseSignal,
    DealDeadSignal,
)
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows import deadlines as dl
from wholesaling.workflows import disposition as ws
from wholesaling.workflows.disposition import (
    BlastHumanDecision,
    DispositionInput,
    DispositionWorkflow,
    disposition_workflow_id,
)


def _defn() -> workflow._Definition:
    d = workflow._Definition.from_class(DispositionWorkflow)
    assert d is not None
    return d


def test_workflow_type_name_and_id() -> None:
    assert _defn().name == "DispositionWorkflow" == ws.WORKFLOW_NAME
    assert disposition_workflow_id("abc") == "disposition:abc"


def test_run_takes_single_dataclass_input_with_defaults() -> None:
    assert list(inspect.signature(DispositionWorkflow.run).parameters) == ["self", "input"]
    assert dataclasses.is_dataclass(DispositionInput)
    names = [f.name for f in dataclasses.fields(DispositionInput)]
    assert names == ["contract_id", "alert_offsets_days", "blast_limit", "include_fallback_buyers"]
    assert DispositionInput("c").alert_offsets_days == [3, 1, 0]


def test_signal_contract() -> None:
    d = _defn()
    assert set(d.signals) == {
        SIGNAL_BUYER_RESPONSE,
        SIGNAL_BUYER_SELECTED,
        SIGNAL_DEPOSIT_RECEIVED,
        SIGNAL_DEADLINES_UPDATED,
        SIGNAL_HUMAN_DECISION,
        SIGNAL_DEAL_DEAD,
        SIGNAL_DEAL_CLOSED,
    }
    assert set(d.queries) == {"status"}
    # Same shape as the sequence workflow's HumanDecision so the review queue needs no special case.
    fields = [f.name for f in dataclasses.fields(BlastHumanDecision)]
    assert fields == ["step", "approved", "edited_body", "actor_id", "message_id"]
    assert dataclasses.fields(BuyerResponseSignal)[0].name == "investor_id"
    assert DealDeadSignal("x").actor_id is None


def test_registered_in_worker_with_activities() -> None:
    assert DispositionWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {
        "disposition_load_contract",
        "disposition_run_blast",
        "disposition_send_approved_blast",
        "disposition_record_buyer_response",
        "disposition_raise_deadline_alert",
        "disposition_record_workflow_state",
        "disposition_deal_dead_followup",
    } <= names
    for fn in (acts.run_blast, acts.load_contract, acts.raise_deadline_alert):
        assert activity._Definition.from_callable(fn) is not None


def test_workflow_module_never_imports_docusign() -> None:
    """XC-05 / POL-03i: no automated path can reach the e-signature gateway."""
    import sys

    for mod in (
        "wholesaling.workflows.disposition",
        "wholesaling.activities.disposition",
        "wholesaling.agents.s6",
    ):
        source = inspect.getsource(sys.modules[mod])
        assert "integrations.docusign" not in source, mod
        imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        assert not any("docusign" in line.lower() for line in imports), mod


# -- deadline arithmetic ---------------------------------------------------------------------------

T0 = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)


def test_schedule_three_alerts_per_deadline_soonest_first() -> None:
    sched = dl.schedule({"closing_date": T0, "inspection_deadline": T0 - timedelta(days=10)})
    assert [a.key for a in sched] == [
        "inspection_deadline:T-3d",
        "inspection_deadline:T-1d",
        "inspection_deadline:T-0",
        "closing_date:T-3d",
        "closing_date:T-1d",
        "closing_date:T-0",
    ]
    assert sched[0].due_at == T0 - timedelta(days=13) and sched[-1].due_at == T0
    assert sched[0].label == "T-3d" and sched[2].label == "T-0"


def test_pending_due_and_next_honour_raised_and_now() -> None:
    deadlines = {"closing_date": T0}
    now = T0 - timedelta(days=2)
    assert [a.key for a in dl.due(deadlines, set(), now)] == ["closing_date:T-3d"]
    assert dl.next_due(deadlines, set(), now) is not None
    assert dl.next_due(deadlines, set(), now).key == "closing_date:T-1d"  # type: ignore[union-attr]
    raised = {"closing_date:T-3d", "closing_date:T-1d"}
    assert [a.key for a in dl.pending(deadlines, raised)] == ["closing_date:T-0"]
    assert dl.due(deadlines, raised, now) == []
    assert (
        dl.next_due(deadlines, {"closing_date:T-3d", "closing_date:T-1d", "closing_date:T-0"}, now)
        is None
    )
    assert dl.pending({}, set()) == []


def test_parse_deadlines_from_activity_strings() -> None:
    parsed = dl.parse_deadlines(
        {"closing_date": T0.isoformat(), "option_period_end": None, "inspection_deadline": ""}
    )
    assert parsed == {"closing_date": T0}
    naive = dl.parse_deadlines({"closing_date": "2026-11-20T17:00:00"})
    assert naive["closing_date"].tzinfo is UTC


def test_custom_offsets() -> None:
    sched = dl.schedule({"option_period_end": T0}, offsets_days=[7, 0])
    assert [a.key for a in sched] == ["option_period_end:T-7d", "option_period_end:T-0"]
