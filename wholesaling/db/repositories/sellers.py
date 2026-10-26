"""``seller_leads`` / ``intakes`` repository (REQUIREMENTS.md SEL-02, SEL-03, SEL-06, SEL-07,
SEL-08, SEL-12, WF-10).

Dedupe keys: ``parcel_id`` first, then ``address_key`` (``normalize_address`` output), then the
web form's ``submission_id`` (SEL-12 idempotency). ``seller_leads`` has no ``meta`` column; the
lead's working notes (skip-trace provenance, the DNC scrub, the submission payload) live in
``source_refs``.

Intake merging never overwrites a stated value with ``None`` and flips the ``*_unknown`` flag
to ``False`` only when the seller actually stated the field (SEL-06 "unknowns flagged").
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from collections.abc import Sequence as Seq
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import (
    Channel,
    ConditionClass,
    Direction,
    IntakeTimeline,
    SellerLeadStatus,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.messaging import Message, Sequence, Thread
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.normalize import normalize_email

# SEL-06 required intake fields, in the order S4 asks for them.
REQUIRED_INTAKE_FIELDS: tuple[str, ...] = ("address", "motivation", "condition", "timeline")
# SEL-06 "asked, unknowns flagged" (optional for completion).
ASKED_INTAKE_FIELDS: tuple[str, ...] = ("co_owners", "occupancy", "liens", "hoa")

ACTIVE_SEQUENCE_STATUSES: frozenset[SequenceStatus] = frozenset(
    {SequenceStatus.ACTIVE, SequenceStatus.PAUSED}
)


@dataclass(slots=True)
class IntakeFields:
    """What S4 extracted from one message or form. ``None`` = not stated. ``seller_unsure``
    lists optional fields the seller said they do not know (flag stays unknown, no re-ask)."""

    property_address: str | None = None
    motivation: str | None = None
    condition_class: ConditionClass | None = None
    disclosed_issues: list[str] = field(default_factory=list)
    timeline: IntakeTimeline | None = None
    timeline_text: str | None = None
    asking_price: Decimal | None = None
    co_owners: str | None = None
    occupancy: str | None = None
    liens: str | None = None
    hoa: str | None = None
    seller_unsure: list[str] = field(default_factory=list)
    photos_mentioned: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "property_address": self.property_address,
            "motivation": self.motivation,
            "condition_class": self.condition_class.value if self.condition_class else None,
            "disclosed_issues": list(self.disclosed_issues),
            "timeline": self.timeline.value if self.timeline else None,
            "timeline_text": self.timeline_text,
            "asking_price": None if self.asking_price is None else str(self.asking_price),
            "co_owners": self.co_owners,
            "occupancy": self.occupancy,
            "liens": self.liens,
            "hoa": self.hoa,
            "seller_unsure": list(self.seller_unsure),
            "photos_mentioned": self.photos_mentioned,
        }


def missing_required(lead: SellerLead, intake: Intake) -> list[str]:
    """SEL-06 required fields still missing, in ask order. The address counts only when the
    parcel is verified; ``timeline=unknown`` counts as missing."""
    missing: list[str] = []
    if not lead.parcel_verified:
        missing.append("address")
    if not (intake.motivation or "").strip():
        missing.append("motivation")
    if intake.condition_class is None:
        missing.append("condition")
    if intake.timeline is None or intake.timeline is IntakeTimeline.UNKNOWN:
        missing.append("timeline")
    return missing


def unasked_optional(intake: Intake) -> list[str]:
    """Optional SEL-06 fields neither stated, declared unknown by the seller, nor asked yet."""
    asked = set((intake.property_facts or {}).get("asked_optional") or [])
    unsure = set((intake.property_facts or {}).get("seller_unsure") or [])
    out: list[str] = []
    for name in ASKED_INTAKE_FIELDS:
        unknown = bool(getattr(intake, f"{name}_unknown"))
        if unknown and name not in asked and name not in unsure:
            out.append(name)
    return out


class SellerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # -- lookups -------------------------------------------------------------------------------

    async def get(self, lead_id: uuid.UUID) -> SellerLead | None:
        return await self._s.get(SellerLead, lead_id)

    async def require(self, lead_id: uuid.UUID) -> SellerLead:
        row = await self.get(lead_id)
        if row is None:
            msg = f"seller lead {lead_id} not found"
            raise LookupError(msg)
        return row

    async def find_existing(
        self, *, parcel_id: str | None, address_key: str | None
    ) -> SellerLead | None:
        """The lead already on record for this parcel, else for this normalised address."""
        if parcel_id:
            stmt = (
                select(SellerLead)
                .where(SellerLead.parcel_id == parcel_id)
                .order_by(SellerLead.created_at)
                .limit(1)
            )
            found = (await self._s.execute(stmt)).scalar_one_or_none()
            if found is not None:
                return found
        if address_key:
            stmt = (
                select(SellerLead)
                .where(SellerLead.address_key == address_key)
                .order_by(SellerLead.created_at)
                .limit(1)
            )
            return (await self._s.execute(stmt)).scalar_one_or_none()
        return None

    async def get_by_submission_id(self, submission_id: str) -> SellerLead | None:
        stmt = select(SellerLead).where(SellerLead.web_form_submission_id == submission_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def find_by_email(self, address: str | None) -> SellerLead | None:
        """The lead whose thread counterpart is ``address`` (the sequence or the web form
        created that thread). ``None`` for an unknown sender."""
        if not address:
            return None
        try:
            normalized = normalize_email(address)
        except ValueError:
            return None
        stmt = (
            select(Thread)
            .where(Thread.seller_lead_id.is_not(None))
            .order_by(Thread.created_at.desc())
        )
        for thread in (await self._s.execute(stmt)).scalars().all():
            counterpart = thread.counterpart_address
            if not counterpart:
                continue
            try:
                if normalize_email(counterpart) == normalized and thread.seller_lead_id:
                    return await self.get(thread.seller_lead_id)
            except ValueError:
                continue
        return None

    async def list_by_status(
        self, market_id: uuid.UUID, status: SellerLeadStatus, *, limit: int | None = None
    ) -> list[SellerLead]:
        stmt = (
            select(SellerLead)
            .where(SellerLead.market_id == market_id, SellerLead.status == status)
            .order_by(SellerLead.signal_score.desc().nulls_last(), SellerLead.created_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self._s.execute(stmt)).scalars().all())

    # -- S1 writes -----------------------------------------------------------------------------

    async def create_from_distress(
        self,
        *,
        market_id: uuid.UUID,
        owner_name: str,
        address_line1: str,
        city: str,
        state: str,
        postal_code: str,
        address_key: str,
        parcel_id: str,
        signals: Seq[str],
        signal_score: int,
        assessed_value: Decimal | None,
        estimated_value: Decimal | None,
        source: str,
        source_refs: Mapping[str, Any],
    ) -> SellerLead:
        row = SellerLead(
            market_id=market_id,
            status=SellerLeadStatus.QUEUED,
            owner_name=owner_name,
            property_address_line1=address_line1,
            property_city=city,
            property_state=state,
            property_postal_code=postal_code,
            address_key=address_key,
            parcel_id=parcel_id,
            parcel_verified=True,  # the parcel id came from the county list itself
            signals=list(signals),
            signal_score=signal_score,
            assessed_value=assessed_value,
            estimated_value=estimated_value,
            source=source,
            source_refs=dict(source_refs),
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def refresh_from_distress(
        self,
        lead: SellerLead,
        *,
        signals: Seq[str],
        signal_score: int,
        assessed_value: Decimal | None,
        estimated_value: Decimal | None,
        source_refs: Mapping[str, Any],
    ) -> SellerLead:
        """WF-10 weekly refresh: update the facts, keep the status and everything else."""
        lead.signals = list(signals)
        lead.signal_score = signal_score
        if assessed_value is not None:
            lead.assessed_value = assessed_value
        if estimated_value is not None:
            lead.estimated_value = estimated_value
        lead.source_refs = {**(lead.source_refs or {}), **dict(source_refs)}
        await self._s.flush()
        return lead

    # -- S2 writes -----------------------------------------------------------------------------

    async def record_contacts(
        self,
        lead: SellerLead,
        contacts: Seq[Mapping[str, Any]],
        *,
        trace_meta: Mapping[str, Any],
    ) -> SellerLead:
        lead.contact_channels = [dict(c) for c in contacts]
        lead.source_refs = {**(lead.source_refs or {}), "skip_trace": dict(trace_meta)}
        await self._s.flush()
        return lead

    @staticmethod
    def primary_email(lead: SellerLead) -> str | None:
        for c in lead.contact_channels or []:
            if c.get("channel") == "email" and c.get("is_primary"):
                return str(c.get("value"))
        return None

    # -- status --------------------------------------------------------------------------------

    async def set_status(self, lead: SellerLead, status: SellerLeadStatus) -> SellerLead:
        if (
            lead.status in {SellerLeadStatus.SUPPRESSED}
            and status is not SellerLeadStatus.SUPPRESSED
        ):
            return lead  # suppression is final
        lead.status = status
        await self._s.flush()
        return lead

    # -- web form ------------------------------------------------------------------------------

    async def create_from_web_form(
        self,
        *,
        submission_id: str,
        market_id: uuid.UUID | None,
        owner_name: str | None,
        address_line1: str,
        city: str | None,
        state: str | None,
        postal_code: str | None,
        address_key: str,
        contact_email: str | None,
        contact_phone: str | None,
        source_refs: Mapping[str, Any],
    ) -> SellerLead:
        contacts: list[dict[str, Any]] = []
        if contact_email:
            contacts.append(
                {
                    "channel": "email",
                    "value": contact_email,
                    "normalized_value": normalize_email(contact_email),
                    "source": "web_form",
                    "provenance": f"seller-submitted (form {submission_id})",
                    "confidence": 1.0,
                    "is_primary": True,
                    "use_limitations": "seller consented to contact by email",
                }
            )
        if contact_phone:
            contacts.append(
                {
                    "channel": "phone",
                    "value": contact_phone,
                    "source": "web_form",
                    "provenance": f"seller-submitted (form {submission_id})",
                    "confidence": 1.0,
                    "is_primary": False,
                    "use_limitations": "capture only: no calls or SMS in this build (RUL-06)",
                }
            )
        row = SellerLead(
            market_id=market_id,
            status=SellerLeadStatus.INTAKE,
            owner_name=owner_name,
            property_address_line1=address_line1,
            property_city=city,
            property_state=state,
            property_postal_code=postal_code,
            address_key=address_key,
            source="web_form",
            source_refs=dict(source_refs),
            contact_channels=contacts,
            web_form_submission_id=submission_id,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    # -- intakes -------------------------------------------------------------------------------

    async def latest_intake(self, lead_id: uuid.UUID) -> Intake | None:
        stmt = (
            select(Intake)
            .where(Intake.seller_lead_id == lead_id)
            .order_by(Intake.created_at.desc(), Intake.id.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_or_create_intake(self, lead: SellerLead) -> Intake:
        """The open intake for ``lead`` (one per lead until it completes; a completed intake is
        reused rather than duplicated so A7 sees one record)."""
        existing = await self.latest_intake(lead.id)
        if existing is not None:
            return existing
        row = Intake(seller_lead_id=lead.id)
        self._s.add(row)
        await self._s.flush()
        return row

    async def merge_intake(self, intake: Intake, fields: IntakeFields) -> list[str]:
        """Apply stated fields (never clearing anything). Returns the field names changed."""
        changed: list[str] = []
        motivation = (fields.motivation or "").strip()
        if motivation and intake.motivation != motivation:
            intake.motivation = motivation
            changed.append("motivation")
        if fields.condition_class is not None and intake.condition_class != fields.condition_class:
            intake.condition_class = fields.condition_class
            changed.append("condition_class")
        if fields.disclosed_issues:
            merged = list(intake.disclosed_issues or [])
            for issue in fields.disclosed_issues:
                text = str(issue).strip()
                if text and text.lower() not in {m.lower() for m in merged}:
                    merged.append(text)
            if merged != list(intake.disclosed_issues or []):
                intake.disclosed_issues = merged
                changed.append("disclosed_issues")
        timeline = fields.timeline
        if (
            timeline is not None
            and timeline is not IntakeTimeline.UNKNOWN
            and intake.timeline != timeline
        ):
            intake.timeline = timeline
            changed.append("timeline")
        if fields.timeline_text and intake.timeline_text != fields.timeline_text.strip():
            intake.timeline_text = fields.timeline_text.strip()[:255]
            changed.append("timeline_text")
        if fields.asking_price is not None and intake.asking_price != fields.asking_price:
            intake.asking_price = fields.asking_price
            changed.append("asking_price")
        for name in ASKED_INTAKE_FIELDS:
            value = getattr(fields, name)
            if value and str(value).strip():
                if getattr(intake, name) != str(value).strip():
                    setattr(intake, name, str(value).strip())
                    changed.append(name)
                setattr(intake, f"{name}_unknown", False)
        if fields.seller_unsure:
            facts = dict(intake.property_facts or {})
            unsure = list(facts.get("seller_unsure") or [])
            for name in fields.seller_unsure:
                if name in ASKED_INTAKE_FIELDS and name not in unsure:
                    unsure.append(name)
            facts["seller_unsure"] = unsure
            intake.property_facts = facts
            changed.append("seller_unsure")
        await self._s.flush()
        return changed

    async def note_asked(self, intake: Intake, asked: Seq[str]) -> None:
        facts = dict(intake.property_facts or {})
        seen = list(facts.get("asked_optional") or [])
        for name in asked:
            if name in ASKED_INTAKE_FIELDS and name not in seen:
                seen.append(name)
        facts["asked_optional"] = seen
        intake.property_facts = facts
        await self._s.flush()

    async def add_photos(self, intake: Intake, photos: Seq[Mapping[str, Any]]) -> Intake:
        existing = list(intake.photos or [])
        keys = {p.get("blob_key") for p in existing}
        for p in photos:
            if p.get("blob_key") not in keys:
                existing.append(dict(p))
                keys.add(p.get("blob_key"))
        intake.photos = existing
        await self._s.flush()
        return intake

    async def set_missing(self, intake: Intake, missing: Seq[str]) -> None:
        intake.missing_required = list(missing)
        await self._s.flush()

    async def complete(self, intake: Intake, *, at: datetime) -> bool:
        """Mark complete once; returns whether this call did it."""
        if intake.intake_complete:
            return False
        intake.intake_complete = True
        intake.completed_at = at
        intake.missing_required = []
        await self._s.flush()
        return True

    # -- sequences / threads -------------------------------------------------------------------

    async def active_sequence(self, lead_id: uuid.UUID) -> Sequence | None:
        stmt = (
            select(Sequence)
            .where(
                Sequence.kind == SequenceKind.SELLER,
                Sequence.seller_lead_id == lead_id,
                Sequence.status.in_(list(ACTIVE_SEQUENCE_STATUSES)),
            )
            .order_by(Sequence.created_at.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def has_sequence_on_record(self, lead_id: uuid.UUID, *, ignore_reason: str) -> bool:
        stmt = select(Sequence.status, Sequence.cancel_reason).where(
            Sequence.kind == SequenceKind.SELLER, Sequence.seller_lead_id == lead_id
        )
        for status, reason in (await self._s.execute(stmt)).all():
            if status is SequenceStatus.CANCELLED and reason == ignore_reason:
                continue
            return True
        return False

    async def thread_for_lead(self, lead_id: uuid.UUID) -> Thread | None:
        stmt = (
            select(Thread)
            .where(Thread.seller_lead_id == lead_id)
            .order_by(Thread.created_at.desc())
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def create_thread(
        self,
        *,
        lead: SellerLead,
        channel: Channel,
        counterpart_address: str | None,
        subject: str | None,
    ) -> Thread:
        thread = Thread(
            channel=channel,
            subject=subject,
            seller_lead_id=lead.id,
            counterpart_address=counterpart_address,
        )
        self._s.add(thread)
        await self._s.flush()
        return thread

    async def get_message_by_key(
        self, channel: Channel, external_message_id: str
    ) -> Message | None:
        stmt = select(Message).where(
            Message.channel == channel, Message.external_message_id == external_message_id
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def create_form_message(
        self,
        *,
        thread: Thread,
        submission_id: str,
        from_address: str | None,
        body: str,
        payload: Mapping[str, Any],
        received_at: datetime,
    ) -> tuple[Message, bool]:
        key = f"form:{submission_id}"
        existing = await self.get_message_by_key(Channel.WEB_FORM, key)
        if existing is not None:
            return existing, False
        row = Message(
            thread_id=thread.id,
            direction=Direction.IN,
            channel=Channel.WEB_FORM,
            external_message_id=key,
            from_address=from_address,
            to_addresses=[],
            subject="Web form submission",
            body=body,
            received_at=received_at,
            meta={"form": dict(payload), "submission_id": submission_id},
        )
        self._s.add(row)
        thread.last_message_at = received_at
        await self._s.flush()
        return row, True

    async def inbound_for_lead(self, lead_id: uuid.UUID) -> list[Message]:
        stmt = (
            select(Message)
            .join(Thread, Thread.id == Message.thread_id)
            .where(
                Thread.seller_lead_id == lead_id,
                or_(Message.direction == Direction.IN, Message.sent_at.is_not(None)),
            )
            .order_by(Message.created_at, Message.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())


__all__ = [
    "ASKED_INTAKE_FIELDS",
    "REQUIRED_INTAKE_FIELDS",
    "IntakeFields",
    "SellerRepository",
    "missing_required",
    "unasked_optional",
]
