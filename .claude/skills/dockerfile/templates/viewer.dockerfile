# syntax=docker/dockerfile:1.11
# rask viewer image — FastAPI on python:3.13-slim-bookworm.
# Install to repo root as: cp .claude/skills/dockerfile/templates/viewer.dockerfile .docker/viewer.dockerfile
# Build:
#   docker buildx build -f .docker/viewer.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t viewer:dev .

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

# Step 1: install workspace deps (frozen — workspace member sources not yet COPYed).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=projects/viewer/pyproject.toml,target=projects/viewer/pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=components,target=components \
    uv sync --frozen --no-install-workspace --package viewer --no-editable

# Step 2: COPY real sources and resolve workspace deps (locked).
COPY pyproject.toml uv.lock ./
COPY packages    packages
COPY components  components
COPY projects/viewer projects/viewer
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package viewer --no-editable

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
      org.opencontainers.image.title="rask-viewer" \
      org.opencontainers.image.description="rask viewer service — FastAPI on :8888"

# hadolint ignore=DL3008  # Reason: tini and ca-certificates have no stable version pins in apt on slim; pinning would break on next base image update.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app

RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8888))" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# Note: --forwarded-allow-ips MUST be set to the nginx network CIDR at deploy time,
# never '*' (header-spoofing risk). --workers is intentionally unset — orchestrator scales.
CMD ["uvicorn", "viewer.app:app", \
     "--host", "0.0.0.0", "--port", "8888", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1", \
     "--no-access-log", \
     "--loop", "uvloop", \
     "--http", "httptools"]
