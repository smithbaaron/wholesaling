"""Client-credentials OAuth for Microsoft Graph, without MSAL (REQUIREMENTS.md INT-11).

Token endpoint: ``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`` with
``grant_type=client_credentials`` and ``scope=https://graph.microsoft.com/.default``. The
``.default`` scope resolves to every *application* permission granted to the app registration
(``Mail.Read``, ``Mail.ReadWrite``, ``Mail.Send``; see docs/integrations/graph.md).

The token is cached until shortly before ``expires_in`` elapses and refreshed under a lock so
concurrent activities never stampede the token endpoint. Token values are never logged: the
only thing logged is that a token was acquired and when it expires.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from wholesaling.integrations.graph.interface import GraphAuthError
from wholesaling.logging import get_logger

log = get_logger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# Refresh this long before the token actually expires (clock skew + in-flight request time).
REFRESH_SKEW = timedelta(seconds=300)

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_at: datetime
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type, not a secret

    def is_valid(self, now: datetime, skew: timedelta = REFRESH_SKEW) -> bool:
        return now + skew < self.expires_at

    def __repr__(self) -> str:  # never leak the value through repr / logging
        return f"AccessToken(expires_at={self.expires_at.isoformat()}, value='***')"


class ClientCredentialsTokenProvider:
    """Acquire and cache an application token for one tenant / app registration."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient,
        scope: str = GRAPH_SCOPE,
        authority: str = AUTHORITY,
        clock: Clock | None = None,
    ) -> None:
        if not tenant_id or not client_id or not client_secret:
            msg = "GRAPH_TENANT_ID, GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET are all required"
            raise GraphAuthError(msg)
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._scope = scope
        self._token_url = f"{authority.rstrip('/')}/{tenant_id}/oauth2/v2.0/token"
        self._clock: Clock = clock or _utcnow
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()
        self.fetch_count = 0

    @property
    def cached(self) -> AccessToken | None:
        return self._token

    def invalidate(self) -> None:
        """Drop the cached token (after a 401 from Graph)."""
        self._token = None

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
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }
        try:
            response = await self._http.post(self._token_url, data=data)
        except httpx.HTTPError as exc:
            raise GraphAuthError(f"token endpoint unreachable: {exc!r}") from exc
        self.fetch_count += 1
        if response.status_code != 200:
            detail = _safe_error(response)
            raise GraphAuthError(
                f"token request failed: {detail}", status_code=response.status_code
            )
        payload: dict[str, Any] = response.json()
        value = payload.get("access_token")
        if not isinstance(value, str) or not value:
            raise GraphAuthError("token response had no access_token")
        expires_in = int(payload.get("expires_in", 3600))
        token = AccessToken(
            value=value,
            expires_at=self._clock() + timedelta(seconds=expires_in),
            token_type=str(payload.get("token_type", "Bearer")),
        )
        log.info("graph.token_acquired", expires_at=token.expires_at.isoformat())
        return token


def _safe_error(response: httpx.Response) -> str:
    """Error description from an AAD error body, without echoing anything secret."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    code = body.get("error", "unknown_error") if isinstance(body, dict) else "unknown_error"
    desc = body.get("error_description", "") if isinstance(body, dict) else ""
    first_line = str(desc).splitlines()[0] if desc else ""
    return f"HTTP {response.status_code} {code}: {first_line}".strip()
