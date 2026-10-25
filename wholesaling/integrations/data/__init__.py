"""Data providers (deeds, market signals, contact enrichment): interface, mock, factory.

REQUIREMENTS.md INT-01..03, INT-07. ``DATA_PROVIDER_MODE`` selects the implementation; only
``mock`` exists until a provider is selected under INT-02 (see docs/agents/discovery.md).
"""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.data.interface import (
    ContactEnrichmentProvider,
    DataProviders,
    DeedRecordsProvider,
    MarketSignalsProvider,
)
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.data.models import (
    ContactCandidate,
    DeedRecord,
    DistressSignals,
    MarketRef,
    MarketSignals,
    ReferenceLists,
)


def get_data_providers(settings: Settings) -> DataProviders:
    """Return the provider bundle selected by ``DATA_PROVIDER_MODE``."""
    if settings.data_provider_mode == "mock":
        return mock_data_providers()
    # INT-03 is OPEN: no deed, signals or enrichment provider has been selected. When one is,
    # add ``integrations/data/real_<vendor>.py`` and construct it here, never elsewhere.
    msg = (
        "DATA_PROVIDER_MODE=live is not available: no data provider has been selected yet "
        "(REQUIREMENTS.md INT-03). Use DATA_PROVIDER_MODE=mock."
    )
    raise NotImplementedError(msg)


__all__ = [
    "ContactCandidate",
    "ContactEnrichmentProvider",
    "DataProviders",
    "DeedRecord",
    "DeedRecordsProvider",
    "DistressSignals",
    "MarketRef",
    "MarketSignals",
    "MarketSignalsProvider",
    "ReferenceLists",
    "get_data_providers",
]
