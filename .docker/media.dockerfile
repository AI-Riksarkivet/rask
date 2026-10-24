# syntax=docker/dockerfile:1.11
# The media image — the viewer and search services, built from the ROOT uv workspace (src-layout services
# installed as wheels; no PYTHONPATH, no raw source COPY in the final stage). The product chart's explorer runs
# both from it with a different command (chart/templates/explorer.yaml → `rask.image` "media"):
#   viewer.main:app (:8101) · search.main:app (:8102)
# — wrapped in `opentelemetry-instrument` when observability is on (opentelemetry-distro, which both declare).
# They are clients of the lakehouse, so they build apart from its image (.docker/rest-catalog.dockerfile): deleting
# them changes nothing the lakehouse image installs ([[XC-177]]).
# Build context = repo root:  scripts/dagger-image.sh --name media --tag media:dev

# ── builder: two-step uv sync against the ROOT uv.lock ───────────────────────
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# A TOOLCHAIN FOR THE ONE DEPENDENCY WITH NO ARM64 WHEEL. lance-graph (the viewer's graph door) publishes
# x86_64 wheels only, measured on PyPI 2026-10-13 for 0.5.0 to 0.5.4, so on an arm64 host uv builds it from
# its sdist. The crates' build scripts need a C/C++ compiler, cmake, protoc with its well-known types and the OpenSSL headers, and the
# sdist's Cargo.lock pins ethnum 1.5.2, which fails to compile on Rust 1.99 (E0512, measured 2026-10-13), so
# Rust is pinned rather than left for maturin to fetch at latest. Builder stage only: the final stage copies
# the venv and carries no toolchain, and the uv cache mount keeps the compiled wheel across builds.
ARG RUST_TOOLCHAIN=1.90.0
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gcc g++ libc6-dev make cmake pkg-config protobuf-compiler libprotobuf-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain "$RUST_TOOLCHAIN"

# Step 1: third-party deps only, from the lockfile (--frozen tolerates the absent workspace
# source trees; --no-install-workspace skips the members themselves). Cached until pyproject/uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --frozen --no-dev --no-install-workspace --no-editable \
        --package viewer --package search

# Step 2: COPY real sources and install the workspace members as wheels (--no-editable →
# they land in site-packages; the final stage needs only the venv).
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
        --package viewer --package search

# Strip residual setuid/setgid bits before the venv leaves the builder.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ── final: minimum runtime surface ───────────────────────────────────────────
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="media" \
      org.opencontainers.image.description="The media viewer and search services, clients of the lakehouse; one image, per-container uvicorn commands" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

# tini for correct PID 1 signal handling / zombie reaping. No `|| true` here — a failed apt/useradd
# MUST fail the build (else the image ships without tini and every container fails to start).
# hadolint ignore=DL3008  # Reason: tini has no stable version pin in apt on slim; pinning would break on next base image update.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
# The venv is root-owned and the app runs as 10001, so it is immutable to the account running it.
COPY --from=builder --link /opt/venv /opt/venv

# ── the import gate ──────────────────────────────────────────────────────────
# Import every module this image serves, against the venv the runtime stage ships. A missing
# DECLARED dependency is invisible to the workspace venv (a sibling member resolves it) and can
# only be seen here, where the deployable's own closure is the only one present. See the script.
COPY .docker/import-gate.py /tmp/import-gate.py
RUN --network=none /opt/venv/bin/python /tmp/import-gate.py viewer search client_kit && rm /tmp/import-gate.py

# Strip setuid/setgid bits from the whole shipped filesystem (base account tools passwd/chsh/... +
# anything in the venv) — no use in a container, residual privesc surface. Own RUN so its `|| true`
# (which only tolerates find matching nothing) cannot mask a real build failure above.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true

# NUMERIC USER (the `app` account is uid 10001) — k8s `runAsNonRoot: true` can only VERIFY non-root at
# admission when the image user is numeric; a name ("app") makes the kubelet reject the pod
# (CreateContainerConfigError). The chart's lance.securityContext relies on this.
USER 10001
EXPOSE 8101 8102

# Cheap socket-connect probe against the default CMD's port (avoids urllib's ssl/http.client
# cold-import cost). k8s owns real readiness via /readyz; this HEALTHCHECK is for a standalone run.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8101)); s.close()"]

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "viewer.main:app", "--host", "0.0.0.0", "--port", "8101"]
