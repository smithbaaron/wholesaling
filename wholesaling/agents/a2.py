"""A2 Contact Research (REQUIREMENTS.md INV-08, INV-09; AGT-A2).

Looks up professional contact channels for one resolved investor, dedupes them with the shared
normalisers, drops junk, honours the suppression table, and writes ``contacts`` rows with
provenance, confidence and use limitations. Email is the only investor channel (INV-09: the
investor side is Outlook only), so the chosen primary is the highest-confidence valid email.
Phones are captured for the record with an explicit "capture only" limitation and are never
used for calls or SMS.

Deterministic code over provider output; no model call. ``run`` returns the primary email (or
``None``), ``run_detailed`` the full accounting.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import Channel, ContactChannel
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.messaging import Suppression
from wholesaling.db.normalize import normalize_address, normalize_email, normalize_phone
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.models import ContactCandidate
from wholesaling.logging import get_logger

log = get_logger(__name__)

PHONE_CAPTURE_ONLY = (
    "Capture only (INV-09): investor channel is email; no calls or SMS to investors in this build."
)


@dataclass(slots=True)
class A2Result:
    investor_id: uuid.UUID
    primary_email: str | None = None
    candidates: int = 0
    junk_dropped: int = 0
    duplicates_folded: int = 0
    suppressed: int = 0
    owned_by_other_investor: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0
    by_channel: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["investor_id"] = str(self.investor_id)
        return d


@dataclass(frozen=True, slots=True)
class _Normalized:
    channel: ContactChannel
    normalized: str
    candidate: ContactCandidate


def _normalize(c: ContactCandidate) -> _Normalized | None:
    """Apply the shared normaliser for the channel; ``None`` means junk (drop)."""
    value = c.value.strip()
    if not value or value.lower() in {"n/a", "na", "none", "null", "unknown", "-"}:
        return None
    try:
        if c.channel == "email":
            return _Normalized(ContactChannel.EMAIL, normalize_email(value), c)
        if c.channel == "phone":
            return _Normalized(ContactChannel.PHONE, normalize_phone(value), c)
        return _Normalized(ContactChannel.ADDRESS, normalize_address(value), c)
    except ValueError:
        return None


def dedupe_candidates(candidates: list[ContactCandidate]) -> tuple[list[_Normalized], int, int]:
    """Normalise, drop junk, fold duplicates per ``(channel, normalized)`` keeping the
    highest-confidence candidate. Returns ``(kept, junk_dropped, duplicates_folded)``."""
    best: dict[tuple[ContactChannel, str], _Normalized] = {}
    junk = dups = 0
    for c in candidates:
        n = _normalize(c)
        if n is None:
            junk += 1
            continue
        key = (n.channel, n.normalized)
        prior = best.get(key)
        if prior is None:
            best[key] = n
        else:
            dups += 1
            if n.candidate.confidence > prior.candidate.confidence:
                best[key] = n
    return list(best.values()), junk, dups


def _mailing_address(inv: Investor) -> str | None:
    if not inv.mailing_address_line1:
        return None
    parts = [inv.mailing_address_line1, inv.mailing_address_line2, inv.mailing_city]
    tail = " ".join(p for p in (inv.mailing_state, inv.mailing_postal_code) if p)
    return ", ".join(p for p in [*parts, tail] if p)


class A2ContactResearch:
    async def run(
        self, session: AsyncSession, providers: DataProviders, investor: Investor
    ) -> str | None:
        return (await self.run_detailed(session, providers, investor)).primary_email

    async def run_detailed(
        self, session: AsyncSession, providers: DataProviders, investor: Investor
    ) -> A2Result:
        result = A2Result(investor_id=investor.id)
        raw = await providers.contacts.lookup(investor.canonical_name, _mailing_address(investor))
        result.candidates = len(raw)
        kept, result.junk_dropped, result.duplicates_folded = dedupe_candidates(raw)

        suppressed = await self._suppressed_values(session)
        emails: list[tuple[Decimal, Contact]] = []
        for n in kept:
            sup_channel = Channel.EMAIL if n.channel is ContactChannel.EMAIL else Channel.SMS
            if (sup_channel, n.normalized) in suppressed:
                result.suppressed += 1
                continue
            stmt = select(Contact).where(
                Contact.channel == n.channel, Contact.normalized_value == n.normalized
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            limits = n.candidate.use_limitations
            if n.channel is ContactChannel.PHONE:
                limits = f"{limits}; {PHONE_CAPTURE_ONLY}" if limits else PHONE_CAPTURE_ONLY
            confidence = Decimal(str(round(n.candidate.confidence, 4)))
            if existing is None:
                contact = Contact(
                    investor_id=investor.id,
                    channel=n.channel,
                    value=n.candidate.value.strip(),
                    normalized_value=n.normalized,
                    provenance=f"{n.candidate.source}: {n.candidate.provenance}"[:255],
                    provenance_url=n.candidate.provenance_url,
                    use_limitations=limits,
                    confidence=confidence,
                )
                session.add(contact)
                result.contacts_created += 1
            elif existing.investor_id != investor.id:
                # (channel, normalized_value) is unique across investors (DM-02). A shared
                # inbox belongs to whoever was researched first; log, never reassign.
                result.owned_by_other_investor += 1
                log.info(
                    "a2.contact_owned_elsewhere",
                    investor_id=str(investor.id),
                    channel=n.channel.value,
                    owner_investor_id=str(existing.investor_id),
                )
                continue
            else:
                contact = existing
                if existing.confidence is None or confidence > existing.confidence:
                    existing.confidence = confidence
                    existing.provenance = f"{n.candidate.source}: {n.candidate.provenance}"[:255]
                    existing.provenance_url = n.candidate.provenance_url
                if existing.use_limitations is None and limits:
                    existing.use_limitations = limits
                result.contacts_updated += 1
            result.by_channel[n.channel.value] = result.by_channel.get(n.channel.value, 0) + 1
            if n.channel is ContactChannel.EMAIL:
                emails.append((confidence, contact))

        await session.flush()
        primary = await self._choose_primary(session, investor, emails)
        result.primary_email = primary
        log.info(
            "a2.run_complete",
            investor_id=str(investor.id),
            primary=primary is not None,
            **{
                k: v
                for k, v in result.to_json().items()
                if k not in {"investor_id", "primary_email"}
            },
        )
        return result

    @staticmethod
    async def _suppressed_values(session: AsyncSession) -> set[tuple[Channel, str]]:
        rows = (
            await session.execute(select(Suppression.channel, Suppression.normalized_value))
        ).all()
        return {(row[0], row[1]) for row in rows}

    @staticmethod
    async def _choose_primary(
        session: AsyncSession, investor: Investor, emails: list[tuple[Decimal, Contact]]
    ) -> str | None:
        """Highest-confidence email among *all* of the investor's email contacts (including
        rows from earlier runs) becomes primary; every other email loses the flag."""
        stmt = select(Contact).where(
            Contact.investor_id == investor.id, Contact.channel == ContactChannel.EMAIL
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return None
        rows.sort(key=lambda c: (-(c.confidence or Decimal(0)), c.normalized_value))
        for i, c in enumerate(rows):
            c.is_primary = i == 0
        await session.flush()
        return rows[0].normalized_value


__all__ = ["PHONE_CAPTURE_ONLY", "A2ContactResearch", "A2Result", "dedupe_candidates"]
