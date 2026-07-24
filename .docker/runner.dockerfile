# syntax=docker/dockerfile:1.11
# check=skip=SecretsUsedInArgOrEnv
# Reason: HF_HUB_DISABLE_IMPLICIT_TOKEN=1 is a boolean flag that disables implicit
# HF token look-up at runtime, not a credential. False positive from the lint rule.
# rask runner image — Python 3.13 + Ray + PyTorch on nvidia/cuda:12.4.0-runtime-ubuntu22.04.
# Install to repo root as: cp .claude/skills/dockerfile/templates/runner.dockerfile .docker/runner.dockerfile
# Build:
#   docker buildx build -f .docker/runner.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t runner:dev .
# Runtime requirements (paired with this image — set in deploy manifest):
#   --shm-size >= 30% of RAM   (Ray object store; ray-project/ray#13619, #14535)
#   --ulimit nofile=65535      (Ray Serve FD limit; ray-project/ray#13045)
#   --read-only --tmpfs /tmp   (image is built to support this)
#   GPU device exposure        (nvidia-container-toolkit)

# ---- builder stage: uv-managed Python + uv sync ----------------------------
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04@sha256:af8bd179ed3bf69d4b63b19a763662a6141f0f62ef099283f68d0b14b4bab0e3 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=auto \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    DEBIAN_FRONTEND=noninteractive

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv

# git is for htrflow (git-source dep), kept in builder ONLY.
# Reason: nvidia/cuda apt indexes ship rolling minor-versions; pinning every transitive
# dep would force constant base-image bumps. The digest pin on the base + nvidia's signed
# repo metadata bound the supply-chain risk.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Step 1: install workspace deps (frozen — workspace member sources not yet COPYed).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=components,target=components \
    uv sync --frozen --no-install-workspace --package runner --no-editable

# Step 2: COPY sources + lock metadata (bind-mounts from Step 1 are gone), then resolve (locked).
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY components components
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package runner --no-editable

# Optional gated-model fetch step. Uncomment if rask pulls licensed weights at build time.
# RUN --mount=type=secret,id=hf_token \
#     HF_TOKEN=$(cat /run/secrets/hf_token) \
#     /opt/venv/bin/python -m runner.fetch_models

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final stage: minimum CUDA runtime + venv + uv-Python -----------------
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04@sha256:af8bd179ed3bf69d4b63b19a763662a6141f0f62ef099283f68d0b14b4bab0e3

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-runner" \
      org.opencontainers.image.description="rask runner — Ray Data HTR pipelines"

ENV DEBIAN_FRONTEND=noninteractive
# Reason: see builder stage. The digest pin on the CUDA base is the supply-chain anchor;
# apt version-pinning these transitives would require chasing rolling updates.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates tini libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app \
 && mkdir -p /cache/hf /app && chown -R app:app /cache /app

RUN mkdir -p /opt/uv
RUN --network=none --mount=from=builder,source=/opt/uv/python,target=/tmp/uvpy \
    cp -a /tmp/uvpy /opt/uv/python
RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/cache/hf \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

USER app
WORKDIR /app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["runner"]
