"""Server-free workflow simulation for the synthetic pilot.

``tests/workflows/test_investor_sequence_sim.py`` drives one ``InvestorSequenceWorkflow.run()``
with a fake ``workflow`` module whose activities are scripted. The pilot needs the same trick
for *many* workflows at once (sequences and inbound pipelines), with the **real** activities
running against the database and the mock gateways, and with simulated time so four-day
sleeps and 14-day human waits cost nothing. That is what ``WorkflowSimulator`` provides:

- a simulated clock every component reads (``GraphActivityContext.clock``,
  ``ConversationContext.clock``, ``MockGraphGateway`` and ``workflow.now()``);
- ``execute_activity`` that calls the real activity function and lets the runner observe each
  call (``ActivityObserver`` hooks);
- ``wait_condition`` that parks the workflow until a signal makes the predicate true or the
  simulated clock reaches the timeout;
- a deterministic event loop: exactly one workflow runs at a time, the earliest deadline or
  scheduled external event decides what happens next, and ties resolve in registration order,
  so a run with the same inputs always takes the same path;
- ``SimTemporalGateway``, a ``TemporalGateway`` whose ``start_workflow`` instantiates the real
  workflow class and whose ``signal`` invokes the real signal handler the way the Temporal SDK
  would (``handler(*args)`` after converting dataclass payloads by field name). A payload the
  handler cannot accept is recorded as a ``SignalContractError`` - in a real worker that
  ``TypeError`` fails the workflow task - and then re-delivered without the payload so the
  pilot can keep measuring everything else.

The ``temporalio.workflow`` module is swapped on the two workflow modules for the duration of
``patched_workflow_modules`` only; nothing is monkeypatched globally.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import heapq
import inspect
import logging
import typing
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from wholesaling.integrations.temporal.interface import WorkflowHandleInfo
from wholesaling.integrations.temporal.mock import MockTemporalGateway
from wholesaling.workflows import graph_maintenance as gm
from wholesaling.workflows import investor_sequence as ws

log = logging.getLogger("wholesaling.pilot.sim")

_current_workflow: ContextVar[str | None] = ContextVar("pilot_current_workflow", default=None)

Event = Callable[[], Awaitable[None]]
ActivityObserver = Callable[["ActivityCall"], Awaitable[None]]


@dataclass(slots=True)
class ActivityCall:
    """One activity invocation the simulator executed."""

    workflow_id: str
    name: str
    arg: Any
    result: Any
    at: datetime
    seq: int


@dataclass(slots=True)
class SignalContractError:
    """A signal whose payload the handler could not accept (would fail the workflow task)."""

    workflow_id: str
    signal: str
    payload_types: tuple[str, ...]
    handler_params: int
    error: str


@dataclass(slots=True)
class _Waiter:
    workflow_id: str
    pred: Callable[[], bool]
    deadline: datetime | None
    future: asyncio.Future[bool]
    order: int


@dataclass(order=True)
class _ScheduledEvent:
    at: datetime
    order: int
    label: str = field(compare=False)
    fn: Event = field(compare=False)


class WorkflowStuck(RuntimeError):
    """Raised when every workflow waits with no timeout and nothing is scheduled."""


class WorkflowSimulator:
    def __init__(self, start: datetime, *, horizon: timedelta = timedelta(days=60)) -> None:
        self.clock = start
        self.start = start
        self.horizon = start + horizon
        self.instances: dict[str, Any] = {}
        self.results: dict[str, Any] = {}
        self.errors: dict[str, BaseException] = {}
        self.activity_calls: list[ActivityCall] = []
        self.signal_contract_errors: list[SignalContractError] = []
        self.signals_delivered: list[tuple[str, str]] = []
        self.observers: list[ActivityObserver] = []
        self._waiters: list[_Waiter] = []
        self._events: list[_ScheduledEvent] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._order = 0
        self._stuck_at_end: list[_Waiter] = []

    # -- clock -----------------------------------------------------------------------------

    def now(self) -> datetime:
        return self.clock

    def elapsed(self) -> timedelta:
        return self.clock - self.start

    # -- workflows -------------------------------------------------------------------------

    def start_workflow(self, workflow_id: str, instance: Any, coro: Awaitable[Any]) -> bool:
        """Run ``coro`` (the instance's ``run``) as a simulated workflow. A second start with
        the same id is a no-op, like Temporal's default id-reuse policy for a running workflow."""
        if workflow_id in self.instances:
            return False
        self.instances[workflow_id] = instance
        self._inc()
        self._tasks[workflow_id] = asyncio.create_task(
            self._run(workflow_id, coro), name=f"pilot-wf:{workflow_id}"
        )
        return True

    async def _run(self, workflow_id: str, coro: Awaitable[Any]) -> None:
        _current_workflow.set(workflow_id)
        try:
            self.results[workflow_id] = await coro
        except Exception as exc:
            log.exception("pilot.workflow_failed", extra={"workflow_id": workflow_id})
            self.errors[workflow_id] = exc
        finally:
            self._dec()

    def current_workflow_id(self) -> str | None:
        return _current_workflow.get()

    def is_done(self, workflow_id: str) -> bool:
        return workflow_id in self.results or workflow_id in self.errors

    # -- the workflow module surface ---------------------------------------------------------

    async def execute_activity(self, fn: Any, arg: Any, **_: Any) -> Any:
        wf = _current_workflow.get() or "?"
        result = await fn(arg)
        self._order += 1
        call = ActivityCall(
            workflow_id=wf, name=fn.__name__, arg=arg, result=result, at=self.clock, seq=self._order
        )
        self.activity_calls.append(call)
        for observer in list(self.observers):
            await observer(call)
        return result

    async def wait_condition(
        self,
        pred: Callable[[], bool],
        timeout: timedelta | None = None,  # noqa: ASYNC109 - mirrors workflow.wait_condition
    ) -> None:
        if pred():
            return
        wf = _current_workflow.get() or "?"
        self._order += 1
        waiter = _Waiter(
            workflow_id=wf,
            pred=pred,
            deadline=(self.clock + timeout) if timeout is not None else None,
            future=asyncio.get_running_loop().create_future(),
            order=self._order,
        )
        self._waiters.append(waiter)
        self._dec()  # this workflow is parked; the scheduler may proceed
        ok = await waiter.future
        if not ok:
            raise TimeoutError

    # -- scheduling ----------------------------------------------------------------------------

    def schedule(self, at: datetime, fn: Event, *, label: str = "") -> None:
        """Run ``fn`` inline in the scheduler at simulated time ``at`` (never earlier than now).
        Events may ``await self.settle()`` to let the workflows they started reach their next
        wait before continuing."""
        self._order += 1
        heapq.heappush(self._events, _ScheduledEvent(max(at, self.clock), self._order, label, fn))

    def schedule_in(self, delay: timedelta, fn: Event, *, label: str = "") -> None:
        self.schedule(self.clock + delay, fn, label=label)

    async def settle(self) -> None:
        """Wait until every workflow is parked in ``wait_condition`` or finished."""
        while self._active > 0:
            await self._idle.wait()

    async def run(self, on_idle: Callable[[], Awaitable[bool]] | None = None) -> None:
        """Drive the simulation until nothing can happen any more.

        Order at each quiescent point: (1) wake a waiter whose predicate became true (a signal
        arrived); (2) ``on_idle`` (the pilot's operator: approvals, policy flips) - it returns
        True when it scheduled or changed something; (3) the earliest of the next scheduled
        event and the next waiter deadline, advancing the clock to it.
        """
        while True:
            await self.settle()
            if self._wake_first_satisfied():
                continue
            if on_idle is not None and await on_idle():
                continue
            next_event = self._events[0] if self._events else None
            timed = [w for w in self._waiters if w.deadline is not None]
            next_waiter = min(timed, key=lambda w: (w.deadline, w.order)) if timed else None
            if next_event is None and next_waiter is None:
                self._stuck_at_end = list(self._waiters)
                return
            if next_event is not None and (
                next_waiter is None
                or next_waiter.deadline is None
                or next_event.at <= next_waiter.deadline
            ):
                heapq.heappop(self._events)
                self._advance(next_event.at)
                log.debug("pilot.event", extra={"label": next_event.label, "at": str(self.clock)})
                await next_event.fn()
                continue
            assert next_waiter is not None and next_waiter.deadline is not None
            self._advance(next_waiter.deadline)
            self._wake(next_waiter, next_waiter.pred())

    @property
    def parked_without_timeout(self) -> list[str]:
        """Workflow ids still waiting forever when the run ended (e.g. awaiting a human)."""
        return [w.workflow_id for w in self._stuck_at_end if w.deadline is None]

    @property
    def parked(self) -> list[str]:
        return [w.workflow_id for w in self._stuck_at_end]

    # -- internals -----------------------------------------------------------------------------

    def _advance(self, to: datetime) -> None:
        if to > self.horizon:
            msg = f"simulation passed its horizon ({self.horizon.isoformat()}) at {to.isoformat()}"
            raise WorkflowStuck(msg)
        if to > self.clock:
            self.clock = to

    def _wake_first_satisfied(self) -> bool:
        for waiter in sorted(self._waiters, key=lambda w: w.order):
            if waiter.pred():
                self._wake(waiter, True)
                return True
        return False

    def _wake(self, waiter: _Waiter, ok: bool) -> None:
        self._waiters.remove(waiter)
        self._inc()
        waiter.future.set_result(ok)

    def _inc(self) -> None:
        self._active += 1
        self._idle.clear()

    def _dec(self) -> None:
        self._active -= 1
        if self._active <= 0:
            self._active = 0
            self._idle.set()


# --------------------------------------------------------------------------------------------
# The ``temporalio.workflow`` stand-in
# --------------------------------------------------------------------------------------------


class _Unsafe:
    @staticmethod
    @contextlib.contextmanager
    def imports_passed_through() -> Iterator[None]:
        yield


class WorkflowModuleShim:
    """What ``workflows/*.py`` need from ``temporalio.workflow`` at run time."""

    unsafe = _Unsafe

    def __init__(self, sim: WorkflowSimulator) -> None:
        self._sim = sim
        self.logger = logging.getLogger("wholesaling.pilot.workflow")

    def now(self) -> datetime:
        return self._sim.clock

    async def execute_activity(self, fn: Any, arg: Any, **kwargs: Any) -> Any:
        return await self._sim.execute_activity(fn, arg, **kwargs)

    async def wait_condition(
        self,
        pred: Callable[[], bool],
        timeout: timedelta | None = None,  # noqa: ASYNC109 - mirrors workflow.wait_condition
    ) -> None:
        await self._sim.wait_condition(pred, timeout)

    def info(self) -> Any:
        return SimpleNamespace(workflow_id=self._sim.current_workflow_id() or "", run_id="sim")


@contextlib.asynccontextmanager
async def patched_workflow_modules(sim: WorkflowSimulator) -> AsyncIterator[WorkflowModuleShim]:
    """Point both workflow modules' ``workflow`` name at the simulator for the duration."""
    shim = WorkflowModuleShim(sim)
    saved = [(mod, mod.workflow) for mod in (ws, gm)]
    for mod, _ in saved:
        setattr(mod, "workflow", shim)  # noqa: B010 - deliberate module-attribute swap
    try:
        yield shim
    finally:
        for mod, original in saved:
            setattr(mod, "workflow", original)  # noqa: B010


# --------------------------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------------------------

WORKFLOW_CLASSES: dict[str, type[Any]] = {
    ws.WORKFLOW_NAME: ws.InvestorSequenceWorkflow,
    "GraphInboundMessageWorkflow": gm.GraphInboundMessageWorkflow,
}


class SimTemporalGateway(MockTemporalGateway):
    """``TemporalGateway`` that runs workflows inside a ``WorkflowSimulator``.

    Records every call like the plain mock (``calls``) so the review route, the webhook and the
    conversation activities see exactly the surface they see in tests.
    """

    def __init__(self, sim: WorkflowSimulator, *, task_queue: str = "pilot") -> None:
        super().__init__(task_queue=task_queue)
        self._sim = sim

    async def start_workflow(
        self,
        workflow: str,
        args: Sequence[Any],
        *,
        workflow_id: str,
        task_queue: str | None = None,
    ) -> WorkflowHandleInfo:
        info = await super().start_workflow(
            workflow, args, workflow_id=workflow_id, task_queue=task_queue
        )
        cls = WORKFLOW_CLASSES.get(workflow)
        if cls is None:
            msg = f"the pilot simulator has no workflow named {workflow!r}"
            raise LookupError(msg)
        instance = cls()
        self._sim.start_workflow(workflow_id, instance, instance.run(*args))
        return info

    async def signal(self, workflow_id: str, signal: str, *args: Any) -> None:
        await super().signal(workflow_id, signal, *args)
        instance = self._sim.instances.get(workflow_id)
        if instance is None:
            msg = f"workflow {workflow_id} not found"
            raise LookupError(msg)
        if self._sim.is_done(workflow_id):
            # Temporal rejects signals to closed workflows; the review route turns that into a
            # 502 and the activities' retry policy would surface it. Keep the same failure.
            msg = f"workflow {workflow_id} already completed"
            raise RuntimeError(msg)
        handler = getattr(instance, signal, None)
        if handler is None:
            msg = f"workflow {workflow_id} has no signal {signal!r}"
            raise LookupError(msg)
        await self._deliver(workflow_id, signal, handler, list(args))
        self._sim.signals_delivered.append((workflow_id, signal))

    async def query(self, workflow_id: str, query: str, *args: Any) -> Any:
        await super().query(workflow_id, query, *args)
        instance = self._sim.instances.get(workflow_id)
        if instance is None:
            msg = f"workflow {workflow_id} not found"
            raise LookupError(msg)
        result = getattr(instance, query)(*args)
        return await result if inspect.isawaitable(result) else result

    async def _deliver(
        self, workflow_id: str, signal: str, handler: Callable[..., Any], args: list[Any]
    ) -> None:
        params = [
            p
            for p in inspect.signature(handler).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        if len(args) > len(params):
            # The SDK calls ``handler(*args)``: this TypeError would fail the workflow task.
            try:
                handler(*args)
            except TypeError as exc:
                self._sim.signal_contract_errors.append(
                    SignalContractError(
                        workflow_id=workflow_id,
                        signal=signal,
                        payload_types=tuple(type(a).__name__ for a in args),
                        handler_params=len(params),
                        error=str(exc),
                    )
                )
            args = args[: len(params)]
        hints = _type_hints(handler)
        converted = [
            _convert_payload(arg, hints.get(param.name))
            for arg, param in zip(args, params, strict=False)
        ]
        result = handler(*converted)
        if inspect.isawaitable(result):
            await result


def _type_hints(handler: Callable[..., Any]) -> dict[str, Any]:
    try:
        return typing.get_type_hints(handler)
    except Exception:
        return {}


def _convert_payload(value: Any, hint: Any) -> Any:
    """Map a dataclass payload onto the handler's dataclass by field name (JSON semantics:
    unknown fields are dropped, missing ones take their defaults)."""
    if not (isinstance(hint, type) and dataclasses.is_dataclass(hint)):
        return value
    target: type[Any] = hint
    if isinstance(value, target):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        data = dataclasses.asdict(value)
    elif isinstance(value, dict):
        data = dict(value)
    else:
        return value
    names = {f.name for f in dataclasses.fields(target)}
    return target(**{k: v for k, v in data.items() if k in names})


__all__ = [
    "WORKFLOW_CLASSES",
    "ActivityCall",
    "ActivityObserver",
    "SignalContractError",
    "SimTemporalGateway",
    "WorkflowModuleShim",
    "WorkflowSimulator",
    "WorkflowStuck",
    "patched_workflow_modules",
]
