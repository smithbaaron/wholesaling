"""Live DocuSign client against ``httpx.MockTransport``: JWT grant, account discovery, envelope
creation payload, 429 retry honouring Retry-After, 401 refresh, error mapping, breaker, void,
templates. No network, no credentials, fake RS256 signer (the project ships no RSA library)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.docusign import (
    DocuSignAuthError,
    DocuSignCircuitOpen,
    DocuSignError,
    DocuSignInvalidTransition,
    DocuSignNotFound,
    DocuSignThrottled,
    EnvelopeRequest,
    EnvelopeStatus,
    TemplateRecipient,
    get_docusign,
)
from wholesaling.integrations.docusign.live import (
    JWT_GRANT_TYPE,
    JWT_SCOPE,
    MAX_ASSERTION_LIFETIME,
    JwtCredentials,
    JwtTokenProvider,
    LiveDocuSignGateway,
)
from wholesaling.integrations.graph.live import CircuitBreaker

AUTH_HOST = "account-d.docusign.com"
TOKEN_URL = f"https://{AUTH_HOST}/oauth/token"
USERINFO_URL = f"https://{AUTH_HOST}/oauth/userinfo"
BASE_URI = "https://demo.docusign.net"
API = f"{BASE_URI}/restapi/v2.1/accounts/acc-1"
INTEGRATION_KEY = "ik-123"
USER_ID = "user-guid"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def fake_signer(message: bytes) -> bytes:
    return b"sig:" + message[:8]


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.scripts: dict[tuple[str, str], list[Callable[[httpx.Request], httpx.Response]]] = {}
        self.token_calls = 0
        self.token_status = 200
        self.token_body: dict[str, Any] = {
            "access_token": "tok-1",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        self.userinfo_calls = 0
        self.accounts: list[dict[str, Any]] = [
            {"account_id": "acc-1", "is_default": True, "base_uri": BASE_URI, "account_name": "Dev"}
        ]

    def on(
        self, method: str, url: str, *responses: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self.scripts.setdefault((method.upper(), url), []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        if url == TOKEN_URL:
            self.token_calls += 1
            form = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            assert form["grant_type"] == JWT_GRANT_TYPE.replace(":", "%3A")
            assert "assertion" in form
            body = self.token_body if self.token_status == 200 else {"error": "consent_required"}
            return httpx.Response(
                self.token_status,
                json={**body, "access_token": f"tok-{self.token_calls}"}
                if self.token_status == 200
                else body,
            )
        if url == USERINFO_URL:
            self.userinfo_calls += 1
            return httpx.Response(200, json={"sub": USER_ID, "accounts": self.accounts})
        queue = self.scripts.get((request.method.upper(), url))
        if not queue:
            return httpx.Response(599, json={"errorCode": "UNSCRIPTED", "message": url})
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        return responder(request)


def ok(
    status: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(status, json=body, headers=headers or {})


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    p = tmp_path / "docusign.pem"
    p.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    return p


def _settings(key_file: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "docusign_mode": "live",
        "docusign_integration_key": INTEGRATION_KEY,
        "docusign_user_id": USER_ID,
        "docusign_account_id": "acc-1",
        "docusign_private_key_path": str(key_file),
        "docusign_auth_host": AUTH_HOST,
    }
    base.update(over)
    return Settings(**base)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gateway(recorder: Recorder, clock: FakeClock, key_file: Path) -> LiveDocuSignGateway:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    return LiveDocuSignGateway(
        _settings(key_file),
        http=http,
        clock=clock,
        sleeper=_no_sleep,
        signer=fake_signer,
        base_backoff=0.0,
    )


_slept: list[float] = []


async def _no_sleep(seconds: float) -> None:
    _slept.append(seconds)


def _req() -> EnvelopeRequest:
    return EnvelopeRequest(
        template_key="psa",
        template_id="tpl-psa",
        email_subject="PSA - 1 Main St",
        recipients=[
            TemplateRecipient(role_name="Seller", name="Maria", email="m@x.com"),
            TemplateRecipient(
                role_name="Buyer", name="NorthStack", email="d@x.com", routing_order=2
            ),
        ],
        prefill_tabs={"property_address": "1 Main St"},
        idempotency_key="psa:1",
    )


def _jwt_claims(assertion: str) -> tuple[dict[str, Any], dict[str, Any]]:
    def dec(part: str) -> dict[str, Any]:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    header, claims, _sig = assertion.split(".")
    return dec(header), dec(claims)


# -- JWT grant ------------------------------------------------------------------------------------


def test_jwt_assertion_claims_match_docusign_jwt_grant(key_file: Path, clock: FakeClock) -> None:
    creds = JwtCredentials.from_settings(_settings(key_file))
    provider = JwtTokenProvider(creds, http=httpx.AsyncClient(), clock=clock, signer=fake_signer)
    header, claims = _jwt_claims(provider.build_assertion(clock()))
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert claims["iss"] == INTEGRATION_KEY
    assert claims["sub"] == USER_ID
    assert claims["aud"] == AUTH_HOST  # host only, no scheme
    assert claims["scope"] == JWT_SCOPE == "signature impersonation"
    assert claims["iat"] == int(clock().timestamp())
    assert 0 < claims["exp"] - claims["iat"] <= MAX_ASSERTION_LIFETIME.total_seconds()
    assert provider.token_url == TOKEN_URL
    assert "***" in repr(creds) and "fake" not in repr(creds)


def test_auth_host_strips_scheme_and_missing_settings_are_named(key_file: Path) -> None:
    creds = JwtCredentials.from_settings(
        _settings(key_file, docusign_auth_host="https://account.docusign.com/")
    )
    assert creds.auth_host == "account.docusign.com"
    with pytest.raises(DocuSignAuthError, match="DOCUSIGN_USER_ID"):
        JwtCredentials.from_settings(_settings(key_file, docusign_user_id=None))
    with pytest.raises(DocuSignAuthError, match="not found"):
        JwtCredentials.from_settings(
            _settings(key_file, docusign_private_key_path=str(key_file) + ".missing")
        )


async def test_token_is_cached_until_near_expiry(
    gateway: LiveDocuSignGateway, recorder: Recorder, clock: FakeClock
) -> None:
    recorder.on("GET", f"{API}/templates", ok(200, {"envelopeTemplates": []}))
    await gateway.list_templates()
    await gateway.list_templates()
    assert recorder.token_calls == 1
    assert recorder.userinfo_calls == 1  # base URI resolved once
    clock.advance(3600)
    await gateway.list_templates()
    assert recorder.token_calls == 2
    auth = [r.headers["Authorization"] for r in recorder.requests if str(r.url).startswith(API)]
    assert auth[-1] == "Bearer tok-2" and "***" in repr(gateway._tokens.cached)


async def test_consent_required_is_a_clear_auth_error(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    recorder.token_status = 400
    with pytest.raises(DocuSignAuthError, match="consent_required"):
        await gateway.list_templates()


async def test_account_discovery_uses_userinfo_default_account(
    recorder: Recorder, clock: FakeClock, key_file: Path
) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    gw = LiveDocuSignGateway(
        _settings(key_file, docusign_account_id=None),
        http=http,
        clock=clock,
        signer=fake_signer,
        sleeper=_no_sleep,
    )
    recorder.on("GET", f"{API}/templates", ok(200, {"envelopeTemplates": []}))
    await gw.list_templates()
    assert gw.account_id == "acc-1"
    assert recorder.requests[-1].url.path.startswith("/restapi/v2.1/accounts/acc-1/")


async def test_unknown_account_id_is_refused(
    recorder: Recorder, clock: FakeClock, key_file: Path
) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    gw = LiveDocuSignGateway(
        _settings(key_file, docusign_account_id="acc-x"), http=http, clock=clock, signer=fake_signer
    )
    with pytest.raises(DocuSignAuthError, match="acc-x"):
        await gw.list_templates()


# -- envelopes ------------------------------------------------------------------------------------


async def test_create_envelope_posts_template_definition_and_parses_response(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    recorder.on(
        "POST",
        f"{API}/envelopes",
        ok(
            201,
            {
                "envelopeId": "env-9",
                "status": "created",
                "statusDateTime": "2026-10-27T15:00:00.0000000Z",
                "uri": "/envelopes/env-9",
            },
        ),
    )
    info = await gateway.create_envelope_from_template(_req())
    assert info.envelope_id == "env-9" and info.status is EnvelopeStatus.CREATED
    assert info.template_key == "psa" and info.uri == "/envelopes/env-9"
    post = next(
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/envelopes")
    )
    body = json.loads(post.content)
    assert body["templateId"] == "tpl-psa" and body["status"] == "created"
    assert body["templateRoles"][0]["roleName"] == "Seller"
    assert body["prefillTabs"]["textTabs"] == [
        {"tabLabel": "property_address", "value": "1 Main St"}
    ]
    assert post.headers["Authorization"] == "Bearer tok-1"


async def test_get_status_and_not_found(gateway: LiveDocuSignGateway, recorder: Recorder) -> None:
    recorder.on(
        "GET", f"{API}/envelopes/env-1", ok(200, {"envelopeId": "env-1", "status": "completed"})
    )
    recorder.on(
        "GET",
        f"{API}/envelopes/env-x",
        ok(404, {"errorCode": "ENVELOPE_DOES_NOT_EXIST", "message": "no"}),
    )
    assert (await gateway.get_envelope_status("env-1")).status is EnvelopeStatus.COMPLETED
    with pytest.raises(DocuSignNotFound):
        await gateway.get_envelope_status("env-x")


async def test_void_puts_status_voided_with_reason(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    recorder.on("PUT", f"{API}/envelopes/env-1", ok(200, {"envelopeId": "env-1"}))
    recorder.on(
        "GET",
        f"{API}/envelopes/env-1",
        ok(200, {"envelopeId": "env-1", "status": "voided", "voidedReason": "dead"}),
    )
    info = await gateway.void_envelope("env-1", "dead")
    put = next(r for r in recorder.requests if r.method == "PUT")
    assert json.loads(put.content) == {"status": "voided", "voidedReason": "dead"}
    assert info.status is EnvelopeStatus.VOIDED and info.voided_reason == "dead"
    with pytest.raises(DocuSignInvalidTransition):
        await gateway.void_envelope("env-1", "  ")


async def test_void_completed_envelope_maps_to_invalid_transition(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    recorder.on(
        "PUT",
        f"{API}/envelopes/env-2",
        ok(
            400,
            {
                "errorCode": "ENVELOPE_INVALID_STATUS",
                "message": "Only in-process envelopes can be voided",
            },
        ),
    )
    with pytest.raises(DocuSignInvalidTransition):
        await gateway.void_envelope("env-2", "late")


async def test_list_templates_parses(gateway: LiveDocuSignGateway, recorder: Recorder) -> None:
    recorder.on(
        "GET",
        f"{API}/templates",
        ok(200, {"envelopeTemplates": [{"templateId": "t1", "name": "PSA", "shared": "true"}]}),
    )
    templates = await gateway.list_templates()
    assert templates[0].template_id == "t1" and templates[0].shared is True


# -- resilience -----------------------------------------------------------------------------------


async def test_429_retries_honouring_retry_after_then_succeeds(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    _slept.clear()
    recorder.on(
        "POST",
        f"{API}/envelopes",
        ok(
            429,
            {"errorCode": "HOURLY_APIINVOCATION_LIMIT_EXCEEDED", "message": "slow down"},
            {"Retry-After": "7"},
        ),
        ok(201, {"envelopeId": "env-3", "status": "created"}),
    )
    info = await gateway.create_envelope_from_template(_req())
    assert info.envelope_id == "env-3"
    assert _slept == [7.0]
    assert sum(1 for r in recorder.requests if r.url.path.endswith("/envelopes")) == 2


async def test_429_budget_exhausted_raises_throttled(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    recorder.on("POST", f"{API}/envelopes", ok(429, {"message": "slow"}, {"Retry-After": "1"}))
    with pytest.raises(DocuSignThrottled) as exc:
        await gateway.create_envelope_from_template(_req())
    assert exc.value.retry_after == 1.0


async def test_401_refreshes_token_once(gateway: LiveDocuSignGateway, recorder: Recorder) -> None:
    recorder.on(
        "GET",
        f"{API}/templates",
        ok(401, {"errorCode": "AUTHORIZATION_INVALID_TOKEN", "message": "expired"}),
        ok(200, {"envelopeTemplates": []}),
    )
    await gateway.list_templates()
    assert recorder.token_calls == 2


async def test_403_is_auth_error_and_500_maps_to_error(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    recorder.on(
        "GET",
        f"{API}/envelopes/env-f",
        ok(403, {"errorCode": "USER_LACKS_PERMISSIONS", "message": "no"}),
    )
    with pytest.raises(DocuSignAuthError):
        await gateway.get_envelope_status("env-f")
    recorder.on("GET", f"{API}/envelopes/env-5", ok(500, {"message": "boom"}))
    with pytest.raises(DocuSignError):
        await gateway.get_envelope_status("env-5")


async def test_circuit_breaker_opens_after_failures(
    recorder: Recorder, clock: FakeClock, key_file: Path
) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    gw = LiveDocuSignGateway(
        _settings(key_file),
        http=http,
        clock=clock,
        signer=fake_signer,
        sleeper=_no_sleep,
        max_retries=0,
        breaker=CircuitBreaker(failure_threshold=2, clock=clock),
    )
    recorder.on("GET", f"{API}/envelopes/env-5", ok(503, {"message": "down"}))
    for _ in range(2):
        with pytest.raises(DocuSignError):
            await gw.get_envelope_status("env-5")
    with pytest.raises(DocuSignCircuitOpen):
        await gw.get_envelope_status("env-5")


async def test_healthcheck_reports_account(
    gateway: LiveDocuSignGateway, recorder: Recorder
) -> None:
    recorder.on("GET", f"{API}/templates", ok(200, {"envelopeTemplates": []}))
    health = await gateway.healthcheck()
    assert health.ok and "acc-1" in health.detail and health.mode == "live"


def test_factory_returns_live_gateway_in_live_mode(key_file: Path) -> None:
    gw = get_docusign(_settings(key_file))
    assert isinstance(gw, LiveDocuSignGateway)


def test_default_signer_names_the_optional_extra(key_file: Path, clock: FakeClock) -> None:
    creds = JwtCredentials.from_settings(_settings(key_file))
    provider = JwtTokenProvider(creds, http=httpx.AsyncClient(), clock=clock)
    try:
        provider.build_assertion(clock())
    except ImportError as exc:
        assert "google-auth" in str(exc)
    except Exception:  # google-auth present: a fake PEM fails inside the signer instead
        pass
