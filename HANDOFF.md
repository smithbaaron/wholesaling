# HANDOFF.md

Written 2026-10-25 at the user's request to stop all work and hand off to a cloud session.
Everything below is the true state on disk at that moment. Nothing is deployed, no integration is
live, no credentials exist.

## 1. State in one paragraph

REQUIREMENTS.md is complete and approved by the user, including the section 20 engineering choices.
Build order step 1 (repo, config, requirements matrix, lint/type/test toolchain) is done except for
CI and a hosted Git remote. Steps 2 and 3 were briefed to three parallel sub-agents (database,
runtime, deployment) and those agents were stopped before they wrote any file, so steps 2 and 3
have no code on disk. One git commit exists (skeleton) plus the handoff commit. All 16 unit tests,
ruff, and mypy --strict pass.

## 2. Every file created or modified, and its purpose

Source documents (unchanged inputs, now tracked in git):

| File | Purpose |
|---|---|
| `Wholesale_Automation_Master_Spec_v1.docx` | Product intent (v1). |
| `Claude_Code_Wholesaling_Build_Guide_v1.docx` | Original technical plan (v1). |
| `Wholesaling_Automation_Architecture_v2.docx` | Current architecture; supersedes v1. Copied from the user's Downloads folder. |
| `Wholesaling Automation Architecture v2.pdf` | Same content as the .docx (PDF export). Not needed; kept because it was in the folder. |

Files I created:

| File | Purpose |
|---|---|
| `REQUIREMENTS.md` | The build contract. ~230 rows: every requirement from the three documents with CONFIRMED / PROPOSED / REJECTED / OPEN, source, and reason for every v1 item v2 overrides. Sections 20 (engineering choices) and 21 (toolchain) were approved by the user. |
| `HANDOFF.md` | This file. |
| `README.md` | Entry point: status, quick start, unchanging rules. |
| `pyproject.toml` | Package metadata; deps (fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, aiosqlite, alembic, pydantic, pydantic-settings, temporalio, pyyaml, httpx, jinja2, python-multipart, structlog); dev group (pytest, pytest-asyncio, pytest-cov, ruff, mypy, types-PyYAML, greenlet); scripts `wholesaling-api` and `wholesaling-worker` (entry modules NOT yet written); ruff, mypy --strict, pytest config with markers `postgres` and `temporal`. |
| `uv.lock` | Lockfile produced by `uv sync` (54 packages). |
| `.python-version` | `3.12`. |
| `.gitignore` | Excludes .env, *.db, var/, .venv, caches. |
| `.env.example` | Every settings key, grouped by human-action checkpoint, with every `*_MODE` defaulting to mock. A unit test enforces parity with `Settings`. |
| `config/README.md` | Explains the YAML tunables and which REQUIREMENTS rows they implement. |
| `config/deal_band.yaml` | Global $75k–$350k purchase band, ARV $120k–$500k (M1-06). |
| `config/m1.yaml` | M1 weights (0.30/0.25/0.20/0.10/0.10/−0.05), active cap 5, Watch→Active at 10 buy boxes, legal-friction placeholder (TX: 1), OPN-08 seed market list (Houston + 20 metros). |
| `config/sequences.yaml` | Follow-up intervals 4/4/4 investor, 3/7/14 seller; negotiation 2/5/10, expire 14, re-engage 60/90, requeue 90. |
| `config/a7.yaml` | Fee $10k, market_pct 0.70, room 7%, closing 3% (unvalidated), round-down $500, cash close 21 d (unvalidated), min offer 35% ARV, asking tolerance 25%, repair $/sqft placeholders (15/30/50/75, unvalidated), Houston foundation $12k (unvalidated), comp tiers. |
| `config/caps.yaml` | L1 daily caps 50 investor / 200 seller email; QA auto-reply confidence 0.85 (proposed). |
| `docs/adr/0001-repo-layout-and-toolchain.md` | Accepted ADR: single package layout, uv/ruff/mypy/pytest, SQLAlchemy+Alembic, Jinja2+HTMX. |
| `docs/adr/0002-portable-schema.md` | Accepted ADR: Postgres in prod/CI, SQLite in-memory for service-free tests, portable column types, no ON CONFLICT / partial indexes in core schema, Alembic batch mode. |
| `wholesaling/__init__.py` | Package docstring describing subpackages; `__version__ = "0.1.0"`. |
| `wholesaling/config/__init__.py` | Exports `Settings`, `get_settings`. |
| `wholesaling/config/settings.py` | pydantic-settings model for every env var; `SMS_MODE=live` raises (RUL-06); `validate_for_production()` flags SQLite and missing Temporal Cloud TLS/API key. |
| `wholesaling/config/tunables.py` | Pydantic models + loader for `config/*.yaml` (DealBand, M1Config, SequenceConfig, A7Config, CapsConfig); falls back to model defaults when a file is missing; rejects non-mapping YAML. |
| `wholesaling/db/__init__.py` | Exports Base, mixins, engine/session factories. |
| `wholesaling/db/base.py` | `Base` with naming convention; `UUIDPrimaryKeyMixin`; `TimestampMixin` (tz-aware created/updated). |
| `wholesaling/db/types.py` | `GUID` (native UUID on Postgres, CHAR(36) elsewhere), `JSONDoc` (JSONB/JSON), `Money = Numeric(12,2)`, `Ratio = Numeric(6,4)`. |
| `wholesaling/db/engine.py` | `make_engine` (SQLite FK pragma), `make_session_factory`, cached `get_engine`/`get_session_factory`. |
| `wholesaling/policy/__init__.py` | Exports the policy contract. |
| `wholesaling/policy/actions.py` | `PolicyAction` (12 actions), `AutonomyLevel` (L0/L1/L2), `Behaviour` (draft/auto/auto_if/human/off), `POLICY_TABLE` transcribed verbatim from v2 with condition text, `HARD_FLOOR_ACTIONS`, `default_level()` = L0. The policy ENGINE (step 7) is not written. |
| `wholesaling/integrations/__init__.py` | Docstring: every integration = `interface.py` + `mock.py` + `real.py`, selected by `*_MODE`. |
| `wholesaling/integrations/base.py` | `HealthStatus` dataclass and `Integration` Protocol with `healthcheck()`. |
| `wholesaling/api/__init__.py`, `wholesaling/workflows/__init__.py`, `wholesaling/activities/__init__.py`, `wholesaling/workers/__init__.py`, `wholesaling/agents/__init__.py` | Package markers with docstrings only. No code. |
| `tests/conftest.py` | Fixtures: `settings` (APP_ENV=test), `engine` (SQLite in-memory by default, `TEST_DATABASE_URL` for Postgres; imports `wholesaling.db.models` which DOES NOT EXIST yet, so the `engine`/`session` fixtures will fail until the models package is written), `session_factory`, `session`; auto-skips `postgres`-marked tests. |
| `tests/unit/test_settings.py` | Mock-by-default, SMS live rejected, production validation, `.env.example` ↔ `Settings` parity. |
| `tests/unit/test_tunables.py` | YAML files exist, v2 defaults load, weights sum 0.95, fallback and bad-YAML behaviour. |
| `tests/unit/test_policy_table.py` | Policy table matches v2 row for row; L0 never sends except opt-out suppression; hard floors human at every level; default L0. |

Files outside the project:

| File | Purpose |
|---|---|
| `C:\Users\User\.claude\projects\C--Users-User-Desktop-wholesaling\memory\wholesaling-project-context.md` (+ `MEMORY.md` index) | Claude Code memory note: three source docs, v2 precedence, REQUIREMENTS.md is the contract, machine lacked Python/Git/Docker. Not part of the repo. |

Toolchain installed on this machine via winget (user approved): Python 3.12.10 (user scope), uv 0.13.5, Git 2.56.0, Temporal CLI 1.9.1. Not installed: Docker, Railway CLI (not on winget), Node, Postgres.

Git: initialised with `main`, `core.autocrlf=input`, user.name "Aaron Smith", user.email smithbaaron@gmail.com (taken from the session; change if wrong). No remote.

## 3. Build order status (v2 Build order; REQUIREMENTS.md BO-xx)

| Step | Status | Detail |
|---|---|---|
| BO-01 Repo, CI, .env.example, requirements matrix | IN PROGRESS (≈80%) | Done: repo, package skeleton, pyproject, lockfile, .env.example, REQUIREMENTS.md, ADRs, unit tests, ruff/mypy green. Not done: `.github/workflows/ci.yml`, hosted remote (ENG-Q12: private GitHub repo, user to create), `SETUP.md`, `docs/CHECKPOINTS.md`, `docs/RUNBOOK.md`, `Dockerfile`, `railway.toml`, `.gitattributes`, `.dockerignore`. |
| BO-02 Postgres schema and migrations | NOT STARTED | Briefed to the database owner; agent stopped before writing. Target: `wholesaling/db/models/` (20 tables: the 18 in v2 plus `search_profiles` DM-05 and `source_evidence` DM-06), `wholesaling/db/normalize.py`, `wholesaling/db/repositories/{investors,buy_boxes,policies,audit}.py`, `alembic.ini`, `wholesaling/db/migrations/{env.py,script.py.mako,versions/0001_initial_schema.py}` (seeds 12 policy rows at L0), `tests/db/{test_migrations,test_buy_box_scenarios,test_investor_dedupe,test_policies}.py`. |
| BO-03 Temporal worker skeleton, hello workflow, Railway deploy, health endpoint | NOT STARTED | Briefed to the runtime owner; agent stopped before writing. Target: `wholesaling/logging.py`, `wholesaling/integrations/temporal/{interface,client,mock}.py`, `wholesaling/activities/hello.py`, `wholesaling/workflows/hello.py` (run + pause/resume signals + status query), `wholesaling/workers/main.py` (`run()`, `build_worker()`, WORKFLOWS/ACTIVITIES registries), `wholesaling/api/main.py` (`create_app()`, `run()`), `wholesaling/api/routes/health.py` (`/health` liveness, `/ready` DB SELECT 1 + Temporal check, 503 on DB failure), `tests/api/test_health.py`, `tests/workflows/test_hello_workflow.py` (time-skipping test env, marked `temporal`), `tests/workers/test_registry.py`. Railway deploy is blocked on checkpoints PRE-07 (Temporal Cloud) and PRE-08 (Railway). |
| BO-04 Graph integration | NOT STARTED | |
| BO-05 Model provider abstraction (`agents/llm.py`) | NOT STARTED | Load the `claude-api` skill before writing it (model IDs, pricing for cost logging). |
| BO-06 M1, A1, A2 with mock data provider | NOT STARTED | |
| BO-07 A3 InvestorSequence; A4, A5; policy engine; review queue; daily digest | NOT STARTED | |
| BO-08 Sheets sync; Zapier path behind flag | NOT STARTED | |
| BO-09 Synthetic pilot | NOT STARTED | |
| BO-10 … BO-14 Seller side, A7, D1/D2, DocuSign, eval harness | NOT STARTED | |

## 4. Decisions I made that are not in the v2 document

All of these are recorded in REQUIREMENTS.md section 20 (ENG-Q1..Q14) and were approved by the
user in one message ("REQUIREMENTS.md and the section 20 recommendations are approved"). Listed
here so the next session does not re-decide them:

1. Single package `wholesaling/` with subpackages `config, db, api, workflows, activities, workers, integrations, agents, policy` (ENG-Q1; ADR 0001).
2. `uv` + `pyproject.toml` + `uv.lock`; `ruff` + `mypy --strict`; `pytest` + `pytest-asyncio` (ENG-Q2..Q4).
3. SQLAlchemy 2 async + asyncpg + Alembic; money as NUMERIC(12,2); all timestamps timestamptz UTC; per-market IANA timezone (ENG-Q5).
4. SQLite in-memory for service-free tests, Postgres in CI/prod, portable column types, no dialect-specific SQL in the core schema, Alembic `render_as_batch` (ADR 0002). This was my call because the machine has no Docker/Postgres; it is a reversible test strategy, not a product change.
5. YAML tunables under `config/` validated by pydantic at startup; `policies` stays a DB table (ENG-Q6).
6. Docker Compose for local services recommended but Docker is not installed; Temporal CLI `temporal server start-dev` is the working local alternative (ENG-Q7).
7. Model routing config keys `model_classify/extract/draft/qa/comp_audit` defaulting to Anthropic Haiku for classify/extract and Sonnet for the rest; IDs in `.env.example` are `claude-haiku-5-5` / `claude-sonnet-5-5` and must be verified against the `claude-api` skill before step 5 (ENG-Q8).
8. Seller photo storage: `BlobStorage` interface, local dir for pilot, S3-compatible later (ENG-Q9).
9. A7 ARV weighting: linear recency × distance decay to 0.5 at tier limits, 15% trim each end, weighted mean (ENG-Q10).
10. A7 verification ordering: V1 after step 1, V3 then V2 after step 3, V4 after step 4, V5 after step 6, gates at 7, QA at 8, V6 immediately before send with gates re-run (ENG-Q11).
11. Private GitHub repo for CI and Railway deploys; user creates it (ENG-Q12, still OPEN: no remote exists).
12. Server-rendered FastAPI + Jinja2 + HTMX for review queue and web form (ENG-Q13).
13. Multi-agent dev workflow: parallel owners on disjoint files, integrated and reviewed by the lead (ENG-Q14).
14. `search_profiles` and `source_evidence` tables added beyond v2's list (DM-05, DM-06, PROPOSED; user approved the file as a whole).
15. v2's 07:00 "review due outreach" schedule from v1 is REJECTED (WF-18); the review queue and daily digest replace it.
16. Seven numeric defaults v2 does not give, all placeholders flagged UNVALIDATED in `config/*.yaml` and OPEN in REQUIREMENTS.md section 18: closing cost 3%, cash close 21 d, repair $/sqft 15/30/50/75, Houston foundation $12k, QA auto-reply confidence 0.85, market-drop threshold (<3 buy boxes and 0 offers in 90 d), market hard cap = deal band upper bound.
17. `SMS_MODE=live` is rejected at settings-validation time, not just by policy, so RUL-06 cannot be bypassed by config in this build.
18. Git `core.autocrlf=input` so the Windows working copy keeps LF.

## 5. Open questions

1. ENG-Q12: which Git host and repo name? Nothing is pushed; CI cannot run until a remote exists.
2. REQUIREMENTS.md section 18 (OPN-01..16): every v2 open decision is running on its default. The seven placeholders in item 16 above need real numbers before any offer is ever computed (seller phase), but not before steps 2 to 3.
3. Model IDs (ENG-Q8): confirm the exact Anthropic model IDs and prices when writing `agents/llm.py`.
4. Docker: install Docker Desktop (needs admin; the user account is not admin) or accept Temporal CLI + a hosted/CI Postgres as the local story. Current plan assumes the latter.
5. Railway CLI: not on winget; install via Railway's official installer when the Railway checkpoint (PRE-08) is cleared, with user approval for the download.
6. Git identity in `.git/config` was set to the session's account email; confirm or change.
7. The `.pdf` copy of v2 is redundant with the `.docx`; delete or keep?

## 6. Exact next action I would have taken

Wait for the three owner agents to report, then: run `.\.venv\Scripts\python.exe -m ruff check --fix .; ruff format .; mypy; pytest` on the integrated tree, review each owner's diff against REQUIREMENTS.md (DM-01..11 for the schema, WF-01/WF-22/BO-03 for the runtime, RUL-01/RUL-09/PRE-xx for the deployment docs), fix what the review turns up, commit each owner's work as its own commit, and then write the step 1–3 milestone report (files implemented, tests passing, blocked items: PRE-07 Temporal Cloud, PRE-08 Railway, ENG-Q12 Git remote; next steps: BO-04 Graph integration behind interface + mock, then BO-05 model layer).

Concretely for the cloud session: re-issue the three briefs from section 3 (the targets listed per step are the full deliverable lists), starting with the database owner because `tests/conftest.py` already imports `wholesaling.db.models`.

## 7. How to verify this handoff

```
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest tests/unit -q      # 16 passed as of this handoff
```

`uv run pytest` (whole tree) will error at collection of any test using the `engine` fixture until
`wholesaling/db/models` exists; no such tests exist yet, so the full run also passes 16.
