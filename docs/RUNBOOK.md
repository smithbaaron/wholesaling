# RUNBOOK — operating the deployed system

Audience: the operator (Aaron) and whoever is on the keyboard during a deploy. Development setup
is in [../SETUP.md](../SETUP.md); the human checkpoints that gate every live integration are in
[CHECKPOINTS.md](CHECKPOINTS.md).

Status at the time of writing: nothing is deployed. PRE-07 (Temporal Cloud) and PRE-08 (Railway)
are open, so every procedure below is written to be executed, not yet executed. The first run of
section 6 is the gate for any 24/7 claim (REQUIREMENTS.md RUL-09, WF-22).

## 1. Topology

| Component | Where | Entry | Public? | Config |
|---|---|---|---|---|
| `api` | Railway service, 1 replica | image CMD `wholesaling-api` (uvicorn on `PORT`=8000) | yes: `/health`, `/ready`, later `/webhooks/graph`, review queue, web form | `.railway/railway.py`, `SERVICE_ROLE=api` |
| `worker` | Railway service, 1 replica | `wholesaling-worker` | no domain, no port | `.railway/railway.py`, `SERVICE_ROLE=worker` |
| Postgres | Railway Postgres plugin `postgres` | — | private network only | `DATABASE_URL` assembled from the plugin's variables |
| Temporal | Temporal Cloud namespace | — | — | `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY`, `TEMPORAL_TLS=true`, `TEMPORAL_MODE=live` |
| Source | private GitHub repo (ENG-Q12, not yet created) | GitHub Actions CI | — | `.github/workflows/ci.yml` |

Both Railway services are built from the one `Dockerfile`. The only differences are the start
command and `SERVICE_ROLE`. Railway config is code: `.railway/railway.py` (Railway's Infrastructure
as Code; `railway.toml`/`railway.json` are deprecated and stop being read on 2026-12-01).

Two services, one task queue (`wholesaling-main`). The worker polls it; the API starts workflows
and sends signals on it. Adding a second worker replica later is a `replicas` change in
`.railway/railway.py`, nothing else.

## 2. Prerequisites on the operator machine

```bash
# Railway CLI (pick one)
npm i -g @railway/cli            # macOS, Linux, Windows
scoop install railway            # Windows
brew install railway             # macOS
bash <(curl -fsSL railway.com/install.sh)   # Linux/macOS shell installer

# The Python authoring helpers Railway's CLI evaluates .railway/railway.py with
pip install railway-sdk==0.7.0   # into the interpreter that is on PATH

# Temporal CLI: see SETUP.md section 1 (winget Temporal.TemporalCLI / brew install temporal)
```

```bash
railway login
cd <repo root>
railway link                     # pick project "wholesaling", environment "production"
```

Temporal CLI against the cloud namespace (put these in a shell profile on the operator machine
only; they are secrets):

```bash
export TEMPORAL_ADDRESS=<namespace>.<account>.tmprl.cloud:7233
export TEMPORAL_NAMESPACE=<namespace>.<account>
export TEMPORAL_API_KEY=<key>
# every `temporal workflow ...` command below then needs only `--tls`
```

## 3. Deploy

### 3.1 First deploy (once, after PRE-07 and PRE-08)

1. Set the secret variables on both services in the Railway dashboard (Variables tab) or:
   ```bash
   railway variable set --service api    TEMPORAL_ADDRESS=... TEMPORAL_NAMESPACE=... TEMPORAL_API_KEY=...
   railway variable set --service worker TEMPORAL_ADDRESS=... TEMPORAL_NAMESPACE=... TEMPORAL_API_KEY=...
   ```
   (The services may not exist before the first `config apply`; if `variable set` refuses, run
   step 2 first, expect the deploys to fail on `validate_for_production`, set the variables, then
   redeploy.)
2. Preview and apply the declared infrastructure:
   ```bash
   railway config plan            # read every line; values are redacted, add --show-values to see them
   railway config apply           # confirms, then creates postgres, api, worker
   ```
3. If `GITHUB_REPO` in `.railway/railway.py` is still `None` (ENG-Q12 open), push the working tree:
   ```bash
   railway up --service api
   railway up --service worker
   ```
   Once the GitHub repo exists and `GITHUB_REPO` is set, Railway builds from `main` on push and
   `railway up` is no longer needed.
4. Give `api` a public domain: dashboard > api > Settings > Networking > Generate Domain (target
   port 8000). Record it; `GRAPH_WEBHOOK_URL` (PRE-05) is `https://<that domain>/webhooks/graph`.
5. Verify:
   ```bash
   curl -fsS https://<api domain>/health     # {"status":"ok","version":"0.1.0"}
   curl -fsS https://<api domain>/ready      # {"status":"ok",...,"failing":[]}
   railway logs --service worker | head      # "worker.started" with task_queue=wholesaling-main
   temporal task-queue describe --task-queue wholesaling-main --tls   # one poller
   ```
6. Run the restart test (section 6). Record the result in section 10.

### 3.2 Routine deploy

With GitHub connected: merge to `main` after CI is green; Railway builds both services and swaps
`api` only after `/health` returns 2xx within `healthcheckTimeout` (120 s). The worker has no
healthcheck; its deploy swaps when the container starts. Without GitHub: `railway up --service api
&& railway up --service worker`.

Infrastructure changes (variables that are not secrets, replicas, restart policy, healthcheck):
edit `.railway/railway.py`, `railway config plan`, `railway config apply`.

Database migrations (from BO-02 onward): run them before the new code serves traffic, with
`preDeploy` on the `api` service in `.railway/railway.py` (`"preDeploy": "alembic upgrade head"`)
once `alembic.ini` lands, or by hand:
```bash
railway run --service api alembic upgrade head
```
Never run migrations from the worker; one writer.

### 3.3 What a deploy does to running workflows

Nothing is lost. Temporal holds every timer and pending activity; the old worker's `Worker.shutdown()`
finishes in-flight activities and returns tasks to the queue; the new worker picks them up. This
is the property section 6 proves. Workflow code changes still need Temporal's versioning
(`workflow.patched`) when a workflow definition changes while runs are open; that discipline starts
with the first long-running sequence (BO-07).

## 4. Rollback

Dashboard only (Railway has no CLI rollback to an arbitrary deployment):

1. Project > service (`api` or `worker`) > **Deployments**.
2. Find the last known-good deployment, three-dots menu > **Rollback**, confirm.
3. Repeat for the other service so both run the same code.
4. `curl /ready`, check `railway logs` for both.

Rollback restores that deployment's image **and its variables** in seconds, without a rebuild.
It is available only while Railway still has the image (Hobby 72 h, Pro 120 h). Older than that:
`git revert` the offending commit, push, and let it build; or `railway redeploy --service <name>
--yes` an older commit after checking it out locally and `railway up`.

Database migrations are not rolled back by a Railway rollback. If the bad deploy ran a migration,
run `alembic downgrade -1` through `railway run --service api` first, or roll forward.

## 5. Secrets

### 5.1 Where they live

Only in Railway variables (deployed) and `.env` (laptop). Never in the repo, the image, logs
(`wholesaling/logging.py` redacts any key containing SECRET, TOKEN, KEY, PASSWORD) or chat.
`.railway/railway.py` declares secrets as `preserve()` so `railway config apply` never overwrites
them and `railway config pull` never writes them into a file.

### 5.2 Rotate

| Secret | Where issued | Procedure |
|---|---|---|
| `TEMPORAL_API_KEY` | Temporal Cloud > Settings > API Keys | Create the new key first. `railway variable set --service api TEMPORAL_API_KEY=...` then the same for `worker`; Railway redeploys each. Check `/ready` and `worker.started` in logs. Delete the old key in Temporal Cloud. |
| Postgres password | Railway Postgres plugin | Rotate in the plugin; `DATABASE_URL` is a template over the plugin's variables so the services pick it up on redeploy (`railway redeploy --service api --yes`, then `worker`). |
| `GRAPH_CLIENT_SECRET` | Entra app > Certificates & secrets | Add the new secret, set it on `api` and `worker`, redeploy, delete the old one. Subscriptions keep working; the next renewal (WF-11) uses the new secret. |
| `GRAPH_WEBHOOK_CLIENT_STATE` | you generate it | Set the new value, redeploy `api`, then recreate the Graph subscription (BO-04 provides the command); notifications carrying the old state are rejected. |
| `ANTHROPIC_API_KEY` | Anthropic Console | New key, set on `worker` (agents run in activities) and `api` if it ever calls models, redeploy, revoke old. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` (file) | Google Cloud > service account > Keys | New key file, replace the mounted file, redeploy, delete old key id. |
| `DOCUSIGN_PRIVATE_KEY_PATH` (file) | DocuSign app | Same pattern as the Google key. |
| `SLACK_WEBHOOK_URL` | Slack app | Replace, redeploy `worker`. |

After any rotation: `railway variable list --service api` and `--service worker` to confirm
both have the new value; names only, do not paste values anywhere.

### 5.3 Suspected leak

Rotate immediately per 5.2, then search Railway logs for the window of exposure
(`railway logs --service <name>`), then note the incident in section 10.

## 6. Restart test (RUL-09 / WF-22) — mandatory before any 24/7 claim

Goal: prove that a workflow sleeping across a worker restart completes exactly once, with no lost
and no duplicated activity. Uses `HelloWorkflow` (`wholesaling/workflows/hello.py`), whose
`run(name, delay_seconds)` sleeps durably, then runs the `hello` activity once.

Runs against the deployed worker and Temporal Cloud. Rehearse locally first with
`temporal server start-dev --db-filename var/temporal.db` and a local worker (same commands without
`--tls` and with the local worker killed by Ctrl-C).

**Procedure**

1. Confirm the baseline: one poller on the queue.
   ```bash
   temporal task-queue describe --task-queue wholesaling-main --tls
   ```
2. Start a run that sleeps 10 minutes. Use a fresh id each time.
   ```bash
   export RT_ID=restart-test-$(date +%Y%m%d-%H%M%S)
   temporal workflow start --tls --type HelloWorkflow --task-queue wholesaling-main \
     --workflow-id "$RT_ID" --input '"restart-test"' --input '600'
   ```
3. After about 60 s, confirm it is waiting and note the remaining time.
   ```bash
   temporal workflow query --tls --workflow-id "$RT_ID" --type status
   # expect state "waiting", paused false, remaining_seconds about 540
   ```
4. Kill the worker. Either is acceptable; do the first on the first run, the second on a later run.
   ```bash
   railway restart --service worker --yes     # SIGTERM, container restarts (graceful path)
   railway redeploy --service worker --yes    # full redeploy (what a code push does)
   ```
   Watch `railway logs --service worker`: `worker.shutdown_requested`, `worker.stopped`, then
   `worker.started` from the new container. Note how long the queue had no poller.
5. While the worker is down (be quick, or make `delay_seconds` longer), confirm Temporal still owns
   the run: the query in step 3 still answers, `remaining_seconds` keeps falling.
6. Wait for the 10 minutes to elapse, then collect the result.
   ```bash
   temporal workflow result --tls --workflow-id "$RT_ID"       # "Hello, restart-test!"
   temporal workflow show   --tls --workflow-id "$RT_ID" > "$RT_ID.history.txt"
   ```
7. Prove "once and only once" from the history file:
   - exactly one `ActivityTaskScheduled` for activity type `hello`;
   - exactly one `ActivityTaskCompleted`;
   - exactly one `WorkflowExecutionCompleted`;
   - no `ActivityTaskFailed`, `ActivityTaskTimedOut`, `WorkflowTaskFailed`, or
     `WorkflowExecutionFailed`.
   And from the worker logs: exactly one `activity.hello` line whose `correlation_id` is `$RT_ID`,
   emitted by the **new** container (after `worker.started`).
   ```bash
   grep -c "ActivityTaskScheduled" "$RT_ID.history.txt"      # 1
   grep -c "ActivityTaskCompleted" "$RT_ID.history.txt"      # 1
   grep -c "WorkflowExecutionCompleted" "$RT_ID.history.txt" # 1
   railway logs --service worker | grep "activity.hello" | grep -c "$RT_ID"   # 1
   ```
8. Record in section 10: date, run id, kill method, poller gap in seconds, result (pass/fail).
   The 24/7 claim may be made only after a pass with each kill method.

**Pass**: step 7 counts are all exactly 1 and the result string is correct.
**Fail**: any count is 0 (lost) or more than 1 (duplicated), or the run is not completed 2 minutes
after the sleep should have ended (worker never resumed polling). On failure, do not deploy any
sequence workflow (BO-07) until the cause is fixed and the test is rerun.

Later sequences add an assertion to this test: exactly one `outbound_messages` row per planned send
after a restart (RUL-05 idempotency). That version lands with BO-07.

## 7. Health and readiness

| Endpoint | Meaning | Who calls it | On failure |
|---|---|---|---|
| `GET /health` | Liveness: the API process answers. Always 200 with `{"status":"ok","version":...}`. Checks nothing external. | Railway deploy probe (`healthcheckPath`), the image `HEALTHCHECK`, humans | Railway marks the new deploy failed and keeps the old one serving; traffic never reaches an unhealthy container. |
| `GET /ready` | Readiness: `SELECT 1` through the async engine, plus the Temporal gateway health check. 200 with `failing: []`, or 503 with `failing: ["database"]`/`["temporal"]` and a `detail` per check. | Humans, external uptime monitor, the watchdog (WF-17, later) | Nothing automatic yet. See section 8. |

Railway probes `/health` only during a deploy, not continuously. Continuous monitoring of `/ready`
is an external uptime check (any HTTP monitor hitting `/ready` every 1 to 5 minutes, alerting on
non-200) and the Temporal watchdog schedule (WF-17) once built.

The worker has no HTTP endpoint. Its health is observable as: a poller on the task queue
(`temporal task-queue describe`), the `worker.started` log line, and (from BO-07) the heartbeat
rows the watchdog reads.

Restart policy for both services: `ON_FAILURE`, max 10 retries, declared in `.railway/railway.py`.
A container that exits non-zero is restarted up to 10 times, then Railway stops restarting and the
dashboard shows it crashed. Both entrypoints exit non-zero deliberately when
`validate_for_production()` finds a problem, so a misconfigured deploy surfaces within a minute
instead of running on SQLite or without TLS.

## 8. Logs

```bash
railway logs --service api               # stream
railway logs --service worker
railway logs --service worker --json     # machine-readable
```

Format: JSON lines in `staging`/`production` (structlog), one event per line, fields `event`,
`level`, `timestamp`, `correlation_id` (the workflow id inside activities), plus event-specific
keys. Sensitive keys are redacted before they are written.

Events worth knowing by name:

| Event | Emitted by | Meaning |
|---|---|---|
| `api.started` / `api.stopped` | api | lifespan |
| `api.refused` / `worker.refused` | api / worker | `validate_for_production` or `TEMPORAL_MODE` problem; the process exits |
| `worker.started` / `worker.stopped` | worker | with `task_queue`, registered `workflows` and `activities` |
| `worker.shutdown_requested` | worker | SIGTERM/SIGINT received; graceful drain begins |
| `temporal.connected` / `temporal.lazy_connect` | both | connection to Temporal |
| `readiness.failed` | api | `/ready` returned 503, with `failing` |
| `activity.hello` | worker | the hello activity ran |

Temporal's own view of every workflow (history, pending activities, failures) is in the Temporal
Cloud UI for the namespace, or `temporal workflow list --tls` / `temporal workflow show --tls
--workflow-id <id>`.

## 9. Alerts (WF-23 and related rows)

Channels: email digest to `ALERT_EMAIL_TO`, optional Slack via `SLACK_WEBHOOK_URL` (STK-09). The
alerting activity itself is built in BO-07 with the daily digest; until then "alert" means a log
line and a human looking at the dashboard.

| Alert | Source row | Condition | Implemented in step | Status |
|---|---|---|---|---|
| Deploy health check failed | WF-23 | `/health` not 2xx within 120 s on deploy | BO-01 (Railway config) + BO-03 (`/health`) | done pending first deploy |
| Container crash / restart loop | WF-23 restart policies | non-zero exit, up to 10 restarts | BO-01 (Railway config) | done pending first deploy |
| Readiness degraded | WF-23 | `/ready` 503 | BO-03 (endpoint); external monitor at first deploy | endpoint done; monitor future |
| Worker restart test failed | WF-22, RUL-09 | section 6 fails | BO-03 (procedure) | procedure written; not run |
| Graph subscription renewal failed twice | WF-11 | two consecutive renewal failures | BO-04 | future |
| Webhook expiration | WF-23 | subscription past expiry without renewal | BO-04 | future |
| Delta poll / webhook reconciliation gap | WF-12 | poll finds messages the webhook never delivered | BO-04 | future |
| Bounced emails | WF-23 | bounce signal on a sequence | BO-07 (A3 bounce handling) | future |
| Stuck review queue | WF-23 stuck queues | items older than a threshold without decision | BO-07 (review queue + digest) | future |
| Agent QA failure | WF-23 | A5/QA pass fails or confidence below `caps.yaml` threshold | BO-07 | future |
| Parser errors (Zapier path) | WF-23 | parser returns error when `ZAPIER_PARSER_ENABLED=true` | BO-08 | future |
| Failed Sheets sync | WF-23, WF-13 | sync run raises or writes 0 rows unexpectedly | BO-08 | future |
| Daily reconciliation mismatch | WF-14 | Outlook vs Postgres vs Sheets counts disagree | BO-08 | future |
| Budget soft cap | WF-16, OPN-07 | spend ≥ `SOFT_BUDGET_ALERT_PCT`% of `SOFT_BUDGET_CAP_USD`; at 100% pause discovery and search | BO-05 (cost logging) + BO-07 (schedule) | future |
| Stale heartbeats (watchdog) | WF-17 | worker heartbeat older than 10 min | BO-07 (`heartbeat_probe` exists since BO-03) | future |
| Dead-letter queue | WF-23 | activity exhausted retries | BO-07 (per-workflow policy); Temporal retains failed runs meanwhile | future |
| Policy promotion check result | WF-15 | daily; informational | BO-07 | future |
| Opt-out received | WF-21 | immediate suppression; cancel queued sends | BO-07 | future |

## 10. Change log and test record

Append one line per deploy, rollback, rotation, restart test, or `*_MODE` flip. Names and ids
only, never values.

| Date (UTC) | Who | Action | Detail | Result |
|---|---|---|---|---|
| 2026-10-25 | cloud session | wrote this runbook | nothing deployed; PRE-07, PRE-08, ENG-Q12 open | — |
