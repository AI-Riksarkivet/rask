# syntax=docker/dockerfile:1.11
# check=skip=SecretsUsedInArgOrEnv
# Reason: HF_HUB_DISABLE_IMPLICIT_TOKEN=1 is a boolean flag that disables implicit
# HF token look-up at runtime, not a credential. False positive from the lint rule.
# rask ray-head image — Python 3.13 + Ray + PyTorch on nvidia/cuda:13.0.1-runtime-ubuntu24.04 (arm64 CUDA 13).
# Build:
#   docker buildx build -f .docker/ray.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t ray:dev .
# Runtime requirements (paired with this image — set in deploy manifest):
#   --shm-size >= 30% of RAM   (Ray object store; ray-project/ray#13619, #14535)
#   --ulimit nofile=65535      (Ray Serve FD limit; ray-project/ray#13045)
#   --read-only --tmpfs /tmp   (image is built to support this)
#   GPU device exposure        (nvidia-container-toolkit)

# ---- builder stage: uv-managed Python + uv sync ----------------------------
FROM nvidia/cuda:13.0.1-runtime-ubuntu24.04 AS builder

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
# dep would force constant base-image bumps. The CUDA base + nvidia's signed
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
FROM nvidia/cuda:13.0.1-runtime-ubuntu24.04

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-ray" \
      org.opencontainers.image.description="rask ray head + htrflow Serve"

ENV DEBIAN_FRONTEND=noninteractive
# base is an unpinned arm64 CUDA-13 tag; pin a digest once a known-good one is chosen.
# Reason: apt version-pinning these transitives would require chasing rolling updates.
# hadolint ignore=DL3008
# libgl1 libglib2.0-0 libxcb1: OpenCV (cv2, pulled in by htrflow) dlopen-links
# libGL.so.1, libglib-2.0.so.0/libgthread-2.0.so.0, and libxcb.so.1 — absent from
# the slim CUDA runtime base, so `import cv2` fails at replica init without them.
# wget: KubeRay's generated head readiness/liveness probes shell out to `wget`
# (raylet + gcs healthz); without it the head never goes Ready and the operator
# never submits the Serve config.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates tini libgomp1 curl wget \
      libgl1 libglib2.0-0 libxcb1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app \
 && mkdir -p /cache/hf /app && chown -R app:app /cache /app

RUN mkdir -p /opt/uv
RUN --network=none --mount=from=builder,source=/opt/uv/python,target=/tmp/uvpy \
    cp -a /tmp/uvpy /opt/uv/python
RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

COPY components/scripts/deploy_serve.py /app/deploy_serve.py
RUN chown app:app /app/deploy_serve.py

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
CMD ["bash", "-lc", "sleep infinity"]
