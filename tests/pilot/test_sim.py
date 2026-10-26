"""The discrete-event workflow simulator and its Temporal-shaped gateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from wholesaling.activities.conversation import BounceSignal, OptOutSignal, ReplySignal
from wholesaling.db.repositories.review import HumanDecision as ReviewDecision
from wholesaling.pilot.sim import (
    SimTemporalGateway,
    WorkflowSimulator,
    patched_workflow_modules,
)
from wholesaling.workflows import graph_maintenance as gm
from wholesaling.workflows import investor_sequence as ws
from wholesaling.workflows.investor_sequence import HumanDecision as SequenceDecision
from wholesaling.workflows.investor_sequence import InvestorSequenceWorkflow

T0 = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


class _Sleeper:
    """A tiny workflow: sleep, then wait for a flag, recording what it saw."""

    def __init__(self, sim: WorkflowSimulator, sleep: timedelta) -> None:
        self.sim = sim
        self.sleep = sleep
        self.flag = False
        self.woke_at: datetime | None = None
        self.flagged_at: datetime | None = None

    def set_flag(self) -> None:
        self.flag = True

    async def run(self) -> str:
        with pytest.raises(TimeoutError):
            await self.sim.wait_condition(lambda: False, timeout=self.sleep)
        self.woke_at = self.sim.clock
        await self.sim.wait_condition(lambda: self.flag)
        self.flagged_at = self.sim.clock
        return "done"


async def test_clock_advances_to_the_earliest_deadline_and_signals_wake_waiters() -> None:
    sim = WorkflowSimulator(T0)
    a = _Sleeper(sim, timedelta(hours=4))
    b = _Sleeper(sim, timedelta(hours=1))
    sim.start_workflow("a", a, a.run())
    sim.start_workflow("b", b, b.run())
    sim.schedule(T0 + timedelta(hours=2), _flag(b), label="flag b")
    sim.schedule(T0 + timedelta(hours=6), _flag(a), label="flag a")
    await sim.run()
    assert (b.woke_at, b.flagged_at) == (T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    assert (a.woke_at, a.flagged_at) == (T0 + timedelta(hours=4), T0 + timedelta(hours=6))
    assert sim.results == {"a": "done", "b": "done"}
    assert sim.clock == T0 + timedelta(hours=6) and not sim.parked


def _flag(w: _Sleeper) -> Any:
    async def _f() -> None:
        w.set_flag()

    return _f


async def test_run_stops_when_only_untimed_waiters_remain_and_reports_them() -> None:
    sim = WorkflowSimulator(T0)
    a = _Sleeper(sim, timedelta(minutes=1))
    sim.start_workflow("a", a, a.run())
    await sim.run()
    assert sim.parked_without_timeout == ["a"] and "a" not in sim.results


async def test_duplicate_workflow_id_is_a_no_op_and_errors_are_recorded() -> None:
    sim = WorkflowSimulator(T0)

    async def boom() -> None:
        raise ValueError("nope")

    assert sim.start_workflow("x", object(), boom())
    second = boom()
    assert not sim.start_workflow("x", object(), second)
    second.close()  # Temporal would reject the duplicate start; nothing to run
    await sim.run()
    assert isinstance(sim.errors["x"], ValueError)


async def test_on_idle_runs_before_time_advances() -> None:
    sim = WorkflowSimulator(T0)
    a = _Sleeper(sim, timedelta(hours=1))
    sim.start_workflow("a", a, a.run())
    seen: list[datetime] = []

    async def on_idle() -> bool:
        seen.append(sim.clock)
        if a.woke_at is not None and not a.flag:
            a.set_flag()
            return True
        return False

    await sim.run(on_idle=on_idle)
    assert a.flagged_at == T0 + timedelta(hours=1)
    assert seen[0] == T0


async def test_execute_activity_runs_the_function_and_notifies_observers() -> None:
    sim = WorkflowSimulator(T0)
    calls: list[str] = []

    async def act(arg: int) -> int:
        return arg * 2

    async def observer(call: Any) -> None:
        calls.append(f"{call.workflow_id}:{call.name}:{call.result}")

    sim.observers.append(observer)

    class Wf:
        async def run(self) -> int:
            return int(await sim.execute_activity(act, 21))

    wf = Wf()
    sim.start_workflow("w", wf, wf.run())
    await sim.run()
    assert sim.results["w"] == 42 and calls == ["w:act:42"]


# -- gateway ------------------------------------------------------------------------------------


@dataclass
class _Target:
    decisions: list[Any]

    def human_decision(self, decision: SequenceDecision) -> None:
        self.decisions.append(decision)

    def reply(self) -> None:
        self.decisions.append("reply")

    async def status(self) -> dict[str, int]:
        return {"n": len(self.decisions)}


async def test_signal_converts_dataclass_payload_by_field_name_like_temporal() -> None:
    sim = WorkflowSimulator(T0)
    target = _Target([])
    sim.instances["t"] = target
    gw = SimTemporalGateway(sim)
    await gw.signal(
        "t",
        "human_decision",
        ReviewDecision(approved=True, edited_body=None, step=2, message_id="m", actor_id="op"),
    )
    got = target.decisions[0]
    assert isinstance(got, SequenceDecision)
    assert (got.step, got.approved, got.actor_id, got.message_id) == (2, True, "op", "m")
    assert await gw.query("t", "status") == {"n": 1}
    assert gw.calls[0].method == "signal"


async def test_signal_with_a_payload_the_handler_cannot_take_is_recorded_as_a_contract_error() -> (
    None
):
    """Regression guard for GAP-01: a handler that takes no payload gets the signal re-delivered
    bare and the mismatch is recorded (Temporal would fail the workflow task)."""
    sim = WorkflowSimulator(T0)
    target = _Target([])
    sim.instances["t"] = target
    gw = SimTemporalGateway(sim)
    await gw.signal("t", "reply", ReplySignal(message_id="m", classification="interested"))
    assert target.decisions == ["reply"]
    assert len(sim.signal_contract_errors) == 1
    err = sim.signal_contract_errors[0]
    assert err.signal == "reply" and err.payload_types == ("ReplySignal",)
    assert err.handler_params == 0 and "positional argument" in err.error


async def test_signal_to_unknown_or_finished_workflow_raises() -> None:
    sim = WorkflowSimulator(T0)
    gw = SimTemporalGateway(sim)
    with pytest.raises(LookupError):
        await gw.signal("nope", "reply")
    sim.instances["done"] = _Target([])
    sim.results["done"] = "x"
    with pytest.raises(RuntimeError):
        await gw.signal("done", "reply")


def test_sequence_signal_handlers_accept_the_inbound_pipelines_payloads() -> None:
    """GAP-01 fixed: the handlers take the payloads ``activities.conversation`` sends (the same
    dataclasses, from ``wholesaling.signals``) and still accept no payload at all."""
    wf = InvestorSequenceWorkflow()
    wf.reply(ReplySignal(message_id="m", classification="interested"))
    assert wf.status()["cancel_reason"] == "reply"
    assert wf.status()["last_signal"] == {
        "signal": "reply",
        "message_id": "m",
        "classification": "interested",
    }
    wf.opt_out(OptOutSignal(message_id="m2"))  # at-least-once: a later signal is a no-op
    assert wf.status()["cancel_reason"] == "reply"
    bare = InvestorSequenceWorkflow()
    bare.bounce()
    assert bare.status()["cancel_reason"] == "bounce" and bare.status()["last_signal"] == {
        "signal": "bounce"
    }
    wf3 = InvestorSequenceWorkflow()
    wf3.bounce(BounceSignal(message_id="m3", bounced_address="x@y.z"))
    assert wf3.status()["last_signal"]["bounced_address"] == "x@y.z"


async def test_gateway_delivers_the_sequence_payloads_without_contract_errors() -> None:
    """End to end through the simulated gateway: dataclass payload -> ``X | None`` handler."""
    sim = WorkflowSimulator(T0)
    wf = InvestorSequenceWorkflow()
    sim.instances["seq"] = wf
    gw = SimTemporalGateway(sim)
    await gw.signal("seq", "reply", ReplySignal(message_id="m", classification="question"))
    assert sim.signal_contract_errors == []
    assert wf.status()["last_signal"]["classification"] == "question"
    # A payload shaped like the sender's dataclass but of another class maps by field name.
    other = InvestorSequenceWorkflow()
    sim.instances["seq2"] = other
    await gw.signal("seq2", "bounce", {"message_id": "m", "bounced_address": "a@b.c"})
    assert other.status()["last_signal"]["bounced_address"] == "a@b.c"
    assert sim.signal_contract_errors == []


async def test_patched_workflow_modules_swaps_and_restores() -> None:
    sim = WorkflowSimulator(T0)
    before = (ws.workflow, gm.workflow)
    async with patched_workflow_modules(sim) as shim:
        assert ws.workflow is shim and gm.workflow is shim
        assert shim.now() == T0
    assert (ws.workflow, gm.workflow) == before
