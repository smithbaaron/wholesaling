"""Worker registries: every entry importable and decorated; hello workflow registered."""

from __future__ import annotations

import importlib
import inspect

import pytest
from temporalio import activity, workflow
from temporalio.worker import Worker

from wholesaling.workers import main as worker_main
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS, build_worker
from wholesaling.workflows.hello import HelloWorkflow


def test_registries_are_lists() -> None:
    assert isinstance(WORKFLOWS, list)
    assert isinstance(ACTIVITIES, list)
    assert WORKFLOWS and ACTIVITIES


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda w: w.__name__)
def test_every_workflow_is_importable_and_decorated(wf: type) -> None:
    module = importlib.import_module(wf.__module__)
    assert getattr(module, wf.__name__) is wf
    defn = workflow._Definition.from_class(wf)
    assert defn is not None, f"{wf.__name__} is missing @workflow.defn"
    assert defn.run_fn is not None, f"{wf.__name__} has no @workflow.run method"


@pytest.mark.parametrize("act", ACTIVITIES, ids=lambda a: a.__name__)
def test_every_activity_is_importable_and_decorated(act: object) -> None:
    assert callable(act)
    module = importlib.import_module(act.__module__)  # type: ignore[attr-defined]
    assert getattr(module, act.__name__) is act  # type: ignore[attr-defined]
    defn = activity._Definition.from_callable(act)  # type: ignore[arg-type]
    assert defn is not None, f"{act} is missing @activity.defn"
    assert inspect.iscoroutinefunction(act), "activities in this project are async"


def test_hello_workflow_is_registered() -> None:
    assert HelloWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {"hello", "heartbeat_probe"} <= names


def test_workflow_names_are_unique() -> None:
    names = [workflow._Definition.from_class(w).name for w in WORKFLOWS]  # type: ignore[union-attr]
    assert len(names) == len(set(names))


def test_build_worker_signature_and_registries() -> None:
    sig = inspect.signature(build_worker)
    assert list(sig.parameters)[:2] == ["client", "task_queue"]
    assert sig.return_annotation in (Worker, "Worker")


def test_serve_refuses_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from wholesaling.config.settings import Settings

    s = Settings(_env_file=None, app_env="test", temporal_mode="mock")  # type: ignore[call-arg]
    with pytest.raises(SystemExit):
        asyncio.run(worker_main.serve(s))
