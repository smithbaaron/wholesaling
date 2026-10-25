"""Pydantic models for what the application needs from Microsoft Graph mail.

Everything here is provider-shaped but application-owned: the live client translates Graph
JSON into these models, the mock produces them directly, and nothing outside this package
touches raw Graph payloads. Each model keeps the raw payload in ``raw`` so nothing is lost.

REQUIREMENTS.md INT-12 (fetch message content and conversationId; detect replies and
delivery failures), INT-13 (draft and send are separate), XC-03 (idempotency on
``internetMessageId``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ChangeType = Literal["created", "updated", "deleted"]

# --------------------------------------------------------------------------------------------
# Bounce / NDR detection (INT-25)
# --------------------------------------------------------------------------------------------

# Senders that only ever deliver bounce reports. Matched against the local part, lower-cased.
_BOUNCE_SENDERS = ("postmaster", "mailer-daemon", "mail-daemon", "microsoftexchange")
# Subject prefixes Exchange / Gmail / common MTAs use for non-delivery reports.
_BOUNCE_SUBJECT_RE = re.compile(
    r"^\s*(undeliverable|undelivered mail returned to sender|delivery status notification"
    r"|delivery (has )?failed|mail delivery failed|failure notice|returned mail"
    r"|delivery failure|message not delivered)",
    re.IGNORECASE,
)
# Header markers; keys are compared case-insensitively.
_DELIVERY_STATUS_RE = re.compile(r"report-type\s*=\s*\"?delivery-status", re.IGNORECASE)
# Where an NDR names the failed recipient: the DSN header first, then common body phrasings.
_FAILED_RECIPIENT_HEADER = "x-failed-recipients"
_BODY_RECIPIENT_RE = re.compile(
    r"(?:following recipient|recipient|delivery to|wasn't found|couldn't be delivered"
    r"|could not be delivered|rejected)[^\n<]{0,80}?[<:\s]"
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
_ANY_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class BounceInfo(BaseModel):
    """Result of NDR detection on an inbound message."""

    model_config = ConfigDict(frozen=True)

    is_bounce: bool = False
    bounced_address: str | None = None
    reason: str | None = None  # which signal fired: "header" | "sender" | "subject"


def detect_bounce(
    *,
    from_address: str | None,
    subject: str | None,
    headers: dict[str, str],
    body_text: str,
    own_mailbox: str | None = None,
) -> BounceInfo:
    """Heuristic NDR detection. Conservative: a signal must come from the sender, the DSN
    content-type, or a well-known subject. Plain replies that merely mention "undeliverable"
    in the body are not bounces.
    """
    lower_headers = {k.lower(): v for k, v in headers.items()}
    reason: str | None = None

    content_type = lower_headers.get("content-type", "")
    if _DELIVERY_STATUS_RE.search(content_type):
        reason = "header"
    elif from_address and from_address.split("@", 1)[0].lower().startswith(_BOUNCE_SENDERS):
        reason = "sender"
    elif subject and _BOUNCE_SUBJECT_RE.match(subject):
        reason = "subject"
    if reason is None:
        return BounceInfo()

    bounced: str | None = None
    failed = lower_headers.get(_FAILED_RECIPIENT_HEADER)
    if failed:
        bounced = failed.split(",")[0].strip()
    if bounced is None:
        m = _BODY_RECIPIENT_RE.search(body_text)
        if m:
            bounced = m.group(1)
    if bounced is None:
        own = (own_mailbox or "").lower()
        sender = (from_address or "").lower()
        for candidate in _ANY_EMAIL_RE.findall(body_text):
            c = candidate.lower()
            if c != own and c != sender and not c.split("@", 1)[0].startswith(_BOUNCE_SENDERS):
                bounced = candidate
                break
    return BounceInfo(is_bounce=True, bounced_address=bounced, reason=reason)


# --------------------------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------------------------

# Headers worth keeping on the message row. Anything else in ``internetMessageHeaders`` is
# dropped to keep rows small (CMP-05 data minimisation).
HEADER_SUBSET: frozenset[str] = frozenset(
    {
        "message-id",
        "in-reply-to",
        "references",
        "content-type",
        "auto-submitted",
        "x-failed-recipients",
        "x-auto-response-suppress",
        "list-unsubscribe",
        "return-path",
        "authentication-results",
    }
)


class InboundMessage(BaseModel):
    """One mail item as the application sees it.

    ``external_message_id`` is Graph's ``internetMessageId`` (the RFC 5322 Message-ID) and is
    the idempotency key for the ``messages`` table (XC-03, INV-24). ``graph_id`` is the
    mutable Graph item id used for API calls. ``conversation_id`` is the Outlook conversation
    (``threads.external_thread_id``, INV-23).
    """

    model_config = ConfigDict(frozen=True)

    graph_id: str
    external_message_id: str
    conversation_id: str | None = None
    from_address: str | None = None
    from_name: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str | None = None
    body_text: str = ""
    body_html: str | None = None
    received_at: datetime | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    is_draft: bool = False
    is_read: bool = False
    bounce: BounceInfo = Field(default_factory=BounceInfo)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_bounce(self) -> bool:
        return self.bounce.is_bounce

    @property
    def is_reply(self) -> bool:
        return self.in_reply_to is not None or bool(self.references)

    @classmethod
    def from_graph(
        cls, payload: dict[str, Any], *, own_mailbox: str | None = None
    ) -> InboundMessage:
        """Translate a Graph ``message`` resource (v1.0 JSON) into the application model."""
        sender = payload.get("from") or payload.get("sender") or {}
        addr = (sender.get("emailAddress") or {}) if isinstance(sender, dict) else {}
        headers: dict[str, str] = {}
        for h in payload.get("internetMessageHeaders") or []:
            name = str(h.get("name", "")).lower()
            if name in HEADER_SUBSET:
                headers[name] = str(h.get("value", ""))
        body = payload.get("body") or {}
        content = str(body.get("content") or "")
        body_html: str | None = None
        body_text = content
        if str(body.get("contentType", "")).lower() == "html":
            body_html = content
            body_text = str(payload.get("bodyPreview") or _strip_html(content))
        received = _parse_dt(payload.get("receivedDateTime"))
        references = [r for r in headers.get("references", "").split() if r]
        internet_id = payload.get("internetMessageId") or headers.get("message-id")
        from_address = addr.get("address") or None
        subject = payload.get("subject")
        bounce = detect_bounce(
            from_address=from_address,
            subject=subject,
            headers=headers,
            body_text=body_text,
            own_mailbox=own_mailbox,
        )
        return cls(
            graph_id=str(payload["id"]),
            # Drafts can lack an internetMessageId; fall back to the Graph id so the row
            # still has a unique key.
            external_message_id=str(internet_id or f"graph:{payload['id']}"),
            conversation_id=payload.get("conversationId"),
            from_address=from_address,
            from_name=addr.get("name") or None,
            to=_addresses(payload.get("toRecipients")),
            cc=_addresses(payload.get("ccRecipients")),
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            received_at=received,
            in_reply_to=headers.get("in-reply-to") or None,
            references=references,
            headers=headers,
            is_draft=bool(payload.get("isDraft", False)),
            is_read=bool(payload.get("isRead", False)),
            bounce=bounce,
            raw=dict(payload),
        )


def _addresses(recipients: Any) -> list[str]:
    out: list[str] = []
    for r in recipients or []:
        email = (r.get("emailAddress") or {}).get("address") if isinstance(r, dict) else None
        if email:
            out.append(str(email))
    return out


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    text = _TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class OutboundDraft(BaseModel):
    """What the application wants to put in a Graph draft (never sends; INT-13).

    Exactly one of three threading modes applies:

    - ``reply_to_graph_message_id`` set: created with ``createReply`` on that item, so Graph
      keeps the ``conversationId`` and threading headers (INV-10, INV-23).
    - ``conversation_id`` set: the gateway replies to the latest message in that conversation.
    - neither: a brand-new message that starts a conversation.
    """

    model_config = ConfigDict(frozen=True)

    to: list[str] = Field(min_length=1)
    subject: str
    body_text: str
    body_html: str | None = None
    cc: list[str] = Field(default_factory=list)
    reply_to_graph_message_id: str | None = None
    conversation_id: str | None = None


class SendResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    sent: bool
    sent_at: datetime | None = None
    graph_message_id: str | None = None
    internet_message_id: str | None = None
    conversation_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class DraftInfo(BaseModel):
    """What the gateway knows about a draft after creating it."""

    model_config = ConfigDict(frozen=True)

    draft_id: str
    internet_message_id: str | None = None
    conversation_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------
# Subscriptions, delta, notifications
# --------------------------------------------------------------------------------------------


class Subscription(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    resource: str
    expires_at: datetime
    change_types: list[str] = Field(default_factory=lambda: ["created"])
    notification_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_graph(cls, payload: dict[str, Any]) -> Subscription:
        expires = _parse_dt(payload.get("expirationDateTime")) or datetime.now(tz=UTC)
        return cls(
            id=str(payload["id"]),
            resource=str(payload.get("resource", "")),
            expires_at=expires,
            change_types=[c.strip() for c in str(payload.get("changeType", "created")).split(",")],
            notification_url=payload.get("notificationUrl"),
            raw=dict(payload),
        )

    def expires_within(self, now: datetime, window_seconds: float) -> bool:
        return (self.expires_at - now).total_seconds() <= window_seconds


class DeltaPage(BaseModel):
    """Result of one delta round: the changed messages plus the token for the next round.

    ``removed_ids`` lists Graph ids reported with ``@removed`` (deleted in the folder).
    """

    model_config = ConfigDict(frozen=True)

    messages: list[InboundMessage] = Field(default_factory=list)
    removed_ids: list[str] = Field(default_factory=list)
    next_delta_token: str | None = None
    pages_fetched: int = 1


class ChangeNotification(BaseModel):
    """One entry of a Graph change-notification POST body (``value[]``).

    Lifecycle notifications share the shape and carry ``lifecycle_event``.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    client_state: str | None = Field(default=None, alias="clientState")
    change_type: str | None = Field(default=None, alias="changeType")
    resource: str | None = None
    resource_data: dict[str, Any] = Field(default_factory=dict, alias="resourceData")
    subscription_expiration: datetime | None = Field(
        default=None, alias="subscriptionExpirationDateTime"
    )
    tenant_id: str | None = Field(default=None, alias="tenantId")
    lifecycle_event: str | None = Field(default=None, alias="lifecycleEvent")

    @property
    def resource_id(self) -> str | None:
        """The Graph item id, from ``resourceData.id`` or the tail of ``resource``."""
        rid = self.resource_data.get("id")
        if rid:
            return str(rid)
        if self.resource:
            tail = self.resource.rsplit("/", 1)[-1]
            # ``Users/{u}/Messages/{id}`` or ``.../Messages('{id}')``
            if tail.startswith("Messages('") and tail.endswith("')"):
                return tail[len("Messages('") : -2]
            return tail or None
        return None

    @property
    def is_lifecycle(self) -> bool:
        return self.lifecycle_event is not None

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.subscription_id, self.resource_id or "")


class NotificationBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: list[ChangeNotification] = Field(default_factory=list)
