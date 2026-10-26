"""``NegotiationWorkflow`` - S5 (REQUIREMENTS.md WF-05, SEL-09, POL-03h, POL-06).

Started by the offer path once the offer is sent: workflow id ``negotiation:{offer_id}``, input
``NegotiationInput(offer_id, seller_lead_id)``. The schedule comes from ``config/sequences.yaml``
through the ``load_negotiation`` activity (reminders 2/5/10 d, expire 14 d, re-engage 60/90 d,
declined leads requeued after 90 d).

Timeline (durable timers, each interruptible by a signal)::

    day 0    offer sent (by OfferWorkflow)
    day 2/5/10   send_negotiation_message(reminder)   A5 QA + OFFER_SEND policy; queued
                                                      reminders sit in the review queue
    day 14   expire_offer                             status expired
    day 60/90    send_negotiation_message(reengage)   no number: a fresh A7 run is needed
    then     done ("expired")

Signals::

    counter(CounterSignal)      seller countered at ``amount`` -> evaluate_counter:
                                inside the room (offer..MAO) and policy says auto (L1/L2) ->
                                accepted, ``contract.requested``; otherwise (L0 human, above
                                MAO, lapsed offer) -> wait for human_override
    accept(AcceptSignal|None)   seller accepted the number -> accept_offer, contract.requested
    decline(DeclineSignal|None) reason captured; requeue after decline_requeue_days
    human_override(HumanOverride)  action accept(amount) | decline(reason) | withdraw

``contract.requested`` is where this workflow stops: a human signs (POL-03i), and the S6
disposition side starts from the ``contract.signed`` audit event the ops surface writes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.offers import (
        AcceptOfferInput,
        AcceptOfferResult,
        CounterDecision,
        DeclineOfferInput,
        EvaluateCounterInput,
        ExpireOfferInput,
        LoadNegotiationInput,
        NegotiationMessageInput,
        NegotiationMessageResult,
        NegotiationTerms,
        RequeueInput,
        RequeueResult,
        WithdrawOfferInput,
        accept_offer,
        decline_offer,
        evaluate_counter,
        expire_offer,
        load_negotiation,
        requeue_seller,
        send_negotiation_message,
        withdraw_offer,
    )

WORKFLOW_NAME = "NegotiationWorkflow"
WORKFLOW_ID_PREFIX = "negotiation:"
ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
SECONDS_PER_DAY = 86_400

SIGNAL_COUNTER = "counter"
SIGNAL_ACCEPT = "accept"
SIGNAL_DECLINE = "decline"
SIGNAL_HUMAN_OVERRIDE = "human_override"

STATE_PENDING = "pending"
STATE_WAITING = "waiting"
STATE_EVALUATING = "evaluating_counter"
STATE_AWAITING_HUMAN = "awaiting_human"
STATE_ACCEPTED = "accepted"
STATE_DECLINED = "declined"
STATE_EXPIRED = "expired"
STATE_WITHDRAWN = "withdrawn"
STATE_DONE = "done"


def negotiation_workflow_id(offer_id: str) -> str:
    return f"{WORKFLOW_ID_PREFIX}{offer_id}"


@dataclass(slots=True)
class NegotiationInput:
    offer_id: str
    seller_lead_id: str


@dataclass(slots=True)
class CounterSignal:
    amount: str
    message_id: str | None = None


@dataclass(slots=True)
class AcceptSignal:
    message_id: str | None = None


@dataclass(slots=True)
class DeclineSignal:
    reason: str = "declined"
    message_id: str | None = None


@dataclass(slots=True)
class HumanOverride:
    action: str  # accept | decline | withdraw
    amount: str | None = None
    reason: str | None = None
    actor_id: str | None = None


@dataclass(slots=True)
class NegotiationResult:
    offer_id: str
    outcome: str  # accepted | declined | expired | withdrawn
    accepted_price: str | None
    reminders_sent: list[int]
    reengagements_sent: list[int]
    counters: int
    decline_reason: str | None
    requeued: bool


@dataclass(slots=True)
class NegotiationStatus:
    offer_id: str
    state: str
    day: int
    offer_price: str | None
    mao: str | None
    reminders_sent: list[int]
    reengagements_sent: list[int]
    counters: list[dict[str, Any]]
    pending_counter: dict[str, Any] | None
    last_signal: dict[str, Any] | None
    outcome: str | None


@workflow.defn(name=WORKFLOW_NAME)
class NegotiationWorkflow:
    def __init__(self) -> None:
        self._input: NegotiationInput | None = None
        self._state = STATE_PENDING
        self._terms: NegotiationTerms | None = None
        self._queue: list[tuple[str, Any]] = []  # pending signals, in arrival order
        self._pending_counter: CounterSignal | None = None
        self._override: HumanOverride | None = None
        self._reminders: list[int] = []
        self._reengagements: list[int] = []
        self._counters: list[dict[str, Any]] = []
        self._accepted_price: str | None = None
        self._decline_reason: str | None = None
        self._outcome: str | None = None
        self._requeued = False
        self._last_signal: dict[str, Any] | None = None
        self._elapsed_seconds = 0.0
        self._expired = False

    async def _act(self, fn: Any, arg: Any) -> Any:
        return await workflow.execute_activity(
            fn, arg, start_to_close_timeout=ACTIVITY_TIMEOUT, retry_policy=ACTIVITY_RETRY
        )

    # -- run -------------------------------------------------------------------------------------

    @workflow.run
    async def run(self, input: NegotiationInput) -> NegotiationResult:
        self._input = input
        self._terms = await self._act(
            load_negotiation, LoadNegotiationInput(offer_id=input.offer_id)
        )
        assert self._terms is not None
        schedule = self._terms.schedule or {}
        timeline: list[tuple[int, str]] = [
            *((int(d), "reminder") for d in schedule.get("reminder_days", [2, 5, 10])),
            (int(schedule.get("expire_day", 14)), "expire"),
            *((int(d), "reengage") for d in schedule.get("reengage_days", [60, 90])),
        ]
        wf_id = workflow.info().workflow_id

        for day, event in timeline:
            # Wait until ``day``; any signal interrupts the wait and is handled first.
            target = float(day * SECONDS_PER_DAY)
            while self._elapsed_seconds < target:
                outcome = await self._drain_signals()
                if outcome is not None:
                    return await self._finish(outcome)
                remaining = target - self._elapsed_seconds
                started = workflow.now()
                try:
                    await workflow.wait_condition(
                        lambda: bool(self._queue) or self._override is not None,
                        timeout=timedelta(seconds=remaining),
                    )
                except TimeoutError:
                    self._elapsed_seconds = target
                    break
                self._elapsed_seconds += (workflow.now() - started).total_seconds()
            outcome = await self._drain_signals()
            if outcome is not None:
                return await self._finish(outcome)

            if event == "reminder":
                sent: NegotiationMessageResult = await self._act(
                    send_negotiation_message,
                    NegotiationMessageInput(
                        offer_id=input.offer_id, kind="reminder", day=day, workflow_id=wf_id
                    ),
                )
                if sent.skipped_reason is None:
                    self._reminders.append(day)
            elif event == "expire":
                await self._act(expire_offer, ExpireOfferInput(offer_id=input.offer_id))
                self._expired = True
                self._state = STATE_EXPIRED
            elif event == "reengage":
                sent = await self._act(
                    send_negotiation_message,
                    NegotiationMessageInput(
                        offer_id=input.offer_id, kind="reengage", day=day, workflow_id=wf_id
                    ),
                )
                if sent.skipped_reason is None:
                    self._reengagements.append(day)
            self._state = STATE_WAITING if not self._expired else STATE_EXPIRED

        outcome = await self._drain_signals()
        return await self._finish(outcome or "expired")

    # -- signal handling -------------------------------------------------------------------------

    async def _drain_signals(self) -> str | None:
        """Handle queued signals in order. Returns a terminal outcome or ``None``."""
        assert self._input is not None
        while self._queue or self._override is not None:
            if self._override is not None:
                outcome = await self._apply_override(self._override)
                self._override = None
                if outcome is not None:
                    return outcome
                continue
            kind, payload = self._queue.pop(0)
            if kind == SIGNAL_ACCEPT:
                self._state = STATE_ACCEPTED
                res: AcceptOfferResult = await self._act(
                    accept_offer, AcceptOfferInput(offer_id=self._input.offer_id, source="seller")
                )
                self._accepted_price = res.price
                return "accepted"
            if kind == SIGNAL_DECLINE:
                reason = payload.reason if isinstance(payload, DeclineSignal) else "declined"
                self._decline_reason = reason
                self._state = STATE_DECLINED
                await self._act(
                    decline_offer, DeclineOfferInput(offer_id=self._input.offer_id, reason=reason)
                )
                return "declined"
            if kind == SIGNAL_COUNTER and isinstance(payload, CounterSignal):
                outcome = await self._handle_counter(payload)
                if outcome is not None:
                    return outcome
        return None

    async def _handle_counter(self, counter: CounterSignal) -> str | None:
        assert self._input is not None
        self._state = STATE_EVALUATING
        decision: CounterDecision = await self._act(
            evaluate_counter,
            EvaluateCounterInput(
                offer_id=self._input.offer_id,
                amount=counter.amount,
                message_id=counter.message_id,
                workflow_id=workflow.info().workflow_id,
            ),
        )
        self._counters.append(
            {
                "amount": decision.amount,
                "classification": decision.classification,
                "within_room": decision.within_room,
                "decision": decision.decision,
                "reason": decision.reason,
                "level": decision.level,
                "accepted": decision.accepted,
            }
        )
        if decision.accepted:
            self._accepted_price = decision.amount
            self._state = STATE_ACCEPTED
            return "accepted"
        if decision.decision == "blocked":
            self._state = STATE_WAITING
            return None
        # queue_for_human: above MAO, L0, or a lapsed offer. Wait for the human.
        self._pending_counter = counter
        self._state = STATE_AWAITING_HUMAN
        await workflow.wait_condition(lambda: self._override is not None or bool(self._queue))
        self._pending_counter = None
        return None  # the loop in _drain_signals applies the override / next signal

    async def _apply_override(self, override: HumanOverride) -> str | None:
        assert self._input is not None
        if override.action == "accept":
            res: AcceptOfferResult = await self._act(
                accept_offer,
                AcceptOfferInput(
                    offer_id=self._input.offer_id,
                    amount=override.amount,
                    actor_id=override.actor_id,
                    source="human",
                ),
            )
            self._accepted_price = res.price
            self._state = STATE_ACCEPTED
            return "accepted"
        if override.action == "decline":
            reason = override.reason or "declined by human"
            self._decline_reason = reason
            self._state = STATE_DECLINED
            await self._act(
                decline_offer,
                DeclineOfferInput(
                    offer_id=self._input.offer_id, reason=reason, actor_id=override.actor_id
                ),
            )
            return "declined"
        if override.action == "withdraw":
            self._state = STATE_WITHDRAWN
            await self._act(
                withdraw_offer,
                WithdrawOfferInput(
                    offer_id=self._input.offer_id,
                    reason=override.reason or "withdrawn by human",
                    actor_id=override.actor_id,
                ),
            )
            return "withdrawn"
        # "continue" / unknown: the human declined to act; keep waiting on the timeline.
        self._state = STATE_WAITING if not self._expired else STATE_EXPIRED
        return None

    async def _finish(self, outcome: str) -> NegotiationResult:
        assert self._input is not None
        if outcome == "declined":
            # SEL-09: requeue the seller after decline_requeue_days (durable timer).
            days = (
                int((self._terms.schedule or {}).get("decline_requeue_days", 90))
                if self._terms
                else 90
            )
            try:
                await workflow.wait_condition(
                    lambda: self._override is not None and self._override.action == "withdraw",
                    timeout=timedelta(days=days),
                )
            except TimeoutError:
                res: RequeueResult = await self._act(
                    requeue_seller,
                    RequeueInput(
                        offer_id=self._input.offer_id, seller_lead_id=self._input.seller_lead_id
                    ),
                )
                self._requeued = res.requeued
        self._outcome = outcome
        self._state = STATE_DONE
        return NegotiationResult(
            offer_id=self._input.offer_id,
            outcome=outcome,
            accepted_price=self._accepted_price,
            reminders_sent=list(self._reminders),
            reengagements_sent=list(self._reengagements),
            counters=len(self._counters),
            decline_reason=self._decline_reason,
            requeued=self._requeued,
        )

    # -- signals ---------------------------------------------------------------------------------

    def _record(self, kind: str, payload: Any) -> None:
        record: dict[str, Any] = {"signal": kind}
        if payload is not None:
            record.update(asdict(payload))
        self._last_signal = record

    @workflow.signal
    def counter(self, payload: CounterSignal) -> None:
        if self._outcome is not None:
            return
        self._record(SIGNAL_COUNTER, payload)
        self._queue.append((SIGNAL_COUNTER, payload))

    @workflow.signal
    def accept(self, payload: AcceptSignal | None = None) -> None:
        if self._outcome is not None:
            return
        self._record(SIGNAL_ACCEPT, payload)
        self._queue.append((SIGNAL_ACCEPT, payload))

    @workflow.signal
    def decline(self, payload: DeclineSignal | None = None) -> None:
        if self._outcome is not None:
            return
        self._record(SIGNAL_DECLINE, payload)
        self._queue.append((SIGNAL_DECLINE, payload or DeclineSignal()))

    @workflow.signal
    def human_override(self, payload: HumanOverride) -> None:
        if self._outcome is not None and payload.action != "withdraw":
            return
        self._record(SIGNAL_HUMAN_OVERRIDE, payload)
        self._override = payload

    # -- query -----------------------------------------------------------------------------------

    @workflow.query
    def status(self) -> dict[str, Any]:
        return asdict(
            NegotiationStatus(
                offer_id=self._input.offer_id if self._input else "",
                state=self._state,
                day=int(self._elapsed_seconds // SECONDS_PER_DAY),
                offer_price=self._terms.offer_price if self._terms else None,
                mao=self._terms.mao if self._terms else None,
                reminders_sent=list(self._reminders),
                reengagements_sent=list(self._reengagements),
                counters=list(self._counters),
                pending_counter=asdict(self._pending_counter) if self._pending_counter else None,
                last_signal=dict(self._last_signal) if self._last_signal else None,
                outcome=self._outcome,
            )
        )


__all__ = [
    "ACTIVITY_RETRY",
    "ACTIVITY_TIMEOUT",
    "SIGNAL_ACCEPT",
    "SIGNAL_COUNTER",
    "SIGNAL_DECLINE",
    "SIGNAL_HUMAN_OVERRIDE",
    "WORKFLOW_ID_PREFIX",
    "WORKFLOW_NAME",
    "AcceptSignal",
    "CounterSignal",
    "DeclineSignal",
    "HumanOverride",
    "NegotiationInput",
    "NegotiationResult",
    "NegotiationStatus",
    "NegotiationWorkflow",
    "negotiation_workflow_id",
]
