# syntax=docker/dockerfile:1.11
# check=skip=SecretsUsedInArgOrEnv
# Reason: HF_HUB_DISABLE_IMPLICIT_TOKEN=1 is a boolean flag that disables implicit HF token
# look-up at runtime, not a credential. False positive from the lint rule.
#
# ONE WORKLOAD'S Ray image: the agnostic platform environment plus exactly one sealed runner.
#
# Build (RUNNER is the directory name under runners/):
#   dagger call runner-image --runner=htr publish --address=<registry>/ray-htr:<tag>
#   bash scripts/dagger-image.sh --runner htr --tag ray-htr:dev
#
# ── WHY THIS IS PARAMETRIZED AND NOT ONE DOCKERFILE PER WORKLOAD ───────────────────────────────────
# It replaces `.docker/ray-htr.dockerfile`, which named one workload and — measured 2026-08-25 —
# could never be built at all:
#
#     $ bash scripts/dagger-image.sh --name ray-htr --tag ray-htr:dev
#     ! failed to convert Dockerfile to LLB: ray-cluster:dev: pull access denied,
#       repository does not exist or may require authorization
#
# It opened `FROM ${BASE_IMAGE}` with `BASE_IMAGE=ray-cluster:dev`, a tag in the HOST daemon. Every
# build in this repo goes through `scripts/dagger-image.sh`, whose build runs inside the Dagger
# engine; BuildKit resolves that reference against a REGISTRY and there is no build-context option on
# Dagger's DockerBuild to hand it a locally-built container instead. So the image existed as a file,
# was referenced by no build target, and nothing was red.
#
# Being self-contained is the fix that travels with the code (CLAUDE.md): this builds anywhere the
# platform image builds — CI, a sandbox, a laptop with no registry — where a layered image is
# correct only where a registry has already been provisioned. The cost is that the platform half is
# expressed twice, here and in ray-cluster.dockerfile; that is deliberate, and the two are held
# together by tests/unit/test_invariants.py, which compares them rather than trusting them.
#
# `ARG RUNNER` follows the estate's own precedent for "one definition, N images":
# `.docker/frontend.dockerfile` builds all seven zones from `ARG APP`. A tenth runner is a new
# `--runner=` value and changes NO file here — which is what keeps the platform workload-agnostic.
#
# ── HOW A CLUSTER CONSUMES IT ─────────────────────────────────────────────────────────────────────
# Per Serve application, so several workloads share one cluster without sharing one environment:
#     serveConfigV2: applications[].runtime_env.image_uri: "<registry>/ray-<runner>:<tag>"
# `image_uri` names a whole prebuilt IMAGE (Ray 2.43+). That is not the rejected mechanism: the
# 2026-08-23 ruling refuses shipping DEPENDENCIES through runtime_env (`pip`/`uv` field resolving a
# stack at replica start). Handing a deployment an image built here is the baked-image answer, and it
# is what `chart/values.yaml`'s `serveApplications[].image` renders into.
#
# Only a runner that ships its own `uv.lock` can be built: `--locked` is what seals it. A runner
# without one has no reproducible environment to bake, and failing here is the honest outcome.
ARG RUNNER

# ---- platform builder: the AGNOSTIC compute environment, from the ROOT lock ----------------------
FROM nvidia/cuda:13.0.1-runtime-ubuntu24.04 AS platform-builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=auto \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    DEBIAN_FRONTEND=noninteractive

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv

# git: a runner may declare a git-source dependency. Builder only.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Identical to ray-cluster.dockerfile on purpose: this image and the fleet must resolve the SAME
# Lance/Ray versions, because a split between them is a correctness bug rather than a currency
# preference (measured: pylance 8.0.0 against a 9.0.0-written dataset made the row-aligned blob read
# path unusable and mis-detected payload presence SILENTLY).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --package ratch --frozen --no-install-project --no-editable

COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --package ratch --locked --no-editable

# ---- workload builder: ONE sealed runner, from ITS OWN lock --------------------------------------
FROM platform-builder AS workload-builder
ARG RUNNER

# A SECOND venv, never a second resolution into the first. The seal is the point of runners/: the
# workload's lock and the root lock disagree by design (a runner carries torch and no lance; the
# platform carries lance and no torch), so resolving them together is exactly the fattening
# CLAUDE.md refuses. Two environments, one PATH order, no shared resolution.
ENV UV_PROJECT_ENVIRONMENT=/opt/runner-venv

# `packages/storage` is a PATH dependency of every runner; COPY (not bind) because the ARG-expanded
# runner path below is resolved by COPY, and the two must land in the same WORKDIR layout.
COPY packages/storage packages/storage
COPY runners/${RUNNER} runners/${RUNNER}

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project runners/${RUNNER} --locked --no-editable

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final: CUDA runtime + BOTH environments -----------------------------------------------------
FROM nvidia/cuda:13.0.1-runtime-ubuntu24.04
ARG RUNNER

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-ray-runner" \
      org.opencontainers.image.description="rask ray platform environment + one sealed runner"

# The runner's own provenance channel (#88): output this image produces stamps the commit that built
# it. `unknown` or empty stamps NOTHING — silence is honest, a wrong sha is not.
ENV RASK_GIT_SHA=${VCS_REF}
# Which workload this image actually contains, readable from the image itself. The chart declares an
# import path and the image either provides it or does not; without this the only way to tell them
# apart is to run the image and try the import.
ENV RASK_RUNNER=${RUNNER}

ENV DEBIAN_FRONTEND=noninteractive
# libgl1/libglib2.0-0/libxcb1: OpenCV dlopen-links libGL.so.1, libglib-2.0.so.0 and libxcb.so.1,
# absent from the slim CUDA runtime base, so `import cv2` fails at replica init without them. Carried
# for every runner rather than gated on one: gating would need this file to know which workloads use
# OpenCV, which is precisely the knowledge a sealed runner does not hand the platform.
# wget: KubeRay's generated head probes shell out to it; without it the head never goes Ready and the
# operator never submits the Serve config.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates tini libgomp1 curl wget \
      libgl1 libglib2.0-0 libxcb1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app \
 && mkdir -p /cache/hf /app && chown -R app:app /cache /app

RUN mkdir -p /opt/uv
RUN --network=none --mount=from=platform-builder,source=/opt/uv/python,target=/tmp/uvpy \
    cp -a /tmp/uvpy /opt/uv/python
RUN --network=none --mount=from=platform-builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv
RUN --network=none --mount=from=workload-builder,source=/opt/runner-venv,target=/tmp/rvenv \
    cp -a /tmp/rvenv /opt/runner-venv \
 && chown -R app:app /opt/runner-venv

# NO JOB SCRIPTS ARE BAKED HERE, deliberately. `ray-cluster.dockerfile` bakes them because the
# CLUSTER runs them — `python /home/ray/jobs/ray_stage_job.py`, under an interpreter whose environment
# is the platform's and therefore has lance. This image's interpreter is the WORKLOAD's and does not
# (measured below), so a job script baked here would resolve to a python that cannot import lance and
# die `ModuleNotFoundError` instead of `can't open file` — a worse failure, because it names the
# module rather than the image. The union gate
# `test_every_ray_job_script_is_BAKED_INTO_SOME_image` is satisfied by the cluster image.

# The WORKLOAD venv wins on PATH, and that is the ONLY environment this image's default interpreter
# has. Measured in the built image 2026-08-25:
#
#     sys.executable                -> /opt/runner-venv/bin/python
#     import runner.htrflow_service -> OK
#     import lance                  -> ModuleNotFoundError
#
# `.docker/ray-htr.dockerfile` asserted the opposite — "the platform's own lance/ray stay importable
# from the base venv behind it" — and it was never true: two venvs are two interpreters, and putting
# one first on PATH does not make the other's site-packages visible. Nothing caught it because that
# image could not be built at all.
#
# It is left this way ON PURPOSE rather than bridged with a .pth. This image exists to back ONE Serve
# application through `runtime_env.image_uri`, and that application imports its own stack; the
# platform's Lance plane belongs to the cluster image, which runs the jobs that need it. Making both
# importable from one interpreter would merge two independently-resolved locks — the runner's pins
# against the root's — which is the collision the sealed-runner rule exists to prevent. /opt/venv is
# still COPYed in so the image can host a platform process explicitly
# (`/opt/venv/bin/python`), but nothing resolves there by accident.
ENV PATH=/opt/runner-venv/bin:$PATH \
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
