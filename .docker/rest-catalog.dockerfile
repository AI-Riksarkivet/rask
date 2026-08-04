# syntax=docker/dockerfile:1.11
# Lance lakehouse image — ONE image, many entrypoints, built from the ROOT uv workspace
# (src-layout services installed as wheels; no PYTHONPATH, no raw source COPY in the final stage).
# The chart runs every lakehouse container from this image with a different command
# (see chart/templates/{services,medallion,maintenance,media}.yaml → include "lance.catalogImage"):
#   catalog.main:app (:2333) · lineage.main:app (:8000) · medallion.producer:app /
#   medallion.mover:app (:8000) · maintenance.service:app (:8000) ·
#   viewer.main:app (:8101) · search.main:app (:8102) · annotator.main:app (:8103)
# — optionally wrapped in `opentelemetry-instrument` (shipped via lineage's opentelemetry-distro).
# Build context = repo root:  docker build -f .docker/rest-catalog.dockerfile .

# ── builder: two-step uv sync against the ROOT uv.lock ───────────────────────
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Step 1: third-party deps only, from the lockfile (--frozen tolerates the absent workspace
# source trees; --no-install-workspace skips the members themselves). The union of --package
# flags covers every service the chart runs from this image — cached until pyproject/uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --frozen --no-dev --no-install-workspace --no-editable \
        --package catalog --package lineage --package medallion --package maintenance \
        --package viewer --package search --package annotator

# Step 2: COPY real sources and install the workspace members as wheels (--no-editable →
# they land in site-packages; the final stage needs only the venv).
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
        --package catalog --package lineage --package medallion --package maintenance \
        --package viewer --package search --package annotator

# Strip residual setuid/setgid bits before the venv leaves the builder.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ── final: minimum runtime surface ───────────────────────────────────────────
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="lance-rest-catalog" \
      org.opencontainers.image.description="Lance lakehouse fleet image — REST catalog + lineage + medallion + maintenance + media trio (viewer/search/annotator); one image, per-container uvicorn commands" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

# tini for correct PID 1 signal handling / zombie reaping. No `|| true` here — a failed apt/useradd
# MUST fail the build (else the image ships without tini and every container fails to start).
# hadolint ignore=DL3008  # Reason: tini has no stable version pin in apt on slim; pinning would break on next base image update.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
# The venv is root-owned and the app runs as 10001, so it is immutable to the account running it.
COPY --from=builder --link /opt/venv /opt/venv

# Strip setuid/setgid bits from the whole shipped filesystem (base account tools passwd/chsh/... +
# anything in the venv) — no use in a container, residual privesc surface. Own RUN so its `|| true`
# (which only tolerates find matching nothing) cannot mask a real build failure above.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true

# NUMERIC USER (the `app` account is uid 10001) — k8s `runAsNonRoot: true` can only VERIFY non-root at
# admission when the image user is numeric; a name ("app") makes the kubelet reject the pod
# (CreateContainerConfigError). The chart's lance.securityContext relies on this.
USER 10001
# 2333 catalog · 8000 lineage/medallion/maintenance · 8101/8102/8103 viewer/search/annotator.
EXPOSE 2333 8000 8101 8102 8103

# Cheap socket-connect probe against the default CMD's port (avoids urllib's ssl/http.client
# cold-import cost). k8s owns real readiness via /readyz; this HEALTHCHECK is for the compose demo.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',2333)); s.close()"]

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "catalog.main:app", "--host", "0.0.0.0", "--port", "2333"]
