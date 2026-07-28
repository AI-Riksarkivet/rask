# syntax=docker/dockerfile:1.11
# rask fleet DEV image — editable install with source kept at /app, so Tilt's
# live_update can sync changed .py files and `uvicorn --reload` (added by the chart
# when dev.reload=true) hot-reloads the service in-cluster.
#
# NOT for production. Prod uses the per-service .docker/<svc>.dockerfile, which does
# a two-stage `uv sync --no-editable` (wheel install) and ships only a stripped venv.
#
#   docker build -f .docker/fleet.dev.dockerfile --build-arg PACKAGE=gateway -t gateway:dev .
# hadolint ignore=DL3026
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv

WORKDIR /app

# Which workspace package this image runs (gateway, ray-api, controlplane, ...).
ARG PACKAGE

# Step 1: external deps only (frozen, no workspace sources yet) — cached across edits.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=components,target=components \
    uv sync --frozen --no-install-workspace --package "${PACKAGE}"

# Step 2: COPY real sources and resolve EDITABLE (note: no --no-editable). uv points
# the workspace members (the package + service-kit/core/etc.) at /app/.../src, so the
# files Tilt syncs into /app/{packages,components} are exactly what gets imported.
COPY pyproject.toml uv.lock ./
COPY packages    packages
COPY components  components
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package "${PACKAGE}"

# The container command (uvicorn <module> --host 0.0.0.0 --port <n> [--reload ...])
# is supplied by the chart's fleet template, not baked here.
