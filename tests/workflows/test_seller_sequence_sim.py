"""SellerSequenceWorkflow run loop driven by a fake ``workflow`` module (no server), the harness
of ``test_investor_sequence_sim.py`` (REQUIREMENTS.md SEL-04, WF-03, WF-21)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.workflows.test_investor_sequence_sim import (
    FakeWorkflowModule,
    _approved,
    _decide,
    _ok_check,
    _open_window,
    _update,
)
from wholesaling.activities import outreach as oacts
from wholesaling.activities import seller as acts
from wholesaling.workflows import seller_sequence as ws
from wholesaling.workflows.seller_sequence import (
    HumanDecision,
    SellerSequenceInput,
    SellerSequenceWorkflow,
)

SEQ = "22222222-2222-2222-2222-222222222222"
DAY = 86_400.0


def _prepare(i: acts.SellerPrepareStepInput) -> oacts.PrepareStepResult:
    return oacts.PrepareStepResult(
        message_id=f"msg-{i.step}",
        thread_id="thread",
        idempotency_key=oacts.idempotency_key(i.sequence_id, i.step),
        subject="s",
        template_key="seller_initial" if i.step == 0 else f"seller_followup_{i.step}",
        approval_status="pending_approval",
        policy_action="seller_outreach_email",
        created=True,
        already_sent=False,
    )


def _handlers(**overrides: Callable[[Any], Any]) -> dict[str, Callable[[Any], Any]]:
    base: dict[str, Callable[[Any], Any]] = {
        "check_cancel_conditions": _ok_check,
        "send_window": _open_window,
        "seller_prepare_step": _prepare,
        "seller_decide_and_send": _decide("send", "auto"),
        "seller_send_approved": _approved,
        "update_sequence": _update,
    }
    base.update(overrides)
    return base


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWorkflowModule]:
    def _make(**overrides: Callable[[Any], Any]) -> FakeWorkflowModule:
        wf = SellerSequenceWorkflow()
        fake = FakeWorkflowModule(wf=wf, handlers=_handlers(**overrides))  # type: ignore[arg-type]
        monkeypatch.setattr(ws, "workflow", fake)
        return fake

    return _make


def _input(**kw: Any) -> SellerSequenceInput:
    return SellerSequenceInput(
        sequence_id=SEQ,
        seller_lead_id="lead",
        contact_email="dolores@example.test",
        market="Houston",
        **kw,
    )


def _names(fake: FakeWorkflowModule) -> list[str]:
    return [c.name for c in fake.calls]


def _awaiting(step: int) -> Callable[[Any], bool]:
    return lambda wf: wf._state == ws.STATE_AWAITING_HUMAN and wf._step == step


async def test_l2_auto_sends_four_steps_with_3_7_14_day_sleeps(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    result = await fake.wf.run(_input())
    assert result.completed and result.steps_sent == [0, 1, 2, 3]
    assert fake.slept == [3 * DAY, 7 * DAY, 14 * DAY]
    per_step = [
        "check_cancel_conditions",
        "send_window",
        "seller_prepare_step",
        "seller_decide_and_send",
        "update_sequence",
    ]
    assert _names(fake) == per_step * 4 + ["update_sequence"]
    keys = [c.arg for c in fake.calls if c.name == "seller_prepare_step"]
    assert [(k.sequence_id, k.step, k.contact_email, k.market) for k in keys] == [
        (SEQ, s, "dolores@example.test", "Houston") for s in range(4)
    ]
    updates = [c.arg for c in fake.calls if c.name == "update_sequence"]
    assert [u.next_send_in_seconds for u in updates[:4]] == [3 * DAY, 7 * DAY, 14 * DAY, None]
    assert fake.wf.status()["state"] == ws.STATE_DONE


async def test_intake_complete_during_sleep_cancels_remaining_steps(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.events.append(
        (
            lambda wf: wf._steps_sent == [0],
            lambda: fake.wf.intake_complete(
                acts.IntakeCompleteSignal(intake_id="i1", seller_lead_id="lead")
            ),
        )
    )
    result = await fake.wf.run(_input())
    assert (
        not result.completed
        and result.cancel_reason == "intake_complete"
        and result.steps_sent == [0]
    )
    assert _names(fake).count("seller_prepare_step") == 1
    assert fake.calls[-1].arg.cancel_reason == "intake_complete"
    assert fake.wf.status()["last_signal"]["intake_id"] == "i1"


@pytest.mark.parametrize("signal", ["reply", "opt_out", "bounce", "intake_complete"])
async def test_cancelling_signals_while_awaiting_human(
    sim: Callable[..., FakeWorkflowModule], signal: str
) -> None:
    fake = sim(seller_decide_and_send=_decide("queue_for_human"))
    fake.events.append((_awaiting(0), getattr(fake.wf, signal)))
    result = await fake.wf.run(_input())
    assert result.cancel_reason == signal and result.steps_sent == []
    assert "seller_send_approved" not in _names(fake)


async def test_reply_during_sleep_after_step_1(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim()
    fake.events.append((lambda wf: wf._steps_sent == [0, 1], fake.wf.reply))
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "reply" and result.steps_sent == [0, 1]
    assert fake.slept == [3 * DAY]


async def test_scripted_human_approvals_send_each_queued_step(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(seller_decide_and_send=_decide("queue_for_human", "draft at L0: human sends"))
    for step in range(4):
        fake.events.append(
            (
                _awaiting(step),
                lambda step=step: fake.wf.human_decision(  # type: ignore[misc]
                    HumanDecision(
                        step=step,
                        approved=True,
                        edited_body="edited" if step == 1 else None,
                        actor_id="aaron",
                    )
                ),
            )
        )
    result = await fake.wf.run(_input())
    assert result.completed and result.decisions == {str(s): "send" for s in range(4)}
    approvals = [c.arg for c in fake.calls if c.name == "seller_send_approved"]
    assert [(a.message_id, a.step, a.edited_body) for a in approvals] == [
        ("msg-0", 0, None),
        ("msg-1", 1, "edited"),
        ("msg-2", 2, None),
        ("msg-3", 3, None),
    ]


async def test_human_rejection_and_blocked_cancel(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(seller_decide_and_send=_decide("queue_for_human"))
    fake.events.append(
        (_awaiting(0), lambda: fake.wf.human_decision(HumanDecision(step=0, approved=False)))
    )
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "human_rejected:step:0"
    blocked = sim(seller_decide_and_send=_decide("blocked", "QA blocked the draft"))
    result = await blocked.wf.run(_input())
    assert result.cancel_reason == "blocked:QA blocked the draft"


async def test_thread_reply_or_suppression_found_by_check_cancels(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(
        check_cancel_conditions=lambda _: oacts.CheckCancelResult(
            cancel_reason="suppressed", paused=False
        )
    )
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "suppressed" and _names(fake) == [
        "check_cancel_conditions",
        "update_sequence",
    ]


async def test_pause_resume_and_send_window(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(send_window=lambda _: oacts.SendWindowResult(3600.0, "America/Chicago", 9, 17))
    fake.events.append(
        (lambda wf: wf._state == ws.STATE_SLEEPING and not wf._paused, fake.wf.pause)
    )
    fake.events.append((lambda wf: wf._paused, fake.wf.resume))
    result = await fake.wf.run(_input(intervals_days=[3]))
    assert result.completed and fake.slept == [3600.0, 3 * DAY, 3600.0]
    decided = [c.arg for c in fake.calls if c.name == "seller_decide_and_send"]
    assert all(d.timezone == "America/Chicago" for d in decided)


async def test_custom_intervals_and_disabled_window(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(send_window=lambda _: oacts.SendWindowResult(3600.0, "UTC", 9, 17))
    result = await fake.wf.run(_input(intervals_days=[2, 5], honor_send_window=False))
    assert result.steps_sent == [0, 1, 2] and fake.slept == [2 * DAY, 5 * DAY]
    assert "send_window" not in _names(fake)


async def test_duplicate_cancelling_signals_are_no_ops() -> None:
    wf = SellerSequenceWorkflow()
    wf.opt_out()
    wf.reply()
    wf.intake_complete()
    assert wf.status()["cancel_reason"] == "opt_out"


async def test_human_decision_without_step_maps_onto_pending_message() -> None:
    wf = SellerSequenceWorkflow()
    wf._pending_message_id = "msg-1"
    wf._step = 1
    wf.human_decision(HumanDecision(step=None, approved=True, message_id="nope"))
    assert wf._human == {} and wf.status()["unmatched_decisions"] == 1
    wf.human_decision(HumanDecision(step=None, approved=True, message_id="msg-1"))
    assert wf._human[1].approved
