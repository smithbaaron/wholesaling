"""Live Sheets client against ``httpx.MockTransport``: no network, no credentials, no RSA
library. A fake signer stands in for RS256 so the JWT claim shape and the token exchange are
tested exactly; the real signer path is tested only for its ImportError message."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.graph.live import CircuitBreaker
from wholesaling.integrations.sheets import (
    SheetsAuthError,
    SheetsCircuitOpen,
    SheetsError,
    SheetsNotFound,
    SheetsThrottled,
    TabSpec,
    get_sheets,
)
from wholesaling.integrations.sheets.live import (
    ASSERTION_LIFETIME,
    INSTALL_HINT,
    JWT_GRANT_TYPE,
    SHEETS_BASE_URL,
    SHEETS_SCOPE,
    TOKEN_URL,
    LiveSheetsGateway,
    ServiceAccountKey,
    ServiceAccountTokenProvider,
    a1_range,
    column_letter,
    google_auth_signer,
    quote_tab,
)

SPREADSHEET = "1AbC_def-GHI"
SA_EMAIL = "sync@proj.iam.gserviceaccount.com"
KEY_JSON = {
    "type": "service_account",
    "client_email": SA_EMAIL,
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    "private_key_id": "kid-1",
    "token_uri": TOKEN_URL,
}
SPEC = TabSpec(name="Buy Boxes", header=("buy_box_id", "investor_id", "status", "price_max"))
SHEET_PATH = f"/v4/spreadsheets/{SPREADSHEET}"


def fake_signer(message: bytes) -> bytes:
    return b"sig:" + message[:8]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.scripts: dict[tuple[str, str], list[Callable[[httpx.Request], httpx.Response]]] = {}
        self.token_calls = 0
        self.token_status = 200

    def on(
        self, method: str, path: str, *responses: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self.scripts.setdefault((method.upper(), path), []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(
                    self.token_status, json={"error": "invalid_grant", "error_description": "bad"}
                )
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.token_calls}",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        key = (request.method, unquote(request.url.path))
        queue = self.scripts.get(key)
        if not queue:
            return httpx.Response(500, json={"error": {"message": f"unscripted {key}"}})
        fn = queue.pop(0) if len(queue) > 1 else queue[0]
        return fn(request)

    def api_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) != TOKEN_URL]


def ok(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, json=payload)


def status(
    code: int, headers: dict[str, str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(
        code,
        headers=headers or {},
        json={"error": {"code": code, "message": f"err {code}", "status": "X"}},
    )


def settings(**extra: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        sheets_mode="live",
        sheets_spreadsheet_id=SPREADSHEET,
        google_service_account_json=json.dumps(KEY_JSON),
        **extra,
    )


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gw(rec: Recorder, clock: FakeClock) -> LiveSheetsGateway:
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    g = LiveSheetsGateway(
        settings(), http=http, clock=clock, sleeper=sleeper, signer=fake_signer, max_retries=2
    )
    g.sleeps = sleeps  # type: ignore[attr-defined]
    return g


# --------------------------------------------------------------------------------------------
# Helpers and key loading
# --------------------------------------------------------------------------------------------


def test_column_letters_and_a1_ranges() -> None:
    assert [column_letter(i) for i in (0, 25, 26, 27, 51, 52, 701)] == [
        "A",
        "Z",
        "AA",
        "AB",
        "AZ",
        "BA",
        "ZZ",
    ]
    assert quote_tab("Buy Boxes") == "'Buy Boxes'"
    assert quote_tab("Bob's") == "'Bob''s'"
    assert a1_range("Buy Boxes", 2, 2, 4) == "'Buy Boxes'!A2:D2"
    assert a1_range("QA Log", 1, None, 11) == "'QA Log'!A1:K"
    with pytest.raises(ValueError):
        column_letter(-1)


def test_service_account_key_loads_inline_json_or_path(tmp_path: Any) -> None:
    key = ServiceAccountKey.load(json.dumps(KEY_JSON))
    assert key.client_email == SA_EMAIL and key.private_key_id == "kid-1"
    assert "fake" not in repr(key) and "***" in repr(key)
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(KEY_JSON))
    assert ServiceAccountKey.load(str(path)).client_email == SA_EMAIL
    with pytest.raises(SheetsAuthError, match="not found"):
        ServiceAccountKey.load(str(tmp_path / "missing.json"))
    with pytest.raises(SheetsAuthError, match="valid JSON"):
        ServiceAccountKey.load("{not json")
    with pytest.raises(SheetsAuthError, match="service_account"):
        ServiceAccountKey.load(json.dumps({"type": "authorized_user"}))
    with pytest.raises(SheetsAuthError, match="missing"):
        ServiceAccountKey.load(json.dumps({"type": "service_account", "client_email": "x"}))


def test_live_mode_requires_spreadsheet_id_and_key() -> None:
    with pytest.raises(SheetsAuthError, match="SHEETS_SPREADSHEET_ID"):
        LiveSheetsGateway(
            Settings(_env_file=None, app_env="test", sheets_mode="live")  # type: ignore[call-arg]
        )
    with pytest.raises(SheetsAuthError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        LiveSheetsGateway(
            Settings(  # type: ignore[call-arg]
                _env_file=None, app_env="test", sheets_mode="live", sheets_spreadsheet_id="x"
            )
        )


def test_factory_builds_live_gateway_in_live_mode() -> None:
    g = get_sheets(settings())
    assert isinstance(g, LiveSheetsGateway)
    assert g.mode == "live" and g.service_account_email == SA_EMAIL


def test_default_signer_without_google_auth_gives_install_hint() -> None:
    pytest.importorskip  # noqa: B018 - documenting intent: we require google.auth to be ABSENT
    try:
        import google.auth  # type: ignore[import-not-found,unused-ignore]  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError, match="google-auth"):
            google_auth_signer("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
        assert "uv add google-auth" in INSTALL_HINT
    else:  # pragma: no cover - only when the optional extra is installed
        pytest.skip("google-auth is installed; ImportError path not exercised")


# --------------------------------------------------------------------------------------------
# Token exchange
# --------------------------------------------------------------------------------------------


async def test_jwt_assertion_claims_and_token_exchange(rec: Recorder, clock: FakeClock) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    provider = ServiceAccountTokenProvider(
        ServiceAccountKey.from_json(KEY_JSON), http=http, clock=clock, signer=fake_signer
    )
    token = await provider.get_token()
    assert token == "tok-1" and rec.token_calls == 1
    body = rec.requests[0].content.decode()
    assert f"grant_type={JWT_GRANT_TYPE.replace(':', '%3A')}" in body
    assertion = unquote(body.split("assertion=")[1])
    header_b64, claims_b64, sig_b64 = assertion.split(".")

    def _dec(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    header = json.loads(_dec(header_b64))
    claims = json.loads(_dec(claims_b64))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    assert claims["iss"] == SA_EMAIL
    assert claims["scope"] == SHEETS_SCOPE == "https://www.googleapis.com/auth/spreadsheets"
    assert claims["aud"] == TOKEN_URL == "https://oauth2.googleapis.com/token"
    assert claims["iat"] == int(clock.now.timestamp())
    assert claims["exp"] - claims["iat"] == int(ASSERTION_LIFETIME.total_seconds()) <= 3600
    assert _dec(sig_b64) == fake_signer(f"{header_b64}.{claims_b64}".encode())
    # Cached until near expiry, refreshed after.
    assert await provider.get_token() == "tok-1" and rec.token_calls == 1
    clock.advance(3600 - 100)
    assert await provider.get_token() == "tok-2" and rec.token_calls == 2
    provider.invalidate()
    assert provider.cached is None
    assert "***" in repr(await provider._fetch())


async def test_token_failure_is_auth_error(rec: Recorder, clock: FakeClock) -> None:
    rec.token_status = 400
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    provider = ServiceAccountTokenProvider(
        ServiceAccountKey.from_json(KEY_JSON), http=http, clock=clock, signer=fake_signer
    )
    with pytest.raises(SheetsAuthError, match="invalid_grant"):
        await provider.get_token()


# --------------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------------


async def test_healthcheck_hits_spreadsheet_metadata(gw: LiveSheetsGateway, rec: Recorder) -> None:
    rec.on("GET", SHEET_PATH, ok({"spreadsheetId": SPREADSHEET, "properties": {"title": "Ops"}}))
    h = await gw.healthcheck()
    assert h.ok and "Ops" in h.detail and h.mode == "live"
    req = rec.api_requests()[0]
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert req.url.params["fields"] == "spreadsheetId,properties.title"
    assert str(req.url).startswith(SHEETS_BASE_URL)


async def test_ensure_tabs_adds_missing_sheets_and_writes_headers(
    gw: LiveSheetsGateway, rec: Recorder
) -> None:
    rec.on("GET", SHEET_PATH, ok({"sheets": [{"properties": {"title": "Investors"}}]}))
    rec.on("POST", f"{SHEET_PATH}:batchUpdate", ok({"replies": [{"addSheet": {}}]}))
    rec.on("POST", f"{SHEET_PATH}/values:batchUpdate", ok({"totalUpdatedRows": 1}))
    created = await gw.ensure_tabs(
        [TabSpec(name="Investors", header=("investor_id", "status")), SPEC]
    )
    assert created == ["Buy Boxes"]
    posts = [r for r in rec.api_requests() if r.method == "POST"]
    assert json.loads(posts[0].content) == {
        "requests": [{"addSheet": {"properties": {"title": "Buy Boxes"}}}]
    }
    header_write = json.loads(posts[1].content)
    assert header_write["valueInputOption"] == "RAW"
    assert header_write["data"] == [
        {"range": "'Buy Boxes'!A1:D1", "majorDimension": "ROWS", "values": [list(SPEC.header)]}
    ]


async def test_ensure_tabs_noop_when_all_present(gw: LiveSheetsGateway, rec: Recorder) -> None:
    rec.on("GET", SHEET_PATH, ok({"sheets": [{"properties": {"title": "Buy Boxes"}}]}))
    assert await gw.ensure_tabs([SPEC]) == []
    assert len(rec.api_requests()) == 1


async def test_upsert_payload_shape_updates_in_place_and_appends(
    gw: LiveSheetsGateway, rec: Recorder
) -> None:
    values_path = f"{SHEET_PATH}/values/'Buy Boxes'!A:ZZ"
    rec.on(
        "GET",
        values_path,
        ok(
            {
                "range": "'Buy Boxes'!A1:D3",
                "majorDimension": "ROWS",
                "values": [
                    list(SPEC.header),
                    ["bb-1", "inv-1", "current", 250000],
                    ["bb-2", "inv-1", "current"],  # trailing empty omitted by the API
                ],
            }
        ),
    )
    rec.on("POST", f"{SHEET_PATH}/values:batchUpdate", ok({"totalUpdatedRows": 2}))
    result = await gw.upsert_rows(
        SPEC,
        [
            ["bb-1", "inv-1", "current", 250000.0],  # unchanged (numeric round trip)
            ["bb-2", "inv-1", "inactive", None],  # status changed -> rewritten in place
            ["bb-3", "inv-2", "current", 90000],  # new -> appended at row 4
        ],
    )
    assert (result.created, result.updated, result.unchanged) == (1, 1, 1)
    assert result.header_written is False
    get_req = next(r for r in rec.api_requests() if r.method == "GET")
    assert get_req.url.params["valueRenderOption"] == "UNFORMATTED_VALUE"
    post = json.loads(next(r for r in rec.api_requests() if r.method == "POST").content)
    assert post["valueInputOption"] == "RAW" and post["includeValuesInResponse"] is False
    assert post["data"] == [
        {
            "range": "'Buy Boxes'!A3:D3",
            "majorDimension": "ROWS",
            "values": [["bb-2", "inv-1", "inactive", ""]],
        },
        {
            "range": "'Buy Boxes'!A4:D4",
            "majorDimension": "ROWS",
            "values": [["bb-3", "inv-2", "current", 90000]],
        },
    ]


async def test_upsert_with_nothing_changed_makes_no_write(
    gw: LiveSheetsGateway, rec: Recorder
) -> None:
    rec.on(
        "GET",
        f"{SHEET_PATH}/values/'Buy Boxes'!A:ZZ",
        ok({"values": [list(SPEC.header), ["bb-1", "inv-1", "current", 1]]}),
    )
    result = await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1]])
    assert result.unchanged == 1 and result.changed == 0
    assert all(r.method == "GET" for r in rec.api_requests())


async def test_upsert_repairs_header_and_handles_empty_tab(
    gw: LiveSheetsGateway, rec: Recorder
) -> None:
    rec.on("GET", f"{SHEET_PATH}/values/'Buy Boxes'!A:ZZ", ok({"range": "x"}))  # no values at all
    rec.on("POST", f"{SHEET_PATH}/values:batchUpdate", ok({}))
    result = await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1]])
    assert result.header_written is True and result.created == 1
    post = json.loads(rec.api_requests()[-1].content)
    assert [d["range"] for d in post["data"]] == ["'Buy Boxes'!A1:D1", "'Buy Boxes'!A2:D2"]


async def test_large_upsert_is_chunked(gw: LiveSheetsGateway, rec: Recorder) -> None:
    from wholesaling.integrations.sheets.live import BATCH_RANGES

    rec.on("GET", f"{SHEET_PATH}/values/'Buy Boxes'!A:ZZ", ok({"values": [list(SPEC.header)]}))
    rec.on("POST", f"{SHEET_PATH}/values:batchUpdate", ok({}))
    n = BATCH_RANGES + 5
    result = await gw.upsert_rows(SPEC, [[f"bb-{i}", "inv", "current", i] for i in range(n)])
    assert result.created == n
    posts = [r for r in rec.api_requests() if r.method == "POST"]
    assert len(posts) == 2
    assert len(json.loads(posts[0].content)["data"]) == BATCH_RANGES
    assert len(json.loads(posts[1].content)["data"]) == 5


async def test_read_keys_and_read_rows(gw: LiveSheetsGateway, rec: Recorder) -> None:
    rec.on(
        "GET",
        f"{SHEET_PATH}/values/'Buy Boxes'!A:ZZ",
        ok(
            {
                "values": [
                    list(SPEC.header),
                    ["bb-1", "i", "current", 1],
                    [],
                    ["bb-2", "i", "current"],
                ]
            }
        ),
    )
    assert await gw.read_keys("Buy Boxes") == {"bb-1": 2, "bb-2": 4}
    assert await gw.read_rows("Buy Boxes") == [
        ["bb-1", "i", "current", 1],
        [],
        ["bb-2", "i", "current"],
    ]


async def test_missing_tab_read_maps_to_not_found(gw: LiveSheetsGateway, rec: Recorder) -> None:
    rec.on(
        "GET",
        f"{SHEET_PATH}/values/'Buy Boxes'!A:ZZ",
        lambda _r: httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "Unable to parse range: 'Buy Boxes'!A:ZZ",
                    "status": "INVALID_ARGUMENT",
                }
            },
        ),
    )
    with pytest.raises(SheetsNotFound):
        await gw.read_rows("Buy Boxes")


# --------------------------------------------------------------------------------------------
# Resilience
# --------------------------------------------------------------------------------------------


async def test_429_retries_with_retry_after_then_succeeds(
    gw: LiveSheetsGateway, rec: Recorder
) -> None:
    rec.on(
        "GET",
        SHEET_PATH,
        status(429, {"Retry-After": "2"}),
        status(503),
        ok({"spreadsheetId": SPREADSHEET, "properties": {"title": "Ops"}}),
    )
    assert (await gw.healthcheck()).ok
    sleeps: list[float] = gw.sleeps  # type: ignore[attr-defined]
    assert sleeps[0] == 2.0  # honoured Retry-After
    assert 0.9 < sleeps[1] <= 1.2  # 0.5 * 2^(2-1) + jitter
    assert len(rec.api_requests()) == 3
    assert gw.breaker.failures == 0


async def test_429_budget_exhausted_raises_throttled_and_counts_failure(
    gw: LiveSheetsGateway, rec: Recorder
) -> None:
    rec.on("GET", SHEET_PATH, status(429, {"Retry-After": "1"}))
    with pytest.raises(SheetsThrottled) as exc:
        await gw.list_tabs()
    assert exc.value.retry_after == 1.0 and exc.value.status_code == 429
    assert len(rec.api_requests()) == 3  # 1 + max_retries(2)
    assert gw.breaker.failures == 1


async def test_401_refreshes_token_once(gw: LiveSheetsGateway, rec: Recorder) -> None:
    rec.on("GET", SHEET_PATH, status(401), ok({"sheets": []}))
    assert await gw.list_tabs() == []
    assert rec.token_calls == 2
    auths = [r.headers["Authorization"] for r in rec.api_requests()]
    assert auths == ["Bearer tok-1", "Bearer tok-2"]


async def test_403_is_auth_error_with_share_hint(gw: LiveSheetsGateway, rec: Recorder) -> None:
    rec.on(
        "GET",
        SHEET_PATH,
        lambda _r: httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "The caller does not have permission",
                    "status": "PERMISSION_DENIED",
                }
            },
        ),
    )
    with pytest.raises(SheetsAuthError, match=SA_EMAIL) as exc:
        await gw.list_tabs()
    assert exc.value.code == "PERMISSION_DENIED"
    assert gw.breaker.failures == 0  # client errors do not trip the breaker


async def test_404_is_not_found_and_other_4xx_generic(gw: LiveSheetsGateway, rec: Recorder) -> None:
    rec.on("GET", SHEET_PATH, status(404), status(400))
    with pytest.raises(SheetsNotFound):
        await gw.list_tabs()
    with pytest.raises(SheetsError) as exc:
        await gw.list_tabs()
    assert exc.value.status_code == 400


async def test_breaker_opens_after_consecutive_failures_and_half_opens(
    rec: Recorder, clock: FakeClock
) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))

    async def sleeper(_d: float) -> None:
        return None

    gw = LiveSheetsGateway(
        settings(),
        http=http,
        clock=clock,
        sleeper=sleeper,
        signer=fake_signer,
        max_retries=0,
        breaker=CircuitBreaker(failure_threshold=2, open_seconds=60, clock=clock),
    )
    rec.on("GET", SHEET_PATH, status(500))
    for _ in range(2):
        with pytest.raises(SheetsError):
            await gw.list_tabs()
    assert gw.breaker.state == "open"
    with pytest.raises(SheetsCircuitOpen):
        await gw.list_tabs()
    assert len(rec.api_requests()) == 2  # fail-fast: no third request
    health = await gw.healthcheck()
    assert not health.ok and "circuit open" in health.detail
    clock.advance(61)
    assert gw.breaker.state == "half_open"
    rec.scripts.clear()
    rec.on("GET", SHEET_PATH, ok({"sheets": [{"properties": {"title": "Investors"}}]}))
    assert await gw.list_tabs() == ["Investors"]
    assert gw.breaker.state == "closed"


async def test_transport_error_retries_then_raises(rec: Recorder, clock: FakeClock) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return rec.handler(request)
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    async def sleeper(_d: float) -> None:
        return None

    gw = LiveSheetsGateway(
        settings(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleeper=sleeper,
        signer=fake_signer,
        max_retries=1,
    )
    with pytest.raises(SheetsError, match="after 2 attempts"):
        await gw.list_tabs()
    assert attempts["n"] == 2 and gw.breaker.failures == 1
