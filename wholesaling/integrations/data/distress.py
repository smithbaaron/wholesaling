"""Distress-list provider: what S1 needs from a vendor (REQUIREMENTS.md SEL-02, INT-04, RUL-02).

INT-04 is OPEN (PropStream, BatchLeads, county tax / probate lists are candidates). This
Protocol is the selection contract; the only implementation is the fixture-backed mock.
``DISTRESS_PROVIDER_MODE=live`` is refused in the factory until a vendor is chosen.

A ``DistressRecord`` is one parcel with the distress signals a vendor reports on it. S1 adds
the signals it can derive itself (``long_term_hold`` from ``last_sale_date``) and does every
gating decision (residential 1-4, deal band, >= 2 signals) in code, so the vendor only has to
supply facts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal, Protocol, runtime_checkable

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.data.models import MarketRef, PropertyType

FIXTURES_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures"

DistressSignalName = Literal[
    "absentee",
    "tax_delinquent",
    "pre_foreclosure",
    "probate",
    "code_violation",
    "long_term_hold",
    "vacant",
    "expired_listing",
]

# SEL-02 list criteria, in the order they are reported. ``long_term_hold`` may come from the
# vendor or be derived by S1 from ``last_sale_date`` (>= 15 years).
DISTRESS_SIGNALS: Final[tuple[str, ...]] = (
    "absentee",
    "tax_delinquent",
    "pre_foreclosure",
    "probate",
    "code_violation",
    "long_term_hold",
    "vacant",
    "expired_listing",
)


@dataclass(frozen=True, slots=True)
class DistressRecord:
    """One parcel on a distress list with the vendor-reported signals."""

    parcel_id: str
    address: str
    city: str
    state: str
    zip: str
    owner_name: str
    property_type: PropertyType
    units: int
    signals: tuple[str, ...]
    source: str
    source_ref: str
    retrieved_at: datetime
    assessed_value: Decimal | None = None
    estimated_value: Decimal | None = None
    last_sale_date: date | None = None
    owner_mailing_address: str | None = None
    owner_mailing_city: str | None = None
    owner_mailing_state: str | None = None
    owner_mailing_zip: str | None = None
    source_url: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["signals"] = list(self.signals)
        d["assessed_value"] = None if self.assessed_value is None else str(self.assessed_value)
        d["estimated_value"] = None if self.estimated_value is None else str(self.estimated_value)
        d["last_sale_date"] = (
            None if self.last_sale_date is None else self.last_sale_date.isoformat()
        )
        d["retrieved_at"] = self.retrieved_at.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DistressRecord:
        unknown = [s for s in d.get("signals", []) if s not in DISTRESS_SIGNALS]
        if unknown:
            msg = f"unknown distress signal(s) {unknown} on parcel {d.get('parcel_id')!r}"
            raise ValueError(msg)
        return cls(
            parcel_id=str(d["parcel_id"]),
            address=str(d["address"]),
            city=str(d["city"]),
            state=str(d["state"]),
            zip=str(d["zip"]),
            owner_name=str(d["owner_name"]),
            property_type=d["property_type"],
            units=int(d["units"]),
            signals=tuple(str(s) for s in d.get("signals", [])),
            source=str(d["source"]),
            source_ref=str(d["source_ref"]),
            retrieved_at=datetime.fromisoformat(d["retrieved_at"]),
            assessed_value=(
                None if d.get("assessed_value") is None else Decimal(str(d["assessed_value"]))
            ),
            estimated_value=(
                None if d.get("estimated_value") is None else Decimal(str(d["estimated_value"]))
            ),
            last_sale_date=(
                None if d.get("last_sale_date") is None else date.fromisoformat(d["last_sale_date"])
            ),
            owner_mailing_address=d.get("owner_mailing_address"),
            owner_mailing_city=d.get("owner_mailing_city"),
            owner_mailing_state=d.get("owner_mailing_state"),
            owner_mailing_zip=d.get("owner_mailing_zip"),
            source_url=d.get("source_url"),
        )


@runtime_checkable
class DistressListProvider(Protocol):
    """Distress lists per market (feeds S1)."""

    name: str
    mode: IntegrationMode

    async def distress_records(self, market: MarketRef) -> list[DistressRecord]:
        """Every parcel the vendor lists in ``market`` with at least one signal. The provider
        may return the same parcel more than once (one row per list); S1 merges them."""
        ...

    async def healthcheck(self) -> HealthStatus: ...


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass(slots=True)
class MockDistressListProvider:
    """Fixture-backed distress lists (``fixtures/distress.json``). Records every call."""

    name: str = "distress_lists"
    mode: IntegrationMode = "mock"
    records: list[DistressRecord] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    healthy: bool = True

    @classmethod
    def from_fixtures(cls, fixtures_dir: Path = FIXTURES_DIR) -> MockDistressListProvider:
        raw = _load(fixtures_dir / "distress.json")
        return cls(records=[DistressRecord.from_json(d) for d in raw])

    async def distress_records(self, market: MarketRef) -> list[DistressRecord]:
        self.calls.append({"method": "distress_records", "market": market.label})
        return [r for r in self.records if r.city == market.name and r.state == market.state]

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=self.healthy,
            detail=f"{len(self.records)} fixture distress records",
        )


__all__ = [
    "DISTRESS_SIGNALS",
    "FIXTURES_DIR",
    "DistressListProvider",
    "DistressRecord",
    "DistressSignalName",
    "MockDistressListProvider",
]
