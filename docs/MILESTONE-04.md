# Milestone report — Build order steps 8 and 9 (investor side complete in mock mode)

Date: 2026-10-26. Reported per REQUIREMENTS.md RUL-08. CI green on `main`.

## What was implemented

| Step | Delivered |
|---|---|
| BO-08 Sheets sync | One-way Postgres → Google Sheets projection of 10 tabs (Investors, Contacts, Buy Boxes incl. every version with status, Search Profiles, Properties, Matches, Conversations, Suppressions, QA Log, Outreach Events) with stable ids in the first columns; mock + live gateway (service-account JWT, batched upserts, retry, breaker; no new dependency — `google-auth` is the single optional extra for live mode); 15-min sync workflow; daily reconciliation (Outlook vs Postgres vs Sheets) that reports mismatches and never writes (XC-09) |
| BO-08 Zapier | A6 parser-email formatter (stable labels, `ROW_KEY = investor_id|buy_box_key` so corrections hit the same sheet row), send path through the guarded Graph draft/send, behind `ZAPIER_PARSER_ENABLED=false` (RUL-07); Zap steps and acceptance documented |
| BO-09 Synthetic pilot | 16 scripted investor personas run through the entire loop in mock mode with a server-free simulator (and an optional real-Temporal driver): discovery → sequence start → 4-touch outreach → scripted replies (interested, partial then correction, two buy boxes in one email, question, not-now with date, not interested, opt-out, out-of-office then real reply, sarcasm, bounce, quoted text, suppressed, price talk, L1 auto-send) → classification → buy-box versions → reply drafts → QA → policy → review-queue approvals via the real route. Report with 11 invariants and POL-04 readiness; `wholesaling-pilot run`; `/pilot/report` |
| BO-09 follow-up | The pilot found 7 gaps; 6 fixed (one is by design): shared signal payloads between the inbound pipeline and sequence workflow; out-of-office never cancels a sequence; a production sequence starter (priority-first, daily cap, skips suppressed/active) with a daily kickoff schedule; A5 QA on every outreach draft (INV-17); inbound workflow ids resolvable from the review queue; "qualified but unreachable" audited and reported. A5's cadence and repeated-request pre-checks are skipped for scheduled sequence drafts (the workflow owns cadence) |

## Pilot result (L0 and L1)

16/16 scenarios pass at both levels, zero signal-contract errors, all invariants hold: every touch in one Outlook conversation, one inbound row per injected message even with duplicate webhook delivery plus delta replay, 29 QA reviews, at L1 19/19 QA-approved sends go out through the real policy path and the single price-talk reply queues for a human.

## Tests

CI: 1,120 passed (SQLite + Postgres + 23 Temporal end-to-end workflow tests); ruff, ruff format, mypy --strict clean on 129 source files.

## Definition of done — investor side (BO-20)

Met in mock mode: code, config templates, deployment infra, tests, runbook, operator dashboard exist; all investor agents (M1, A1–A6) pass end-to-end tests with synthetic data. Not met: live authorized integrations and the deployed restart test — both wait on human checkpoints.

## Blocked on human action — deploy is now the next step

| Checkpoint | Needed for |
|---|---|
| PRE-07 Temporal Cloud namespace + API key | Deploy, RUNBOOK restart test (RUL-09) |
| PRE-08 Railway project | Deploy |
| PRE-05 Entra app registration + M365 mailbox on the sending domain | First live Graph test |
| PRE-04 sending domain + warmup | Investor L0 live |
| PRE-01/02 LLC + postal address | CAN-SPAM footer (A3 refuses to render without it) |
| PRE-16 approve follow-up copy + send windows | Investor L0 live |

## Exact next steps

1. Deploy to Railway with Temporal Cloud; run the RUNBOOK restart test; then the 24/7 claim is allowed.
2. Seller side: BO-10 (S1 lists, S2 skip trace, S3 sequences, web form with photos, S4 intake), BO-11 (A7 with six verification passes, S5 negotiation, D1/D2), BO-12 (DocuSign mocks, S6 disposition, deadline alerts), BO-13 eval harness, BO-14 seller pilot.
