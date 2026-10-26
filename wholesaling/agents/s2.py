"""S2 Skip Trace (REQUIREMENTS.md AGT-S2, SEL-03, CMP-02, CMP-05, RUL-06; build step BO-10).

Deterministic code around a vendor lookup (V2 "Code + vendor"). For one seller lead:

    SkipTraceProvider.trace(owner, property address, mailing address)
      -> normalise (``normalize_email`` / ``normalize_phone``), drop junk ("n/a", unparseable)
      -> dedupe per (channel, normalised value), keeping the highest confidence
      -> emails on the ``suppression`` table are dropped (prior opt-outs, bounces; POL-03l)
      -> every phone is scrubbed against the DNC list and *flagged*, never used: SMS is OFF in
         this build (POL-03e, RUL-06); the scrub is recorded so a later SMS build cannot skip it
      -> the highest-confidence email is primary: email is the seller channel (STK-13)

Everything is written to ``seller_leads.contact_channels`` with provenance, confidence and the
vendor's use limitations (SEL-03, CMP-05), and the trace summary (vendor, reference, ``dnc_listed``)
to ``seller_leads.source_refs["skip_trace"]``. No model is involved.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.base import utcnow
from wholesaling.db.models.enums import AgentId
from wholesaling.db.models.sellers import SellerLead
from wholesaling.db.normalize import normalize_email, normalize_phone
from wholesaling.db.repositories.messages import MessageRepository
from wholesaling.db.repositories.sellers import SellerRepository
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.models import ContactCandidate
from wholesaling.logging import get_logger

log = get_logger(__name__)

JUNK_VALUES: Final[frozenset[str]] = frozenset({"", "n/a", "na", "none", "null", "unknown", "-"})
PHONE_LIMITATION: Final[str] = "capture only: no calls or SMS in this build (RUL-06, POL-03e)"
PREFERRED_CHANNEL: Final[str] = "email"


@dataclass(slots=True)
class TracedContact:
    channel: str
    value: str
    normalized_value: str
    source: str
    provenance: str
    confidence: float
    is_primary: bool = False
    use_limitations: str | None = None
    provenance_url: str | None = None
    dnc_listed: bool | None = None  # phones only
    suppressed: bool = False  # emails only; kept in the record, never used

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class S2Result:
    seller_lead_id: uuid.UUID
    owner_name: str
    vendor: str | None = None
    vendor_ref: str | None = None
    candidates: int = 0
    kept: int = 0
    dropped_junk: int = 0
    dropped_duplicate: int = 0
    suppressed_emails: int = 0
    dnc_listed: bool = False
    dnc_checked: int = 0
    primary_email: str | None = None
    contacts: list[TracedContact] = field(default_factory=list)
    skipped_reason: str | None = None  # "no_skiptrace_provider" | "no_owner_name"

    @property
    def reachable(self) -> bool:
        return self.primary_email is not None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["seller_lead_id"] = str(self.seller_lead_id)
        return d


def _normalize(candidate: ContactCandidate) -> str | None:
    value = candidate.value.strip()
    if value.lower() in JUNK_VALUES:
        return None
    try:
        if candidate.channel == "email":
            return normalize_email(value)
        if candidate.channel == "phone":
            return normalize_phone(value)
    except ValueError:
        return None
    return " ".join(value.upper().split())


def dedupe_candidates(candidates: list[ContactCandidate]) -> tuple[list[TracedContact], int, int]:
    """Normalise and fold duplicates (highest confidence wins). Returns (kept, junk, dupes)."""
    best: dict[tuple[str, str], TracedContact] = {}
    junk = dupes = 0
    for c in candidates:
        normalized = _normalize(c)
        if normalized is None:
            junk += 1
            continue
        key = (c.channel, normalized)
        traced = TracedContact(
            channel=c.channel,
            value=c.value.strip(),
            normalized_value=normalized,
            source=c.source,
            provenance=c.provenance,
            confidence=float(c.confidence),
            use_limitations=(
                PHONE_LIMITATION if c.channel == "phone" else (c.use_limitations or None)
            ),
            provenance_url=c.provenance_url,
        )
        if key in best:
            dupes += 1
            if traced.confidence > best[key].confidence:
                best[key] = traced
            continue
        best[key] = traced
    return list(best.values()), junk, dupes


class S2SkipTrace:
    agent_id: Final = AgentId.S2

    async def run(
        self,
        session: AsyncSession,
        providers: DataProviders,
        seller_lead: SellerLead,
        *,
        now: datetime | None = None,
    ) -> S2Result:
        """Trace, scrub and record contacts for one lead. Commits nothing."""
        owner = (seller_lead.owner_name or "").strip()
        result = S2Result(seller_lead_id=seller_lead.id, owner_name=owner)
        if providers.skiptrace is None or providers.dnc is None:
            result.skipped_reason = "no_skiptrace_provider"
            return result
        if not owner:
            result.skipped_reason = "no_owner_name"
            return result

        refs = seller_lead.source_refs or {}
        mailing = refs.get("owner_mailing_address")
        property_address = ", ".join(
            p
            for p in (
                seller_lead.property_address_line1,
                seller_lead.property_city,
                seller_lead.property_state,
                seller_lead.property_postal_code,
            )
            if p
        )
        trace = await providers.skiptrace.trace(owner, property_address, mailing)
        result.vendor = trace.vendor
        result.vendor_ref = trace.vendor_ref
        result.candidates = len(trace.contacts)

        kept, junk, dupes = dedupe_candidates(list(trace.contacts))
        result.dropped_junk = junk
        result.dropped_duplicate = dupes

        messages = MessageRepository(session)
        for contact in kept:
            if contact.channel == "email":
                contact.suppressed = await messages.is_suppressed(contact.normalized_value)
                if contact.suppressed:
                    result.suppressed_emails += 1
            elif contact.channel == "phone":
                contact.dnc_listed = await providers.dnc.is_listed(contact.normalized_value)
                result.dnc_checked += 1
                if contact.dnc_listed:
                    result.dnc_listed = True

        emails = [c for c in kept if c.channel == "email" and not c.suppressed]
        if emails:
            primary = max(emails, key=lambda c: (c.confidence, c.normalized_value))
            primary.is_primary = True
            result.primary_email = primary.value
        # Deterministic order: emails first, then phones, then addresses; confidence desc.
        order = {"email": 0, "phone": 1, "address": 2}
        kept.sort(key=lambda c: (order.get(c.channel, 9), -c.confidence, c.normalized_value))
        result.kept = len(kept)
        result.contacts = kept

        await SellerRepository(session).record_contacts(
            seller_lead,
            [c.to_json() for c in kept],
            trace_meta={
                "vendor": trace.vendor,
                "vendor_ref": trace.vendor_ref,
                "retrieved_at": trace.retrieved_at.isoformat(),
                "traced_at": (now or utcnow()).isoformat(),
                "candidates": result.candidates,
                "kept": result.kept,
                "dropped_junk": junk,
                "dropped_duplicate": dupes,
                "suppressed_emails": result.suppressed_emails,
                # SEL-03 / CMP-02: the DNC scrub is recorded even though SMS never sends here.
                "dnc_checked": result.dnc_checked,
                "dnc_listed": result.dnc_listed,
                "preferred_channel": PREFERRED_CHANNEL,
                "sms_allowed": False,
            },
        )
        log.info(
            "s2.traced",
            seller_lead_id=str(seller_lead.id),
            kept=result.kept,
            primary_email=result.primary_email,
            dnc_listed=result.dnc_listed,
        )
        return result


__all__ = [
    "JUNK_VALUES",
    "PHONE_LIMITATION",
    "PREFERRED_CHANNEL",
    "S2Result",
    "S2SkipTrace",
    "TracedContact",
    "dedupe_candidates",
]
