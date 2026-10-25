"""Temporal integration: narrow gateway Protocol, live client, in-memory mock, factory."""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.temporal.interface import (
    RecordedCall,
    TemporalGateway,
    WorkflowHandleInfo,
)
from wholesaling.integrations.temporal.mock import MockTemporalGateway


def get_temporal(settings: Settings) -> TemporalGateway:
    """Return the gateway implementation selected by ``TEMPORAL_MODE``."""
    if settings.temporal_mode == "mock":
        return MockTemporalGateway(task_queue=settings.temporal_task_queue)
    # Imported lazily so mock-mode processes never import the live module (RUL-02 pattern).
    from wholesaling.integrations.temporal.client import LiveTemporalGateway

    return LiveTemporalGateway(settings)


__all__ = [
    "MockTemporalGateway",
    "RecordedCall",
    "TemporalGateway",
    "WorkflowHandleInfo",
    "get_temporal",
]
