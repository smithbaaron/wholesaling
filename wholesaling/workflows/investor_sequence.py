"""``InvestorSequenceWorkflow``: initial + 3 follow-ups in one Outlook conversation
(REQUIREMENTS.md INV-10, INV-12, INV-23, WF-01, WF-02, WF-21, WF-22, POL-03a/b).

Started by A2 when a qualified contact exists (WF-02). Workflow id:
``investor_sequence_workflow_id(sequence_id)`` = ``investor-sequence:{sequence_id}``.

Per step ``n`` in 0..3::

    check_cancel_conditions  ->  reply / opt_out / bounce / suppressed  => cancel, return
    send_window              ->  sleep until the market's business hours (INV-12)
    prepare_step             ->  draft row keyed seq:{sequence_id}:step:{n}
    decide_and_send          ->  send | queue_for_human | blocked
        queue_for_human      ->  wait for the ``human_decision`` signal for this step
                                 approved  => send_approved
                                 rejected  => cancel ("human_rejected:step:{n}"), return
        blocked              ->  cancel ("blocked:<reason>"), return
    update_sequence(step)
    durable sleep intervals_days[n] (steps 0..2), interruptible by signals

Signal contract (the review-queue API codes against this; see docs/agents/outreach.md):

    human_decision(HumanDecision)   dataclass: step:int|None, approved:bool,
                                    edited_body:str|None, actor_id:str|None,
                                    message_id:str|None - sent when an operator approves /
                                    edits / rejects the queued draft for ``step`` (or for the
                                    ``messages.id`` the workflow is currently waiting on)
    reply()      a substantive inbound reply arrived     -> cancel remaining steps ("reply")
    opt_out()    recipient opted out                      -> cancel ("opt_out")
    bounce()     delivery failed                          -> cancel ("bounce")
    pause()      hold before the next send (no cancel)
    resume()     release a pause

Query ``status`` -> ``InvestorSequenceStatus`` as a dict.

Replay safety: every timestamp comes from ``workflow.now()``; all I/O is in activities whose
inputs are deterministic (the idempotency key is derived from the sequence id and step), so a
worker restart mid-sequence neither loses nor duplicates a send (WF-22).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.outreach import (
        CheckCancelInput,
        CheckCancelResult,
        DecideAndSendInput,
        DecideAndSendResult,
        PrepareStepInput,
        PrepareStepResult,
        SendApprovedInput,
        SendApprovedResult,
        SendWindowInput,
        SendWindowResult,
        UpdateSequenceInput,
        check_cancel_conditions,
        decide_and_send,
        prepare_step,
        send_approved,
        send_window,
        update_sequence,
    )

WORKFLOW_NAME = "InvestorSequenceWorkflow"
WORKFLOW_ID_PREFIX = "investor-sequence:"
ACTIVITY_TIMEOUT = timedelta(minutes=2)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
DEFAULT_INTERVALS_DAYS = (4, 4, 4)  # OPN-04; the starter passes config/sequences.yaml values
SECONDS_PER_DAY = 86_400

STATE_PENDING = "pending"
STATE_WAITING_WINDOW = "waiting_send_window"
STATE_DECIDING = "deciding"
STATE_AWAITING_HUMAN = "awaiting_human"
STATE_SLEEPING = "sleeping"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"


def investor_sequence_workflow_id(sequence_id: str) -> str:
    return f"{WORKFLOW_ID_PREFIX}{sequence_id}"


@dataclass(slots=True)
class InvestorSequenceInput:
    sequence_id: str
    investor_id: str
    contact_email: str
    market: str
    intervals_days: list[int] = field(default_factory=lambda: list(DEFAULT_INTERVALS_DAYS))
    honor_send_window: bool = True


@dataclass(slots=True)
class HumanDecision:
    """Payload of the ``human_decision`` signal.

    ``step`` identifies the queued draft; the review queue may instead (or also) pass the
    ``messages.id`` it acted on as ``message_id``, which the workflow maps onto the step it is
    currently waiting for. Unknown JSON fields are ignored by Temporal's converter, so either
    side may carry extra fields.
    """

    step: int | None
    approved: bool
    edited_body: str | None = None
    actor_id: str | None = None
    message_id: str | None = None


@dataclass(slots=True)
class InvestorSequenceResult:
    sequence_id: str
    steps_sent: list[int]
    cancel_reason: str | None
    completed: bool
    decisions: dict[str, str]  # str(step) -> policy decision value (JSON-safe keys)


@dataclass(slots=True)
class InvestorSequenceStatus:
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


@workflow.defn(name=WORKFLOW_NAME)
class InvestorSequenceWorkflow:
    def __init__(self) -> None:
        self._input: InvestorSequenceInput | None = None
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

    # -- run -------------------------------------------------------------------------------------

    @workflow.run
    async def run(self, input: InvestorSequenceInput) -> InvestorSequenceResult:
        self._input = input
        intervals = [int(d) for d in input.intervals_days]
        total_steps = len(intervals) + 1
        workflow.logger.info(
            "investor_sequence.started",
            extra={"sequence_id": input.sequence_id, "total_steps": total_steps},
        )

        for step in range(total_steps):
            self._step = step

            # 1. Anything on the thread that stops us? (reply / opt-out / bounce / suppression)
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

            # 2. Business-hour window (INV-12).
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

            # 3. Draft row + policy decision.
            self._state = STATE_DECIDING
            prepared: PrepareStepResult = await self._act(
                prepare_step,
                PrepareStepInput(
                    sequence_id=input.sequence_id,
                    step=step,
                    contact_email=input.contact_email,
                    market=input.market,
                ),
            )
            decided: DecideAndSendResult = await self._act(
                decide_and_send,
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
                    send_approved,
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

            # 4. Record progress and sleep until the next step.
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
        return InvestorSequenceResult(
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

    async def _finish_cancelled(self) -> InvestorSequenceResult:
        assert self._input is not None
        self._state = STATE_CANCELLED
        reason = self._cancel_reason or "cancelled"
        await self._act(
            update_sequence,
            UpdateSequenceInput(sequence_id=self._input.sequence_id, cancel_reason=reason),
        )
        workflow.logger.info(
            "investor_sequence.cancelled",
            extra={"sequence_id": self._input.sequence_id, "reason": reason, "step": self._step},
        )
        return InvestorSequenceResult(
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
        """Sleep ``seconds`` honouring pause/resume and the cancelling signals (HelloWorkflow
        pattern). Returns early when a cancel reason is set."""
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
        """Review-queue outcome for one step. A later signal for the same step is ignored once
        the step has been acted on; before that the latest wins."""
        step = decision.step
        if step is None:
            if decision.message_id is not None and decision.message_id == self._pending_message_id:
                step = self._step
            else:
                self._unmatched_decisions += 1  # visible in ``status``; nothing else to do
                return
        if step in self._steps_sent:
            return
        self._human[step] = decision

    @workflow.signal
    def reply(self) -> None:
        self._cancel("reply")

    @workflow.signal
    def opt_out(self) -> None:
        self._cancel("opt_out")

    @workflow.signal
    def bounce(self) -> None:
        self._cancel("bounce")

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    def _cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason

    # -- query -----------------------------------------------------------------------------------

    @workflow.query
    def status(self) -> dict[str, Any]:
        pending = (
            self._step
            if self._state == STATE_AWAITING_HUMAN and self._step not in self._human
            else None
        )
        return asdict(
            InvestorSequenceStatus(
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
            )
        )


__all__ = [
    "ACTIVITY_RETRY",
    "ACTIVITY_TIMEOUT",
    "DEFAULT_INTERVALS_DAYS",
    "WORKFLOW_ID_PREFIX",
    "WORKFLOW_NAME",
    "HumanDecision",
    "InvestorSequenceInput",
    "InvestorSequenceResult",
    "InvestorSequenceStatus",
    "InvestorSequenceWorkflow",
    "investor_sequence_workflow_id",
]
