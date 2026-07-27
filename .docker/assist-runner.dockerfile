# syntax=docker/dockerfile:1.11
# Assist runner — the annotator's interactive AI-assist model server (runners/assist), CPU-only.
# Real GroundingDINO-tiny (text-prompted detection) + SAM-ViT-base (box/point segmentation) behind
# the MEDIA_ASSIST_URL contract. Weights are BAKED at build (HF cache layout → /opt/models) and the
# runtime is HF_HUB_OFFLINE, so pods never reach the internet — the same reproducibility stance as
# the pinned uv.lock. Build context = repo root (RA/rask convention):
#   docker build -f .docker/assist-runner.dockerfile -t assist-runner:dev .

# ── builder: the runner's sealed venv (torch cpu wheels) + the baked model weights ────────────────
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# runners/assist is a sealed standalone env (NOT a member of the root workspace) with its own
# committed uv.lock — bind-mount just its two files and sync frozen (no project to install:
# package=false, the server is a single module COPY'd into the final stage).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=runners/assist/uv.lock,target=uv.lock \
    --mount=type=bind,source=runners/assist/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev

# Bake the two model snapshots into an HF-cache-layout directory that ships in the image. Model ids
# MUST match runners/assist/server.py Settings defaults (the chart does not override them).
# Transformers loads model.safetensors; the repos' legacy duplicates (.bin/.h5/.msgpack) are excluded
# — shipping them doubled the layer (verified: 2.5 GB → ~1.1 GB). The transient xet chunk cache is
# dropped (only hub/ is the load-bearing layout) and the cache is made world-readable: hf writes some
# metadata 600-root, which the runtime uid 10001 then can't read (harmless but noisy warnings).
RUN HF_HOME=/opt/models /opt/venv/bin/hf download IDEA-Research/grounding-dino-tiny \
        --exclude "*.bin" --exclude "*.h5" --exclude "*.msgpack" \
    && HF_HOME=/opt/models /opt/venv/bin/hf download facebook/sam-vit-base \
        --exclude "*.bin" --exclude "*.h5" --exclude "*.msgpack" \
    && rm -rf /opt/models/xet \
    && chmod -R a+rX /opt/models

# ── final: minimal runtime — venv + weights + the single server module, non-root, offline ─────────
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="assist-runner" \
      org.opencontainers.image.description="Interactive assist model server (GroundingDINO-tiny + SAM-ViT-base, CPU) for the annotator's MEDIA_ASSIST_URL seam" \
      org.opencontainers.image.source="https://github.com/Borg93/lance-ns" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"

# tini for PID 1 signal handling; libglib2.0-0 is opencv-python-headless's one non-bundled shared
# lib. No `|| true`: a failed apt/useradd must fail the build.
# Reason: the base is digest-pinned (the actual reproducibility anchor); trixie point-release
# version pins for tini/libglib churn and break rebuilds without adding determinism.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app

# HF_HUB_OFFLINE: from_pretrained resolves ONLY from the baked /opt/models cache — a missing weight
# is a hard start-up failure, never a silent runtime download. HOME=/tmp keeps the stray dot-dirs
# (torch/hf runtime scribbles) on the pod's writable tmp mount → read-only-rootfs ready.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    HOME=/tmp

WORKDIR /srv
COPY --from=builder --link /opt/venv /opt/venv
COPY --from=builder --link /opt/models /opt/models
COPY --link runners/assist/server.py ./

# Strip setuid/setgid bits from the shipped filesystem — no use in a container, residual privesc
# surface. Own RUN so its `|| true` (tolerating find matching nothing) can't mask a failure above.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true

# Numeric (not the name `app`): runAsNonRoot admission can only verify a numeric UID.
USER 10001
EXPOSE 8110
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "server:app", "--host=0.0.0.0", "--port=8110"]
