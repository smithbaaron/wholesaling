"""What the application needs from Microsoft Graph mail, kept deliberately narrow.

Everything outside ``integrations.graph`` talks to ``GraphGateway`` so it runs unchanged
against the mock (``GRAPH_MODE=mock``, the default) and the live client. Draft creation and
sending are separate operations by design (REQUIREMENTS.md INT-13): there is no combined
"send a new message" call anywhere on this surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.graph.models import (
    DeltaPage,
    DraftInfo,
    InboundMessage,
    OutboundDraft,
    SendResult,
    Subscription,
)

DEFAULT_FOLDER = "inbox"

# Graph caps Outlook ``message`` subscriptions at 10,080 minutes (7 days):
# https://learn.microsoft.com/en-us/graph/change-notifications-overview  (Subscription lifetime)
MAX_SUBSCRIPTION_LIFETIME = timedelta(minutes=10_080)
# What renewal asks for: comfortably over the 48 h renewal cadence (WF-11) so one missed run
# does not lapse the subscription, and well under the ceiling.
SUBSCRIPTION_LIFETIME = timedelta(hours=70)


def subscription_expiry(now: datetime, lifetime: timedelta = SUBSCRIPTION_LIFETIME) -> datetime:
    return now + min(lifetime, MAX_SUBSCRIPTION_LIFETIME)


class GraphError(Exception):
    """Base class for every error the gateway raises."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class GraphAuthError(GraphError):
    """Token acquisition failed or Graph rejected the token (401/403)."""


class GraphNotFound(GraphError):
    """The item or subscription does not exist (404)."""


class GraphThrottled(GraphError):
    """Graph returned 429 / 503 after the retry budget was spent."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class GraphCircuitOpen(GraphError):
    """The circuit breaker is open; no request was attempted (XC-08)."""


class GraphSendForbidden(GraphError):
    """A live send was attempted while live sending is disabled (RUL-06)."""


@dataclass(slots=True)
class RecordedCall:
    """One recorded method call on the mock (tests and the synthetic pilot assert on these)."""

    method: str
    args: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GraphGateway(Protocol):
    name: str
    mode: IntegrationMode

    async def healthcheck(self) -> HealthStatus: ...

    # -- subscriptions (INT-12, WF-11) -------------------------------------------------------

    async def create_subscription(
        self,
        *,
        resource: str,
        notification_url: str,
        client_state: str,
        expires_at: datetime,
        change_types: list[str] | None = None,
        lifecycle_notification_url: str | None = None,
    ) -> Subscription:
        """Create a change-notification subscription. Graph validates ``notification_url``
        synchronously (the handshake in ``api/routes/graph_webhook.py``)."""
        ...

    async def renew_subscription(self, subscription_id: str, expires_at: datetime) -> Subscription:
        """Extend an existing subscription. Raises ``GraphNotFound`` when it has lapsed."""
        ...

    async def list_subscriptions(self) -> list[Subscription]: ...

    async def delete_subscription(self, subscription_id: str) -> None: ...

    # -- reading (INT-12, WF-12) ---------------------------------------------------------------

    async def get_message(self, graph_id: str) -> InboundMessage:
        """Fetch one message with body and the header subset. Raises ``GraphNotFound``."""
        ...

    async def delta_messages(
        self,
        folder: str = DEFAULT_FOLDER,
        delta_token: str | None = None,
        *,
        since: datetime | None = None,
    ) -> DeltaPage:
        """One full delta round for ``folder``.

        ``delta_token=None`` starts a new round (optionally bounded to ``received >= since``);
        otherwise continue from the token returned last time. Follows ``@odata.nextLink`` until
        the ``@odata.deltaLink`` and returns that as ``next_delta_token``.
        """
        ...

    # -- writing (INT-13: drafts and sends are separate) --------------------------------------

    async def create_draft(self, draft: OutboundDraft) -> DraftInfo:
        """Create a draft in the mailbox. Nothing is sent."""
        ...

    async def create_reply_draft(self, graph_message_id: str, body: str) -> DraftInfo:
        """Create a reply draft to an existing message; Graph keeps its ``conversationId``."""
        ...

    async def send_draft(self, draft_id: str) -> SendResult:
        """Send a previously created draft. Raises ``GraphNotFound`` if the draft no longer
        exists (already sent) and ``GraphSendForbidden`` when live sending is disabled."""
        ...
