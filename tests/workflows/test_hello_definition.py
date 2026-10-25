"""HelloWorkflow contract checks that need no Temporal server."""

from __future__ import annotations

import inspect

from temporalio import workflow

from wholesaling.workflows.hello import HelloWorkflow


def _defn() -> workflow._Definition:
    d = workflow._Definition.from_class(HelloWorkflow)
    assert d is not None
    return d


def test_workflow_type_name() -> None:
    assert _defn().name == "HelloWorkflow"


def test_signals_and_queries_are_declared() -> None:
    d = _defn()
    assert {"pause", "resume"} <= set(d.signals)
    assert "status" in d.queries


def test_run_signature_is_name_and_delay() -> None:
    params = list(inspect.signature(HelloWorkflow.run).parameters)
    assert params == ["self", "name", "delay_seconds"]


def test_signals_mutate_state_visible_to_status() -> None:
    wf = HelloWorkflow()
    before = wf.status()
    assert before == {
        "name": "",
        "state": "waiting",
        "paused": False,
        "remaining_seconds": 0.0,
        "greeting": None,
    }
    wf.pause()
    assert wf.status()["paused"] is True
    wf.resume()
    assert wf.status()["paused"] is False
