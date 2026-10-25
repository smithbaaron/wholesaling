"""Graph activities against SQLite with the mock gateway.

INV-24: one inbound message = one ``messages`` row and one audit event, however many times it
arrives (webhook, delta, retry). INT-25: bounce -> suppression. INT-13 / RUL-05: draft and send
are separate and a retried send never sends twice. WF-11: renewal bookkeeping and alerting.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wholesaling.activities import graph as acts
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, Message, Suppression, Thread
from wholesaling.db.models.enums import Direction
from wholesaling.integrations.graph import GraphNotFound, MockGraphGateway

MAILBOX = "deals@northstack-deals.com"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gw(clock: FakeClock) -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=clock)


@pytest.fixture
def graph_settings(**_: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        graph_mode="mock",
        graph_mailbox=MAILBOX,
        graph_webhook_url="https://api.example/webhooks/graph",
        graph_webhook_client_state="state-secret",
    )


@pytest.fixture
async def ctx(
    gw: MockGraphGateway,
    session_factory: async_sessionmaker[AsyncSession],
    graph_settings: Settings,
    clock: FakeClock,
) -> AsyncIterator[acts.GraphActivityContext]:
    context = acts.GraphActivityContext(
        gateway=gw, session_factory=session_factory, settings=graph_settings, clock=clock
    )
    acts.set_graph_context(context)
    try:
        yield context
    finally:
        acts.set_graph_context(None)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


async def _count(session: AsyncSession, model: type[Any], **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int((await session.execute(stmt)).scalar_one())


# -- fetch_message -----------------------------------------------------------------------------


async def test_fetch_message_creates_thread_message_and_one_audit_event(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    inbound = gw.inject_inbound(
        from_address="jane@acme.com", subject="RE: Deals", body="Send them over."
    )
    result = await acts.fetch_message(acts.FetchMessageInput(graph_id=inbound.graph_id))
    assert result.found and result.created and not result.is_bounce
    assert result.external_message_id == inbound.external_message_id

    assert await _count(session, Message) == 1
    assert await _count(session, Thread) == 1
    assert await _count(session, AuditEvent, event_type="message.received") == 1
    row = (await session.execute(select(Message))).scalar_one()
    assert row.direction == Direction.IN
    assert row.external_message_id == inbound.external_message_id
    assert row.meta["graph_id"] == inbound.graph_id
    assert row.meta["source"] == "webhook"
    thread = await session.get(Thread, row.thread_id)
    assert thread is not None and thread.external_thread_id == inbound.conversation_id
    assert thread.counterpart_address == "jane@acme.com"


async def test_fetch_message_twice_is_idempotent(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    inbound = gw.inject_inbound(from_address="jane@acme.com", body="hi")
    first = await acts.fetch_message(acts.FetchMessageInput(graph_id=inbound.graph_id))
    second = await acts.fetch_message(acts.FetchMessageInput(graph_id=inbound.graph_id))
    assert first.created and not second.created
    assert first.message_id == second.message_id
    assert await _count(session, Message) == 1
    assert await _count(session, AuditEvent) == 1, "INV-24: exactly one logged event"


async def test_webhook_and_delta_paths_share_one_row(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    inbound = gw.inject_inbound(from_address="jane@acme.com", body="hi")
    await acts.fetch_message(acts.FetchMessageInput(graph_id=inbound.graph_id))
    poll = await acts.delta_poll(acts.DeltaPollInput())
    assert poll.fetched == 1 and poll.created == 0
    assert await _count(session, Message) == 1
    assert await _count(session, AuditEvent, event_type="message.received") == 1


async def test_fetch_message_gone_returns_not_found_without_rows(
    ctx: acts.GraphActivityContext, session: AsyncSession
) -> None:
    result = await acts.fetch_message(acts.FetchMessageInput(graph_id="deleted-already"))
    assert result.found is False and result.message_id is None
    assert await _count(session, Message) == 0


async def test_replies_in_same_conversation_share_a_thread(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    a = gw.inject_inbound(from_address="jane@acme.com", subject="Deals")
    b = gw.inject_inbound(
        from_address="jane@acme.com",
        subject="RE: Deals",
        conversation_id=a.conversation_id,
        in_reply_to=a.external_message_id,
        received_at=NOW + timedelta(minutes=5),
    )
    ra = await acts.fetch_message(acts.FetchMessageInput(graph_id=a.graph_id))
    rb = await acts.fetch_message(acts.FetchMessageInput(graph_id=b.graph_id))
    assert ra.thread_id == rb.thread_id and rb.is_reply
    assert await _count(session, Thread) == 1
    thread = (await session.execute(select(Thread))).scalar_one()
    assert _aware(thread.last_message_at) == NOW + timedelta(minutes=5)
    row_b = (
        await session.execute(
            select(Message).where(Message.external_message_id == b.external_message_id)
        )
    ).scalar_one()
    assert row_b.in_reply_to_external_id == a.external_message_id


async def test_own_sent_mail_seen_by_delta_is_recorded_outbound(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    gw.inject_inbound(from_address=MAILBOX, to=["jane@acme.com"], subject="Deals")
    await acts.delta_poll(acts.DeltaPollInput())
    row = (await session.execute(select(Message))).scalar_one()
    assert row.direction == Direction.OUT


# -- bounces (INT-25) ---------------------------------------------------------------------------


async def test_bounce_creates_suppression_once(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    ndr = gw.inject_inbound(
        from_address="postmaster@outlook.com",
        subject="Undeliverable: Off-market deals in Houston",
        body="Delivery has failed to these recipients: Bob.Dead+x@Gmail.com",
    )
    first = await acts.fetch_message(acts.FetchMessageInput(graph_id=ndr.graph_id))
    assert first.is_bounce and first.suppression_created
    assert first.bounced_address == "Bob.Dead+x@Gmail.com"
    second = await acts.fetch_message(acts.FetchMessageInput(graph_id=ndr.graph_id))
    assert second.is_bounce and not second.suppression_created

    rows = (await session.execute(select(Suppression))).scalars().all()
    assert len(rows) == 1
    assert rows[0].normalized_value == "bobdead@gmail.com", "normalized like the dedupe key"
    assert rows[0].reason == "bounce"
    assert str(rows[0].source_message_id) == first.message_id
    assert await _count(session, AuditEvent, event_type="suppression.added") == 1


async def test_second_bounce_for_same_address_does_not_duplicate_suppression(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    for i in range(2):
        ndr = gw.inject_inbound(
            from_address="postmaster@outlook.com",
            subject=f"Undeliverable: attempt {i}",
            body="Recipient: dead@nowhere.com",
        )
        await acts.fetch_message(acts.FetchMessageInput(graph_id=ndr.graph_id))
    assert await _count(session, Message) == 2
    assert await _count(session, Suppression) == 1


async def test_bounce_without_identifiable_address_adds_no_suppression(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    ndr = gw.inject_inbound(
        from_address="postmaster@outlook.com", subject="Undeliverable: x", body="no address here"
    )
    result = await acts.fetch_message(acts.FetchMessageInput(graph_id=ndr.graph_id))
    assert result.is_bounce and result.bounced_address is None and not result.suppression_created
    assert await _count(session, Suppression) == 0


# -- delta_poll (WF-12) --------------------------------------------------------------------------


async def test_delta_poll_without_token_uses_lookback_window(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    gw.inject_inbound(from_address="old@x.com", received_at=NOW - timedelta(days=2))
    gw.inject_inbound(from_address="new@x.com", received_at=NOW - timedelta(hours=3))
    result = await acts.delta_poll(acts.DeltaPollInput(lookback_hours=24))
    assert result.fetched == 1 and result.created == 1
    assert gw.calls[-1].args["since"] == NOW - timedelta(hours=24)
    assert result.next_delta_token is not None
    assert await _count(session, Message) == 1


async def test_delta_poll_with_token_continues_and_is_idempotent(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    gw.inject_inbound(from_address="a@x.com")
    r1 = await acts.delta_poll(acts.DeltaPollInput())
    gw.inject_inbound(from_address="b@x.com")
    r2 = await acts.delta_poll(acts.DeltaPollInput(delta_token=r1.next_delta_token))
    r3 = await acts.delta_poll(acts.DeltaPollInput(delta_token=r2.next_delta_token))
    assert (r1.created, r2.created, r3.created) == (1, 1, 0)
    assert await _count(session, Message) == 2
    assert await _count(session, AuditEvent, event_type="message.received") == 2


async def test_delta_poll_skips_drafts(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway
) -> None:
    msg = gw.inject_inbound(from_address="a@x.com")
    gw.messages[msg.graph_id] = msg.model_copy(update={"is_draft": True})
    result = await acts.delta_poll(acts.DeltaPollInput())
    assert result.fetched == 1 and result.created == 0


# -- renew_subscriptions (WF-11) -----------------------------------------------------------------


async def test_renew_creates_when_none_then_renews(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, clock: FakeClock
) -> None:
    first = await acts.renew_subscriptions(acts.RenewSubscriptionsInput())
    assert len(first.created) == 1 and first.renewed == []
    sub = gw.subscriptions[first.created[0]]
    assert sub.resource == f"users/{MAILBOX}/mailFolders('inbox')/messages"
    assert sub.notification_url == "https://api.example/webhooks/graph"
    assert sub.raw["clientState"] == "state-secret"
    assert sub.expires_at == NOW + timedelta(hours=70)
    create_call = next(c for c in gw.calls if c.method == "create_subscription")
    assert create_call.args["lifecycle_notification_url"] == "https://api.example/webhooks/graph"

    clock.now = NOW + timedelta(hours=48)
    second = await acts.renew_subscriptions(acts.RenewSubscriptionsInput())
    assert second.renewed == [sub.id] and second.created == []
    assert gw.subscriptions[sub.id].expires_at == NOW + timedelta(hours=48 + 70)


async def test_renew_replaces_stale_subscription_pointing_elsewhere(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway
) -> None:
    stale = await gw.create_subscription(
        resource=f"users/{MAILBOX}/mailFolders('inbox')/messages",
        notification_url="https://old-host/webhooks/graph",
        client_state="x",
        expires_at=NOW + timedelta(hours=1),
    )
    result = await acts.renew_subscriptions(acts.RenewSubscriptionsInput())
    assert result.removed_stale == [stale.id]
    assert len(result.created) == 1
    assert stale.id not in gw.subscriptions


async def test_renew_lapsed_subscription_creates_fresh_one(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway
) -> None:
    first = await acts.renew_subscriptions(acts.RenewSubscriptionsInput())
    gw.fail_next("renew_subscription", GraphNotFound("gone", status_code=404))
    result = await acts.renew_subscriptions(acts.RenewSubscriptionsInput())
    assert result.renewed == [] and len(result.created) == 1
    assert result.created != first.created


async def test_renew_requires_webhook_url_and_client_state(
    ctx: acts.GraphActivityContext,
    session_factory: async_sessionmaker[AsyncSession],
    gw: MockGraphGateway,
) -> None:
    acts.set_graph_context(
        acts.GraphActivityContext(
            gateway=gw,
            session_factory=session_factory,
            settings=Settings(_env_file=None, app_env="test", graph_mailbox=MAILBOX),  # type: ignore[call-arg]
        )
    )
    with pytest.raises(ValueError, match="GRAPH_WEBHOOK_URL"):
        await acts.renew_subscriptions(acts.RenewSubscriptionsInput())
    with pytest.raises(ValueError, match="GRAPH_WEBHOOK_CLIENT_STATE"):
        await acts.renew_subscriptions(
            acts.RenewSubscriptionsInput(notification_url="https://api/webhooks/graph")
        )


async def test_renew_propagates_gateway_failure(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway
) -> None:
    gw.fail_next("list_subscriptions", RuntimeError("graph down"))
    with pytest.raises(RuntimeError):
        await acts.renew_subscriptions(acts.RenewSubscriptionsInput())


async def test_renewal_outcome_streak_and_alert(
    ctx: acts.GraphActivityContext, session: AsyncSession
) -> None:
    r = await acts.record_renewal_outcome(acts.RecordRenewalOutcomeInput(ok=True))
    assert r.consecutive_failures == 0
    r = await acts.record_renewal_outcome(acts.RecordRenewalOutcomeInput(ok=False, detail="x"))
    assert r.consecutive_failures == 1
    r = await acts.record_renewal_outcome(acts.RecordRenewalOutcomeInput(ok=False, detail="y"))
    assert r.consecutive_failures == 2
    r = await acts.record_renewal_outcome(acts.RecordRenewalOutcomeInput(ok=True))
    assert r.consecutive_failures == 0
    assert await _count(session, AuditEvent, event_type=acts.RENEWAL_EVENT) == 4

    created = await acts.raise_alert(
        acts.RaiseAlertInput(
            kind="graph.subscription_renewal_failed", detail="twice", idempotency_key="k1"
        )
    )
    again = await acts.raise_alert(
        acts.RaiseAlertInput(
            kind="graph.subscription_renewal_failed", detail="twice", idempotency_key="k1"
        )
    )
    assert created is True and again is False
    assert await _count(session, AuditEvent, event_type=acts.ALERT_EVENT) == 1


# -- drafts and sends (INT-13, RUL-05) -----------------------------------------------------------


def _draft_input(key: str = "seq:abc:step:1", **over: Any) -> acts.CreateDraftInput:
    base: dict[str, Any] = {
        "idempotency_key": key,
        "to": ["jane@acme.com"],
        "subject": "Off-market deals in Houston",
        "body_text": "Hi Jane, ...",
    }
    base.update(over)
    return acts.CreateDraftInput(**base)


async def test_create_draft_writes_out_row_and_does_not_send(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    result = await acts.create_draft(_draft_input())
    assert result.created and result.draft_id in gw.drafts and not result.already_sent
    assert gw.sent == []
    row = (await session.execute(select(Message))).scalar_one()
    assert row.direction == Direction.OUT and row.draft is True and row.sent_at is None
    assert row.external_message_id == "seq:abc:step:1"
    assert row.from_address == MAILBOX and row.to_addresses == ["jane@acme.com"]
    assert row.meta["graph_draft_id"] == result.draft_id
    assert await _count(session, AuditEvent, event_type="message.drafted") == 1
    thread = await session.get(Thread, row.thread_id)
    assert thread is not None and thread.external_thread_id is not None


async def test_create_draft_retry_returns_same_row_without_second_draft(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    first = await acts.create_draft(_draft_input())
    second = await acts.create_draft(_draft_input())
    assert second.message_id == first.message_id and not second.created
    assert second.draft_id == first.draft_id
    assert len(gw.drafts) == 1
    assert await _count(session, Message) == 1


async def test_create_reply_draft_stays_in_conversation_and_thread(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    inbound = gw.inject_inbound(from_address="jane@acme.com", subject="Deals?", body="Any?")
    fetched = await acts.fetch_message(acts.FetchMessageInput(graph_id=inbound.graph_id))
    result = await acts.create_draft(
        _draft_input(
            key="reply:1",
            reply_to_graph_message_id=inbound.graph_id,
            thread_id=fetched.thread_id,
        )
    )
    assert result.thread_id == fetched.thread_id
    assert gw.drafts[result.draft_id]["conversation_id"] == inbound.conversation_id
    assert await _count(session, Thread) == 1, "INV-23: same Outlook conversation, same thread"


async def test_send_draft_marks_sent_and_is_idempotent(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession, clock: FakeClock
) -> None:
    created = await acts.create_draft(_draft_input())
    sent = await acts.send_draft(acts.SendDraftInput(message_id=created.message_id))
    assert sent.sent and not sent.already_sent and sent.sent_at == NOW.isoformat()
    again = await acts.send_draft(acts.SendDraftInput(message_id=created.message_id))
    assert not again.sent and again.already_sent
    assert len(gw.sent) == 1, "RUL-05: a retried send activity never sends twice"
    assert [c.method for c in gw.calls].count("send_draft") == 1

    row = await session.get(Message, __import__("uuid").UUID(created.message_id))
    assert row is not None and row.draft is False and row.sent_at is not None
    assert row.meta["internet_message_id"]
    assert await _count(session, AuditEvent, event_type="message.sent") == 1
    thread = await session.get(Thread, row.thread_id)
    assert thread is not None and _aware(thread.last_message_at) == NOW


async def test_send_draft_treats_vanished_draft_as_already_sent(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway, session: AsyncSession
) -> None:
    created = await acts.create_draft(_draft_input())
    # Simulate: previous attempt sent it (Graph has no draft any more) but crashed before commit.
    await gw.send_draft(created.draft_id or "")
    result = await acts.send_draft(acts.SendDraftInput(message_id=created.message_id))
    assert result.sent is True
    assert len(gw.sent) == 1
    row = await session.get(Message, __import__("uuid").UUID(created.message_id))
    assert row is not None and row.sent_at is not None


async def test_send_draft_rejects_inbound_or_unknown_rows(
    ctx: acts.GraphActivityContext, gw: MockGraphGateway
) -> None:
    inbound = gw.inject_inbound(from_address="jane@acme.com")
    fetched = await acts.fetch_message(acts.FetchMessageInput(graph_id=inbound.graph_id))
    with pytest.raises(ValueError, match="not an outbound draft"):
        await acts.send_draft(acts.SendDraftInput(message_id=fetched.message_id or ""))
    with pytest.raises(ValueError, match="not found"):
        await acts.send_draft(
            acts.SendDraftInput(message_id="00000000-0000-0000-0000-000000000000")
        )


def test_context_is_lazily_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    acts.set_graph_context(None)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("GRAPH_MODE", "mock")
    from wholesaling.config.settings import get_settings

    get_settings.cache_clear()
    try:
        ctx = acts.get_graph_context()
        assert isinstance(ctx.gateway, MockGraphGateway)
        assert acts.get_graph_context() is ctx
    finally:
        acts.set_graph_context(None)
        get_settings.cache_clear()
