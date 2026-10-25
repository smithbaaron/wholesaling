"""What M1, A1 and A2 need from data providers (REQUIREMENTS.md INT-01..03, INT-07, RUL-02).

No provider has been selected (INT-03 is OPEN). These Protocols are the selection contract:
a candidate (county API, ATTOM, PropStream, BatchData) is adapted to them, and INT-02's criteria
(coverage in Active markets, API access, cost per record, licence permits automated use) decide
which adapter ships. Until then only the mock exists and ``DATA_PROVIDER_MODE=live`` is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.data.models import (
    ContactCandidate,
    DeedRecord,
    MarketRef,
    MarketSignals,
    ReferenceLists,
)


@runtime_checkable
class DeedRecordsProvider(Protocol):
    """Recorded purchases per market (feeds A1 today; M1 and S1 later)."""

    name: str
    mode: IntegrationMode

    async def purchases(self, market: MarketRef, since: date, until: date) -> list[DeedRecord]:
        """Deeds recorded in ``market`` with ``since <= sale_date <= until``.

        Must include resales *by* investors (the investor as seller) so A1 can see flip and
        wholesale patterns; A1 filters purchases from resales itself.
        """
        ...

    async def healthcheck(self) -> HealthStatus: ...


@runtime_checkable
class MarketSignalsProvider(Protocol):
    """M1 inputs per market (M1-02)."""

    name: str
    mode: IntegrationMode

    async def signals(self, market: MarketRef) -> MarketSignals:
        """Signals for one market. A market the provider does not cover returns
        ``data_available=False`` rather than raising."""
        ...

    async def healthcheck(self) -> HealthStatus: ...


@runtime_checkable
class ContactEnrichmentProvider(Protocol):
    """Professional contact channels for a resolved investor (INV-08)."""

    name: str
    mode: IntegrationMode

    async def lookup(
        self,
        entity_name: str,
        mailing_address: str | None,
        registered_agent: str | None = None,
    ) -> list[ContactCandidate]:
        """Every candidate the provider has, junk included; A2 dedupes and ranks."""
        ...

    async def healthcheck(self) -> HealthStatus: ...


@dataclass(frozen=True, slots=True)
class DataProviders:
    """The bundle handed to the discovery agents."""

    deeds: DeedRecordsProvider
    signals: MarketSignalsProvider
    contacts: ContactEnrichmentProvider
    reference_lists: ReferenceLists

    async def healthcheck(self) -> list[HealthStatus]:
        return [
            await self.deeds.healthcheck(),
            await self.signals.healthcheck(),
            await self.contacts.healthcheck(),
        ]


__all__ = [
    "ContactEnrichmentProvider",
    "DataProviders",
    "DeedRecordsProvider",
    "MarketSignalsProvider",
]
