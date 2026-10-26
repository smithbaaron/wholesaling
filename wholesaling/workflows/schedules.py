"""Registry of Temporal Schedules the system needs (REQUIREMENTS.md WF-11, WF-12).

This is data only. Nothing here talks to Temporal; an ops command (or the worker, later)
reads ``SCHEDULES`` and creates/updates the schedules through the SDK. Keeping it declarative
means tests can assert every scheduled workflow is registered in the worker and that the
cadences match the requirements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

from wholesaling.workflows.daily_digest import DailyDigestInput
from wholesaling.workflows.graph_maintenance import (
    DeltaPollWorkflowInput,
    SubscriptionRenewalInput,
)
from wholesaling.workflows.reconciliation import DailyReconciliationInput
from wholesaling.workflows.sheets_sync import SheetsSyncInput

OverlapPolicy = Literal["skip", "buffer_one", "allow_all"]


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    id: str
    workflow: str  # registered workflow type name
    interval: timedelta
    args: tuple[Any, ...] = ()
    overlap: OverlapPolicy = "skip"
    task_queue: str | None = None  # None = settings.temporal_task_queue
    note: str = ""
    requirement: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def workflow_id_prefix(self) -> str:
        return f"sched-{self.id}"


SCHEDULES: list[ScheduleSpec] = [
    ScheduleSpec(
        id="graph-subscription-renewal",
        workflow="GraphSubscriptionRenewalWorkflow",
        interval=timedelta(hours=48),
        args=(SubscriptionRenewalInput(),),
        overlap="skip",
        note=(
            "Renews the inbox change-notification subscription to now+70h. Graph caps Outlook "
            "message subscriptions at 10,080 min (7 days). Alerts after 2 consecutive failures."
        ),
        requirement="WF-11",
        tags=frozenset({"graph"}),
    ),
    ScheduleSpec(
        id="graph-delta-poll",
        workflow="GraphDeltaPollWorkflow",
        interval=timedelta(minutes=15),
        args=(DeltaPollWorkflowInput(),),
        overlap="skip",
        note="Reconciles missed webhooks over the last 24 h through the idempotent upsert.",
        requirement="WF-12",
        tags=frozenset({"graph"}),
    ),
    ScheduleSpec(
        id="daily-digest",
        workflow="DailyDigestWorkflow",
        interval=timedelta(days=1),
        args=(DailyDigestInput(),),
        overlap="skip",
        note=(
            "Daily KPI digest (BO-21) through the configured sinks. Fires once a day at "
            "DIGEST_HOUR_LOCAL in OPERATOR_TIMEZONE (defaults 07:00 America/Chicago); the ops "
            "command that materialises schedules uses digest_calendar_spec(settings) for the "
            "calendar spec. WF-18's fixed review time was rejected: the review queue itself is "
            "not time-gated, only this summary is."
        ),
        requirement="BO-07 / INT-18",
        tags=frozenset({"digest", "reporting"}),
    ),
    ScheduleSpec(
        id="sheets-sync",
        workflow="SheetsSyncWorkflow",
        interval=timedelta(minutes=15),
        args=(SheetsSyncInput(),),
        overlap="skip",
        note=(
            "One-way Postgres -> Google Sheets projection of every tab in TAB_ORDER, idempotent "
            "by row key; never deletes (deactivated rows change status). Agents never write to "
            "Sheets directly; Postgres is the only writer of truth."
        ),
        requirement="WF-13",
        tags=frozenset({"sheets"}),
    ),
    ScheduleSpec(
        id="daily-reconciliation",
        workflow="DailyReconciliationWorkflow",
        interval=timedelta(days=1),
        args=(DailyReconciliationInput(),),
        overlap="skip",
        note=(
            "Compares Outlook (Graph delta, last 24 h) with messages and Postgres with every "
            "Sheets tab; records mismatches as an audit event and raises an alert. Never "
            "silently overwrites anything (XC-09)."
        ),
        requirement="WF-14 / XC-09",
        tags=frozenset({"sheets", "graph", "reconciliation"}),
    ),
]


def digest_calendar_spec(settings: Any) -> dict[str, Any]:
    """Calendar spec for the ``daily-digest`` schedule from ``DIGEST_HOUR_LOCAL`` and
    ``OPERATOR_TIMEZONE``: ``{"hour": H, "minute": 0, "time_zone": TZ}`` (the SDK's
    ``ScheduleCalendarSpec`` field names)."""
    return {
        "hour": int(settings.digest_hour_local),
        "minute": 0,
        "time_zone": str(settings.operator_timezone),
    }


def get_schedule(schedule_id: str) -> ScheduleSpec:
    for spec in SCHEDULES:
        if spec.id == schedule_id:
            return spec
    msg = f"unknown schedule {schedule_id!r}"
    raise KeyError(msg)


__all__ = ["SCHEDULES", "OverlapPolicy", "ScheduleSpec", "digest_calendar_spec", "get_schedule"]
