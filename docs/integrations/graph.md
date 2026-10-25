# Microsoft Graph (Outlook mail) integration

Build order step BO-04. Implements REQUIREMENTS.md INT-10..13, INT-25, WF-11, WF-12, WF-19 (step 1),
XC-03, XC-08, INV-23, INV-24, RUL-05, RUL-06. Human checkpoint: PRE-05 (docs/CHECKPOINTS.md).

Code map:

| Path | Role |
|---|---|
| `wholesaling/integrations/graph/interface.py` | `GraphGateway` Protocol, error types, subscription lifetime constants |
| `wholesaling/integrations/graph/models.py` | `InboundMessage`, `OutboundDraft`, `DraftInfo`, `SendResult`, `Subscription`, `DeltaPage`, `ChangeNotification`, bounce detection |
| `wholesaling/integrations/graph/mock.py` | `MockGraphGateway` (default; tests and the synthetic pilot) |
| `wholesaling/integrations/graph/auth.py` | client-credentials token provider (httpx, no MSAL), cached with expiry |
| `wholesaling/integrations/graph/live.py` | `LiveGraphGateway`: retries, Retry-After, circuit breaker, live-send guard |
| `wholesaling/api/routes/graph_webhook.py` | `POST /webhooks/graph`: handshake, clientState check, dedupe, enqueue |
| `wholesaling/activities/graph.py` | `fetch_message`, `delta_poll`, `renew_subscriptions`, `record_renewal_outcome`, `raise_alert`, `create_draft`, `send_draft` |
| `wholesaling/workflows/graph_maintenance.py` | `GraphSubscriptionRenewalWorkflow`, `GraphDeltaPollWorkflow`, `GraphInboundMessageWorkflow` |
| `wholesaling/workflows/schedules.py` | `SCHEDULES` data registry (48 h renewal, 15 min delta poll) |

## 1. Modes and `.env` keys

| Key | Meaning |
|---|---|
| `GRAPH_MODE` | `mock` (default) or `live`. Mock needs nothing else. |
| `GRAPH_TENANT_ID` | Entra Directory (tenant) ID |
| `GRAPH_CLIENT_ID` | Application (client) ID of the app registration |
| `GRAPH_CLIENT_SECRET` | Client secret **value** (not the secret ID). Rotate before its expiry. |
| `GRAPH_MAILBOX` | UPN of the sending mailbox, e.g. `deals@<sending-domain>` (PRE-04 domain) |
| `GRAPH_WEBHOOK_URL` | Public `https://.../webhooks/graph` of the `api` service (PRE-08 Railway domain) |
| `GRAPH_WEBHOOK_CLIENT_STATE` | Random 32+ char secret; Graph echoes it in every notification and the webhook rejects anything else |
| `GRAPH_ALLOW_LIVE_SEND` | `false` (default). RUL-06: the live gateway refuses `send_draft` unless `true`. Drafts are unaffected. |

Secrets are `SecretStr` in `Settings`; `wholesaling/logging.py` redacts any log key containing
SECRET/TOKEN/KEY/PASSWORD, and `AccessToken.__repr__` hides the value. Tokens are never put in a
log call.

### Mock mode

`MockGraphGateway` is an in-memory mailbox:

- `inject_inbound(from_address=..., subject=..., body=..., conversation_id=..., ...)` seeds a
  message exactly as a webhook or delta poll would see it (bounce detection runs on it) and
  returns the `InboundMessage`, whose `graph_id` can be posted to the webhook or passed to
  `fetch_message`.
- Every call is recorded in `.calls`; `fail_next(method, exc)` injects one failure.
- Drafts live in `.drafts`, sends in `.sent`; sending the same draft twice raises
  `GraphNotFound` like Graph does.
- Subscriptions expire against an injectable clock (`set_clock`, `expire_subscriptions`).
- Delta tokens are cursors; `delta_messages(folder, None, since=...)` bounds the first round.

The synthetic pilot (BO-09) drives the whole inbound path with `inject_inbound` + the webhook.

## 2. PRE-05: Entra app registration (what the human does)

Prerequisite: a Microsoft 365 **work/school** tenant (Business Basic or above). Personal
Outlook.com accounts cannot grant application permissions, and the application-only flow below
needs a tenant admin.

1. **Mailbox.** Create the sending mailbox on the PRE-04 domain, e.g. `deals@<domain>`. A licensed
   user mailbox is simplest. (A shared mailbox also works for Graph, but see the scoping note in
   step 5: shared mailboxes cannot be the direct target of an application access policy and must
   be put in a mail-enabled security group.)
2. **App registration.** Entra admin center > App registrations > New registration. Single
   tenant. No redirect URI (client-credentials flow has none). Record **Application (client) ID**
   and **Directory (tenant) ID**.
3. **Client secret.** Certificates & secrets > New client secret. Pick an expiry you will track
   (12 months suggested) and copy the **value** into `GRAPH_CLIENT_SECRET`. Prefer a certificate
   later; the token provider only needs a change to the `client_assertion` fields.
4. **API permissions (least privilege).** API permissions > Add > Microsoft Graph >
   **Application permissions**:

   | Permission | Why |
   |---|---|
   | `Mail.Read` | subscriptions, `GET /users/{mbx}/messages/{id}`, delta query |
   | `Mail.ReadWrite` | create drafts (`POST .../messages`, `.../createReply`) |
   | `Mail.Send` | `POST .../messages/{id}/send` (only exercised when `GRAPH_ALLOW_LIVE_SEND=true`) |

   Then **Grant admin consent**. Do not add delegated permissions; nothing in this system acts
   on behalf of a signed-in user.
5. **Scope the app to the one mailbox.** Application permissions are tenant-wide by default:
   `Mail.Read` lets the app read every mailbox. Restrict it to `GRAPH_MAILBOX` in Exchange
   Online. Microsoft now recommends **RBAC for Applications** and says not to create new
   application access policies (they "will eventually require migration"). Both are documented
   here because the policy cmdlets are still widely referenced.

   **Preferred: RBAC for Applications** (Exchange Online PowerShell, `Connect-ExchangeOnline`):

   ```powershell
   # 1. A scope that matches only the sending mailbox (Alias of deals@<domain>)
   New-ManagementScope -Name "Wholesaling deals mailbox" -RecipientRestrictionFilter "Alias -eq 'deals'"

   # 2. A pointer to the app's service principal. Take BOTH ids from Entra > Enterprise applications
   #    (the App registrations page shows a different object id).
   New-ServicePrincipal -AppId <client id> -ObjectId <enterprise-app object id> -DisplayName "wholesaling-graph"

   # 3. Assign the mail roles, scoped
   New-ManagementRoleAssignment -App <client id> -Role "Application Mail.Read"      -CustomResourceScope "Wholesaling deals mailbox"
   New-ManagementRoleAssignment -App <client id> -Role "Application Mail.ReadWrite" -CustomResourceScope "Wholesaling deals mailbox"
   New-ManagementRoleAssignment -App <client id> -Role "Application Mail.Send"      -CustomResourceScope "Wholesaling deals mailbox"

   # 4. Verify (bypasses the permission cache)
   Test-ServicePrincipalAuthorization -Identity <client id> -Resource deals@<domain>
   ```

   With RBAC the resource scoping only takes effect if the **unscoped Entra grant is removed**:
   Microsoft's FAQ says "the union of an unscoped `Mail.Read` grant from Microsoft Entra and a
   resource-scoped `Mail.Read` grant in Application RBAC results in no effective resource
   scoping." So when using RBAC, grant the roles in Exchange and remove (or never consent to) the
   tenant-wide Graph permissions from step 4. Permission changes are cached 30 minutes to 2 hours;
   the `Test-` cmdlet bypasses the cache.

   **Alternative: application access policy** (older mechanism, still functional):

   ```powershell
   # Mail-enabled security group containing only deals@<domain>
   New-ApplicationAccessPolicy -AccessRight RestrictAccess -AppId <client id> `
     -PolicyScopeGroupId wholesaling-mailboxes@<domain> `
     -Description "Restrict the wholesaling Graph app to the deals mailbox"
   Test-ApplicationAccessPolicy -Identity deals@<domain> -AppId <client id>
   ```

   `PolicyScopeGroupId` accepts a user mailbox, a mail user, or a **mail-enabled security
   group**; it rejects distribution groups, Microsoft 365 Groups and shared mailboxes directly.
6. **Webhook secret.** Generate a random 32+ character string for `GRAPH_WEBHOOK_CLIENT_STATE`.
7. Fill the `.env` keys above, set `GRAPH_MODE=live`, and run the live test: `GET /ready` should
   report `graph` ok, `renew_subscriptions` should create a subscription, and a draft created
   through `create_draft` should appear in the mailbox's Drafts folder. Sending stays off.

## 3. Facts verified on Microsoft Learn (2026-10-25)

Change notifications overview
(https://learn.microsoft.com/en-us/graph/change-notifications-overview):

- Subscription lifetime for Outlook `message` (and event, contact): **10,080 minutes (under
  seven days)**. Rich notifications (with resource data) are limited to 1,440 minutes. The
  previous ~3-day (4,230 minute) ceiling the task brief mentioned is no longer current.
  Apps "need to renew their subscriptions before the expiration time; otherwise, they need to
  create a new subscription."
- Outlook message notification latency: average under 1 minute, maximum 3 minutes.
- Quota: 1,000 active subscriptions per mailbox across all applications.

Webhook delivery
(https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks):

- **Validation handshake**: Graph POSTs to the notification URL with `?validationToken=...` and
  expects, within **10 seconds**, `200 OK`, content type `text/plain`, and the **URL-decoded**
  token as the body. An encoded token fails validation and the subscription is not created.
- **clientState** is required, "should remain secret", and must be validated on every
  notification: "If there's a mismatch, don't consider the change notification as valid."
- **Response to notifications**: a 2xx within **3 seconds** counts as delivered; otherwise Graph
  retries with exponential backoff for up to **4 hours** (timeout extended to 10 s on retries).
  Recommended: validate, queue, return `202 Accepted`. Return 5xx when nothing was queued.
- The notification URL must be publicly reachable over **HTTPS**.
- Throttling "might include dropping notifications in a way that they can't be recovered";
  hence the delta-poll reconciliation (WF-12).

Delta query for messages (https://learn.microsoft.com/en-us/graph/delta-query-messages):

- Per-folder: `GET /users/{id}/mailFolders/{folder}/messages/delta`; follow `@odata.nextLink`
  until an `@odata.deltaLink` appears, then store that URL for the next round.
- `Prefer: odata.maxpagesize=N` sets the page size; `$select` is supported;
  `$filter=receivedDateTime ge {value}` is the only supported filter form (bounded to 5,000
  messages); deleted items arrive with `"@removed": {"reason": "deleted"}`.

Mailbox scoping:

- `New-ApplicationAccessPolicy` reference
  (https://learn.microsoft.com/powershell/module/exchange/New-ApplicationAccessPolicy): accepted
  scope principals, `RestrictAccess` example, and the notice that policies are replaced by RBAC
  for Applications.
- RBAC for Applications (https://learn.microsoft.com/Exchange/permissions-exo/application-rbac):
  role names `Application Mail.Read`, `Application Mail.ReadWrite`, `Application Mail.Send`,
  `New-ServicePrincipal`, `New-ManagementRoleAssignment -CustomResourceScope`,
  `Test-ServicePrincipalAuthorization`, the 30 min to 2 h cache, and the union-with-Entra caveat.

## 4. How the pieces fit

```
Graph ──POST /webhooks/graph──▶ api (validate clientState, dedupe, 202)
                                   │ start_workflow(GraphInboundMessageWorkflow, id=sha(sub,msg))
                                   ▼
                              Temporal ──▶ worker: graph_fetch_message
                                              │ GET message → upsert threads/messages (unique
                                              │ (channel, internetMessageId)) → audit event
                                              │ NDR? → suppression(reason=bounce)
Schedule 15 min ──▶ GraphDeltaPollWorkflow ──▶ graph_delta_poll (same upsert; lookback 24 h)
Schedule 48 h   ──▶ GraphSubscriptionRenewalWorkflow ──▶ graph_renew_subscriptions (expiry now+70 h)
                                                     └─▶ record outcome; alert after 2 failures
Sequence/agent  ──▶ graph_create_draft (messages row: out, draft) ──▶ graph_send_draft (sent_at)
```

### Idempotency, in one place

| Event | Key | Where enforced |
|---|---|---|
| Inbound message | `(channel='email', external_message_id=internetMessageId)` | `messages` unique constraint; `upsert_inbound` |
| Inbound audit event | `graph:{internetMessageId}:received` | `audit_events.idempotency_key` |
| Webhook notification | `(subscriptionId, resourceData.id)` → workflow id `graph-inbound-{sha256[:32]}` | in-memory LRU, then Temporal's workflow-id policy |
| Bounce suppression | `(channel, normalized email)`; audit `graph:{id}:bounce` | `suppression` unique constraint |
| Draft | caller's `idempotency_key` stored as `external_message_id` | `messages` unique constraint |
| Send | `messages.sent_at` already set → no Graph call; Graph 404 on the draft → treated as sent | `send_draft` activity |

### Subscription renewal cadence

The renewal schedule fires every 48 h (WF-11) and sets expiry to now + 70 h. One missed run
leaves 22 h of slack; two missed runs lapse the subscription, at which point
`renew_subscriptions` creates a new one (and the webhook's lifecycle handler starts a renewal
immediately on `reauthorizationRequired` / `missed` / `subscriptionRemoved`). Delta polling
covers anything missed in between.

### Bounce detection (INT-25)

An inbound message is a bounce when any of these holds: `Content-Type` is
`multipart/report; report-type=delivery-status`; the sender local part is `postmaster`,
`mailer-daemon` or `microsoftexchange…`; or the subject starts with a known NDR phrase
("Undeliverable:", "Delivery Status Notification (Failure)", "Mail delivery failed", ...). The
failed address is taken from `X-Failed-Recipients`, else from the body around the usual NDR
phrasing, else the first address in the body that is not ours or the sender's. A bounce with no
identifiable address is recorded on the message (`meta.is_bounce`) but adds no suppression.

## 5. Live-mode behaviour (XC-08)

- Retries: up to 3 on 429/500/502/503/504 and on transport errors; `Retry-After` (seconds or
  HTTP-date) wins over the exponential backoff (0.5 s base, 30 s cap, 10 % jitter).
- 401: one token refresh and retry; a second 401/403 raises `GraphAuthError`.
- 404 raises `GraphNotFound` and does not count against the breaker.
- Circuit breaker: 5 consecutive failed requests open it for 60 s; while open every call raises
  `GraphCircuitOpen` without a request and `/ready` reports `graph` down with the breaker state.
  The first call after the window is a half-open probe.
- `send_draft` is refused with `GraphSendForbidden` unless `GRAPH_ALLOW_LIVE_SEND=true`
  (RUL-06). It is read before sending so `internetMessageId` / `conversationId` are recorded;
  Graph moves a sent item to Sent Items under a new id.

## 6. Operating the schedules

`wholesaling/workflows/schedules.py` is a data registry. Nothing creates schedules yet; an ops
command will read `SCHEDULES` and call the Temporal Schedules API (`ScheduleSpec.interval`,
`overlap="skip"`, args already built). Until then the two workflows can be started by hand with
`Client.start_workflow("GraphDeltaPollWorkflow", DeltaPollWorkflowInput(), ...)`.

## 7. Decisions not covered by the spec

1. **Delta token persistence deferred.** Scheduled runs are stateless; `delta_poll` without a
   token reconciles the last 24 h via `$filter=receivedDateTime ge ...` instead. Every write is
   idempotent so the overlapping windows are harmless. Persisting the `deltaLink` (a tiny
   `integration_state` table or a Temporal continue-as-new loop) is a later optimisation.
2. **Renewal failure streak lives in `audit_events`** (`graph.subscription_renewal` rows), not in
   workflow state, so the "alert after two consecutive failures" rule works across scheduled runs.
3. **`GraphInboundMessageWorkflow` added** as the Temporal target of the webhook (the brief named
   no workflow). BO-07 extends it with A4 -> A5 -> policy.
4. **Lifecycle notifications** land on the same URL (`lifecycleNotificationUrl` = webhook URL) and
   start the renewal workflow.
5. **Replies use `createReply` with `comment`**, which keeps the quoted original and Graph's
   threading; `OutboundDraft.conversation_id` is resolved to the latest message in that
   conversation and replied to, since Graph does not let a new message set `conversationId`.
6. **Webhook fails closed** when `GRAPH_WEBHOOK_CLIENT_STATE` is unset (401 for notifications,
   handshake still answered) and returns 503 when nothing could be queued so Graph retries.
7. **`GRAPH_ALLOW_LIVE_SEND`** setting added (default false) to make RUL-06 a code-level guard in
   the live gateway, mirroring how `SMS_MODE=live` is rejected.
8. **Own-mailbox messages seen by delta** (from == `GRAPH_MAILBOX`) are stored with
   `direction=out` so Sent-items reconciliation later does not misfile them.
