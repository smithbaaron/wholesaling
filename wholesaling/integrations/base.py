"""Common shape for every integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

IntegrationMode = Literal["mock", "live", "off"]


@dataclass(frozen=True, slots=True)
class HealthStatus:
    name: str
    mode: IntegrationMode
    ok: bool
    detail: str = ""


@runtime_checkable
class Integration(Protocol):
    name: str
    mode: IntegrationMode

    async def healthcheck(self) -> HealthStatus: ...
