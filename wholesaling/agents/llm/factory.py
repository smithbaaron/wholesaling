"""Build the provider and runner from settings (``MODEL_PROVIDER``).

Adding a provider: implement ``ModelProvider`` in its own module, add a branch here keyed by the
``MODEL_PROVIDER`` value, and add its model ids to ``pricing.PRICES``. Nothing else changes.
"""

from __future__ import annotations

from wholesaling.agents.llm.interface import ModelProvider
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.routing import ModelRouter, Overrides
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings


def build_provider(settings: Settings) -> ModelProvider:
    if settings.model_provider == "mock":
        return MockModelProvider()
    if settings.model_provider == "anthropic":
        from wholesaling.agents.llm.anthropic import AnthropicProvider

        return AnthropicProvider(settings)
    if settings.model_provider == "openai-compatible":
        msg = (
            "MODEL_PROVIDER=openai-compatible is reserved for a later step (Grok / OpenAI); "
            "implement ModelProvider and register it in wholesaling.agents.llm.factory"
        )
        raise NotImplementedError(msg)
    msg = f"unknown MODEL_PROVIDER {settings.model_provider!r}"  # pragma: no cover
    raise ValueError(msg)


def build_runner(
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
    overrides: Overrides | None = None,
) -> AgentRunner:
    return AgentRunner(
        provider or build_provider(settings), ModelRouter.from_settings(settings, overrides)
    )
