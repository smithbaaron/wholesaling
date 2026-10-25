"""Hello-world activities: the smallest activities that prove the worker runs end to end."""

from __future__ import annotations

from datetime import UTC, datetime

from temporalio import activity

from wholesaling.logging import bind_correlation_id, clear_context, get_logger

log = get_logger(__name__)


def _bind_workflow_context() -> None:
    """Bind the workflow id as correlation id when running inside a Temporal activity."""
    if activity.in_activity():
        info = activity.info()
        bind_correlation_id(info.workflow_id)


@activity.defn(name="hello")
async def hello(name: str) -> str:
    _bind_workflow_context()
    try:
        greeting = f"Hello, {name}!"
        log.info("activity.hello", name=name)
        return greeting
    finally:
        clear_context()


@activity.defn(name="heartbeat_probe")
async def heartbeat_probe() -> str:
    """Return the worker's current UTC time (ISO 8601). Used by the watchdog later (WF-23)."""
    _bind_workflow_context()
    try:
        now = datetime.now(tz=UTC).isoformat()
        log.debug("activity.heartbeat_probe", server_time=now)
        return now
    finally:
        clear_context()
