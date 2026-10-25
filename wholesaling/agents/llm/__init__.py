"""Provider-agnostic model layer (REQUIREMENTS.md STK-07, XC-02, XC-08; build step BO-05).

v2 names this ``agents/llm.py``; it is a package so each concern has its own module:

- ``models``    request / response types, ``TaskKind``, ``prompt_hash``
- ``interface`` ``ModelProvider`` protocol and the error hierarchy
- ``routing``   ``ModelRouter``: (agent, task kind) -> model id
- ``mock``      deterministic provider for tests and the synthetic pilot
- ``anthropic`` the live provider (the only real dependency)
- ``pricing``   price table and ``estimate_cost``
- ``runner``    ``AgentRunner``: route, call, price, persist ``agent_runs``
- ``factory``   ``build_provider`` / ``build_runner`` from settings

The ``anthropic`` module is imported lazily by the factory so ``MODEL_PROVIDER=mock`` never
touches the SDK.
"""

from __future__ import annotations

from wholesaling.agents.llm.breaker import BreakerState, CircuitBreaker
from wholesaling.agents.llm.factory import build_provider, build_runner
from wholesaling.agents.llm.interface import (
    CircuitOpen,
    InvalidRequest,
    ModelError,
    ModelProvider,
    ProviderUnavailable,
    RateLimited,
    StructuredOutputError,
)
from wholesaling.agents.llm.mock import MockModelProvider, ScriptedResponse, placeholder_for_schema
from wholesaling.agents.llm.models import (
    Message,
    ModelRequest,
    ModelResponse,
    RequestMetadata,
    TaskKind,
    ToolCall,
    ToolSpec,
    prompt_hash,
)
from wholesaling.agents.llm.pricing import PRICES, estimate_cost
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner

__all__ = [
    "PRICES",
    "AgentRunner",
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpen",
    "InvalidRequest",
    "Message",
    "MockModelProvider",
    "ModelError",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ProviderUnavailable",
    "RateLimited",
    "RequestMetadata",
    "ScriptedResponse",
    "StructuredOutputError",
    "TaskKind",
    "ToolCall",
    "ToolSpec",
    "build_provider",
    "build_runner",
    "estimate_cost",
    "placeholder_for_schema",
    "prompt_hash",
]
