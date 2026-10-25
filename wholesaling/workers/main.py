"""Temporal worker entrypoint (``wholesaling-worker`` script).

Registries are plain lists so tests can assert every entry is importable and decorated, and
so later build steps add workflows/activities in one place.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from wholesaling import __version__
from wholesaling.activities.hello import heartbeat_probe, hello
from wholesaling.config.settings import Settings, get_settings
from wholesaling.logging import configure_logging, get_logger
from wholesaling.workflows.hello import HelloWorkflow

log = get_logger(__name__)

WORKFLOWS: list[type] = [HelloWorkflow]
ACTIVITIES: list[Callable[..., Any]] = [hello, heartbeat_probe]


def build_worker(
    client: Client,
    task_queue: str,
    *,
    workflows: Sequence[type] | None = None,
    activities: Sequence[Callable[..., Any]] | None = None,
) -> Worker:
    """Build a worker over the registries (overridable for tests)."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=list(workflows if workflows is not None else WORKFLOWS),
        activities=list(activities if activities is not None else ACTIVITIES),
    )


def _install_shutdown_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:
        log.info("worker.shutdown_requested", signal=sig.name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            signal.signal(sig, lambda s, _f: _request_stop(signal.Signals(s)))


async def serve(settings: Settings | None = None) -> None:
    """Connect, run the worker until SIGTERM/SIGINT, then shut down gracefully."""
    settings = settings or get_settings()
    configure_logging(settings)

    if settings.temporal_mode != "live":
        # The worker only exists to poll a real Temporal server. Mock mode is for the API and
        # tests; refuse to start rather than silently idle.
        log.error(
            "worker.refused",
            reason="TEMPORAL_MODE must be 'live' to run a worker",
            temporal_mode=settings.temporal_mode,
        )
        msg = "Worker requires TEMPORAL_MODE=live"
        raise SystemExit(msg)

    if settings.is_production:
        problems = settings.validate_for_production()
        if problems:
            log.error("worker.refused", problems=problems)
            raise SystemExit("; ".join(problems))

    from wholesaling.integrations.temporal.client import connect_client

    client = await connect_client(settings)
    worker = build_worker(client, settings.temporal_task_queue)

    log.info(
        "worker.started",
        version=__version__,
        app_env=settings.app_env.value,
        temporal_mode=settings.temporal_mode,
        temporal_address=settings.temporal_address,
        temporal_namespace=settings.temporal_namespace,
        task_queue=settings.temporal_task_queue,
        workflows=[getattr(w, "__name__", str(w)) for w in WORKFLOWS],
        activities=[getattr(a, "__name__", str(a)) for a in ACTIVITIES],
    )

    stop = asyncio.Event()
    _install_shutdown_handlers(stop)
    async with worker:
        await stop.wait()
    # Leaving the context manager runs Worker.shutdown(): in-flight activities finish,
    # pending tasks are returned to the queue (WF-22: nothing lost or duplicated).
    log.info("worker.stopped", task_queue=settings.temporal_task_queue)


def run() -> None:
    """Console-script entrypoint."""
    asyncio.run(serve())


if __name__ == "__main__":
    run()
