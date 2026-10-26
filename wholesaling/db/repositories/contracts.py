"""``contracts`` repository: every transition writes an audit event, and the hard-floor
transitions refuse any actor but a human (REQUIREMENTS.md POL-03i/j, POL-06, XC-05, SEL-11).

Lifecycle of the PSA row::

    offer.status = accepted  (A7/S5; audit ``contract.requested``)
        -> create_from_accepted_offer     status requested            any actor (bookkeeping)
        -> mark_sent_for_signature        status sent_for_signature   HUMAN (envelope created)
        -> mark_signed                    status signed               HUMAN  ``contract.signed``
        -> select_buyer / record_deposit / set_deadlines / set_earnest_money / checklist   HUMAN
        -> mark_closed                    status closed               HUMAN  ``contract.closed``
        -> mark_dead                      status cancelled            HUMAN  ``contract.dead``

``record_buyer_response`` is the one write an automated path may make (the inbound pipeline or
S6 classifying a buyer's reply): it touches ``buyer_responses`` only, never the status.

Enforcement is in code, here, not only in the UI: ``HumanActionRequired`` is raised for any
``actor`` other than ``Actor.HUMAN`` on the guarded methods, with a test proving it
(tests/db/test_contracts.py).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.deals import (
    BuyerResponse,
    Contract,
    ContractStatus,
    ContractType,
    EarnestMoneyStatus,
)
from wholesaling.db.models.enums import Actor, OfferStatus
from wholesaling.db.models.offers import Offer
from wholesaling.db.models.sellers import SellerLead
from wholesaling.db.repositories.audit import AuditRepository

ENTITY_TYPE = "contract"

EVENT_CREATED = "contract.created"
EVENT_SENT_FOR_SIGNATURE = "contract.sent_for_signature"
EVENT_SIGNED = "contract.signed"
EVENT_EARNEST_MONEY = "contract.earnest_money"
EVENT_DEADLINES_SET = "contract.deadlines_set"
EVENT_BUYER_RESPONSE = "contract.buyer_response"
EVENT_BUYER_SELECTED = "contract.buyer_selected"
EVENT_DEPOSIT_RECEIVED = "contract.deposit_received"
EVENT_CHECKLIST = "contract.checklist_updated"
EVENT_CLOSED = "contract.closed"
EVENT_DEAD = "contract.dead"
# Written by the A7/S5 builder when an offer is accepted (the contract with that side).
EVENT_REQUESTED = "contract.requested"

EARNEST_MONEY_TRANSITIONS: dict[EarnestMoneyStatus, frozenset[EarnestMoneyStatus]] = {
    EarnestMoneyStatus.NONE: frozenset({EarnestMoneyStatus.REQUESTED}),
    EarnestMoneyStatus.REQUESTED: frozenset(
        {EarnestMoneyStatus.DEPOSITED, EarnestMoneyStatus.NONE}
    ),
    EarnestMoneyStatus.DEPOSITED: frozenset({EarnestMoneyStatus.RELEASED}),
    EarnestMoneyStatus.RELEASED: frozenset(),
}


class HumanActionRequired(PermissionError):
    """A hard-floor transition was attempted by a non-human actor (POL-06)."""


class InvalidContractTransition(ValueError):
    """The contract is not in a state that allows the requested change."""


class ContractNotFound(LookupError):
    pass


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _require_human(actor: Actor, what: str) -> None:
    if actor is not Actor.HUMAN:
        msg = f"{what} is a human-only action (POL-06 hard floor); actor was {actor.value!r}"
        raise HumanActionRequired(msg)


@dataclass(slots=True)
class AwaitingContract:
    """An accepted offer with no PSA row yet (the ``/deals`` "awaiting contract" list)."""

    offer_id: uuid.UUID
    seller_lead_id: uuid.UUID
    property_address: str
    offer_price: Decimal | None
    assignment_fee: Decimal | None
    accepted_at: datetime | None


class ContractRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._audit = AuditRepository(session)

    # -- reads -------------------------------------------------------------------------------

    async def get(self, contract_id: uuid.UUID) -> Contract | None:
        return await self._s.get(Contract, contract_id)

    async def require(self, contract_id: uuid.UUID) -> Contract:
        row = await self.get(contract_id)
        if row is None:
            raise ContractNotFound(f"contract {contract_id} not found")
        return row

    async def list_all(self, *, status: ContractStatus | None = None) -> list[Contract]:
        stmt = select(Contract).order_by(Contract.created_at.desc(), Contract.id)
        if status is not None:
            stmt = stmt.where(Contract.status == status)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_psa(self) -> list[Contract]:
        stmt = (
            select(Contract)
            .where(Contract.type == ContractType.PSA)
            .order_by(Contract.created_at.desc(), Contract.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def for_offer(self, offer_id: uuid.UUID, type_: ContractType) -> Contract | None:
        stmt = select(Contract).where(Contract.offer_id == offer_id, Contract.type == type_)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def assignment_for(self, psa_id: uuid.UUID) -> Contract | None:
        stmt = select(Contract).where(
            Contract.parent_contract_id == psa_id, Contract.type == ContractType.ASSIGNMENT
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def awaiting_contract(self) -> list[AwaitingContract]:
        """Accepted offers (SEL-11 ``contract.requested``) that have no PSA row yet."""
        have = select(Contract.offer_id).where(Contract.type == ContractType.PSA)
        stmt = (
            select(Offer, SellerLead)
            .join(SellerLead, SellerLead.id == Offer.seller_lead_id)
            .where(Offer.status == OfferStatus.ACCEPTED, Offer.id.not_in(have))
            .order_by(Offer.updated_at.desc())
        )
        out: list[AwaitingContract] = []
        for offer, lead in (await self._s.execute(stmt)).all():
            out.append(
                AwaitingContract(
                    offer_id=offer.id,
                    seller_lead_id=lead.id,
                    property_address=_address(lead),
                    offer_price=offer.offer_price,
                    assignment_fee=offer.assignment_fee,
                    accepted_at=_aware(offer.updated_at),
                )
            )
        return out

    # -- create ------------------------------------------------------------------------------

    async def create_from_accepted_offer(
        self,
        offer_id: uuid.UUID,
        *,
        actor: Actor,
        actor_id: str | None,
        title_company: str | None = None,
    ) -> tuple[Contract, bool]:
        """The PSA row for an accepted offer (idempotent on ``(offer_id, psa)``)."""
        existing = await self.for_offer(offer_id, ContractType.PSA)
        if existing is not None:
            return existing, False
        offer = await self._s.get(Offer, offer_id)
        if offer is None:
            raise ContractNotFound(f"offer {offer_id} not found")
        if offer.status is not OfferStatus.ACCEPTED:
            msg = f"offer {offer_id} is {offer.status.value}, not accepted (SEL-11)"
            raise InvalidContractTransition(msg)
        lead = await self._s.get(SellerLead, offer.seller_lead_id)
        if lead is None:
            raise ContractNotFound(f"seller lead {offer.seller_lead_id} not found")
        row = Contract(
            offer_id=offer.id,
            seller_lead_id=lead.id,
            type=ContractType.PSA,
            status=ContractStatus.REQUESTED,
            property_address_line1=lead.property_address_line1,
            property_city=lead.property_city,
            property_state=lead.property_state,
            property_postal_code=lead.property_postal_code,
            purchase_price=offer.offer_price,
            assignment_fee=offer.assignment_fee,
            title_company=title_company,
        )
        self._s.add(row)
        await self._s.flush()
        await self._event(
            row,
            EVENT_CREATED,
            actor,
            actor_id,
            {"offer_id": str(offer.id), "seller_lead_id": str(lead.id), "type": row.type.value},
            key=f"contract:{row.id}:created",
        )
        return row, True

    async def create_assignment(
        self,
        psa_id: uuid.UUID,
        *,
        actor: Actor,
        actor_id: str | None,
    ) -> tuple[Contract, bool]:
        """The Assignment Agreement row for a signed PSA with a chosen buyer. HUMAN only: it is
        the step that generates a contract envelope (POL-03i)."""
        _require_human(actor, "create_assignment")
        psa = await self.require(psa_id)
        if psa.type is not ContractType.PSA:
            raise InvalidContractTransition("assignment must hang off a PSA row")
        if psa.status is not ContractStatus.SIGNED:
            msg = f"PSA {psa_id} is {psa.status.value}; an assignment needs a signed PSA"
            raise InvalidContractTransition(msg)
        if psa.buyer_investor_id is None:
            raise InvalidContractTransition("select a buyer before generating the assignment")
        existing = await self.assignment_for(psa.id)
        if existing is not None:
            return existing, False
        row = Contract(
            offer_id=psa.offer_id,
            seller_lead_id=psa.seller_lead_id,
            parent_contract_id=psa.id,
            type=ContractType.ASSIGNMENT,
            status=ContractStatus.REQUESTED,
            property_address_line1=psa.property_address_line1,
            property_city=psa.property_city,
            property_state=psa.property_state,
            property_postal_code=psa.property_postal_code,
            purchase_price=psa.purchase_price,
            buyer_investor_id=psa.buyer_investor_id,
            assignment_fee=psa.assignment_fee,
            deposit_amount=psa.deposit_amount,
            closing_date=psa.closing_date,
            title_company=psa.title_company,
        )
        self._s.add(row)
        await self._s.flush()
        await self._event(
            row,
            EVENT_CREATED,
            actor,
            actor_id,
            {
                "offer_id": str(psa.offer_id),
                "parent_contract_id": str(psa.id),
                "buyer_investor_id": str(psa.buyer_investor_id),
                "type": row.type.value,
            },
            key=f"contract:{row.id}:created",
        )
        return row, True

    # -- hard-floor transitions (HUMAN only) -------------------------------------------------

    async def mark_sent_for_signature(
        self,
        contract_id: uuid.UUID,
        *,
        envelope_id: str,
        envelope_status: str,
        actor: Actor,
        actor_id: str | None,
        at: datetime,
    ) -> Contract:
        _require_human(actor, "mark_sent_for_signature")
        row = await self.require(contract_id)
        if row.status not in (ContractStatus.REQUESTED, ContractStatus.SENT_FOR_SIGNATURE):
            msg = f"contract {contract_id} is {row.status.value}; cannot send for signature"
            raise InvalidContractTransition(msg)
        row.status = ContractStatus.SENT_FOR_SIGNATURE
        row.docusign_envelope_id = envelope_id
        row.envelope_status = envelope_status
        row.sent_for_signature_at = row.sent_for_signature_at or at
        await self._s.flush()
        await self._event(
            row,
            EVENT_SENT_FOR_SIGNATURE,
            actor,
            actor_id,
            {"envelope_id": envelope_id, "envelope_status": envelope_status},
            key=f"contract:{row.id}:sent:{envelope_id}",
        )
        return row

    async def mark_signed(
        self,
        contract_id: uuid.UUID,
        *,
        signed_by: str,
        signed_at: datetime,
        envelope_id: str | None,
        actor: Actor,
        actor_id: str | None,
    ) -> tuple[Contract, bool]:
        """Record the human-witnessed signature. Returns ``(row, changed)``: a second call is a
        no-op so a retried route click cannot start two workflows."""
        _require_human(actor, "mark_signed")
        if not signed_by.strip():
            raise InvalidContractTransition("signed_by (the human actor) is required")
        row = await self.require(contract_id)
        if row.status is ContractStatus.SIGNED:
            return row, False
        if row.status not in (ContractStatus.REQUESTED, ContractStatus.SENT_FOR_SIGNATURE):
            msg = f"contract {contract_id} is {row.status.value}; cannot mark signed"
            raise InvalidContractTransition(msg)
        row.status = ContractStatus.SIGNED
        row.signed_by = signed_by
        row.signed_at = signed_at
        if envelope_id:
            row.docusign_envelope_id = envelope_id
        row.envelope_status = "completed" if (envelope_id or row.docusign_envelope_id) else None
        await self._s.flush()
        await self._event(
            row,
            EVENT_SIGNED,
            actor,
            actor_id,
            {
                "signed_by": signed_by,
                "signed_at": signed_at.isoformat(),
                "envelope_id": row.docusign_envelope_id,
                "type": row.type.value,
                "offer_id": str(row.offer_id),
                "seller_lead_id": str(row.seller_lead_id),
            },
            key=f"contract:{row.id}:signed",
        )
        return row, True

    async def set_earnest_money(
        self,
        contract_id: uuid.UUID,
        *,
        status: EarnestMoneyStatus,
        amount: Decimal | None,
        actor: Actor,
        actor_id: str | None,
        note: str | None = None,
    ) -> Contract:
        """Earnest-money transitions are human at every level (POL-03j)."""
        _require_human(actor, "set_earnest_money")
        row = await self.require(contract_id)
        if status not in EARNEST_MONEY_TRANSITIONS[row.earnest_money_status]:
            msg = (
                f"earnest money {row.earnest_money_status.value} -> {status.value} is not a "
                "valid transition"
            )
            raise InvalidContractTransition(msg)
        before = row.earnest_money_status
        row.earnest_money_status = status
        if amount is not None:
            row.earnest_money_amount = amount
        await self._s.flush()
        await self._event(
            row,
            EVENT_EARNEST_MONEY,
            actor,
            actor_id,
            {
                "from": before.value,
                "to": status.value,
                "amount": str(row.earnest_money_amount) if row.earnest_money_amount else None,
                "note": note,
            },
        )
        return row

    async def set_deadlines(
        self,
        contract_id: uuid.UUID,
        *,
        actor: Actor,
        actor_id: str | None,
        option_period_end: datetime | None = None,
        inspection_deadline: datetime | None = None,
        closing_date: datetime | None = None,
        title_company: str | None = None,
    ) -> Contract:
        """Dates come off the signed paper, typed by the human who read it."""
        _require_human(actor, "set_deadlines")
        row = await self.require(contract_id)
        changed: dict[str, Any] = {}
        if option_period_end is not None:
            row.option_period_end = option_period_end
            changed["option_period_end"] = option_period_end.isoformat()
        if inspection_deadline is not None:
            row.inspection_deadline = inspection_deadline
            changed["inspection_deadline"] = inspection_deadline.isoformat()
        if closing_date is not None:
            row.closing_date = closing_date
            changed["closing_date"] = closing_date.isoformat()
        if title_company is not None:
            row.title_company = title_company.strip() or None
            changed["title_company"] = row.title_company
        await self._s.flush()
        if changed:
            await self._event(row, EVENT_DEADLINES_SET, actor, actor_id, changed)
        return row

    async def select_buyer(
        self,
        contract_id: uuid.UUID,
        *,
        investor_id: uuid.UUID,
        actor: Actor,
        actor_id: str | None,
        assignment_fee: Decimal | None = None,
        deposit_amount: Decimal | None = None,
    ) -> Contract:
        """Choosing the assignee is a dollar commitment outside A7's formula -> human (POL-06)."""
        _require_human(actor, "select_buyer")
        row = await self.require(contract_id)
        if row.status is not ContractStatus.SIGNED:
            msg = f"contract {contract_id} is {row.status.value}; select a buyer once signed"
            raise InvalidContractTransition(msg)
        row.buyer_investor_id = investor_id
        if assignment_fee is not None:
            row.assignment_fee = assignment_fee
        if deposit_amount is not None:
            row.deposit_amount = deposit_amount
        await self._s.flush()
        await self._event(
            row,
            EVENT_BUYER_SELECTED,
            actor,
            actor_id,
            {
                "buyer_investor_id": str(investor_id),
                "assignment_fee": str(row.assignment_fee) if row.assignment_fee else None,
                "deposit_amount": str(row.deposit_amount) if row.deposit_amount else None,
            },
        )
        return row

    async def record_deposit(
        self,
        contract_id: uuid.UUID,
        *,
        amount: Decimal,
        received_at: datetime,
        actor: Actor,
        actor_id: str | None,
    ) -> Contract:
        """The buyer's non-refundable deposit arrived (a human confirmed it with title)."""
        _require_human(actor, "record_deposit")
        row = await self.require(contract_id)
        if row.status is not ContractStatus.SIGNED:
            msg = f"contract {contract_id} is {row.status.value}; cannot record a deposit"
            raise InvalidContractTransition(msg)
        if row.buyer_investor_id is None:
            raise InvalidContractTransition("select a buyer before recording a deposit")
        if amount <= 0:
            raise InvalidContractTransition("deposit amount must be positive")
        row.deposit_amount = amount
        row.deposit_received_at = received_at
        await self._s.flush()
        await self._event(
            row,
            EVENT_DEPOSIT_RECEIVED,
            actor,
            actor_id,
            {"amount": str(amount), "received_at": received_at.isoformat()},
        )
        return row

    async def update_checklist(
        self,
        contract_id: uuid.UUID,
        *,
        items: dict[str, bool],
        actor: Actor,
        actor_id: str | None,
    ) -> Contract:
        """Title/escrow coordination ticks (human-maintained)."""
        _require_human(actor, "update_checklist")
        row = await self.require(contract_id)
        row.checklist = {**row.checklist, **{k: bool(v) for k, v in items.items()}}
        await self._s.flush()
        await self._event(row, EVENT_CHECKLIST, actor, actor_id, {"items": dict(items)})
        return row

    async def mark_closed(
        self,
        contract_id: uuid.UUID,
        *,
        realized_assignment_fee: Decimal,
        closed_at: datetime,
        actor: Actor,
        actor_id: str | None,
    ) -> tuple[Contract, bool]:
        """Closing happened; record the realised assignment fee (BB-11)."""
        _require_human(actor, "mark_closed")
        row = await self.require(contract_id)
        if row.status is ContractStatus.CLOSED:
            return row, False
        if row.status is not ContractStatus.SIGNED:
            msg = f"contract {contract_id} is {row.status.value}; only a signed deal closes"
            raise InvalidContractTransition(msg)
        if realized_assignment_fee < 0:
            raise InvalidContractTransition("realized assignment fee cannot be negative")
        row.status = ContractStatus.CLOSED
        row.realized_assignment_fee = realized_assignment_fee
        row.closed_at = closed_at
        await self._s.flush()
        await self._event(
            row,
            EVENT_CLOSED,
            actor,
            actor_id,
            {
                "realized_assignment_fee": str(realized_assignment_fee),
                "closed_at": closed_at.isoformat(),
                "buyer_investor_id": str(row.buyer_investor_id) if row.buyer_investor_id else None,
            },
            key=f"contract:{row.id}:closed",
        )
        return row, True

    async def mark_dead(
        self,
        contract_id: uuid.UUID,
        *,
        reason: str,
        actor: Actor,
        actor_id: str | None,
        at: datetime,
    ) -> tuple[Contract, bool]:
        """Deal death. Earnest money is *not* released here: that is its own human step."""
        _require_human(actor, "mark_dead")
        if not reason.strip():
            raise InvalidContractTransition("a deal-dead reason is required")
        row = await self.require(contract_id)
        if row.status is ContractStatus.CANCELLED:
            return row, False
        if row.status is ContractStatus.CLOSED:
            raise InvalidContractTransition("a closed deal cannot be marked dead")
        row.status = ContractStatus.CANCELLED
        row.deal_dead_reason = reason.strip()
        row.meta = {**row.meta, "dead_at": at.isoformat()}
        await self._s.flush()
        await self._event(
            row,
            EVENT_DEAD,
            actor,
            actor_id,
            {
                "reason": row.deal_dead_reason,
                "earnest_money_status": row.earnest_money_status.value,
                "release_earnest_money_reminder": row.earnest_money_status
                is EarnestMoneyStatus.DEPOSITED,
            },
            key=f"contract:{row.id}:dead",
        )
        return row, True

    # -- the one automated write ----------------------------------------------------------------

    async def record_buyer_response(
        self,
        contract_id: uuid.UUID,
        *,
        investor_id: uuid.UUID,
        response: BuyerResponse,
        proof_of_funds: bool,
        deposit_terms: str | None,
        actor: Actor,
        actor_id: str | None,
        at: datetime,
    ) -> Contract:
        """A buyer said interested / pass. Any actor; touches ``buyer_responses`` only."""
        row = await self.require(contract_id)
        responses = dict(row.buyer_responses)
        responses[str(investor_id)] = {
            "response": response.value,
            "proof_of_funds": bool(proof_of_funds),
            "deposit_terms": (deposit_terms or "").strip() or None,
            "at": at.isoformat(),
            "recorded_by": actor_id,
        }
        row.buyer_responses = responses
        await self._s.flush()
        await self._event(
            row,
            EVENT_BUYER_RESPONSE,
            actor,
            actor_id,
            {
                "investor_id": str(investor_id),
                "response": response.value,
                "proof_of_funds": bool(proof_of_funds),
                "deposit_terms": responses[str(investor_id)]["deposit_terms"],
            },
        )
        return row

    # -- internals -------------------------------------------------------------------------

    async def _event(
        self,
        row: Contract,
        event_type: str,
        actor: Actor,
        actor_id: str | None,
        payload: dict[str, Any],
        *,
        key: str | None = None,
    ) -> None:
        await self._audit.record(
            actor=actor,
            actor_id=actor_id,
            event_type=event_type,
            entity_type=ENTITY_TYPE,
            entity_id=row.id,
            payload=payload,
            correlation_id=f"disposition:{row.parent_contract_id or row.id}",
            idempotency_key=key,
        )


def _address(lead: SellerLead) -> str:
    parts = [lead.property_address_line1]
    tail = ", ".join(p for p in (lead.property_city, lead.property_state) if p)
    if tail:
        parts.append(tail)
    if lead.property_postal_code:
        parts.append(lead.property_postal_code)
    return ", ".join(parts[:2]) + (f" {parts[2]}" if len(parts) > 2 else "")


def contract_address(row: Contract) -> str:
    tail = ", ".join(p for p in (row.property_city, row.property_state) if p)
    text = row.property_address_line1 + (f", {tail}" if tail else "")
    if row.property_postal_code:
        text += f" {row.property_postal_code}"
    return text


__all__ = [
    "EARNEST_MONEY_TRANSITIONS",
    "ENTITY_TYPE",
    "EVENT_BUYER_RESPONSE",
    "EVENT_BUYER_SELECTED",
    "EVENT_CHECKLIST",
    "EVENT_CLOSED",
    "EVENT_CREATED",
    "EVENT_DEAD",
    "EVENT_DEADLINES_SET",
    "EVENT_DEPOSIT_RECEIVED",
    "EVENT_EARNEST_MONEY",
    "EVENT_REQUESTED",
    "EVENT_SENT_FOR_SIGNATURE",
    "EVENT_SIGNED",
    "AwaitingContract",
    "ContractNotFound",
    "ContractRepository",
    "HumanActionRequired",
    "InvalidContractTransition",
    "contract_address",
]
