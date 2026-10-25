"""Per-agent, per-task model routing (REQUIREMENTS.md STK-07, ENG-Q8).

Resolution order for ``route(agent, task_kind)``:

1. ``overrides[agent][task_kind]``
2. ``overrides[agent]["*"]`` (one model for everything that agent does)
3. the settings default for the task kind (``MODEL_CLASSIFY`` ... ``MODEL_COMP_AUDIT``)

Defaults per v2: Haiku for classify/extract, Sonnet for draft/qa/comp_audit. Overrides come
from an optional plain dict (a YAML tunable later) so a single agent can be moved to another
model, or another provider's model, without touching code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wholesaling.agents.llm.models import TaskKind
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId

ALL_TASKS = "*"

# Overrides shape: {"A5": {"qa": "claude-opus-5-5"}, "A3": {"*": "claude-sonnet-5-5"}}
Overrides = Mapping[str, Mapping[str, str]]


class ModelRouter:
    def __init__(
        self, defaults: Mapping[TaskKind, str], overrides: Overrides | None = None
    ) -> None:
        missing = [k for k in TaskKind if k not in defaults]
        if missing:
            msg = f"ModelRouter needs a default for every TaskKind; missing {missing}"
            raise ValueError(msg)
        self._defaults: dict[TaskKind, str] = dict(defaults)
        self._overrides: dict[str, dict[str, str]] = {}
        for agent, by_task in (overrides or {}).items():
            self._overrides[self._agent_key(agent)] = {
                self._task_key(task): model for task, model in by_task.items()
            }

    @classmethod
    def from_settings(cls, settings: Settings, overrides: Overrides | None = None) -> ModelRouter:
        return cls(
            {
                TaskKind.CLASSIFY: settings.model_classify,
                TaskKind.EXTRACT: settings.model_extract,
                TaskKind.DRAFT: settings.model_draft,
                TaskKind.QA: settings.model_qa,
                TaskKind.COMP_AUDIT: settings.model_comp_audit,
            },
            overrides,
        )

    def route(self, agent: AgentId | str, task_kind: TaskKind | str) -> str:
        agent_key = self._agent_key(agent)
        task_key = self._task_key(task_kind)
        per_agent = self._overrides.get(agent_key)
        if per_agent:
            if task_key in per_agent:
                return per_agent[task_key]
            if ALL_TASKS in per_agent:
                return per_agent[ALL_TASKS]
        return self._defaults[TaskKind(task_key)]

    def default_for(self, task_kind: TaskKind) -> str:
        return self._defaults[task_kind]

    def table(self) -> dict[str, Any]:
        """Routing as data, for the health endpoint / docs."""
        return {
            "defaults": {k.value: v for k, v in self._defaults.items()},
            "overrides": {a: dict(t) for a, t in self._overrides.items()},
        }

    @staticmethod
    def _agent_key(agent: AgentId | str) -> str:
        value = agent.value if isinstance(agent, AgentId) else str(agent)
        # Validate so a typo in config fails at startup, not at the first run.
        return AgentId(value).value

    @staticmethod
    def _task_key(task: TaskKind | str) -> str:
        value = task.value if isinstance(task, TaskKind) else str(task)
        if value == ALL_TASKS:
            return value
        return TaskKind(value).value
