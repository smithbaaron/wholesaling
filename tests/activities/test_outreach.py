"""Outreach activities against SQLite with the mock Graph gateway (REQUIREMENTS.md POL-03a/b,
INV-10, INV-12, INV-23, WF-21, XC-03, XC-07).

L0: everything is queued, nothing is sent. L1: QA-approved and under cap sends; cap reached
queues; a suppressed recipient is blocked; a reply on the thread cancels. Retried activities
never create a second draft or a second send.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.db.conftest import MakeInvestor, MakeMessage
from wholesaling.activities import graph as gacts
from wholesaling.activities import outreach as acts
from wholesaling.agents.templates import investor_outreach as t
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, Investor, Message, Policy, QaReview, Suppression
from wholesaling.db.models.enums import (
    Channel,
    Direction,
    MessageClassification,
    PolicyDecision,
    QaVerdict,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Sequence, Thread
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

MAILBOX = "deals@example.test"
CONTACT = "jane@acme.com"
# Tuesday 2026-10-27 10:00 America/Chicago (CDT, UTC-5)
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
INITIAL = PolicyAction.INVESTOR_INITIAL_OUTREACH


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


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "graph_mode": "mock",
        "graph_mailbox": MAILBOX,
        "sender_name": "Aaron",
        "sender_company": "NorthStack",
        "sender_postal_address": "123 Main St, Houston, TX 77002",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
async def ctx(
    gw: MockGraphGateway,
    clock: FakeClock,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[gacts.GraphActivityContext]:
    context = gacts.GraphActivityContext(
        gateway=gw, session_factory=session_factory, settings=_settings(), clock=clock
    )
    gacts.set_graph_context(context)
    try:
        yield context
    finally:
        gacts.set_graph_context(None)


@pytest.fixture
async def seq(session: AsyncSession, make_investor: MakeInvestor) -> Sequence:
    inv = await make_investor("Acme Holdings LLC", first_name="Jane")
    row, _ = await SequenceRepository(session).create(
        kind=SequenceKind.INVESTOR,
        investor_id=inv.id,
        temporal_workflow_id=f"investor-sequence:{uuid.uuid4()}",
    )
    await session.commit()
    return row


async def _set_level(
    session: AsyncSession,
    action: PolicyAction,
    level: AutonomyLevel,
    *,
    daily_cap: int | None = None,
) -> None:
    await PolicyRepository(session).set_level(action, level, updated_by="test", daily_cap=daily_cap)
    await session.commit()


async def _qa(session: AsyncSession, message_id: str, verdict: QaVerdict) -> None:
    session.add(
        QaReview(message_id=uuid.UUID(message_id), verdict=verdict, confidence=Decimal("0.9"))
    )
    await session.commit()


async def _prepare(seq: Sequence, step: int, market: str = "Dallas") -> acts.PrepareStepResult:
    return await acts.prepare_step(
        acts.PrepareStepInput(
            sequence_id=str(seq.id), step=step, contact_email=CONTACT, market=market
        )
    )


async def _decide(prepared: acts.PrepareStepResult, step: int) -> acts.DecideAndSendResult:
    return await acts.decide_and_send(
        acts.DecideAndSendInput(message_id=prepared.message_id, step=step)
    )


async def _count(session: AsyncSession, model: type, **where: object) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int((await session.execute(stmt)).scalar_one())


async def _message(session: AsyncSession, message_id: str) -> Message:
    row = await session.get(Message, uuid.UUID(message_id))
    assert row is not None
    await session.refresh(row)
    return row


# -- prepare_step ----------------------------------------------------------------------------------


async def test_prepare_step_writes_draft_row_and_thread(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    res = await _prepare(seq, 0)
    assert res.created and not res.already_sent
    assert res.idempotency_key == f"seq:{seq.id}:step:0" == acts.idempotency_key(str(seq.id), 0)
    assert res.policy_action == "investor_initial_outreach"
    assert res.approval_status == t.APPROVED and res.template_key == "investor_initial"
    row = await _message(session, res.message_id)
    assert row.draft and row.direction is Direction.OUT and row.sent_at is None
    assert row.external_message_id == res.idempotency_key
    assert row.to_addresses == [CONTACT] and row.from_address == MAILBOX
    assert row.sequence_id == seq.id and row.sequence_step == 0
    assert row.policy_decision is None  # no decision yet
    assert row.body.startswith("Hi Jane,") and "Dallas" in row.body and "123 Main St" in row.body
    assert row.meta["subject_variant"] in (0, 1, 2)
    await session.refresh(seq)
    assert seq.thread_id == uuid.UUID(res.thread_id)
    assert gw.drafts == {}, "prepare_step never touches Graph"
    assert await _count(session, AuditEvent, event_type="outreach.step_prepared") == 1


async def test_prepare_step_is_idempotent(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    first = await _prepare(seq, 0)
    second = await _prepare(seq, 0)
    assert not second.created and second.message_id == first.message_id
    assert await _count(session, Message) == 1
    assert await _count(session, AuditEvent, event_type="outreach.step_prepared") == 1


async def test_prepare_step_followups_share_the_thread(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    results = [await _prepare(seq, s) for s in range(4)]
    assert len({r.thread_id for r in results}) == 1
    assert [r.approval_status for r in results] == [t.APPROVED] + [t.PENDING_APPROVAL] * 3
    assert results[3].template_key == "investor_followup_3"
    assert all(r.policy_action == "investor_followup" for r in results[1:])


async def test_prepare_step_refuses_prohibited_market_text(
    ctx: gacts.GraphActivityContext, seq: Sequence
) -> None:
    with pytest.raises(t.ProhibitedClaimError):
        await _prepare(seq, 0, market="Dallas (exclusive inventory)")


async def test_prepare_step_unknown_sequence(ctx: gacts.GraphActivityContext) -> None:
    with pytest.raises(LookupError):
        await acts.prepare_step(
            acts.PrepareStepInput(
                sequence_id=str(uuid.uuid4()), step=0, contact_email=CONTACT, market="Dallas"
            )
        )


# -- decide_and_send: L0 ---------------------------------------------------------------------------


async def test_l0_initial_is_queued_and_nothing_is_sent(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    prepared = await _prepare(seq, 0)
    res = await _decide(prepared, 0)
    assert res.decision == "queue_for_human" and not res.sent and res.level == "L0"
    assert res.sends_today == 0 and res.qa_verdict is None
    row = await _message(session, res.message_id)
    assert row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN and row.draft
    assert row.sent_at is None and "graph_draft_id" not in row.meta
    assert gw.drafts == {} and gw.sent == []
    assert await _count(session, AuditEvent, event_type="outreach.policy_decided") == 1
    assert await _count(session, AuditEvent, event_type="message.sent") == 0


async def test_l0_followup_is_queued_even_with_qa_approval(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    p0 = await _prepare(seq, 0)
    await _qa(session, p0.message_id, QaVerdict.APPROVE)
    assert (await _decide(p0, 0)).decision == "queue_for_human"
    p1 = await _prepare(seq, 1)
    await _qa(session, p1.message_id, QaVerdict.APPROVE)
    res = await _decide(p1, 1)
    assert res.decision == "queue_for_human" and res.action == "investor_followup"
    assert gw.sent == []


# -- decide_and_send: L1 ---------------------------------------------------------------------------


async def test_l1_without_qa_review_is_queued(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    await _set_level(
        session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, daily_cap=50
    )
    res = await _decide(await _prepare(seq, 0), 0)
    assert res.decision == "queue_for_human" and "QA" in res.reason


async def test_l1_qa_approved_under_cap_sends_through_graph(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(
        session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, daily_cap=50
    )
    prepared = await _prepare(seq, 0)
    await _qa(session, prepared.message_id, QaVerdict.APPROVE)
    res = await _decide(prepared, 0)
    assert res.decision == "send" and res.sent and res.qa_verdict == "approve"
    row = await _message(session, res.message_id)
    assert row.sent_at is not None and row.draft is False
    assert row.policy_decision is PolicyDecision.SEND
    assert row.meta["graph_draft_id"] in gw.drafts and gw.drafts[row.meta["graph_draft_id"]]["sent"]
    assert len(gw.sent) == 1
    sent = gw.drafts[row.meta["graph_draft_id"]]
    assert sent["to"] == [CONTACT] and sent["body"] == row.body and sent["in_reply_to"] is None
    # Thread now carries the Outlook conversation id (INV-23).
    await session.refresh(seq)
    assert seq.thread_id is not None
    thread = await session.get(Thread, seq.thread_id)
    assert thread is not None and thread.external_thread_id == sent["conversation_id"]
    assert await _count(session, AuditEvent, event_type="message.sent") == 1
    assert await _count(session, AuditEvent, event_type="message.drafted") == 0  # inline draft


async def test_l1_qa_block_is_blocked_and_rewrite_is_queued(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(
        session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, daily_cap=50
    )
    p = await _prepare(seq, 0)
    await _qa(session, p.message_id, QaVerdict.REWRITE)
    assert (await _decide(p, 0)).decision == "queue_for_human"
    await _qa(session, p.message_id, QaVerdict.BLOCK)  # latest review wins
    res = await _decide(p, 0)
    assert res.decision == "blocked"
    assert (await _message(session, p.message_id)).policy_decision is PolicyDecision.BLOCKED
    assert gw.sent == []


async def test_l1_daily_cap_reached_queues(
    ctx: gacts.GraphActivityContext,
    seq: Sequence,
    session: AsyncSession,
    gw: MockGraphGateway,
    make_investor: MakeInvestor,
) -> None:
    await _set_level(session, INITIAL, AutonomyLevel.L1, daily_cap=1)
    p = await _prepare(seq, 0)
    await _qa(session, p.message_id, QaVerdict.APPROVE)
    assert (await _decide(p, 0)).decision == "send"
    other = await make_investor("Beta Capital LLC", first_name="Bob")
    seq2, _ = await SequenceRepository(session).create(
        kind=SequenceKind.INVESTOR, investor_id=other.id, temporal_workflow_id="investor-sequence:2"
    )
    await session.commit()
    p2 = await _prepare(seq2, 0)
    await _qa(session, p2.message_id, QaVerdict.APPROVE)
    res = await _decide(p2, 0)
    assert res.decision == "queue_for_human" and "cap" in res.reason and res.sends_today == 1
    assert len(gw.sent) == 1


async def test_cap_counts_the_market_local_day(
    ctx: gacts.GraphActivityContext,
    seq: Sequence,
    session: AsyncSession,
    gw: MockGraphGateway,
    clock: FakeClock,
    make_investor: MakeInvestor,
) -> None:
    await _set_level(session, INITIAL, AutonomyLevel.L1, daily_cap=1)
    p = await _prepare(seq, 0)
    await _qa(session, p.message_id, QaVerdict.APPROVE)
    assert (await _decide(p, 0)).decision == "send"
    # Next Chicago day (05:00 UTC boundary): the cap resets.
    clock.now = datetime(2026, 10, 28, 5, 1, tzinfo=UTC)
    other = await make_investor("Gamma LLC", first_name="Gus")
    seq2, _ = await SequenceRepository(session).create(
        kind=SequenceKind.INVESTOR, investor_id=other.id, temporal_workflow_id="investor-sequence:3"
    )
    await session.commit()
    p2 = await _prepare(seq2, 0)
    await _qa(session, p2.message_id, QaVerdict.APPROVE)
    res = await acts.decide_and_send(
        acts.DecideAndSendInput(message_id=p2.message_id, step=0, timezone="America/Chicago")
    )
    assert res.decision == "send" and res.sends_today == 0
    assert len(gw.sent) == 2


async def test_suppressed_recipient_is_blocked(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(
        session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, daily_cap=50
    )
    p = await _prepare(seq, 0)
    await _qa(session, p.message_id, QaVerdict.APPROVE)
    session.add(Suppression(channel=Channel.EMAIL, normalized_value=CONTACT, reason="opt_out"))
    await session.commit()
    res = await _decide(p, 0)
    assert res.decision == "blocked" and "suppressed" in res.reason and not res.sent
    assert (await _message(session, p.message_id)).policy_decision is PolicyDecision.BLOCKED
    assert gw.drafts == {} and gw.sent == []


async def test_l1_followup_requires_sent_initial_and_qa(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(
        session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, daily_cap=50
    )
    await _set_level(session, PolicyAction.INVESTOR_FOLLOWUP, AutonomyLevel.L1)
    p0 = await _prepare(seq, 0)
    p1 = await _prepare(seq, 1)
    await _qa(session, p1.message_id, QaVerdict.APPROVE)
    # Initial still queued (no QA) -> follow-up cannot auto-send.
    assert (await _decide(p0, 0)).decision == "queue_for_human"
    res = await _decide(p1, 1)
    assert res.decision == "queue_for_human" and "initial" in res.reason
    # Initial goes out -> follow-up with QA approval sends as a reply in the same conversation.
    await _qa(session, p0.message_id, QaVerdict.APPROVE)
    assert (await _decide(p0, 0)).decision == "send"
    res = await _decide(p1, 1)
    assert res.decision == "send" and res.sent
    convs = {d["conversation_id"] for d in gw.drafts.values()}
    assert len(convs) == 1 and len(gw.sent) == 2


async def test_decide_and_send_retry_after_send_does_not_resend(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(
        session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, daily_cap=50
    )
    p = await _prepare(seq, 0)
    await _qa(session, p.message_id, QaVerdict.APPROVE)
    first = await _decide(p, 0)
    again = await _decide(p, 0)
    assert first.sent and again.sent and again.reason == "already sent"
    assert len(gw.sent) == 1 and len(gw.drafts) == 1
    assert await _count(session, AuditEvent, event_type="message.sent") == 1
    assert await _count(session, AuditEvent, event_type="outreach.policy_decided") == 1


async def test_l2_sends_without_qa(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L2)
    res = await _decide(await _prepare(seq, 0), 0)
    assert res.decision == "send" and res.sent and res.level == "L2"


# -- send_approved ---------------------------------------------------------------------------------


async def test_send_approved_sends_queued_draft_with_edit_and_footer(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    p = await _prepare(seq, 0)
    assert (await _decide(p, 0)).decision == "queue_for_human"
    res = await acts.send_approved(
        acts.SendApprovedInput(
            message_id=p.message_id, step=0, edited_body="Hi Jane, quick hello.", actor_id="aaron"
        )
    )
    assert res.sent and res.body_edited and res.blocked_reason is None
    row = await _message(session, p.message_id)
    assert row.sent_at is not None and row.policy_decision is PolicyDecision.SEND
    assert row.body.startswith("Hi Jane, quick hello.") and "123 Main St" in row.body
    assert "unsubscribe" in row.body  # CMP-01 footer re-attached
    assert row.meta["human_approved"] is True and row.meta["approved_by"] == "aaron"
    assert "original_body" in row.meta
    assert len(gw.sent) == 1
    assert await _count(session, AuditEvent, event_type="outreach.human_approved") == 1
    # Replay: nothing happens twice.
    again = await acts.send_approved(acts.SendApprovedInput(message_id=p.message_id, step=0))
    assert again.sent and not again.body_edited and len(gw.sent) == 1


async def test_send_approved_without_edit_keeps_body(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    p = await _prepare(seq, 0)
    before = (await _message(session, p.message_id)).body
    res = await acts.send_approved(acts.SendApprovedInput(message_id=p.message_id, step=0))
    assert res.sent and not res.body_edited
    assert (await _message(session, p.message_id)).body == before


async def test_send_approved_still_honours_suppression(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    p = await _prepare(seq, 0)
    session.add(Suppression(channel=Channel.EMAIL, normalized_value=CONTACT, reason="opt_out"))
    await session.commit()
    res = await acts.send_approved(acts.SendApprovedInput(message_id=p.message_id, step=0))
    assert not res.sent and res.blocked_reason == "recipient is suppressed"
    assert gw.sent == []


async def test_followup_without_conversation_cannot_be_sent(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    await _prepare(seq, 0)  # never sent -> thread has no conversation id
    p1 = await _prepare(seq, 1)
    with pytest.raises(RuntimeError, match="conversation"):
        await acts.send_approved(acts.SendApprovedInput(message_id=p1.message_id, step=1))


# -- check_cancel_conditions -----------------------------------------------------------------------


async def _check(seq: Sequence) -> acts.CheckCancelResult:
    return await acts.check_cancel_conditions(
        acts.CheckCancelInput(sequence_id=str(seq.id), contact_email=CONTACT)
    )


async def test_check_cancel_reply_on_thread(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, make_message: MakeMessage
) -> None:
    assert await _check(seq) == acts.CheckCancelResult(cancel_reason=None, paused=False)
    await _prepare(seq, 0)
    await session.refresh(seq)
    assert await _check(seq) == acts.CheckCancelResult(cancel_reason=None, paused=False)
    thread = await session.get(Thread, seq.thread_id)
    assert thread is not None
    reply = await make_message(thread=thread, body="Yes - send me what you have")
    reply.classification = MessageClassification.INTERESTED
    await session.commit()
    assert (await _check(seq)).cancel_reason == "reply"


async def test_check_cancel_opt_out_bounce_suppression_and_pause(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, make_message: MakeMessage
) -> None:
    await _prepare(seq, 0)
    await session.refresh(seq)
    thread = await session.get(Thread, seq.thread_id)
    assert thread is not None
    bounce = await make_message(thread=thread, body="Undeliverable")
    bounce.meta = {"is_bounce": True}
    await session.commit()
    assert (await _check(seq)).cancel_reason == "bounce"
    opt = await make_message(thread=thread, body="remove me")
    opt.classification = MessageClassification.OPT_OUT
    await session.commit()
    assert (await _check(seq)).cancel_reason == "opt_out"

    await SequenceRepository(session).set_paused(seq.id, True)
    await session.commit()
    # Opt-out outranks pause; without inbound messages pause is reported.
    assert (await _check(seq)).cancel_reason == "opt_out"
    for m in (bounce, opt):
        await session.delete(m)
    await session.commit()
    assert await _check(seq) == acts.CheckCancelResult(cancel_reason=None, paused=True)

    session.add(Suppression(channel=Channel.EMAIL, normalized_value=CONTACT, reason="bounce"))
    await session.commit()
    assert (await _check(seq)).cancel_reason == "suppressed"


async def test_check_cancel_reports_row_state(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    await SequenceRepository(session).cancel(seq.id, "manual")
    await session.commit()
    assert (await _check(seq)).cancel_reason == "manual"


# -- send window (INV-12) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now_utc", "expected_seconds"),
    [
        (datetime(2026, 10, 27, 15, 0, tzinfo=UTC), 0.0),  # Tue 10:00 CDT, open
        (datetime(2026, 10, 27, 13, 59, tzinfo=UTC), 60.0),  # Tue 08:59 CDT -> 09:00
        (datetime(2026, 10, 27, 22, 0, tzinfo=UTC), 16 * 3600.0),  # Tue 17:00 CDT -> Wed 09:00
        (datetime(2026, 10, 31, 15, 0, tzinfo=UTC), 2 * 86400.0 - 3600.0),  # Sat 10:00 -> Mon 9
        # Sun 09:00 CST (DST ended that morning) -> Mon 09:00 CST
        (datetime(2026, 11, 1, 15, 0, tzinfo=UTC), 86400.0),
    ],
)
def test_seconds_until_send_window(now_utc: datetime, expected_seconds: float) -> None:
    secs = acts.seconds_until_send_window(
        now_utc, "America/Chicago", start_hour=9, end_hour=17, weekdays_only=True
    )
    assert secs == expected_seconds


def test_send_window_weekends_allowed_and_bad_zone_falls_back() -> None:
    sat = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)
    assert (
        acts.seconds_until_send_window(
            sat, "America/Chicago", start_hour=9, end_hour=17, weekdays_only=False
        )
        == 0.0
    )
    assert (
        acts.seconds_until_send_window(
            sat, "Not/AZone", start_hour=9, end_hour=24, weekdays_only=False
        )
        == 0.0
    )
    with pytest.raises(ValueError, match="window"):
        acts.seconds_until_send_window(sat, "UTC", start_hour=17, end_hour=9)


async def test_send_window_activity_uses_market_timezone(
    ctx: gacts.GraphActivityContext, session: AsyncSession, clock: FakeClock
) -> None:
    session.add(Market(name="Phoenix", state="AZ", timezone="America/Phoenix"))
    await session.commit()
    clock.now = datetime(2026, 10, 27, 15, 30, tzinfo=UTC)  # 08:30 in Phoenix (UTC-7)
    res = await acts.send_window(acts.SendWindowInput(market="Phoenix"))
    assert res.timezone == "America/Phoenix" and res.seconds_until_open == 1800.0
    assert (res.start_hour, res.end_hour) == (9, 17)
    unknown = await acts.send_window(acts.SendWindowInput(market="Nowhere"))
    assert unknown.timezone == acts.DEFAULT_TIMEZONE and unknown.seconds_until_open == 0.0


# -- update_sequence -------------------------------------------------------------------------------


async def test_update_sequence_progress_completion_and_cancel(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, clock: FakeClock
) -> None:
    res = await acts.update_sequence(
        acts.UpdateSequenceInput(
            sequence_id=str(seq.id), step_done=0, next_send_in_seconds=4 * 86400
        )
    )
    assert res.status == "active" and res.step == 1
    await session.refresh(seq)
    assert seq.next_send_at is not None
    next_at = (
        seq.next_send_at.replace(tzinfo=UTC)
        if seq.next_send_at.tzinfo is None
        else seq.next_send_at
    )
    assert next_at == clock.now + timedelta(days=4)
    for step in (1, 2):
        await acts.update_sequence(
            acts.UpdateSequenceInput(
                sequence_id=str(seq.id), step_done=step, next_send_in_seconds=1
            )
        )
    res = await acts.update_sequence(acts.UpdateSequenceInput(sequence_id=str(seq.id), step_done=3))
    assert res.status == "completed" and res.step == 4
    res = await acts.update_sequence(
        acts.UpdateSequenceInput(sequence_id=str(seq.id), completed=True)
    )
    assert res.status == "completed"
    assert await _count(session, AuditEvent, event_type="sequence.completed") == 1


async def test_update_sequence_cancel_is_audited_once(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    for _ in range(2):
        res = await acts.update_sequence(
            acts.UpdateSequenceInput(sequence_id=str(seq.id), cancel_reason="reply")
        )
        assert res.status == "cancelled" and res.cancel_reason == "reply"
    assert await _count(session, AuditEvent, event_type="sequence.cancelled") == 1
    await session.refresh(seq)
    assert seq.status is SequenceStatus.CANCELLED


async def test_update_sequence_pause_flag(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    assert (
        await acts.update_sequence(acts.UpdateSequenceInput(sequence_id=str(seq.id), paused=True))
    ).status == "paused"
    assert (
        await acts.update_sequence(acts.UpdateSequenceInput(sequence_id=str(seq.id), paused=False))
    ).status == "active"


# -- full L1 step sequence in one conversation (INV-23) --------------------------------------------


async def test_four_steps_land_in_one_conversation_at_l1(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession, gw: MockGraphGateway
) -> None:
    await _set_level(
        session, PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, daily_cap=50
    )
    await _set_level(session, PolicyAction.INVESTOR_FOLLOWUP, AutonomyLevel.L1)
    for step in range(4):
        p = await _prepare(seq, step)
        await _qa(session, p.message_id, QaVerdict.APPROVE)
        res = await _decide(p, step)
        assert res.decision == "send" and res.sent, step
    assert len(gw.sent) == 4
    assert len({d["conversation_id"] for d in gw.drafts.values()}) == 1
    rows = (await session.execute(select(Message).order_by(Message.sequence_step))).scalars().all()
    assert [r.sequence_step for r in rows] == [0, 1, 2, 3]
    assert len({r.thread_id for r in rows}) == 1
    assert all(r.sent_at is not None for r in rows)
    assert "Did we miss the mark" in rows[3].body


async def test_policy_context_for_investor(
    ctx: gacts.GraphActivityContext, seq: Sequence, session: AsyncSession
) -> None:
    p0 = await _prepare(seq, 0)
    p1 = await _prepare(seq, 1)
    row1 = await _message(session, p1.message_id)
    pctx = await acts.build_policy_context(session, row1, tz="America/Chicago", now=NOW)
    assert pctx.initial_was_approved is False and pctx.sends_today == 0
    assert pctx.recipient_suppressed is False and pctx.qa_verdict is None
    row0 = await _message(session, p0.message_id)
    pctx0 = await acts.build_policy_context(session, row0, tz="America/Chicago", now=NOW)
    assert pctx0.initial_was_approved is None  # not a follow-up


async def test_investor_fixture_has_first_name(session: AsyncSession, seq: Sequence) -> None:
    inv = await session.get(Investor, seq.investor_id)
    assert inv is not None and inv.first_name == "Jane"
    assert await _count(session, Policy) == 0  # policies table empty -> L0 everywhere (POL-02)
