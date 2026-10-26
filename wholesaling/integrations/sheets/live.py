"""Live Google Sheets API v4 gateway over ``httpx`` (REQUIREMENTS.md INT-14, INT-15, STK-06).

Endpoints (service endpoint ``https://sheets.googleapis.com``; see docs/integrations/sheets.md
for the reference URLs that were checked):

- ``GET  /v4/spreadsheets/{id}``                      metadata (health, tab list)
- ``POST /v4/spreadsheets/{id}:batchUpdate``          ``addSheet`` requests (create tabs)
- ``GET  /v4/spreadsheets/{id}/values/{range}``       read one tab
- ``POST /v4/spreadsheets/{id}/values:batchUpdate``   write many ranges in one call

Auth is a service account (INT-15): a JWT (``RS256``) with ``iss`` = the service-account
email, ``scope`` = ``https://www.googleapis.com/auth/spreadsheets``, ``aud`` = the token
endpoint, ``exp`` <= ``iat`` + 1 h, exchanged at ``https://oauth2.googleapis.com/token`` with
``grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer``. The project carries no RSA library
(``uv tree`` has neither ``cryptography`` nor ``PyJWT``), so signing goes through an injectable
``Signer``; the default signer imports ``google.auth`` lazily and raises a clear ``ImportError``
naming the one optional extra when it is absent. Tests inject a fake signer.

Resilience mirrors the Graph client: retries with backoff on 429/5xx honouring ``Retry-After``,
one token refresh on 401, and a circuit breaker (XC-08). Tokens and the private key never reach
logs. There is no delete call anywhere in this module.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from wholesaling.config.settings import Settings
from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.graph.live import CircuitBreaker, parse_retry_after
from wholesaling.integrations.sheets.interface import (
    FIRST_DATA_ROW_NUMBER,
    HEADER_ROW_NUMBER,
    Cell,
    SheetRow,
    SheetsAuthError,
    SheetsCircuitOpen,
    SheetsError,
    SheetsNotFound,
    SheetsThrottled,
    TabSpec,
    UpsertResult,
    canon_row,
)
from wholesaling.logging import get_logger

log = get_logger(__name__)

SHEETS_BASE_URL = "https://sheets.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
JWT_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# Google caps a service-account assertion at one hour after ``iat``.
MAX_ASSERTION_LIFETIME = timedelta(hours=1)
ASSERTION_LIFETIME = timedelta(minutes=55)
REFRESH_SKEW = timedelta(seconds=300)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Ranges per ``values:batchUpdate`` call. Google's documented limit is on payload size and
# per-minute quota, not range count; 200 keeps a single request comfortably small.
BATCH_RANGES = 200
# Widest tab the projection reads: ``A:ZZ`` is 702 columns, far past any header we ship.
READ_LAST_COLUMN = "ZZ"
INSTALL_HINT = (
    "RS256 signing needs the optional 'google-auth' extra: run `uv add google-auth` (or set "
    "SHEETS_MODE=mock). The project ships no RSA library of its own."
)

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
Signer = Callable[[bytes], bytes]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def column_letter(index: int) -> str:
    """0-based column index to A1 letters: 0 -> A, 25 -> Z, 26 -> AA."""
    if index < 0:
        raise ValueError("column index must be >= 0")
    letters = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def quote_tab(tab: str) -> str:
    """A1-notation sheet name: always single-quoted, inner quotes doubled."""
    return "'" + tab.replace("'", "''") + "'"


def a1_range(tab: str, first_row: int, last_row: int | None, width: int) -> str:
    """``'Buy Boxes'!A2:K2`` style range. Sheet names are always quoted (a quote inside the
    name is doubled, per A1 notation)."""
    name = quote_tab(tab)
    last_col = column_letter(width - 1)
    if last_row is None:
        return f"{name}!A{first_row}:{last_col}"
    return f"{name}!A{first_row}:{last_col}{last_row}"


# --------------------------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------------------------


class TokenProvider(Protocol):
    async def get_token(self) -> str: ...

    def invalidate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceAccountKey:
    client_email: str
    private_key: str
    private_key_id: str | None = None
    token_uri: str = TOKEN_URL

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ServiceAccountKey:
        if payload.get("type") != "service_account":
            raise SheetsAuthError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service_account key")
        try:
            return cls(
                client_email=str(payload["client_email"]),
                private_key=str(payload["private_key"]),
                private_key_id=payload.get("private_key_id"),
                token_uri=str(payload.get("token_uri") or TOKEN_URL),
            )
        except KeyError as exc:
            raise SheetsAuthError(f"service account key is missing {exc}") from exc

    @classmethod
    def load(cls, value: str) -> ServiceAccountKey:
        """``value`` is either the path to the downloaded key file (the documented form) or the
        JSON itself (handy for secret managers that inject file contents)."""
        text = value.strip()
        if not text.startswith("{"):
            path = Path(text)
            if not path.is_file():
                raise SheetsAuthError(f"GOOGLE_SERVICE_ACCOUNT_JSON file not found: {path}")
            text = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise SheetsAuthError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SheetsAuthError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object")
        return cls.from_json(payload)

    def __repr__(self) -> str:  # never leak the key through repr / logging
        return f"ServiceAccountKey(client_email={self.client_email!r}, private_key='***')"


def google_auth_signer(private_key_pem: str) -> Signer:
    """RS256 signer backed by ``google.auth.crypt`` (the only optional extra; imported lazily)."""
    try:
        from google.auth import crypt  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise ImportError(INSTALL_HINT) from exc
    signer = crypt.RSASigner.from_string(private_key_pem)

    def _sign(message: bytes) -> bytes:
        return bytes(signer.sign(message))

    return _sign


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_at: datetime

    def is_valid(self, now: datetime, skew: timedelta = REFRESH_SKEW) -> bool:
        return now + skew < self.expires_at

    def __repr__(self) -> str:
        return f"AccessToken(expires_at={self.expires_at.isoformat()}, value='***')"


class ServiceAccountTokenProvider:
    """Service-account JWT -> OAuth2 access token, cached until shortly before expiry."""

    def __init__(
        self,
        key: ServiceAccountKey,
        *,
        http: httpx.AsyncClient,
        scope: str = SHEETS_SCOPE,
        clock: Clock | None = None,
        signer: Signer | None = None,
        assertion_lifetime: timedelta = ASSERTION_LIFETIME,
    ) -> None:
        self._key = key
        self._http = http
        self._scope = scope
        self._clock: Clock = clock or _utcnow
        self._signer = signer
        self._lifetime = min(assertion_lifetime, MAX_ASSERTION_LIFETIME)
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()
        self.fetch_count = 0

    @property
    def cached(self) -> AccessToken | None:
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def build_assertion(self, now: datetime) -> str:
        """The signed JWT. Header ``{"alg":"RS256","typ":"JWT"[,"kid"]}``; claims
        ``iss``/``scope``/``aud``/``iat``/``exp``."""
        header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
        if self._key.private_key_id:
            header["kid"] = self._key.private_key_id
        iat = int(now.timestamp())
        claims = {
            "iss": self._key.client_email,
            "scope": self._scope,
            "aud": self._key.token_uri,
            "iat": iat,
            "exp": iat + int(self._lifetime.total_seconds()),
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        if self._signer is None:
            self._signer = google_auth_signer(self._key.private_key)
        signature = self._signer(signing_input.encode("ascii"))
        return f"{signing_input}.{_b64url(signature)}"

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
        now = self._clock()
        data = {"grant_type": JWT_GRANT_TYPE, "assertion": self.build_assertion(now)}
        try:
            response = await self._http.post(self._key.token_uri, data=data)
        except httpx.HTTPError as exc:
            raise SheetsAuthError(f"token endpoint unreachable: {exc!r}") from exc
        self.fetch_count += 1
        if response.status_code != 200:
            raise SheetsAuthError(
                f"token request failed: {_safe_token_error(response)}",
                status_code=response.status_code,
            )
        payload: dict[str, Any] = response.json()
        value = payload.get("access_token")
        if not isinstance(value, str) or not value:
            raise SheetsAuthError("token response had no access_token")
        expires_in = int(payload.get("expires_in", 3600))
        token = AccessToken(value=value, expires_at=self._clock() + timedelta(seconds=expires_in))
        log.info("sheets.token_acquired", expires_at=token.expires_at.isoformat())
        return token


def _safe_token_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if not isinstance(body, dict):
        return f"HTTP {response.status_code}"
    code = body.get("error", "unknown_error")
    desc = str(body.get("error_description", "")).splitlines()
    return f"HTTP {response.status_code} {code}: {desc[0] if desc else ''}".strip()


# --------------------------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------------------------


class LiveSheetsGateway:
    name = "sheets"
    mode: IntegrationMode = "live"

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        token_provider: TokenProvider | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        max_retries: int = 3,
        base_backoff: float = 0.5,
        max_backoff: float = 30.0,
        breaker: CircuitBreaker | None = None,
        base_url: str = SHEETS_BASE_URL,
        signer: Signer | None = None,
    ) -> None:
        if not settings.sheets_spreadsheet_id:
            raise SheetsAuthError("SHEETS_SPREADSHEET_ID is required in live mode")
        self.spreadsheet_id = settings.sheets_spreadsheet_id
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_http = http is None
        self._clock: Clock = clock or _utcnow
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._base_url = base_url.rstrip("/")
        self.breaker = breaker or CircuitBreaker(clock=self._clock)
        if token_provider is None:
            if not settings.google_service_account_json:
                raise SheetsAuthError("GOOGLE_SERVICE_ACCOUNT_JSON is required in live mode")
            key = ServiceAccountKey.load(settings.google_service_account_json)
            self.service_account_email: str | None = key.client_email
            token_provider = ServiceAccountTokenProvider(
                key, http=self._http, clock=self._clock, signer=signer
            )
        else:
            self.service_account_email = None
        self._tokens = token_provider
        self.request_count = 0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def spreadsheet_path(self) -> str:
        return f"/spreadsheets/{quote(self.spreadsheet_id, safe='')}"

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
            raise SheetsCircuitOpen(
                f"sheets circuit open since {self.breaker.opened_at}; last error: "
                f"{self.breaker.last_error}"
            )
        url = f"{self._base_url}{path}"
        refreshed_token = False
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
                raise SheetsError(
                    f"sheets request failed after {attempt} attempts: {error}"
                ) from exc

            status = response.status_code
            log.debug("sheets.request", method=method, path=path, status=status, attempt=attempt)
            if status in expected:
                self.breaker.record_success()
                return response
            if status == 401 and not refreshed_token:
                self._tokens.invalidate()
                refreshed_token = True
                continue
            if status in RETRYABLE_STATUS:
                retry_after = parse_retry_after(response.headers.get("Retry-After"), self._clock())
                if attempt <= self._max_retries:
                    await self._backoff(attempt, retry_after, reason=f"HTTP {status}")
                    continue
                self.breaker.record_failure(f"HTTP {status}")
                if status == 429:
                    raise SheetsThrottled(
                        "sheets quota exceeded; retry budget exhausted",
                        status_code=status,
                        retry_after=retry_after,
                    )
                raise SheetsError(
                    f"sheets server error after {attempt} attempts", status_code=status
                )
            code, message = _error_body(response)
            if status == 404:
                raise SheetsNotFound(message or "spreadsheet not found", status_code=404, code=code)
            if status in (401, 403):
                hint = ""
                if status == 403 and self.service_account_email:
                    hint = f" (is the sheet shared with {self.service_account_email}?)"
                raise SheetsAuthError(
                    (message or "forbidden") + hint, status_code=status, code=code
                )
            raise SheetsError(message or f"HTTP {status}", status_code=status, code=code)

    async def _backoff(self, attempt: int, retry_after: float | None, *, reason: str) -> None:
        if retry_after is not None:
            delay = min(retry_after, self._max_backoff)
        else:
            delay = min(self._base_backoff * (2 ** (attempt - 1)), self._max_backoff)
            delay += random.uniform(0, delay * 0.1)
        log.info("sheets.retry", attempt=attempt, delay_seconds=round(delay, 3), reason=reason)
        await self._sleep(delay)

    # -- SheetsGateway -----------------------------------------------------------------------

    async def healthcheck(self) -> HealthStatus:
        breaker = self.breaker.describe()
        if breaker["state"] == "open":
            return HealthStatus(
                name=self.name, mode=self.mode, ok=False, detail=f"circuit open: {breaker}"
            )
        try:
            response = await self._request(
                "GET", self.spreadsheet_path, params={"fields": "spreadsheetId,properties.title"}
            )
        except Exception as exc:  # health must never raise
            return HealthStatus(name=self.name, mode=self.mode, ok=False, detail=str(exc))
        title = (response.json().get("properties") or {}).get("title", "")
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=True,
            detail=f"spreadsheet {title!r}; circuit {breaker['state']}",
        )

    async def list_tabs(self) -> list[str]:
        response = await self._request(
            "GET", self.spreadsheet_path, params={"fields": "sheets.properties.title"}
        )
        return [
            str(s["properties"]["title"])
            for s in response.json().get("sheets", [])
            if "properties" in s
        ]

    async def ensure_tabs(self, specs: Sequence[TabSpec]) -> list[str]:
        existing = set(await self.list_tabs())
        missing = [s for s in specs if s.name not in existing]
        if not missing:
            return []
        await self._request(
            "POST",
            f"{self.spreadsheet_path}:batchUpdate",
            json_body={
                "requests": [{"addSheet": {"properties": {"title": s.name}}} for s in missing]
            },
        )
        await self._values_batch_update(
            [
                {
                    "range": a1_range(s.name, HEADER_ROW_NUMBER, HEADER_ROW_NUMBER, s.width),
                    "majorDimension": "ROWS",
                    "values": [list(s.header)],
                }
                for s in missing
            ]
        )
        created = [s.name for s in missing]
        log.info("sheets.tabs_created", tabs=created)
        return created

    async def read_keys(self, tab: str, key_col: int = 0) -> dict[str, int]:
        rows = await self._read_all(tab)
        keys: dict[str, int] = {}
        for offset, row in enumerate(rows[1:]):
            if key_col < len(row) and row[key_col] not in (None, ""):
                keys.setdefault(str(row[key_col]), FIRST_DATA_ROW_NUMBER + offset)
        return keys

    async def read_rows(self, tab: str) -> list[SheetRow]:
        rows = await self._read_all(tab)
        return [list(r) for r in rows[1:]]

    async def upsert_rows(
        self, spec: TabSpec, rows: Sequence[Sequence[Cell]], *, key_col: int | None = None
    ) -> UpsertResult:
        kc = spec.key_col if key_col is None else key_col
        existing = await self._read_all(spec.name)
        result = UpsertResult(tab=spec.name)
        data: list[dict[str, Any]] = []
        header_now = existing[0] if existing else []
        if canon_row(header_now, spec.width) != canon_row(spec.header, spec.width):
            data.append(_value_range(spec.name, HEADER_ROW_NUMBER, spec.header, spec.width))
            result.header_written = True
        index: dict[str, int] = {}
        for offset, row in enumerate(existing[1:]):
            if kc < len(row) and row[kc] not in (None, ""):
                index.setdefault(str(row[kc]), FIRST_DATA_ROW_NUMBER + offset)
        next_row = max(len(existing), HEADER_ROW_NUMBER) + 1
        for incoming in rows:
            row = [_cell(v) for v in incoming]
            key = str(row[kc])
            at = index.get(key)
            if at is None:
                data.append(_value_range(spec.name, next_row, row, spec.width))
                index[key] = next_row
                next_row += 1
                result.created += 1
            elif canon_row(existing[at - 1], spec.width) != canon_row(row, spec.width):
                data.append(_value_range(spec.name, at, row, spec.width))
                result.updated += 1
            else:
                result.unchanged += 1
        if data:
            await self._values_batch_update(data)
        log.info(
            "sheets.upserted",
            tab=spec.name,
            created=result.created,
            updated=result.updated,
            unchanged=result.unchanged,
        )
        return result

    # -- internals ---------------------------------------------------------------------------

    async def _read_all(self, tab: str) -> list[SheetRow]:
        rng = f"{quote_tab(tab)}!A:{READ_LAST_COLUMN}"
        try:
            response = await self._request(
                "GET",
                f"{self.spreadsheet_path}/values/{quote(rng, safe='')}",
                params={
                    "majorDimension": "ROWS",
                    "valueRenderOption": "UNFORMATTED_VALUE",
                    "dateTimeRenderOption": "FORMATTED_STRING",
                },
            )
        except SheetsError as exc:
            # A missing tab is a 400 INVALID_ARGUMENT "Unable to parse range"; surface it as
            # not-found so callers can ensure_tabs() and retry.
            if exc.status_code == 400 and "range" in str(exc).lower():
                raise SheetsNotFound(
                    f"tab {tab!r} not found", status_code=400, code=exc.code
                ) from exc
            raise
        values = response.json().get("values") or []
        return [list(r) for r in values]

    async def _values_batch_update(self, data: list[dict[str, Any]]) -> None:
        for start in range(0, len(data), BATCH_RANGES):
            chunk = data[start : start + BATCH_RANGES]
            await self._request(
                "POST",
                f"{self.spreadsheet_path}/values:batchUpdate",
                json_body={
                    "valueInputOption": "RAW",
                    "includeValuesInResponse": False,
                    "data": chunk,
                },
            )


def _cell(value: Cell) -> Cell:
    # The API rejects NaN/inf and we never write them; None becomes an empty cell.
    return "" if value is None else value


def _value_range(tab: str, row_number: int, row: Sequence[Cell], width: int) -> dict[str, Any]:
    cells = [_cell(v) for v in row[:width]]
    cells.extend("" for _ in range(width - len(cells)))
    return {
        "range": a1_range(tab, row_number, row_number, width),
        "majorDimension": "ROWS",
        "values": [cells],
    }


def _error_body(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        body = response.json()
    except ValueError:
        return None, None
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return None, None
    return err.get("status") or err.get("code"), err.get("message")


__all__ = [
    "ASSERTION_LIFETIME",
    "BATCH_RANGES",
    "INSTALL_HINT",
    "JWT_GRANT_TYPE",
    "MAX_ASSERTION_LIFETIME",
    "SHEETS_BASE_URL",
    "SHEETS_SCOPE",
    "TOKEN_URL",
    "AccessToken",
    "LiveSheetsGateway",
    "ServiceAccountKey",
    "ServiceAccountTokenProvider",
    "Signer",
    "TokenProvider",
    "a1_range",
    "column_letter",
    "google_auth_signer",
    "quote_tab",
]
