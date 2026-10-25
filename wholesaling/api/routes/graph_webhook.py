"""``POST /webhooks/graph``: Microsoft Graph change-notification endpoint (REQUIREMENTS.md
INT-12, XC-03, WF-19, WF-23).

Three request shapes land here:

1. **Validation handshake** (on subscription create/renew): Graph POSTs with a
   ``validationToken`` query parameter and expects the URL-decoded token echoed back as
   ``text/plain`` with 200 within 10 s. FastAPI decodes the query string for us.
2. **Change notifications**: a JSON body ``{"value": [...]}``. Each entry's ``clientState``
   must equal ``GRAPH_WEBHOOK_CLIENT_STATE`` or the whole batch is rejected with 401. Valid
   entries are deduped by ``(subscriptionId, resourceData.id)`` and handed to Temporal by
   starting ``GraphInboundMessageWorkflow`` with a deterministic workflow id, so a repeated
   notification is a no-op at both layers. The message itself is never fetched here. We answer
   202 as soon as the starts are issued (Graph wants a 2xx within 3 s and retries otherwise).
3. **Lifecycle notifications** (``lifecycleEvent``: ``reauthorizationRequired``, ``missed``,
   ``subscriptionRemoved``): logged and answered by starting the renewal workflow.

Facts and limits are from Microsoft Learn; see docs/integrations/graph.md for citations.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from wholesaling.config.settings import Settings
from wholesaling.integrations.graph.models import ChangeNotification, NotificationBatch
from wholesaling.integrations.temporal.interface import TemporalGateway
from wholesaling.logging import get_logger
from wholesaling.workflows.graph_maintenance import (
    InboundMessageInput,
    SubscriptionRenewalInput,
    inbound_workflow_id,
)

log = get_logger(__name__)
router = APIRouter(tags=["webhooks"])

INBOUND_WORKFLOW = "GraphInboundMessageWorkflow"
RENEWAL_WORKFLOW = "GraphSubscriptionRenewalWorkflow"
RENEWAL_LIFECYCLE_EVENTS = frozenset({"reauthorizationRequired", "missed", "subscriptionRemoved"})


class NotificationDeduper:
    """Process-local LRU of notification keys already handed to Temporal.

    The durable guarantee is the deterministic workflow id (Temporal rejects a second start)
    and the ``(channel, external_message_id)`` unique key in the database; this cache only
    saves the round-trip for the common "Graph delivered it twice" case.
    """

    def __init__(self, capacity: int = 10_000) -> None:
        self.capacity = capacity
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def seen(self, key: tuple[str, str]) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        return False

    def add(self, key: tuple[str, str]) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)

    def __len__(self) -> int:
        return len(self._seen)


def get_deduper(request: Request) -> NotificationDeduper:
    deduper = getattr(request.app.state, "graph_dedupe", None)
    if deduper is None:
        deduper = NotificationDeduper()
        request.app.state.graph_dedupe = deduper
    assert isinstance(deduper, NotificationDeduper)
    return deduper


def _expected_client_state(settings: Settings) -> str | None:
    secret = settings.graph_webhook_client_state
    value = secret.get_secret_value() if secret else None
    return value or None


def _already_started(exc: Exception) -> bool:
    """True when the Temporal client reports the workflow id is already in use."""
    name = type(exc).__name__
    return name == "WorkflowAlreadyStartedError" or "already started" in str(exc).lower()


@router.post("/webhooks/graph", status_code=status.HTTP_202_ACCEPTED)
async def graph_webhook(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        # Handshake. Echo the decoded token verbatim as text/plain (an encoded token fails
        # validation). ``nosniff`` keeps a browser from ever treating the echo as HTML.
        log.info("graph.webhook.validation", token_length=len(validation_token))
        return PlainTextResponse(
            validation_token,
            status_code=200,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    try:
        payload: Any = await request.json()
        batch = NotificationBatch.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        log.warning("graph.webhook.bad_payload", error=str(exc)[:200])
        return JSONResponse({"detail": "invalid notification payload"}, status_code=400)

    expected = _expected_client_state(settings)
    if expected is None:
        # Fail closed: without a configured secret nothing can be verified as Graph's.
        log.error("graph.webhook.client_state_unconfigured")
        return JSONResponse({"detail": "webhook not configured"}, status_code=401)
    if any(n.client_state != expected for n in batch.value):
        log.warning("graph.webhook.client_state_mismatch", count=len(batch.value))
        return JSONResponse({"detail": "clientState mismatch"}, status_code=401)

    temporal: TemporalGateway = request.app.state.temporal
    deduper = get_deduper(request)
    accepted: list[str] = []
    duplicates: list[str] = []
    lifecycle: list[str] = []
    failed = 0

    for note in batch.value:
        if note.is_lifecycle:
            lifecycle.append(note.lifecycle_event or "")
            await _handle_lifecycle(temporal, note, settings)
            continue
        resource_id = note.resource_id
        if not resource_id:
            log.warning("graph.webhook.no_resource_id", subscription_id=note.subscription_id)
            continue
        key = note.dedupe_key
        if deduper.seen(key):
            duplicates.append(resource_id)
            continue
        workflow_id = inbound_workflow_id(note.subscription_id, resource_id)
        try:
            await temporal.start_workflow(
                INBOUND_WORKFLOW,
                [
                    InboundMessageInput(
                        graph_id=resource_id,
                        subscription_id=note.subscription_id,
                        change_type=note.change_type,
                    )
                ],
                workflow_id=workflow_id,
            )
        except Exception as exc:
            if _already_started(exc):
                deduper.add(key)
                duplicates.append(resource_id)
                continue
            failed += 1
            log.error("graph.webhook.enqueue_failed", workflow_id=workflow_id, error=str(exc))
            continue
        deduper.add(key)
        accepted.append(resource_id)

    log.info(
        "graph.webhook.received",
        accepted=len(accepted),
        duplicates=len(duplicates),
        lifecycle=lifecycle,
        failed=failed,
    )
    body = {
        "accepted": len(accepted),
        "duplicates": len(duplicates),
        "lifecycle": len(lifecycle),
        "failed": failed,
    }
    if failed and not accepted:
        # Nothing was queued: tell Graph to retry (it retries non-2xx for up to 4 hours).
        return JSONResponse(body, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse(body, status_code=status.HTTP_202_ACCEPTED)


async def _handle_lifecycle(
    temporal: TemporalGateway, note: ChangeNotification, settings: Settings
) -> None:
    event = note.lifecycle_event or ""
    log.warning(
        "graph.webhook.lifecycle",
        lifecycle_event=event,
        subscription_id=note.subscription_id,
        expires_at=note.subscription_expiration.isoformat()
        if note.subscription_expiration
        else None,
    )
    if event not in RENEWAL_LIFECYCLE_EVENTS:
        return
    # One renewal per subscription per event type at a time; a second identical lifecycle
    # notification while that run is in flight is a duplicate.
    workflow_id = f"graph-renewal-lifecycle-{note.subscription_id}-{event}"
    try:
        await temporal.start_workflow(
            RENEWAL_WORKFLOW,
            [SubscriptionRenewalInput(notification_url=settings.graph_webhook_url)],
            workflow_id=workflow_id,
        )
    except Exception as exc:
        if not _already_started(exc):
            log.error(
                "graph.webhook.lifecycle_enqueue_failed", workflow_id=workflow_id, error=str(exc)
            )


__all__ = ["NotificationDeduper", "get_deduper", "graph_webhook", "router"]
