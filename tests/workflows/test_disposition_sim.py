"""DispositionWorkflow run-loop simulation without a Temporal server (WF-06).

Same approach as ``test_investor_sequence_sim.py``: a fake ``workflow`` module scripts the
activities by name, ``wait_condition`` delivers signals at chosen moments and advances a fake
clock on timeout. Checks the control flow deterministically: blast -> buyer responses -> buyer
selected -> deposit -> deadline alerts at T-3d / T-1d / T-0 -> closed; the deal-dead path
cancels the timers and runs the follow-up; review-queue approvals send queued blast drafts;
an unsigned contract never blasts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from wholesaling.activities import disposition as acts
from wholesaling.signals import (
    BuyerResponseSignal,
    BuyerSelectedSignal,
    DealClosedSignal,
    DealDeadSignal,
    DepositReceivedSignal,
)
from wholesaling.workflows import disposition as ws
from wholesaling.workflows.disposition import (
    BlastHumanDecision,
    DispositionInput,
    DispositionWorkflow,
)

CONTRACT = "22222222-2222-2222-2222-222222222222"
START = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
CLOSING = START + timedelta(days=10)
INSPECTION = START + timedelta(days=5)
DAY = 86_400.0


@dataclass
class Call:
    name: str
    arg: Any


@dataclass
class FakeWorkflowModule:
    wf: DispositionWorkflow
    handlers: dict[str, Callable[[Any], Any]]
    events: list[tuple[Callable[[DispositionWorkflow], bool], Callable[[], None]]] = field(
        default_factory=list
    )
    calls: list[Call] = field(default_factory=list)
    clock: datetime = START
    slept: list[float] = field(default_factory=list)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("sim"))

    class unsafe:
        @staticmethod
        def imports_passed_through() -> Any:  # pragma: no cover
            raise AssertionError("not used after import")

    def now(self) -> datetime:
        return self.clock

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        self.calls.append(Call(fn.__name__, arg))
        return self.handlers[fn.__name__](arg)

    def _fire(self) -> None:
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
        self._fire()
        if pred():
            return
        if timeout is None:
            msg = f"workflow would wait forever (state={self.wf._state})"
            raise RuntimeError(msg)
        self.clock += timeout
        self.slept.append(timeout.total_seconds())
        self._fire()
        if pred():
            return
        raise TimeoutError


def _snapshot(**over: Any) -> Callable[[Any], acts.ContractSnapshot]:
    def _h(i: acts.LoadContractInput) -> acts.ContractSnapshot:
        base: dict[str, Any] = {
            "contract_id": i.contract_id,
            "status": "signed",
            "signed_at": START.isoformat(),
            "option_period_end": None,
            "inspection_deadline": INSPECTION.isoformat(),
            "closing_date": CLOSING.isoformat(),
            "buyer_investor_id": None,
            "deposit_received_at": None,
            "earnest_money_status": "none",
            "deal_dead_reason": None,
            "closed_at": None,
            "raised_alerts": [],
        }
        base.update(over)
        return acts.ContractSnapshot(**base)

    return _h


def _blast(queued: int = 0, sent: int = 2) -> Callable[[Any], acts.BlastResult]:
    def _h(i: acts.RunBlastInput) -> acts.BlastResult:
        recipients = [
            acts.BlastRecipient(
                investor_id=f"inv-{n}",
                email=f"b{n}@x.com",
                tier=1,
                message_id=f"msg-{n}",
                decision="send",
                reason="auto",
                qa_verdict="approve",
                sent=True,
                created=True,
            )
            for n in range(sent)
        ] + [
            acts.BlastRecipient(
                investor_id=f"inv-q{n}",
                email=f"q{n}@x.com",
                tier=2,
                message_id=f"msg-q{n}",
                decision="queue_for_human",
                reason="draft at L0",
                qa_verdict="approve",
                sent=False,
                created=True,
            )
            for n in range(queued)
        ]
        return acts.BlastResult(
            contract_id=i.contract_id,
            recipients=recipients,
            skipped_suppressed=[],
            level="L1",
            deal_approved=True,
        )

    return _h


def _alert(i: acts.DeadlineAlertInput) -> acts.DeadlineAlertResult:
    return acts.DeadlineAlertResult(created=True, notified=["log"])


def _state(_: Any) -> bool:
    return True


def _dead(i: acts.DealDeadFollowupInput) -> acts.DealDeadFollowupResult:
    return acts.DealDeadFollowupResult(
        created=True, earnest_money_status="deposited", release_reminder=True
    )


def _approved(i: acts.SendApprovedBlastInput) -> acts.SendApprovedBlastResult:
    return acts.SendApprovedBlastResult(message_id=i.message_id, sent=True)


def _handlers(**over: Callable[[Any], Any]) -> dict[str, Callable[[Any], Any]]:
    base: dict[str, Callable[[Any], Any]] = {
        "load_contract": _snapshot(),
        "run_blast": _blast(),
        "raise_deadline_alert": _alert,
        "record_workflow_state": _state,
        "deal_dead_followup": _dead,
        "send_approved_blast": _approved,
    }
    base.update(over)
    return base


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWorkflowModule]:
    def _make(**over: Callable[[Any], Any]) -> FakeWorkflowModule:
        wf = DispositionWorkflow()
        fake = FakeWorkflowModule(wf=wf, handlers=_handlers(**over))
        monkeypatch.setattr(ws, "workflow", fake)
        return fake

    return _make


def _names(fake: FakeWorkflowModule) -> list[str]:
    return [c.name for c in fake.calls]


def _alerts(fake: FakeWorkflowModule) -> list[str]:
    return [c.arg.alert_key for c in fake.calls if c.name == "raise_deadline_alert"]


def _at(when: datetime) -> Callable[[DispositionWorkflow], bool]:
    """Fire once the fake clock has reached ``when`` - checked at every wait boundary."""
    return lambda wf: _CLOCK[0] is not None and _CLOCK[0]() >= when


_CLOCK: list[Callable[[], datetime] | None] = [None]


@pytest.fixture(autouse=True)
def _bind_clock(sim: Callable[..., FakeWorkflowModule]) -> None:
    _CLOCK[0] = None


def _arm(fake: FakeWorkflowModule) -> None:
    _CLOCK[0] = fake.now


def _after_alert(key: str) -> Callable[[DispositionWorkflow], bool]:
    """Fire once the workflow has raised ``key`` (the human closes after the T-0 alert)."""
    return lambda wf: key in wf._raised


# -- happy path ------------------------------------------------------------------------------------


async def test_full_path_blast_responses_selection_deposit_alerts_closed(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    _arm(fake)
    wf = fake.wf
    # Day 1: two buyers answer; day 2: human selects one; day 3: deposit; closing day: human closes.
    fake.events += [
        (
            _at(START + timedelta(days=1)),
            lambda: wf.buyer_response(BuyerResponseSignal("inv-0", "interested", True, "5k/24h")),
        ),
        (
            _at(START + timedelta(days=1)),
            lambda: wf.buyer_response(BuyerResponseSignal("inv-1", "pass")),
        ),
        (
            _at(START + timedelta(days=2)),
            lambda: wf.buyer_selected(BuyerSelectedSignal("inv-0", "aaron")),
        ),
        (
            _at(START + timedelta(days=3)),
            lambda: wf.deposit_received(DepositReceivedSignal("5000", START.isoformat(), "aaron")),
        ),
        (
            _after_alert("closing_date:T-0"),
            lambda: wf.deal_closed(DealClosedSignal("15000", CLOSING.isoformat(), "aaron")),
        ),
    ]
    result = await wf.run(DispositionInput(contract_id=CONTRACT))
    assert result.outcome == "closed" and result.blast_sent == 2 and result.blast_queued == 0
    assert result.buyer_investor_id == "inv-0"
    # Every alert fired exactly once, in order: inspection T-3d (day 2), T-1d (day 4), T-0 (day 5),
    # closing T-3d (day 7), T-1d (day 9), T-0 (day 10).
    assert _alerts(fake) == [
        "inspection_deadline:T-3d",
        "inspection_deadline:T-1d",
        "inspection_deadline:T-0",
        "closing_date:T-3d",
        "closing_date:T-1d",
        "closing_date:T-0",
    ]
    assert result.alerts_raised == sorted(_alerts(fake))
    assert _names(fake)[:2] == ["load_contract", "run_blast"]
    assert _names(fake).count("deal_dead_followup") == 0
    status = wf.status()
    assert status["state"] == ws.STATE_CLOSED and status["closed"] is True
    assert status["responses"] == {"inv-0": "interested", "inv-1": "pass"}
    assert status["deposit_received"] is True and status["buyer_investor_id"] == "inv-0"
    assert status["last_signal"]["signal"] == "deal_closed"
    alert_args = [c.arg for c in fake.calls if c.name == "raise_deadline_alert"]
    assert all(a.contract_id == CONTRACT for a in alert_args)
    assert alert_args[0].deadline_at == INSPECTION.isoformat() and alert_args[0].label == "T-3d"


async def test_alert_timers_fire_at_t3_t1_t0(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(load_contract=_snapshot(inspection_deadline=None))
    _arm(fake)
    fake.events.append(
        (_after_alert("closing_date:T-0"), lambda: fake.wf.deal_closed(DealClosedSignal("1", "x")))
    )
    await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    # Sleeps: to T-3d (7 days), T-1d (2 days), T-0 (1 day).
    assert fake.slept == [7 * DAY, 2 * DAY, 1 * DAY]
    assert _alerts(fake) == ["closing_date:T-3d", "closing_date:T-1d", "closing_date:T-0"]


async def test_already_raised_alerts_are_not_repeated_after_restart(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    """Replay / restart safety: the snapshot carries the alerts already in the audit log."""
    fake = sim(
        load_contract=_snapshot(inspection_deadline=None, raised_alerts=["closing_date:T-3d"])
    )
    _arm(fake)
    fake.clock = CLOSING - timedelta(days=2)  # restarted after T-3d passed
    fake.events.append(
        (_after_alert("closing_date:T-0"), lambda: fake.wf.deal_closed(DealClosedSignal("1", "x")))
    )
    await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    assert _alerts(fake) == ["closing_date:T-1d", "closing_date:T-0"]


async def test_overdue_alert_fires_immediately_once(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(load_contract=_snapshot(inspection_deadline=None))
    _arm(fake)
    fake.clock = CLOSING - timedelta(days=2, hours=1)  # T-3d is already in the past
    fake.events.append(
        (_after_alert("closing_date:T-0"), lambda: fake.wf.deal_closed(DealClosedSignal("1", "x")))
    )
    await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    assert _alerts(fake) == ["closing_date:T-3d", "closing_date:T-1d", "closing_date:T-0"]


# -- deal dead -------------------------------------------------------------------------------------


async def test_deal_dead_cancels_timers_and_runs_followup(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    _arm(fake)
    fake.events.append(
        (
            _at(START + timedelta(days=1)),
            lambda: fake.wf.deal_dead(DealDeadSignal("failed inspection", "aaron")),
        )
    )
    result = await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    assert result.outcome == "dead" and result.reason == "failed inspection"
    assert _alerts(fake) == []  # first timer was inspection T-3d at day 2; dead on day 1
    followups = [c.arg for c in fake.calls if c.name == "deal_dead_followup"]
    assert len(followups) == 1 and followups[0].reason == "failed inspection"
    assert (
        fake.wf.status()["state"] == ws.STATE_DEAD
        and fake.wf.status()["dead_reason"] == "failed inspection"
    )
    # Later signals are ignored once terminal.
    fake.wf.deal_closed(DealClosedSignal("1", "x"))
    assert fake.wf.status()["closed"] is False


async def test_second_terminal_signal_is_ignored(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim()
    _arm(fake)
    fake.events.append(
        (
            _at(START + timedelta(days=1)),
            lambda: fake.wf.deal_closed(DealClosedSignal("1", CLOSING.isoformat())),
        )
    )
    fake.events.append(
        (_at(START + timedelta(days=1)), lambda: fake.wf.deal_dead(DealDeadSignal("late")))
    )
    result = await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    assert result.outcome == "closed"


# -- deadlines updated -----------------------------------------------------------------------------


async def test_deadlines_updated_reloads_and_rearms(sim: Callable[..., FakeWorkflowModule]) -> None:
    moved = CLOSING + timedelta(days=5)
    snapshots = [
        _snapshot(inspection_deadline=None),
        _snapshot(inspection_deadline=None, closing_date=moved.isoformat()),
    ]

    def _load(i: acts.LoadContractInput) -> acts.ContractSnapshot:
        return snapshots.pop(0)(i) if len(snapshots) > 1 else snapshots[0](i)

    fake = sim(load_contract=_load)
    _arm(fake)
    fake.events.append((_at(START + timedelta(days=1)), lambda: fake.wf.deadlines_updated()))
    fake.events.append(
        (_after_alert("closing_date:T-0"), lambda: fake.wf.deal_closed(DealClosedSignal("1", "x")))
    )
    await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    assert _names(fake).count("load_contract") == 2
    assert _alerts(fake) == ["closing_date:T-3d", "closing_date:T-1d", "closing_date:T-0"]
    assert fake.calls[-1].name == "record_workflow_state"
    t3 = next(c.arg for c in fake.calls if c.name == "raise_deadline_alert")
    assert t3.deadline_at == moved.isoformat()


# -- review queue approvals ------------------------------------------------------------------------


async def test_human_decision_sends_queued_blast_drafts(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(run_blast=_blast(queued=2, sent=0))
    _arm(fake)
    wf = fake.wf
    fake.events += [
        (
            _at(START + timedelta(days=1)),
            lambda: wf.human_decision(
                BlastHumanDecision(
                    step=None,
                    approved=True,
                    actor_id="aaron",
                    message_id="msg-q0",
                    edited_body="tighter",
                )
            ),
        ),
        (
            _at(START + timedelta(days=1)),
            lambda: wf.human_decision(
                BlastHumanDecision(step=None, approved=False, actor_id="aaron", message_id="msg-q1")
            ),
        ),
        (
            _at(START + timedelta(days=1)),
            lambda: wf.human_decision(
                BlastHumanDecision(step=None, approved=True, message_id="unknown")
            ),
        ),
        (_after_alert("closing_date:T-0"), lambda: wf.deal_closed(DealClosedSignal("1", "x"))),
    ]
    result = await wf.run(DispositionInput(contract_id=CONTRACT))
    sends = [c.arg for c in fake.calls if c.name == "send_approved_blast"]
    assert [s.message_id for s in sends] == ["msg-q0"] and sends[0].edited_body == "tighter"
    assert result.blast_sent == 1 and result.blast_queued == 0
    assert wf.status()["pending_human_messages"] == []


# -- guards ----------------------------------------------------------------------------------------


async def test_unsigned_contract_never_blasts(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(load_contract=_snapshot(status="requested"))
    result = await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    assert result.outcome == "not_signed" and "requested" in (result.reason or "")
    assert "run_blast" not in _names(fake)
    assert fake.wf.status()["state"] == ws.STATE_NOT_SIGNED


async def test_no_deadlines_waits_for_a_signal_only(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(load_contract=_snapshot(inspection_deadline=None, closing_date=None))
    _arm(fake)
    with pytest.raises(RuntimeError, match="wait forever"):
        await fake.wf.run(DispositionInput(contract_id=CONTRACT))
    assert (
        fake.wf.status()["next_alert_key"] is None
        and fake.wf.status()["state"] == ws.STATE_MARKETING
    )
