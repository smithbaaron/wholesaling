"""The ``ModelProvider`` protocol every provider implements, and the error hierarchy.

Providers are adapters: they translate a ``ModelRequest`` into one vendor call and the vendor's
reply into a ``ModelResponse``. Routing, hashing, cost and persistence live in ``runner.py`` so
they are identical for every provider (XC-02).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from wholesaling.agents.llm.models import ModelRequest, ModelResponse
from wholesaling.integrations.base import HealthStatus


class ModelError(Exception):
    """Base class for anything the model layer raises."""


class RateLimited(ModelError):
    """The provider returned 429 (or 529 overloaded) and retries were exhausted."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ProviderUnavailable(ModelError):
    """Transport failure, 5xx after retries, or an open circuit breaker (XC-08)."""


class CircuitOpen(ProviderUnavailable):
    """Raised without calling the provider while the breaker is open."""


class InvalidRequest(ModelError):
    """The provider rejected the request (4xx other than 429). Not retried."""


class StructuredOutputError(ModelError):
    """A ``json_schema`` was requested but nothing parseable came back."""


@runtime_checkable
class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one completion. ``request.model`` must be set (the router does this)."""
        ...

    async def healthcheck(self) -> HealthStatus: ...
