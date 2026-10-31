# syntax=docker/dockerfile:1.11
# Lance lakehouse image — ONE image, many entrypoints, built from the ROOT uv workspace
# (src-layout services installed as wheels; no PYTHONPATH, no raw source COPY in the final stage).
# The chart runs every lakehouse container from this image with a different command
# (see chart/templates/{services,maintenance}.yaml → include "lance.catalogImage"):
#   catalog.main:app (:2333) · lineage.main:app (:8000) · maintenance.service:app (:8000)
# — optionally wrapped in `opentelemetry-instrument` (shipped via lineage's opentelemetry-distro).
# It installs and import-gates the lakehouse's services and nothing else: every client builds its own image
# (the media viewer and search: .docker/media.dockerfile; the annotator: .docker/annotator.dockerfile, ADR 0171), so
# deleting a client changes nothing this image builds or installs ([[XC-177]]).
# Build context = repo root:  scripts/dagger-image.sh --name rest-catalog --tag lance-rest-catalog:dev

# ── builder: two-step uv sync against the ROOT uv.lock ───────────────────────
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Step 1: third-party deps only, from the lockfile (--frozen tolerates the absent workspace
# source trees; --no-install-workspace skips the members themselves). The union of --package
# flags covers every service the chart runs from this image — cached until pyproject/uv.lock change.
# No toolchain precedes it: every package in this closure locks an arm64 and an x86_64 wheel (read from uv.lock
# 2026-10-24). lance-graph, the one with no arm64 wheel, is the viewer's and compiles in the media image.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --frozen --no-dev --no-install-workspace --no-editable \
        --package catalog --package lineage --package maintenance

# Step 2: COPY real sources and install the workspace members as wheels (--no-editable →
# they land in site-packages; the final stage needs only the venv).
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
        --package catalog --package lineage --package maintenance

# Strip residual setuid/setgid bits before the venv leaves the builder.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ── final: minimum runtime surface ───────────────────────────────────────────
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="lance-rest-catalog" \
      org.opencontainers.image.description="Lance lakehouse image — REST catalog + lineage + maintenance; one image, per-container uvicorn commands" \
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
# ONE THREAD FOR THE POOLS THAT SIZE THEMSELVES TO THE HOST, NOT TO THE QUOTA ([[LH-250]]): numpy's OpenBLAS and
# pyarrow's CPU pool both read OMP_NUM_THREADS, so it alone bounds both (OpenBLAS reads its own OPENBLAS_NUM_THREADS
# first, which nothing sets). Without it, each lakehouse process holds 19 idle threads that OpenBLAS starts at import,
# under a 1-CPU quota with 20 visible CPUs: read from the host's /proc on helm rev 18, 2026-10-31. Lance sizes its own
# pools to the quota and needs no variable (lance_docs/PROVENANCE.md § The thread pools). The pool gate below measures
# both pools.
ENV OMP_NUM_THREADS=1

WORKDIR /srv
# The venv is root-owned and the app runs as 10001, so it is immutable to the account running it.
COPY --from=builder --link /opt/venv /opt/venv
# FOUR FILES OUT OF `scripts/`, NAMED ONE BY ONE. Each is run by a chart workload, so each has to
# exist in an image; copying the whole directory would put every dev and ops script into a
# request-serving image for the sake of four. Each imports stdlib plus what the venv above already
# carries (`storage`, boto3/botocore, pydantic), so none adds a dependency, and no chart Job needs a
# third-party image that a registry could withdraw.
#
# INTO THE FINAL STAGE, not the builder: the builder hands over `/opt/venv` and nothing else, so a
# file staged there ships nowhere.
#
#   control_root_backup.py — the backup CronJob (`backups.controlRoot`).
#   pg_dump_upload.py      — the `backups.pgDump` CronJob. What it uploads is the lineage graph and
#                            the authorization store.
#   rustfs_admin.py        — the scoped-users hook (`object-store-scoped-users`): RustFS's native
#                            admin API, SigV4-signed with the root pair read from a mounted file.
#                            Its `derive` init container also runs in this image (sh + sha256sum).
COPY --link scripts/control_root_backup.py /srv/control_root_backup.py
COPY --link scripts/pg_dump_upload.py /srv/pg_dump_upload.py
COPY --link scripts/rustfs_admin.py /srv/rustfs_admin.py

# ── the import gate ──────────────────────────────────────────────────────────
# Import every module this image serves, against the venv the runtime stage ships. A missing
# DECLARED dependency is invisible to the workspace venv (a sibling member resolves it) and can
# only be seen here, where the deployable's own closure is the only one present. See the script.
# `--absent`: the client kit and lancedb (a client's dataset reader) never ship in the lakehouse image ([[XC-178]]).
COPY .docker/import-gate.py /tmp/import-gate.py
RUN --network=none /opt/venv/bin/python /tmp/import-gate.py catalog lineage maintenance --absent client_kit lancedb && rm /tmp/import-gate.py

# ── the pool gate ────────────────────────────────────────────────────────────
# Measure the pools OMP_NUM_THREADS bounds, under the environment this stage ships: a pool wider than it fails the build.
# Bind-mounted rather than copied, so the script ships in no layer. See the script.
RUN --network=none --mount=type=bind,source=.docker/pool-gate.py,target=/tmp/pool-gate.py \
    /opt/venv/bin/python /tmp/pool-gate.py

# Strip setuid/setgid bits from the whole shipped filesystem (base account tools passwd/chsh/... +
# anything in the venv) — no use in a container, residual privesc surface. Own RUN so its `|| true`
# (which only tolerates find matching nothing) cannot mask a real build failure above.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true

# NUMERIC USER (the `app` account is uid 10001) — k8s `runAsNonRoot: true` can only VERIFY non-root at
# admission when the image user is numeric; a name ("app") makes the kubelet reject the pod
# (CreateContainerConfigError). The chart's lance.securityContext relies on this.
USER 10001
# 2333 catalog · 8000 lineage/maintenance.
EXPOSE 2333 8000

# Cheap socket-connect probe against the default CMD's port (avoids urllib's ssl/http.client
# cold-import cost). k8s owns real readiness via /readyz; this HEALTHCHECK is for the compose demo.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',2333)); s.close()"]

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "catalog.main:app", "--host", "0.0.0.0", "--port", "2333"]
