"""``SellerSequenceWorkflow``: initial + 3 follow-ups at 3 / 7 / 14 days in one Outlook
conversation (REQUIREMENTS.md SEL-04, SEL-05, WF-01, WF-03, WF-21, WF-22, POL-03d), and
``SellerListRefreshWorkflow``, the weekly S1 / S2 refresh that starts them (WF-10).

Started by the refresh (or ``activities.seller.start_seller_sequence``). Workflow id:
``seller_sequence_workflow_id(sequence_id)`` = ``seller-sequence:{sequence_id}``.

The run loop is ``InvestorSequenceWorkflow``'s, step for step; what differs is the activities
behind ``prepare`` / ``decide`` / ``approved`` (seller templates, ``seller_outreach_email``) and
one extra cancelling signal, ``intake_complete`` (WF-03): once S4 has everything A7 needs, the
remaining follow-ups stop. ``check_cancel_conditions`` / ``send_window`` / ``update_sequence``
are the investor-side activities reused unchanged (they work on the ``sequences`` row).

Signal contract (the review queue codes against ``HumanDecision`` exactly as for investors):

    human_decision(HumanDecision)                  approve / edit / reject a queued step
    reply(ReplySignal | None)                      -> cancel ("reply")
    opt_out(OptOutSignal | None)                   -> cancel ("opt_out")
    bounce(BounceSignal | None)                    -> cancel ("bounce")
    intake_complete(IntakeCompleteSignal | None)   -> cancel ("intake_complete")
    pause() / resume()

Query ``status`` -> ``SellerSequenceStatus`` as a dict (same shape as the investor status).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from wholesaling.signals import (
    SIGNAL_BOUNCE,
    SIGNAL_OPT_OUT,
    SIGNAL_REPLY,
    BounceSignal,
    OptOutSignal,
    ReplySignal,
)
from wholesaling.workflows.investor_sequence import HumanDecision

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.outreach import (
        CheckCancelInput,
        CheckCancelResult,
        DecideAndSendInput,
        DecideAndSendResult,
        PrepareStepResult,
        SendApprovedInput,
        SendApprovedResult,
        SendWindowInput,
        SendWindowResult,
        UpdateSequenceInput,
        check_cancel_conditions,
        send_window,
        update_sequence,
    )
    from wholesaling.activities.seller import (
        SELLER_SEQUENCE_WORKFLOW_NAME,
        SIGNAL_INTAKE_COMPLETE,
        IntakeCompleteSignal,
        SellerListRefreshInput,
        SellerListRefreshResult,
        SellerPrepareStepInput,
        seller_decide_and_send,
        seller_list_refresh,
        seller_prepare_step,
        seller_send_approved,
        seller_sequence_workflow_id,
    )

WORKFLOW_NAME = SELLER_SEQUENCE_WORKFLOW_NAME
REFRESH_WORKFLOW_NAME = "SellerListRefreshWorkflow"
ACTIVITY_TIMEOUT = timedelta(minutes=5)  # A5 runs inside decide
REFRESH_ACTIVITY_TIMEOUT = timedelta(minutes=30)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
DEFAULT_INTERVALS_DAYS = (3, 7, 14)  # OPN-05; the starter passes config/sequences.yaml values
SECONDS_PER_DAY = 86_400

STATE_PENDING = "pending"
STATE_WAITING_WINDOW = "waiting_send_window"
STATE_DECIDING = "deciding"
STATE_AWAITING_HUMAN = "awaiting_human"
STATE_SLEEPING = "sleeping"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"


@dataclass(slots=True)
class SellerSequenceInput:
    sequence_id: str
    seller_lead_id: str
    contact_email: str
    market: str
    intervals_days: list[int] = field(default_factory=lambda: list(DEFAULT_INTERVALS_DAYS))
    honor_send_window: bool = True


@dataclass(slots=True)
class SellerSequenceResult:
    sequence_id: str
    steps_sent: list[int]
    cancel_reason: str | None
    completed: bool
    decisions: dict[str, str]


@dataclass(slots=True)
class SellerSequenceStatus:
    sequence_id: str
    state: str
    step: int
    steps_sent: list[int]
    paused: bool
    cancel_reason: str | None
    remaining_seconds: float
    decisions: dict[str, str]
    pending_human_step: int | None
    pending_message_id: str | None
    unmatched_decisions: int
    last_signal: dict[str, Any] | None = None


@workflow.defn(name=WORKFLOW_NAME)
class SellerSequenceWorkflow:
    def __init__(self) -> None:
        self._input: SellerSequenceInput | None = None
        self._state = STATE_PENDING
        self._step = 0
        self._steps_sent: list[int] = []
        self._paused = False
        self._cancel_reason: str | None = None
        self._remaining = 0.0
        self._decisions: dict[str, str] = {}
        self._human: dict[int, HumanDecision] = {}
        self._timezone = "America/Chicago"
        self._pending_message_id: str | None = None
        self._unmatched_decisions = 0
        self._last_signal: dict[str, Any] | None = None

    @workflow.run
    async def run(self, input: SellerSequenceInput) -> SellerSequenceResult:
        self._input = input
        intervals = [int(d) for d in input.intervals_days]
        total_steps = len(intervals) + 1
        workflow.logger.info(
            "seller_sequence.started",
            extra={"sequence_id": input.sequence_id, "total_steps": total_steps},
        )
        for step in range(total_steps):
            self._step = step
            check: CheckCancelResult = await self._act(
                check_cancel_conditions,
                CheckCancelInput(sequence_id=input.sequence_id, contact_email=input.contact_email),
            )
            if check.cancel_reason and check.cancel_reason != "completed":
                self._cancel_reason = check.cancel_reason
            if check.paused:
                self._paused = True
            if self._cancel_reason is not None:
                return await self._finish_cancelled()
            await self._hold_while_paused()
            if self._cancel_reason is not None:
                return await self._finish_cancelled()

            if input.honor_send_window:
                window: SendWindowResult = await self._act(
                    send_window, SendWindowInput(market=input.market, sequence_id=input.sequence_id)
                )
                self._timezone = window.timezone
                if window.seconds_until_open > 0:
                    self._state = STATE_WAITING_WINDOW
                    await self._durable_sleep(window.seconds_until_open)
                    if self._cancel_reason is not None:
                        return await self._finish_cancelled()

            self._state = STATE_DECIDING
            prepared: PrepareStepResult = await self._act(
                seller_prepare_step,
                SellerPrepareStepInput(
                    sequence_id=input.sequence_id,
                    step=step,
                    contact_email=input.contact_email,
                    market=input.market,
                ),
            )
            decided: DecideAndSendResult = await self._act(
                seller_decide_and_send,
                DecideAndSendInput(
                    message_id=prepared.message_id, step=step, timezone=self._timezone
                ),
            )
            self._decisions[str(step)] = decided.decision
            self._pending_message_id = prepared.message_id

            if decided.decision == "blocked":
                self._cancel_reason = f"blocked:{decided.reason}"
                return await self._finish_cancelled()

            if decided.decision == "queue_for_human":
                self._state = STATE_AWAITING_HUMAN
                await workflow.wait_condition(self._human_decided_or_cancelled(step))
                if self._cancel_reason is not None:
                    return await self._finish_cancelled()
                human = self._human[step]
                if not human.approved:
                    self._cancel_reason = f"human_rejected:step:{step}"
                    return await self._finish_cancelled()
                approved: SendApprovedResult = await self._act(
                    seller_send_approved,
                    SendApprovedInput(
                        message_id=prepared.message_id,
                        step=step,
                        edited_body=human.edited_body,
                        actor_id=human.actor_id,
                    ),
                )
                if approved.blocked_reason:
                    self._cancel_reason = f"blocked:{approved.blocked_reason}"
                    return await self._finish_cancelled()
                self._decisions[str(step)] = "send"

            self._steps_sent.append(step)
            is_last = step == total_steps - 1
            next_in = None if is_last else intervals[step] * SECONDS_PER_DAY
            await self._act(
                update_sequence,
                UpdateSequenceInput(
                    sequence_id=input.sequence_id, step_done=step, next_send_in_seconds=next_in
                ),
            )
            if not is_last and next_in is not None:
                self._state = STATE_SLEEPING
                await self._durable_sleep(float(next_in))
                if self._cancel_reason is not None:
                    return await self._finish_cancelled()

        self._state = STATE_DONE
        await self._act(
            update_sequence, UpdateSequenceInput(sequence_id=input.sequence_id, completed=True)
        )
        return SellerSequenceResult(
            sequence_id=input.sequence_id,
            steps_sent=list(self._steps_sent),
            cancel_reason=None,
            completed=True,
            decisions=dict(self._decisions),
        )

    # -- helpers ---------------------------------------------------------------------------------

    async def _act(self, fn: Any, arg: Any) -> Any:
        return await workflow.execute_activity(
            fn, arg, start_to_close_timeout=ACTIVITY_TIMEOUT, retry_policy=ACTIVITY_RETRY
        )

    async def _finish_cancelled(self) -> SellerSequenceResult:
        assert self._input is not None
        self._state = STATE_CANCELLED
        reason = self._cancel_reason or "cancelled"
        await self._act(
            update_sequence,
            UpdateSequenceInput(sequence_id=self._input.sequence_id, cancel_reason=reason),
        )
        workflow.logger.info(
            "seller_sequence.cancelled",
            extra={"sequence_id": self._input.sequence_id, "reason": reason, "step": self._step},
        )
        return SellerSequenceResult(
            sequence_id=self._input.sequence_id,
            steps_sent=list(self._steps_sent),
            cancel_reason=reason,
            completed=False,
            decisions=dict(self._decisions),
        )

    def _human_decided_or_cancelled(self, step: int) -> Callable[[], bool]:
        def _ready() -> bool:
            return step in self._human or self._cancel_reason is not None

        return _ready

    async def _hold_while_paused(self) -> None:
        if self._paused:
            previous = self._state
            self._state = STATE_PAUSED
            await workflow.wait_condition(
                lambda: not self._paused or self._cancel_reason is not None
            )
            self._state = previous

    async def _durable_sleep(self, seconds: float) -> None:
        self._remaining = max(0.0, float(seconds))
        while self._remaining > 0 and self._cancel_reason is None:
            if self._paused:
                await self._hold_while_paused()
                continue
            started = workflow.now()
            try:
                await workflow.wait_condition(
                    lambda: self._paused or self._cancel_reason is not None,
                    timeout=timedelta(seconds=self._remaining),
                )
            except TimeoutError:
                self._remaining = 0.0
                return
            elapsed = (workflow.now() - started).total_seconds()
            self._remaining = max(0.0, self._remaining - elapsed)
        self._remaining = 0.0

    # -- signals ---------------------------------------------------------------------------------

    @workflow.signal
    def human_decision(self, decision: HumanDecision) -> None:
        step = decision.step
        if step is None:
            if decision.message_id is not None and decision.message_id == self._pending_message_id:
                step = self._step
            else:
                self._unmatched_decisions += 1
                return
        if step in self._steps_sent:
            return
        self._human[step] = decision

    @workflow.signal
    def reply(self, payload: ReplySignal | None = None) -> None:
        self._cancel(SIGNAL_REPLY, payload)

    @workflow.signal
    def opt_out(self, payload: OptOutSignal | None = None) -> None:
        self._cancel(SIGNAL_OPT_OUT, payload)

    @workflow.signal
    def bounce(self, payload: BounceSignal | None = None) -> None:
        self._cancel(SIGNAL_BOUNCE, payload)

    @workflow.signal
    def intake_complete(self, payload: IntakeCompleteSignal | None = None) -> None:
        """WF-03: S4 has every required field; A7 takes over, no more follow-ups."""
        self._cancel(SIGNAL_INTAKE_COMPLETE, payload)

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    def _cancel(self, reason: str, payload: Any = None) -> None:
        if self._cancel_reason is not None:
            return
        self._cancel_reason = reason
        record: dict[str, Any] = {"signal": reason}
        if payload is not None:
            record.update(asdict(payload))
        self._last_signal = record

    # -- query -----------------------------------------------------------------------------------

    @workflow.query
    def status(self) -> dict[str, Any]:
        pending = (
            self._step
            if self._state == STATE_AWAITING_HUMAN and self._step not in self._human
            else None
        )
        return asdict(
            SellerSequenceStatus(
                sequence_id=self._input.sequence_id if self._input else "",
                state=self._state,
                step=self._step,
                steps_sent=list(self._steps_sent),
                paused=self._paused,
                cancel_reason=self._cancel_reason,
                remaining_seconds=self._remaining,
                decisions=dict(self._decisions),
                pending_human_step=pending,
                pending_message_id=self._pending_message_id if pending is not None else None,
                unmatched_decisions=self._unmatched_decisions,
                last_signal=dict(self._last_signal) if self._last_signal else None,
            )
        )


# --------------------------------------------------------------------------------------------
# Weekly refresh (WF-10)
# --------------------------------------------------------------------------------------------


@workflow.defn(name=REFRESH_WORKFLOW_NAME)
class SellerListRefreshWorkflow:
    """One run per schedule fire: S1 per Active market, S2 on new leads, sequences started."""

    def __init__(self) -> None:
        self._state = STATE_PENDING
        self._result: SellerListRefreshResult | None = None

    @workflow.run
    async def run(self, input: SellerListRefreshInput) -> SellerListRefreshResult:
        self._state = "refreshing"
        self._result = await workflow.execute_activity(
            seller_list_refresh,
            input,
            start_to_close_timeout=REFRESH_ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        self._state = STATE_DONE
        return self._result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


__all__ = [
    "ACTIVITY_RETRY",
    "ACTIVITY_TIMEOUT",
    "DEFAULT_INTERVALS_DAYS",
    "REFRESH_WORKFLOW_NAME",
    "WORKFLOW_NAME",
    "HumanDecision",
    "IntakeCompleteSignal",
    "SellerListRefreshInput",
    "SellerListRefreshResult",
    "SellerListRefreshWorkflow",
    "SellerSequenceInput",
    "SellerSequenceResult",
    "SellerSequenceStatus",
    "SellerSequenceWorkflow",
    "seller_sequence_workflow_id",
]
