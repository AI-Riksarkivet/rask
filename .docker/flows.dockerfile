# syntax=docker/dockerfile:1.11
# rask flows service image — FastAPI on python:3.13-slim-bookworm.
# The studio flow-builder's server half: node catalog, graph validation, run execution (inline, or
# over Dapr Workflow when the injected sidecar sets DAPR_GRPC_PORT).
# Build (DAGGER builds every image in this repo — this recipe is the definition, not the driver):
#   dagger call image --name=flows
#   dagger call image --name=flows publish --address=172.17.0.1:5000/flows:dev

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

# Step 1: install the package's external deps (frozen — workspace sources not yet COPYed).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --frozen --no-install-workspace --package flows --no-editable

# Step 2: COPY real sources and resolve the workspace package (locked).
COPY pyproject.toml uv.lock ./
COPY packages    packages
COPY services  services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package flows --no-editable

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
      org.opencontainers.image.title="rask-flows" \
      org.opencontainers.image.description="rask flows service — the studio flow-builder's node catalog, graph validation and run execution, FastAPI on :8840"

# curl: used by the docker-compose healthcheck.
# hadolint ignore=DL3008  # Reason: tini and ca-certificates have no stable version pins in apt on slim; pinning would break on next base image update.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app

# cp -a from a mount rather than COPY --from: a COPY of a large venv can trip BuildKit's
# exit code 2 and silently fall back to a full rebuild. Dev builds pass 10001:10001; prod stays root.
RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# NUMERIC, not `app`. The kubelet compares runAsNonRoot against a NUMBER, so a named user is
# refused outright — `cannot verify user is non-root` — and the container never starts while its
# previous pod keeps serving, so the deployment silently stops being able to roll. Same uid the
# useradd above creates; only the spelling changes. Pinned by
# tests/unit/test_invariants.py::test_an_image_the_chart_hardens_declares_a_NUMERIC_user
USER 10001
EXPOSE 8840

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8840))" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# This service sits behind the FastAPI gateway (services/gateway); --forwarded-allow-ips
# MUST be the gateway's CIDR at deploy time, never '*' (header-spoofing risk).
# --workers intentionally unset: the run store is a process-local dict (v0), so a second worker
# would serve GET /flows/runs/{id} from a process that never saw the run — a 404 half the time.
CMD ["uvicorn", "flows:app", \
     "--host", "0.0.0.0", "--port", "8840", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1"]
