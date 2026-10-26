"""Skip-trace and DNC providers: what S2 needs from a vendor (REQUIREMENTS.md SEL-03, INT-05,
CMP-02, RUL-02, RUL-06).

INT-05 is OPEN (BatchSkipTracing, Tracerfy, PropStream are candidates; PRE-11 selects one).
These Protocols are the selection contract; only the fixture-backed mocks exist and
``SKIPTRACE_PROVIDER_MODE=live`` is refused in the factory.

The DNC list is modelled although SMS never sends in this build: SEL-03 and CMP-02 require the
scrub to be *recorded* on every traced lead before any SMS could ever be considered, so the
record exists from day one and a later SMS build cannot skip it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from wholesaling.db.normalize import normalize_entity_name, normalize_phone
from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.data.models import ContactCandidate

FIXTURES_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True, slots=True)
class SkipTraceResult:
    """One vendor lookup. ``contacts`` keep the vendor's provenance and confidence per channel
    (SEL-03); S2 normalises, dedupes and scrubs them."""

    owner_name: str
    contacts: tuple[ContactCandidate, ...]
    vendor: str
    vendor_ref: str | None
    retrieved_at: datetime

    @property
    def found_any(self) -> bool:
        return bool(self.contacts)

    def to_json(self) -> dict[str, Any]:
        return {
            "owner_name": self.owner_name,
            "contacts": [c.to_json() for c in self.contacts],
            "vendor": self.vendor,
            "vendor_ref": self.vendor_ref,
            "retrieved_at": self.retrieved_at.isoformat(),
        }


@runtime_checkable
class SkipTraceProvider(Protocol):
    """Owner -> email / phone / mailing address with provenance (SEL-03)."""

    name: str
    mode: IntegrationMode

    async def trace(
        self,
        owner_name: str,
        property_address: str,
        mailing_address: str | None = None,
    ) -> SkipTraceResult:
        """Every contact the vendor has for the owner, junk included; S2 filters."""
        ...

    async def healthcheck(self) -> HealthStatus: ...


@runtime_checkable
class DncListProvider(Protocol):
    """Do-not-call scrub (CMP-02). Phones only; email has no DNC registry."""

    name: str
    mode: IntegrationMode

    async def is_listed(self, phone: str) -> bool: ...

    async def healthcheck(self) -> HealthStatus: ...


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass(slots=True)
class MockSkipTraceProvider:
    """Fixture-backed skip trace (``fixtures/skiptrace.json``), keyed by the normalised owner
    name. An owner the fixture does not know yields an empty result (the "unreachable" path)."""

    name: str = "skip_trace"
    mode: IntegrationMode = "mock"
    by_owner_key: dict[str, list[ContactCandidate]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    healthy: bool = True
    retrieved_at: datetime = field(
        default_factory=lambda: datetime.fromisoformat("2026-10-01T06:00:00+00:00")
    )

    @classmethod
    def from_fixtures(cls, fixtures_dir: Path = FIXTURES_DIR) -> MockSkipTraceProvider:
        raw = _load(fixtures_dir / "skiptrace.json")
        return cls(
            by_owner_key={
                normalize_entity_name(k): [ContactCandidate.from_json(c) for c in v]
                for k, v in raw.items()
            }
        )

    async def trace(
        self,
        owner_name: str,
        property_address: str,
        mailing_address: str | None = None,
    ) -> SkipTraceResult:
        self.calls.append(
            {
                "method": "trace",
                "owner_name": owner_name,
                "property_address": property_address,
                "mailing_address": mailing_address,
            }
        )
        key = normalize_entity_name(owner_name)
        contacts = tuple(self.by_owner_key.get(key, []))
        return SkipTraceResult(
            owner_name=owner_name,
            contacts=contacts,
            vendor=self.name,
            vendor_ref=f"mock-trace:{key}" if contacts else None,
            retrieved_at=self.retrieved_at,
        )

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=self.healthy,
            detail=f"{len(self.by_owner_key)} owners with contacts",
        )


@dataclass(slots=True)
class MockDncListProvider:
    """Fixture-backed DNC list (``fixtures/dnc.json``: a list of phone numbers)."""

    name: str = "dnc_list"
    mode: IntegrationMode = "mock"
    listed: frozenset[str] = frozenset()
    calls: list[dict[str, Any]] = field(default_factory=list)
    healthy: bool = True

    @classmethod
    def from_fixtures(cls, fixtures_dir: Path = FIXTURES_DIR) -> MockDncListProvider:
        raw = _load(fixtures_dir / "dnc.json")
        numbers: set[str] = set()
        for value in raw.get("phones", []):
            try:
                numbers.add(normalize_phone(str(value)))
            except ValueError:
                continue
        return cls(listed=frozenset(numbers))

    async def is_listed(self, phone: str) -> bool:
        self.calls.append({"method": "is_listed", "phone": phone})
        try:
            return normalize_phone(phone) in self.listed
        except ValueError:
            return False

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(
            name=self.name, mode=self.mode, ok=self.healthy, detail=f"{len(self.listed)} numbers"
        )


__all__ = [
    "FIXTURES_DIR",
    "DncListProvider",
    "MockDncListProvider",
    "MockSkipTraceProvider",
    "SkipTraceProvider",
    "SkipTraceResult",
]
