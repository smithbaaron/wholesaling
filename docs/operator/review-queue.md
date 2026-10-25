# Operator surface: review queue, policies, daily digest

Build step BO-07 part D. Server-rendered FastAPI + Jinja2 + HTMX, no JS build step (ENG-Q13).
Three pages, all behind one operator login:

| Page | What it is |
|---|---|
| `GET /review` | Drafts the policy engine queued for a human, grouped by action. Approve / edit-and-approve / reject. |
| `GET /policies` | The twelve autonomy actions: level, caps, QA threshold, promotion readiness. Change a level here. |
| `GET /digest` | The BO-21 KPI digest for any local day, as the scheduled job sends it. |

Every page has a JSON twin (`/api/review`, `/api/policies`, `/api/digest`) and `/digest.txt` gives
the plain-text digest body. `/health`, `/ready`, `/webhooks/graph` and `/static/*` stay open.

## Setting the operator password

Single operator, HTTP Basic. The browser asks once per session.

```bash
uv run python -c "from wholesaling.api.auth import hash_password; print(hash_password('your-password'))"
```

Put the output in `.env`:

```
OPERATOR_USERNAME=operator          # or your name; it is what the audit log records
OPERATOR_PASSWORD_HASH=pbkdf2_sha256$600000$<salt>$<hash>
```

The hash is PBKDF2-HMAC-SHA256 with a random 16-byte salt and 600 000 iterations
(`hashlib`, no new dependency). Username and password are both compared in constant time, and a
wrong username still runs the full derivation so response time does not say which half was wrong.
Without `OPERATOR_PASSWORD_HASH` every operator route answers **503**, never "open". Credentials
are never logged; the only thing that reaches the audit trail is the username. Serve the app over
HTTPS (Railway does) because Basic auth sends the password with every request.

## The review queue

### What a queued item is

A `messages` row with `draft = true` and `policy_decision = 'queue_for_human'`. The policy engine
(`wholesaling/policy/engine.py`) put it there because the action is at L0 ("draft, human sends"),
because an L1 condition failed (daily cap reached, QA confidence below threshold, follow-up whose
initial was never approved), because A5 asked for a rewrite, or because the action is a hard floor.

Each card shows:

- recipient, investor, market, sequence step, and how long it has waited;
- subject and a one-line body preview; expand for the full body;
- the A5 QA review: verdict (`approve` / `rewrite` / `block`), confidence, flags, rationale (INV-17/18);
- the policy engine's reason (`messages.meta.policy_reason`).

Filter by action and by market (markets are resolved through the thread's investor). The queue is
**not time-gated**: WF-18's fixed 07:00 review time was rejected; act whenever you like.

### Approve, edit-and-approve, reject

The buttons post to `POST /review/{message_id}/approve` (optional form field `edited_body`) and
`POST /review/{message_id}/reject` (form field `reason`). The route does, in this order:

1. Resolves the Temporal workflow that is parked waiting for you:
   - a sequence step: `sequences.temporal_workflow_id` via `messages.sequence_id`;
   - an inbound-reply draft (no sequence): `messages.meta["temporal_workflow_id"]` if the A4 path
     stored it there, otherwise the `audit_events` row with `entity_type = 'message'`,
     `entity_id = <message id>`, `event_type = 'inbound.workflow_started'` and
     `payload.workflow_id` (the A4 builder writes that event when it starts the workflow).
2. Sends the signal `human_decision` with payload

   ```python
   HumanDecision(approved: bool, edited_body: str | None, step: int | None, message_id: str,
                 actor_id: str | None)   # actor_id = operator username, optional
   ```

   (`wholesaling/db/repositories/review.py`). Temporal serialises it as a JSON object keyed by
   field name, so the workflow may declare its own dataclass with the same names; unknown fields
   are ignored by the SDK converter.
3. Only if the signal succeeded: flips `policy_decision` to `send` (approve) or `blocked`
   (reject), stores the edited body on the row so the record matches what will be sent, records
   `meta.human_decision` (operator, reason, time) and appends an audit event with `actor = human`
   and your username: `review.approved`, `review.approved_with_edits` or `review.rejected`.

The workflow owns the actual send (RUL-05 idempotent send, `GRAPH_ALLOW_LIVE_SEND` guard) and
clears `draft` / sets `sent_at` itself. Approving does not send anything from the API process.

Failure modes:

| Situation | Response | Row |
|---|---|---|
| Temporal signal raises | **502**, error shown in place of the card | unchanged, still queued; retry |
| no resolvable workflow | **422** | unchanged |
| message already decided / not queued | **409** | unchanged |
| unknown message | **404** | - |

An unchanged or blank textarea is not an edit: the row stays `review.approved`, so edit-rate math
(below) is not inflated by opening the editor.

## The policy page

Shows all twelve actions from `policy.actions.POLICY_TABLE` with the live level from the
`policies` table (missing row = L0, POL-02), the behaviour at that level, the daily cap and the QA
confidence threshold (default 0.85, OPN-10). `POST /policies/{action}` with form fields `level`,
`daily_cap`, `threshold`, `note` changes a row; blank fields are left alone.

- A level change is a **human action** (POL-01): it is recorded with `actor = human`, your
  username, and the before/after values as `policy.changed` in `audit_events`.
- **Hard floors** (`contract_execution`, `earnest_money`) cannot leave L0. The repository refuses
  (`HardFloorViolation`), the page answers **400** with the reason, nothing is written or audited.
  Their level selector is disabled; a note at L0 is still allowed.
- Demotion is allowed and audited the same way.

**Promotion readiness** (POL-04) is computed per action over the last 30 days and shown
read-only; nothing promotes or demotes itself in this build:

| Criterion | Source |
|---|---|
| ≥ 50 human-approved drafts | `review.approved` + `review.approved_with_edits` events joined to the message's `policy_action` |
| ≤ 5 % needed edits | share of those that were `review.approved_with_edits` |
| zero QA-blocked drafts sent | messages of the action with `sent_at` in the window whose QA verdict is `block` |
| zero compliance flags | QA flags containing `compliance`, `prohibited`, `opt_out`/`opt-out` or `legal` |
| not a hard floor | always false for the two hard floors |

A POL-05 *demotion signal* (7-day edit rate > 15 %, or any compliance flag) is shown alongside.

## The daily digest

`DailyDigestWorkflow` (`wholesaling/workflows/daily_digest.py`) runs once a day from the
`daily-digest` entry in `workflows/schedules.py`. It fires at `DIGEST_HOUR_LOCAL` (default 7) in
`OPERATOR_TIMEZONE` (default `America/Chicago`); `digest_calendar_spec(settings)` gives the ops
command the calendar spec. The digest summarises the local day so far, the rolling seven days
ending that day, the live queue depth and oldest waiting age, and promotion readiness.

KPIs (BO-21), computed in `wholesaling/reporting/kpis.py`:

| KPI | Definition |
|---|---|
| discovered contacts | `contacts` created in the window |
| messages sent | outbound `messages` with `sent_at` in the window |
| responses / qualified responses / opt-outs | inbound messages received in the window; qualified = classified `interested` or `buy_box_supplied` |
| qualified buy boxes | `buy_boxes` created in the window that are current and active |
| matching properties | `matches` created in the window |
| reviewed deals | `matches` with `reviewed_at` in the window |
| contracts | **proxy**: `offers` that reached `accepted` in the window (no contract table; contract execution is a hard floor) |
| assignment fees | sum of `offers.assignment_fee` over those |
| cost per qualified buyer | `agent_runs.cost_usd` in the window / qualified buy boxes |
| queue depth, oldest age | live |

There is deliberately no open-rate or click metric (BO-21: optimise on verified qualified
responses and completed deals).

Delivery goes through `DigestSink`s (`wholesaling/reporting/digest.py`):

- `LogSink` always: one structured `digest.sent` log event with the KPI payload.
- `SlackWebhookSink` when `SLACK_WEBHOOK_URL` is set (INT-18 optional webhook): header + text block.
- **Email is deferred to live mode.** RUL-06 forbids live mail in this build and the Graph send
  path is guarded by `GRAPH_ALLOW_LIVE_SEND`. When live mode is cleared, add an `EmailSink` over the
  Graph gateway to `build_sinks` with `ALERT_EMAIL_TO` as recipient; nothing else changes.

A failing sink is reported in the activity result (`failed_sinks`) and the `digest.sent` audit
event; it never fails the run. The activity is idempotent per (day, workflow id), so a Temporal
retry does not send the digest twice.

Run it by hand: `GET /digest?day=2026-10-20` shows what that day's digest contains;
`GET /digest.txt` is the exact text the sinks receive.

## Files

- `wholesaling/api/auth.py`, `wholesaling/api/deps.py`
- `wholesaling/api/routes/review.py`, `policies.py`, `digest.py`
- `wholesaling/api/templates/`, `wholesaling/api/static/` (`style.css`, vendored `htmx.min.js` 2.0.4,
  sha384 `1c67f3b687e8b5fb21705efef27e382502f6a099a8c150a13d3838f12fa3bd9a33b4f7efc03efd382fcb4ed1ba74ca7e`)
- `wholesaling/db/repositories/review.py`
- `wholesaling/reporting/kpis.py`, `wholesaling/reporting/digest.py`
- `wholesaling/activities/digest.py`, `wholesaling/workflows/daily_digest.py`
