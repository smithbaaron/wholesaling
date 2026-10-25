"""Fixtures for the model-layer tests: a mock provider, the settings router and a runner."""

from __future__ import annotations

import pytest

from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import Message, ModelRequest, RequestMetadata, TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


@pytest.fixture
def provider() -> MockModelProvider:
    return MockModelProvider()


@pytest.fixture
def router(settings: Settings) -> ModelRouter:
    return ModelRouter.from_settings(settings)


@pytest.fixture
def runner(provider: MockModelProvider, router: ModelRouter) -> AgentRunner:
    return AgentRunner(provider, router)


def make_request(
    agent: AgentId = AgentId.A4,
    task_kind: TaskKind = TaskKind.CLASSIFY,
    *,
    system: str = "You are a test agent.",
    user: str = "Hello",
    json_schema: dict | None = None,  # type: ignore[type-arg]
    prompt_version: str = "1",
    model: str | None = None,
) -> ModelRequest:
    return ModelRequest(
        system=system,
        messages=(Message(role="user", content=user),),
        metadata=RequestMetadata(agent=agent, task_kind=task_kind, prompt_version=prompt_version),
        json_schema=json_schema,
        model=model,
    )


SIMPLE_SCHEMA: dict = {  # type: ignore[type-arg]
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["interested", "not_interested"]},
        "confidence": {"type": "number"},
        "count": {"type": "integer"},
        "flag": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "nested": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "zip": {"type": ["string", "null"]}},
            "required": ["city", "zip"],
        },
        "optional_note": {"type": "string"},
    },
    "required": ["label", "confidence", "count", "flag", "tags", "nested"],
}
