"""Railway Infrastructure as Code for the wholesaling system (REQUIREMENTS.md STK-04, BO-03).

Why this file and not railway.toml / railway.json
--------------------------------------------------
Railway's Config as Code (railway.toml / railway.json) is deprecated: new services cannot opt in and
existing files stop being read on 2026-12-01 (https://docs.railway.com/config-as-code/reference).
The replacement is one code file under ``.railway/`` evaluated by the Railway CLI
(https://docs.railway.com/infrastructure-as-code). TypeScript is GA; Python (this file) is beta.
Python was chosen because the original machine has Python and no Node. If the Python authoring path
gives trouble, ``railway config migrate --lang ts`` or hand-translating this file to
``.railway/railway.ts`` is a mechanical change; the service definitions below are the contract.

What it declares
----------------
One Postgres database and two services built from the same Dockerfile:

* ``api``    - FastAPI, public, Railway probes ``/health`` before switching traffic to a new deploy.
* ``worker`` - Temporal worker, start command ``wholesaling-worker``, no domain, no public port.

How to use it (docs/RUNBOOK.md "Deploy" has the full procedure)
---------------------------------------------------------------
    pip install railway-sdk==0.7.0            # into the interpreter the Railway CLI will find
    railway login && railway link             # once, after checkpoint PRE-08
    railway config plan                       # read the diff; secrets are redacted by default
    railway config apply

Secrets never live here. Every variable marked ``preserve()`` must be set in the Railway dashboard
(or ``railway variable set``) before the first apply; ``preserve()`` tells the CLI to keep whatever
value Railway already holds. The names match .env.example exactly; see docs/CHECKPOINTS.md for which
human checkpoint produces each value.
"""

from __future__ import annotations

from typing import Any

from railway_sdk import define_railway, github, postgres, preserve, project, service

# ENG-Q12 is still OPEN: no hosted repository exists yet. Set this to "owner/repo" once the private
# GitHub repo is created so Railway builds from GitHub on push. While it is None the services are
# declared without a source and you deploy the working tree with `railway up --service <name>`.
GITHUB_REPO: str | None = None
GITHUB_BRANCH = "main"

PROJECT_NAME = "wholesaling"
DOCKERFILE = "Dockerfile"
TASK_QUEUE = "wholesaling-main"

# Railway's Postgres plugin exposes DATABASE_URL as postgresql://...; SQLAlchemy needs the asyncpg
# driver in the scheme, so the URL is assembled from the plugin's component variables with Railway's
# ${{service.VAR}} reference syntax. RAILWAY_PRIVATE_DOMAIN keeps traffic on the private network.
DATABASE_URL_TEMPLATE = (
    "postgresql+asyncpg://${{postgres.PGUSER}}:${{postgres.PGPASSWORD}}"
    "@${{postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{postgres.PGDATABASE}}"
)


def _source() -> dict[str, Any] | None:
    if GITHUB_REPO is None:
        return None
    return github(GITHUB_REPO, branch=GITHUB_BRANCH)


def _common_env(role: str) -> dict[str, Any]:
    """Variables both services need. Non-secret values are literals; secrets are preserve()."""
    return {
        # ---- Runtime (.env.example "Runtime") ----
        "APP_ENV": "production",
        "LOG_LEVEL": "INFO",
        "SERVICE_ROLE": role,
        # ---- Database (.env.example "Database"): Railway Postgres, assembled above ----
        "DATABASE_URL": DATABASE_URL_TEMPLATE,
        # ---- Temporal Cloud (checkpoint PRE-07) ----
        # TEMPORAL_MODE=live makes the real client connect; TEMPORAL_TLS=true is required by
        # Settings.validate_for_production(). Address/namespace come from the Temporal Cloud
        # namespace page: <namespace>.<account>.tmprl.cloud:7233 and <namespace>.<account>.
        "TEMPORAL_MODE": "live",
        "TEMPORAL_TLS": "true",
        "TEMPORAL_TASK_QUEUE": TASK_QUEUE,
        "TEMPORAL_ADDRESS": preserve(),
        "TEMPORAL_NAMESPACE": preserve(),
        "TEMPORAL_API_KEY": preserve(),
        # ---- Everything external stays mocked until its checkpoint clears (RUL-01, RUL-06) ----
        "MODEL_PROVIDER": "mock",
        "GRAPH_MODE": "mock",
        "SHEETS_MODE": "mock",
        "ZAPIER_PARSER_ENABLED": "false",
        "SMS_MODE": "off",
        "DOCUSIGN_MODE": "mock",
        "BLOB_MODE": "local",
        # ---- Budget (OPN-07 defaults) ----
        "SOFT_BUDGET_CAP_USD": "1500",
        "SOFT_BUDGET_ALERT_PCT": "80",
        # ---- Alerting: set in the dashboard when you have them; blank is allowed ----
        "ALERT_EMAIL_TO": preserve(),
        "SLACK_WEBHOOK_URL": preserve(),
    }


@define_railway
def main(ctx: Any = None) -> Any:  # ctx is supplied by the Railway CLI
    db = postgres("postgres")

    build: dict[str, Any] = {"builder": "DOCKERFILE", "dockerfilePath": DOCKERFILE}
    # Restart on crash, not on clean exit. ON_FAILURE is Railway's default; the retry cap is
    # explicit so a crash loop is visible in the dashboard instead of silent (WF-23).
    restart: dict[str, Any] = {"restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 10}

    api_config: dict[str, Any] = {
        "build": build,
        "deploy": restart,
        "start": "wholesaling-api",
        "healthcheck": "/health",
        "healthcheckTimeout": 120,
        "replicas": 1,
        "env": {**_common_env("api"), "PORT": "8000"},
    }
    worker_config: dict[str, Any] = {
        "build": build,
        "deploy": restart,
        "start": "wholesaling-worker",
        # No healthcheck and no domains: the worker has no HTTP listener and is never public.
        "replicas": 1,
        "env": _common_env("worker"),
    }
    source = _source()
    if source is not None:
        api_config["source"] = source
        worker_config["source"] = source

    api = service("api", **api_config)
    worker = service("worker", **worker_config)

    return project(PROJECT_NAME, resources=[db, api, worker])
