# syntax=docker/dockerfile:1.11
# rask ray-api image — FastAPI on python:3.13-slim-bookworm.
# Build from repo root:
#   docker buildx build -f .docker/ray-api.dockerfile -t ray-api:dev .

# ---- builder stage: install deps via uv ------------------------------------
# hadolint ignore=DL3026
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
    --mount=type=bind,source=projects/ray-api/pyproject.toml,target=projects/ray-api/pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=components,target=components \
    uv sync --frozen --no-install-workspace --package ray-api --no-editable

# Step 2: COPY real sources and resolve workspace deps (locked).
COPY pyproject.toml uv.lock ./
COPY packages    packages
COPY components  components
COPY projects/ray-api projects/ray-api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package ray-api --no-editable

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final stage -----------------------------------------------------------
# hadolint ignore=DL3026
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2

LABEL org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-ray-api" \
      org.opencontainers.image.description="rask ray-api on :8804"

# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app

RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8804

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8804/api/v1/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "ray_api:app", "--host", "0.0.0.0", "--port", "8804", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
