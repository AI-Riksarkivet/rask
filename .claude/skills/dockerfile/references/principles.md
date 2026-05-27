# Dockerfile principles (rask)

Universal patterns that apply to every dockerfile in this repo. Loaded for any dockerfile change.

## Dockerfile syntax frontend

Always declare the BuildKit frontend as the very first line: `# syntax=docker/dockerfile:1.11` (or a newer pinned version). This is a parser directive — it must appear before any other content including blank lines. It enables BuildKit-only features (cache mounts, bind mounts, `--network=none`, etc.) and unlocks the `docker buildx build --check` linting command.

Run `docker buildx build --check .` during authoring to catch problems before a full build. The check flag activates two lints that matter here: `InvalidDefinitionDescription`, which fires when a `FROM` stage lacks the required stage-description comment above it, and `SecretsUsedInArgOrEnv`, which fires when a secret is passed via `ARG` or `ENV` rather than through a secret mount. Catching these at authoring time costs seconds; catching them in a CI pipeline costs minutes and a pipeline re-run.

```dockerfile
# syntax=docker/dockerfile:1.11

# ── base: minimal Python runtime ──────────────────────────────────────────────
FROM python:3.12-slim AS base

# ── builder: install dependencies ─────────────────────────────────────────────
FROM base AS builder
RUN pip install --no-cache-dir uv

# ── final: copy artefacts only ────────────────────────────────────────────────
FROM base AS final
COPY --from=builder /app /app
```

## Multi-stage discipline

Every stage in a multi-stage build must have a consistent, descriptive `AS <name>` alias. Use lowercase with hyphens (e.g., `AS base`, `AS builder`, `AS test`, `AS final`). The name must be consistent if other stages reference it with `COPY --from=<name>` — a rename is a breaking change. Keep stage names short but unambiguous.

Each `FROM` line must have a `# ──` comment immediately above it describing the stage's purpose (required by the `InvalidDefinitionDescription` lint in dockerfile syntax 1.11+). A minimal two-stage skeleton:

```dockerfile
# syntax=docker/dockerfile:1.11

# ── builder: compile and install ──────────────────────────────────────────────
FROM python:3.12-slim AS builder
RUN pip install uv
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ── final: runtime image ───────────────────────────────────────────────────────
FROM python:3.12-slim AS final
COPY --from=builder /app/.venv /app/.venv
```

## Layer cache order

Arrange `COPY` and `RUN` instructions so that the least-frequently-changing content comes first. The canonical order is: lockfile and metadata files first (e.g., `pyproject.toml`, `uv.lock`, `package.json`, `bun.lock`), application source code second, and any generated artefacts (compiled assets, migrations, etc.) last.

The reason is that Docker invalidates every layer below the first changed layer. If you copy source code before copying the lockfile, any source change busts the expensive dependency-install layer. Example of the wrong order causing an unnecessary cache bust:

```dockerfile
# BAD: source change busts the uv sync layer
COPY src/ ./src/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# GOOD: only a lockfile change busts the uv sync layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
COPY src/ ./src/
```

## `COPY --link` discipline

`COPY --link` creates the copy as an independent snapshot that can be merged with other layers without invalidating the cache chain beneath it. The win is real but narrow: use it on coarse inter-stage copies (e.g., `COPY --link --from=builder /app/.venv /app/.venv`) and on final-stage asset copies where the content is large and stable.

Do **not** apply `--link` to every `COPY` in a builder stage. Depot's measurements show that for small or frequently-changing files — the kind that appear in incremental dependency-cache patterns — `--link` adds overhead and can produce slower builds. The rule of thumb: if a `COPY` targets a path that changes on every build iteration (e.g., source files during development), skip `--link`. If it moves a large pre-built artefact between stages, use it.

```dockerfile
# DO: coarse inter-stage copy of a pre-built venv
COPY --link --from=builder /app/.venv /app/.venv

# DON'T: --link on every small dev-cycle copy
COPY --link pyproject.toml ./   # unnecessary; adds overhead with no benefit
```

## BuildKit cache mounts

Use `--mount=type=cache` to persist package-manager caches across builds without baking them into layers. The two standard targets in this repo are `/root/.cache/uv` for uv/pip and `/root/.bun/install/cache` for Bun. These directories are written during `RUN` but are not included in the resulting layer — they live only on the builder host and are reused on the next build.

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile
```

Never copy `/root/.cache/uv` into the final image or into a `COPY --from=builder` step. The cache mount is a build-time side-channel, not an artefact.

## BuildKit bind mounts for lockfiles

Use `--mount=type=bind` to make a lockfile available inside a `RUN` step without permanently copying it into that layer. The file is readable during the command but is not retained in the resulting layer. This keeps the layer lean and avoids a redundant `COPY` instruction when the lockfile is only needed during installation.

```dockerfile
RUN --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
```

The bind mount reads from the build context at `source=` and presents it at `target=` inside the container. Unlike `COPY`, no file is written to the layer filesystem.

## `RUN --network=none` on final-stage copies

After all dependencies are installed in the builder stage, the final assembly step (copying the venv or built assets into the runtime image) should run with `--network=none`. This prevents a compromised dependency or a rogue postinstall script from reaching the network at a point where it already has access to installed packages.

`--network=none` is a `RUN` decorator, not a `COPY` decorator. To honor the no-network guarantee on a final-stage copy from the builder, use a bind-mount from the builder stage inside a `RUN --network=none`:

```dockerfile
# ── final: runtime image ───────────────────────────────────────────────────────
FROM python:3.12-slim AS final
RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv
```

`COPY --from=builder` is the simpler alternative — it doesn't itself touch the network — but you lose the explicit guard against any inadvertent `RUN` slipping into the final stage with network access. The bind-mount pattern keeps the guarantee mechanical: every final-stage data movement is wrapped in a no-network `RUN`.

## `.dockerignore`

A `.dockerignore` file at the repo root limits what the build context sends to the daemon. Excluding irrelevant files speeds up context transfer and prevents secrets or build artifacts from accidentally entering the image via a broad `COPY . .`.

The canonical exclusion list for this repo (ships as `templates/dockerignore`, install via `cp .claude/skills/dockerfile/templates/dockerignore .dockerignore`):

```
.git
.venv
.venv*/
node_modules
node_modules/.cache
**/__pycache__
**/*.pyc
dist
build
.svelte-kit
.ruff_cache
.pytest_cache
.mypy_cache
coverage/
htmlcov/
*.egg-info/
*.dist-info/
.docker/
.dagger/
*.log
*.gguf
*.bin
*.pt
*.safetensors
.env*
.claude/
.cursor*
.idea/
.vscode/
.terraform/
.DS_Store
Dockerfile
*.dockerfile
```

`Dockerfile` and `*.dockerfile` are explicitly excluded so that a `COPY . .` instruction does not layer the dockerfile itself into the image. This matters for multi-dockerfile repos: the build context is the same regardless of which dockerfile is being built, so all dockerfile files would enter every image without this exclusion.

## Non-root final stage

The final image must not run as root. Create a dedicated system user with no home directory and the nologin shell, then switch to it before `CMD`:

```dockerfile
RUN useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app
USER app
CMD ["python", "-m", "myapp"]
```

The `-r` flag creates a system account (uid < 1000 by default, overridden here with `--uid 10001`). `--no-create-home` removes `/home/app`. `--shell /usr/sbin/nologin` matters even when `USER app` is set: if an attacker achieves a shell escape or the container is exec'd into interactively without an explicit shell argument, nologin ensures no usable shell session can be established. It is defense-in-depth beyond the `USER` directive.

## Setuid strip

At the end of the builder stage, remove setuid and setgid bits from all binaries:

```dockerfile
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true
```

`-xdev` prevents the search from crossing filesystem boundaries (important in containers where `/proc` and other pseudo-filesystems are mounted). The `|| true` prevents the build from failing if no setuid files are found or if a file is removed between `find` and `chmod`.

Even with `--no-install-recommends`, some apt packages still ship setuid binaries — `passwd`, `chsh`, `chfn`, and similar account-management tools are common examples. These binaries have no purpose in a container and represent residual privilege-escalation surface. Stripping them at build time is cheaper than auditing at deploy time.

## OCI labels

Every dockerfile must declare three `ARG` values and set the standard `org.opencontainers.image.*` labels. The `ARG` declarations make the dockerfile self-documenting about what the build system (Dagger) must supply at build time:

```dockerfile
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="<image-name>" \
      org.opencontainers.image.description="<one-line description>"
```

Replace `<image-name>` and `<one-line description>` with values specific to each image. `BUILD_DATE` should be an RFC 3339 timestamp (e.g., `2025-08-15T12:00:00Z`). `VCS_REF` is the full commit SHA. `VERSION` is the semver tag or `dev` for untagged builds.

## Read-only rootfs design

Design every final image to run cleanly under `--read-only --tmpfs /tmp` at deploy time. This means the image itself must pre-create any directory the application writes to at runtime, and those directories must either be mounted as tmpfs or declared as volumes.

For nginx-based frontends, rewrite the pid file and temp paths into `/tmp`:

```nginx
pid /tmp/nginx.pid;
client_body_temp_path /tmp/client_body;
proxy_temp_path /tmp/proxy;
fastcgi_temp_path /tmp/fastcgi;
```

For Python services, ensure log output goes to stdout/stderr (no file logging by default) and that any file writes use `/tmp` or an explicit volume. If the app absolutely needs a persistent writable path, declare it as `VOLUME /data` in the dockerfile so the requirement is visible.

## `tini` as PID 1

Python (and most application runtimes) are not designed to be PID 1. Install `tini` and use it as the entrypoint:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "myapp"]
```

PID 1 has two responsibilities that application code typically ignores: forwarding signals to child processes and reaping zombie processes. Without a proper init, `SIGTERM` from Docker stop may not reach the Python process, causing a 10-second grace period timeout on every deploy. Even for single-process Python apps with no child processes, tini ensures clean signal forwarding and eliminates the zombie-reaping concern if the app ever spawns a subprocess.

## `HEALTHCHECK` — lightweight idiom

Add a `HEALTHCHECK` only when no orchestrator probe (Kubernetes liveness/readiness, ECS health check) already owns readiness for this container. When a healthcheck is appropriate, always set `--start-period` to give the application time to initialize before the check is considered failing.

Avoid `python -c "import urllib.request, ..."` — `urllib.request` cold-imports `ssl` and `http.client`, adding 30-80 ms of overhead per probe. Prefer a raw socket connect, or `curl` if it is already present in the image:

```dockerfile
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8888))"
# Or if curl is in the image (frontend.dockerfile):
# HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
#   CMD curl --fail --silent --max-time 2 http://127.0.0.1:8080/ || exit 1
```

Pick one idiom across the project — do not mix socket-connect and curl healthchecks in different dockerfiles unless the images genuinely differ in available tools.

## Digest pinning + bump workflow

Pin every `FROM` to a digest, not a mutable tag. Inspect the current digest via:

```bash
docker buildx imagetools inspect python:3.12-slim
```

Before bumping a pinned digest: (a) refuse digests older than approximately 90 days unless the team explicitly approves carrying a stale base; (b) scan the new digest with Trivy, Grype, or Docker Scout before promoting it to the dockerfile.

The motivation goes beyond freshness. CVE-2024-3094 (the xz-utils backdoor) demonstrated that a poisoned package can ship in an otherwise-trusted image. Binarly's August 2025 follow-up research showed that affected images remained on Docker Hub even after the CVE was public. Digest pinning prevents silent tag updates from pulling in a poisoned snapshot — but pinning to a poisoned digest is equally dangerous. Scanning before every bump closes that gap.

## `--provenance=mode=max` + secret-mount discipline

When building with `--provenance=mode=max` (recommended for CI), BuildKit records the full values of all `ARG` declarations into the public SLSA attestation attached to the image. Any secret passed as an `ARG` becomes permanently readable in the attestation. This is the technical reason the `SecretsUsedInArgOrEnv` lint exists.

Secrets must enter via `--mount=type=secret`. The dockerfile reads the secret from the in-memory tmpfs at `/run/secrets/<id>` during the `RUN` step; the value never appears in any layer or attestation:

```dockerfile
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN=$(cat /run/secrets/hf_token) \
    python -m runner.fetch_models
```

```bash
docker buildx build --secret id=hf_token,src=$HOME/.cache/huggingface/token ...
```

The secret file at `src=` is read from the host at build time and is never written to the image filesystem or build cache.

Pair `--provenance=mode=max` with `--sbom=true` in CI so the attestation carries a software bill of materials alongside the build provenance; the two flags together are the SLSA-attestation-quality contract for rask images.

## BuildKit cache export for CI

Local cache mounts (type=cache) cover developer machines. CI runners are ephemeral and need cross-runner cache persistence. Two patterns:

For GitHub Actions with buildx ≥ v0.21.0 and BuildKit ≥ v0.20.0 (after the April 2025 API v2 migration):

```bash
docker buildx build --cache-to=type=gha --cache-from=type=gha ...
```

For other CI environments or registries:

```bash
docker buildx build \
  --cache-to=type=registry,ref=ghcr.io/ai-riksarkivet/rask:cache,mode=max \
  --cache-from=type=registry,ref=ghcr.io/ai-riksarkivet/rask:cache \
  ...
```

`mode=max` caches all intermediate layers, not just the final image layers — this is the correct setting for multi-stage builds where the builder stage is expensive. `mode=inline` embeds cache metadata into the image itself and is suitable only for local development or single-stage images.

## Reproducible builds (optional)

Buildx ≥ v0.10 automatically propagates the `SOURCE_DATE_EPOCH` environment variable from the host into the build, causing image timestamps to be clamped to that epoch. No dockerfile change is required. Set it in CI before the build step:

```bash
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) docker buildx build ...
```

Buildx ≥ v0.13 adds file-level timestamp rewrites via `--output type=image,name=...,rewrite-timestamp=true`. This makes the image byte-for-byte reproducible across builds from the same source tree. Again, no dockerfile change is needed — mention it in the CI snippet for projects that require bit-reproducibility for supply-chain attestation.

## Provenance (out of skill scope; pointer only)

If the project ever makes a formal SLSA provenance claim, use the correct level. Signing an image with `cosign` in the same build job is SLSA Build Level 1: the provenance is unforgeable in transit but the build itself is not isolated, so a compromised build environment could produce a valid-looking attestation. SLSA Build Level 3 requires a hermetic, isolated reusable workflow — use `slsa-framework/slsa-github-generator` for this. Do not claim SLSA-3 from an inline signing step.

## Hadolint rules

Run `hadolint` on every dockerfile. The rules most relevant to this repo:

| Rule | Meaning |
|------|---------|
| DL3007 | Do not use `latest` tag — pin to a specific version |
| DL3008 | Pin apt package versions (`apt-get install pkg=1.2.3`) |
| DL3009 | Delete apt lists after install (`rm -rf /var/lib/apt/lists/*`) |
| DL3015 | Avoid `apt-get install` without `--no-install-recommends` |
| DL3042 | Avoid `pip install` without `--no-cache-dir` |
| DL3059 | Consolidate consecutive `RUN` instructions |
| DL4006 | Set `SHELL ["/bin/bash", "-eo", "pipefail", "-c"]` when using bash features |

Configure `.hadolint.yaml` with `trustedRegistries` to suppress false positives for known-safe base images. Without this, hadolint will warn on `FROM nvidia/cuda:...` and `FROM nginxinc/nginx-unprivileged:...` because they are not `docker.io/library/*`. The template ships at `.claude/skills/dockerfile/templates/hadolint.yaml`.
