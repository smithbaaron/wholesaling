"""Public seller web form: validation, photo upload to blob storage, SEL-12 idempotency, rate
limit, workflow start (REQUIREMENTS.md SEL-06, SEL-08, SEL-12, INT-26, XC-03, CMP-03)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.conftest import operator_settings
from wholesaling.activities.seller import SELLER_INBOUND_WORKFLOW_NAME
from wholesaling.agents.templates.seller_outreach import disclosure
from wholesaling.api.main import create_app
from wholesaling.api.routes.sell import IpRateLimiter
from wholesaling.db import Base, make_session_factory
from wholesaling.db.models.enums import Channel, MarketTier, SellerLeadStatus
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Thread
from wholesaling.db.models.ops import AuditEvent
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.integrations.blob import MockBlobStorage
from wholesaling.integrations.temporal.mock import MockTemporalGateway

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _fields(**over: str) -> dict[str, str]:
    base = {
        "address_line1": "1420 Pecan Grove Dr",
        "city": "Houston",
        "state": "TX",
        "postal_code": "77009",
        "name": "Dolores Ramirez",
        "email": "dolores.ramirez@example.test",
        "phone": "(713) 555-0101",
        "motivation": "Inherited, do not want to be a landlord",
        "condition_class": "Medium",
        "disclosed_issues": "roof\nAC",
        "timeline": "within_30_days",
        "asking_price": "185000",
        "consent_email": "yes",
        "submission_id": str(uuid.uuid4()),
    }
    base.update(over)
    return base


def _photos(n: int, size: int = len(PNG)) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("photos", (f"photo{i}.png", PNG[:size].ljust(size, b"\x00"), "image/png"))
        for i in range(n)
    ]


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    import wholesaling.db.models  # noqa: F401

    application = create_app(
        operator_settings(
            sell_form_rate_limit_per_minute=100,
            sell_form_max_photos=3,
            sell_form_max_photo_bytes=1024,
            sender_postal_address="123 Main St, Houston, TX 77002",
            public_base_url="https://sell.example.test",
        )
    )
    async with application.router.lifespan_context(application):
        async with application.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        application.state.blob = MockBlobStorage()
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def temporal(app: FastAPI) -> MockTemporalGateway:
    gw = app.state.temporal
    assert isinstance(gw, MockTemporalGateway)
    return gw


@pytest.fixture
def blob(app: FastAPI) -> MockBlobStorage:
    store = app.state.blob
    assert isinstance(store, MockBlobStorage)
    return store


@pytest.fixture
def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(app.state.engine)


async def _count(session: AsyncSession, model: type, **where: object) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int((await session.execute(stmt)).scalar_one())


def _starts(temporal: MockTemporalGateway) -> list[Any]:
    return [c for c in temporal.calls if c.method == "start_workflow"]


# -- GET -------------------------------------------------------------------------------------------


async def test_get_renders_the_public_form_with_disclosure_and_token(client: AsyncClient) -> None:
    r = await client.get("/sell")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert 'enctype="multipart/form-data"' in html and 'name="photos"' in html
    assert disclosure("NorthStack") in html  # CMP-03 on the page itself
    assert "contact me by email" in html and "We will not call or text you" in html
    sid = html.split('name="submission_id" value="')[1].split('"')[0]
    uuid.UUID(sid)  # a fresh idempotency key per page load
    assert "123 Main St" in html and "Aaron, NorthStack" in html
    with_lead = await client.get("/sell", params={"lead": "abc-123"})
    assert 'name="lead" value="abc-123"' in with_lead.text


async def test_form_needs_no_auth(client: AsyncClient) -> None:
    assert (await client.get("/sell")).status_code == 200
    assert (await client.get("/review")).status_code == 401  # the operator pages still do


# -- POST: happy path ------------------------------------------------------------------------------


async def test_post_stores_lead_intake_photos_message_audit_and_starts_the_workflow(
    client: AsyncClient,
    temporal: MockTemporalGateway,
    blob: MockBlobStorage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(Market(name="Houston", state="TX", tier=MarketTier.ACTIVE))
        await session.commit()
    fields = _fields()
    sid = fields["submission_id"]
    r = await client.post("/sell", data=fields, files=_photos(2))
    assert r.status_code == 200
    assert "Thank you, Dolores" in r.text and "2 photos" in r.text and sid in r.text
    assert "We have what we need" in r.text
    assert sorted(blob.blobs) == [f"sell/{sid}/01.png", f"sell/{sid}/02.png"]
    async with session_factory() as session:
        lead = (await session.execute(select(SellerLead))).scalar_one()
        assert lead.source == "web_form" and lead.web_form_submission_id == sid
        assert lead.status is SellerLeadStatus.INTAKE and lead.owner_name == "Dolores Ramirez"
        assert (
            lead.address_key == "1420 PECAN GROVE DR|HOUSTON|TX|77009" and not lead.parcel_verified
        )
        assert lead.market_id is not None
        channels = {c["channel"]: c for c in lead.contact_channels}
        assert channels["email"]["is_primary"] and channels["email"]["source"] == "web_form"
        assert "no calls or SMS" in channels["phone"]["use_limitations"]
        intake = (await session.execute(select(Intake))).scalar_one()
        assert [p["blob_key"] for p in intake.photos] == sorted(blob.blobs)
        assert intake.photos[0]["content_type"] == "image/png" and intake.photos[0]["size"] == len(
            PNG
        )
        assert intake.motivation is None  # S4 (the workflow) merges the fields, not the route
        message = (await session.execute(select(Message))).scalar_one()
        assert message.channel is Channel.WEB_FORM and message.external_message_id == f"form:{sid}"
        assert (
            message.meta["form"]["condition_class"] == "Medium"
            and message.from_address == fields["email"]
        )
        thread = await session.get(Thread, message.thread_id)
        assert (
            thread is not None
            and thread.seller_lead_id == lead.id
            and thread.counterpart_address == fields["email"]
        )
        assert await _count(session, AuditEvent, event_type="web_form.submitted") == 1
    starts = _starts(temporal)
    assert len(starts) == 1
    assert starts[0].args["workflow"] == SELLER_INBOUND_WORKFLOW_NAME
    assert starts[0].args["workflow_id"] == f"seller-inbound:form:{sid}"
    inp = starts[0].args["args"][0]
    assert (
        inp.source == "web_form" and inp.submission_id == sid and inp.message_id == str(message.id)
    )


async def test_repost_is_idempotent(
    client: AsyncClient,
    temporal: MockTemporalGateway,
    blob: MockBlobStorage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fields = _fields()
    first = await client.post("/sell", data=fields, files=_photos(1))
    second = await client.post("/sell", data=fields, files=_photos(1))
    assert first.status_code == second.status_code == 200
    assert "already received" in second.text and "already received" not in first.text
    assert len(blob.blobs) == 1 and len(_starts(temporal)) == 1
    async with session_factory() as session:
        assert await _count(session, SellerLead) == 1
        assert await _count(session, Intake) == 1
        assert await _count(session, Message) == 1
        assert await _count(session, AuditEvent, event_type="web_form.submitted") == 1


async def test_missing_submission_id_gets_a_server_issued_one(
    client: AsyncClient, temporal: MockTemporalGateway
) -> None:
    fields = _fields()
    del fields["submission_id"]
    r = await client.post("/sell", data=fields)
    assert r.status_code == 200
    workflow_id = _starts(temporal)[0].args["workflow_id"]
    uuid.UUID(workflow_id.removeprefix("seller-inbound:form:"))


async def test_minimal_submission_tells_the_seller_questions_follow(client: AsyncClient) -> None:
    r = await client.post(
        "/sell",
        data=_fields(motivation="", condition_class="", timeline="", asking_price="", phone=""),
    )
    assert r.status_code == 200 and "one or two quick questions" in r.text


async def test_lead_token_merges_into_the_existing_lead(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        lead = SellerLead(
            status=SellerLeadStatus.INTAKE,
            owner_name="Dolores Ramirez",
            property_address_line1="1420 Pecan Grove Dr",
            property_city="Houston",
            property_state="TX",
            property_postal_code="77009",
            address_key="1420 PECAN GROVE DR|HOUSTON|TX|77009",
            parcel_id="HCAD-D-0000001",
            parcel_verified=True,
            contact_channels=[
                {
                    "channel": "email",
                    "value": "old@example.test",
                    "normalized_value": "old@example.test",
                    "is_primary": True,
                }
            ],
            source="mock_distress_lists",
        )
        session.add(lead)
        await session.commit()
        lead_id = lead.id
    fields = _fields(lead=str(lead_id), address_line1="1420 Pecan Grove Drive")
    r = await client.post("/sell", data=fields, files=_photos(1))
    assert r.status_code == 200
    async with session_factory() as session:
        assert await _count(session, SellerLead) == 1
        merged = await session.get(SellerLead, lead_id)
        assert merged is not None and merged.web_form_submission_id == fields["submission_id"]
        assert merged.parcel_verified and merged.source == "mock_distress_lists"
        emails = [c for c in merged.contact_channels if c["channel"] == "email"]
        assert [c["is_primary"] for c in emails] == [False, True]  # the seller's own address wins
        intake = (await session.execute(select(Intake))).scalar_one()
        assert intake.seller_lead_id == lead_id and len(intake.photos) == 1


async def test_same_address_merges_without_a_token(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await client.post("/sell", data=_fields())
    second = await client.post(
        "/sell", data=_fields(address_line1="1420 Pecan Grove Drive", email="other@example.test")
    )
    assert first.status_code == second.status_code == 200
    async with session_factory() as session:
        assert await _count(session, SellerLead) == 1
        assert await _count(session, Message) == 2  # two submissions, one property


# -- POST: validation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"address_line1": ""}, "street address"),
        ({"name": ""}, "your name"),
        ({"email": "nope"}, "valid email"),
        ({"consent_email": ""}, "contact you by email"),
        ({"condition_class": "Ruined"}, "condition options"),
        ({"timeline": "someday"}, "timeline options"),
        ({"asking_price": "lots"}, "Asking price"),
        ({"state": "Texas"}, "two-letter"),
        ({"phone": "12"}, "valid phone"),
    ],
)
async def test_validation_errors_re_render_the_form_and_store_nothing(
    client: AsyncClient,
    temporal: MockTemporalGateway,
    session_factory: async_sessionmaker[AsyncSession],
    over: dict[str, str],
    message: str,
) -> None:
    fields = _fields(**over)
    r = await client.post("/sell", data=fields)
    assert r.status_code == 422 and message in r.text
    assert f'name="submission_id" value="{fields["submission_id"]}"' in r.text  # same key on retry
    if fields["name"]:
        assert fields["name"] in r.text  # values are kept
    async with session_factory() as session:
        assert await _count(session, SellerLead) == 0
    assert _starts(temporal) == []


async def test_too_many_photos_rejected(client: AsyncClient, blob: MockBlobStorage) -> None:
    r = await client.post("/sell", data=_fields(), files=_photos(4))
    assert r.status_code == 422 and "at most 3 photos" in r.text and blob.blobs == {}


async def test_oversized_and_non_image_uploads_rejected(
    client: AsyncClient, blob: MockBlobStorage
) -> None:
    big = await client.post("/sell", data=_fields(), files=_photos(1, size=2048))
    assert big.status_code == 422 and "larger than" in big.text
    doc = await client.post(
        "/sell", data=_fields(), files=[("photos", ("x.pdf", b"%PDF", "application/pdf"))]
    )
    assert doc.status_code == 422 and "only image files" in doc.text
    assert blob.blobs == {}


async def test_empty_file_input_is_ignored(client: AsyncClient, blob: MockBlobStorage) -> None:
    r = await client.post(
        "/sell", data=_fields(), files=[("photos", ("", b"", "application/octet-stream"))]
    )
    assert r.status_code == 200 and blob.blobs == {}


# -- rate limit ------------------------------------------------------------------------------------


def test_rate_limiter_is_a_sliding_minute_per_ip() -> None:
    clock = {"t": 0.0}
    limiter = IpRateLimiter(2, clock=lambda: clock["t"])
    assert limiter.allow("1.1.1.1") and limiter.allow("1.1.1.1") and not limiter.allow("1.1.1.1")
    assert limiter.allow("2.2.2.2")  # another client is unaffected
    clock["t"] = 61.0
    assert limiter.allow("1.1.1.1")


async def test_rate_limited_requests_get_429(app: FastAPI, client: AsyncClient) -> None:
    app.state.sell_rate_limiter = IpRateLimiter(2)
    assert (await client.get("/sell")).status_code == 200
    assert (await client.get("/sell")).status_code == 200
    limited = await client.get("/sell")
    assert limited.status_code == 429
    posted = await client.post("/sell", data=_fields())
    assert posted.status_code == 429
    other = await client.get("/sell", headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert other.status_code == 200
