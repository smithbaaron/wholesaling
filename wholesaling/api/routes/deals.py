"""The deals page: every human checkpoint of the seller-side transaction (REQUIREMENTS.md SEL-10,
SEL-11, BB-11, POL-03i/j/k, POL-06, XC-05, PRE-13, PRE-14).

- ``GET  /deals``                          HTML: offers awaiting a contract; contracts with status,
                                           deadlines, next alert, buyer responses, candidates.
- ``GET  /api/deals``                      JSON twin (tests, tooling).
- ``POST /deals/offers/{offer_id}/psa``    create the PSA row + DocuSign envelope (mock: an id).
- ``POST /deals/{id}/signed``              **mark contract signed**: ``signed_by``/``signed_at``/
                                           envelope id, deadlines; audit ``contract.signed``;
                                           starts ``DispositionWorkflow`` ``disposition:{id}``.
- ``POST /deals/{id}/earnest``             earnest-money transition (none/requested/deposited/
                                           released).
- ``POST /deals/{id}/deadlines``           option period / inspection / closing / title company.
- ``POST /deals/{id}/buyer-response``      interested / pass + proof of funds + deposit terms.
- ``POST /deals/{id}/select-buyer``        choose the assignee.
- ``POST /deals/{id}/assignment``          Assignment Agreement row + envelope (mock).
- ``POST /deals/{id}/assignment/signed``   mark the assignment signed.
- ``POST /deals/{id}/deposit``             buyer deposit received.
- ``POST /deals/{id}/checklist``           title/escrow checklist tick.
- ``POST /deals/{id}/closed``              closing happened; realised assignment fee (BB-11).
- ``POST /deals/{id}/dead``                deal dead.

Every POST is a human action: operator-authenticated (same Basic auth as ``/review``), written
through ``ContractRepository`` with ``actor=human`` and the operator's username, audited, and
then the owning workflow is signalled. **No route here is reachable from an agent or a workflow;
the DocuSign gateway is only ever called from these handlers.** Signal failures after a write are
reported (502) but the human's record stands: the workflow is idempotent on the row state and a
``deadlines_updated`` re-signal from the page recovers it.

HTMX requests get a partial, plain form posts redirect to ``/deals``, API clients get JSON.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.activities.graph import ALERT_EVENT
from wholesaling.api.auth import Operator
from wholesaling.api.deps import (
    SessionDep,
    SettingsDep,
    TemplatesDep,
    TemporalDep,
    is_htmx,
    wants_html,
)
from wholesaling.config.settings import Settings
from wholesaling.db.models.deals import (
    BuyerResponse,
    Contract,
    ContractType,
    EarnestMoneyStatus,
)
from wholesaling.db.models.enums import Actor, ContactChannel
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.sellers import SellerLead
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.contracts import (
    AwaitingContract,
    ContractNotFound,
    ContractRepository,
    HumanActionRequired,
    InvalidContractTransition,
    contract_address,
)
from wholesaling.integrations.docusign import (
    DocuSignError,
    DocuSignGateway,
    EnvelopeStatus,
    build_assignment_request,
    build_psa_request,
    get_docusign,
    template_id_for,
)
from wholesaling.integrations.temporal.interface import TemporalGateway
from wholesaling.logging import get_logger
from wholesaling.signals import (
    SIGNAL_BUYER_RESPONSE,
    SIGNAL_BUYER_SELECTED,
    SIGNAL_DEADLINES_UPDATED,
    SIGNAL_DEAL_CLOSED,
    SIGNAL_DEAL_DEAD,
    SIGNAL_DEPOSIT_RECEIVED,
    BuyerResponseSignal,
    BuyerSelectedSignal,
    DealClosedSignal,
    DealDeadSignal,
    DepositReceivedSignal,
)
from wholesaling.workflows import deadlines as dl
from wholesaling.workflows.disposition import (
    WORKFLOW_NAME,
    DispositionInput,
    disposition_workflow_id,
)

log = get_logger(__name__)
router = APIRouter(tags=["deals"])

EVENT_WORKFLOW_STARTED = "disposition.workflow_started"


# --------------------------------------------------------------------------------------------
# Dependencies and parsing
# --------------------------------------------------------------------------------------------


def get_docusign_dep(request: Request) -> DocuSignGateway:
    """The app's DocuSign gateway (``app.state.docusign``), built from settings on first use.
    Tests set ``app.state.docusign`` to the mock before the first request."""
    gateway = getattr(request.app.state, "docusign", None)
    if gateway is None:
        settings = request.app.state.settings
        assert isinstance(settings, Settings)
        gateway = get_docusign(settings)
        request.app.state.docusign = gateway
    assert isinstance(gateway, DocuSignGateway)
    return gateway


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_dt(value: str | None, name: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        # A bare date or local datetime from the form: end of that day in UTC is too lenient;
        # treat it as the given moment in UTC (operators type dates off the contract).
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_money(value: str | None, name: str, *, required: bool = False) -> Decimal | None:
    if value is None or not value.strip():
        if required:
            raise HTTPException(status_code=422, detail=f"{name} is required")
        return None
    try:
        amount = Decimal(value.replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be a number") from exc
    return amount.quantize(Decimal("0.01"))


def _parse_uuid(value: str, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be a UUID") from exc


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------------------------
# View model
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CandidateView:
    investor_id: str
    name: str
    email: str | None
    response: str | None
    proof_of_funds: bool
    deposit_terms: str | None


@dataclass(slots=True)
class ContractView:
    id: str
    type: str
    status: str
    offer_id: str
    seller_lead_id: str
    address: str
    purchase_price: Decimal | None
    assignment_fee: Decimal | None
    realized_assignment_fee: Decimal | None
    envelope_id: str | None
    envelope_status: str | None
    signed_at: datetime | None
    signed_by: str | None
    earnest_money_status: str
    earnest_money_amount: Decimal | None
    option_period_end: datetime | None
    inspection_deadline: datetime | None
    closing_date: datetime | None
    buyer_investor_id: str | None
    buyer_name: str | None
    deposit_amount: Decimal | None
    deposit_received_at: datetime | None
    title_company: str | None
    closed_at: datetime | None
    deal_dead_reason: str | None
    checklist: dict[str, bool]
    workflow_id: str
    workflow_state: str | None
    next_alert_key: str | None
    next_alert_at: datetime | None
    alerts_raised: list[str]
    assignment: ContractView | None
    candidates: list[CandidateView] = field(default_factory=list)
    earnest_next: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, datetime):
                # SQLite hands back naive datetimes for DateTime(timezone=True); they are UTC.
                return (v if v.tzinfo is not None else v.replace(tzinfo=UTC)).isoformat()
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v

        return {k: conv(v) for k, v in asdict(self).items()}


CHECKLIST_ITEMS: tuple[tuple[str, str], ...] = (
    ("title_opened", "Title opened with the title company (PRE-14)"),
    ("earnest_delivered", "Earnest money delivered to title"),
    ("assignment_delivered", "Assignment agreement delivered to title"),
    ("buyer_deposit_at_title", "Buyer deposit confirmed at title"),
    ("closing_scheduled", "Closing scheduled with all parties"),
)


async def _buyer_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, Investor]:
    if not ids:
        return {}
    rows = (await session.execute(select(Investor).where(Investor.id.in_(ids)))).scalars().all()
    return {r.id: r for r in rows}


async def _emails(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    stmt = (
        select(Contact.investor_id, Contact.value)
        .where(Contact.channel == ContactChannel.EMAIL, Contact.investor_id.in_(ids))
        .order_by(Contact.is_primary.desc(), Contact.created_at)
    )
    out: dict[uuid.UUID, str] = {}
    for inv_id, value in (await session.execute(stmt)).all():
        out.setdefault(inv_id, value)
    return out


def _view(
    row: Contract,
    *,
    investors: dict[uuid.UUID, Investor],
    emails: dict[uuid.UUID, str],
    alerts: list[str],
    assignment: Contract | None,
) -> ContractView:
    wf = dict(row.meta.get("workflow") or {})
    buyer = investors.get(row.buyer_investor_id) if row.buyer_investor_id else None
    candidates: list[CandidateView] = []
    for inv_id_text, resp in sorted(row.buyer_responses.items()):
        with contextlib.suppress(ValueError):
            inv_id = uuid.UUID(inv_id_text)
            inv = investors.get(inv_id)
            candidates.append(
                CandidateView(
                    investor_id=inv_id_text,
                    name=inv.canonical_name if inv else inv_id_text,
                    email=emails.get(inv_id),
                    response=resp.get("response"),
                    proof_of_funds=bool(resp.get("proof_of_funds")),
                    deposit_terms=resp.get("deposit_terms"),
                )
            )
    next_at = wf.get("next_alert_at")
    from wholesaling.db.repositories.contracts import EARNEST_MONEY_TRANSITIONS

    return ContractView(
        id=str(row.id),
        type=row.type.value,
        status=row.status.value,
        offer_id=str(row.offer_id),
        seller_lead_id=str(row.seller_lead_id),
        address=contract_address(row),
        purchase_price=row.purchase_price,
        assignment_fee=row.assignment_fee,
        realized_assignment_fee=row.realized_assignment_fee,
        envelope_id=row.docusign_envelope_id,
        envelope_status=row.envelope_status,
        signed_at=row.signed_at,
        signed_by=row.signed_by,
        earnest_money_status=row.earnest_money_status.value,
        earnest_money_amount=row.earnest_money_amount,
        option_period_end=row.option_period_end,
        inspection_deadline=row.inspection_deadline,
        closing_date=row.closing_date,
        buyer_investor_id=str(row.buyer_investor_id) if row.buyer_investor_id else None,
        buyer_name=buyer.canonical_name if buyer else None,
        deposit_amount=row.deposit_amount,
        deposit_received_at=row.deposit_received_at,
        title_company=row.title_company,
        closed_at=row.closed_at,
        deal_dead_reason=row.deal_dead_reason,
        checklist={k: bool(row.checklist.get(k)) for k, _ in CHECKLIST_ITEMS},
        workflow_id=disposition_workflow_id(str(row.id)),
        workflow_state=wf.get("state"),
        next_alert_key=wf.get("next_alert_key"),
        next_alert_at=datetime.fromisoformat(next_at) if isinstance(next_at, str) else None,
        alerts_raised=alerts,
        assignment=(
            _view(assignment, investors=investors, emails=emails, alerts=[], assignment=None)
            if assignment is not None
            else None
        ),
        candidates=candidates,
        earnest_next=[s.value for s in sorted(EARNEST_MONEY_TRANSITIONS[row.earnest_money_status])],
    )


async def _load(session: AsyncSession) -> tuple[list[AwaitingContract], list[ContractView]]:
    repo = ContractRepository(session)
    awaiting = await repo.awaiting_contract()
    psas = await repo.list_psa()
    ids: set[uuid.UUID] = set()
    for row in psas:
        if row.buyer_investor_id:
            ids.add(row.buyer_investor_id)
        for k in row.buyer_responses:
            with contextlib.suppress(ValueError):
                ids.add(uuid.UUID(k))
    investors = await _buyer_names(session, ids)
    emails = await _emails(session, ids)
    audit = AuditRepository(session)
    views: list[ContractView] = []
    for row in psas:
        alerts = [
            str(e.payload.get("alert_key"))
            for e in await audit.for_entity("contract", row.id)
            if e.event_type == ALERT_EVENT and e.payload.get("alert_key")
        ]
        assignment = await repo.assignment_for(row.id)
        views.append(
            _view(row, investors=investors, emails=emails, alerts=alerts, assignment=assignment)
        )
    return awaiting, views


def _awaiting_dict(a: AwaitingContract) -> dict[str, Any]:
    return {
        "offer_id": str(a.offer_id),
        "seller_lead_id": str(a.seller_lead_id),
        "property_address": a.property_address,
        "offer_price": str(a.offer_price) if a.offer_price is not None else None,
        "assignment_fee": str(a.assignment_fee) if a.assignment_fee is not None else None,
        "accepted_at": a.accepted_at.isoformat() if a.accepted_at else None,
    }


# --------------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------------


@router.get("/deals", response_class=HTMLResponse)
async def deals_page(
    request: Request, operator: Operator, session: SessionDep, templates: TemplatesDep
) -> Response:
    awaiting, contracts = await _load(session)
    return templates.TemplateResponse(
        request,
        "deals/deals.html",
        {
            "operator": operator,
            "awaiting": awaiting,
            "contracts": contracts,
            "checklist_items": CHECKLIST_ITEMS,
            "earnest_statuses": [s.value for s in EarnestMoneyStatus],
            "now": _now(),
        },
    )


@router.get("/api/deals")
async def deals_json(operator: Operator, session: SessionDep) -> dict[str, Any]:
    awaiting, contracts = await _load(session)
    return {
        "awaiting_contract": [_awaiting_dict(a) for a in awaiting],
        "contracts": [c.as_dict() for c in contracts],
    }


# --------------------------------------------------------------------------------------------
# Action plumbing
# --------------------------------------------------------------------------------------------


def _respond(
    request: Request,
    templates: TemplatesDep,
    result: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    if is_htmx(request):
        return templates.TemplateResponse(
            request, "deals/_deal_result.html", result, status_code=status_code
        )
    if wants_html(request):
        return RedirectResponse("/deals", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(result, status_code=status_code)


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HumanActionRequired):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ContractNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, InvalidContractTransition):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, DocuSignError):
        return HTTPException(status_code=502, detail=f"DocuSign: {exc}")
    raise exc


async def _signal(
    temporal: TemporalGateway, workflow_id: str, signal: str, payload: Any = None
) -> str | None:
    """Best-effort signal after the human's write. Returns an error string, never raises."""
    try:
        if payload is None:
            await temporal.signal(workflow_id, signal)
        else:
            await temporal.signal(workflow_id, signal, payload)
    except Exception as exc:
        log.error(
            "deals.signal_failed", workflow_id=workflow_id, signal=signal, error=str(exc)[:300]
        )
        return f"could not signal {workflow_id} ({signal}): {exc}"
    return None


async def _seller_contact(session: AsyncSession, lead_id: uuid.UUID) -> tuple[str, str | None]:
    lead = await session.get(SellerLead, lead_id)
    if lead is None:
        raise ContractNotFound(f"seller lead {lead_id} not found")
    email: str | None = None
    for ch in lead.contact_channels or []:
        if str(ch.get("channel", "")).lower() == "email" and ch.get("value"):
            email = str(ch["value"])
            break
    return (lead.owner_name or "Seller"), email


def _fields(row: Contract) -> dict[str, Any]:
    return {
        "property_address": contract_address(row),
        "purchase_price": row.purchase_price,
        "earnest_money_amount": row.earnest_money_amount,
        "inspection_deadline": row.inspection_deadline,
        "closing_date": row.closing_date,
        "title_company": row.title_company,
        "assignment_fee": row.assignment_fee,
        "assignee_deposit": row.deposit_amount,
    }


# --------------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------------


@router.post("/deals/offers/{offer_id}/psa")
async def generate_psa(
    request: Request,
    operator: Operator,
    session: SessionDep,
    settings: SettingsDep,
    templates: TemplatesDep,
    offer_id: uuid.UUID,
    seller_name: Annotated[str | None, Form()] = None,
    seller_email: Annotated[str | None, Form()] = None,
    title_company: Annotated[str | None, Form()] = None,
    earnest_money_amount: Annotated[str | None, Form()] = None,
) -> Response:
    """Human: create the PSA row for an accepted offer and its DocuSign envelope (a *draft*
    envelope - the human sends it from DocuSign after reading it; POL-03i)."""
    docusign = get_docusign_dep(request)
    repo = ContractRepository(session)
    try:
        row, created = await repo.create_from_accepted_offer(
            offer_id, actor=Actor.HUMAN, actor_id=operator, title_company=title_company
        )
        if earnest_money_amount:
            row.earnest_money_amount = _parse_money(earnest_money_amount, "earnest_money_amount")
        name, email = await _seller_contact(session, row.seller_lead_id)
        seller = (seller_name or name).strip()
        to = (seller_email or email or "").strip()
        if not to:
            raise HTTPException(
                status_code=422,
                detail="seller_email is required: the seller lead has no email contact",
            )
        envelope = await docusign.create_envelope_from_template(
            build_psa_request(
                template_id=template_id_for(settings, "psa"),
                seller_name=seller,
                seller_email=to,
                buyer_entity=settings.sender_company,
                buyer_email=settings.graph_mailbox or "deals@example.test",
                fields=_fields(row),
                idempotency_key=f"psa:{row.id}",
                status=EnvelopeStatus.CREATED,
            )
        )
        row = await repo.mark_sent_for_signature(
            row.id,
            envelope_id=envelope.envelope_id,
            envelope_status=envelope.status.value,
            actor=Actor.HUMAN,
            actor_id=operator,
            at=_now(),
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition, DocuSignError) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    log.info("deals.psa_generated", contract_id=str(row.id), envelope_id=envelope.envelope_id)
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "generate_psa",
            "contract_id": str(row.id),
            "created": created,
            "status": row.status.value,
            "envelope_id": envelope.envelope_id,
            "envelope_status": envelope.status.value,
            "detail": None,
        },
    )


@router.post("/deals/{contract_id}/signed")
async def mark_signed(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    settings: SettingsDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    signed_by: Annotated[str | None, Form()] = None,
    signed_at: Annotated[str | None, Form()] = None,
    envelope_id: Annotated[str | None, Form()] = None,
    option_period_end: Annotated[str | None, Form()] = None,
    inspection_deadline: Annotated[str | None, Form()] = None,
    closing_date: Annotated[str | None, Form()] = None,
    title_company: Annotated[str | None, Form()] = None,
) -> Response:
    """**The** human checkpoint (SEL-11): the operator saw the executed contract and records it.
    Writes ``contract.signed`` and starts ``DispositionWorkflow`` ``disposition:{id}``."""
    repo = ContractRepository(session)
    when = _parse_dt(signed_at, "signed_at") or _now()
    dates = {
        "option_period_end": _parse_dt(option_period_end, "option_period_end"),
        "inspection_deadline": _parse_dt(inspection_deadline, "inspection_deadline"),
        "closing_date": _parse_dt(closing_date, "closing_date"),
    }
    try:
        row, changed = await repo.mark_signed(
            contract_id,
            signed_by=(signed_by or operator).strip(),
            signed_at=when,
            envelope_id=(envelope_id or "").strip() or None,
            actor=Actor.HUMAN,
            actor_id=operator,
        )
        await repo.set_deadlines(
            contract_id,
            actor=Actor.HUMAN,
            actor_id=operator,
            title_company=title_company,
            **dates,
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc

    workflow_id = disposition_workflow_id(str(row.id))
    started = False
    if row.type is ContractType.PSA:
        try:
            await temporal.start_workflow(
                WORKFLOW_NAME,
                [DispositionInput(contract_id=str(row.id))],
                workflow_id=workflow_id,
                task_queue=settings.temporal_task_queue,
            )
            started = True
        except Exception as exc:
            await session.rollback()
            log.error("deals.workflow_start_failed", workflow_id=workflow_id, error=str(exc)[:300])
            return _respond(
                request,
                templates,
                {
                    "ok": False,
                    "action": "mark_signed",
                    "contract_id": str(contract_id),
                    "detail": f"could not start {workflow_id}: {exc}; nothing was recorded",
                },
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        if changed:
            await AuditRepository(session).record(
                actor=Actor.HUMAN,
                actor_id=operator,
                event_type=EVENT_WORKFLOW_STARTED,
                entity_type="contract",
                entity_id=row.id,
                payload={"workflow_id": workflow_id, "workflow": WORKFLOW_NAME},
                correlation_id=workflow_id,
                idempotency_key=f"disp:{row.id}:workflow_started",
            )
    elif row.parent_contract_id is not None:
        await _signal(
            temporal, disposition_workflow_id(str(row.parent_contract_id)), SIGNAL_DEADLINES_UPDATED
        )
    await session.commit()
    log.info(
        "deals.contract_signed",
        contract_id=str(row.id),
        signed_by=row.signed_by,
        workflow_id=workflow_id,
        started=started,
        operator=operator,
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "mark_signed",
            "contract_id": str(row.id),
            "status": row.status.value,
            "signed_by": row.signed_by,
            "signed_at": row.signed_at.isoformat() if row.signed_at else None,
            "envelope_id": row.docusign_envelope_id,
            "workflow_id": workflow_id if row.type is ContractType.PSA else None,
            "workflow_started": started,
            "changed": changed,
            "detail": None,
        },
    )


@router.post("/deals/{contract_id}/earnest")
async def earnest_money(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    status_value: Annotated[str, Form(alias="status")],
    amount: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> Response:
    """Human: earnest money requested / deposited / released (POL-03j)."""
    try:
        target = EarnestMoneyStatus(status_value.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown earnest money status {status_value!r}"
        ) from exc
    try:
        row = await ContractRepository(session).set_earnest_money(
            contract_id,
            status=target,
            amount=_parse_money(amount, "amount"),
            actor=Actor.HUMAN,
            actor_id=operator,
            note=note,
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(temporal, disposition_workflow_id(str(row.id)), SIGNAL_DEADLINES_UPDATED)
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "earnest_money",
            "contract_id": str(row.id),
            "earnest_money_status": row.earnest_money_status.value,
            "earnest_money_amount": str(row.earnest_money_amount)
            if row.earnest_money_amount
            else None,
            "detail": err,
        },
    )


@router.post("/deals/{contract_id}/deadlines")
async def set_deadlines(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    option_period_end: Annotated[str | None, Form()] = None,
    inspection_deadline: Annotated[str | None, Form()] = None,
    closing_date: Annotated[str | None, Form()] = None,
    title_company: Annotated[str | None, Form()] = None,
) -> Response:
    try:
        row = await ContractRepository(session).set_deadlines(
            contract_id,
            actor=Actor.HUMAN,
            actor_id=operator,
            option_period_end=_parse_dt(option_period_end, "option_period_end"),
            inspection_deadline=_parse_dt(inspection_deadline, "inspection_deadline"),
            closing_date=_parse_dt(closing_date, "closing_date"),
            title_company=title_company,
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(temporal, disposition_workflow_id(str(row.id)), SIGNAL_DEADLINES_UPDATED)
    sched = dl.schedule(
        dl.parse_deadlines(
            {
                "option_period_end": row.option_period_end.isoformat()
                if row.option_period_end
                else None,
                "inspection_deadline": row.inspection_deadline.isoformat()
                if row.inspection_deadline
                else None,
                "closing_date": row.closing_date.isoformat() if row.closing_date else None,
            }
        )
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "set_deadlines",
            "contract_id": str(row.id),
            "alerts": [{"key": a.key, "due_at": a.due_at.isoformat()} for a in sched],
            "detail": err,
        },
    )


@router.post("/deals/{contract_id}/buyer-response")
async def buyer_response(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    investor_id: Annotated[str, Form()],
    response: Annotated[str, Form()],
    proof_of_funds: Annotated[str | None, Form()] = None,
    deposit_terms: Annotated[str | None, Form()] = None,
) -> Response:
    """A buyer's answer, typed by the operator (the inbound pipeline will call the same
    activity later). Never selects the buyer."""
    try:
        resp = BuyerResponse(response.strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="response must be interested or pass") from exc
    inv_id = _parse_uuid(investor_id, "investor_id")
    try:
        row = await ContractRepository(session).record_buyer_response(
            contract_id,
            investor_id=inv_id,
            response=resp,
            proof_of_funds=_bool(proof_of_funds),
            deposit_terms=deposit_terms,
            actor=Actor.HUMAN,
            actor_id=operator,
            at=_now(),
        )
    except (ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(
        temporal,
        disposition_workflow_id(str(row.id)),
        SIGNAL_BUYER_RESPONSE,
        BuyerResponseSignal(
            investor_id=str(inv_id),
            response=resp.value,
            proof_of_funds=_bool(proof_of_funds),
            deposit_terms=(deposit_terms or "").strip() or None,
        ),
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "buyer_response",
            "contract_id": str(row.id),
            "investor_id": str(inv_id),
            "response": resp.value,
            "responses": row.buyer_responses,
            "detail": err,
        },
    )


@router.post("/deals/{contract_id}/select-buyer")
async def select_buyer(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    investor_id: Annotated[str, Form()],
    assignment_fee: Annotated[str | None, Form()] = None,
    deposit_amount: Annotated[str | None, Form()] = None,
) -> Response:
    inv_id = _parse_uuid(investor_id, "investor_id")
    if await session.get(Investor, inv_id) is None:
        raise HTTPException(status_code=404, detail=f"investor {inv_id} not found")
    try:
        row = await ContractRepository(session).select_buyer(
            contract_id,
            investor_id=inv_id,
            actor=Actor.HUMAN,
            actor_id=operator,
            assignment_fee=_parse_money(assignment_fee, "assignment_fee"),
            deposit_amount=_parse_money(deposit_amount, "deposit_amount"),
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(
        temporal,
        disposition_workflow_id(str(row.id)),
        SIGNAL_BUYER_SELECTED,
        BuyerSelectedSignal(investor_id=str(inv_id), actor_id=operator),
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "select_buyer",
            "contract_id": str(row.id),
            "buyer_investor_id": str(inv_id),
            "assignment_fee": str(row.assignment_fee) if row.assignment_fee else None,
            "deposit_amount": str(row.deposit_amount) if row.deposit_amount else None,
            "detail": err,
        },
    )


@router.post("/deals/{contract_id}/assignment")
async def generate_assignment(
    request: Request,
    operator: Operator,
    session: SessionDep,
    settings: SettingsDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    assignee_email: Annotated[str | None, Form()] = None,
) -> Response:
    """Human: Assignment Agreement row + DocuSign envelope (draft) for the selected buyer."""
    docusign = get_docusign_dep(request)
    repo = ContractRepository(session)
    try:
        psa = await repo.require(contract_id)
        row, created = await repo.create_assignment(psa.id, actor=Actor.HUMAN, actor_id=operator)
        assert psa.buyer_investor_id is not None
        buyer = await session.get(Investor, psa.buyer_investor_id)
        emails = await _emails(session, {psa.buyer_investor_id})
        to = (assignee_email or emails.get(psa.buyer_investor_id) or "").strip()
        if not to:
            raise HTTPException(
                status_code=422, detail="assignee_email is required: the buyer has no email contact"
            )
        envelope = await docusign.create_envelope_from_template(
            build_assignment_request(
                template_id=template_id_for(settings, "assignment"),
                assignor_entity=settings.sender_company,
                assignor_email=settings.graph_mailbox or "deals@example.test",
                assignee_name=buyer.canonical_name if buyer else to,
                assignee_email=to,
                fields={
                    **_fields(psa),
                    "original_contract_date": psa.signed_at,
                    "earnest_money_credit": psa.earnest_money_amount,
                },
                idempotency_key=f"assignment:{row.id}",
                status=EnvelopeStatus.CREATED,
            )
        )
        row = await repo.mark_sent_for_signature(
            row.id,
            envelope_id=envelope.envelope_id,
            envelope_status=envelope.status.value,
            actor=Actor.HUMAN,
            actor_id=operator,
            at=_now(),
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition, DocuSignError) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "generate_assignment",
            "contract_id": str(psa.id),
            "assignment_id": str(row.id),
            "created": created,
            "status": row.status.value,
            "envelope_id": envelope.envelope_id,
            "envelope_status": envelope.status.value,
            "detail": None,
        },
    )


@router.post("/deals/{contract_id}/assignment/signed")
async def assignment_signed(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    signed_by: Annotated[str | None, Form()] = None,
    signed_at: Annotated[str | None, Form()] = None,
    envelope_id: Annotated[str | None, Form()] = None,
) -> Response:
    repo = ContractRepository(session)
    try:
        assignment = await repo.assignment_for(contract_id)
        if assignment is None:
            raise ContractNotFound(f"PSA {contract_id} has no assignment row yet")
        row, changed = await repo.mark_signed(
            assignment.id,
            signed_by=(signed_by or operator).strip(),
            signed_at=_parse_dt(signed_at, "signed_at") or _now(),
            envelope_id=(envelope_id or "").strip() or None,
            actor=Actor.HUMAN,
            actor_id=operator,
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(
        temporal, disposition_workflow_id(str(contract_id)), SIGNAL_DEADLINES_UPDATED
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "assignment_signed",
            "contract_id": str(contract_id),
            "assignment_id": str(row.id),
            "status": row.status.value,
            "changed": changed,
            "detail": err,
        },
    )


@router.post("/deals/{contract_id}/deposit")
async def record_deposit(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    amount: Annotated[str, Form()],
    received_at: Annotated[str | None, Form()] = None,
) -> Response:
    money = _parse_money(amount, "amount", required=True)
    assert money is not None
    when = _parse_dt(received_at, "received_at") or _now()
    try:
        row = await ContractRepository(session).record_deposit(
            contract_id, amount=money, received_at=when, actor=Actor.HUMAN, actor_id=operator
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(
        temporal,
        disposition_workflow_id(str(row.id)),
        SIGNAL_DEPOSIT_RECEIVED,
        DepositReceivedSignal(amount=str(money), received_at=when.isoformat(), actor_id=operator),
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "record_deposit",
            "contract_id": str(row.id),
            "deposit_amount": str(row.deposit_amount),
            "deposit_received_at": when.isoformat(),
            "detail": err,
        },
    )


@router.post("/deals/{contract_id}/checklist")
async def checklist(
    request: Request,
    operator: Operator,
    session: SessionDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    item: Annotated[str, Form()],
    value: Annotated[str | None, Form()] = None,
) -> Response:
    keys = {k for k, _ in CHECKLIST_ITEMS}
    if item not in keys:
        raise HTTPException(status_code=422, detail=f"unknown checklist item {item!r}")
    try:
        row = await ContractRepository(session).update_checklist(
            contract_id, items={item: _bool(value)}, actor=Actor.HUMAN, actor_id=operator
        )
    except (HumanActionRequired, ContractNotFound) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "checklist",
            "contract_id": str(row.id),
            "checklist": {k: bool(row.checklist.get(k)) for k in keys},
            "detail": None,
        },
    )


@router.post("/deals/{contract_id}/closed")
async def mark_closed(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    realized_assignment_fee: Annotated[str, Form()],
    closed_at: Annotated[str | None, Form()] = None,
) -> Response:
    fee = _parse_money(realized_assignment_fee, "realized_assignment_fee", required=True)
    assert fee is not None
    when = _parse_dt(closed_at, "closed_at") or _now()
    try:
        row, changed = await ContractRepository(session).mark_closed(
            contract_id,
            realized_assignment_fee=fee,
            closed_at=when,
            actor=Actor.HUMAN,
            actor_id=operator,
        )
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(
        temporal,
        disposition_workflow_id(str(row.id)),
        SIGNAL_DEAL_CLOSED,
        DealClosedSignal(
            realized_assignment_fee=str(fee), closed_at=when.isoformat(), actor_id=operator
        ),
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "mark_closed",
            "contract_id": str(row.id),
            "status": row.status.value,
            "realized_assignment_fee": str(fee),
            "closed_at": when.isoformat(),
            "changed": changed,
            "detail": err,
        },
    )


@router.post("/deals/{contract_id}/dead")
async def mark_dead(
    request: Request,
    operator: Operator,
    session: SessionDep,
    temporal: TemporalDep,
    templates: TemplatesDep,
    contract_id: uuid.UUID,
    reason: Annotated[str, Form()],
    void_envelope: Annotated[str | None, Form()] = None,
) -> Response:
    """Human: deal dead. Optionally voids an in-process envelope (mock/live) - the operator's
    explicit choice, never automatic. Earnest-money release stays its own human step."""
    docusign = get_docusign_dep(request)
    repo = ContractRepository(session)
    voided: str | None = None
    try:
        row, changed = await repo.mark_dead(
            contract_id, reason=reason, actor=Actor.HUMAN, actor_id=operator, at=_now()
        )
        if changed and _bool(void_envelope) and row.docusign_envelope_id:
            with contextlib.suppress(DocuSignError):
                info = await docusign.void_envelope(row.docusign_envelope_id, reason.strip())
                row.envelope_status = info.status.value
                voided = info.status.value
    except (HumanActionRequired, ContractNotFound, InvalidContractTransition) as exc:
        raise _map_error(exc) from exc
    await session.commit()
    err = await _signal(
        temporal,
        disposition_workflow_id(str(row.id)),
        SIGNAL_DEAL_DEAD,
        DealDeadSignal(reason=row.deal_dead_reason or reason, actor_id=operator),
    )
    return _respond(
        request,
        templates,
        {
            "ok": True,
            "action": "mark_dead",
            "contract_id": str(row.id),
            "status": row.status.value,
            "reason": row.deal_dead_reason,
            "earnest_money_status": row.earnest_money_status.value,
            "release_earnest_money_reminder": row.earnest_money_status
            is EarnestMoneyStatus.DEPOSITED,
            "envelope_status": voided,
            "changed": changed,
            "detail": err,
        },
    )


__all__ = [
    "CHECKLIST_ITEMS",
    "EVENT_WORKFLOW_STARTED",
    "ContractView",
    "deals_json",
    "deals_page",
    "get_docusign_dep",
    "router",
]
