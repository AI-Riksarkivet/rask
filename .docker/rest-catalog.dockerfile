# syntax=docker/dockerfile:1.11
# Lance Namespace REST Catalog — production image.
# Build context = repo root:  docker build -f .docker/rest-catalog.dockerfile .

# ── builder ──────────────────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install only third-party deps from the lockfile (no project source yet → cached
# until pyproject.toml / uv.lock change). This project is a virtual app, so
# --no-install-project leaves just the dependency graph in /opt/venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# ── final ────────────────────────────────────────────────────────────────────
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="lance-rest-catalog" \
      org.opencontainers.image.description="Lance Namespace REST Catalog (FastAPI over native DirectoryNamespace on RustFS/S3) + lineage service" \
      org.opencontainers.image.source="https://github.com/Borg93/lance-ns" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"

# tini for correct PID 1 signal handling / zombie reaping. No `|| true` here — a failed apt/useradd
# MUST fail the build (else the image ships without tini and every container fails to start).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/services

WORKDIR /srv
COPY --from=builder --link /opt/venv /opt/venv
# All services + the shared `common` package live under services/ (PYTHONPATH=/srv/services makes
# catalog/lineage/medallion/compaction/common importable). One image, many entrypoints — each container
# runs a different command:  catalog.main:app (2333) · lineage.main:app (8000) · medallion.producer:app /
# medallion.mover:app · compaction.service:app (env-configured in the chart).
COPY --link services ./services

# Strip setuid/setgid bits from the whole shipped filesystem (base account tools passwd/chsh/... +
# anything in the venv) — no use in a container, residual privesc surface. Own RUN so its `|| true`
# (which only tolerates find matching nothing) cannot mask a real build failure above.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true

# NUMERIC USER (the `app` account is uid 10001) — k8s `runAsNonRoot: true` can only VERIFY non-root at
# admission when the image user is numeric; a name ("app") makes the kubelet reject the pod
# (CreateContainerConfigError). The chart's lance.securityContext relies on this.
USER 10001
# 2333 = catalog (catalog.main:app); 8000 = lineage service (lineage.main:app) — same image, run with
# `command: uvicorn lineage.main:app --host 0.0.0.0 --port 8000` (see docker-compose.governance.yml).
EXPOSE 2333 8000

# Cheap socket-connect probe (avoids urllib's ssl/http.client cold-import cost). k8s owns real
# readiness via /readyz; this HEALTHCHECK is for the compose demo where no orchestrator probe exists.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',2333)); s.close()"]

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "catalog.main:app", "--host", "0.0.0.0", "--port", "2333"]
