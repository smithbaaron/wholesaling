# Milestone report — Build order step 7

Date: 2026-10-25. Reported per REQUIREMENTS.md RUL-08. CI green on `main`.

## What was implemented

| Part | Delivered |
|---|---|
| Policy engine (`policy/engine.py`) | `send` / `queue_for_human` / `blocked` from the live `policies` table and the v2 table's per-row conditions (QA approval, daily caps, QA confidence threshold, sensitive content, A7 gates, negotiation room, consent). Conservative on unknowns: a missing fact queues, never sends. Hard floors human at every level; suppression and QA block win at every level. |
| A3 outreach + `InvestorSequenceWorkflow` | CPY-01 initial body locked verbatim (`{first_name}`, `{market}`); three follow-up drafts marked `pending_approval` (PRE-16); XC-07 denylist on rendered text; CAN-SPAM footer requires a postal address; 4 touches in one Outlook conversation; `reply` / `opt_out` / `bounce` cancel the rest immediately, `pause` / `resume` hold; `human_decision` signal from the review queue; business-hour send window per market timezone; daily caps counted in market-local day; idempotent step keys so replays never double-send |
| A4 reply agent | 8-class classification (INV-13) on Sonnet, quoted-text stripping, auto-reply detection, buy-box extraction with explicit unknowns and correction history (never invents criteria), reply drafting that asks only for missing fields; reconnect date capture for not-now |
| A5 QA agent | Deterministic pre-checks before any model call (prohibited claims, opt-out violation, wrong recipient, repeated request, 24 h cadence) then model review → approve / block / rewrite with confidence, flags, rationale; persisted in `qa_reviews` |
| Inbound pipeline (`GraphInboundMessageWorkflow`) | fetch → classify → opt-out suppression (auto at every level) → buy-box versioned upsert → draft → QA → policy → send in-thread or queue → wait for `human_decision` (bounded 14 days). One inbound message = one inbound row + one audit event even when delivered twice |
| Review queue + policies page | HTMX page: approve / edit-and-approve / reject with QA verdict, flags, rationale, policy reason; signals the owning workflow; row flips only after the signal succeeds. Policies page: level/cap/threshold per action with audit trail, hard floors rejected, POL-04 promotion-readiness shown read-only (no auto-promotion in this build) |
| Daily digest + KPIs | BO-21 KPIs (contacts, sends, responses, qualified buy boxes, matches, reviewed deals, contracts, fees, cost per qualified buyer, queue depth/age); log sink always, Slack webhook when configured; email deferred to live mode |
| Golden set | 28 inbound examples (sarcasm, corrections, two buy boxes in one email, quoted text, out-of-office, opt-out variants) — INV-22 — as the BO-13 eval harness input |

## Tests

- Local SQLite: 872 passed · local Postgres 16: 873 passed · CI: 890 passed (includes 19 Temporal end-to-end workflow tests that only run where the test server can be downloaded)
- ruff, ruff format, mypy --strict clean (104 source files)

## What the operator can do now (all in mock mode)

Run API + worker locally per SETUP.md, inject a synthetic inbound email through the mock Graph gateway, watch it classified and a reply drafted, see it in `/review`, approve it, and see the mock "send" recorded in the same conversation. Nothing reaches a real mailbox: `GRAPH_ALLOW_LIVE_SEND=false` and every action is at L0.

## Blocked items (human action) — unchanged

PRE-07 Temporal Cloud, PRE-08 Railway (deploy); PRE-05 Entra app (first live Graph test); PRE-04 sending domain warmup; PRE-01/02 LLC + address (the CAN-SPAM footer refuses to render without a postal address); PRE-16 approve follow-up copy and send windows.

## Decisions made this milestone (beyond v2)

1. Follow-ups reply into the conversation (Graph `createReply` on the latest message) rather than onto the step-0 item specifically; Outlook threads on conversation id.
2. A human reject cancels the whole sequence, not just the step.
3. Edited bodies keep the CAN-SPAM footer; originals kept in `meta.original_body`.
4. Auto-replies (out-of-office) classify as `ambiguous` with `is_auto_reply=true`, never cancel a sequence, never get a reply.
5. Blocked drafts never reach Outlook; a local row carries the QA review for audit.
6. A4 classify is routed to Sonnet via a per-agent override (§5 vs. task-kind routing).
7. Default send window 09:00–17:00 market-local, weekdays; defaults for `SENDER_NAME`/`SENDER_COMPANY` are Aaron / NorthStack — change in `.env` if the venture uses a different identity.
8. Review-queue auth is single-operator HTTP Basic with a PBKDF2 hash (no new dependency).

## Exact next steps

1. BO-08: Sheets one-way sync (Investors, Contacts, Buy Boxes, Search Profiles, Properties, Matches, Conversations, Suppressions, QA Log tabs) every 15 min; Zapier parser path behind the flag.
2. BO-09: synthetic pilot — 10–20 fake investors through M1 → A1 → A2 → A3 → inbound replies → A4/A5 → review queue, end to end in mock mode, with a pilot report.
3. Then the seller side (BO-10..12): S1–S6, A7 with the six verification passes, D1/D2, web form, DocuSign mocks.
4. Deploy + RUNBOOK restart test when PRE-07/08 are done.
