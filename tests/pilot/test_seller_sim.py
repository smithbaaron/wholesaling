"""The seller-side simulator additions: deferred starts, child workflows, pre-activity observers
and start-payload conversion by field name (``pilot/sim.py``, appended for BO-14)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from wholesaling.pilot import sim as pilot_sim
from wholesaling.pilot.sim import (
    SELLER_WORKFLOW_MODULES,
    WORKFLOW_CLASSES,
    DeferredStartSimGateway,
    SellerWorkflowModuleShim,
    SellerWorkflowSimulator,
    convert_run_args,
    patched_workflow_modules_for,
)
from wholesaling.workflows import (
    disposition,
    negotiation,
    offer,
    seller_inbound,
    seller_sequence,
)

START = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


@dataclass(slots=True)
class ChildInput:
    name: str
    extra: int = 0


@dataclass(slots=True)
class ForeignChildInput:  # the caller's own copy of the input (field-compatible)
    name: str
    extra: int = 0
    unknown: str = "dropped"


async def _act(arg: Any) -> Any:
    return {"echo": arg}


class ChildWorkflow:
    def __init__(self) -> None:
        self.seen: ChildInput | None = None

    async def run(self, input: ChildInput) -> str:
        self.seen = input
        result = await _shim().execute_activity(_act, input.name)
        return f"child:{input.name}:{result['echo']}"


class ParentWorkflow:
    async def run(self, input: ChildInput) -> str:
        child = await _shim().execute_child_workflow(
            "PilotTestChild", ForeignChildInput(name=input.name, extra=7), id=f"child:{input.name}"
        )
        return f"parent({child})"


_current_shim: SellerWorkflowModuleShim | None = None


def _shim() -> SellerWorkflowModuleShim:
    assert _current_shim is not None
    return _current_shim


@pytest.fixture
def registered() -> Any:
    saved = dict(WORKFLOW_CLASSES)
    WORKFLOW_CLASSES["PilotTestChild"] = ChildWorkflow
    WORKFLOW_CLASSES["PilotTestParent"] = ParentWorkflow
    yield None
    WORKFLOW_CLASSES.clear()
    WORKFLOW_CLASSES.update(saved)


def test_seller_workflow_classes_and_modules_are_registered() -> None:
    assert WORKFLOW_CLASSES[seller_sequence.WORKFLOW_NAME] is seller_sequence.SellerSequenceWorkflow
    assert WORKFLOW_CLASSES[seller_inbound.WORKFLOW_NAME] is seller_inbound.SellerInboundWorkflow
    assert WORKFLOW_CLASSES[offer.WORKFLOW_NAME] is offer.OfferWorkflow
    assert WORKFLOW_CLASSES[negotiation.WORKFLOW_NAME] is negotiation.NegotiationWorkflow
    assert WORKFLOW_CLASSES[disposition.WORKFLOW_NAME] is disposition.DispositionWorkflow
    assert {seller_sequence, seller_inbound, offer, negotiation, disposition} <= set(
        SELLER_WORKFLOW_MODULES
    )


def test_convert_run_args_maps_a_foreign_dataclass_onto_the_run_annotation() -> None:
    converted = convert_run_args(ChildWorkflow(), [ForeignChildInput(name="x", extra=3)])
    assert converted == [ChildInput(name="x", extra=3)]
    # A payload already of the right type, or one without an annotation, passes through.
    assert convert_run_args(ChildWorkflow(), [ChildInput("y")]) == [ChildInput("y")]
    assert convert_run_args(object(), ["raw"]) == ["raw"]


async def test_deferred_start_runs_at_the_next_turn_and_pre_observers_see_activities(
    registered: Any,
) -> None:
    global _current_shim
    sim = SellerWorkflowSimulator(START, horizon=timedelta(days=1))
    gateway = DeferredStartSimGateway(sim)
    seen: list[tuple[str, str, Any]] = []

    async def pre(workflow_id: str, name: str, arg: Any) -> None:
        seen.append((workflow_id, name, arg))

    sim.pre_observers.append(pre)
    async with patched_workflow_modules_for(sim, [seller_sequence]) as shim:
        _current_shim = shim
        info = await gateway.start_workflow(
            "PilotTestChild", [ForeignChildInput(name="a")], workflow_id="child:a"
        )
        assert info.workflow_id == "child:a"
        # Reserved but not yet running: a second start before the turn is a no-op.
        assert "child:a" in sim.instances and "child:a" not in sim.results
        await gateway.start_workflow("PilotTestChild", [ChildInput("a")], workflow_id="child:a")
        await sim.run()
    assert sim.results["child:a"] == "child:a:a"
    assert seen == [("child:a", "_act", "a")]
    assert sim.errors == {} and gateway.calls[0].method == "start_workflow"
    _current_shim = None


async def test_child_workflow_parks_the_parent_until_the_child_finishes(registered: Any) -> None:
    global _current_shim
    sim = SellerWorkflowSimulator(START, horizon=timedelta(days=1))
    gateway = DeferredStartSimGateway(sim)
    async with patched_workflow_modules_for(sim, [seller_sequence]) as shim:
        _current_shim = shim
        await gateway.start_workflow("PilotTestParent", [ChildInput("p")], workflow_id="parent:p")
        await sim.run()
    assert sim.results["parent:p"] == "parent(child:p:p)"
    child = sim.instances["child:p"]
    assert isinstance(child, ChildWorkflow)
    assert child.seen == ChildInput(name="p", extra=7)  # the unknown field was dropped
    assert sim.child_starts == [("parent:p", "child:p")]
    # The activity ran under the child's workflow id, not the parent's.
    assert [(c.workflow_id, c.name) for c in sim.activity_calls] == [("child:p", "_act")]
    _current_shim = None


async def test_patched_modules_are_restored_and_the_investor_patch_is_untouched() -> None:
    sim = SellerWorkflowSimulator(START)
    originals = {mod: mod.workflow for mod in SELLER_WORKFLOW_MODULES}
    async with patched_workflow_modules_for(sim) as shim:
        assert isinstance(shim, SellerWorkflowModuleShim)
        for mod in SELLER_WORKFLOW_MODULES:
            assert mod.workflow is shim
    for mod, original in originals.items():
        assert mod.workflow is original
    # The investor helper still patches exactly its two modules.
    async with pilot_sim.patched_workflow_modules(sim) as investor_shim:
        assert pilot_sim.ws.workflow is investor_shim and pilot_sim.gm.workflow is investor_shim
        assert offer.workflow is originals[offer]
