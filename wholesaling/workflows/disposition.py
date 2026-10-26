"""``DispositionWorkflow`` (REQUIREMENTS.md WF-06, SEL-10, SEL-11, BB-11, POL-03k, WF-22, WF-23).

Started by the **human** "mark contract signed" action on ``/deals`` (never by an agent), with
workflow id ``disposition_workflow_id(contract_id)`` = ``disposition:{contract_id}``.

Run::

    load_contract                 snapshot; refuses to proceed unless status == signed
    run_blast                     S6 buyer blast (send | queue_for_human | blocked per buyer)
    loop until closed / dead:
        deadline timers           T-3d / T-1d / T-0 for option_period_end, inspection_deadline,
                                  closing_date (workflows/deadlines.py) -> raise_deadline_alert
        signals                   buyer_response, buyer_selected, deposit_received,
                                  deadlines_updated (reload snapshot, re-arm timers),
                                  human_decision (review-queue approval of a queued blast draft
                                  -> send_approved_blast), deal_dead, deal_closed
    deal_dead                     cancel timers, deal_dead_followup (release-earnest reminder)
    deal_closed                   record state, return (the human already wrote the closing +
                                  realised fee on the row - BB-11)

What the workflow never does: create or send a DocuSign envelope, change contract status, move
earnest money, record a deposit, select the buyer, or close the deal. Every one of those is a
human click on ``/deals`` that *then* signals this workflow (XC-05, POL-03i/j).

Deadline alerts are folded into this workflow rather than a separate ``DeadlineAlertWorkflow``
(see ``workflows/deadlines.py`` for the reasoning); ``alerts_raised`` in the status shows them.

Signal contract (``wholesaling/signals.py`` payloads; every payload optional on the wire):

    buyer_response(BuyerResponseSignal)     recorded on the row already; noted in status
    buyer_selected(BuyerSelectedSignal)     a human chose the assignee
    deposit_received(DepositReceivedSignal) a human recorded the deposit
    deadlines_updated()                     a human changed a date -> re-read and re-arm
    human_decision(BlastHumanDecision)      review queue approved/rejected a queued blast draft
    deal_dead(DealDeadSignal)               terminal
    deal_closed(DealClosedSignal)           terminal

Query ``status`` -> ``DispositionStatus`` as a dict.

Replay safety: all timestamps from ``workflow.now()``; activity inputs deterministic (keys from
contract id + investor id / alert key), so a worker restart neither loses nor repeats a send or
an alert (WF-22).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from wholesaling.signals import (
    SIGNAL_BUYER_RESPONSE,
    SIGNAL_BUYER_SELECTED,
    SIGNAL_DEADLINES_UPDATED,
    SIGNAL_DEAL_CLOSED,
    SIGNAL_DEAL_DEAD,
    SIGNAL_DEPOSIT_RECEIVED,
    BuyerResponseSignal,
    BuyerSelectedSignal,
    DealClosedSignal,
    DealDeadSignal,
    DepositReceivedSignal,
)
from wholesaling.workflows import deadlines as dl

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.disposition import (
        BlastResult,
        ContractSnapshot,
        DeadlineAlertInput,
        DealDeadFollowupInput,
        LoadContractInput,
        RunBlastInput,
        SendApprovedBlastInput,
        SendApprovedBlastResult,
        WorkflowStateInput,
        deal_dead_followup,
        disposition_workflow_id,
        load_contract,
        raise_deadline_alert,
        record_buyer_response,
        record_workflow_state,
        run_blast,
        send_approved_blast,
    )

WORKFLOW_NAME = "DispositionWorkflow"
ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)

STATE_PENDING = "pending"
STATE_BLASTING = "blasting"
STATE_MARKETING = "marketing"  # blast out, waiting for buyers
STATE_BUYER_SELECTED = "buyer_selected"
STATE_DEPOSIT_RECEIVED = "deposit_received"
STATE_DEAD = "dead"
STATE_CLOSED = "closed"
STATE_NOT_SIGNED = "not_signed"

CONTRACT_SIGNED = "signed"


@dataclass(slots=True)
class DispositionInput:
    contract_id: str
    alert_offsets_days: list[int] = field(
        default_factory=lambda: list(dl.DEFAULT_ALERT_OFFSETS_DAYS)
    )
    blast_limit: int | None = None
    include_fallback_buyers: bool = True


@dataclass(slots=True)
class BlastHumanDecision:
    """Payload of ``human_decision`` from the review queue (same shape as the sequence's
    ``HumanDecision`` so ``api/routes/review.py`` needs no special case)."""

    step: int | None
    approved: bool
    edited_body: str | None = None
    actor_id: str | None = None
    message_id: str | None = None


@dataclass(slots=True)
class DispositionResult:
    contract_id: str
    outcome: str  # closed | dead | not_signed
    blast_sent: int
    blast_queued: int
    alerts_raised: list[str]
    buyer_investor_id: str | None
    reason: str | None = None


@dataclass(slots=True)
class DispositionStatus:
    contract_id: str
    state: str
    contract_status: str
    blast_sent: int
    blast_queued: int
    blast_blocked: int
    responses: dict[str, str]
    buyer_investor_id: str | None
    deposit_received: bool
    alerts_raised: list[str]
    next_alert_key: str | None
    next_alert_at: str | None
    dead_reason: str | None
    closed: bool
    pending_human_messages: list[str]
    last_signal: dict[str, Any] | None = None


@workflow.defn(name=WORKFLOW_NAME)
class DispositionWorkflow:
    def __init__(self) -> None:
        self._input: DispositionInput | None = None
        self._state = STATE_PENDING
        self._snap: ContractSnapshot | None = None
        self._blast: BlastResult | None = None
        self._responses: dict[str, str] = {}
        self._buyer: str | None = None
        self._deposit = False
        self._raised: set[str] = set()
        self._dead: DealDeadSignal | None = None
        self._closed: DealClosedSignal | None = None
        self._reload = False
        self._human: list[BlastHumanDecision] = []
        self._pending_messages: list[str] = []
        self._last_signal: dict[str, Any] | None = None
        self._next: dl.DeadlineAlert | None = None
        self._recorded_next: str | None = None

    # -- run -------------------------------------------------------------------------------------

    @workflow.run
    async def run(self, input: DispositionInput) -> DispositionResult:
        self._input = input
        self._snap = await self._act(
            load_contract, LoadContractInput(contract_id=input.contract_id)
        )
        self._raised = set(self._snap.raised_alerts)
        self._buyer = self._snap.buyer_investor_id
        self._deposit = self._snap.deposit_received_at is not None
        if self._snap.status != CONTRACT_SIGNED:
            # Defensive: the only caller is the mark-signed route, but a stale start must not blast.
            self._state = STATE_NOT_SIGNED
            await self._record_state()
            return self._result("not_signed", reason=f"contract is {self._snap.status}")

        self._state = STATE_BLASTING
        self._blast = await self._act(
            run_blast,
            RunBlastInput(
                contract_id=input.contract_id,
                limit=input.blast_limit,
                include_fallback=input.include_fallback_buyers,
            ),
        )
        self._pending_messages = [
            r.message_id for r in self._blast.recipients if r.decision == "queue_for_human"
        ]
        self._state = STATE_MARKETING if self._buyer is None else STATE_BUYER_SELECTED
        await self._record_state()

        while True:
            if self._dead is not None:
                return await self._finish_dead()
            if self._closed is not None:
                return await self._finish_closed()
            if self._reload:
                self._reload = False
                self._snap = await self._act(
                    load_contract, LoadContractInput(contract_id=input.contract_id)
                )
                self._raised |= set(self._snap.raised_alerts)
                await self._record_state()
            if self._human:
                await self._apply_human_decisions()
                continue

            deadlines = dl.parse_deadlines(self._snap.deadlines)
            now = workflow.now()
            for alert in dl.due(
                deadlines, self._raised, now, offsets_days=input.alert_offsets_days
            ):
                await self._raise(alert)
            self._next = dl.next_due(
                deadlines, self._raised, now, offsets_days=input.alert_offsets_days
            )
            next_key = self._next.key if self._next is not None else None
            if next_key != self._recorded_next:
                self._recorded_next = next_key
                await self._record_state()
            timeout = (self._next.due_at - now) if self._next is not None else None
            try:
                await workflow.wait_condition(self._woken, timeout=timeout)
            except TimeoutError:
                continue  # a timer fired: loop raises the due alert(s)

    # -- helpers ---------------------------------------------------------------------------------

    def _woken(self) -> bool:
        return (
            self._dead is not None or self._closed is not None or self._reload or bool(self._human)
        )

    async def _act(self, fn: Any, arg: Any) -> Any:
        return await workflow.execute_activity(
            fn, arg, start_to_close_timeout=ACTIVITY_TIMEOUT, retry_policy=ACTIVITY_RETRY
        )

    async def _raise(self, alert: dl.DeadlineAlert) -> None:
        assert self._input is not None
        await self._act(
            raise_deadline_alert,
            DeadlineAlertInput(
                contract_id=self._input.contract_id,
                deadline=alert.deadline,
                label=alert.label,
                alert_key=alert.key,
                deadline_at=alert.deadline_at.isoformat(),
            ),
        )
        self._raised.add(alert.key)

    async def _apply_human_decisions(self) -> None:
        decisions, self._human = list(self._human), []
        for d in decisions:
            if d.message_id is None or d.message_id not in self._pending_messages:
                continue
            self._pending_messages.remove(d.message_id)
            if not d.approved:
                if self._blast is not None:
                    for r in self._blast.recipients:
                        if r.message_id == d.message_id:
                            r.decision = "blocked"
                            r.reason = "rejected by human in review queue"
                continue
            sent: SendApprovedBlastResult = await self._act(
                send_approved_blast,
                SendApprovedBlastInput(
                    message_id=d.message_id, actor_id=d.actor_id, edited_body=d.edited_body
                ),
            )
            if self._blast is not None:
                for r in self._blast.recipients:
                    if r.message_id == d.message_id:
                        r.sent = sent.sent
                        r.decision = "send" if sent.sent else "blocked"
        await self._record_state()

    async def _record_state(self) -> None:
        assert self._input is not None
        await self._act(
            record_workflow_state,
            WorkflowStateInput(
                contract_id=self._input.contract_id,
                state=self._state,
                next_alert_key=self._next.key if self._next else None,
                next_alert_at=self._next.due_at.isoformat() if self._next else None,
                blast_sent=self._blast.sent if self._blast else None,
                blast_queued=self._blast.queued if self._blast else None,
            ),
        )

    async def _finish_dead(self) -> DispositionResult:
        assert self._input is not None and self._dead is not None
        self._state = STATE_DEAD
        self._next = None
        await self._act(
            deal_dead_followup,
            DealDeadFollowupInput(contract_id=self._input.contract_id, reason=self._dead.reason),
        )
        await self._record_state()
        workflow.logger.info(
            "disposition.dead",
            extra={"contract_id": self._input.contract_id, "reason": self._dead.reason},
        )
        return self._result("dead", reason=self._dead.reason)

    async def _finish_closed(self) -> DispositionResult:
        assert self._input is not None
        self._state = STATE_CLOSED
        self._next = None
        await self._record_state()
        return self._result("closed")

    def _result(self, outcome: str, *, reason: str | None = None) -> DispositionResult:
        assert self._input is not None
        return DispositionResult(
            contract_id=self._input.contract_id,
            outcome=outcome,
            blast_sent=self._blast.sent if self._blast else 0,
            blast_queued=self._blast.queued if self._blast else 0,
            alerts_raised=sorted(self._raised),
            buyer_investor_id=self._buyer,
            reason=reason,
        )

    def _note(self, name: str, payload: Any) -> None:
        record: dict[str, Any] = {"signal": name}
        if payload is not None:
            record.update(asdict(payload))
        self._last_signal = record

    # -- signals ---------------------------------------------------------------------------------

    @workflow.signal(name=SIGNAL_BUYER_RESPONSE)
    def buyer_response(self, payload: BuyerResponseSignal | None = None) -> None:
        if payload is not None:
            self._responses[payload.investor_id] = payload.response
        self._note(SIGNAL_BUYER_RESPONSE, payload)

    @workflow.signal(name=SIGNAL_BUYER_SELECTED)
    def buyer_selected(self, payload: BuyerSelectedSignal | None = None) -> None:
        if payload is not None:
            self._buyer = payload.investor_id
        if self._state == STATE_MARKETING:
            self._state = STATE_BUYER_SELECTED
        self._reload = True
        self._note(SIGNAL_BUYER_SELECTED, payload)

    @workflow.signal(name=SIGNAL_DEPOSIT_RECEIVED)
    def deposit_received(self, payload: DepositReceivedSignal | None = None) -> None:
        self._deposit = True
        if self._state in (STATE_MARKETING, STATE_BUYER_SELECTED):
            self._state = STATE_DEPOSIT_RECEIVED
        self._reload = True
        self._note(SIGNAL_DEPOSIT_RECEIVED, payload)

    @workflow.signal(name=SIGNAL_DEADLINES_UPDATED)
    def deadlines_updated(self) -> None:
        self._reload = True
        self._note(SIGNAL_DEADLINES_UPDATED, None)

    @workflow.signal
    def human_decision(self, decision: BlastHumanDecision) -> None:
        self._human.append(decision)

    @workflow.signal(name=SIGNAL_DEAL_DEAD)
    def deal_dead(self, payload: DealDeadSignal | None = None) -> None:
        if self._dead is None and self._closed is None:
            self._dead = payload or DealDeadSignal(reason="deal_dead")
        self._note(SIGNAL_DEAL_DEAD, payload)

    @workflow.signal(name=SIGNAL_DEAL_CLOSED)
    def deal_closed(self, payload: DealClosedSignal | None = None) -> None:
        if self._dead is None and self._closed is None:
            self._closed = payload or DealClosedSignal(realized_assignment_fee="0", closed_at="")
        self._note(SIGNAL_DEAL_CLOSED, payload)

    # -- query -----------------------------------------------------------------------------------

    @workflow.query
    def status(self) -> dict[str, Any]:
        return asdict(
            DispositionStatus(
                contract_id=self._input.contract_id if self._input else "",
                state=self._state,
                contract_status=self._snap.status if self._snap else "",
                blast_sent=self._blast.sent if self._blast else 0,
                blast_queued=self._blast.queued if self._blast else 0,
                blast_blocked=self._blast.blocked if self._blast else 0,
                responses=dict(self._responses),
                buyer_investor_id=self._buyer,
                deposit_received=self._deposit,
                alerts_raised=sorted(self._raised),
                next_alert_key=self._next.key if self._next else None,
                next_alert_at=self._next.due_at.isoformat() if self._next else None,
                dead_reason=self._dead.reason if self._dead else None,
                closed=self._closed is not None,
                pending_human_messages=list(self._pending_messages),
                last_signal=dict(self._last_signal) if self._last_signal else None,
            )
        )


__all__ = [
    "ACTIVITY_RETRY",
    "ACTIVITY_TIMEOUT",
    "STATE_BLASTING",
    "STATE_BUYER_SELECTED",
    "STATE_CLOSED",
    "STATE_DEAD",
    "STATE_DEPOSIT_RECEIVED",
    "STATE_MARKETING",
    "STATE_NOT_SIGNED",
    "STATE_PENDING",
    "WORKFLOW_NAME",
    "BlastHumanDecision",
    "DispositionInput",
    "DispositionResult",
    "DispositionStatus",
    "DispositionWorkflow",
    "disposition_workflow_id",
    "record_buyer_response",
]
