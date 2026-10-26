"""Live DocuSign eSignature REST v2.1 client over ``httpx`` (``DOCUSIGN_MODE=live``).

**Never used in this build's automated paths** (RUL-06, XC-05): the only caller is the
operator's ``/deals`` page, and even there the default envelope status is ``created`` (a draft
in the DocuSign account that the human sends). See docs/integrations/docusign.md for the
reference URLs each fact below was checked against.

Auth: JWT Grant (``docs/integrations/docusign.md`` "JWT grant").
    header  ``{"alg":"RS256","typ":"JWT"}``
    claims  ``iss`` = integration key, ``sub`` = impersonated user GUID, ``aud`` = the auth host
            *without scheme* (``account-d.docusign.com`` sandbox / ``account.docusign.com``
            production), ``iat``, ``exp`` (at most one hour after ``iat``),
            ``scope`` = ``"signature impersonation"``
    POST    ``https://{auth_host}/oauth/token`` form-encoded
            ``grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=<jwt>``
    then    ``GET https://{auth_host}/oauth/userinfo`` -> ``accounts[]`` with ``account_id``,
            ``base_uri``, ``is_default``; the API base is ``{base_uri}/restapi/v2.1``.
    The user must have granted consent to the integration key once (``consent_required`` error
    otherwise) - a human step recorded under PRE-13.

Endpoints:
    POST ``/accounts/{accountId}/envelopes``                 create from template
    GET  ``/accounts/{accountId}/envelopes/{envelopeId}``    status
    PUT  ``/accounts/{accountId}/envelopes/{envelopeId}``    ``{"status":"voided","voidedReason"}``
    GET  ``/accounts/{accountId}/templates``                 list templates

Resilience mirrors the Graph and Sheets clients: retry with backoff on 429/5xx honouring
``Retry-After``, one token refresh on 401, circuit breaker (XC-08). The RSA private key and the
tokens never reach logs. The project ships no RSA library; signing goes through an injectable
``Signer`` (tests inject a fake), and the default lazily reuses the Sheets client's optional
``google-auth`` signer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from wholesaling.config.settings import Settings
from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.docusign.interface import (
    DocuSignAuthError,
    DocuSignCircuitOpen,
    DocuSignError,
    DocuSignInvalidTransition,
    DocuSignNotFound,
    DocuSignThrottled,
)
from wholesaling.integrations.docusign.models import (
    EnvelopeInfo,
    EnvelopeRequest,
    EnvelopeStatus,
    TemplateInfo,
)
from wholesaling.integrations.graph.live import CircuitBreaker, parse_retry_after
from wholesaling.logging import get_logger

log = get_logger(__name__)

JWT_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
JWT_SCOPE = "signature impersonation"
API_SUFFIX = "/restapi/v2.1"
# DocuSign rejects assertions whose exp is more than one hour after iat.
MAX_ASSERTION_LIFETIME = timedelta(hours=1)
ASSERTION_LIFETIME = timedelta(minutes=55)
REFRESH_SKEW = timedelta(seconds=300)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
INSTALL_HINT = (
    "RS256 signing needs the optional 'google-auth' extra (shared with the Sheets client): run "
    "`uv add google-auth`, or keep DOCUSIGN_MODE=mock."
)

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
Signer = Callable[[bytes], bytes]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def default_signer(private_key_pem: str) -> Signer:
    """RS256 signer; reuses the Sheets client's lazily imported ``google-auth`` signer."""
    from wholesaling.integrations.sheets.live import google_auth_signer

    try:
        return google_auth_signer(private_key_pem)
    except ImportError as exc:
        raise ImportError(INSTALL_HINT) from exc


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_at: datetime

    def is_valid(self, now: datetime, skew: timedelta = REFRESH_SKEW) -> bool:
        return now + skew < self.expires_at

    def __repr__(self) -> str:
        return f"AccessToken(expires_at={self.expires_at.isoformat()}, value='***')"


@dataclass(frozen=True, slots=True)
class JwtCredentials:
    integration_key: str
    user_id: str
    auth_host: str
    private_key_pem: str

    @classmethod
    def from_settings(cls, settings: Settings) -> JwtCredentials:
        missing = [
            name
            for name, value in (
                ("DOCUSIGN_INTEGRATION_KEY", settings.docusign_integration_key),
                ("DOCUSIGN_USER_ID", settings.docusign_user_id),
                ("DOCUSIGN_PRIVATE_KEY_PATH", settings.docusign_private_key_path),
            )
            if not value
        ]
        if missing:
            raise DocuSignAuthError(f"{', '.join(missing)} required in live mode")
        assert settings.docusign_private_key_path is not None
        path = Path(settings.docusign_private_key_path)
        if not path.is_file():
            raise DocuSignAuthError(f"DOCUSIGN_PRIVATE_KEY_PATH not found: {path}")
        host = settings.docusign_auth_host.strip().removeprefix("https://").rstrip("/")
        return cls(
            integration_key=str(settings.docusign_integration_key),
            user_id=str(settings.docusign_user_id),
            auth_host=host,
            private_key_pem=path.read_text(encoding="utf-8"),
        )

    def __repr__(self) -> str:
        return (
            f"JwtCredentials(integration_key={self.integration_key!r}, user_id={self.user_id!r},"
            f" auth_host={self.auth_host!r}, private_key_pem='***')"
        )


class JwtTokenProvider:
    """JWT Grant -> access token, cached until shortly before expiry."""

    def __init__(
        self,
        creds: JwtCredentials,
        *,
        http: httpx.AsyncClient,
        clock: Clock | None = None,
        signer: Signer | None = None,
        assertion_lifetime: timedelta = ASSERTION_LIFETIME,
    ) -> None:
        self._creds = creds
        self._http = http
        self._clock: Clock = clock or _utcnow
        self._signer = signer
        self._lifetime = min(assertion_lifetime, MAX_ASSERTION_LIFETIME)
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()
        self.fetch_count = 0

    @property
    def token_url(self) -> str:
        return f"https://{self._creds.auth_host}/oauth/token"

    @property
    def userinfo_url(self) -> str:
        return f"https://{self._creds.auth_host}/oauth/userinfo"

    @property
    def cached(self) -> AccessToken | None:
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def build_assertion(self, now: datetime) -> str:
        header = {"alg": "RS256", "typ": "JWT"}
        iat = int(now.timestamp())
        claims = {
            "iss": self._creds.integration_key,
            "sub": self._creds.user_id,
            "aud": self._creds.auth_host,
            "iat": iat,
            "exp": iat + int(self._lifetime.total_seconds()),
            "scope": JWT_SCOPE,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        if self._signer is None:
            self._signer = default_signer(self._creds.private_key_pem)
        return f"{signing_input}.{_b64url(self._signer(signing_input.encode('ascii')))}"

    async def get_token(self) -> str:
        now = self._clock()
        tok = self._token
        if tok is not None and tok.is_valid(now):
            return tok.value
        async with self._lock:
            now = self._clock()
            tok = self._token
            if tok is not None and tok.is_valid(now):
                return tok.value
            self._token = await self._fetch()
            return self._token.value

    async def _fetch(self) -> AccessToken:
        data = {"grant_type": JWT_GRANT_TYPE, "assertion": self.build_assertion(self._clock())}
        try:
            response = await self._http.post(self.token_url, data=data)
        except httpx.HTTPError as exc:
            raise DocuSignAuthError(f"token endpoint unreachable: {exc!r}") from exc
        self.fetch_count += 1
        if response.status_code != 200:
            code, desc = _oauth_error(response)
            if code == "consent_required":
                raise DocuSignAuthError(
                    "consent_required: the API user has not granted consent to this integration "
                    "key (PRE-13 human step)",
                    status_code=response.status_code,
                    code=code,
                )
            raise DocuSignAuthError(
                f"JWT grant failed: HTTP {response.status_code} {code}: {desc}".strip(),
                status_code=response.status_code,
                code=code,
            )
        payload: dict[str, Any] = response.json()
        value = payload.get("access_token")
        if not isinstance(value, str) or not value:
            raise DocuSignAuthError("token response had no access_token")
        expires_in = int(payload.get("expires_in", 3600))
        token = AccessToken(value=value, expires_at=self._clock() + timedelta(seconds=expires_in))
        log.info("docusign.token_acquired", expires_at=token.expires_at.isoformat())
        return token


def _oauth_error(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        return "unknown_error", ""
    if not isinstance(body, dict):
        return "unknown_error", ""
    return str(body.get("error", "unknown_error")), str(body.get("error_description", ""))


def _error_body(response: httpx.Response) -> tuple[str | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:200]
    if not isinstance(body, dict):
        return None, str(body)[:200]
    code = body.get("errorCode")
    return (str(code) if code else None), str(body.get("message", ""))[:300]


def _parse_envelope(payload: dict[str, Any], *, template_key: str | None = None) -> EnvelopeInfo:
    status_text = str(payload.get("status", "")).lower()
    try:
        status = EnvelopeStatus(status_text)
    except ValueError as exc:
        raise DocuSignError(f"unknown envelope status {status_text!r}") from exc
    when = payload.get("statusDateTime") or payload.get("statusChangedDateTime")
    status_at: datetime | None = None
    if isinstance(when, str) and when:
        try:
            status_at = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            status_at = None
    return EnvelopeInfo(
        envelope_id=str(payload["envelopeId"]),
        status=status,
        status_at=status_at,
        template_key=template_key,
        uri=payload.get("uri"),
        voided_reason=payload.get("voidedReason"),
        raw=payload,
    )


class LiveDocuSignGateway:
    name = "docusign"
    mode: IntegrationMode = "live"

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        token_provider: JwtTokenProvider | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        max_retries: int = 3,
        base_backoff: float = 0.5,
        max_backoff: float = 30.0,
        breaker: CircuitBreaker | None = None,
        signer: Signer | None = None,
        base_url: str | None = None,
    ) -> None:
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_http = http is None
        self._clock: Clock = clock or _utcnow
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self.breaker = breaker or CircuitBreaker(clock=self._clock)
        self.account_id: str | None = settings.docusign_account_id or None
        self._base_url: str | None = base_url.rstrip("/") if base_url else None
        if token_provider is None:
            creds = JwtCredentials.from_settings(settings)
            token_provider = JwtTokenProvider(
                creds, http=self._http, clock=self._clock, signer=signer
            )
        self._tokens = token_provider
        self.request_count = 0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- account discovery -------------------------------------------------------------------

    async def _ensure_base(self) -> str:
        """Resolve ``{base_uri}/restapi/v2.1`` and the account id from ``/oauth/userinfo``."""
        if self._base_url is not None and self.account_id:
            return self._base_url
        token = await self._tokens.get_token()
        try:
            response = await self._http.get(
                self._tokens.userinfo_url, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise DocuSignAuthError(f"userinfo unreachable: {exc!r}") from exc
        if response.status_code != 200:
            raise DocuSignAuthError(
                f"userinfo failed: HTTP {response.status_code}", status_code=response.status_code
            )
        accounts = response.json().get("accounts") or []
        chosen: dict[str, Any] | None = None
        for acct in accounts:
            if self.account_id and str(acct.get("account_id")) == self.account_id:
                chosen = acct
                break
            if not self.account_id and acct.get("is_default"):
                chosen = acct
        if chosen is None and accounts and not self.account_id:
            chosen = accounts[0]
        if chosen is None:
            raise DocuSignAuthError(
                f"account {self.account_id or '(default)'} not found in userinfo accounts"
            )
        self.account_id = str(chosen["account_id"])
        self._base_url = str(chosen["base_uri"]).rstrip("/") + API_SUFFIX
        log.info("docusign.account_resolved", account_id=self.account_id, base=self._base_url)
        return self._base_url

    def _account_path(self, suffix: str) -> str:
        return f"/accounts/{self.account_id}{suffix}"

    # -- HTTP core ---------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        if not self.breaker.allow():
            raise DocuSignCircuitOpen(
                f"docusign circuit open since {self.breaker.opened_at}; last error: "
                f"{self.breaker.last_error}"
            )
        base = await self._ensure_base()
        url = f"{base}{path}"
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            token = await self._tokens.get_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                self.request_count += 1
                response = await self._http.request(
                    method, url, json=json_body, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt <= self._max_retries:
                    await self._backoff(attempt, None, reason=error)
                    continue
                self.breaker.record_failure(error)
                raise DocuSignError(
                    f"docusign request failed after {attempt} attempts: {error}"
                ) from exc

            status = response.status_code
            log.debug("docusign.request", method=method, path=path, status=status, attempt=attempt)
            if status in expected:
                self.breaker.record_success()
                return response
            if status == 401 and not refreshed:
                self._tokens.invalidate()
                refreshed = True
                continue
            if status in RETRYABLE_STATUS:
                retry_after = parse_retry_after(response.headers.get("Retry-After"), self._clock())
                if attempt <= self._max_retries:
                    await self._backoff(attempt, retry_after, reason=f"HTTP {status}")
                    continue
                self.breaker.record_failure(f"HTTP {status}")
                if status == 429:
                    raise DocuSignThrottled(
                        "docusign rate limit; retry budget exhausted",
                        status_code=status,
                        retry_after=retry_after,
                    )
                raise DocuSignError(
                    f"docusign server error after {attempt} attempts", status_code=status
                )
            code, message = _error_body(response)
            if status in (401, 403):
                self.breaker.record_failure(f"HTTP {status} {code}")
                raise DocuSignAuthError(
                    f"docusign rejected the request: {code or status} {message}",
                    status_code=status,
                    code=code,
                )
            if status == 404:
                raise DocuSignNotFound(message or "not found", status_code=status, code=code)
            if status == 400 and code and "ENVELOPE" in code and "STATUS" in code:
                raise DocuSignInvalidTransition(message or code, status_code=status, code=code)
            self.breaker.record_failure(f"HTTP {status} {code}")
            raise DocuSignError(
                f"docusign error HTTP {status} {code or ''}: {message}".strip(),
                status_code=status,
                code=code,
            )

    async def _backoff(self, attempt: int, retry_after: float | None, *, reason: str) -> None:
        delay = (
            retry_after
            if retry_after is not None
            else min(self._max_backoff, self._base_backoff * (2 ** (attempt - 1)))
            + random.uniform(0, self._base_backoff)
        )
        log.warning("docusign.retry", attempt=attempt, delay=round(delay, 2), reason=reason)
        await self._sleep(delay)

    # -- gateway surface ---------------------------------------------------------------------

    async def healthcheck(self) -> HealthStatus:
        try:
            await self._ensure_base()
            await self._request("GET", self._account_path("/templates"), params={"count": "1"})
        except DocuSignError as exc:
            return HealthStatus(name=self.name, mode=self.mode, ok=False, detail=str(exc)[:200])
        return HealthStatus(
            name=self.name, mode=self.mode, ok=True, detail=f"account {self.account_id}"
        )

    async def create_envelope_from_template(self, request: EnvelopeRequest) -> EnvelopeInfo:
        """Human-triggered only (POL-03i). Returns the new envelope's id and status."""
        await self._ensure_base()
        response = await self._request(
            "POST",
            self._account_path("/envelopes"),
            json_body=request.to_api_payload(),
            expected=(201, 200),
        )
        info = _parse_envelope(response.json(), template_key=request.template_key)
        log.info(
            "docusign.envelope_created",
            envelope_id=info.envelope_id,
            status=info.status.value,
            template_key=request.template_key,
        )
        return info

    async def get_envelope_status(self, envelope_id: str) -> EnvelopeInfo:
        await self._ensure_base()
        response = await self._request("GET", self._account_path(f"/envelopes/{envelope_id}"))
        return _parse_envelope(response.json())

    async def void_envelope(self, envelope_id: str, reason: str) -> EnvelopeInfo:
        if not reason.strip():
            raise DocuSignInvalidTransition("voidedReason is required")
        await self._ensure_base()
        await self._request(
            "PUT",
            self._account_path(f"/envelopes/{envelope_id}"),
            json_body={"status": "voided", "voidedReason": reason},
        )
        log.info("docusign.envelope_voided", envelope_id=envelope_id)
        return await self.get_envelope_status(envelope_id)

    async def list_templates(self) -> list[TemplateInfo]:
        await self._ensure_base()
        response = await self._request("GET", self._account_path("/templates"))
        out: list[TemplateInfo] = []
        for item in response.json().get("envelopeTemplates") or []:
            out.append(
                TemplateInfo(
                    template_id=str(item.get("templateId", "")),
                    name=str(item.get("name", "")),
                    shared=str(item.get("shared", "false")).lower() == "true",
                    raw=item,
                )
            )
        return out


__all__ = [
    "API_SUFFIX",
    "ASSERTION_LIFETIME",
    "JWT_GRANT_TYPE",
    "JWT_SCOPE",
    "MAX_ASSERTION_LIFETIME",
    "AccessToken",
    "JwtCredentials",
    "JwtTokenProvider",
    "LiveDocuSignGateway",
]
