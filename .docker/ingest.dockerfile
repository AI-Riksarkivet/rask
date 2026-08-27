# syntax=docker/dockerfile:1.11
# rask ingest service image — the pre-bronze acquisition plane on python:3.13-slim-bookworm.
# Control API + JetStream unit workers + the lander, all one image: they share the workflow
# definitions and the source registry, and Kubernetes runs them as separate Deployments off the same
# image with different CMDs. Two images would mean two places for the workflow registration to drift.
#
# Build (Dagger ONLY — CLAUDE.md law; `docker build` must not appear in the Makefile, scripts/,
# .github/workflows or the Tiltfile):
#   dagger call image --name=ingest
#   scripts/dagger-image.sh ingest

# ---- builder stage: install deps via uv ------------------------------------
# hadolint ignore=DL3026  # Reason: python:slim-bookworm is the official Docker Hub image; digest-pinned for reproducibility.
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv

WORKDIR /app

# Step 1: external deps only, bind-mounted so the workspace sources do not bust this layer. `ingest`
# pulls pylance + dapr-ext-workflow + nats-py, which is most of the build time — keeping it off the
# source-change path is the difference between a 5s rebuild and a 90s one.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --frozen --no-install-workspace --package ingest --no-editable

# Step 2: COPY real sources and resolve the workspace package (locked).
COPY pyproject.toml uv.lock ./
COPY packages    packages
COPY services  services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package ingest --no-editable

# Strip residual setuid bits before the venv leaves the builder.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final stage: minimum runtime surface ----------------------------------
# hadolint ignore=DL3026  # Reason: python:slim-bookworm is the official Docker Hub image; digest-pinned for reproducibility.
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-ingest" \
      org.opencontainers.image.description="rask ingest service — pre-bronze acquisition: control API, JetStream unit workers, and the Lance lander, FastAPI on :8830"

# hadolint ignore=DL3008  # Reason: tini and ca-certificates have no stable version pins in apt on slim; pinning would break on next base image update.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app

# The venv is copied root-owned and stays immutable — the container runs as uid 10001 and never
# writes into it. This carried a `VENV_OWNER` build arg so Tilt's live_update could sync into the venv
# as 10001:10001; Tilt was removed 2026-08-04 along with that arg in the other four dockerfiles, and an
# arg no build passes is only a claim that a dev loop still exists.
RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

# ── the import gate ──────────────────────────────────────────────────────────
# Import every module this image serves, against the venv the runtime stage ships. A missing
# DECLARED dependency is invisible to the workspace venv (a sibling member resolves it) and can
# only be seen here, where the deployable's own closure is the only one present. See the script.
COPY .docker/import-gate.py /tmp/import-gate.py
RUN --network=none /opt/venv/bin/python /tmp/import-gate.py ingest && rm /tmp/import-gate.py

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# NUMERIC, not `app`. The kubelet compares runAsNonRoot against a NUMBER, so a named user is
# refused outright — `cannot verify user is non-root` — and the container never starts while its
# previous pod keeps serving, so the deployment silently stops being able to roll. Same uid the
# useradd above creates; only the spelling changes. Pinned by
# tests/unit/test_invariants.py::test_an_image_the_chart_hardens_declares_a_NUMERIC_user
USER 10001
EXPOSE 8830

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8830))" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# The control API. The worker Deployment overrides CMD with the consumer entrypoint off this same
# image — one image, two roles, no chance of the workflow registration drifting between them.
# Behind the gateway, so --forwarded-allow-ips is the gateway's CIDR at deploy time, never '*'.
CMD ["uvicorn", "ingest:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8830", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1"]
