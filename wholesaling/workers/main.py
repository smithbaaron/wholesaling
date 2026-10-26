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
from wholesaling.activities.conversation import (
    apply_human_decision,
    classify_inbound,
    draft_and_qa_reply,
    extract_and_upsert_buy_box,
    notify_bounce,
)
from wholesaling.activities.deals import run_d1, run_d2
from wholesaling.activities.digest import compute_and_send_digest
from wholesaling.activities.discovery import run_a1, run_a2, run_m1
from wholesaling.activities.disposition import (
    deal_dead_followup,
    load_contract,
    raise_deadline_alert,
    record_buyer_response,
    record_workflow_state,
    run_blast,
    send_approved_blast,
)
from wholesaling.activities.graph import (
    create_draft,
    delta_poll,
    fetch_message,
    raise_alert,
    record_renewal_outcome,
    renew_subscriptions,
    send_draft,
)
from wholesaling.activities.hello import heartbeat_probe, hello
from wholesaling.activities.learning import compute_learning_stats
from wholesaling.activities.offers import (
    accept_offer,
    compute_offer,
    decide_and_send_offer,
    decline_offer,
    evaluate_counter,
    expire_offer,
    handoff_to_s5,
    load_negotiation,
    pre_send_check,
    qa_offer_draft,
    requeue_seller,
    send_approved_offer,
    send_negotiation_message,
    withdraw_offer,
)
from wholesaling.activities.outreach import (
    check_cancel_conditions,
    decide_and_send,
    prepare_step,
    send_approved,
    send_window,
    update_sequence,
)
from wholesaling.activities.seller import (
    seller_apply_human_decision,
    seller_classify_inbound,
    seller_decide_and_send,
    seller_intake,
    seller_list_refresh,
    seller_notify_bounce,
    seller_prepare_step,
    seller_send_approved,
)
from wholesaling.activities.sequence_starter import (
    run_outreach_kickoff,
    start_investor_sequences,
)
from wholesaling.activities.sheets import reconcile_sources, sync_projection
from wholesaling.activities.zapier import send_parser_email
from wholesaling.config.settings import Settings, get_settings
from wholesaling.logging import configure_logging, get_logger
from wholesaling.workflows.buy_box_event import BuyBoxEventWorkflow
from wholesaling.workflows.buy_box_search import BuyBoxSearch
from wholesaling.workflows.daily_digest import DailyDigestWorkflow
from wholesaling.workflows.disposition import DispositionWorkflow
from wholesaling.workflows.graph_maintenance import (
    GraphDeltaPollWorkflow,
    GraphInboundMessageWorkflow,
    GraphSubscriptionRenewalWorkflow,
)
from wholesaling.workflows.hello import HelloWorkflow
from wholesaling.workflows.investor_sequence import InvestorSequenceWorkflow
from wholesaling.workflows.learning_stats import LearningStatsWorkflow
from wholesaling.workflows.negotiation import NegotiationWorkflow
from wholesaling.workflows.offer import OfferWorkflow
from wholesaling.workflows.outreach_kickoff import OutreachKickoffWorkflow
from wholesaling.workflows.reconciliation import DailyReconciliationWorkflow
from wholesaling.workflows.seller_inbound import SellerInboundWorkflow
from wholesaling.workflows.seller_sequence import (
    SellerListRefreshWorkflow,
    SellerSequenceWorkflow,
)
from wholesaling.workflows.sheets_sync import SheetsSyncWorkflow

log = get_logger(__name__)

WORKFLOWS: list[type] = [
    HelloWorkflow,
    GraphSubscriptionRenewalWorkflow,
    GraphDeltaPollWorkflow,
    GraphInboundMessageWorkflow,
    InvestorSequenceWorkflow,
    OutreachKickoffWorkflow,
    DailyDigestWorkflow,
    SheetsSyncWorkflow,
    DailyReconciliationWorkflow,
    BuyBoxEventWorkflow,
    DispositionWorkflow,
    SellerSequenceWorkflow,
    SellerListRefreshWorkflow,
    SellerInboundWorkflow,
    OfferWorkflow,
    NegotiationWorkflow,
    BuyBoxSearch,
    LearningStatsWorkflow,
]
ACTIVITIES: list[Callable[..., Any]] = [
    hello,
    heartbeat_probe,
    fetch_message,
    delta_poll,
    renew_subscriptions,
    record_renewal_outcome,
    raise_alert,
    create_draft,
    send_draft,
    run_m1,
    run_a1,
    run_a2,
    start_investor_sequences,
    run_outreach_kickoff,
    prepare_step,
    decide_and_send,
    send_approved,
    check_cancel_conditions,
    send_window,
    update_sequence,
    classify_inbound,
    extract_and_upsert_buy_box,
    draft_and_qa_reply,
    apply_human_decision,
    notify_bounce,
    compute_and_send_digest,
    sync_projection,
    reconcile_sources,
    send_parser_email,
    load_contract,
    run_blast,
    send_approved_blast,
    record_buyer_response,
    raise_deadline_alert,
    record_workflow_state,
    deal_dead_followup,
    seller_list_refresh,
    seller_prepare_step,
    seller_decide_and_send,
    seller_send_approved,
    seller_classify_inbound,
    seller_intake,
    seller_apply_human_decision,
    seller_notify_bounce,
    compute_offer,
    qa_offer_draft,
    pre_send_check,
    decide_and_send_offer,
    send_approved_offer,
    withdraw_offer,
    handoff_to_s5,
    load_negotiation,
    evaluate_counter,
    accept_offer,
    decline_offer,
    expire_offer,
    send_negotiation_message,
    requeue_seller,
    run_d1,
    run_d2,
    compute_learning_stats,
]


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
