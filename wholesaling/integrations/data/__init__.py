"""Data providers (deeds, market signals, contact enrichment): interface, mock, factory.

REQUIREMENTS.md INT-01..03, INT-07. ``DATA_PROVIDER_MODE`` selects the implementation; only
``mock`` exists until a provider is selected under INT-02 (see docs/agents/discovery.md).
"""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.data.distress import DistressListProvider, DistressRecord
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
    ParcelRecord,
    ReferenceLists,
)
from wholesaling.integrations.data.property import PropertyDataProvider, mock_property_provider
from wholesaling.integrations.data.skiptrace import (
    DncListProvider,
    SkipTraceProvider,
    SkipTraceResult,
)


def get_property_data_provider(settings: Settings) -> PropertyDataProvider:
    """A7 / D2 property data (BO-11, append-only): facts, comps, AVM, listings. Follows
    ``DATA_PROVIDER_MODE``; only the fixture mock exists until a provider is selected (INT-03)."""
    if settings.data_provider_mode == "mock":
        return mock_property_provider()
    msg = (
        "DATA_PROVIDER_MODE=live has no property data provider yet "
        "(REQUIREMENTS.md INT-03). Use DATA_PROVIDER_MODE=mock."
    )
    raise NotImplementedError(msg)


def get_data_providers(settings: Settings) -> DataProviders:
    """Return the provider bundle selected by ``DATA_PROVIDER_MODE``."""
    if settings.data_provider_mode == "mock":
        # BO-10 (append-only): the seller-side providers ride on the same bundle. Their own
        # mode switches are refused when not ``mock`` because INT-04 / INT-05 are OPEN.
        for key, value in (
            ("DISTRESS_PROVIDER_MODE", settings.distress_provider_mode),
            ("SKIPTRACE_PROVIDER_MODE", settings.skiptrace_provider_mode),
        ):
            if value != "mock":
                msg = (
                    f"{key}=live is not available: no vendor has been selected yet "
                    "(REQUIREMENTS.md INT-04 / INT-05, PRE-11). Use mock."
                )
                raise NotImplementedError(msg)
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
    "DistressListProvider",
    "DistressRecord",
    "DistressSignals",
    "DncListProvider",
    "MarketRef",
    "MarketSignals",
    "MarketSignalsProvider",
    "ParcelRecord",
    "PropertyDataProvider",
    "ReferenceLists",
    "SkipTraceProvider",
    "SkipTraceResult",
    "get_data_providers",
    "get_property_data_provider",
]
