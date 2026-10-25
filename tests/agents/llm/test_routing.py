"""ModelRouter: v2 defaults and per-agent overrides (STK-07, ENG-Q8)."""

from __future__ import annotations

import pytest

from tests.agents.llm.conftest import HAIKU, SONNET
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (TaskKind.CLASSIFY, HAIKU),
        (TaskKind.EXTRACT, HAIKU),
        (TaskKind.DRAFT, SONNET),
        (TaskKind.QA, SONNET),
        (TaskKind.COMP_AUDIT, SONNET),
    ],
)
def test_defaults_follow_v2(router: ModelRouter, task: TaskKind, expected: str) -> None:
    for agent in AgentId:
        assert router.route(agent, task) == expected


def test_settings_values_drive_defaults(settings: Settings) -> None:
    s = settings.model_copy(update={"model_draft": "grok-4"})
    router = ModelRouter.from_settings(s)
    assert router.route(AgentId.A3, TaskKind.DRAFT) == "grok-4"
    assert router.route(AgentId.A3, TaskKind.CLASSIFY) == HAIKU


def test_per_agent_task_override(settings: Settings) -> None:
    router = ModelRouter.from_settings(settings, {"A5": {"qa": "claude-opus-5-5"}})
    assert router.route(AgentId.A5, TaskKind.QA) == "claude-opus-5-5"
    assert router.route(AgentId.A5, TaskKind.DRAFT) == SONNET  # untouched
    assert router.route(AgentId.A4, TaskKind.QA) == SONNET  # other agents untouched


def test_wildcard_override_covers_every_task_for_that_agent(settings: Settings) -> None:
    router = ModelRouter.from_settings(settings, {"A2": {"*": "grok-4-mini"}})
    for task in TaskKind:
        assert router.route(AgentId.A2, task) == "grok-4-mini"
    assert router.route(AgentId.A1, TaskKind.CLASSIFY) == HAIKU


def test_specific_override_beats_wildcard(settings: Settings) -> None:
    router = ModelRouter.from_settings(settings, {"A2": {"*": "x", "draft": "y"}})
    assert router.route(AgentId.A2, TaskKind.DRAFT) == "y"
    assert router.route(AgentId.A2, TaskKind.QA) == "x"


def test_accepts_string_keys_and_enum_members(settings: Settings) -> None:
    router = ModelRouter.from_settings(settings, {AgentId.A3: {TaskKind.DRAFT: "m"}})
    assert router.route("A3", "draft") == "m"


def test_bad_agent_or_task_in_overrides_fails_fast(settings: Settings) -> None:
    with pytest.raises(ValueError):
        ModelRouter.from_settings(settings, {"Z9": {"draft": "m"}})
    with pytest.raises(ValueError):
        ModelRouter.from_settings(settings, {"A1": {"summarise": "m"}})


def test_router_requires_every_task_default() -> None:
    with pytest.raises(ValueError, match="missing"):
        ModelRouter({TaskKind.CLASSIFY: HAIKU})


def test_table_exposes_routing_as_data(settings: Settings) -> None:
    table = ModelRouter.from_settings(settings, {"A5": {"qa": "o"}}).table()
    assert table["defaults"]["classify"] == HAIKU
    assert table["overrides"] == {"A5": {"qa": "o"}}
