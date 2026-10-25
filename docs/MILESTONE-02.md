# Milestone report — Build order steps 4 to 6

Date: 2026-10-25. Reported per REQUIREMENTS.md RUL-08. CI green on `main` (run 40243123203).

## What was implemented

| Step | Status | Delivered |
|---|---|---|
| BO-04 Microsoft Graph integration | DONE (mock + live code; live untested until PRE-05) | `integrations/graph/` interface, mock, live (client-credentials OAuth, retry/backoff, circuit breaker), webhook route with validation handshake, clientState check, dedupe and 202 fast-ack; delta poll reconciliation; subscription renewal workflow (48 h cadence, alert after 2 failures); inbound message upsert with one row per message (INV-24); bounce → suppression (INT-25); draft and send as separate operations with idempotent sends (INT-13, RUL-05); `GRAPH_ALLOW_LIVE_SEND=false` code guard (RUL-06); `workflows/schedules.py` registry for Temporal Schedules; docs/integrations/graph.md with Entra setup |
| BO-05 Model layer | DONE | `agents/llm/`: provider protocol, mock provider (schema-filling, scriptable), Anthropic provider (structured output via forced tool, retries, breaker), router (Haiku classify/extract, Sonnet draft/QA/comp-audit, per-agent overrides), pricing table, `AgentRunner` persisting every run to `agent_runs` with prompt hash/tokens/cost (XC-02); `BaseAgent` + `PromptTemplate` that refuses credential-like values (XC-06); docs/agents/llm.md |
| BO-06 M1, A1, A2 with mock data provider | DONE | `integrations/data/` provider interfaces + deterministic mock with committed fixtures (418 deeds, 21 markets); `agents/scoring.py` (M1-02/03, M1-04 tiers with crossed-twice, M1-06 deal band, INV-02..05 qualification/score/type/exclusions); entity resolution (INV-06); M1 report never auto-promotes to Active (M1-11); A1 idempotent discovery with evidence; A2 contact research with provenance; discovery activities with per-market lock (WF-09); docs/agents/discovery.md |
| Follow-up | DONE | Migration 0002: `agent_runs.cost_usd` NUMERIC(12,6) so sub-cent model calls are not recorded as $0.00 |

## Tests passing

- Local SQLite: 466 passed, 7 skipped · Local Postgres 16: 467 passed, 6 skipped
- CI: all three jobs green (SQLite + coverage, Postgres, Docker build); Temporal time-skipping workflow tests run in CI
- ruff, ruff format, mypy --strict: clean (78 source files)

## Facts verified against vendor docs (URLs in the respective docs/ pages)

- Graph mail subscriptions last up to 7 days (not ~3); renewal set to every 48 h with 70 h expiry.
- Microsoft now recommends RBAC for Applications over application access policies to scope an app to one mailbox.
- Model IDs `claude-haiku-5-5` / `claude-sonnet-5-5` confirmed; Haiku $0.10/$0.50, Sonnet $2/$10 per MTok (Haiku long-context tier from a secondary source, marked as such).

## Mock run numbers (for the synthetic pilot baseline)

M1 first run: 21 markets → 0 Active, 20 Watch, 1 Excluded (data gate); Houston ranks #1.
A1 on Houston mock: 47 deeds → 22 investors found, 7 qualified, 3 priority (≥75), 6 excluded with reasons, 38 evidence rows, rerun creates 0 duplicates.

## Blocked items (human action) — unchanged

PRE-07 Temporal Cloud and PRE-08 Railway (deploy); PRE-05 Entra app registration (first live Graph test); PRE-04 sending domain warmup (start now); PRE-01/02 LLC and address (any live send). See docs/CHECKPOINTS.md.

## Decisions made this milestone (beyond v2)

1. Anthropic SDK 2.x drops `temperature`; the field is kept on `ModelRequest` for other providers and ignored by the Anthropic provider.
2. Error runs are committed immediately so failures are logged even if the caller rolls back.
3. Replies use Graph `createReply` with a comment so threading is preserved; conversation-only drafts reply to the latest message in the conversation.
4. Webhook fails closed without `GRAPH_WEBHOOK_CLIENT_STATE`; returns 503 if nothing could be queued so Graph retries.
5. States without a legal-friction config entry can never be promoted to Active (`legal_review_pending`), only TX is configured.
6. INV-05 exclusions are persisted with `excluded_reason` and no score, for auditability.
7. Open: §5 lists A4/S4 as Sonnet while routing is by task kind (classify → Haiku). BO-07 should set a per-agent override for A4 classify if Sonnet is wanted there.

## Exact next steps

1. BO-07: A3 `InvestorSequence` workflow (initial + 3 follow-ups in one thread, cancel on reply/opt-out/pause), A4 reply classification and buy-box extraction (versioned upsert), A5 QA gate, policy engine (send vs. queue_for_human from the `policies` table), review queue page (approve/edit/reject), daily digest. Golden test set per INV-22.
2. BO-08: Sheets one-way sync; Zapier path behind flag.
3. BO-09: synthetic pilot (10–20 fake investors through the whole loop in mock mode).
4. Deploy + RUNBOOK restart test as soon as PRE-07/08 are done.
