# syntax=docker/dockerfile:1.7
#
# One image, two Railway services (REQUIREMENTS.md STK-04, BO-03):
#   api    -> default CMD below (wholesaling-api), public port 8000, /health probed by Railway
#   worker -> same image, start command `wholesaling-worker`, no public port
# Both are defined in .railway/railway.py. Runtime config comes only from environment variables
# listed in .env.example; nothing secret is baked into this image.

# ---- Stage 1: build the virtualenv with uv ------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Pinned uv. Keep in step with .github/workflows/ci.yml (UV_VERSION) and the version that wrote uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself (README.md is referenced by pyproject [project].readme).
COPY README.md ./
COPY wholesaling ./wholesaling
COPY config ./config
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Stage 2: runtime ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# curl is only for the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# var/ is where BLOB_MODE=local writes seller photos (BLOB_LOCAL_DIR=./var/blobs). Mount a volume
# here in Railway if local blob storage is ever used in a deployed environment.
RUN mkdir -p /app/var && chown -R app:app /app/var

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    SERVICE_ROLE=api

USER app

EXPOSE 8000

# Liveness only (see docs/RUNBOOK.md "Health and readiness"). Railway runs its own deploy-time
# probe against healthcheckPath; this HEALTHCHECK is for docker-compose and local runs.
# The worker has no HTTP listener, so docker-compose.yml disables it for that service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["wholesaling-api"]
