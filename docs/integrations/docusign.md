# DocuSign eSignature integration (BO-12; REQUIREMENTS.md INT-16, SEL-10, POL-03i, XC-05, PRE-13)

`wholesaling/integrations/docusign/` — Protocol (`interface.py`), wire shapes (`models.py`),
template registry (`templates.py`), in-memory mock (`mock.py`), live REST v2.1 client (`live.py`).
Selected by `DOCUSIGN_MODE` (`mock` by default; `.env.example` and `Settings` stay in parity by
test). Nothing on this surface is reachable from an agent or a workflow: the only caller is the
operator's `/deals` page (`api/routes/deals.py`), and `tests/workflows/test_disposition_definition.py::test_workflow_module_never_imports_docusign`
pins that.

## The hard floor, in code

| Rule | Where it is enforced |
|---|---|
| Contract execution is human at every level (POL-03i, XC-05) | `PolicyEngine` returns `queue_for_human` for `CONTRACT_EXECUTION` always; `ContractRepository.mark_signed` / `mark_sent_for_signature` / `create_assignment` raise `HumanActionRequired` for any `actor != human`; the gateway is only constructed inside `/deals` handlers |
| Envelopes are never sent by the system | `EnvelopeRequest.status` defaults to `created` (a *draft* in the DocuSign account). The operator opens DocuSign, reads it, sends it, signs it, then clicks **mark signed** on `/deals`. Even the live client's `create_envelope_from_template` is a human click. |
| No contract automation in this build (RUL-06) | `DOCUSIGN_MODE=mock` everywhere; the live module is imported lazily only when `live` is set; PRE-13 (account + attorney-reviewed templates) is open |

## Surface (`DocuSignGateway`)

| Method | REST v2.1 call | Notes |
|---|---|---|
| `create_envelope_from_template(EnvelopeRequest)` | `POST /accounts/{accountId}/envelopes` | body: `templateId`, `emailSubject`, `templateRoles[{roleName,name,email,routingOrder,tabs.textTabs[{tabLabel,value}]}]`, `prefillTabs.textTabs[]`, `status` (`created` default / `sent`), `customFields.textCustomFields[{name:"idempotency_key"}]` |
| `get_envelope_status(envelope_id)` | `GET /accounts/{accountId}/envelopes/{envelopeId}` | `envelopeId`, `status`, `statusDateTime` |
| `void_envelope(envelope_id, reason)` | `PUT /accounts/{accountId}/envelopes/{envelopeId}` with `{"status":"voided","voidedReason":"..."}` | `voidedReason` is required; only in-process envelopes (not draft, not completed) void |
| `list_templates()` | `GET /accounts/{accountId}/templates` | `envelopeTemplates[{templateId,name,shared}]` |
| `healthcheck()` | templates call with `count=1` | |

Envelope statuses handled: `created → sent → delivered → completed`, plus `declined` and `voided`
(`EnvelopeStatus`, `ENVELOPE_TRANSITIONS`). The mock enforces the same lifecycle and nothing in it
advances an envelope on its own: tests (or the operator simulating DocuSign) call
`send/deliver/complete/decline`.

## JWT Grant (live auth)

Facts verified 2026-10-25 against:

- DocuSign developer blog, "Integrate Google Workspace with Docusign APIs using JWT Grant authentication" — claims, `aud` hosts, token endpoint, `grant_type`, scopes, `/oauth/userinfo` → `base_uri` + `/restapi/v2.1`:
  https://www.docusign.com/blog/integrate-google-workspace-with-docusign-apis-using-jwt-grant-authentication
- DocuSign developer blog, "From the Trenches: Authenticate without user interaction via a system user" (JWT = the no-UI grant; one-time consent):
  https://docusign.com/blog/developers/the-trenches-authenticate-without-user-interaction-system-user

| Item | Value in `live.py` |
|---|---|
| JWT header | `{"alg":"RS256","typ":"JWT"}` |
| `iss` | integration key (`DOCUSIGN_INTEGRATION_KEY`) |
| `sub` | API user's GUID (`DOCUSIGN_USER_ID`), impersonated |
| `aud` | the auth host **without scheme**: `account-d.docusign.com` (developer) / `account.docusign.com` (production) — `DOCUSIGN_AUTH_HOST` |
| `iat` / `exp` | now / at most one hour later (`MAX_ASSERTION_LIFETIME`; we use 55 min) |
| `scope` | `signature impersonation` |
| Token request | `POST https://{auth_host}/oauth/token`, form `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=<jwt>` → `access_token`, `expires_in` |
| Account base | `GET https://{auth_host}/oauth/userinfo` → `accounts[]`; pick `DOCUSIGN_ACCOUNT_ID` (or `is_default`) → `{base_uri}/restapi/v2.1` |
| Consent | the user must grant consent to the integration key once (`error=consent_required` otherwise); recorded as a PRE-13 human step |

The project ships no RSA library. Signing goes through an injectable `Signer`; the default reuses
the Sheets client's lazily imported `google-auth` signer and raises an `ImportError` naming the one
optional extra when absent. The private key path is `DOCUSIGN_PRIVATE_KEY_PATH`; the key and the
tokens never reach logs (`repr` masks them).

Resilience (mirrors Graph/Sheets): retry with backoff on 429/5xx honouring `Retry-After`, one
token refresh on 401, circuit breaker (XC-08), `DocuSignThrottled` once the retry budget is spent.
`tests/integrations/docusign/test_live.py` covers all of it over `httpx.MockTransport`.

## Envelope from template (facts verified)

- Community thread confirming the `templateRoles[]` payload (`roleName`, `name`, `email`,
  `recipientId`, `routingOrder`) and that the default status is `created` so an envelope stays a
  draft unless `status: "sent"` is set (DocuSign employee answer):
  https://community.docusign.com/esignature-api-63/why-is-my-envelope-created-from-a-template-via-api-remaining-in-created-status-instead-of-being-sent-22567
- Recipient tabs are set at creation inside `templateRoles[].tabs.textTabs[]` for that role;
  sender pre-fill tabs live at the envelope level (`prefillTabs`) and must match the template's tab labels:
  https://community.docusign.com/esignature-api-63/tab-values-not-filling-when-creating-envelope-from-template-via-api-26627
  https://community.docusign.com/esignature-api-63/how-to-create-an-envelope-from-template-with-prefilled-tabs-23929
- Void: `PUT .../envelopes/{envelopeId}` with `status: voided` and the required `voidedReason`;
  "You can only void envelopes that are in process; you cannot void draft or completed envelopes":
  https://www.docusign.com/blog/dsdev-common-api-tasks-void-an-envelope/

## Templates registry (`templates.py`)

Two templates, drafted and reviewed by a Texas attorney (PRE-13). The code knows only their
shape; the ids go in `DOCUSIGN_PSA_TEMPLATE_ID` / `DOCUSIGN_ASSIGNMENT_TEMPLATE_ID` (the mock
accepts `mock-template-psa` / `mock-template-assignment`).

| Key | Roles (signing order) | Text-tab labels (placeholders agreed with the attorney) |
|---|---|---|
| `psa` — Purchase and Sale Agreement (assignable) | `Seller` → `Buyer` (our entity) | `property_address`, `seller_name`, `buyer_entity`, `purchase_price`, `earnest_money_amount`, `option_fee`, `option_period_days`, `inspection_deadline`, `closing_date`, `title_company`, `assignability_clause`, `equitable_interest_disclosure` (CMP-03) |
| `assignment` — Assignment of PSA | `Assignee` (end buyer) → `Assignor` (our entity) | `property_address`, `original_contract_date`, `seller_name`, `assignor_entity`, `assignee_name`, `assignment_fee`, `assignee_deposit`, `deposit_due_date`, `closing_date`, `title_company`, `earnest_money_credit` |

`tabs_from_fields` maps a deal record onto those labels (money with cents, dates ISO) and never
invents a value: a missing field is left for the signer. `build_psa_request` /
`build_assignment_request` produce the `EnvelopeRequest` the `/deals` handlers send.

## Connect (webhook) payload

Facts verified against:

- DocuSign developer blog, "Event notifications using JSON SIM and HMAC" — JSON SIM delivery
  (`deliveryMode: SIM`, `eventData.version: restv2.1`), the envelope-* / recipient-* event names,
  `includeHMAC`, header names `X-DocuSign-Signature-1..N`, HMAC-SHA256 over the raw body:
  https://www.docusign.com/blog/developers/event-notifications-using-json-sim-and-hmac
- Webhook Relay write-up — payload top level (`event`, `apiVersion`, `uri`, `retryCount`,
  `configurationId`, `generatedDateTime`, `data{accountId, envelopeId, envelopeSummary{status,...}}`),
  Base64 encoding of the HMAC, 2xx-within-100-s rule and ~24 h of retries:
  https://webhookrelay.com/blog/docusign-connect-webhooks/

`ConnectEvent` (pydantic, wire aliases) parses one notification; `envelope_status` maps
`envelope-sent/-delivered/-completed/-declined/-voided` to `EnvelopeStatus` and falls back to
`envelopeSummary.status`. `verify_connect_signature(raw_body, key, header)` is the constant-time
HMAC check against `DOCUSIGN_CONNECT_HMAC_KEY`. The mock's `connect_delivery(envelope_id)` returns
a signed `(body, headers)` pair so a future `/webhooks/docusign` route can be tested end to end.

**There is no Connect route in this build on purpose.** A webhook saying `envelope-completed`
would be tempting to treat as "signed"; SEL-11 says the *human* records the signature. When the
route is added it may only update `contracts.envelope_status` and raise an alert for the operator,
never flip `status` to `signed` (the repository refuses a non-human actor anyway).

## Settings

| Key | Default | Notes |
|---|---|---|
| `DOCUSIGN_MODE` | `mock` | `live` imports `live.py` lazily |
| `DOCUSIGN_INTEGRATION_KEY`, `DOCUSIGN_USER_ID`, `DOCUSIGN_ACCOUNT_ID`, `DOCUSIGN_PRIVATE_KEY_PATH` | unset | PRE-13 |
| `DOCUSIGN_AUTH_HOST` | `account-d.docusign.com` | `account.docusign.com` in production |
| `DOCUSIGN_PSA_TEMPLATE_ID`, `DOCUSIGN_ASSIGNMENT_TEMPLATE_ID` | unset | live mode refuses to create that envelope type without it (`DocuSignTemplateUnconfigured`) |
| `DOCUSIGN_CONNECT_HMAC_KEY` | unset | for the (future) Connect route |

## Tests

`tests/integrations/docusign/`: `test_models.py` (payload shape, registry, Connect, HMAC),
`test_mock.py` (lifecycle, idempotency, void rules, nothing signs by itself, factory),
`test_live.py` (JWT claims, caching, consent error, userinfo discovery, create/get/void/list,
429 + Retry-After, 401 refresh, 403/404/500 mapping, breaker, healthcheck).
