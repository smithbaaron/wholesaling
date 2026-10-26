"""NegotiationWorkflow (S5) control flow without a Temporal server: reminders 2/5/10, expiry at
14, re-engage 60/90; counters inside / above the room; accept; decline + 90-day requeue;
human override."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from wholesaling.activities import offers as acts
from wholesaling.workflows import negotiation as wn
from wholesaling.workflows.negotiation import (
    AcceptSignal,
    CounterSignal,
    DeclineSignal,
    HumanOverride,
    NegotiationInput,
    NegotiationWorkflow,
)

OFFER = "offer-1"
LEAD = "lead-1"
DAY = 86_400.0
SCHEDULE = {
    "reminder_days": [2, 5, 10],
    "expire_day": 14,
    "reengage_days": [60, 90],
    "decline_requeue_days": 90,
}


@dataclass
class Call:
    name: str
    arg: Any


@dataclass
class Info:
    workflow_id: str = f"negotiation:{OFFER}"


@dataclass
class FakeWorkflowModule:
    wf: NegotiationWorkflow
    handlers: dict[str, Callable[[Any], Any]]
    # (fire at elapsed seconds, action); delivered during a wait once the clock passes the moment.
    timed: list[tuple[float, Callable[[], None]]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    start: datetime = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
    clock: datetime = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
    waits: list[float] = field(default_factory=list)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("sim"))

    def info(self) -> Info:
        return Info()

    def now(self) -> datetime:
        return self.clock

    @property
    def elapsed(self) -> float:
        return (self.clock - self.start).total_seconds()

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        self.calls.append(Call(fn.__name__, arg))
        return self.handlers[fn.__name__](arg)

    async def wait_condition(
        self,
        pred: Callable[[], bool],
        timeout: timedelta | None = None,  # noqa: ASYNC109 - mirrors workflow.wait_condition
    ) -> None:
        if pred():
            return
        if timeout is None:
            # An indefinite wait (awaiting a human): the next scheduled signal resolves it.
            if not self.timed:
                msg = f"workflow would wait forever (state={self.wf._state})"
                raise RuntimeError(msg)
            deadline = max(t for t, _ in self.timed)
        else:
            deadline = self.elapsed + timeout.total_seconds()
        due = sorted((t, a) for t, a in self.timed if t <= deadline)
        if due:
            at, action = due[0]
            self.timed.remove((at, action))
            self.clock = self.start + timedelta(seconds=at)
            action()
            if pred():
                return
            remaining = None if timeout is None else timedelta(seconds=deadline - self.elapsed)
            return await self.wait_condition(pred, timeout=remaining)
        if timeout is None:  # pragma: no cover - guarded above
            raise RuntimeError("no signal resolved the wait")
        self.clock = self.start + timedelta(seconds=deadline)
        self.waits.append(timeout.total_seconds())
        raise TimeoutError


def _terms(status: str = "sent") -> Callable[[Any], acts.NegotiationTerms]:
    def _h(i: acts.LoadNegotiationInput) -> acts.NegotiationTerms:
        return acts.NegotiationTerms(
            i.offer_id,
            status,
            "88500",
            "95169.32",
            "2026-10-25T15:00:00+00:00",
            "pat@example.test",
            "1204 Oak Hollow Dr",
            dict(SCHEDULE),
        )

    return _h


def _message(i: acts.NegotiationMessageInput) -> acts.NegotiationMessageResult:
    return acts.NegotiationMessageResult(
        i.offer_id, i.kind, i.day, "send", "auto", True, f"msg-{i.kind}-{i.day}"
    )


def _expire(i: acts.ExpireOfferInput) -> acts.ExpireOfferResult:
    return acts.ExpireOfferResult(i.offer_id, "expired", True)


def _counter(level: str) -> Callable[[Any], acts.CounterDecision]:
    def _h(i: acts.EvaluateCounterInput) -> acts.CounterDecision:
        amount = float(i.amount)
        within = 88500 <= amount <= 95169.32
        accepted = within and level != "L0"
        decision = "send" if accepted else "queue_for_human"
        cls = "within_room" if within else "above_mao"
        return acts.CounterDecision(
            i.offer_id,
            i.amount,
            cls,
            within,
            decision,
            "scripted",
            level,
            accepted,
            "accepted" if accepted else "countered",
        )

    return _h


def _accept(i: acts.AcceptOfferInput) -> acts.AcceptOfferResult:
    return acts.AcceptOfferResult(i.offer_id, "accepted", i.amount or "88500")


def _decline(i: acts.DeclineOfferInput) -> acts.DeclineOfferResult:
    return acts.DeclineOfferResult(i.offer_id, "declined", "2027-01-23")


def _requeue(i: acts.RequeueInput) -> acts.RequeueResult:
    return acts.RequeueResult(i.seller_lead_id, "queued", True)


def _withdraw(i: acts.WithdrawOfferInput) -> acts.WithdrawOfferResult:
    return acts.WithdrawOfferResult(i.offer_id, "withdrawn")


def _handlers(**overrides: Callable[[Any], Any]) -> dict[str, Callable[[Any], Any]]:
    base: dict[str, Callable[[Any], Any]] = {
        "load_negotiation": _terms(),
        "send_negotiation_message": _message,
        "expire_offer": _expire,
        "evaluate_counter": _counter("L0"),
        "accept_offer": _accept,
        "decline_offer": _decline,
        "requeue_seller": _requeue,
        "withdraw_offer": _withdraw,
    }
    base.update(overrides)
    return base


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWorkflowModule]:
    def _make(**overrides: Callable[[Any], Any]) -> FakeWorkflowModule:
        wf = NegotiationWorkflow()
        fake = FakeWorkflowModule(wf=wf, handlers=_handlers(**overrides))
        monkeypatch.setattr(wn, "workflow", fake)
        return fake

    return _make


def _names(fake: FakeWorkflowModule) -> list[str]:
    return [c.name for c in fake.calls]


def _messages(fake: FakeWorkflowModule) -> list[tuple[str, int]]:
    return [(c.arg.kind, c.arg.day) for c in fake.calls if c.name == "send_negotiation_message"]


async def test_full_timeline_reminders_expiry_reengage(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert (
        result.outcome == "expired"
        and result.reminders_sent == [2, 5, 10]
        and result.reengagements_sent == [60, 90]
    )
    assert _messages(fake) == [
        ("reminder", 2),
        ("reminder", 5),
        ("reminder", 10),
        ("reengage", 60),
        ("reengage", 90),
    ]
    assert (
        "expire_offer" in _names(fake) and _names(fake).index("expire_offer") == 1 + 3
    )  # after load + 3 reminders
    assert fake.waits == [2 * DAY, 3 * DAY, 5 * DAY, 4 * DAY, 46 * DAY, 30 * DAY]
    assert fake.elapsed == 90 * DAY and fake.wf.status()["day"] == 90


async def test_accept_signal_on_day_3_requests_contract_and_stops(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.timed.append((3 * DAY, lambda: fake.wf.accept(AcceptSignal(message_id="in-1"))))
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert result.outcome == "accepted" and result.accepted_price == "88500"
    assert _messages(fake) == [("reminder", 2)]  # the day-5 reminder never goes out
    assert fake.calls[-1].name == "accept_offer" and fake.calls[-1].arg.source == "seller"
    assert fake.wf.status()["last_signal"] == {"signal": "accept", "message_id": "in-1"}


async def test_counter_within_room_at_l1_auto_accepts(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(evaluate_counter=_counter("L1"))
    fake.timed.append(
        (1 * DAY, lambda: fake.wf.counter(CounterSignal(amount="92000", message_id="in-2")))
    )
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert (
        result.outcome == "accepted" and result.accepted_price == "92000" and result.counters == 1
    )
    assert _messages(fake) == [] and _names(fake) == ["load_negotiation", "evaluate_counter"]
    assert fake.wf.status()["counters"][0]["within_room"] is True


async def test_counter_above_mao_waits_for_human_who_accepts(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(evaluate_counter=_counter("L1"))
    fake.timed.append((1 * DAY, lambda: fake.wf.counter(CounterSignal(amount="99000"))))
    # The human (not the timeline) resolves it: override arrives while awaiting.
    fake.timed.append(
        (
            1.5 * DAY,
            lambda: fake.wf.human_override(
                HumanOverride(action="accept", amount="97000", actor_id="aaron")
            ),
        )
    )
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert result.outcome == "accepted" and result.accepted_price == "97000"
    accept = fake.calls[-1]
    assert (
        accept.name == "accept_offer"
        and accept.arg.source == "human"
        and accept.arg.actor_id == "aaron"
    )
    assert fake.wf.status()["counters"][0]["classification"] == "above_mao"


async def test_counter_at_l0_waits_for_human_who_declines(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.timed.append((1 * DAY, lambda: fake.wf.counter(CounterSignal(amount="92000"))))
    fake.timed.append(
        (
            2 * DAY,
            lambda: fake.wf.human_override(
                HumanOverride(action="decline", reason="seller wants more", actor_id="aaron")
            ),
        )
    )
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert result.outcome == "declined" and result.decline_reason == "seller wants more"
    assert result.requeued and fake.waits[-1] == 90 * DAY  # the SEL-09 requeue timer
    assert fake.calls[-1].name == "requeue_seller"


async def test_decline_signal_captures_reason_and_requeues_after_90_days(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.timed.append((6 * DAY, lambda: fake.wf.decline(DeclineSignal(reason="not selling"))))
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert (
        result.outcome == "declined" and result.decline_reason == "not selling" and result.requeued
    )
    assert _messages(fake) == [("reminder", 2), ("reminder", 5)]
    decline = next(c for c in fake.calls if c.name == "decline_offer")
    assert decline.arg.reason == "not selling"
    assert fake.elapsed == 96 * DAY  # 6 days + 90-day requeue timer


async def test_bare_decline_signal_defaults_its_reason(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.timed.append((1 * DAY, lambda: fake.wf.decline(None)))
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert result.outcome == "declined" and result.decline_reason == "declined"


async def test_counter_after_expiry_goes_to_human(sim: Callable[..., FakeWorkflowModule]) -> None:
    def _stale(i: acts.EvaluateCounterInput) -> acts.CounterDecision:
        return acts.CounterDecision(
            i.offer_id,
            i.amount,
            "within_room",
            True,
            "queue_for_human",
            "offer is expired; counter needs human review",
            "L1",
            False,
            "expired",
        )

    fake = sim(evaluate_counter=_stale)
    fake.timed.append((20 * DAY, lambda: fake.wf.counter(CounterSignal(amount="90000"))))
    fake.timed.append(
        (
            21 * DAY,
            lambda: fake.wf.human_override(
                HumanOverride(action="withdraw", reason="stale", actor_id="aaron")
            ),
        )
    )
    result = await fake.wf.run(NegotiationInput(OFFER, LEAD))
    assert result.outcome == "withdrawn" and "expire_offer" in _names(fake)
    assert fake.wf.status()["counters"][0]["reason"].startswith("offer is expired")


async def test_signals_after_the_outcome_are_ignored(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.timed.append((1 * DAY, lambda: fake.wf.accept()))
    await fake.wf.run(NegotiationInput(OFFER, LEAD))
    fake.wf.counter(CounterSignal(amount="1"))
    fake.wf.decline(None)
    assert fake.wf.status()["outcome"] == "accepted" and fake.wf.status()["last_signal"] == {
        "signal": "accept"
    }
