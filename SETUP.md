# SETUP.md — from a fresh clone to tests and a local api + worker in mock mode

This file covers one developer machine. Nothing here needs an account, a credential or a paid
service (REQUIREMENTS.md RUL-01). Every external integration runs in `mock` mode until its human
checkpoint in [docs/CHECKPOINTS.md](docs/CHECKPOINTS.md) is cleared. Operating the deployed system
is in [docs/RUNBOOK.md](docs/RUNBOOK.md).

**Docker is optional.** The path the original machine uses, and the one documented first, is:
Python 3.12 + uv + Git + the Temporal CLI (`temporal server start-dev`), with the test suite on
SQLite in memory. Docker Compose (section 6) is an alternative for developers who already have
Docker and want local Postgres.

## 1. Toolchain

| Tool | Version | Why |
|---|---|---|
| Python | 3.12.x (`.python-version` pins 3.12) | Runtime. `pyproject.toml` requires `>=3.12,<3.13`. |
| uv | 0.13.x (the Dockerfile and CI pin 0.13.5) | Dependency manager and task runner (ENG-Q2). |
| Git | 2.4x or newer | Repository. |
| Temporal CLI | 1.9.x | `temporal server start-dev` is the local Temporal server (ENG-Q7). |
| Docker Desktop / Engine | optional | Only for section 6. |
| Railway CLI | optional until checkpoint PRE-08 | Only for deploying; see docs/RUNBOOK.md. |

### Windows (PowerShell 5.1 or 7, `winget`)

Run in a normal (non-admin) PowerShell. All four install to the user scope.

```powershell
winget install --id Python.Python.3.12 --scope user
winget install --id astral-sh.uv
winget install --id Git.Git
winget install --id Temporal.TemporalCLI
```

Close and reopen PowerShell so `PATH` picks up the new tools, then confirm:

```powershell
python --version      # Python 3.12.x  (if this opens the Microsoft Store, disable the "python.exe"
                      #  App execution alias under Settings > Apps > Advanced app settings)
uv --version
git --version
temporal --version
```

Git line endings: the repository ships `.gitattributes` forcing LF. Set `git config --global
core.autocrlf input` once so your working copy also stays LF.

### macOS

```bash
brew install uv git temporal
uv python install 3.12
```

(`uv python install 3.12` downloads a managed CPython; you do not need a system Python 3.12.)

### Linux (Debian/Ubuntu shown)

```bash
sudo apt-get install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh        # uv
uv python install 3.12
# Temporal CLI: Homebrew on Linux (`brew install temporal`) or download the archive from
# https://docs.temporal.io/cli/setup-cli and put `temporal` on your PATH.
```

## 2. Clone and install

ENG-Q12 (Git host) is still OPEN, so until a remote exists the "clone" is a copy of the project
folder. Once the private GitHub repository exists:

```bash
git clone <repo-url> wholesaling
cd wholesaling
```

Install the exact locked dependency set (runtime + dev group) into `./.venv`:

```bash
uv sync --frozen
```

`--frozen` refuses to change `uv.lock`; if it fails, the lockfile and `pyproject.toml` disagree and
someone must run `uv lock` deliberately. Never edit `uv.lock` by hand.

## 3. Configuration

```bash
cp .env.example .env            # PowerShell: Copy-Item .env.example .env
```

Leave every value as shipped. The defaults are:

| Key | Default | Meaning |
|---|---|---|
| `APP_ENV` | `local` | Not production: `validate_for_production()` is not enforced. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./local.db` | A file database in the repo root; ignored by git. |
| `TEMPORAL_MODE` | `mock` | In-memory Temporal client; no server needed for the API alone. |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Where `temporal server start-dev` listens. |
| `MODEL_PROVIDER`, `GRAPH_MODE`, `SHEETS_MODE`, `DOCUSIGN_MODE` | `mock` | No external calls. |
| `SMS_MODE` | `off` | `live` is rejected at startup in this build (RUL-06). |
| `BLOB_MODE` | `local` | Seller photos go to `./var/blobs` (ignored by git). |

`.env` is in `.gitignore`. A unit test (`tests/unit/test_settings.py`) fails if `.env.example` and
`wholesaling/config/settings.py` ever disagree about the set of keys, so when a new setting is
added it must be added in both places.

## 4. Run the checks (no services)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

What runs where:

| Command | Needs | Notes |
|---|---|---|
| `uv run pytest` | nothing | Unit tests on SQLite in memory. Tests marked `postgres` are skipped automatically. |
| `uv run pytest -m temporal` | internet on first run | Downloads the Temporal time-skipping test server binary once (cached by the SDK). No Temporal server needed. |
| `TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest` | a Postgres 16 | Runs the whole suite against Postgres and enables the `postgres`-marked tests. CI does this; locally see section 6. |
| `uv run pytest --cov=wholesaling` | nothing | Coverage report, same as CI. |

Windows: set the variable for one command with
`$env:TEST_DATABASE_URL = "postgresql+asyncpg://..."; uv run pytest` and clear it with
`Remove-Item Env:TEST_DATABASE_URL`.

## 5. Run api + worker locally in mock mode

Three terminals.

**Terminal 1 — Temporal dev server** (gRPC `localhost:7233`, UI `http://localhost:8233`):

```bash
temporal server start-dev --db-filename var/temporal.db
```

`--db-filename` keeps workflow history across restarts so you can rehearse the restart test in
docs/RUNBOOK.md; omit it for a throwaway in-memory server. `var/` is ignored by git.

**Terminal 2 — worker**. Set `TEMPORAL_MODE=live` (it means "talk to a real Temporal server",
here the local one; every other integration stays mock):

```bash
TEMPORAL_MODE=live SERVICE_ROLE=worker uv run wholesaling-worker
```

PowerShell:

```powershell
$env:TEMPORAL_MODE = "live"; $env:SERVICE_ROLE = "worker"; uv run wholesaling-worker
```

**Terminal 3 — API** on `http://localhost:8000`:

```bash
TEMPORAL_MODE=live SERVICE_ROLE=api uv run wholesaling-api
```

Check it:

```bash
curl -s http://localhost:8000/health     # 200 {"status":"ok","version":"0.1.0"} : process is up
curl -s http://localhost:8000/ready      # 200 when DB and Temporal answer; 503 + "failing" list otherwise
```

Open `http://localhost:8233` to see the `default` namespace. Start a `HelloWorkflow` on task
queue `wholesaling-main` and watch the worker pick it up (it sleeps `delay_seconds`, then runs
the `hello` activity):

```bash
temporal workflow start --type HelloWorkflow --task-queue wholesaling-main \
  --workflow-id hello-local-1 --input '"Aaron"' --input '5'
temporal workflow query --workflow-id hello-local-1 --type status
temporal workflow result --workflow-id hello-local-1
```

The `wholesaling-api` and `wholesaling-worker` console scripts are declared in `pyproject.toml`
and resolve to `wholesaling.api.main:run` and `wholesaling.workers.main:run`. The worker refuses
to start unless `TEMPORAL_MODE=live` (a worker exists only to poll a real server); the API runs in
either mode. With `APP_ENV=production` both refuse to start unless `DATABASE_URL` is Postgres and
`TEMPORAL_TLS=true` with `TEMPORAL_API_KEY` set (`Settings.validate_for_production`).

## 6. Optional: Docker Compose (local Postgres + Temporal, or the whole stack)

Requires Docker Desktop (Windows/macOS; needs admin to install on Windows) or Docker Engine.

Services only (then run api/worker with `uv run` as in section 5, pointing `DATABASE_URL` at the
container):

```bash
docker compose up -d postgres temporal
export DATABASE_URL=postgresql+asyncpg://wholesaling:wholesaling@localhost:5432/wholesaling
export TEST_DATABASE_URL=$DATABASE_URL
uv run pytest            # whole suite on Postgres, including `postgres`-marked tests
```

Everything in containers, built from the same `Dockerfile` Railway uses:

```bash
docker compose --profile app up --build
```

| Service | Host port | Purpose |
|---|---|---|
| `postgres` | 5432 | Postgres 16, named volume `postgres-data`. |
| `temporal` | 7233, 8233 | `temporalio/temporal` running `server start-dev`, history persisted in volume `temporal-data`. |
| `api` (profile `app`) | 8000 | Image default command `wholesaling-api`. |
| `worker` (profile `app`) | none | Same image, command `wholesaling-worker`. |

`docker compose down -v` removes the volumes and starts clean.

## 7. Before you commit

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy && uv run pytest
```

CI (`.github/workflows/ci.yml`) runs exactly these plus the Postgres job and a Docker image build;
a change that fails locally will fail there.

## 8. Going live

Nothing in this file touches a real service. Deploying to Railway and turning any integration from
`mock` to `live` is gated by the human checkpoints in [docs/CHECKPOINTS.md](docs/CHECKPOINTS.md):
PRE-07 (Temporal Cloud) and PRE-08 (Railway) unblock the first deploy; the rest unblock their
integrations one at a time. The deploy, rollback, secret rotation and the mandatory restart test
(RUL-09) are in [docs/RUNBOOK.md](docs/RUNBOOK.md).
