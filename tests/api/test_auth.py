"""Operator HTTP Basic auth: hash format, constant-time verify, 401/503 behaviour, open routes."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, BasicAuth

from tests.api.conftest import OPERATOR, PASSWORD, PASSWORD_HASH, operator_settings
from wholesaling.api.auth import DEFAULT_ITERATIONS, SCHEME, hash_password, verify_password
from wholesaling.api.main import create_app


def test_hash_format_and_roundtrip() -> None:
    h = hash_password("s3cret", iterations=2_000)
    scheme, iterations, salt, digest = h.split("$")
    assert scheme == SCHEME
    assert int(iterations) == 2_000
    assert len(bytes.fromhex(salt)) == 16
    assert len(bytes.fromhex(digest)) == 32
    assert verify_password("s3cret", h) is True
    assert verify_password("S3cret", h) is False


def test_hash_default_iterations_is_strong() -> None:
    assert DEFAULT_ITERATIONS >= 600_000
    h = hash_password("x")
    assert h.split("$")[1] == str(DEFAULT_ITERATIONS)


def test_hash_uses_random_salt() -> None:
    assert hash_password("same", iterations=1_000) != hash_password("same", iterations=1_000)


def test_hash_rejects_empty_password_and_bad_iterations() -> None:
    with pytest.raises(ValueError, match="empty"):
        hash_password("")
    with pytest.raises(ValueError, match="positive"):
        hash_password("x", iterations=0)


@pytest.mark.parametrize(
    "stored",
    [None, "", "plain", "md5$1$00$00", "pbkdf2_sha256$notint$00$00", "pbkdf2_sha256$1$zz$00"],
)
def test_verify_malformed_hash_is_false(stored: str | None) -> None:
    assert verify_password("anything", stored) is False


def test_verify_timing_does_not_leak_malformed_vs_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed stored hash still runs a full-cost derivation (same code path cost)."""
    import wholesaling.api.auth as auth

    calls: list[int] = []
    real = auth.hashlib.pbkdf2_hmac
    genuine = hash_password("other", iterations=DEFAULT_ITERATIONS)

    def spy(name: str, pw: bytes, salt: bytes, iterations: int, dklen: int | None = None) -> bytes:
        calls.append(iterations)
        return real(name, pw, salt, iterations, dklen)

    monkeypatch.setattr(auth.hashlib, "pbkdf2_hmac", spy)
    auth.verify_password("pw", "garbage")
    auth.verify_password("pw", genuine)
    assert calls == [DEFAULT_ITERATIONS, DEFAULT_ITERATIONS]


def test_verify_is_not_prefix_sensitive() -> None:
    h = hash_password("abcdef", iterations=1_000)
    t0 = time.perf_counter()
    assert verify_password("abcdef", h)
    t_ok = time.perf_counter() - t0
    t0 = time.perf_counter()
    assert not verify_password("zzzzzz", h)
    t_bad = time.perf_counter() - t0
    # Both paths do the same work; this is a sanity bound, not a precise timing assertion.
    assert abs(t_ok - t_bad) < max(t_ok, t_bad) * 5 + 0.05


# --------------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/review", "/api/review", "/policies", "/api/policies", "/digest", "/api/digest"]
)
async def test_operator_routes_401_without_credentials(client: AsyncClient, path: str) -> None:
    r = await client.get(path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


async def test_wrong_password_is_401(client: AsyncClient) -> None:
    r = await client.get("/api/review", auth=BasicAuth(OPERATOR, "nope"))
    assert r.status_code == 401


async def test_wrong_username_is_401(client: AsyncClient) -> None:
    r = await client.get("/api/review", auth=BasicAuth("someone", PASSWORD))
    assert r.status_code == 401


async def test_correct_credentials_200(auth_client: AsyncClient) -> None:
    r = await auth_client.get("/api/review")
    assert r.status_code == 200
    assert r.json()["count"] == 0


async def test_post_routes_require_auth(client: AsyncClient) -> None:
    r = await client.post("/policies/investor_initial_outreach", data={"level": "L1"})
    assert r.status_code == 401
    r = await client.post(f"/review/{'0' * 32}/approve")
    assert r.status_code == 401


async def test_health_ready_and_webhook_stay_open(client: AsyncClient) -> None:
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/ready")).status_code == 200
    # Webhook handshake needs no auth either.
    r = await client.post("/webhooks/graph?validationToken=abc")
    assert r.status_code == 200 and r.text == "abc"


async def test_static_assets_are_served_without_auth(client: AsyncClient) -> None:
    r = await client.get("/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    r = await client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert r.text.startswith("var htmx=")


async def test_unconfigured_hash_fails_closed() -> None:
    application: FastAPI = create_app(operator_settings(operator_password_hash=None))
    async with (
        application.router.lifespan_context(application),
        AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as c,
    ):
        r = await c.get("/api/review", auth=BasicAuth(OPERATOR, PASSWORD))
        assert r.status_code == 503
        assert "OPERATOR_PASSWORD_HASH" in r.json()["detail"]


def test_settings_defaults_for_operator_surface() -> None:
    s = operator_settings()
    assert s.operator_username == OPERATOR
    assert s.operator_password_hash is not None
    assert s.operator_password_hash.get_secret_value() == PASSWORD_HASH
    assert s.digest_hour_local == 7
    assert s.operator_timezone == "America/Chicago"
    assert "pbkdf2" not in repr(s)  # SecretStr never prints the hash
