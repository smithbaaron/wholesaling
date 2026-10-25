"""SequenceRepository and MessageRepository (REQUIREMENTS.md INV-10, INV-12 caps, INV-23,
WF-21, XC-03). SQLite by default; the same suite runs on Postgres in CI."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import MakeInvestor, MakeMessage
from wholesaling.db.models.enums import (
    Channel,
    Direction,
    MessageClassification,
    PolicyDecision,
    QaVerdict,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.messaging import Message, Suppression, Thread
from wholesaling.db.models.ops import QaReview
from wholesaling.db.repositories.messages import MessageRepository, local_day_start_utc
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.policy.actions import PolicyAction

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)  # Tuesday 10:00 Chicago


# -- sequences -------------------------------------------------------------------------------------


async def test_create_is_idempotent_on_workflow_id(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = SequenceRepository(session)
    seq, created = await repo.create(
        kind=SequenceKind.INVESTOR, investor_id=inv.id, temporal_workflow_id="investor-sequence:x"
    )
    assert created and seq.status is SequenceStatus.ACTIVE and seq.step == 0
    assert seq.total_steps == 4
    again, created2 = await repo.create(
        kind=SequenceKind.INVESTOR, investor_id=inv.id, temporal_workflow_id="investor-sequence:x"
    )
    assert not created2 and again.id == seq.id
    assert await repo.get_by_workflow_id("investor-sequence:x") is not None


async def test_create_enforces_target_matches_kind(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = SequenceRepository(session)
    with pytest.raises(ValueError, match="investor_id"):
        await repo.create(kind=SequenceKind.INVESTOR, temporal_workflow_id="a")
    with pytest.raises(ValueError, match="seller_lead_id"):
        await repo.create(kind=SequenceKind.SELLER, investor_id=inv.id, temporal_workflow_id="b")


async def test_get_active_for_investor_ignores_finished(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = SequenceRepository(session)
    assert await repo.get_active_for_investor(inv.id) is None
    old, _ = await repo.create(
        kind=SequenceKind.INVESTOR, investor_id=inv.id, temporal_workflow_id="old"
    )
    await repo.cancel(old.id, "reply")
    assert await repo.get_active_for_investor(inv.id) is None
    cur, _ = await repo.create(
        kind=SequenceKind.INVESTOR, investor_id=inv.id, temporal_workflow_id="cur"
    )
    active = await repo.get_active_for_investor(inv.id)
    assert active is not None and active.id == cur.id
    await repo.set_paused(cur.id, True)
    active = await repo.get_active_for_investor(inv.id)
    assert active is not None and active.status is SequenceStatus.PAUSED


async def test_mark_step_never_regresses_and_completes_on_last(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = SequenceRepository(session)
    seq, _ = await repo.create(
        kind=SequenceKind.INVESTOR, investor_id=inv.id, temporal_workflow_id="w"
    )
    nxt = NOW + timedelta(days=4)
    await repo.mark_step(seq.id, 0, next_send_at=nxt)
    assert seq.step == 1 and seq.next_send_at is not None
    await repo.mark_step(seq.id, 0, next_send_at=nxt)  # activity replay
    assert seq.step == 1
    await repo.mark_step(seq.id, 2)
    assert seq.step == 3 and seq.status is SequenceStatus.ACTIVE
    await repo.mark_step(seq.id, 3)
    assert seq.step == 4 and seq.status is SequenceStatus.COMPLETED
    assert seq.next_send_at is None


async def test_cancel_keeps_first_reason(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = SequenceRepository(session)
    seq, _ = await repo.create(
        kind=SequenceKind.INVESTOR, investor_id=inv.id, temporal_workflow_id="w"
    )
    await repo.cancel(seq.id, "opt_out", at=NOW)
    await repo.cancel(seq.id, "reply")
    assert seq.status is SequenceStatus.CANCELLED
    assert seq.cancel_reason == "opt_out"
    assert seq.cancelled_at is not None
    # A cancelled sequence cannot be paused or completed back to life.
    await repo.set_paused(seq.id, True)
    await repo.complete(seq.id)
    assert seq.status is SequenceStatus.CANCELLED


async def test_list_due(session: AsyncSession, make_investor: MakeInvestor) -> None:
    repo = SequenceRepository(session)
    a = await make_investor("A LLC")
    b = await make_investor("B LLC")
    c = await make_investor("C LLC")
    due, _ = await repo.create(
        kind=SequenceKind.INVESTOR,
        investor_id=a.id,
        temporal_workflow_id="due",
        next_send_at=NOW - timedelta(hours=1),
    )
    await repo.create(
        kind=SequenceKind.INVESTOR,
        investor_id=b.id,
        temporal_workflow_id="later",
        next_send_at=NOW + timedelta(days=1),
    )
    paused, _ = await repo.create(
        kind=SequenceKind.INVESTOR,
        investor_id=c.id,
        temporal_workflow_id="paused",
        next_send_at=NOW - timedelta(hours=2),
    )
    await repo.set_paused(paused.id, True)
    assert [s.id for s in await repo.list_due(NOW)] == [due.id]


async def test_require_raises_lookup_error(session: AsyncSession) -> None:
    repo = SequenceRepository(session)
    with pytest.raises(LookupError):
        await repo.cancel(uuid.uuid4(), "x")


# -- messages --------------------------------------------------------------------------------------


async def test_create_outbound_draft_is_idempotent_on_key(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = MessageRepository(session)
    thread = await repo.create_thread(
        investor_id=inv.id, counterpart_address="jane@acme.com", subject="Off-market deals"
    )
    key = f"seq:{uuid.uuid4()}:step:0"
    row, created = await repo.create_outbound_draft(
        thread=thread,
        to="jane@acme.com",
        subject="Off-market deals",
        body="Hi Jane",
        policy_action=PolicyAction.INVESTOR_INITIAL_OUTREACH,
        idempotency_key=key,
        from_address="deals@example.test",
        sequence_step=0,
    )
    assert created and row.draft and row.direction is Direction.OUT
    assert row.external_message_id == key and row.meta["idempotency_key"] == key
    assert row.policy_action == "investor_initial_outreach" and row.policy_decision is None
    again, created2 = await repo.create_outbound_draft(
        thread=thread,
        to="jane@acme.com",
        subject="x",
        body="y",
        policy_action=PolicyAction.INVESTOR_INITIAL_OUTREACH,
        idempotency_key=key,
        from_address=None,
    )
    assert not created2 and again.id == row.id and again.body == "Hi Jane"
    found = await repo.get_thread_for_investor(inv.id)
    assert found is not None and found.id == thread.id


async def test_mark_sent_sets_thread_conversation_and_is_idempotent(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = MessageRepository(session)
    thread = await repo.create_thread(
        investor_id=inv.id, counterpart_address="j@a.com", subject="s"
    )
    row, _ = await repo.create_outbound_draft(
        thread=thread,
        to="j@a.com",
        subject="s",
        body="b",
        policy_action=PolicyAction.INVESTOR_INITIAL_OUTREACH,
        idempotency_key="k1",
        from_address=None,
    )
    await repo.mark_sent(row.id, sent_at=NOW, internet_message_id="<m@x>", conversation_id="AAQk1")
    assert row.sent_at is not None and row.draft is False
    assert thread.external_thread_id == "AAQk1" and thread.last_message_at is not None
    await repo.mark_sent(row.id, sent_at=NOW + timedelta(days=1), conversation_id="OTHER")
    assert thread.external_thread_id == "AAQk1"
    assert row.meta["conversation_id"] == "AAQk1"
    await repo.set_policy_decision(row.id, PolicyDecision.SEND, "auto")
    assert row.policy_decision is PolicyDecision.SEND and row.meta["policy_reason"] == "auto"


def test_local_day_start_utc_handles_offsets() -> None:
    # 2026-10-27 03:30 UTC is still Oct 26 22:30 in Chicago (UTC-5 during DST).
    early = datetime(2026, 10, 27, 3, 30, tzinfo=UTC)
    assert local_day_start_utc(early, "America/Chicago") == datetime(2026, 10, 26, 5, 0, tzinfo=UTC)
    assert local_day_start_utc(early, "UTC") == datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
    naive = datetime(2026, 10, 27, 3, 30)
    assert local_day_start_utc(naive, "America/Chicago") == datetime(2026, 10, 26, 5, 0, tzinfo=UTC)


async def _sent(
    repo: MessageRepository,
    thread: Thread,
    key: str,
    action: PolicyAction,
    sent_at: datetime,
) -> Message:
    row, _ = await repo.create_outbound_draft(
        thread=thread,
        to="j@a.com",
        subject="s",
        body="b",
        policy_action=action,
        idempotency_key=key,
        from_address=None,
    )
    await repo.mark_sent(row.id, sent_at=sent_at)
    return row


async def test_count_sends_today_respects_market_timezone_boundary(
    session: AsyncSession, make_investor: MakeInvestor
) -> None:
    inv = await make_investor()
    repo = MessageRepository(session)
    thread = await repo.create_thread(
        investor_id=inv.id, counterpart_address="j@a.com", subject="s"
    )
    init = PolicyAction.INVESTOR_INITIAL_OUTREACH
    # Local day in Chicago on 2026-10-27 starts at 05:00 UTC.
    await _sent(repo, thread, "before", init, datetime(2026, 10, 27, 4, 59, tzinfo=UTC))
    await _sent(repo, thread, "at-midnight", init, datetime(2026, 10, 27, 5, 0, tzinfo=UTC))
    await _sent(repo, thread, "later", init, datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
    await _sent(
        repo,
        thread,
        "other-action",
        PolicyAction.INVESTOR_FOLLOWUP,
        datetime(2026, 10, 27, 14, 0, tzinfo=UTC),
    )
    # A draft that was never sent does not count.
    await repo.create_outbound_draft(
        thread=thread,
        to="j@a.com",
        subject="s",
        body="b",
        policy_action=init,
        idempotency_key="draft-only",
        from_address=None,
    )
    assert await repo.count_sends_today(init, "America/Chicago", now=NOW) == 2
    assert await repo.count_sends_today(init, "UTC", now=NOW) == 3
    assert (
        await repo.count_sends_today(PolicyAction.INVESTOR_FOLLOWUP, "America/Chicago", now=NOW)
        == 1
    )
    # Just before Chicago midnight the previous day's sends are still "today".
    late = datetime(2026, 10, 28, 4, 59, tzinfo=UTC)
    assert await repo.count_sends_today(init, "America/Chicago", now=late) == 2
    # One minute later a new Chicago day has begun.
    assert (
        await repo.count_sends_today(init, "America/Chicago", now=late + timedelta(minutes=1)) == 0
    )


async def test_is_suppressed(session: AsyncSession) -> None:
    repo = MessageRepository(session)
    session.add(
        Suppression(channel=Channel.EMAIL, normalized_value="jane@acme.com", reason="opt_out")
    )
    await session.flush()
    assert await repo.is_suppressed("Jane@Acme.com") is True
    assert await repo.is_suppressed("<jane@acme.com>") is True
    assert await repo.is_suppressed("bob@acme.com") is False
    assert await repo.is_suppressed("not-an-address") is True  # never mail what we cannot normalise


async def test_qa_verdict_lookup(session: AsyncSession, make_investor: MakeInvestor) -> None:
    inv = await make_investor()
    repo = MessageRepository(session)
    thread = await repo.create_thread(
        investor_id=inv.id, counterpart_address="j@a.com", subject="s"
    )
    row, _ = await repo.create_outbound_draft(
        thread=thread,
        to="j@a.com",
        subject="s",
        body="b",
        policy_action=PolicyAction.INVESTOR_INITIAL_OUTREACH,
        idempotency_key="k",
        from_address=None,
    )
    assert await repo.qa_verdict_for(row.id) is None
    session.add(QaReview(message_id=row.id, verdict=QaVerdict.REWRITE))
    await session.flush()
    assert await repo.qa_verdict_for(row.id) is QaVerdict.REWRITE
    approved = QaReview(message_id=row.id, verdict=QaVerdict.APPROVE)
    session.add(approved)
    await session.flush()
    row.qa_review_id = approved.id  # the message points at the review A5 attached
    await session.flush()
    assert await repo.qa_verdict_for(row.id) is QaVerdict.APPROVE


async def test_thread_cancel_reason(
    session: AsyncSession, make_investor: MakeInvestor, make_message: MakeMessage
) -> None:
    inv = await make_investor()
    repo = MessageRepository(session)
    thread = await repo.create_thread(
        investor_id=inv.id, counterpart_address="j@a.com", subject="s"
    )
    assert await repo.thread_cancel_reason(thread.id) is None
    unrelated = await make_message(thread=thread, body="newsletter")
    unrelated.classification = MessageClassification.UNRELATED
    await session.flush()
    assert await repo.thread_cancel_reason(thread.id) is None
    bounce = await make_message(thread=thread, body="Undeliverable")
    bounce.meta = {"is_bounce": True}
    await session.flush()
    assert await repo.thread_cancel_reason(thread.id) == "bounce"
    unclassified = await make_message(thread=thread, body="Yes, send me deals")
    assert unclassified.classification is None
    assert await repo.thread_cancel_reason(thread.id) == "reply"  # conservative
    opt = await make_message(thread=thread, body="unsubscribe")
    opt.classification = MessageClassification.OPT_OUT
    await session.flush()
    assert await repo.thread_cancel_reason(thread.id) == "opt_out"
    assert len(await repo.inbound_messages(thread.id)) == 4
    assert len(await repo.list_thread_messages(thread.id)) == 4
