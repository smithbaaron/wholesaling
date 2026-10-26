"""OfferWorkflow control flow without a Temporal server (same fake-module harness as the investor
sequence sim): L0 queue -> human approve; L1 auto-send; Low confidence -> human; V5 -> blocked;
V6 change after approval -> re-queue."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from wholesaling.activities import offers as acts
from wholesaling.workflows import offer as wo
from wholesaling.workflows.offer import HumanDecision, OfferWorkflow, OfferWorkflowInput

INTAKE = "22222222-2222-2222-2222-222222222222"
LEAD = "33333333-3333-3333-3333-333333333333"
OFFER = "offer-1"


@dataclass
class Call:
    name: str
    arg: Any


@dataclass
class Info:
    workflow_id: str = f"offer:{INTAKE}"


@dataclass
class FakeWorkflowModule:
    wf: OfferWorkflow
    handlers: dict[str, Callable[[Any], Any]]
    events: list[tuple[Callable[[OfferWorkflow], bool], Callable[[], None]]] = field(
        default_factory=list
    )
    calls: list[Call] = field(default_factory=list)
    clock: datetime = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("sim"))

    def info(self) -> Info:
        return Info()

    def now(self) -> datetime:
        return self.clock

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        self.calls.append(Call(fn.__name__, arg))
        return self.handlers[fn.__name__](arg)

    def _fire(self) -> None:
        """Deliver at most one scheduled signal per wait (one review-queue action at a time)."""
        for idx, (pred, action) in enumerate(self.events):
            if pred(self.wf):
                del self.events[idx]
                action()
                return

    async def wait_condition(
        self,
        pred: Callable[[], bool],
        timeout: timedelta | None = None,  # noqa: ASYNC109 - mirrors workflow.wait_condition
    ) -> None:
        self._fire()
        if pred():
            return
        msg = f"workflow would wait forever (state={self.wf._state})"
        raise RuntimeError(msg)


def _computed(status: str = "ready", **kw: Any) -> Callable[[Any], acts.ComputeOfferResult]:
    def _h(i: acts.ComputeOfferInput) -> acts.ComputeOfferResult:
        base: dict[str, Any] = {
            "offer_id": OFFER,
            "status": status,
            "offer_status": "draft" if status != "blocked" else "blocked",
            "offer_price": "88500" if status != "blocked" else None,
            "arv": "240161",
            "arv_confidence": "High" if status == "ready" else "Low",
            "gates_passed": status == "ready",
            "gates_failed": [] if status == "ready" else ["arv_confidence"],
            "verifications": 5,
            "seller_email": "pat@example.test",
        }
        base.update(kw)
        return acts.ComputeOfferResult(**base)

    return _h


def _qa(i: acts.QaOfferInput) -> acts.QaOfferResult:
    return acts.QaOfferResult(i.offer_id, "approve", 0.95)


def _pre(
    changed: bool = False, gates: bool = True, blocked: str | None = None
) -> Callable[[Any], acts.PreSendResult]:
    def _h(i: acts.PreSendInput) -> acts.PreSendResult:
        return acts.PreSendResult(
            i.offer_id,
            changed,
            gates,
            "90000" if changed else "88500",
            "240161",
            blocked,
            "adjusted" if changed else "pass",
        )

    return _h


def _decide(decision: str) -> Callable[[Any], acts.DecideOfferResult]:
    def _h(i: acts.DecideOfferInput) -> acts.DecideOfferResult:
        return acts.DecideOfferResult(
            i.offer_id,
            decision,
            "scripted",
            "L0" if decision != "send" else "L1",
            decision == "send",
            "msg-1",
            {"send": "sent", "queue_for_human": "pending_review", "blocked": "blocked"}[decision],
        )

    return _h


def _approved(i: acts.SendApprovedOfferInput) -> acts.SendApprovedOfferResult:
    return acts.SendApprovedOfferResult(i.offer_id, sent=True)


def _withdraw(i: acts.WithdrawOfferInput) -> acts.WithdrawOfferResult:
    return acts.WithdrawOfferResult(i.offer_id, "withdrawn")


def _handoff(i: acts.HandoffInput) -> acts.HandoffResult:
    return acts.HandoffResult(i.offer_id, f"negotiation:{i.offer_id}", True)


def _handlers(**overrides: Callable[[Any], Any]) -> dict[str, Callable[[Any], Any]]:
    base: dict[str, Callable[[Any], Any]] = {
        "compute_offer": _computed(),
        "qa_offer_draft": _qa,
        "pre_send_check": _pre(),
        "decide_and_send_offer": _decide("queue_for_human"),
        "send_approved_offer": _approved,
        "withdraw_offer": _withdraw,
        "handoff_to_s5": _handoff,
    }
    base.update(overrides)
    return base


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWorkflowModule]:
    def _make(**overrides: Callable[[Any], Any]) -> FakeWorkflowModule:
        wf = OfferWorkflow()
        fake = FakeWorkflowModule(wf=wf, handlers=_handlers(**overrides))
        monkeypatch.setattr(wo, "workflow", fake)
        return fake

    return _make


def _names(fake: FakeWorkflowModule) -> list[str]:
    return [c.name for c in fake.calls]


def _awaiting(wf: OfferWorkflow) -> bool:
    return wf._state == wo.STATE_AWAITING_HUMAN


async def test_l1_all_gates_pass_auto_sends_and_hands_off(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(decide_and_send_offer=_decide("send"))
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "sent" and result.decision == "send"
    assert result.negotiation_workflow_id == f"negotiation:{OFFER}" and result.human_rounds == 0
    assert _names(fake) == [
        "compute_offer",
        "qa_offer_draft",
        "pre_send_check",
        "decide_and_send_offer",
        "handoff_to_s5",
    ]
    compute = fake.calls[0].arg
    assert compute.idempotency_key == f"offer:{INTAKE}:compute" and compute.seller_lead_id == LEAD
    assert fake.calls[3].arg.workflow_id == f"offer:{INTAKE}"
    assert fake.wf.status()["state"] == "done"


async def test_l0_queues_then_human_approves_with_v6_before_the_send(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim()
    fake.events.append(
        (
            _awaiting,
            lambda: fake.wf.human_decision(
                HumanDecision(step=None, approved=True, actor_id="aaron", message_id="msg-1")
            ),
        )
    )
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "queued_then_sent" and result.human_rounds == 1
    assert _names(fake) == [
        "compute_offer",
        "qa_offer_draft",
        "pre_send_check",
        "decide_and_send_offer",
        "pre_send_check",  # V6 again, immediately before the human's send
        "send_approved_offer",
        "handoff_to_s5",
    ]
    assert fake.calls[5].arg.actor_id == "aaron"


async def test_l0_human_rejects_withdraws(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim()
    fake.events.append(
        (
            _awaiting,
            lambda: fake.wf.human_decision(
                HumanDecision(step=None, approved=False, actor_id="aaron")
            ),
        )
    )
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "human_rejected" and _names(fake)[-1] == "withdraw_offer"
    assert fake.calls[-1].arg.reason == "rejected by human in review queue"
    assert fake.wf.status()["state"] == "withdrawn"


async def test_low_confidence_goes_to_human_not_auto_send(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    # The engine escalated; the real policy would queue at L1 because gates failed. The sim's
    # decide handler returns queue_for_human and the workflow waits for the human.
    fake = sim(compute_offer=_computed("escalate"), pre_send_check=_pre(gates=False))
    fake.events.append(
        (_awaiting, lambda: fake.wf.human_decision(HumanDecision(step=None, approved=False)))
    )
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.arv_confidence == "Low" and result.outcome == "human_rejected"
    assert fake.wf.status()["gates_passed"] is False


async def test_v5_mismatch_blocks_before_qa(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(compute_offer=_computed("blocked", blocked_reason="V5 recompute mismatch: MISMATCH"))
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "blocked" and result.offer_price is None
    assert _names(fake) == ["compute_offer"]


async def test_policy_blocked_ends_the_workflow(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(decide_and_send_offer=_decide("blocked"))
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "blocked" and result.decision == "blocked"
    assert "handoff_to_s5" not in _names(fake)


async def test_v6_change_before_decision_requeues_qa(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    fake = sim(pre_send_check=_pre(changed=True), decide_and_send_offer=_decide("send"))
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.v6_changed and result.offer_price == "90000"
    assert _names(fake)[:5] == [
        "compute_offer",
        "qa_offer_draft",
        "pre_send_check",
        "qa_offer_draft",
        "decide_and_send_offer",
    ]
    assert fake.calls[3].arg.force is True


async def test_v6_change_after_human_approval_requeues_for_a_fresh_decision(
    sim: Callable[..., FakeWorkflowModule],
) -> None:
    pre_calls = {"n": 0}

    def _pre_changing(i: acts.PreSendInput) -> acts.PreSendResult:
        pre_calls["n"] += 1
        changed = (
            pre_calls["n"] == 2
        )  # the first V6 (before the decision) is clean; after approval it moves
        return acts.PreSendResult(
            i.offer_id,
            changed,
            True,
            "90000" if changed else "88500",
            "x",
            None,
            "adjusted" if changed else "pass",
        )

    fake = sim(pre_send_check=_pre_changing)
    approvals = {"n": 0}

    def _approve() -> None:
        approvals["n"] += 1
        fake.wf.human_decision(HumanDecision(step=None, approved=True, actor_id="aaron"))

    fake.events.append((_awaiting, _approve))
    fake.events.append((lambda wf: _awaiting(wf) and approvals["n"] == 1, _approve))
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "queued_then_sent" and result.human_rounds == 2 and result.v6_changed
    names = _names(fake)
    assert names.count("pre_send_check") == 3 and names.count("send_approved_offer") == 1
    assert names.count("qa_offer_draft") == 2  # re-QA after the change


async def test_v6_blocked_after_approval(sim: Callable[..., FakeWorkflowModule]) -> None:
    calls = {"n": 0}

    def _pre_block(i: acts.PreSendInput) -> acts.PreSendResult:
        calls["n"] += 1
        return acts.PreSendResult(
            i.offer_id,
            False,
            True,
            "88500",
            "x",
            "V5 recompute mismatch after V6" if calls["n"] == 2 else None,
        )

    fake = sim(pre_send_check=_pre_block)
    fake.events.append(
        (_awaiting, lambda: fake.wf.human_decision(HumanDecision(step=None, approved=True)))
    )
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "blocked" and "send_approved_offer" not in _names(fake)


async def test_too_many_v6_changes_withdraws(sim: Callable[..., FakeWorkflowModule]) -> None:
    def _pre_always_changing(i: acts.PreSendInput) -> acts.PreSendResult:
        return acts.PreSendResult(i.offer_id, True, True, "90000", "x", None, "adjusted")

    fake = sim(pre_send_check=_pre_always_changing)
    fake.events.extend(
        (_awaiting, lambda: fake.wf.human_decision(HumanDecision(step=None, approved=True)))
        for _ in range(wo.MAX_HUMAN_ROUNDS)
    )
    result = await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    assert result.outcome == "withdrawn" and result.human_rounds == wo.MAX_HUMAN_ROUNDS
    assert fake.calls[-1].name == "withdraw_offer" and "V6" in fake.calls[-1].arg.reason


async def test_late_signal_after_done_is_ignored(sim: Callable[..., FakeWorkflowModule]) -> None:
    fake = sim(decide_and_send_offer=_decide("send"))
    await fake.wf.run(OfferWorkflowInput(INTAKE, LEAD))
    fake.wf.human_decision(HumanDecision(step=None, approved=False))
    assert fake.wf.status()["last_human_decision"] is None
