# syntax=docker/dockerfile:1.11
# check=skip=SecretsUsedInArgOrEnv
# Reason: HF_HUB_DISABLE_IMPLICIT_TOKEN=1 is a boolean flag that disables implicit
# HF token look-up at runtime, not a credential. False positive from the lint rule.
# rask ray-head image — Python 3.13 + Ray + PyTorch on nvidia/cuda:13.0.1-runtime-ubuntu24.04 (arm64 CUDA 13).
# Build:
#   docker buildx build -f .docker/ray-cluster.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t ray-cluster:dev .
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

# The HTR runner is a SEALED project (runners/htr), deliberately outside the root uv
# workspace: its model stack (torch/htrflow/ultralytics) must never enter the fleet's
# resolution. So it builds from its OWN lock, not the root one. `storage` comes along
# as a path dependency.
# Step 1: install deps (frozen — first-party sources not yet COPYed).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=runners/htr/uv.lock,target=runners/htr/uv.lock \
    --mount=type=bind,source=runners/htr/pyproject.toml,target=runners/htr/pyproject.toml \
    --mount=type=bind,source=packages/storage,target=packages/storage \
    uv sync --project runners/htr --frozen --no-install-project --no-editable

# Step 2: COPY sources (bind-mounts from Step 1 are gone), then resolve (locked).
COPY packages/storage packages/storage
COPY runners/htr    runners/htr
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project runners/htr --locked --no-editable

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
      org.opencontainers.image.title="rask-ray-cluster" \
      org.opencontainers.image.description="rask ray head + htrflow Serve"

# The runner's own provenance channel (#88): every ALTO this image produces stamps the commit that
# built it (htr.models.COMMIT_SHA -> the `build` Processing block). VCS_REF is already threaded by
# scripts/dagger-image.sh (git rev-parse HEAD) for the OCI label above — this reuses the same value
# rather than inventing a second build arg that could disagree with it. `unknown` or empty stamps
# NOTHING (the serializer's silence-is-honest rule), so a hand-built image without git degrades to
# an unstamped ALTO instead of a lying one.
ENV RASK_GIT_SHA=${VCS_REF}

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

COPY runners/htr/scripts/deploy_serve.py /app/deploy_serve.py
RUN chown app:app /app/deploy_serve.py

# The medallion Ray lane submits `python /home/ray/jobs/<job>.py` — the defaults of `ray_entrypoint`
# and `train_entrypoint` in services/medallion/src/medallion/core/config.py. `ray-lance.dockerfile`
# baked these for the SEPARATE ray-lance demo cluster; this is the image the chart's KubeRay cluster
# actually runs, and it did not, so every submitted stage job died with
#   python: can't open file '/home/ray/jobs/ray_stage_job.py': No such file or directory
# and exit code 2. The cascade's success path had therefore never once run — the failure looked like
# missing data rather than a missing file. Pinned by
# `test_the_ray_image_BAKES_every_job_script_the_medallion_entrypoints_name`, which reads the
# entrypoint defaults out of the config so the two halves cannot drift apart again.
#
# `--chown` rather than a following `RUN chown`: one layer instead of two, and the ownership is part
# of the copy rather than a correction to it.
COPY --chown=app:app scripts/ray_stage_job.py scripts/ray_train_job.py /home/ray/jobs/

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
