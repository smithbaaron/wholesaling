"""InvestorSequenceWorkflow run-loop simulation without a Temporal server.

The time-skipping e2e tests (``test_investor_sequence_workflow.py``) need a binary download that
the sandbox blocks, so this module drives ``run()`` with a fake ``workflow`` module: activities
are scripted by name, ``wait_condition`` delivers signals at chosen moments and advances a fake
clock on timeout. It checks the control flow (step order, cancellation points, pause handling,
send window, idempotency keys) deterministically.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from wholesaling.activities import outreach as acts
from wholesaling.workflows import investor_sequence as ws
from wholesaling.workflows.investor_sequence import (
    HumanDecision,
    InvestorSequenceInput,
    InvestorSequenceWorkflow,
)

SEQ = "11111111-1111-1111-1111-111111111111"
DAY = 86_400.0


@dataclass
class Call:
    name: str
    arg: Any


@dataclass
class FakeWorkflowModule:
    """Stands in for ``temporalio.workflow`` inside the workflow module under test."""

    wf: InvestorSequenceWorkflow
    handlers: dict[str, Callable[[Any], Any]]
    # (predicate on workflow, action) pairs applied at the start of a wait; e.g. deliver a signal
    # once the workflow is awaiting a human for step n.
    events: list[tuple[Callable[[InvestorSequenceWorkflow], bool], Callable[[], None]]] = field(
        default_factory=list
    )
    calls: list[Call] = field(default_factory=list)
    clock: datetime = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
    slept: list[float] = field(default_factory=list)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("sim"))

    class unsafe:
        @staticmethod
        def imports_passed_through() -> Any:  # pragma: no cover - import-time only
            raise AssertionError("not used after import")

    def now(self) -> datetime:
        return self.clock

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        name = fn.__name__
        self.calls.append(Call(name, arg))
        return self.handlers[name](arg)

    def _fire_events(self) -> None:
        remaining = []
        for pred, action in self.events:
            if pred(self.wf):
                action()
            else:
                remaining.append((pred, action))
        self.events = remaining

    async def wait_condition(
        self,
        pred: Callable[[], bool],
        timeout: timedelta | None = None,  # noqa: ASYNC109 - mirrors workflow.wait_condition
    ) -> None:
        self._fire_events()
        if pred():
            return
        if timeout is None:
            msg = f"workflow would wait forever (state={self.wf._state}, step={self.wf._step})"
            raise RuntimeError(msg)
        self.clock += timeout
        self.slept.append(timeout.total_seconds())
        self._fire_events()
        if pred():
            return
        raise TimeoutError


def _ok_check(_: Any) -> acts.CheckCancelResult:
    return acts.CheckCancelResult(cancel_reason=None, paused=False)


def _open_window(_: Any) -> acts.SendWindowResult:
    return acts.SendWindowResult(0.0, "America/Chicago", 9, 17)


def _prepare(i: acts.PrepareStepInput) -> acts.PrepareStepResult:
    return acts.PrepareStepResult(
        message_id=f"msg-{i.step}",
        thread_id="thread",
        idempotency_key=acts.idempotency_key(i.sequence_id, i.step),
        subject="s",
        template_key="t",
        approval_status="approved" if i.step == 0 else "pending_approval",
        policy_action="investor_initial_outreach" if i.step == 0 else "investor_followup",
        created=True,
        already_sent=False,
    )


def _decide(decision: str, reason: str = "") -> Callable[[Any], acts.DecideAndSendResult]:
    def _h(i: acts.DecideAndSendInput) -> acts.DecideAndSendResult:
        return acts.DecideAndSendResult(
            message_id=i.message_id,
            decision=decision,
            reason=reason,
            level="L0",
            action="x",
            sent=decision == "send",
        )

    return _h


def _approved(i: acts.SendApprovedInput) -> acts.SendApprovedResult:
    return acts.SendApprovedResult(message_id=i.message_id, sent=True, body_edited=False)


def _update(i: acts.UpdateSequenceInput) -> acts.UpdateSequenceResult:
    status = "cancelled" if i.cancel_reason else ("completed" if i.completed else "active")
    return acts.UpdateSequenceResult(status=status, step=0, cancel_reason=i.cancel_reason)


def _handlers(**overrides: Callable[[Any], Any]) -> dict[str, Callable[[Any], Any]]:
    base: dict[str, Callable[[Any], Any]] = {
        "check_cancel_conditions": _ok_check,
        "send_window": _open_window,
        "prepare_step": _prepare,
        "decide_and_send": _decide("send", "auto"),
        "send_approved": _approved,
        "update_sequence": _update,
    }
    base.update(overrides)
    return base


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWorkflowModule]:
    def _make(**overrides: Callable[[Any], Any]) -> FakeWorkflowModule:
        wf = InvestorSequenceWorkflow()
        fake = FakeWorkflowModule(wf=wf, handlers=_handlers(**overrides))
        monkeypatch.setattr(ws, "workflow", fake)
        return fake

    return _make


def _input(**kw: Any) -> InvestorSequenceInput:
    return InvestorSequenceInput(
        sequence_id=SEQ, investor_id="inv", contact_email="jane@acme.com", market="Dallas", **kw
    )


def _names(fake: FakeWorkflowModule) -> list[str]:
    return [c.name for c in fake.calls]


def _awaiting(step: int) -> Callable[[InvestorSequenceWorkflow], bool]:
    return lambda wf: wf._state == ws.STATE_AWAITING_HUMAN and wf._step == step


# -- happy paths -----------------------------------------------------------------------------------


async def test_l2_auto_sends_four_steps_with_three_four_day_sleeps(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    result = await fake.wf.run(_input())
    assert result.completed and result.cancel_reason is None
    assert result.steps_sent == [0, 1, 2, 3]
    assert result.decisions == {"0": "send", "1": "send", "2": "send", "3": "send"}
    assert fake.slept == [4 * DAY, 4 * DAY, 4 * DAY]
    assert _names(fake).count("prepare_step") == 4
    assert _names(fake).count("send_approved") == 0
    per_step = [
        "check_cancel_conditions",
        "send_window",
        "prepare_step",
        "decide_and_send",
        "update_sequence",
    ]
    assert _names(fake) == per_step * 4 + ["update_sequence"]
    keys = [c.arg for c in fake.calls if c.name == "prepare_step"]
    assert [(k.sequence_id, k.step, k.contact_email, k.market) for k in keys] == [
        (SEQ, s, "jane@acme.com", "Dallas") for s in range(4)
    ]
    updates = [c.arg for c in fake.calls if c.name == "update_sequence"]
    assert [u.step_done for u in updates] == [0, 1, 2, 3, None]
    assert [u.next_send_in_seconds for u in updates[:4]] == [4 * DAY, 4 * DAY, 4 * DAY, None]
    assert updates[-1].completed is True
    assert fake.wf.status()["state"] == ws.STATE_DONE


async def test_custom_intervals_from_config_are_honoured(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    result = await fake.wf.run(_input(intervals_days=[3, 7]))
    assert result.steps_sent == [0, 1, 2]
    assert fake.slept == [3 * DAY, 7 * DAY]


async def test_scripted_human_approvals_send_each_queued_step(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(decide_and_send=_decide("queue_for_human", "draft at L0: human sends"))
    for step in range(4):
        fake.events.append(
            (
                _awaiting(step),
                lambda step=step: fake.wf.human_decision(  # type: ignore[misc]
                    HumanDecision(step=step, approved=True, edited_body=None, actor_id="aaron")
                ),
            )
        )
    result = await fake.wf.run(_input())
    assert result.completed and result.steps_sent == [0, 1, 2, 3]
    assert result.decisions == {str(s): "send" for s in range(4)}
    approvals = [c.arg for c in fake.calls if c.name == "send_approved"]
    assert [(a.message_id, a.step, a.actor_id) for a in approvals] == [
        (f"msg-{s}", s, "aaron") for s in range(4)
    ]
    assert fake.slept == [4 * DAY] * 3


async def test_status_while_awaiting_human_names_the_pending_step(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(decide_and_send=_decide("queue_for_human"))
    seen: list[dict[str, Any]] = []

    def _capture() -> None:
        seen.append(fake.wf.status())
        fake.wf.human_decision(HumanDecision(step=0, approved=False))

    fake.events.append((_awaiting(0), _capture))
    await fake.wf.run(_input())
    assert seen[0]["state"] == "awaiting_human" and seen[0]["pending_human_step"] == 0
    assert seen[0]["decisions"] == {"0": "queue_for_human"}


# -- cancellation (INV-10, INV-23, WF-21) ----------------------------------------------------------


async def test_reply_signal_during_sleep_after_step_1_cancels_steps_2_and_3(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.events.append((lambda wf: wf._steps_sent == [0, 1], fake.wf.reply))
    result = await fake.wf.run(_input())
    assert not result.completed and result.cancel_reason == "reply"
    assert result.steps_sent == [0, 1]
    assert _names(fake).count("prepare_step") == 2
    assert fake.calls[-1].name == "update_sequence"
    assert fake.calls[-1].arg.cancel_reason == "reply"
    assert fake.wf.status()["state"] == ws.STATE_CANCELLED
    assert fake.slept == [4 * DAY]  # the second sleep was interrupted by the signal


@pytest.mark.parametrize("signal", ["opt_out", "bounce"])
async def test_opt_out_and_bounce_cancel_while_awaiting_human(
    sim: Callable[..., FakeWorkflowModule], signal: str
) -> None:
    fake = sim(decide_and_send=_decide("queue_for_human"))
    fake.events.append((_awaiting(0), getattr(fake.wf, signal)))
    result = await fake.wf.run(_input())
    assert result.cancel_reason == signal and result.steps_sent == []
    assert "send_approved" not in _names(fake)


async def test_thread_reply_found_by_check_cancels_before_preparing(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    calls = {"n": 0}

    def _check(_: Any) -> acts.CheckCancelResult:
        calls["n"] += 1
        return acts.CheckCancelResult(
            cancel_reason="reply" if calls["n"] == 2 else None, paused=False
        )

    fake = sim(check_cancel_conditions=_check)
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "reply" and result.steps_sent == [0]
    assert _names(fake).count("prepare_step") == 1


async def test_suppression_found_by_check_cancels(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(
        check_cancel_conditions=lambda _: acts.CheckCancelResult(
            cancel_reason="suppressed", paused=False
        )
    )
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "suppressed" and result.steps_sent == []
    assert _names(fake) == ["check_cancel_conditions", "update_sequence"]


async def test_blocked_decision_cancels_with_reason(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(decide_and_send=_decide("blocked", "recipient is suppressed"))
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "blocked:recipient is suppressed"
    assert result.decisions == {"0": "blocked"} and result.steps_sent == []


async def test_human_rejection_cancels(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(decide_and_send=_decide("queue_for_human"))
    fake.events.append(
        (_awaiting(1), lambda: fake.wf.human_decision(HumanDecision(step=1, approved=False)))
    )
    fake.events.append(
        (_awaiting(0), lambda: fake.wf.human_decision(HumanDecision(step=0, approved=True)))
    )
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "human_rejected:step:1" and result.steps_sent == [0]


async def test_approved_but_suppressed_at_send_time_cancels(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(
        decide_and_send=_decide("queue_for_human"),
        send_approved=lambda i: acts.SendApprovedResult(
            message_id=i.message_id, sent=False, body_edited=False, blocked_reason="suppressed"
        ),
    )
    fake.events.append(
        (_awaiting(0), lambda: fake.wf.human_decision(HumanDecision(step=0, approved=True)))
    )
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "blocked:suppressed" and result.steps_sent == []


# -- pause / resume and the send window ------------------------------------------------------------


async def test_pause_during_sleep_preserves_remaining_time(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    # Pause as soon as the first sleep starts; the fake wait returns immediately on pause, so the
    # elapsed time is 0 and the full interval remains. Resume on the next wait.
    fake.events.append(
        (lambda wf: wf._state == ws.STATE_SLEEPING and not wf._paused, fake.wf.pause)
    )
    fake.events.append((lambda wf: wf._paused, fake.wf.resume))
    result = await fake.wf.run(_input())
    assert result.completed
    assert fake.slept == [4 * DAY, 4 * DAY, 4 * DAY]


async def test_db_pause_holds_until_resume_signal(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(
        check_cancel_conditions=lambda _: acts.CheckCancelResult(cancel_reason=None, paused=True)
    )
    states: list[str] = []

    def _resume() -> None:
        states.append(fake.wf._state)
        fake.wf.resume()
        fake.events.append((lambda wf: wf._paused, _resume))  # every step re-pauses

    fake.events.append((lambda wf: wf._paused, _resume))
    result = await fake.wf.run(_input(intervals_days=[4]))
    assert result.completed and result.steps_sent == [0, 1]
    assert states and states[0] == ws.STATE_PAUSED


async def test_send_window_closed_sleeps_until_it_opens(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(send_window=lambda _: acts.SendWindowResult(3600.0, "America/Phoenix", 9, 17))
    result = await fake.wf.run(_input(intervals_days=[4]))
    assert result.completed
    assert fake.slept == [3600.0, 4 * DAY, 3600.0]
    decided = [c.arg for c in fake.calls if c.name == "decide_and_send"]
    assert all(d.timezone == "America/Phoenix" for d in decided)


async def test_send_window_can_be_disabled(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(send_window=lambda _: acts.SendWindowResult(3600.0, "UTC", 9, 17))
    await fake.wf.run(_input(intervals_days=[4], honor_send_window=False))
    assert "send_window" not in _names(fake)
    assert fake.slept == [4 * DAY]


async def test_cancel_signal_while_waiting_for_window(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(send_window=lambda _: acts.SendWindowResult(7200.0, "UTC", 9, 17))
    fake.events.append((lambda wf: wf._state == ws.STATE_WAITING_WINDOW, fake.wf.opt_out))
    result = await fake.wf.run(_input())
    assert result.cancel_reason == "opt_out" and result.steps_sent == []
    assert "prepare_step" not in _names(fake)
