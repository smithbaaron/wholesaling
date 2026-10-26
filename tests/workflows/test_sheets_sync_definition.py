"""BO-08 workflow contracts without a Temporal server: ``SheetsSyncWorkflow`` (WF-13),
``DailyReconciliationWorkflow`` (WF-14), ``BuyBoxEventWorkflow`` (WF-20); worker registration;
schedule registry entries; settings keys."""

from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity, workflow

from wholesaling.activities.sheets import reconcile_sources, sync_projection
from wholesaling.activities.zapier import send_parser_email
from wholesaling.config.settings import Settings
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows import SCHEDULES
from wholesaling.workflows.buy_box_event import (
    EVENT_TYPES,
    BuyBoxEventInput,
    BuyBoxEventWorkflow,
    buy_box_event_workflow_id,
)
from wholesaling.workflows.reconciliation import (
    DailyReconciliationInput,
    DailyReconciliationWorkflow,
)
from wholesaling.workflows.schedules import get_schedule
from wholesaling.workflows.sheets_sync import SheetsSyncInput, SheetsSyncWorkflow

REPO_ROOT = Path(__file__).resolve().parents[2]
BO08_WORKFLOWS = [SheetsSyncWorkflow, DailyReconciliationWorkflow, BuyBoxEventWorkflow]


@pytest.mark.parametrize("wf", BO08_WORKFLOWS, ids=lambda w: w.__name__)
def test_definitions_take_single_dataclass_input_and_expose_status(wf: type) -> None:
    defn = workflow._Definition.from_class(wf)
    assert defn is not None and defn.name == wf.__name__
    assert "status" in defn.queries and defn.signals == {}
    assert list(inspect.signature(wf.run).parameters) == ["self", "input"]
    assert wf().status() == {"state": "pending", "result": None}


def test_inputs_have_safe_defaults() -> None:
    assert [f.name for f in fields(SheetsSyncInput)] == ["tabs"]
    assert SheetsSyncInput().tabs is None
    assert [f.name for f in fields(DailyReconciliationInput)] == ["lookback_hours", "tabs"]
    assert DailyReconciliationInput().lookback_hours == 24
    names = [f.name for f in fields(BuyBoxEventInput)]
    assert names[:3] == ["buy_box_id", "investor_id", "version"]
    assert (
        BuyBoxEventInput(buy_box_id="b", investor_id="i", version=1).event_type == "buy_box.updated"
    )


def test_registered_in_worker() -> None:
    for wf in BO08_WORKFLOWS:
        assert wf in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {
        "sheets_sync_projection",
        "sheets_reconcile_sources",
        "zapier_send_parser_email",
    } <= names
    for act in (sync_projection, reconcile_sources, send_parser_email):
        assert act in ACTIVITIES


def test_sheets_sync_schedule_every_15_minutes() -> None:
    spec = get_schedule("sheets-sync")
    assert spec in SCHEDULES
    assert spec.workflow == "SheetsSyncWorkflow"
    assert spec.interval == timedelta(minutes=15)  # WF-13
    assert spec.overlap == "skip"
    assert spec.args == (SheetsSyncInput(),)
    assert spec.requirement == "WF-13"
    assert "one-way" in spec.note.lower() and "never deletes" in spec.note
    assert spec.workflow_id_prefix == "sched-sheets-sync"
    assert "sheets" in spec.tags


def test_daily_reconciliation_schedule_daily() -> None:
    spec = get_schedule("daily-reconciliation")
    assert spec.workflow == "DailyReconciliationWorkflow"
    assert spec.interval == timedelta(days=1)  # WF-14
    assert spec.args == (DailyReconciliationInput(),)
    assert "XC-09" in spec.requirement
    assert "never" in spec.note.lower() and "overwrite" in spec.note.lower()
    assert {"sheets", "graph", "reconciliation"} <= spec.tags


def test_schedule_workflows_all_registered_and_ids_unique() -> None:
    registered = {workflow._Definition.from_class(w).name for w in WORKFLOWS}  # type: ignore[union-attr]
    for spec in SCHEDULES:
        assert spec.workflow in registered, spec.id
    ids = [s.id for s in SCHEDULES]
    assert len(ids) == len(set(ids))


def test_buy_box_event_workflow_id_is_per_version_and_event_types_fixed() -> None:
    assert buy_box_event_workflow_id("bb-1", 2) == "buy-box-event-bb-1-v2"
    assert buy_box_event_workflow_id("bb-1", 2) != buy_box_event_workflow_id("bb-1", 3)
    assert frozenset({"buy_box.created", "buy_box.updated"}) == EVENT_TYPES


def test_buy_box_event_workflow_has_d1_extension_point() -> None:
    assert hasattr(BuyBoxEventWorkflow, "_run_search_steps")
    doc = BuyBoxEventWorkflow._run_search_steps.__doc__ or ""
    assert "BO-11" in doc and "D1" in doc and "D2" in doc
    # The parser step is invoked after the search step (source order contract).
    src = inspect.getsource(BuyBoxEventWorkflow.run)
    assert src.index("_run_search_steps") < src.index("send_parser_email")


def test_settings_keys_for_bo08_exist_with_safe_defaults() -> None:
    s = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    assert s.sheets_mode == "mock"
    assert s.google_service_account_json is None and s.sheets_spreadsheet_id is None
    assert s.zapier_parser_enabled is False and s.zapier_parser_address is None
    env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "SHEETS_MODE",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "SHEETS_SPREADSHEET_ID",
        "ZAPIER_PARSER_ENABLED",
        "ZAPIER_PARSER_ADDRESS",
    ):
        assert f"{key}=" in env
