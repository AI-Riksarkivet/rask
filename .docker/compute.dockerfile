# syntax=docker/dockerfile:1.11
# rask compute service image — FastAPI on python:3.13-slim-bookworm.
# (R22: the Ray-plane service is `compute` on every surface — uv member, import,
# k8s/dapr/image name. The Ray CLUSTER image stays .docker/ray-cluster.dockerfile.)
# Build:
#   docker buildx build -f .docker/compute.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t compute:dev .

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
    uv sync --frozen --no-install-workspace --package compute --no-editable

# Step 2: COPY real sources and resolve the workspace package (locked).
COPY pyproject.toml uv.lock ./
COPY packages    packages
COPY services  services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package compute --no-editable

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
      org.opencontainers.image.title="rask-compute" \
      org.opencontainers.image.description="rask compute service — Ray dashboard introspection + serve proxy, FastAPI on :8804"

# curl: used by the docker-compose healthcheck.
# hadolint ignore=DL3008  # Reason: tini and ca-certificates have no stable version pins in apt on slim; pinning would break on next base image update.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app

# exit code 2 and silently fall back to a full rebuild. Dev builds pass 10001:10001; prod stays root.
RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

# ── the import gate ──────────────────────────────────────────────────────────
# Import every module this image serves, against the venv the runtime stage ships. A missing
# DECLARED dependency is invisible to the workspace venv (a sibling member resolves it) and can
# only be seen here, where the deployable's own closure is the only one present. See the script.
COPY .docker/import-gate.py /tmp/import-gate.py
RUN --network=none /opt/venv/bin/python /tmp/import-gate.py compute && rm /tmp/import-gate.py

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# NUMERIC, not `app`. The kubelet compares runAsNonRoot against a NUMBER, so a named user is
# refused outright — `cannot verify user is non-root` — and the container never starts while its
# previous pod keeps serving, so the deployment silently stops being able to roll. Same uid the
# useradd above creates; only the spelling changes. Pinned by
# tests/unit/test_invariants.py::test_an_image_the_chart_hardens_declares_a_NUMERIC_user
USER 10001
EXPOSE 8804

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8804))" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# This service sits behind the FastAPI gateway (services/gateway); --forwarded-allow-ips
# MUST be the gateway's CIDR at deploy time, never '*' (header-spoofing risk).
# --workers intentionally unset.
CMD ["uvicorn", "compute:app", \
     "--host", "0.0.0.0", "--port", "8804", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1"]
