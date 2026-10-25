"""In-memory Graph gateway. Selected when ``GRAPH_MODE=mock`` (the default).

Behaviour the tests and the synthetic pilot (BO-09) rely on:

- ``inject_inbound(...)`` seeds an inbound message exactly as a webhook or delta poll would see
  it and returns the model;
- every call is recorded in ``calls``;
- drafts and sends are tracked separately (INT-13): ``send_draft`` on an unknown or already
  sent draft raises ``GraphNotFound`` like Graph does;
- ``create_reply_draft`` keeps the original ``conversation_id`` (INV-23) and sets
  ``In-Reply-To``;
- subscriptions expire against an injectable clock (``now``);
- ``fail_next(method, exc)`` injects one failure for the next call of ``method``.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.graph.interface import (
    DEFAULT_FOLDER,
    GraphNotFound,
    RecordedCall,
)
from wholesaling.integrations.graph.models import (
    BounceInfo,
    DeltaPage,
    DraftInfo,
    InboundMessage,
    OutboundDraft,
    SendResult,
    Subscription,
    detect_bounce,
)

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class MockGraphGateway:
    name = "graph"
    mode: IntegrationMode = "mock"

    def __init__(self, mailbox: str = "deals@example.test", *, clock: Clock | None = None) -> None:
        self.mailbox = mailbox
        self._clock: Clock = clock or _utcnow
        self.calls: list[RecordedCall] = []
        # graph_id -> message, in arrival order (dict preserves insertion order).
        self.messages: dict[str, InboundMessage] = {}
        self.folders: dict[str, str] = {}  # graph_id -> folder
        self.subscriptions: dict[str, Subscription] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[SendResult] = []
        self.healthy = True
        self._failures: dict[str, deque[Exception]] = {}
        self._seq = 0

    # -- test helpers ------------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def set_clock(self, clock: Clock) -> None:
        self._clock = clock

    def fail_next(self, method: str, exc: Exception) -> None:
        self._failures.setdefault(method, deque()).append(exc)

    def reset(self) -> None:
        self.calls.clear()
        self.messages.clear()
        self.folders.clear()
        self.subscriptions.clear()
        self.drafts.clear()
        self.sent.clear()
        self._failures.clear()
        self.healthy = True
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}-{uuid.uuid4().hex[:8]}"

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append(RecordedCall(method=method, args=args))
        pending = self._failures.get(method)
        if pending:
            raise pending.popleft()

    def inject_inbound(
        self,
        *,
        from_address: str,
        subject: str = "",
        body: str = "",
        to: list[str] | None = None,
        conversation_id: str | None = None,
        internet_message_id: str | None = None,
        graph_id: str | None = None,
        received_at: datetime | None = None,
        in_reply_to: str | None = None,
        headers: dict[str, str] | None = None,
        folder: str = DEFAULT_FOLDER,
        from_name: str | None = None,
        bounce: BounceInfo | None = None,
    ) -> InboundMessage:
        """Seed an inbound message. Returns the model so a test can hand its ``graph_id`` to the
        webhook or to ``fetch_message``. Bounce detection runs exactly as in the live path
        unless ``bounce`` is given explicitly."""
        gid = graph_id or self._next_id("AAMk")
        ext = internet_message_id or f"<{uuid.uuid4().hex}@{from_address.split('@')[-1]}>"
        conv = conversation_id or self._next_id("AAQk")
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        hdrs.setdefault("message-id", ext)
        if in_reply_to:
            hdrs.setdefault("in-reply-to", in_reply_to)
        info = bounce or detect_bounce(
            from_address=from_address,
            subject=subject,
            headers=hdrs,
            body_text=body,
            own_mailbox=self.mailbox,
        )
        msg = InboundMessage(
            graph_id=gid,
            external_message_id=ext,
            conversation_id=conv,
            from_address=from_address,
            from_name=from_name,
            to=to or [self.mailbox],
            subject=subject,
            body_text=body,
            received_at=received_at or self.now(),
            in_reply_to=in_reply_to,
            headers=hdrs,
            bounce=info,
            raw={"id": gid, "internetMessageId": ext, "conversationId": conv, "mock": True},
        )
        self.messages[gid] = msg
        self.folders[gid] = folder
        return msg

    def expire_subscriptions(self, now: datetime | None = None) -> list[str]:
        """Drop subscriptions whose expiry has passed (Graph deletes them silently)."""
        now = now or self.now()
        gone = [sid for sid, s in self.subscriptions.items() if s.expires_at <= now]
        for sid in gone:
            del self.subscriptions[sid]
        return gone

    # -- GraphGateway ------------------------------------------------------------------------

    async def healthcheck(self) -> HealthStatus:
        self._record("healthcheck")
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=self.healthy,
            detail="mock gateway" if self.healthy else "mock gateway forced unhealthy",
        )

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
        self._record(
            "create_subscription",
            resource=resource,
            notification_url=notification_url,
            expires_at=expires_at,
            change_types=change_types,
            lifecycle_notification_url=lifecycle_notification_url,
        )
        sub = Subscription(
            id=str(uuid.uuid4()),
            resource=resource,
            expires_at=expires_at,
            change_types=list(change_types or ["created"]),
            notification_url=notification_url,
            raw={"clientState": client_state, "mock": True},
        )
        self.subscriptions[sub.id] = sub
        return sub

    async def renew_subscription(self, subscription_id: str, expires_at: datetime) -> Subscription:
        self._record("renew_subscription", subscription_id=subscription_id, expires_at=expires_at)
        self.expire_subscriptions()
        current = self.subscriptions.get(subscription_id)
        if current is None:
            raise GraphNotFound(f"subscription {subscription_id} not found", status_code=404)
        renewed = current.model_copy(update={"expires_at": expires_at})
        self.subscriptions[subscription_id] = renewed
        return renewed

    async def list_subscriptions(self) -> list[Subscription]:
        self._record("list_subscriptions")
        self.expire_subscriptions()
        return list(self.subscriptions.values())

    async def delete_subscription(self, subscription_id: str) -> None:
        self._record("delete_subscription", subscription_id=subscription_id)
        if subscription_id not in self.subscriptions:
            raise GraphNotFound(f"subscription {subscription_id} not found", status_code=404)
        del self.subscriptions[subscription_id]

    async def get_message(self, graph_id: str) -> InboundMessage:
        self._record("get_message", graph_id=graph_id)
        msg = self.messages.get(graph_id)
        if msg is None:
            raise GraphNotFound(f"message {graph_id} not found", status_code=404)
        return msg

    async def delta_messages(
        self,
        folder: str = DEFAULT_FOLDER,
        delta_token: str | None = None,
        *,
        since: datetime | None = None,
    ) -> DeltaPage:
        self._record("delta_messages", folder=folder, delta_token=delta_token, since=since)
        ordered = [m for gid, m in self.messages.items() if self.folders.get(gid) == folder]
        start = 0
        if delta_token is not None:
            if not delta_token.startswith(f"mock-delta:{folder}:"):
                raise GraphNotFound("delta token is not valid for this folder", status_code=410)
            start = int(delta_token.rsplit(":", 1)[-1])
        selected = ordered[start:]
        if delta_token is None and since is not None:
            selected = [m for m in selected if m.received_at is not None and m.received_at >= since]
        return DeltaPage(
            messages=selected,
            next_delta_token=f"mock-delta:{folder}:{len(ordered)}",
            pages_fetched=1,
        )

    async def create_draft(self, draft: OutboundDraft) -> DraftInfo:
        self._record("create_draft", draft=draft.model_dump())
        if draft.reply_to_graph_message_id is not None:
            return await self._reply_draft(
                draft.reply_to_graph_message_id, draft.body_text, record=False
            )
        conv = draft.conversation_id
        if conv is not None:
            latest = self._latest_in_conversation(conv)
            if latest is not None:
                return await self._reply_draft(latest.graph_id, draft.body_text, record=False)
        return self._store_draft(
            to=list(draft.to),
            subject=draft.subject,
            body=draft.body_text,
            conversation_id=conv or self._next_id("AAQk"),
            in_reply_to=None,
        )

    async def create_reply_draft(self, graph_message_id: str, body: str) -> DraftInfo:
        return await self._reply_draft(graph_message_id, body, record=True)

    async def send_draft(self, draft_id: str) -> SendResult:
        self._record("send_draft", draft_id=draft_id)
        draft = self.drafts.get(draft_id)
        if draft is None or draft["sent"]:
            raise GraphNotFound(f"draft {draft_id} not found", status_code=404)
        draft["sent"] = True
        now = self.now()
        result = SendResult(
            draft_id=draft_id,
            sent=True,
            sent_at=now,
            graph_message_id=draft_id,
            internet_message_id=draft["internet_message_id"],
            conversation_id=draft["conversation_id"],
            raw={"mock": True},
        )
        self.sent.append(result)
        return result

    # -- internals ---------------------------------------------------------------------------

    def _latest_in_conversation(self, conversation_id: str) -> InboundMessage | None:
        candidates = [m for m in self.messages.values() if m.conversation_id == conversation_id]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.received_at or datetime.min.replace(tzinfo=UTC))

    async def _reply_draft(self, graph_message_id: str, body: str, *, record: bool) -> DraftInfo:
        if record:
            self._record("create_reply_draft", graph_message_id=graph_message_id)
        original = self.messages.get(graph_message_id)
        if original is None:
            raise GraphNotFound(f"message {graph_message_id} not found", status_code=404)
        subject = original.subject or ""
        if not subject.lower().startswith("re:"):
            subject = f"RE: {subject}".strip()
        return self._store_draft(
            to=[original.from_address] if original.from_address else [],
            subject=subject,
            body=body,
            conversation_id=original.conversation_id,
            in_reply_to=original.external_message_id,
        )

    def _store_draft(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        conversation_id: str | None,
        in_reply_to: str | None,
    ) -> DraftInfo:
        draft_id = self._next_id("DRAFT")
        internet_id = f"<{uuid.uuid4().hex}@{self.mailbox.split('@')[-1]}>"
        self.drafts[draft_id] = {
            "to": to,
            "subject": subject,
            "body": body,
            "conversation_id": conversation_id,
            "in_reply_to": in_reply_to,
            "internet_message_id": internet_id,
            "sent": False,
            "created_at": self.now(),
        }
        return DraftInfo(
            draft_id=draft_id,
            internet_message_id=internet_id,
            conversation_id=conversation_id,
            raw={"mock": True, "inReplyTo": in_reply_to},
        )
