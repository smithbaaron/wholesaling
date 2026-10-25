"""/digest, /api/digest and /digest.txt: the on-demand KPI view behind operator auth."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.conftest import seed_queued_message
from tests.reporting.conftest import seed_kpis

SF = async_sessionmaker[AsyncSession]


async def test_api_digest_empty(auth_client: AsyncClient) -> None:
    r = await auth_client.get("/api/digest")
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "America/Chicago"
    assert body["queue"] == {"depth": 0, "oldest_age_seconds": None}
    assert body["today"]["messages_sent"] == 0
    assert len(body["readiness"]) == 12


async def test_api_digest_for_a_given_day(auth_client: AsyncClient, session_factory: SF) -> None:
    async with session_factory() as s:
        await seed_kpis(s)
    r = await auth_client.get("/api/digest", params={"day": "2026-10-20"})
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == "2026-10-20"
    assert body["today"]["messages_sent"] == 3
    assert body["rolling_7d"]["messages_sent"] == 5
    assert body["queue"]["depth"] == 3
    assert (await auth_client.get("/api/digest", params={"day": "yesterday"})).status_code == 422


async def test_digest_page_and_text(auth_client: AsyncClient, session_factory: SF) -> None:
    async with session_factory() as s:
        await seed_queued_message(s)
        await s.commit()
    page = await auth_client.get("/digest")
    assert page.status_code == 200
    assert "Daily digest" in page.text
    assert "Awaiting review" in page.text
    assert "07:00" in page.text
    assert "America/Chicago" in page.text
    assert "Promotion readiness (POL-04)" in page.text
    text = await auth_client.get("/digest.txt")
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert "awaiting your decision : 1" in text.text


async def test_digest_routes_require_auth(client: AsyncClient) -> None:
    for path in ("/digest", "/api/digest", "/digest.txt"):
        assert (await client.get(path)).status_code == 401
