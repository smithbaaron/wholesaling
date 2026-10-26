# Google Sheets one-way projection

Build order step BO-08. Implements REQUIREMENTS.md STK-02, STK-06, INT-14, INT-15, WF-13, WF-14,
XC-09. Human checkpoint: PRE-06 (docs/CHECKPOINTS.md).

Code map:

| Path | Role |
|---|---|
| `wholesaling/integrations/sheets/interface.py` | `SheetsGateway` Protocol, `TabSpec`, `UpsertResult`, error types, `canon_cell`/`canon_row`/`iso_utc` |
| `wholesaling/integrations/sheets/projection.py` | `TAB_SPECS` (the tab contract), `ProjectionSnapshot`, `build_rows`, one pure `project_*` function per tab |
| `wholesaling/integrations/sheets/mock.py` | `MockSheetsGateway` (default; tests and the synthetic pilot) |
| `wholesaling/integrations/sheets/live.py` | `LiveSheetsGateway` (Sheets API v4 over httpx), `ServiceAccountTokenProvider` (JWT -> access token), retries, breaker |
| `wholesaling/activities/sheets.py` | `sync_projection` (WF-13) and `reconcile_sources` (WF-14 / XC-09) |
| `wholesaling/workflows/sheets_sync.py` | `SheetsSyncWorkflow` |
| `wholesaling/workflows/reconciliation.py` | `DailyReconciliationWorkflow` |
| `wholesaling/workflows/buy_box_event.py` | `BuyBoxEventWorkflow` (WF-20; Sheets step is "wait for the next sync") |
| `wholesaling/workflows/schedules.py` | `sheets-sync` (15 min) and `daily-reconciliation` (24 h) entries |

## 1. Why one-way

V2 made PostgreSQL the only writer of truth and demoted Sheets from "initial shared database"
(MS §1, rejected as GEN-05 / V1R-01) to an **operator projection**: a read-only window the
operator can sort, filter and share without touching the system. Consequences, all enforced in
code:

- **Agents never write to Sheets directly.** The only code that calls the Sheets API is the
  `sync_projection` activity; it reads Postgres and writes cells. No agent, workflow or API
  route imports the gateway for writing. `BuyBoxEventWorkflow` does not push to Sheets on a
  `buy_box.*` event; the next scheduled sync picks the change up.
- **Nothing is ever read back.** Reconciliation reads Sheets to *compare*, and reports. It
  never copies a Sheets value into Postgres (V1R-06 rejected "reconciliation from Sheets back to
  the database").
- **No delete call exists on the gateway surface.** A deactivated buy box, a superseded version
  or an excluded investor keeps its row and gets a new `status` value. Every tab has a `status`
  column; `TabSpec` refuses a header without one.
- **Idempotent by key.** Each row's first column is the stable Postgres id. `upsert_rows`
  rewrites a row only when its canonical cells differ and appends unknown keys, so a sync that
  runs twice changes nothing the second time and a Temporal retry with the same idempotency key
  returns the recorded counts without calling the API.

If an operator edits a projected cell by hand, the next sync overwrites it (the sheet is a
projection) and the daily reconciliation flags the drift in between (`sheets_stale_row`). Rows an
operator adds by hand are left alone by the sync and flagged as `sheets_orphan_key`.

## 2. Modes and `.env` keys

| Key | Meaning |
|---|---|
| `SHEETS_MODE` | `mock` (default) or `live`. Mock needs nothing else. |
| `SHEETS_SPREADSHEET_ID` | The id in the sheet URL: `https://docs.google.com/spreadsheets/d/<THIS>/edit` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Path to the downloaded service-account key file (documented form). The loader also accepts the JSON text itself, for secret managers that inject file contents. |

Secrets never reach logs: `ServiceAccountKey.__repr__` and `AccessToken.__repr__` hide their
values and `wholesaling/logging.py` redacts any key containing SECRET/TOKEN/KEY/PASSWORD. Request
logs carry method, path, status and attempt only.

### Mock mode

`MockSheetsGateway` is an in-memory spreadsheet (`sheets[tab] = [header, row, ...]`) with the
same semantics as the live client: 1-based row numbers, header in row 1, idempotent upsert,
no deletes, trailing empties trimmed on read. Test helpers: `row(tab, key)`, `header(tab)`,
`set_cell(...)` (simulate an operator edit), `append_raw(...)` (simulate a hand-typed row),
`fail_next(method, exc)`, `calls`.

## 3. Service account setup (PRE-06)

Everything below happens in the Google Cloud console and the Google Sheets UI; the application
needs only the key file path and the spreadsheet id.

1. **Create (or pick) a Google Cloud project.** <https://console.cloud.google.com/projectcreate>
   Any project works; name it e.g. `northstack-wholesaling`.
2. **Enable the Google Sheets API** for the project.
   <https://console.cloud.google.com/apis/library/sheets.googleapis.com> -> *Enable*.
   (The Drive API is **not** needed: the sheet is created by hand and shared with the service
   account; the `spreadsheets` scope covers reading and writing it.)
3. **Create a service account.**
   <https://console.cloud.google.com/iam-admin/serviceaccounts> -> *Create service account* ->
   name `sheets-sync`. Grant **no** project roles (Sheets access comes from sharing the file, not
   from IAM). Note the email it gets: `sheets-sync@<project>.iam.gserviceaccount.com`.
4. **Create a JSON key.** Open the service account -> *Keys* -> *Add key* -> *Create new key* ->
   *JSON*. The browser downloads `<project>-<id>.json`. Store it outside the repo (e.g.
   `~/.config/wholesaling/sheets-sa.json`), `chmod 600`, and set
   `GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/that/file`. Never commit it; `.gitignore` already
   excludes `*.json` under `var/` but not elsewhere, so keep it out of the tree.
   Reference: <https://developers.google.com/identity/protocols/oauth2/service-account>
   ("Creating a service account").
5. **Create the spreadsheet** in Google Sheets (<https://sheets.new>) under the operator's own
   Google account, e.g. *Wholesaling Ops*. Copy the id from the URL into
   `SHEETS_SPREADSHEET_ID`. Leave the default `Sheet1`; the sync adds the tabs it needs and
   never touches tabs it does not own.
6. **Share the sheet with the service account.** *Share* -> paste the service-account email from
   step 3 -> role **Editor** -> untick "Notify people" -> *Share*. Without this step every API
   call returns `403 PERMISSION_DENIED`; the live client's error message names the service-account
   email so this is the first thing to check.
7. **Set `SHEETS_MODE=live`** and run `uv run python -c "..."` or the API health endpoint: the
   gateway health check does `GET /v4/spreadsheets/{id}?fields=spreadsheetId,properties.title`
   and reports the sheet title.
8. **First sync.** Trigger `SheetsSyncWorkflow` once (or wait for the schedule). It creates the
   ten tabs with their header rows, then projects every row. Pin the header row (*View* ->
   *Freeze* -> *1 row*) by hand if you like; formatting is never touched by the sync.

Optional extra: the live token provider signs the service-account JWT with **RS256**. The
project ships no RSA library (`uv tree` has neither `cryptography` nor `PyJWT`, and adding one
was out of scope for this step), so signing goes through an injectable `Signer`; the default
signer imports `google.auth.crypt` lazily. In live mode run **`uv add google-auth`** once;
without it the first token request raises `ImportError` with that exact command. Mock mode and
the test suite need nothing.

## 4. Google API facts the live client relies on (verified 2026-10-25)

Service endpoint `https://sheets.googleapis.com`; REST method index:
<https://developers.google.com/sheets/api/reference/rest>

| Call | Used for |
|---|---|
| `GET /v4/spreadsheets/{spreadsheetId}` (`fields=sheets.properties.title`) | health, list tabs |
| `POST /v4/spreadsheets/{spreadsheetId}:batchUpdate` with `{"requests":[{"addSheet":{"properties":{"title":...}}}]}` | create missing tabs |
| `GET /v4/spreadsheets/{spreadsheetId}/values/{range}` with `valueRenderOption=UNFORMATTED_VALUE` | read a whole tab (`'Tab Name'!A:ZZ`) |
| `POST /v4/spreadsheets/{spreadsheetId}/values:batchUpdate` with `{"valueInputOption":"RAW","data":[{"range","majorDimension":"ROWS","values"}]}` | header write, in-place row rewrites, appends (200 ranges per call) |

- `spreadsheets.values.batchUpdate` reference (request body `valueInputOption`, `data[]` of
  `ValueRange`, `includeValuesInResponse`; response `totalUpdatedRows` etc.; scopes `drive`,
  `drive.file` or `spreadsheets`):
  <https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate>
- `RAW` vs `USER_ENTERED` and "empty trailing rows and columns are omitted" on read:
  <https://developers.google.com/workspace/sheets/api/guides/values>. The client writes `RAW` so a
  value like `=1+2` or `77009` is stored verbatim, and compares rows through `canon_row`, which
  pads the trailing empties the API omits.
- Sheet names in A1 notation are always single-quoted by the client (`'Buy Boxes'!A2:Z2`), a
  quote inside a name is doubled.

Service-account token flow (<https://developers.google.com/identity/protocols/oauth2/service-account>,
"HTTP/REST" section):

- JWT header `{"alg":"RS256","typ":"JWT"}` (+ optional `kid` = `private_key_id`).
- Claims: `iss` = service-account email, `scope` = `https://www.googleapis.com/auth/spreadsheets`,
  `aud` = `https://oauth2.googleapis.com/token`, `iat`, `exp` **at most 1 hour after `iat`** (the
  client uses 55 minutes).
- `POST https://oauth2.googleapis.com/token` form-encoded with
  `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer` and `assertion=<signed JWT>`.
- Response `{"access_token","token_type":"Bearer","expires_in":3600,...}`; cached and refreshed
  300 s before expiry, one transparent refresh on a 401.

Resilience (same shape as the Graph client): retry with exponential backoff on 429/500/502/503/504
honouring `Retry-After`; consecutive-failure circuit breaker (`CircuitBreaker`, XC-08) that fails
fast with `SheetsCircuitOpen` and half-opens after 60 s; 4xx client errors never trip the breaker;
403 maps to `SheetsAuthError` with the "is the sheet shared with ...?" hint; a `400 Unable to parse
range` on a missing tab maps to `SheetsNotFound`.

## 5. Tab contract (`TAB_SPECS`)

Ten tabs, INT-14's nine plus *Outreach Events* from the Master Spec. The first column of every tab
is the stable Postgres id and the key for upserts; every tab has `status`; datetimes are ISO-8601
UTC (`2026-10-24T15:45:00Z`); money and ratios are numbers; lists are `"; "`-joined; nested JSON
(`closing_preferences`, `other_criteria`, `criteria`) is compact JSON text; free text is capped at
5,000 characters. Rows are sorted by key so repeated syncs produce identical batches.

| Tab | Key | Source | `status` values |
|---|---|---|---|
| Investors | `investor_id` | `investors` (+ market name) | `active`, `excluded` |
| Contacts | `contact_id` | `contacts` | `active` |
| Buy Boxes | `buy_box_id` (+ `investor_id`, `buy_box_key`, `version`) | `buy_boxes`, every version | `current`, `superseded`, `inactive` |
| Search Profiles | `search_profile_id` (+ `buy_box_id`, `investor_id`) | `search_profiles` | enum: `active`, `superseded`, `inactive` |
| Properties | `property_candidate_id` | `property_candidates` | `active` |
| Matches | `match_id` (+ `search_profile_id`, `property_candidate_id`, `buy_box_id`, `investor_id`) | `matches` | review status: `pending`, `approved`, `rejected` |
| Conversations | `thread_id` | `threads` + message aggregates | `active` |
| Suppressions | `suppression_id` | `suppression` | `suppressed` |
| QA Log | `qa_review_id` | `qa_reviews` | verdict: `approve`, `block`, `rewrite` |
| Outreach Events | `message_id` (+ `thread_id`, `investor_id`) | `messages`, both directions | `draft`, `sent`, `received` |

Full headers are in `projection.py`; `row_as_dict(tab, row)` maps a row back to `{header: cell}`.
Changing a header is a contract change: the next sync rewrites the header row in place (the
`header_written` flag in the result) and existing data columns shift only if a column was inserted
rather than appended, so **append new columns at the end**.

## 6. Schedules and results

- `sheets-sync` -> `SheetsSyncWorkflow` every 15 min, overlap `skip` (WF-13). Result:
  `SyncProjectionResult(tabs=[TabSyncResult(tab, rows, created, updated, unchanged, header_written,
  error)], tabs_created, duplicate, spreadsheet_id, synced_at)`. One `sheets.sync` audit event per
  run with the same counts; tab failures are reported per tab, never raised.
- `daily-reconciliation` -> `DailyReconciliationWorkflow` every 24 h (WF-14). Compares:
  - Outlook vs Postgres: Graph `delta_messages(since=now-24h)` against inbound `messages` rows
    received in the window (`outlook_message_missing_in_postgres`,
    `postgres_message_missing_in_outlook`);
  - Postgres vs Sheets per tab: `sheets_missing_key`, `sheets_orphan_key`, `sheets_stale_row`
    (names the differing columns), `sheets_tab_missing`; a source that cannot be read is
    `source_unavailable`.
  Result `ReconcileResult(mismatches=[Mismatch(kind, source, tab, key, detail)], alerted, ...)`,
  one `reconciliation.completed` audit event (first 50 mismatches inline, `truncated` flag) and,
  when anything mismatched, an `alert.raised` event + `log.error("alert", kind=
  "reconciliation_mismatch")`. **It writes nothing else** (XC-09).

## 7. Decisions beyond the spec

1. The Buy Boxes tab carries **every version** (one row per `buy_box_id`), not only the current
   one, so a correction history is visible without a second tab; `status` distinguishes
   `current` / `superseded` / `inactive`.
2. Deleted entities are not modelled (the schema has no deletes either); "inactive"/"excluded"
   statuses cover BB-02 and INV exclusions.
3. `UNFORMATTED_VALUE` on read + `RAW` on write keeps numbers as numbers and avoids Sheets
   re-interpreting ids or zips.
4. The live client reads a whole tab (`A:ZZ`) once per upsert instead of a key-column read plus
   per-row fetches: one GET and one POST per tab per sync at the volumes this build targets.
5. RS256 is provided through the optional `google-auth` extra rather than a new hard dependency;
   see §3.
