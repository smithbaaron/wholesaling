# Milestone report — Build order steps 1 to 3

Date: 2026-10-25. Reported per REQUIREMENTS.md RUL-08.
Repo: https://github.com/smithbaaron/wholesaling (private). CI: green on `main` (run 40240188923).

## What was implemented

| Step | Status | Delivered |
|---|---|---|
| BO-01 Repo, CI, `.env.example`, requirements matrix | DONE | REQUIREMENTS.md (approved), pyproject + uv.lock, `.env.example` (parity-tested), ADRs 0001/0002, GitHub Actions CI (3 jobs: SQLite suite + coverage, Postgres 16 suite, Docker build), Dockerfile (multi-stage, non-root, healthcheck), docker-compose (optional), Railway IaC (`.railway/railway.py`), SETUP.md, docs/CHECKPOINTS.md (PRE-01..17), docs/RUNBOOK.md (incl. the RUL-09 restart test procedure) |
| BO-02 Postgres schema and migrations | DONE | 20 tables (`wholesaling/db/models/`), Alembic migration 0001 seeding 12 policy rows at L0, normalizers (email/phone/entity/address), repositories (investors upsert-by-entity-key, buy-box versioning with replay no-op, policies with hard-floor enforcement, audit with idempotency), 100 DB tests incl. DM-04 scenarios; migration verified against real Postgres 16 in CI |
| BO-03 Temporal worker skeleton, hello workflow, health endpoint | DONE except deploy | Temporal gateway (live + mock), `HelloWorkflow` (durable sleep, pause/resume signals, status query — the template for every sequence workflow), worker entrypoint with graceful shutdown, FastAPI `/health` + `/ready`, structlog with secret redaction. 4 time-skipping workflow tests pass in CI. **Railway deploy not done** — blocked on human checkpoints PRE-07 and PRE-08 |

## Tests passing

- Local SQLite: 133 passed, 5 skipped (Postgres variant + 4 Temporal tests needing the test server binary)
- CI SQLite job: 137 passed, 1 skipped
- CI Postgres job: 134 passed, 4 skipped (same migration/metadata parity verified on Postgres)
- ruff, ruff format, mypy --strict: clean (45 source files)
- Docker image: builds in CI

## Blocked items (human action)

| Item | Blocks | Where |
|---|---|---|
| PRE-07 Temporal Cloud namespace + API key | BO-03 deploy, RUL-09 restart test | docs/CHECKPOINTS.md |
| PRE-08 Railway account + project | BO-03 deploy | docs/CHECKPOINTS.md |
| PRE-04 sending domain purchase + warmup | Investor L0 live (week 4) — start now, warmup takes 2–3 weeks | docs/CHECKPOINTS.md |
| PRE-01 LLC + PRE-02 business address | Any live send (CAN-SPAM footer) | docs/CHECKPOINTS.md |

Nothing else is blocked. Steps 4 through 9 can all be built behind mocks.

## Decisions made this milestone (beyond v2 / REQUIREMENTS §20)

1. Railway config: `railway.toml` is deprecated by Railway (stops being read 2026-12-01); replaced with Railway's infrastructure-as-code file `.railway/railway.py`. Documented in RUNBOOK.
2. `validate_for_production()` now requires `TEMPORAL_MODE=live` and accepts mTLS cert+key as an alternative to `TEMPORAL_API_KEY`.
3. `TEMPORAL_MODE` (mock|live) added to settings; default mock.
4. Buy-box uniqueness is `(investor_id, buy_box_key, version)`; duplicate structured criteria from a new message do not create a version.
5. `messages.qa_review_id` has no DB-level FK (cycle not portable to SQLite batch migrations); `qa_reviews.message_id` carries the relationship.
6. Email normalization folds dots / strips `+tag` only for Gmail-family and a few known providers, never for unknown domains.
7. CI posts the pytest log as a commit comment on failure (logs are otherwise unreadable from the build sandbox).
8. `HelloWorkflow.run` takes positional args; sequence workflows from BO-07 onward should take a single dataclass input (Temporal's recommendation) — decide before BO-07.

## Exact next steps

1. BO-04 Graph integration behind `GRAPH_MODE`: interface + mock + live (Entra app registration documented as PRE-05), webhook endpoint with validation handshake, delta-poll reconciliation, draft and send as separate operations, idempotency on `internetMessageId`.
2. BO-05 `agents/llm.py`: provider-agnostic model layer with per-agent routing (Haiku classify/extract, Sonnet draft/QA/comp audit), `agent_runs` logging (prompt hash, tokens, cost), mock provider for tests. Verify current model IDs and prices first.
3. BO-06 M1, A1, A2 with a mock data provider and the scoring rules from REQUIREMENTS §6–7.
4. When PRE-07/08 are done: Railway deploy and the RUNBOOK restart test, then the 24/7 claim is allowed.
