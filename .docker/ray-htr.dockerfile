# syntax=docker/dockerfile:1.11
# check=skip=SecretsUsedInArgOrEnv
# Reason: HF_HUB_DISABLE_IMPLICIT_TOKEN is a boolean flag, not a credential.
#
# The HTR WORKLOAD image — the platform cluster image plus one workload's sealed stack.
#
# Build:
#   dagger call image --name=ray-htr publish --address=<registry>/ray-htr:<tag>
#
# WHY THIS EXISTS AS A SEPARATE IMAGE. `ray-cluster` used to build straight from
# `runners/htr/uv.lock`, so every lane on the shared cluster carried torch, htrflow and
# ultralytics whether it wanted them or not — and the platform's own `lance` had to be bolted
# on afterwards with hand-matched pins, because the workload's lock contains no lance at all.
# That is the coupling CLAUDE.md's sealed-runner rule forbids: "a workload's awkward
# dependencies are ITS problem, isolated by Ray Data / Ray Serve runtime environments — never by
# fattening a shared image".
#
# So the split is: `ray-cluster` is the agnostic platform base (ray + pylance + lance-ray +
# pyarrow, from the ROOT lock, versions identical to the fleet by construction), and THIS image
# is that base plus `runners/htr`. A second workload is a second sibling dockerfile and changes
# nothing here.
#
# HOW THE CLUSTER USES IT. The Serve app declares it per-deployment:
#     runtime_env: { image_uri: "<registry>/ray-htr:<tag>" }
# (Ray 2.43+; verified 2.56.1 on this cluster). A KubeRay worker group running this image is the
# alternative when a deployment needs it as its node image rather than its runtime env.
ARG BASE_IMAGE=ray-cluster:dev

# ---- builder: the workload's sealed environment, from ITS OWN lock ----------
FROM ${BASE_IMAGE} AS htr-builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/htr-venv \
    DEBIAN_FRONTEND=noninteractive

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv

USER root
# git: htrflow is a git-source dependency. Builder only.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Its OWN lock, never the root one — that seal is the point of `runners/`.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=runners/htr/uv.lock,target=runners/htr/uv.lock \
    --mount=type=bind,source=runners/htr/pyproject.toml,target=runners/htr/pyproject.toml \
    --mount=type=bind,source=packages/storage,target=packages/storage \
    uv sync --project runners/htr --frozen --no-install-project --no-editable

COPY packages/storage packages/storage
COPY runners/htr    runners/htr
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project runners/htr --locked --no-editable

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final: the platform base + the workload venv ---------------------------
FROM ${BASE_IMAGE}

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-ray-htr" \
      org.opencontainers.image.description="rask ray cluster image + the HTR workload (Ray Serve)"

USER root
# OpenCV (via htrflow) dlopens libGL/libglib/libxcb, absent from the CUDA runtime base.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libxcb1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=htr-builder --chown=app:app /opt/htr-venv /opt/htr-venv
COPY --chown=app:app runners/htr/scripts/deploy_serve.py /app/deploy_serve.py

# The workload venv WINS on PATH: `import htrflow` and `import runner.*` resolve here, while the
# platform's own lance/ray stay importable from the base venv behind it.
ENV PATH=/opt/htr-venv/bin:$PATH

USER app
