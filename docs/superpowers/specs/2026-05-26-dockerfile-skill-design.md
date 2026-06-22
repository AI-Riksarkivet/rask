# `dockerfile` skill — design

**Date:** 2026-05-26
**Status:** Approved for implementation
**Scope:** Project-local Claude Code skill at `.claude/skills/dockerfile/` that teaches future agents how to author production-grade dockerfiles for the rask monorepo. Dockerfile-only; the dagger Go SDK that consumes the files is out of scope.

## Motivation

`.docker/` and `.dagger/` are both empty stubs. Before either is filled in, the project needs a single source of truth for *how* containerized images are built: which base images, what cache strategy, what security hardening, what build-context contract. A skill makes that decision-tree mechanically applicable by future Claude sessions; a one-off doc would drift.

## Contract the skill enforces

- **File location & naming.** All dockerfiles live at `.docker/<image-name>.dockerfile` (repo root). One dockerfile per image; image name == filename stem.
- **Build context.** Always the repo root. COPY paths are repo-relative (`packages/htr`, `projects/viewer`, etc.).
- **`.dockerignore`.** A single shared file at repo root. No per-image ignore files.
- **Multi-stage.** Builder stage(s) carry toolchain; final stage is minimum runtime surface, non-root UID ≥ 10000, created with `useradd -r --no-create-home --shell /usr/sbin/nologin`.
- **Digest pinning.** Every `FROM` reference pinned by `@sha256:<digest>`, not a floating tag.
- **BuildKit cache mounts.** `--mount=type=cache` for `uv` and `bun` package caches; caches never ship in image layers.
- **PID 1.** `tini --` as ENTRYPOINT for Python processes (forks, signal forwarding). `nginx-unprivileged` already has its own init.
- **OCI labels.** Every dockerfile declares `ARG BUILD_DATE`, `ARG VCS_REF`, `ARG VERSION` and emits `org.opencontainers.image.{created,revision,version,source,title,description}` labels. Build system supplies the ARGs; dockerfile is self-documenting.
- **Read-only-rootfs ready.** Final image runs cleanly under `--read-only --tmpfs /tmp`. Any writable path needed at runtime is either `/tmp` or an explicitly mounted volume; nginx config rewrites pid/temp paths into `/tmp/`.
- **No build leakage.** Final image must not contain build toolchain (gcc, make), package managers (apt, the `uv` binary itself), `.git`, tests, or dev-dependencies.

## Skill layout

```
.claude/skills/dockerfile/
├── SKILL.md                          # Always-loaded entry. Frontmatter + level-2 body (~80 lines).
├── references/
│   ├── principles.md                 # Universal dockerfile patterns.
│   ├── python-uv.md                  # uv-in-Docker (viewer, runner shared concerns).
│   ├── gpu-cuda.md                   # NVIDIA CUDA base + Ray/PyTorch (runner-specific).
│   └── static-nginx.md               # bun build + nginx-unprivileged (frontend).
└── templates/
    ├── viewer.dockerfile             # FastAPI on python:3.13-slim-bookworm.
    ├── runner.dockerfile             # uv on nvidia/cuda:12.x-runtime-ubuntu22.04.
    ├── frontend.dockerfile           # oven/bun:1-debian → nginxinc/nginx-unprivileged:1.27-alpine.
    ├── frontend.nginx.conf           # SPA config: try_files fallback + /_app/immutable cache + tmp-path overrides for read-only rootfs.
    ├── dockerignore                  # Repo-root .dockerignore (renamed on install).
    └── hadolint.yaml                 # Repo-root .hadolint.yaml (renamed on install): rule set + trustedRegistries.
```

## SKILL.md content shape

Sections, in order:

1. **Frontmatter** (`name`, `description`). Description carries the enforcement contract verbatim — it's what surfaces in skill triggers.
2. **When to use this skill.** Three triggers: new image, modifying existing `.docker/*.dockerfile`, reviewing.
3. **Decision tree.** Python+GPU → `runner.dockerfile` + `gpu-cuda.md`; Python no-GPU → `viewer.dockerfile` + `python-uv.md`; static bundle → `frontend.dockerfile` + `static-nginx.md`.
4. **Hard rules** (10 items from the Contract section above, restated tersely as a numbered list).
5. **When to load each reference** (one line per reference).
6. **After authoring** — `hadolint` invocation; second-build cache-hit verification; digest-bump workflow.

## Reference file contents (summary; full content authored in implementation)

### `references/principles.md` — applies to every dockerfile

- **Syntax frontend.** First line is `# syntax=docker/dockerfile:1.11` (or newer). Run `docker buildx build --check .` as part of authoring — catches `SecretsUsedInArgOrEnv`, missing stage-description comments (`InvalidDefinitionDescription`), and other lints the runtime won't.
- Multi-stage discipline; `AS builder` consistent. Each `FROM` line gets a `#` comment above it describing the stage's purpose (required by `InvalidDefinitionDescription` in dockerfile 1.11).
- Layer cache order: lockfile + metadata first, sources second, generated artefacts last.
- `COPY --link` applied **only** to coarse inter-stage and final-stage asset copies; **not** on every dependency-cache COPY (measurements show it loses there).
- BuildKit cache mounts (`--mount=type=cache,target=/root/.cache/uv` and `target=/root/.bun/install/cache`).
- BuildKit bind mounts for lockfiles read but not retained (`--mount=type=bind,source=uv.lock,target=uv.lock`).
- `RUN --network=none` on the final venv/asset COPY stage — prevents a compromised mirror or transitive dep's `postinstall` from exfiltrating after everything should be on-disk.
- `.dockerignore` excludes: `.git`, `.venv`, `.venv*/`, `node_modules`, `node_modules/.cache`, `**/__pycache__`, `**/*.pyc`, `dist`, `build`, `.svelte-kit`, `.ruff_cache`, `.pytest_cache`, `.mypy_cache`, `coverage/`, `htmlcov/`, `*.egg-info/`, `*.dist-info/`, `.docker/`, `.dagger/`, `*.log`, model weights, `.env*`, `.claude/`, `.cursor*`, `.idea/`, `.vscode/`, `.terraform/`, `.DS_Store`, `Dockerfile`, `*.dockerfile`. (Excluding `Dockerfile*` prevents accidental `COPY . .` from layering the dockerfile itself into the image.)
- Non-root final stage; `useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app`; `USER app` before `CMD`. Beyond `USER`, denies shell + login as defense-in-depth.
- **Setuid strip.** Final builder step: `RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true`. Neutralizes residual privilege-escalation surface even when `--no-install-recommends` is set; some apt packages still pull setuid binaries (`chsh`, `passwd`).
- **OCI labels.** Each dockerfile declares `ARG BUILD_DATE`, `ARG VCS_REF`, `ARG VERSION` and sets `org.opencontainers.image.{created,revision,version,source,title,description}` labels. The build system (dagger) supplies the ARGs; the dockerfile is self-documenting about what it expects.
- **Read-only rootfs design.** Final image runs cleanly under `--read-only --tmpfs /tmp`. Any writable runtime path is `/tmp` or an explicit volume mount.
- `tini` as PID 1 for Python entrypoints.
- **`HEALTHCHECK` — lightweight idiom only.** Only when no orchestrator probe owns readiness; `--start-period` set. Avoid `python -c "import urllib.request,..."` — `urllib.request` cold-imports ssl/http.client (~30-80ms). Prefer raw socket connect: `CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',<PORT>))"` or `curl --fail --silent --max-time 2 http://127.0.0.1:<PORT>/health` if curl is in the image. Pick one idiom across the project — don't mix.
- **`--provenance=mode=max` + `--sbom=true` CI flags pair with secret-mount discipline.** `mode=max` records full `ARG` values into the public SLSA attestation. Any secret-as-ARG becomes public. This reinforces the rule: secrets enter via `--mount=type=secret`, never `ARG`.
- **BuildKit cache export for CI (build-invocation, not dockerfile).** Local cache mounts cover dev; CI needs cross-runner cache. Recommendation: `--cache-to=type=gha` on GitHub Actions (requires buildx ≥ v0.21.0 / BuildKit ≥ v0.20.0 after the April 2025 API v2 migration); `--cache-to=type=registry,ref=<registry>/<image>:cache,mode=max --cache-from=type=registry,ref=<registry>/<image>:cache` elsewhere. `mode=max` caches intermediate layers; `mode=inline` is dev-only.
- **Reproducible builds (optional, build-invocation only).** Buildx ≥ v0.10 auto-propagates `SOURCE_DATE_EPOCH` from host env to image timestamps. Buildx ≥ v0.13 adds `--output type=image,name=...,rewrite-timestamp=true` for file-level timestamp rewrites. No dockerfile change needed; mention in CI snippet for projects that care about bit-reproducibility.
- **Digest pinning + bump workflow.** Inspect via `docker buildx imagetools inspect <ref>`. Before bumping: (a) refuse digests older than ~90 days unless explicitly approved; (b) scan with Trivy or Grype or Docker Scout. **Reference:** CVE-2024-3094 (xz-utils backdoor) was still being detected in Docker Hub images by Binarly in August 2025 — digest pinning alone doesn't protect against pinning to a poisoned snapshot.
- **Provenance (out of skill scope; pointer only).** If the project ever claims SLSA provenance, claim the right level: cosign-sign in the same build job is SLSA-1 (forgeable). SLSA-3 requires an isolated reusable workflow (`slsa-framework/slsa-github-generator`).
- Hadolint rules to obey: DL3007 (no `latest`), DL3008 (pin apt versions), DL3009 (`apt-get clean`), DL3015 (`--no-install-recommends`), DL3042 (`pip --no-cache-dir`), DL3059 (consolidate `RUN`), DL4006 (`SHELL ["/bin/bash","-eo","pipefail","-c"]`). Configure `.hadolint.yaml` `trustedRegistries` so `nvidia/cuda` and `nginxinc/nginx-unprivileged` don't false-positive.

### `references/python-uv.md` — every Python image

- **Two-step `uv sync` with `--frozen` then `--locked`.** Step 1: `uv sync --frozen --no-install-workspace --package <name> --no-editable` (workspace members not yet copied → `--frozen` skips re-resolve). Step 2 after COPYing sources: `uv sync --locked --package <name> --no-editable` (verifies lockfile matches resolved deps). Rationale: `--locked` on step 1 fails because workspace member sources aren't present yet — multiple uv issues (#16758, #16200, #12984, #15459) document this exact Docker failure for workspace projects.
- `UV_LINK_MODE=copy`, `UV_COMPILE_BYTECODE=1`, `UV_PYTHON_DOWNLOADS` set per base image.
- **`UV_PROJECT_ENVIRONMENT=/opt/venv`** — relocate the venv out of `/app` so dev-compose bind-mounts can shadow `/app` without nuking the venv, and so the venv path is stable across stages.
- **`PYTHONDONTWRITEBYTECODE=1` caveat.** Set it for clarity, but understand: `UV_COMPILE_BYTECODE=1` already wrote the `.pyc` files at install time. `PYTHONDONTWRITEBYTECODE` only suppresses *runtime* recompilation of user code that uv didn't pre-compile (rare). Not contradictory; both are correct.
- Workspace handling for rask's `projects/<name>/pyproject.toml` shape — bind-mount the relevant pyproject set during first sync; COPY real sources for second sync.
- **arm64 cache-mount is load-bearing.** On `linux/arm64` builders (Apple silicon devs) where manylinux wheels are absent, the `--mount=type=cache,target=/root/.cache/uv` mount is the difference between "rebuilds in 30s" and "recompiles Rust/C deps from source on every build" (10+ minutes). Document this so the cache mount is never dropped to "simplify" a dockerfile.
- `htrflow` from git → keep `git` in builder only.
- `--no-editable` installs workspace member as wheel; final stage doesn't need source tree.
- Venv copied out via `COPY --from=builder --link /opt/venv /opt/venv`; PATH starts with `/opt/venv/bin`; no `uv` binary in final image.

### `references/gpu-cuda.md` — runner-class images

- CUDA variant matrix: `-base` / `-runtime` / `-devel`. Default `-runtime`; switch to `-devel` only if a wheel compiles CUDA C++ at install.
- uv-managed Python on the CUDA Ubuntu base (`UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_PYTHON_PREFERENCE=only-managed`), not deadsnakes PPA.
- System libs Ray + PyTorch actually need: `libgomp1`, `ca-certificates`, `tini`. Nothing else.
- **Model-weight strategy — prescribed default: runtime-download to `$HF_HOME=/cache/hf` (persistent volume).** Bake-at-build only when deploy target has no volume. Sidecar init-container is K8s territory and out of scope.
- **Pin `huggingface_hub>=0.24.7`** in `projects/runner/pyproject.toml`. Older versions hit a documented race condition where `.lock` files hang indefinitely with concurrent multi-process downloads (huggingface_hub #2543, #2038). Multi-Ray-worker setups trip this constantly.
- **Mount `$HF_HOME` on a local volume**, not CIFS/NFS. The HF lock-file mechanism deadlocks on networked filesystems.
- **`HF_HUB_ENABLE_HF_TRANSFER=1` for >500 MB/s downloads.** Trade-off: no progress bars and tail-end slowdowns can look like a hung download. Document the caveat so operators don't kill a healthy download.
- **`--mount=type=secret,id=hf_token` for gated weights.** Keeps the token out of layer history. Pattern: `RUN --mount=type=secret,id=hf_token HF_TOKEN=$(cat /run/secrets/hf_token) python -m runner.fetch_models`. Build invocation: `docker buildx build --secret id=hf_token,src=$HOME/.cache/huggingface/token ...`.
- Optional build-arg-gated CUDA smoke test (`python -c "import torch; assert torch.cuda.is_available()"`).
- **Thread-storm env vars baked as ENV defaults.** Without these, every Ray actor spawns `cpu_count()` OpenMP threads → 100s of threads contending. Bake into runner.dockerfile: `ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1`. **OpenBLAS must be set before `import numpy`** — only an ENV line in the dockerfile is reliable; a Python-side `os.environ` assignment is too late. Ray can still override per-actor via `runtime_env={"env_vars": {...}}`.
- **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` baked as ENV default.** Addresses CUDA memory fragmentation that OOMs long-running Ray Serve replicas after hours. **Caveat:** conflicts with NCCL VMM allocators in multi-GPU setups (pytorch/pytorch#165419). rask's Ray Serve replicas are single-GPU per `pipeline.py`, so it's safe — but document the caveat for anyone tempted to move to multi-GPU NCCL.
- **HF telemetry off by default for a government archive.** Bake `ENV HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1` into runner.dockerfile. The cross-ecosystem `DO_NOT_TRACK=1` also covers `gradio`, `datasets`, `diffusers` if any are added later. `HF_HUB_OFFLINE=1` is **runtime-only** (don't bake; image must still be able to fetch on first warm-up unless explicitly air-gapped).
- **Runtime config that pairs with this image (out of scope but document the pointer).** Ray containers need `--shm-size` ≥ 30% of RAM (or Ray silently degrades the object store to `/tmp`) and `--ulimit nofile=65535` (Ray Serve warns below 8192). The dockerfile can't set these; the deploy manifest must. References: ray-project/ray #13619, #14535, #13045, #16820.

### `references/static-nginx.md` — frontend-class images

- Rationale for nginx over FastAPI for static assets (sendfile, immutable headers, ~5 MB RAM).
- Builder: `oven/bun:1-debian`, cache mount on `/root/.bun/install/cache`, `bun install --frozen-lockfile`, `bun run build`. adapter-static output dir: `build/`.
- Build context is repo root; `bun install` runs from root (resolves bun workspace deps including `packages/ui`); build runs via `bun --cwd components/apps/frontend run build`.
- Runtime: `nginxinc/nginx-unprivileged:1.27-alpine` — listens on 8080, UID 101, PID file in `/tmp`, no `USER` directive needed.
- **Read-only rootfs nginx config.** Override `pid /tmp/nginx.pid;`, `client_body_temp_path /tmp/client_body;`, `proxy_temp_path /tmp/proxy;`, `fastcgi_temp_path /tmp/fastcgi;`, `uwsgi_temp_path /tmp/uwsgi;`, `scgi_temp_path /tmp/scgi;` so the image runs under `--read-only --tmpfs /tmp`.
- SPA nginx config essentials: `try_files $uri $uri.html $uri/index.html /index.html;` fallback; `/_app/immutable/` location with `Cache-Control: public, max-age=31536000, immutable`; root location `no-cache`; gzip on for text MIMEs.
- **`/_app/version.json` needs its own `Cache-Control: no-cache` override**, placed *above* the `/_app/immutable/` block. SvelteKit polls this file to detect deploys and trigger client reload — caching it as immutable breaks reload-on-deploy. References: sveltejs/kit #3194, #15150.
- **Block dotfiles except `/.well-known/`.** Add `location ~ /\.(?!well-known) { deny all; }` so accidental `.env`, `.git`, `.DS_Store` artefacts don't leak if the `.dockerignore` misses something. adapter-static legitimately emits `.well-known` for ACME and `.nojekyll`.
- **Brotli is non-trivial on nginx-unprivileged.** The official image doesn't ship `ngx_brotli`. Two paths: (a) accept gzip-only — enable `precompress: true` in `adapter-static` so `.gz` assets are pre-built, and use `gzip_static on;`; (b) build a layered image with brotli (e.g., `fholzer/docker-nginx-brotli` as base). The skill recommends (a) by default; (b) only if measured TTFB gains justify the maintenance cost of a derived base.
- **Svelte 5 CSP gotcha.** Svelte 5 still emits inline event handlers (`__e=event` on `<img>`, etc. — svelte #14014). A pure `strict-dynamic` CSP in nginx breaks them. Either use SvelteKit's `csp.directives` in `svelte.config.js` (lets the framework emit nonces/hashes per build) or include `'unsafe-hashes'` plus the specific hashes for those inline snippets in the nginx CSP header. The skill recommends the SvelteKit-side approach; the nginx config provides a CSP-friendly base but doesn't try to be the source of truth.
- HEALTHCHECK via wget (alpine ships it).
- Rationale for separate frontend container vs. serving from viewer: independent deploy lifecycles.

## Templates

Each template is a complete, working dockerfile (or config file). All `FROM` references digest-pinned at time of authoring; the bump workflow documented in `principles.md` covers updating them. All dockerfile templates start with `# syntax=docker/dockerfile:1.11`, include stage-description comments above each `FROM`, declare `ARG BUILD_DATE / VCS_REF / VERSION`, and emit the standard OCI label set.

### `templates/viewer.dockerfile`
- Base: `python:3.13-slim-bookworm@sha256:...`
- Builder: `uv` from `ghcr.io/astral-sh/uv` pinned; `UV_PROJECT_ENVIRONMENT=/opt/venv`; bind-mount `uv.lock` + relevant `pyproject.toml`s; `uv sync --frozen --no-install-workspace --package viewer --no-editable`; COPY sources; `uv sync --locked --package viewer --no-editable`. Setuid-strip at end of stage.
- Final: same slim base, `tini` only; `RUN --network=none` for `COPY --from=builder --link /opt/venv /opt/venv`; `useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app`; OCI labels emitted from ARGs; `EXPOSE 8888`.
- HEALTHCHECK uses the lightweight socket-connect idiom (no urllib import); template has a commented-out `curl --fail` variant for environments that ship curl.
- `ENTRYPOINT ["/usr/bin/tini","--"]`; `CMD ["uvicorn","viewer.app:app","--host","0.0.0.0","--port","8888","--proxy-headers","--forwarded-allow-ips","<set-to-nginx-CIDR-at-deploy-time>","--no-access-log","--loop","uvloop","--http","httptools"]`. **Do not set `--workers`** — orchestrator scales replicas. **Do not use `--forwarded-allow-ips=*`** — header spoofing risk; pass the actual nginx network CIDR via deploy config.

### `templates/runner.dockerfile`
- Base: `nvidia/cuda:12.4.0-runtime-ubuntu22.04@sha256:...`
- Builder: install `libgomp1 git tini ca-certificates`; `uv` binary; `UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_PYTHON_PREFERENCE=only-managed UV_PROJECT_ENVIRONMENT=/opt/venv`; two-step sync `--frozen` then `--locked` with `--package runner`; bind-mount lockfile + `projects/runner/pyproject.toml` + `packages/` + `components/`. Optional `--mount=type=secret,id=hf_token` step for gated model fetches. Setuid-strip at end of stage.
- Final: same runtime base, `libgomp1 tini ca-certificates`; `RUN --network=none` for `COPY --from=builder --link /opt/uv/python /opt/uv/python` and `/opt/venv`; PATH = `/opt/venv/bin:$PATH` (the `.venv/bin/python` shebang points back to uv-managed interpreter inside `/opt/uv/python`); non-root `app` user with `--no-create-home --shell /usr/sbin/nologin --uid 10001`; OCI labels.
- Runtime ENV defaults: `HF_HOME=/cache/hf HF_HUB_ENABLE_HF_TRANSFER=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`.
- HEALTHCHECK uses the lightweight socket-connect idiom (or omit entirely if running under K8s with readiness probes).
- `ENTRYPOINT ["/usr/bin/tini","--"]`; `CMD ["python","-m","runner"]`.

### `templates/frontend.dockerfile`
- Builder: `oven/bun:1-debian@sha256:...`; cache mount on `/root/.bun/install/cache`. To resolve bun workspaces, bind-mount **root `package.json` + root `bun.lock` + every workspace member's `package.json`** (`components/apps/frontend/package.json`, `packages/ui/package.json`); run `bun install --frozen-lockfile` from repo root. Then COPY `components/apps/frontend/` + `packages/ui/` sources; build via `bun --cwd components/apps/frontend run build`.
- Final: `nginxinc/nginx-unprivileged:1.27-alpine@sha256:...`; OCI labels emitted from ARGs; COPY `frontend.nginx.conf` → `/etc/nginx/conf.d/default.conf`; `RUN --network=none` for `COPY --from=builder --link build/ → /usr/share/nginx/html`; `EXPOSE 8080`; `HEALTHCHECK` via wget.

### `templates/frontend.nginx.conf`
- `listen 8080;`, `root /usr/share/nginx/html;`.
- Read-only-rootfs path overrides at top level: `pid /tmp/nginx.pid;`, `client_body_temp_path /tmp/client_body;`, `proxy_temp_path /tmp/proxy;`, `fastcgi_temp_path /tmp/fastcgi;`, `uwsgi_temp_path /tmp/uwsgi;`, `scgi_temp_path /tmp/scgi;`.
- gzip on for `text/css application/javascript application/json image/svg+xml`; `gzip_static on;` (adapter-static `precompress: true` emits `.gz`).
- `location = /_app/version.json { add_header Cache-Control "no-cache" always; }` — *above* the immutable block so SvelteKit's deploy-reload polling isn't broken by caching.
- `location ^~ /_app/immutable/ { add_header Cache-Control "public, max-age=31536000, immutable" always; try_files $uri =404; }`.
- `location ~ /\.(?!well-known) { deny all; }` — blocks accidental dotfile exposure.
- Root location → `try_files $uri $uri.html $uri/index.html /index.html;` + `Cache-Control: no-cache`.

### `templates/dockerignore`
- File ships under `templates/dockerignore` (without leading dot) to avoid being silently applied to the skill itself. The skill instructs the user to `cp` it to repo-root `.dockerignore` on install. Contents per the principles.md exclude list.

### `templates/hadolint.yaml`
- File ships under `templates/hadolint.yaml`; install instruction is `cp .claude/skills/dockerfile/templates/hadolint.yaml .hadolint.yaml`. Contents:
  - `ignored: []` (start empty; document the override-with-comment workflow).
  - `trustedRegistries: ["nvidia/cuda", "nginxinc/nginx-unprivileged", "ghcr.io", "docker.io/python", "docker.io/oven"]`.
  - `failure-threshold: warning`.
  - Override-comment workflow: when a rule must be ignored, use `# hadolint ignore=DLxxxx` on the offending line with a `# Reason: ...` comment immediately above. The skill instructs reviewers to gate on the reason being plausible.

## Considered alternatives

- **Distroless Python final stage.** Considered for the viewer (no shell, smallest CVE surface). Rejected — distroless `python3-debian12` ships Python 3.11; the 3.13 variant lives on `python3-debian13` (trixie), per distroless issues #1703 and #1409. If anyone revisits this decision, that's the right base — but pragmatic slim debugging wins for now.
- **Chainguard / Wolfi.** Considered for lower CVE surface. Rejected to avoid an extra registry dependency; revisit if Snyk findings warrant.
- **Alpine for Python.** Rejected — manylinux wheels (pydantic-core, watchfiles) ship glibc binaries; Alpine forces musl rebuilds.

## Out of scope

- `.dagger/main.go` Go module that consumes the dockerfiles.
- Image registry, tagging, signing, SBOM generation.
- Compose / Kubernetes / Helm manifests.
- Multi-arch builds (belongs in dagger code).
- Runtime config injection (env vars, secrets).

## Acceptance criteria

The skill is "done" when:

1. `.claude/skills/dockerfile/` exists with `SKILL.md`, four `references/*.md`, and six `templates/*` files (viewer/runner/frontend `.dockerfile`, `frontend.nginx.conf`, `dockerignore`, `hadolint.yaml`).
2. SKILL.md frontmatter `description` clearly states the trigger conditions and contract.
3. Each `.dockerfile` template starts with `# syntax=docker/dockerfile:1.11`, declares `ARG BUILD_DATE/VCS_REF/VERSION`, emits the standard OCI label set, and runs as a non-root user UID ≥ 10000 created with `--no-create-home --shell /usr/sbin/nologin`.
4. All three templates build successfully against the actual rask repo (verified by running `docker buildx build -f .docker/<name>.dockerfile --build-arg BUILD_DATE=... --build-arg VCS_REF=... --build-arg VERSION=... .` after copying the template into `.docker/`).
5. Each template's second build is dominated by cache hits (lockfile/source layer cache reuse verifiable in build output; uv/bun cache mounts persist).
6. `docker buildx build --check` passes on all three templates (catches `SecretsUsedInArgOrEnv`, `InvalidDefinitionDescription`, etc.).
7. `hadolint --config .hadolint.yaml .docker/<name>.dockerfile` passes on all three templates.
8. `dockerignore` and `hadolint.yaml` templates are installable to repo root via straight copy.
9. The skill is invoked successfully when a future Claude session is asked to "add a new image to .docker".

## Non-goals (deliberately deferred to future work)

- Automating template instantiation (a `scripts/new-dockerfile.sh` was offered and declined).
- Auto-copying `templates/dockerignore` to repo root — user does it deliberately.
- Adding `hadolint` to `make check` — flagged but not part of this skill's scope.

## References cited in the skill body

**Authoritative docs**
- [Docker Build best practices](https://docs.docker.com/build/building/best-practices/)
- [Dockerfile reference](https://docs.docker.com/reference/dockerfile/)
- [Dockerfile syntax release notes (frontend 1.11+)](https://docs.docker.com/build/dockerfile/release-notes/)
- [uv: Using uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/)
- [uv: Installing Python](https://docs.astral.sh/uv/guides/install-python/)
- [nvidia/cuda Docker Hub](https://hub.docker.com/r/nvidia/cuda)
- [Ray installation](https://docs.ray.io/en/latest/ray-overview/installation.html)
- [nginx-unprivileged](https://github.com/nginx/docker-nginx-unprivileged)
- [Hadolint rules](https://github.com/hadolint/hadolint/wiki)
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html)
- [Yelp/dumb-init](https://github.com/Yelp/dumb-init) + [Peter Malmgren: PID 1](https://petermalmgren.com/signal-handling-docker/)

**Workspace + production gotchas**
- uv issues: [#16758](https://github.com/astral-sh/uv/issues/16758), [#16200](https://github.com/astral-sh/uv/issues/16200), [#12984](https://github.com/astral-sh/uv/issues/12984), [#15459](https://github.com/astral-sh/uv/issues/15459) — `--locked` vs `--frozen` in Docker workspace builds.
- Ray container sizing: [#13619](https://github.com/ray-project/ray/issues/13619), [#14535](https://github.com/ray-project/ray/issues/14535), [#13045](https://github.com/ray-project/ray/issues/13045), [#16820](https://github.com/ray-project/ray/issues/16820) — `--shm-size` + `--ulimit nofile`.
- HF cache race: [huggingface_hub #2543](https://github.com/huggingface/huggingface_hub/issues/2543), [#2038](https://github.com/huggingface/huggingface_hub/issues/2038), [v0.24.7 release](https://github.com/huggingface/huggingface_hub/releases/tag/v0.24.7).
- [hf_transfer](https://github.com/huggingface/hf_transfer) — speed/visibility trade-off.
- SvelteKit static + nginx: [kit#15150](https://github.com/sveltejs/kit/issues/15150), [kit#3194 (version.json)](https://github.com/sveltejs/kit/issues/3194), [svelte#14014 (inline event handlers)](https://github.com/sveltejs/svelte/issues/14014).

**Runtime tuning + observability**
- [Ray scheduling: resources](https://docs.ray.io/en/latest/ray-core/scheduling/resources.html) + Ray PR #6998 — `OMP_NUM_THREADS=1` thread-storm defaults.
- pytorch/pytorch [#119547](https://github.com/pytorch/pytorch/issues/119547), [#165419](https://github.com/pytorch/pytorch/issues/165419) — `expandable_segments:True` for long-running CUDA processes and NCCL caveat.
- [HuggingFace env vars](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables) — telemetry/offline/transfer toggles.
- [uvicorn deployment](https://www.uvicorn.org/deployment/) — `--proxy-headers`, `--forwarded-allow-ips`, `--workers` guidance.

**CI / build invocation (paired with the dockerfiles)**
- [Docker BuildKit cache backends](https://docs.docker.com/build/cache/backends/) — `type=gha`, `type=registry`, `mode=max`.
- [Docker BuildKit cache for GitHub Actions](https://docs.docker.com/build/ci/github-actions/cache/) — April 2025 v2 API migration.
- [Docker attestations: SLSA provenance](https://docs.docker.com/build/metadata/attestations/slsa-provenance/) — `--provenance=mode=max` ARG-secrets gotcha.
- [moby/buildkit build-repro](https://github.com/moby/buildkit/blob/master/docs/build-repro.md) — `SOURCE_DATE_EPOCH`, `rewrite-timestamp=true`.

**Considered-alternative sources**
- distroless [#1703](https://github.com/GoogleContainerTools/distroless/issues/1703), [#1409](https://github.com/GoogleContainerTools/distroless/issues/1409) — Python 3.13 lives on `-debian13`, not `-debian12`.

**Supply chain + tradeoffs**
- [CVE-2024-3094 (xz-utils backdoor)](https://en.wikipedia.org/wiki/XZ_Utils_backdoor) + [Binarly Aug 2025 follow-up](https://thehackernews.com/2025/08/researchers-spot-xz-utils-backdoor-in.html).
- [SLSA Level 3 via slsa-github-generator](https://github.com/slsa-framework/slsa-github-generator).
- [Ubuntu: 60% size reduction with --no-install-recommends](https://ubuntu.com/blog/we-reduced-our-docker-images-by-60-with-no-install-recommends).
- [Docker blog: Advanced Dockerfiles & BuildKit](https://www.docker.com/blog/advanced-dockerfiles-faster-builds-and-smaller-images-using-buildkit-and-multistage-builds/).
- [Depot: Why you should avoid COPY --link](https://depot.dev/blog/why-you-should-avoid-copy-link-in-your-dockerfile) — the contrarian case for the COPY --link tradeoff.
- [Red Hat: PyTorch + NVIDIA containers](https://next.redhat.com/2025/08/26/a-developers-guide-to-pytorch-containers-and-nvidia-solving-the-puzzle/) — runtime vs devel CUDA variant.
