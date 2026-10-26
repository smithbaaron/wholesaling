"""The /deals operator page: auth, every human checkpoint, its audit event, the workflow start /
signals through the mock Temporal gateway, and the DocuSign mock envelopes (REQUIREMENTS.md
SEL-10, SEL-11, POL-03i/j, POL-06, XC-05, BB-11)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.conftest import OPERATOR
from tests.disposition_seed import seed_accepted_offer, seed_investor, seed_psa
from wholesaling.api.routes.deals import EVENT_WORKFLOW_STARTED
from wholesaling.db.models import AuditEvent
from wholesaling.db.models.deals import Contract, ContractStatus, ContractType, EarnestMoneyStatus
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.contracts import (
    EVENT_BUYER_RESPONSE,
    EVENT_BUYER_SELECTED,
    EVENT_CLOSED,
    EVENT_DEAD,
    EVENT_DEPOSIT_RECEIVED,
    EVENT_EARNEST_MONEY,
    EVENT_SENT_FOR_SIGNATURE,
    EVENT_SIGNED,
    ContractRepository,
)
from wholesaling.integrations.docusign import EnvelopeStatus, MockDocuSignGateway
from wholesaling.integrations.temporal.mock import MockTemporalGateway

JSON = {"Accept": "application/json"}
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


@pytest.fixture
def docusign(app: FastAPI) -> MockDocuSignGateway:
    gw = MockDocuSignGateway()
    app.state.docusign = gw
    return gw


async def _events(
    session_factory: async_sessionmaker[AsyncSession], contract_id: str
) -> list[AuditEvent]:
    async with session_factory() as s:
        stmt = (
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "contract", AuditEvent.entity_id == uuid.UUID(contract_id)
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        return list((await s.execute(stmt)).scalars().all())


async def _contract(
    session_factory: async_sessionmaker[AsyncSession], contract_id: str
) -> Contract:
    async with session_factory() as s:
        return await ContractRepository(s).require(uuid.UUID(contract_id))


# -- auth ------------------------------------------------------------------------------------------


async def test_deals_requires_operator_auth(client: AsyncClient) -> None:
    for path in ("/deals", "/api/deals"):
        r = await client.get(path)
        assert r.status_code == 401 and r.headers["WWW-Authenticate"].startswith("Basic")
    r = await client.post(f"/deals/{uuid.uuid4()}/signed", data={})
    assert r.status_code == 401
    r = await client.post(f"/deals/offers/{uuid.uuid4()}/psa", data={})
    assert r.status_code == 401


async def test_empty_page_and_json(auth_client: AsyncClient) -> None:
    r = await auth_client.get("/deals")
    assert r.status_code == 200 and "No accepted offers waiting" in r.text
    assert "never automated" in r.text
    j = await auth_client.get("/api/deals", headers=JSON)
    assert j.json() == {"awaiting_contract": [], "contracts": []}


# -- awaiting contract -> PSA envelope -------------------------------------------------------------


async def test_awaiting_contract_then_generate_psa_draft_envelope(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    docusign: MockDocuSignGateway,
) -> None:
    async with session_factory() as s:
        deal = await seed_accepted_offer(s)
        await s.commit()
    j = (await auth_client.get("/api/deals", headers=JSON)).json()
    assert [a["offer_id"] for a in j["awaiting_contract"]] == [str(deal.offer.id)]
    assert j["awaiting_contract"][0]["property_address"] == "1427 Pecan Grove Dr, Houston, TX 77092"
    page = await auth_client.get("/deals")
    assert "Generate PSA envelope" in page.text

    r = await auth_client.post(
        f"/deals/offers/{deal.offer.id}/psa",
        data={"title_company": "Lone Star Title", "earnest_money_amount": "2,500"},
        headers=JSON,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["created"] and body["status"] == "sent_for_signature"
    assert body["envelope_status"] == "created"  # a draft: the human sends it from DocuSign
    envelope = docusign.envelopes[body["envelope_id"]]
    assert envelope.status is EnvelopeStatus.CREATED and envelope.template_key == "psa"
    payload = envelope.raw["payload"]
    assert payload["templateRoles"][0] == {
        "roleName": "Seller",
        "name": "Maria Lopez",
        "email": "seller@example.com",
        "routingOrder": "1",
    }
    assert payload["templateRoles"][1]["name"] == "NorthStack"
    tabs = {t["tabLabel"]: t["value"] for t in payload["prefillTabs"]["textTabs"]}
    assert tabs["property_address"] == "1427 Pecan Grove Dr, Houston, TX 77092"
    assert tabs["purchase_price"] == "185,000.00" and tabs["title_company"] == "Lone Star Title"
    assert tabs["earnest_money_amount"] == "2,500.00"
    row = await _contract(session_factory, body["contract_id"])
    assert row.docusign_envelope_id == body["envelope_id"] and row.earnest_money_amount == Decimal(
        "2500"
    )
    kinds = [e.event_type for e in await _events(session_factory, body["contract_id"])]
    assert kinds == ["contract.created", EVENT_SENT_FOR_SIGNATURE]
    assert all(
        e.actor is Actor.HUMAN and e.actor_id == OPERATOR
        for e in await _events(session_factory, body["contract_id"])
    )
    # Idempotent: a second click reuses the row and the envelope.
    again = (
        await auth_client.post(f"/deals/offers/{deal.offer.id}/psa", data={}, headers=JSON)
    ).json()
    assert again["contract_id"] == body["contract_id"] and not again["created"]
    assert again["envelope_id"] == body["envelope_id"] and len(docusign.envelopes) == 1
    assert (await auth_client.get("/api/deals", headers=JSON)).json()["awaiting_contract"] == []


async def test_generate_psa_needs_a_seller_email_and_an_accepted_offer(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    docusign: MockDocuSignGateway,
) -> None:
    async with session_factory() as s:
        deal = await seed_accepted_offer(s, seller_email=None)
        await s.commit()
    r = await auth_client.post(f"/deals/offers/{deal.offer.id}/psa", data={}, headers=JSON)
    assert r.status_code == 422 and "seller_email" in r.json()["detail"]
    r = await auth_client.post(
        f"/deals/offers/{deal.offer.id}/psa", data={"seller_email": "m@x.com"}, headers=JSON
    )
    assert r.status_code == 200
    r = await auth_client.post(f"/deals/offers/{uuid.uuid4()}/psa", data={}, headers=JSON)
    assert r.status_code == 404


# -- mark signed -----------------------------------------------------------------------------------


async def test_mark_signed_writes_audit_and_starts_disposition_workflow(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
) -> None:
    async with session_factory() as s:
        deal = await seed_accepted_offer(s)
        row = await seed_psa(s, deal)
        await s.commit()
    closing = (NOW + timedelta(days=30)).isoformat()
    r = await auth_client.post(
        f"/deals/{row.id}/signed",
        data={
            "envelope_id": "env-psa-1",
            "closing_date": closing,
            "inspection_deadline": (NOW + timedelta(days=10)).isoformat(),
            "title_company": "Lone Star Title",
        },
        headers=JSON,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "signed" and body["signed_by"] == OPERATOR and body["changed"]
    assert body["workflow_id"] == f"disposition:{row.id}" and body["workflow_started"]
    starts = [c for c in temporal.calls if c.method == "start_workflow"]
    assert len(starts) == 1
    assert starts[0].args["workflow"] == "DispositionWorkflow"
    assert starts[0].args["workflow_id"] == f"disposition:{row.id}"
    assert starts[0].args["args"][0].contract_id == str(row.id)
    fresh = await _contract(session_factory, str(row.id))
    assert fresh.status is ContractStatus.SIGNED and fresh.signed_at is not None
    assert fresh.closing_date is not None and fresh.title_company == "Lone Star Title"
    events = await _events(session_factory, str(row.id))
    signed = [e for e in events if e.event_type == EVENT_SIGNED]
    assert len(signed) == 1 and signed[0].actor is Actor.HUMAN and signed[0].actor_id == OPERATOR
    assert (
        signed[0].payload["signed_by"] == OPERATOR
        and signed[0].payload["envelope_id"] == "env-psa-1"
    )
    assert any(e.event_type == EVENT_WORKFLOW_STARTED for e in events)
    # Second click: no second signed event, workflow start is idempotent on the id.
    again = (await auth_client.post(f"/deals/{row.id}/signed", data={}, headers=JSON)).json()
    assert not again["changed"]
    assert (
        sum(1 for e in await _events(session_factory, str(row.id)) if e.event_type == EVENT_SIGNED)
        == 1
    )
    assert len(temporal.workflows) == 1
    page = await auth_client.get("/deals")
    assert "Signed" in page.text and "disposition" not in page.text.split("Mark deal dead")[0][:0]


async def test_mark_signed_rolls_back_when_workflow_start_fails(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
) -> None:
    async with session_factory() as s:
        deal = await seed_accepted_offer(s)
        row = await seed_psa(s, deal)
        await s.commit()

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("temporal down")

    temporal.start_workflow = boom  # type: ignore[method-assign]
    r = await auth_client.post(f"/deals/{row.id}/signed", data={}, headers=JSON)
    assert r.status_code == 502 and "nothing was recorded" in r.json()["detail"]
    fresh = await _contract(session_factory, str(row.id))
    assert fresh.status is ContractStatus.REQUESTED and fresh.signed_at is None
    assert not any(
        e.event_type == EVENT_SIGNED for e in await _events(session_factory, str(row.id))
    )


async def test_mark_signed_unknown_or_dead_contract(
    auth_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    r = await auth_client.post(f"/deals/{uuid.uuid4()}/signed", data={}, headers=JSON)
    assert r.status_code == 404
    async with session_factory() as s:
        deal = await seed_accepted_offer(s)
        row = await seed_psa(s, deal)
        await ContractRepository(s).mark_dead(
            row.id, reason="x", actor=Actor.HUMAN, actor_id=OPERATOR, at=NOW
        )
        await s.commit()
    r = await auth_client.post(f"/deals/{row.id}/signed", data={}, headers=JSON)
    assert r.status_code == 409
    r = await auth_client.post(
        f"/deals/{row.id}/signed", data={"closing_date": "not-a-date"}, headers=JSON
    )
    assert r.status_code == 422


# -- the rest of the human checkpoints -------------------------------------------------------------


async def _signed(session_factory: async_sessionmaker[AsyncSession]) -> tuple[Contract, uuid.UUID]:
    async with session_factory() as s:
        deal = await seed_accepted_offer(s)
        row = await seed_psa(s, deal, signed=True, closing_date=NOW + timedelta(days=30))
        inv = await seed_investor(s, name="Buyer LLC", email="buyer@x.com", market=deal.market)
        await s.commit()
        return row, inv.id


async def test_earnest_money_transitions_and_audit(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
) -> None:
    row, _ = await _signed(session_factory)
    r = await auth_client.post(
        f"/deals/{row.id}/earnest", data={"status": "deposited"}, headers=JSON
    )
    assert r.status_code == 409  # none -> deposited is not allowed
    r = await auth_client.post(
        f"/deals/{row.id}/earnest", data={"status": "requested", "amount": "2500"}, headers=JSON
    )
    assert r.status_code == 200 and r.json()["earnest_money_status"] == "requested"
    r = await auth_client.post(
        f"/deals/{row.id}/earnest", data={"status": "deposited"}, headers=JSON
    )
    assert (
        r.json()["earnest_money_status"] == "deposited"
        and r.json()["earnest_money_amount"] == "2500.00"
    )
    r = await auth_client.post(f"/deals/{row.id}/earnest", data={"status": "bogus"}, headers=JSON)
    assert r.status_code == 422
    money = [
        e
        for e in await _events(session_factory, str(row.id))
        if e.event_type == EVENT_EARNEST_MONEY
    ]
    assert [(e.payload["from"], e.payload["to"]) for e in money] == [
        ("none", "requested"),
        ("requested", "deposited"),
    ]
    assert all(e.actor is Actor.HUMAN for e in money)
    assert any(
        c.method == "signal" and c.args["signal"] == "deadlines_updated" for c in temporal.calls
    )


async def test_deadlines_route_signals_and_lists_alert_schedule(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
) -> None:
    row, _ = await _signed(session_factory)
    option = NOW + timedelta(days=7)
    r = await auth_client.post(
        f"/deals/{row.id}/deadlines", data={"option_period_end": option.isoformat()}, headers=JSON
    )
    assert r.status_code == 200
    keys = [a["key"] for a in r.json()["alerts"]]
    assert keys[:3] == ["option_period_end:T-3d", "option_period_end:T-1d", "option_period_end:T-0"]
    assert "closing_date:T-0" in keys
    assert temporal.signals[f"disposition:{row.id}"][-1][0] == "deadlines_updated"
    j = (await auth_client.get("/api/deals", headers=JSON)).json()
    assert j["contracts"][0]["option_period_end"] == option.isoformat()


async def test_buyer_response_select_buyer_assignment_deposit_closed(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
    docusign: MockDocuSignGateway,
) -> None:
    row, inv_id = await _signed(session_factory)
    wf = f"disposition:{row.id}"

    r = await auth_client.post(
        f"/deals/{row.id}/buyer-response",
        data={
            "investor_id": str(inv_id),
            "response": "interested",
            "proof_of_funds": "true",
            "deposit_terms": "5k in 24h",
        },
        headers=JSON,
    )
    assert r.status_code == 200 and r.json()["responses"][str(inv_id)]["proof_of_funds"] is True
    sig = temporal.signals[wf][-1]
    assert (
        sig[0] == "buyer_response"
        and sig[1][0].investor_id == str(inv_id)
        and sig[1][0].proof_of_funds
    )
    r = await auth_client.post(
        f"/deals/{row.id}/buyer-response",
        data={"investor_id": str(inv_id), "response": "maybe"},
        headers=JSON,
    )
    assert r.status_code == 422
    page = await auth_client.get("/deals")
    assert "Buyer LLC" in page.text and "Select as buyer" in page.text

    # Assignment before a buyer is selected is refused.
    r = await auth_client.post(f"/deals/{row.id}/assignment", data={}, headers=JSON)
    assert r.status_code == 409
    r = await auth_client.post(f"/deals/{row.id}/deposit", data={"amount": "5000"}, headers=JSON)
    assert r.status_code == 409

    r = await auth_client.post(
        f"/deals/{row.id}/select-buyer",
        data={"investor_id": str(inv_id), "assignment_fee": "17500", "deposit_amount": "5000"},
        headers=JSON,
    )
    assert r.status_code == 200 and r.json()["buyer_investor_id"] == str(inv_id)
    assert temporal.signals[wf][-1][0] == "buyer_selected"
    r = await auth_client.post(
        f"/deals/{row.id}/select-buyer", data={"investor_id": str(uuid.uuid4())}, headers=JSON
    )
    assert r.status_code == 404

    r = await auth_client.post(f"/deals/{row.id}/assignment", data={}, headers=JSON)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["created"]
        and body["status"] == "sent_for_signature"
        and body["envelope_status"] == "created"
    )
    envelope = docusign.envelopes[body["envelope_id"]]
    assert envelope.template_key == "assignment"
    roles = envelope.raw["payload"]["templateRoles"]
    assert (
        roles[0]["roleName"] == "Assignee"
        and roles[0]["email"] == "buyer@x.com"
        and roles[0]["name"] == "Buyer LLC"
    )
    tabs = {t["tabLabel"]: t["value"] for t in envelope.raw["payload"]["prefillTabs"]["textTabs"]}
    assert tabs["assignment_fee"] == "17,500.00" and tabs["assignee_deposit"] == "5,000.00"
    assignment_id = body["assignment_id"]
    async with session_factory() as s:
        asg = await ContractRepository(s).require(uuid.UUID(assignment_id))
        assert asg.type is ContractType.ASSIGNMENT and asg.parent_contract_id == row.id
    again = (await auth_client.post(f"/deals/{row.id}/assignment", data={}, headers=JSON)).json()
    assert again["assignment_id"] == assignment_id and not again["created"]

    r = await auth_client.post(f"/deals/{row.id}/assignment/signed", data={}, headers=JSON)
    assert r.status_code == 200 and r.json()["status"] == "signed" and r.json()["changed"]
    assert not any(
        c.method == "start_workflow" for c in temporal.calls
    )  # assignment never starts a workflow

    r = await auth_client.post(
        f"/deals/{row.id}/deposit", data={"amount": "$5,000.00"}, headers=JSON
    )
    assert r.status_code == 200 and r.json()["deposit_amount"] == "5000.00"
    assert (
        temporal.signals[wf][-1][0] == "deposit_received"
        and temporal.signals[wf][-1][1][0].amount == "5000.00"
    )
    r = await auth_client.post(f"/deals/{row.id}/deposit", data={"amount": "abc"}, headers=JSON)
    assert r.status_code == 422

    r = await auth_client.post(
        f"/deals/{row.id}/checklist", data={"item": "title_opened", "value": "true"}, headers=JSON
    )
    assert r.status_code == 200 and r.json()["checklist"]["title_opened"] is True
    r = await auth_client.post(
        f"/deals/{row.id}/checklist", data={"item": "nope", "value": "true"}, headers=JSON
    )
    assert r.status_code == 422

    r = await auth_client.post(
        f"/deals/{row.id}/closed", data={"realized_assignment_fee": "17000"}, headers=JSON
    )
    assert r.status_code == 200 and r.json()["status"] == "closed" and r.json()["changed"]
    assert (
        temporal.signals[wf][-1][0] == "deal_closed"
        and temporal.signals[wf][-1][1][0].realized_assignment_fee == "17000.00"
    )
    fresh = await _contract(session_factory, str(row.id))
    assert fresh.status is ContractStatus.CLOSED and fresh.realized_assignment_fee == Decimal(
        "17000"
    )
    kinds = [e.event_type for e in await _events(session_factory, str(row.id))]
    for k in (EVENT_BUYER_RESPONSE, EVENT_BUYER_SELECTED, EVENT_DEPOSIT_RECEIVED, EVENT_CLOSED):
        assert k in kinds
    r = await auth_client.post(f"/deals/{row.id}/dead", data={"reason": "oops"}, headers=JSON)
    assert r.status_code == 409  # closed deals do not die
    j = (await auth_client.get("/api/deals", headers=JSON)).json()
    c = j["contracts"][0]
    assert (
        c["status"] == "closed"
        and c["buyer_name"] == "Buyer LLC"
        and c["assignment"]["status"] == "signed"
    )
    assert c["realized_assignment_fee"] == "17000.00" and c["checklist"]["title_opened"] is True


async def test_mark_dead_signals_voids_envelope_and_flags_earnest_release(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
    docusign: MockDocuSignGateway,
) -> None:
    async with session_factory() as s:
        deal = await seed_accepted_offer(s)
        row = await seed_psa(s, deal)
        await s.commit()
    psa = (
        await auth_client.post(f"/deals/offers/{deal.offer.id}/psa", data={}, headers=JSON)
    ).json()
    assert psa["contract_id"] == str(row.id)
    await auth_client.post(
        f"/deals/{row.id}/signed", data={"envelope_id": psa["envelope_id"]}, headers=JSON
    )
    await auth_client.post(
        f"/deals/{row.id}/earnest", data={"status": "requested", "amount": "2500"}, headers=JSON
    )
    await auth_client.post(f"/deals/{row.id}/earnest", data={"status": "deposited"}, headers=JSON)
    docusign.send(psa["envelope_id"])  # the human sent it from DocuSign; still in process

    r = await auth_client.post(f"/deals/{row.id}/dead", data={"reason": "  "}, headers=JSON)
    assert r.status_code == 409
    r = await auth_client.post(
        f"/deals/{row.id}/dead",
        data={"reason": "failed inspection", "void_envelope": "true"},
        headers=JSON,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled" and body["release_earnest_money_reminder"] is True
    assert body["envelope_status"] == "voided"
    assert docusign.envelopes[psa["envelope_id"]].status is EnvelopeStatus.VOIDED
    sig = temporal.signals[f"disposition:{row.id}"][-1]
    assert sig[0] == "deal_dead" and sig[1][0].reason == "failed inspection"
    dead = [e for e in await _events(session_factory, str(row.id)) if e.event_type == EVENT_DEAD]
    assert (
        len(dead) == 1
        and dead[0].actor is Actor.HUMAN
        and dead[0].payload["release_earnest_money_reminder"] is True
    )
    fresh = await _contract(session_factory, str(row.id))
    assert fresh.earnest_money_status is EarnestMoneyStatus.DEPOSITED  # not released by the route
    page = await auth_client.get("/deals")
    assert "Release earnest money (human)" in page.text
    r = await auth_client.post(
        f"/deals/{row.id}/earnest", data={"status": "released"}, headers=JSON
    )
    assert r.status_code == 200 and r.json()["earnest_money_status"] == "released"


async def test_htmx_and_form_responses(
    auth_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row, _ = await _signed(session_factory)
    r = await auth_client.post(
        f"/deals/{row.id}/checklist",
        data={"item": "title_opened", "value": "true"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Checklist recorded" in r.text and "result ok" in r.text
    r = await auth_client.post(
        f"/deals/{row.id}/checklist",
        data={"item": "title_opened", "value": "false"},
        headers={"Accept": "text/html"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/deals"


async def test_signal_failure_is_reported_but_the_human_record_stands(
    auth_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
) -> None:
    row, inv_id = await _signed(session_factory)

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("temporal down")

    temporal.signal = boom  # type: ignore[method-assign]
    r = await auth_client.post(
        f"/deals/{row.id}/select-buyer", data={"investor_id": str(inv_id)}, headers=JSON
    )
    assert r.status_code == 200 and "could not signal" in r.json()["detail"]
    assert (await _contract(session_factory, str(row.id))).buyer_investor_id == inv_id
