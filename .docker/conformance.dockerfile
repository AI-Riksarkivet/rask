# The spec-conformance suite, runnable INSIDE the cluster ([[LH-020]]).
#
# WHY AN IMAGE AT ALL, when `make e2e-spec-conformance` already runs from the host: the catalog vends
# its own in-cluster address (`http://rask-minio:9000`), so a host-side client gets a correct 900s
# credential for a host it cannot resolve. Measured 2026-09-19 — the k3s service network IS routable
# from this host (`10.43.44.177` answers), and `rask-minio` does NOT resolve on it. So the barrier is
# DNS, not routing, and it is a property of where the process runs rather than of the clients. The
# lancedb and lance-ray cases therefore SKIP from the host with that reason, and the row's closing
# condition says PASSES — a skip that reads as green is the defect.
#
# Owner ruling 2026-09-19 (`docs/DECISIONS.md`): the conformance target runs from inside the cluster.
# Whether an external client should receive an externally-resolvable endpoint is a product question
# with its own row; this image answers the test-coverage half and makes no claim about the other.
#
# THE WHOLE ROOT ENVIRONMENT, from the root lock, and that is deliberate rather than lazy. The suite
# needs all three stock clients plus pytest, and they come from two different places: `lancedb`,
# `pylance`, `lance_namespace`, `pytest` and `requests` from the root dev group, `lance-ray` and `ray`
# from `packages/ray-cluster-env` (the deps-only platform-env member). A narrower sync would have to
# name both and would drift from what `make e2e-spec-conformance` runs on a developer's machine — and
# the point of a conformance suite is that the clients are STOCK, so installing them differently here
# than there would be testing a different thing. `--all-packages` with dev is exactly the host env.
#
# NOT A DEPLOYABLE. It is never rendered by the chart and runs only as a Job driven by
# `make e2e-spec-conformance-incluster`, which is why it carries the tests and no service entrypoint.

FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:7820aa74c8a3147ab13553c127432656969548971fbe350ba46a975b59dd42b2 AS builder
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
# Dependencies before sources, so editing a test does not re-resolve the environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=services,target=services \
    uv sync --frozen --all-packages --no-install-workspace --no-editable
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-packages --no-editable
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS final
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="rask-conformance" \
      org.opencontainers.image.description="Spec-conformance suite (lance_namespace + lancedb + lance-ray) run as an in-cluster Job" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"
RUN useradd -r -u 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# `pyproject.toml` carries the pytest config the suite runs under — the `spec_conformance` marker, the
# importlib import mode and the explicit testpaths. Shipping the sources without it would run the suite
# under pytest's defaults, which is a different runner than the one the row's claim is about.
COPY pyproject.toml conftest.py ./
COPY tests/e2e-py tests/e2e-py
# RAY WRITES, and this container runs read-only with a writable /tmp. `ray.init(address="local")` needs
# a session directory; `_temp_dir` is passed by the suite, and TMPDIR keeps anything else that
# scribbles inside the one writable mount.
ENV PATH="/opt/venv/bin:${PATH}" \
    TMPDIR=/tmp \
    PYTHONUNBUFFERED=1
USER 10001
# `-p no:cacheprovider`: the source tree is read-only, and pytest's cache write is a hard error there.
ENTRYPOINT ["pytest", "tests/e2e-py", "-m", "spec_conformance", "-v", "-p", "no:cacheprovider"]
