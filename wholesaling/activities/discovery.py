"""Discovery activities: M1 rescore, A1 per-market discovery, A2 per-investor research.

Each wraps one agent in one DB session (REQUIREMENTS.md WF-08, WF-09). ``run_a1`` takes the
per-market discovery lock before doing anything (WF-09 "no overlapping runs"): the lock is a
timestamp in ``markets.signals["a1_running_since"]``, committed in its own short transaction
so a second worker sees it, and expires after ``DEFAULT_LOCK_TTL`` so a crashed run cannot
block tomorrow's schedule. The agents themselves stay plain classes; tests call them directly.

Dependencies (session factory, providers) come from ``get_context()`` so tests can inject an
in-memory database and the fixture providers with ``configure()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.agents.a1 import A1InvestorDiscovery
from wholesaling.agents.a2 import A2ContactResearch
from wholesaling.agents.m1 import M1MarketSelection
from wholesaling.db.repositories.investors import InvestorRepository
from wholesaling.db.repositories.markets import DEFAULT_LOCK_TTL, MarketRepository
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.logging import bind_correlation_id, clear_context, get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class DiscoveryContext:
    session_factory: async_sessionmaker[AsyncSession]
    providers: DataProviders


_context: DiscoveryContext | None = None


def _default_context() -> DiscoveryContext:
    from wholesaling.config.settings import get_settings
    from wholesaling.db.engine import get_session_factory
    from wholesaling.integrations.data import get_data_providers

    return DiscoveryContext(
        session_factory=get_session_factory(), providers=get_data_providers(get_settings())
    )


def configure(
    session_factory: async_sessionmaker[AsyncSession], providers: DataProviders
) -> DiscoveryContext:
    """Inject dependencies (tests, synthetic pilot). ``reset()`` restores settings-driven wiring."""
    global _context
    _context = DiscoveryContext(session_factory=session_factory, providers=providers)
    return _context


def reset() -> None:
    global _context
    _context = None


def get_context() -> DiscoveryContext:
    global _context
    if _context is None:
        _context = _default_context()
    return _context


def _bind() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


class DiscoveryLocked(RuntimeError):
    """Raised by ``discovery_lock`` when another A1 run holds the market."""


class discovery_lock:
    """Per-market A1 guard (WF-09). Acquire and release each commit in their own transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        market_id: uuid.UUID,
        *,
        now: datetime | None = None,
        ttl: Any = DEFAULT_LOCK_TTL,
    ) -> None:
        self._sf = session_factory
        self._market_id = market_id
        self._now = now
        self._ttl = ttl

    async def __aenter__(self) -> discovery_lock:
        async with self._sf() as s, s.begin():
            ok = await MarketRepository(s).acquire_discovery_lock(
                self._market_id, now=self._now, ttl=self._ttl
            )
        if not ok:
            msg = f"A1 already running for market {self._market_id}"
            raise DiscoveryLocked(msg)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        async with self._sf() as s, s.begin():
            await MarketRepository(s).release_discovery_lock(self._market_id)


@activity.defn(name="run_m1")
async def run_m1(as_of: str | None = None) -> dict[str, Any]:
    """Rescore every seed market (WF-08). Returns the ``M1Report`` as JSON for the digest
    and the human approval step (M1-11)."""
    _bind()
    ctx = get_context()
    try:
        scored_at = datetime.fromisoformat(as_of).astimezone(UTC) if as_of else None
        async with ctx.session_factory() as s, s.begin():
            report = await M1MarketSelection().run(s, ctx.providers, as_of=scored_at)
        return report.to_json()
    finally:
        clear_context()


@activity.defn(name="run_a1")
async def run_a1(market_id: str, as_of: str | None = None) -> dict[str, Any]:
    """Investor discovery for one market under the discovery lock (WF-09). A locked market
    returns ``{"skipped": "locked", ...}`` rather than failing, so the schedule stays quiet."""
    _bind()
    ctx = get_context()
    mid = uuid.UUID(market_id)
    try:
        when = datetime.fromisoformat(as_of).astimezone(UTC) if as_of else None
        try:
            async with discovery_lock(ctx.session_factory, mid, now=when):
                async with ctx.session_factory() as s, s.begin():
                    market = await MarketRepository(s).get(mid)
                    if market is None:
                        msg = f"unknown market {market_id}"
                        raise LookupError(msg)
                    result = await A1InvestorDiscovery().run(s, ctx.providers, market, as_of=when)
                return result.to_json()
        except DiscoveryLocked as exc:
            log.warning("a1.skipped_locked", market_id=market_id, reason=str(exc))
            return {"skipped": "locked", "market_id": market_id}
    finally:
        clear_context()


@activity.defn(name="run_a2")
async def run_a2(investor_id: str) -> dict[str, Any]:
    """Contact research for one investor. Returns the ``A2Result`` as JSON."""
    _bind()
    ctx = get_context()
    try:
        async with ctx.session_factory() as s, s.begin():
            investor = await InvestorRepository(s).get(uuid.UUID(investor_id))
            if investor is None:
                msg = f"unknown investor {investor_id}"
                raise LookupError(msg)
            result = await A2ContactResearch().run_detailed(s, ctx.providers, investor)
        return result.to_json()
    finally:
        clear_context()


__all__ = [
    "DiscoveryContext",
    "DiscoveryLocked",
    "configure",
    "discovery_lock",
    "get_context",
    "reset",
    "run_a1",
    "run_a2",
    "run_m1",
]
