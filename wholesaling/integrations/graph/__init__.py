"""Microsoft Graph (Outlook mail) integration: Protocol, models, mock, live client, factory.

Selected by ``GRAPH_MODE`` (``mock`` by default). See docs/integrations/graph.md.
"""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.graph.interface import (
    DEFAULT_FOLDER,
    MAX_SUBSCRIPTION_LIFETIME,
    SUBSCRIPTION_LIFETIME,
    GraphAuthError,
    GraphCircuitOpen,
    GraphError,
    GraphGateway,
    GraphNotFound,
    GraphSendForbidden,
    GraphThrottled,
    RecordedCall,
    subscription_expiry,
)
from wholesaling.integrations.graph.mock import MockGraphGateway
from wholesaling.integrations.graph.models import (
    BounceInfo,
    ChangeNotification,
    DeltaPage,
    DraftInfo,
    InboundMessage,
    NotificationBatch,
    OutboundDraft,
    SendResult,
    Subscription,
    detect_bounce,
)


def get_graph(settings: Settings) -> GraphGateway:
    """Return the gateway implementation selected by ``GRAPH_MODE``."""
    if settings.graph_mode == "mock":
        return MockGraphGateway(mailbox=settings.graph_mailbox or "deals@example.test")
    # Imported lazily so mock-mode processes never import the live module (RUL-02 pattern).
    from wholesaling.integrations.graph.live import LiveGraphGateway

    return LiveGraphGateway(settings)


__all__ = [
    "DEFAULT_FOLDER",
    "MAX_SUBSCRIPTION_LIFETIME",
    "SUBSCRIPTION_LIFETIME",
    "BounceInfo",
    "ChangeNotification",
    "DeltaPage",
    "DraftInfo",
    "GraphAuthError",
    "GraphCircuitOpen",
    "GraphError",
    "GraphGateway",
    "GraphNotFound",
    "GraphSendForbidden",
    "GraphThrottled",
    "InboundMessage",
    "MockGraphGateway",
    "NotificationBatch",
    "OutboundDraft",
    "RecordedCall",
    "SendResult",
    "Subscription",
    "detect_bounce",
    "get_graph",
    "subscription_expiry",
]
