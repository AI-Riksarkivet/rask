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

# The PLATFORM compute environment, from the ROOT workspace lock.
#
# `packages/ratch` is the platform's own Ray+Lance package: ray[data,default], pylance, lance-ray,
# pyarrow and service-kit[lancekit]. Building from the root lock means this image and the fleet
# resolve the SAME versions by construction — a split between them is a correctness bug, not a
# currency preference (measured: pylance 8.0.0 against a 9.0.0-written dataset made the whole
# row-aligned blob read path unusable and mis-detected payload presence silently).
#
# NO WORKLOAD DEPENDENCIES LIVE HERE. This image previously built from `runners/htr/uv.lock`, so
# every lane inherited torch and htrflow whether it wanted them or not, and the platform's own
# `lance` had to be bolted on afterwards with hand-matched pins. A workload's deps belong to the
# workload (`.docker/ray-htr.dockerfile`, or a per-deployment `runtime_env`), never to the shared
# base — CLAUDE.md's sealed-runner ruling.
# Step 1: deps only (first-party sources not yet COPYed).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --package ratch --frozen --no-install-project --no-editable

# Step 2: COPY sources, then resolve.
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --package ratch --locked --no-editable

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
COPY --chown=app:app scripts/ray_stage_job.py scripts/ray_train_job.py scripts/ray_dummy_job.py /home/ray/jobs/

# The DUMMY runner's package, beside its job script — the estate's own end-to-end probe.
#
# `python /home/ray/jobs/ray_dummy_job.py` puts that directory on `sys.path[0]`, so
# `from dummy_runner.job import main` resolves with no PYTHONPATH edit and, more importantly, NO
# second dependency resolution. That distinction is what keeps this inside the "a workload's
# awkward dependencies are ITS problem, never a fattened shared image" ruling: `dummy_runner`
# imports pyarrow and lance and nothing else, and both are already here from the platform compute
# trio above. A `uv sync --project runners/dummy` would instead resolve a SECOND lock into this
# image and could quietly move pylance out from under the htr runner — the actual harm the ruling
# names. A source copy adds no resolvable dependency at all.
#
# It is baked rather than shipped via `runtime_env` for the same reason every other job here is:
# Ray documents runtime_env as development-only, and the whole point of a dummy lane is to exercise
# the PRODUCTION submission path with a transform that needs no GPU and no model download. A probe
# that ran differently from the thing it probes would be worth nothing.
COPY --chown=app:app runners/dummy/src/dummy_runner /home/ray/jobs/dummy_runner

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

# NUMERIC, not `app`. The kubelet compares runAsNonRoot against a NUMBER, so a named user is
# refused outright — `cannot verify user is non-root` — and the container never starts while its
# previous pod keeps serving, so the deployment silently stops being able to roll. Same uid the
# useradd above creates; only the spelling changes. Pinned by
# tests/unit/test_invariants.py::test_an_image_the_chart_hardens_declares_a_NUMERIC_user
USER 10001
WORKDIR /app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-lc", "sleep infinity"]
