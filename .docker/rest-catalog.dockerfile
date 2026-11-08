# syntax=docker/dockerfile:1.11
# Lance lakehouse image — ONE image, many entrypoints, built from the ROOT uv workspace
# (src-layout services installed as wheels; no PYTHONPATH, no raw source COPY in the final stage).
# The chart runs every lakehouse container from this image with a different command
# (see chart/templates/{services,maintenance}.yaml → include "lance.catalogImage"):
#   catalog.main:app (:2333) · lineage.main:app (:8000) · maintenance.service:app (:8000)
# — optionally wrapped in `opentelemetry-instrument` (shipped via lineage's opentelemetry-distro).
# It installs and import-gates the lakehouse's services and nothing else: the console is its own image
# (.docker/frontend.dockerfile), and a client builds its own in its own repository, so no client changes what this image
# builds or installs ([[XC-177]]).
# Build context = repo root:  scripts/dagger-image.sh --name rest-catalog --tag lance-rest-catalog:dev

# ── builder: two-step uv sync against the ROOT uv.lock ───────────────────────
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:6ff812e34a0e14b4b8bc9ddf25e77b3e16bac6e0c4d9b67641f40366fa382d4c AS builder

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
FROM python:3.13-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS final

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
# DEBIAN'S SECURITY FIXES ARE TAKEN HERE, not left to the next base bump ([[XC-184]]): Debian publishes a fix on
# trixie-security the day it ships, and the official Python image takes it only when it is rebuilt, so the pinned digest
# is the starting point and `apt-get upgrade` brings it current. Measured 2026-11-07: the pinned base and the newest
# python:3.13-slim-trixie both carried zlib1g 1:1.3.dfsg+really1.3.1-1+b1, two HIGH CVEs fixed in trixie-security's
# +deb13u1, which was the base's only pending upgrade. The nightly scan (.dagger/scan.go) gates what this stage ships.
# hadolint ignore=DL3008  # Reason: tini has no stable version pin in apt on slim; pinning would break on next base image update.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app

# The base image's own pip (with its vendored msgpack and urllib3) and ensurepip's bundled pip wheel are a package
# manager nothing here runs: uv builds the venv in the builder without seeding pip, and the lakehouse's locked closure
# holds no pip or setuptools. The trailing checks fail the build if a base bump leaves pip importable, or leaves a pip
# wheel or dist-info anywhere under /usr/local, which is what an image scanner reads.
RUN rm -rf /usr/local/lib/python3*/site-packages/pip /usr/local/lib/python3*/site-packages/pip-*.dist-info \
           /usr/local/lib/python3*/ensurepip /usr/local/bin/pip /usr/local/bin/pip3* \
    && ! python3 -c "import pip" 2>/dev/null \
    && test -z "$(find /usr/local \( -name 'pip-*.whl' -o -name 'pip-*.dist-info' \) -print -quit)"

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
# THREE FILES OUT OF `scripts/`, NAMED ONE BY ONE. Each is run by a chart workload, so each has to
# exist in an image; copying the whole directory would put every dev and ops script into a
# request-serving image for the sake of three. Each imports stdlib plus what the venv above already
# carries (`lakehouse.storage`, boto3/botocore, pydantic), so none adds a dependency, and no chart Job needs a
# third-party image that a registry could withdraw.
#
# INTO THE FINAL STAGE, not the builder: the builder hands over `/opt/venv` and nothing else, so a
# file staged there ships nowhere.
#
#   control_root_backup.py — the backup CronJob (`backups.controlRoot`).
#   pg_dump_upload.py      — the `backups.pgDump` CronJob. What it uploads is the authorization store.
#   rustfs_admin.py        — the provisioning hook (`object-store-provisioning`): RustFS's native
#                            admin API, SigV4-signed with the root pair read from a mounted file.
COPY --link scripts/control_root_backup.py /srv/control_root_backup.py
COPY --link scripts/pg_dump_upload.py /srv/pg_dump_upload.py
COPY --link scripts/rustfs_admin.py /srv/rustfs_admin.py

# ── the import gate ──────────────────────────────────────────────────────────
# Import every module this image serves, against the venv the runtime stage ships. A missing
# DECLARED dependency is invisible to the workspace venv (a sibling member resolves it) and can
# only be seen here, where the deployable's own closure is the only one present. See the script.
# `lakehouse` is gated beside the three services because the chart runs modules of it directly (the issuer mirror,
# the authorization model's writer), so a module no service imports still has to import here.
# `--absent`: lancedb (a client's dataset reader) never ships in the lakehouse image ([[XC-178]]).
COPY .docker/import-gate.py /tmp/import-gate.py
RUN --network=none /opt/venv/bin/python /tmp/import-gate.py catalog lineage maintenance lakehouse --absent lancedb && rm /tmp/import-gate.py

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
