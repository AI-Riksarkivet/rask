# syntax=docker/dockerfile:1.11
# rask annotator image — a labeling tool that works on the lakehouse through its public doors (ADR 0171).
# It is a client of the lakehouse with its own release (deploy/fixtures/annotator), so it ships its own
# image built from the ROOT uv workspace with its own closure: `uv sync --package annotator` and nothing else.
# Build (DAGGER builds every image in this repo — this recipe is the definition, not the driver):
#   dagger call image --name=annotator
#   dagger call image --name=annotator publish --address=172.17.0.1:5000/annotator:dev

# ---- builder stage: two-step uv sync against the ROOT uv.lock --------------
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Step 1: third-party deps only, from the lockfile (--frozen tolerates the absent workspace source trees;
# --no-install-workspace skips the members themselves) — cached until pyproject/uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --frozen --no-dev --no-install-workspace --no-editable --package annotator

# Step 2: COPY real sources and install the workspace members as wheels (--no-editable → they land in
# site-packages; the final stage needs only the venv).
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package annotator

# Strip residual setuid/setgid bits before the venv leaves the builder.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final stage: minimum runtime surface ----------------------------------
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="rask-annotator" \
      org.opencontainers.image.description="rask annotator — labeling projects, tasks and publishes over the lakehouse's public doors; FastAPI on :8103" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

# tini for correct PID 1 signal handling / zombie reaping. A failed apt/useradd MUST fail the build.
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

# The import gate: every module this image serves imports against the closure it ships, so a dependency the
# annotator uses and does not declare fails the build here instead of a pod at start.
COPY .docker/import-gate.py /tmp/import-gate.py
RUN --network=none /opt/venv/bin/python /tmp/import-gate.py annotator && rm /tmp/import-gate.py

# Strip setuid/setgid bits from the whole shipped filesystem. Own RUN so its `|| true` (which only tolerates
# find matching nothing) cannot mask a real build failure above.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true

# NUMERIC USER (the `app` account is uid 10001): `runAsNonRoot: true` can only VERIFY non-root at admission
# when the image user is numeric.
USER 10001
EXPOSE 8103

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8103)); s.close()"]

ENTRYPOINT ["tini", "--"]
# --workers intentionally unset: Dapr places an actor id on a HOST (one daprd and the one app behind its app-port),
# so several workers would be several actor runtimes sharing one address. Scale replicas, never workers. The release
# overrides these args with its `forwardedAllowIps`, never '*'.
CMD ["uvicorn", "annotator.main:app", "--host", "0.0.0.0", "--port", "8103", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
