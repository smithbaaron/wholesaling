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

from wholesaling.workflows.graph_maintenance import (
    DeltaPollWorkflowInput,
    SubscriptionRenewalInput,
)

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
]


def get_schedule(schedule_id: str) -> ScheduleSpec:
    for spec in SCHEDULES:
        if spec.id == schedule_id:
            return spec
    msg = f"unknown schedule {schedule_id!r}"
    raise KeyError(msg)


__all__ = ["SCHEDULES", "OverlapPolicy", "ScheduleSpec", "get_schedule"]
