# Dockerfile Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Author `.claude/skills/dockerfile/` — a project-local skill that teaches future Claude sessions how to write production-grade dockerfiles for rask's three deployables (viewer, runner, frontend), placed at `.docker/<name>.dockerfile`, consumed by the dagger build system.

**Architecture:** Single skill directory with `SKILL.md` entry, 4 reference markdown files (principles, python-uv, gpu-cuda, static-nginx), and 6 templates (3 dockerfiles, nginx.conf, dockerignore, hadolint.yaml). Templates are TDD-verified via `docker buildx build`, `docker buildx build --check`, and `hadolint`. Reference markdown is verified by self-review against the spec at `docs/superpowers/specs/2026-05-26-dockerfile-skill-design.md`.

**Tech Stack:** Markdown (skill content), Dockerfile syntax 1.11, hadolint 2.14, docker buildx 0.31.

**Spec:** `docs/superpowers/specs/2026-05-26-dockerfile-skill-design.md` — the source of truth. The plan references it heavily rather than duplicating its bullet content. Implementers must read the relevant spec section before writing each file.

**Pre-flight check (do once before starting):**

```bash
command -v docker >/dev/null && docker version --format '{{.Client.Version}}' || echo "docker required"
docker buildx version
command -v hadolint >/dev/null && hadolint --version || echo "hadolint required"
test -d /home/morgan/rask && cd /home/morgan/rask || echo "wrong repo"
test -f docs/superpowers/specs/2026-05-26-dockerfile-skill-design.md || echo "spec missing"
```

Expected: docker ≥ 27, buildx ≥ 0.20, hadolint ≥ 2.14, spec file present.

---

## Task 1: Scaffold skill directory + minimal SKILL.md

**Files:**
- Create: `.claude/skills/dockerfile/SKILL.md`
- Create: `.claude/skills/dockerfile/references/` (directory)
- Create: `.claude/skills/dockerfile/templates/` (directory)

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p .claude/skills/dockerfile/references .claude/skills/dockerfile/templates
ls .claude/skills/dockerfile/
```

Expected: lists `references` and `templates` directories.

- [ ] **Step 2: Write SKILL.md**

Write `.claude/skills/dockerfile/SKILL.md` with the structure defined in the spec section **"SKILL.md content shape"** (sections 1-6). The full file:

````markdown
---
name: dockerfile
description: Author production dockerfiles for the rask monorepo. Use when adding a new containerized image, modifying .docker/*.dockerfile, debugging a slow/large build, or reviewing a dockerfile for security and cache efficiency. Enforces the .docker/<name>.dockerfile + repo-root build-context contract consumed by the dagger build system.
---

# dockerfile (rask)

## When to use this skill

- Creating a new image → start from `templates/<closest-match>.dockerfile`.
- Modifying an existing `.docker/*.dockerfile`.
- Reviewing a dockerfile for size, security, or cache efficiency.
- Debugging a slow or bloated container build.

## Decision tree

```
Workload?
├── Python + GPU (Ray, PyTorch, CUDA)  → templates/runner.dockerfile  → references/gpu-cuda.md
├── Python, no GPU (FastAPI, CLI)      → templates/viewer.dockerfile  → references/python-uv.md
└── Static bundle (SvelteKit, Vite)    → templates/frontend.dockerfile → references/static-nginx.md
```

Always also load `references/principles.md` — applies to every dockerfile.

## Hard rules

1. **File path:** `.docker/<image-name>.dockerfile` at repo root. Build context is **always the repo root**.
2. **Single `.dockerignore`** at repo root. No per-image ignore files. Install: `cp .claude/skills/dockerfile/templates/dockerignore .dockerignore`.
3. **Multi-stage.** Final stage = minimum runtime surface, non-root UID ≥ 10000, created with `useradd -r --no-create-home --shell /usr/sbin/nologin`.
4. **Digest-pinned `FROM`.** Every base image referenced by `@sha256:<digest>`, not a floating tag. Bump workflow in `references/principles.md`.
5. **BuildKit cache mounts.** `--mount=type=cache,target=/root/.cache/uv` and `target=/root/.bun/install/cache`. Caches never ship in image layers.
6. **PID 1.** `tini --` as ENTRYPOINT for Python processes. `nginx-unprivileged` already has its own init.
7. **OCI labels.** Declare `ARG BUILD_DATE`, `ARG VCS_REF`, `ARG VERSION` and emit `org.opencontainers.image.{created,revision,version,source,title,description}` labels.
8. **Read-only-rootfs ready.** Final image runs cleanly under `--read-only --tmpfs /tmp`. Writable paths are `/tmp` or explicit volumes.
9. **Secrets via `--mount=type=secret`, never `ARG`.** With `--provenance=mode=max` (SLSA), ARG values become public in the attestation.
10. **No build leakage.** Final image must not contain build toolchain (gcc, make), package managers (apt, the `uv` binary), `.git`, tests, or dev-dependencies.

## When to load each reference

- `references/principles.md` — **every** dockerfile change. Cache, layer ordering, COPY --link tradeoff, HEALTHCHECK, hadolint, setuid-strip, OCI labels, CVE-2024-3094 bump-guard, CI cache export.
- `references/python-uv.md` — any Python image. uv two-step `--frozen`/`--locked` sync, `UV_PROJECT_ENVIRONMENT=/opt/venv`, workspace handling, arm64 cache-mount note.
- `references/gpu-cuda.md` — only when the image needs CUDA. Runtime vs devel, uv-managed Python on Ubuntu base, HF telemetry/transfer/secret patterns, thread-storm + PYTORCH_CUDA_ALLOC_CONF ENV defaults.
- `references/static-nginx.md` — only when serving static assets. bun build, nginx-unprivileged config, SPA fallback, /_app/version.json override, dotfile block, Svelte 5 CSP gotcha.

## After authoring

1. Pin the syntax frontend: first line is `# syntax=docker/dockerfile:1.11`.
2. Run `docker buildx build --check -f .docker/<name>.dockerfile .` — catches `SecretsUsedInArgOrEnv`, missing stage-description comments.
3. Run `hadolint --config .hadolint.yaml .docker/<name>.dockerfile`. CI gates on this.
4. Build twice: `docker buildx build -f .docker/<name>.dockerfile --build-arg BUILD_DATE=$(date -u +%FT%TZ) --build-arg VCS_REF=$(git rev-parse HEAD) --build-arg VERSION=$(git describe --always) -t <name>:dev .`. The second build should be dominated by `CACHED` layers — that confirms the cache mount + bind mount + COPY-order discipline are correct.
5. When bumping a base image: `docker buildx imagetools inspect <ref>` → record digest. **Refuse digests older than ~90 days** unless explicitly approved; scan with Trivy/Grype/Docker Scout (CVE-2024-3094 was still found in pinned images in mid-2025).
````

- [ ] **Step 3: Verify SKILL.md is well-formed**

```bash
head -5 .claude/skills/dockerfile/SKILL.md
```

Expected: starts with `---`, contains `name: dockerfile`, `description:` field is one line and starts with "Author production dockerfiles". Frontmatter closes with `---` on line 4.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/dockerfile/SKILL.md
git commit -m "feat(skill): scaffold dockerfile skill with SKILL.md entry"
```

---

## Task 2: Write `references/principles.md`

**Files:**
- Create: `.claude/skills/dockerfile/references/principles.md`

**Source bullets:** Spec section **"`references/principles.md` — applies to every dockerfile"** (15 bullets). Each bullet must be expanded into 1-2 paragraphs of prose. The reference is the canonical detail; SKILL.md only summarizes.

- [ ] **Step 1: Re-read the spec section**

```bash
sed -n '/^### `references\/principles.md`/,/^### `references\/python-uv.md`/p' docs/superpowers/specs/2026-05-26-dockerfile-skill-design.md
```

This is your content brief. Every bullet becomes a section heading.

- [ ] **Step 2: Write the file**

Use this skeleton; each `## <heading>` corresponds to one spec bullet, expanded into prose with concrete examples:

```markdown
# Dockerfile principles (rask)

Universal patterns that apply to every dockerfile in this repo. Loaded for any dockerfile change.

## Dockerfile syntax frontend
[expand bullet: # syntax=docker/dockerfile:1.11, what InvalidDefinitionDescription catches, what SecretsUsedInArgOrEnv catches, the `docker buildx build --check` invocation. Include a 5-line example with stage-description comment.]

## Multi-stage discipline
[expand bullet: `AS builder` consistent across stages, stage-description comment required above each FROM. Include a minimal 2-stage skeleton.]

## Layer cache order
[expand bullet: lockfile + metadata first, sources second, generated artefacts last. Show a concrete example where reversing the order causes a cache bust.]

## `COPY --link` discipline
[expand bullet: when it wins (coarse inter-stage), when it loses (small frequent COPYs in builder). Cite Depot's measurement. Show a do/don't example.]

## BuildKit cache mounts
[expand bullet: --mount=type=cache,target=/root/.cache/uv and target=/root/.bun/install/cache; never persist in layer. Show the exact RUN line.]

## BuildKit bind mounts for lockfiles
[expand bullet: --mount=type=bind,source=uv.lock,target=uv.lock; lockfile read but not retained.]

## `RUN --network=none` on final-stage copies
[expand bullet: prevents postinstall/transitive exfiltration after deps are on-disk. Show the COPY-from-builder pattern with --network=none.]

## `.dockerignore`
[expand bullet: full exclusion list from spec line 65, with the rationale for excluding `Dockerfile` / `*.dockerfile` themselves. Note that the canonical .dockerignore ships as `templates/dockerignore`; install via `cp`.]

## Non-root final stage
[expand bullet: useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app; USER app before CMD. Why nologin shell matters even with USER set.]

## Setuid strip
[expand bullet: `RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + || true` at end of builder stage. Why some apt packages still ship setuid binaries even with --no-install-recommends.]

## OCI labels
[expand bullet: ARG BUILD_DATE / VCS_REF / VERSION declarations and the standard label set. Include the exact label snippet for copy-paste:]

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

## Read-only rootfs design
[expand bullet: --read-only --tmpfs /tmp at deploy time; image pre-creates writable dirs; nginx config rewrites pid/temp paths into /tmp.]

## `tini` as PID 1
[expand bullet: signal forwarding, zombie reaping, why even single-process Python apps benefit from it.]

## `HEALTHCHECK` — lightweight idiom
[expand bullet: only when no orchestrator probe owns readiness; --start-period; raw socket connect not urllib import. Include both the socket-connect and curl variants:]

```dockerfile
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8888))"
# Or if curl is in the image (frontend.dockerfile):
# HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
#   CMD curl --fail --silent --max-time 2 http://127.0.0.1:8080/ || exit 1
```

## Digest pinning + bump workflow
[expand bullet: docker buildx imagetools inspect; 90-day freshness check; Trivy/Grype/Scout scan; cite CVE-2024-3094 + Binarly Aug 2025 follow-up as the why.]

## `--provenance=mode=max` + secret-mount discipline
[expand bullet: ARG values become public in SLSA attestation; secrets must enter via --mount=type=secret. Show the secret-mount pattern:]

```dockerfile
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN=$(cat /run/secrets/hf_token) \
    python -m runner.fetch_models
```

```bash
docker buildx build --secret id=hf_token,src=$HOME/.cache/huggingface/token ...
```

## BuildKit cache export for CI
[expand bullet: --cache-to=type=gha for GHA (buildx ≥ 0.21, after April 2025 v2 API migration); --cache-to=type=registry,ref=...,mode=max for everything else; mode=inline dev-only.]

## Reproducible builds (optional)
[expand bullet: SOURCE_DATE_EPOCH auto-propagated by buildx ≥ 0.10; --output ...,rewrite-timestamp=true on buildx ≥ 0.13. No dockerfile change needed.]

## Provenance (out of skill scope; pointer only)
[expand bullet: cosign-sign-in-same-job is SLSA-1; SLSA-3 requires isolated reusable workflow (slsa-framework/slsa-github-generator).]

## Hadolint rules
[expand bullet: list DL3007/3008/3009/3015/3042/3059/4006 with one-line meaning each; pointer to .hadolint.yaml template; trustedRegistries explanation.]
```

Each `[expand bullet: ...]` is a writing instruction, not a placeholder — replace it with 1-2 prose paragraphs covering exactly those points. Use the spec bullet as the technical source; the prose makes it digestible.

- [ ] **Step 3: Self-review checklist**

```bash
wc -l .claude/skills/dockerfile/references/principles.md
grep -c "^## " .claude/skills/dockerfile/references/principles.md
grep -n "TBD\|TODO\|\[expand bullet" .claude/skills/dockerfile/references/principles.md
```

Expected: ~200-300 lines; ~19 `##` sections matching the spec bullets; zero `TBD`/`TODO`/`[expand bullet` placeholders.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/dockerfile/references/principles.md
git commit -m "docs(skill/dockerfile): add references/principles.md"
```

---

## Task 3: Write `references/python-uv.md`

**Files:**
- Create: `.claude/skills/dockerfile/references/python-uv.md`

**Source bullets:** Spec section **"`references/python-uv.md` — every Python image"** (9 bullets).

- [ ] **Step 1: Re-read the spec section**

```bash
sed -n '/^### `references\/python-uv.md`/,/^### `references\/gpu-cuda.md`/p' docs/superpowers/specs/2026-05-26-dockerfile-skill-design.md
```

- [ ] **Step 2: Write the file**

Skeleton — one section per spec bullet, expanded to prose:

```markdown
# Python + uv in Docker (rask)

Patterns that apply to every Python image in this repo. Loaded when authoring a Python dockerfile.

## Two-step `uv sync`: `--frozen` then `--locked`
[expand bullet: rationale for `--frozen` on step 1 (workspace member sources not yet copied) vs `--locked` on step 2 (verifies lockfile matches resolved deps after sources are present). Cite uv issues #16758, #16200, #12984, #15459. Include the exact two RUN blocks:]

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=projects/viewer/pyproject.toml,target=projects/viewer/pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    uv sync --frozen --no-install-workspace --package viewer --no-editable

COPY packages packages
COPY components components
COPY projects/viewer projects/viewer
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package viewer --no-editable
```

## uv environment variables
[expand bullet: UV_LINK_MODE=copy (required with cache mounts on different mount than target), UV_COMPILE_BYTECODE=1 (pre-compile .pyc), UV_PYTHON_DOWNLOADS (per-base-image policy: =0 for slim where Python is pre-installed, =1 or only-managed for CUDA bases). Include the ENV line.]

## `UV_PROJECT_ENVIRONMENT=/opt/venv`
[expand bullet: relocates venv out of /app so dev-compose bind-mounts don't shadow it; stable path across stages. Final COPY line: `COPY --from=builder --link /opt/venv /opt/venv`.]

## `PYTHONDONTWRITEBYTECODE=1` caveat
[expand bullet: set for clarity but understand UV_COMPILE_BYTECODE=1 already wrote .pyc at install time; PYTHONDONTWRITEBYTECODE only suppresses runtime recompilation of code uv didn't pre-compile (rare). Not contradictory.]

## Workspace handling for `projects/<name>/`
[expand bullet: bind-mount root pyproject.toml + uv.lock + the target project's pyproject.toml + every workspace member's pyproject.toml (packages/htr, packages/storage, components/...) during step 1. COPY sources during step 2. Use --package <name> consistently.]

## arm64 cache-mount is load-bearing
[expand bullet: on linux/arm64 (Apple silicon devs) where manylinux wheels are absent, the cache mount is the difference between 30s rebuilds and 10+ minute recompiles of Rust/C deps from source. Never drop the cache mount to "simplify" a dockerfile.]

## `htrflow` from git
[expand bullet: htrflow is a git-source dependency in projects/runner/pyproject.toml; keep `git` in the builder stage only, never in the final image.]

## `--no-editable` installs as wheel
[expand bullet: workspace member becomes a wheel in .venv/lib/python.../site-packages/<name>/; no .pth link to source tree; final stage doesn't need the source files on disk.]

## Final-stage venv copy
[expand bullet: COPY --from=builder --link /opt/venv /opt/venv; PATH=/opt/venv/bin:$PATH; no uv binary in final image. Include the exact PATH ENV line and a note that the final stage doesn't need uv (everything's already installed).]
```

- [ ] **Step 3: Self-review checklist**

```bash
wc -l .claude/skills/dockerfile/references/python-uv.md
grep -c "^## " .claude/skills/dockerfile/references/python-uv.md
grep -n "TBD\|TODO\|\[expand bullet" .claude/skills/dockerfile/references/python-uv.md
```

Expected: ~120-180 lines; ~9 `##` sections; zero placeholders.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/dockerfile/references/python-uv.md
git commit -m "docs(skill/dockerfile): add references/python-uv.md"
```

---

## Task 4: Write `references/gpu-cuda.md`

**Files:**
- Create: `.claude/skills/dockerfile/references/gpu-cuda.md`

**Source bullets:** Spec section **"`references/gpu-cuda.md` — runner-class images"** (10 bullets).

- [ ] **Step 1: Re-read the spec section**

```bash
sed -n '/^### `references\/gpu-cuda.md`/,/^### `references\/static-nginx.md`/p' docs/superpowers/specs/2026-05-26-dockerfile-skill-design.md
```

- [ ] **Step 2: Write the file**

```markdown
# GPU + CUDA + Ray + PyTorch (rask)

Patterns specific to images that need a GPU at runtime. rask's runner is the only such image.

## CUDA variant matrix: `-base` / `-runtime` / `-devel`
[expand bullet: variant breakdown; default to `-runtime`; switch to `-devel` only if a wheel compiles CUDA C++ at install. Why PyTorch/Ray don't need devel: they ship their own CUDA libs and only need driver ABI + shared libs from runtime image.]

## uv-managed Python on the CUDA Ubuntu base
[expand bullet: UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_PYTHON_PREFERENCE=only-managed beats deadsnakes PPA — no apt churn, exact 3.13 parity with slim viewer. +50 MB cost; fix by putting .venv/bin on PATH and never invoking system python.]

## System libs Ray + PyTorch actually need
[expand bullet: libgomp1 (openmp), ca-certificates, tini. Nothing else. No python3-dev, no compilers in the final image. Include the apt-get install line.]

## Model-weight strategy
[expand bullet: prescribed default = runtime-download to $HF_HOME=/cache/hf (persistent volume). Bake-at-build only when deploy target has no volume. Sidecar init container is K8s territory, out of scope. Include a comparison table of the three options.]

## `huggingface_hub>=0.24.7` pin
[expand bullet: older versions hit a race condition where .lock files hang indefinitely with concurrent multi-process downloads (HF #2543, #2038). Multi-Ray-worker setups trip this constantly. Pin in projects/runner/pyproject.toml.]

## Mount `$HF_HOME` on a local volume
[expand bullet: HF lock-file mechanism deadlocks on CIFS/NFS. Use a local persistent volume (emptyDir or hostPath in K8s, named volume in compose).]

## `HF_HUB_ENABLE_HF_TRANSFER=1`
[expand bullet: >500 MB/s downloads; trade-off = no progress bars and tail-end slowdowns look like a hung download. Document the caveat. Bake as ENV.]

## HF telemetry off by default
[expand bullet: HF_HUB_DISABLE_TELEMETRY=1 + HF_HUB_DISABLE_IMPLICIT_TOKEN=1 baked into the image. DO_NOT_TRACK=1 covers gradio/datasets/diffusers too if those are ever added. HF_HUB_OFFLINE=1 is runtime-only, never baked (image must be able to fetch on first warm-up). Cite the Riksarkivet-as-government-archive privacy rationale.]

## `--mount=type=secret,id=hf_token` for gated weights
[expand bullet: keeps token out of layer history; build invocation pattern with --secret flag. Show both the RUN line and the build invocation.]

## Thread-storm ENV defaults
[expand bullet: ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1. Why OpenBLAS must be set in Dockerfile ENV not Python (read before `import numpy`). Ray can still override per-actor via runtime_env.]

## `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
[expand bullet: fixes long-running Ray Serve replica OOM-after-hours. Caveat: conflicts with NCCL VMM in multi-GPU (pytorch/pytorch#165419). rask's Ray Serve replicas are single-GPU per pipeline.py, so it's safe. Bake as ENV; document the caveat for anyone tempted to move to multi-GPU NCCL.]

## CUDA smoke test (optional)
[expand bullet: build-arg-gated `python -c "import torch; assert torch.cuda.is_available()"`. When to keep vs defer to runtime.]

## Runtime config that pairs with this image (pointer)
[expand bullet: dockerfile cannot fix; deploy manifest must. --shm-size ≥ 30% RAM (or Ray's object store silently degrades to /tmp); --ulimit nofile=65535 (Ray Serve warns below 8192). Cite Ray issues #13619, #14535, #13045, #16820.]
```

- [ ] **Step 3: Self-review checklist**

```bash
wc -l .claude/skills/dockerfile/references/gpu-cuda.md
grep -c "^## " .claude/skills/dockerfile/references/gpu-cuda.md
grep -n "TBD\|TODO\|\[expand bullet" .claude/skills/dockerfile/references/gpu-cuda.md
```

Expected: ~180-250 lines; ~13 sections; zero placeholders.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/dockerfile/references/gpu-cuda.md
git commit -m "docs(skill/dockerfile): add references/gpu-cuda.md"
```

---

## Task 5: Write `references/static-nginx.md`

**Files:**
- Create: `.claude/skills/dockerfile/references/static-nginx.md`

**Source bullets:** Spec section **"`references/static-nginx.md` — frontend-class images"** (11 bullets).

- [ ] **Step 1: Re-read the spec section**

```bash
sed -n '/^### `references\/static-nginx.md`/,/^## Templates/p' docs/superpowers/specs/2026-05-26-dockerfile-skill-design.md
```

- [ ] **Step 2: Write the file**

```markdown
# Static bundle + nginx (rask)

Patterns for static-asset frontends (SvelteKit adapter-static, Vite, etc.) served by nginx-unprivileged.

## Why nginx, not FastAPI, for static assets
[expand bullet: sendfile, immutable cache headers, ~5 MB RAM per worker, precompressed-static support. FastAPI/Starlette adds an event-loop hop per file. Cite SvelteKit kit#15150 patterns.]

## Builder: oven/bun:1-debian + cache mount
[expand bullet: pin by digest; --mount=type=cache,target=/root/.bun/install/cache; `bun install --frozen-lockfile`; `bun run build`. adapter-static output dir is `build/` by default.]

## Build context is repo root
[expand bullet: bun workspaces require all workspace member package.json files. Bind-mount root package.json + root bun.lock + each workspace member's package.json (components/apps/frontend/package.json, packages/ui/package.json) for the install step. Then COPY components/apps/frontend/ + packages/ui/ sources. Build via `bun --cwd components/apps/frontend run build`.]

## Runtime: nginxinc/nginx-unprivileged:1.27-alpine
[expand bullet: listens on 8080, runs as UID 101, PID file in /tmp. No USER directive needed — image is already rootless.]

## Read-only-rootfs nginx config
[expand bullet: override pid /tmp/nginx.pid; client_body_temp_path /tmp/client_body; proxy_temp_path /tmp/proxy; fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi; so the image runs under --read-only --tmpfs /tmp.]

## SPA `try_files` fallback
[expand bullet: `try_files $uri $uri.html $uri/index.html /index.html;` — SvelteKit serves prerendered pages at /foo.html, falls through to client-rendered /index.html.]

## `/_app/immutable/` cache
[expand bullet: location ^~ /_app/immutable/ → Cache-Control: public, max-age=31536000, immutable. Hashed asset filenames make this safe.]

## `/_app/version.json` no-cache override
[expand bullet: placed ABOVE the immutable block. SvelteKit polls this file to detect deploys and trigger reload-on-deploy. Caching it as immutable breaks the UX. Cite kit#3194, #15150.]

## Block dotfiles except `/.well-known/`
[expand bullet: location ~ /\.(?!well-known) { deny all; }. adapter-static legitimately emits .well-known (ACME) and .nojekyll. Other dotfiles (.env, .git, .DS_Store) should never leak.]

## Brotli is non-trivial on nginx-unprivileged
[expand bullet: official image doesn't ship ngx_brotli. Two paths: (a) accept gzip-only — enable `precompress: true` in adapter-static + `gzip_static on;`; (b) build a layered image with brotli (fholzer/docker-nginx-brotli as base). Skill recommends (a) by default; (b) only if measured TTFB gains justify maintenance cost.]

## Svelte 5 CSP gotcha
[expand bullet: Svelte 5 still emits inline event handlers (`__e=event` on <img> etc., svelte#14014). Pure strict-dynamic CSP breaks them. Two paths: SvelteKit's `csp.directives` in svelte.config.js (recommended — framework emits nonces/hashes per build) OR include 'unsafe-hashes' + per-snippet hashes in the nginx CSP header. The nginx config is a CSP-friendly base; SvelteKit is the source of truth.]

## HEALTHCHECK via wget
[expand bullet: alpine ships wget. Exact line: `HEALTHCHECK CMD wget -qO- http://127.0.0.1:8080/ || exit 1`. Optional — usually orchestrator probes own readiness.]

## Why a separate frontend container, not served from viewer
[expand bullet: independent deploy lifecycles. Frontend can ship without re-rolling the API; viewer can ship without breaking client-side caches. Plus the perf reasons in section 1.]
```

- [ ] **Step 3: Self-review checklist**

```bash
wc -l .claude/skills/dockerfile/references/static-nginx.md
grep -c "^## " .claude/skills/dockerfile/references/static-nginx.md
grep -n "TBD\|TODO\|\[expand bullet" .claude/skills/dockerfile/references/static-nginx.md
```

Expected: ~150-220 lines; ~13 sections; zero placeholders.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/dockerfile/references/static-nginx.md
git commit -m "docs(skill/dockerfile): add references/static-nginx.md"
```

---

## Task 6: Write `templates/dockerignore`

**Files:**
- Create: `.claude/skills/dockerfile/templates/dockerignore`

- [ ] **Step 1: Write the file**

Exact content (copy verbatim):

```
# rask .dockerignore
# Install: cp .claude/skills/dockerfile/templates/dockerignore .dockerignore
# Single source of truth for ALL .docker/*.dockerfile builds (build context = repo root).

# VCS
.git
.gitignore
.gitattributes

# CI / docker / build orchestration (in the source tree, but never inside the image)
.docker/
.dagger/
Dockerfile
*.dockerfile
.dockerignore

# Python venvs and caches
.venv
.venv*/
**/__pycache__
**/*.pyc
**/*.pyo
*.egg-info/
*.dist-info/
.mypy_cache
.ruff_cache
.pytest_cache

# JS/TS build artefacts and caches
node_modules
node_modules/.cache
dist
build
.svelte-kit
storybook-static

# Test/coverage outputs
coverage/
htmlcov/
.coverage
.coverage.*

# Editor / IDE / OS junk
.idea/
.vscode/
.cursor*
.DS_Store
*.swp
*.swo

# Infra-as-code state
.terraform/
.terraform.lock.hcl

# Secrets and local config
.env
.env.*
.secret
.secret.*

# Claude Code project state
.claude/

# Logs
*.log
logs/

# Model weights (large; should never be in build context — mount at runtime)
**/weights/
**/*.pt
**/*.bin
**/*.safetensors
```

- [ ] **Step 2: Self-review**

```bash
test -f .claude/skills/dockerfile/templates/dockerignore && wc -l .claude/skills/dockerfile/templates/dockerignore
grep -c "^#" .claude/skills/dockerfile/templates/dockerignore
```

Expected: ~50 lines; multiple `#` comment lines. File exists.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/dockerfile/templates/dockerignore
git commit -m "feat(skill/dockerfile): add templates/dockerignore"
```

---

## Task 7: Write `templates/hadolint.yaml`

**Files:**
- Create: `.claude/skills/dockerfile/templates/hadolint.yaml`

- [ ] **Step 1: Write the file**

Exact content:

```yaml
# rask .hadolint.yaml
# Install: cp .claude/skills/dockerfile/templates/hadolint.yaml .hadolint.yaml
# Hadolint config — paired with the dockerfile skill at .claude/skills/dockerfile/.

ignored: []

trustedRegistries:
  - nvidia/cuda
  - nginxinc/nginx-unprivileged
  - ghcr.io
  - docker.io/python
  - docker.io/oven

failure-threshold: warning

# Override-comment workflow: when a rule MUST be ignored, use:
#   # hadolint ignore=DLxxxx
#   # Reason: <why this case is justified>
#   RUN ...
# Reviewers gate merges on the reason being plausible.
```

- [ ] **Step 2: Verify hadolint accepts the config**

```bash
hadolint --config .claude/skills/dockerfile/templates/hadolint.yaml --no-fail - <<<'FROM python:3.13-slim-bookworm@sha256:dummy'
echo "exit=$?"
```

Expected: hadolint runs (may emit warnings about the dummy digest but should not crash on the config). Exit code 0 or 1 acceptable; not 2 (config-parse error).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/dockerfile/templates/hadolint.yaml
git commit -m "feat(skill/dockerfile): add templates/hadolint.yaml"
```

---

## Task 8: Write `templates/frontend.nginx.conf`

**Files:**
- Create: `.claude/skills/dockerfile/templates/frontend.nginx.conf`

- [ ] **Step 1: Write the file**

Exact content:

```nginx
# rask frontend nginx config — SvelteKit adapter-static SPA + nginx-unprivileged.
# Used by templates/frontend.dockerfile, copied to /etc/nginx/conf.d/default.conf.
# Image runs as UID 101 and supports --read-only --tmpfs /tmp.

# Read-only-rootfs path overrides (writable paths must be in /tmp).
pid /tmp/nginx.pid;

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # Writable paths inside the read-only rootfs.
    client_body_temp_path /tmp/client_body;
    proxy_temp_path       /tmp/proxy;
    fastcgi_temp_path     /tmp/fastcgi;
    uwsgi_temp_path       /tmp/uwsgi;
    scgi_temp_path        /tmp/scgi;

    sendfile        on;
    tcp_nopush      on;
    tcp_nodelay     on;
    keepalive_timeout  65;

    # gzip — Brotli is non-trivial on nginx-unprivileged (no ngx_brotli module).
    # adapter-static `precompress: true` emits .gz files; gzip_static serves them.
    gzip              on;
    gzip_vary         on;
    gzip_static       on;
    gzip_types        text/css application/javascript application/json image/svg+xml;

    server {
        listen 8080;
        server_name _;
        root   /usr/share/nginx/html;
        index  index.html;

        # CSP-friendly base. Svelte 5 emits inline `__e=event` handlers (svelte#14014);
        # let SvelteKit's csp.directives in svelte.config.js own the policy — this header
        # is the minimum-viable default that doesn't break dev.
        # add_header Content-Security-Policy "default-src 'self'; ..." always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;

        # /_app/version.json — MUST be no-cache so SvelteKit's deploy-reload polling works.
        # Placed ABOVE the immutable block (kit#3194, #15150).
        location = /_app/version.json {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }

        # Hashed assets in /_app/immutable/ get a year of caching.
        location ^~ /_app/immutable/ {
            add_header Cache-Control "public, max-age=31536000, immutable" always;
            try_files $uri =404;
        }

        # Block accidental dotfile exposure (.env, .git, .DS_Store) — but allow .well-known.
        location ~ /\.(?!well-known) {
            deny all;
        }

        # SPA fallback: prerendered .html → /foo/index.html → client-rendered shell.
        location / {
            add_header Cache-Control "no-cache" always;
            try_files $uri $uri.html $uri/index.html /index.html;
        }
    }
}

events {}
```

- [ ] **Step 2: Self-review**

```bash
grep -c "^    location" .claude/skills/dockerfile/templates/frontend.nginx.conf
grep -n "/_app/version.json\|/_app/immutable\|well-known\|gzip_static\|tmp/" .claude/skills/dockerfile/templates/frontend.nginx.conf
```

Expected: 4 `location` blocks; all five required directives present (`/_app/version.json`, `/_app/immutable/`, `well-known` block, `gzip_static on`, multiple `/tmp/` overrides).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/dockerfile/templates/frontend.nginx.conf
git commit -m "feat(skill/dockerfile): add templates/frontend.nginx.conf"
```

---

## Task 9: Fetch current digests for base images

**Files:**
- Create: `/tmp/rask-digests.txt` (scratchpad, NOT committed)

This task pins digests at authoring time. The digests go into the dockerfile templates in tasks 10-12.

- [ ] **Step 1: Inspect each base**

```bash
{
  echo "# base image digests captured $(date -u +%FT%TZ)"
  for ref in \
    "python:3.13-slim-bookworm" \
    "nvidia/cuda:12.4.0-runtime-ubuntu22.04" \
    "oven/bun:1-debian" \
    "nginxinc/nginx-unprivileged:1.27-alpine" \
    "ghcr.io/astral-sh/uv:0.5"; do
      echo "## $ref"
      docker buildx imagetools inspect "$ref" 2>&1 | grep -E '^(Name|Digest):' | head -2
      echo
  done
} | tee /tmp/rask-digests.txt
```

Expected: one `Name:` + `Digest: sha256:...` pair per ref. Copy each digest into the matching template in subsequent tasks.

- [ ] **Step 2: Verify no digest is missing**

```bash
grep -c '^Digest: sha256:' /tmp/rask-digests.txt
```

Expected: `5` (one per base). If any are missing, re-run step 1 for that specific ref.

- [ ] **Step 3: Do NOT commit `/tmp/rask-digests.txt`**

This is a scratch file. Digests are baked directly into templates in the next tasks.

---

## Task 10: Write `templates/viewer.dockerfile` + verify

**Files:**
- Create: `.claude/skills/dockerfile/templates/viewer.dockerfile`

**Spec references:** spec section **"`templates/viewer.dockerfile`"** + the three reference files.

- [ ] **Step 1: Write the file**

Replace `<DIGEST-FOR-X>` with the digests captured in Task 9. The complete template:

```dockerfile
# syntax=docker/dockerfile:1.11
# rask viewer image — FastAPI on python:3.13-slim-bookworm.
# Install to repo root as: cp .claude/skills/dockerfile/templates/viewer.dockerfile .docker/viewer.dockerfile
# Build:
#   docker buildx build -f .docker/viewer.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t viewer:dev .

# ---- builder stage: install deps via uv ------------------------------------
FROM python:3.13-slim-bookworm@sha256:<DIGEST-FOR-PYTHON> AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:<DIGEST-FOR-UV> /uv /usr/local/bin/uv

WORKDIR /app

# Step 1: install workspace deps (frozen — workspace member sources not yet COPYed).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=projects/viewer/pyproject.toml,target=projects/viewer/pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=components,target=components \
    uv sync --frozen --no-install-workspace --package viewer --no-editable

# Step 2: COPY real sources and resolve workspace deps (locked).
COPY packages    packages
COPY components  components
COPY projects/viewer projects/viewer
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package viewer --no-editable

# Strip residual setuid bits before the venv leaves the builder.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final stage: minimum runtime surface ----------------------------------
FROM python:3.13-slim-bookworm@sha256:<DIGEST-FOR-PYTHON>

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-viewer" \
      org.opencontainers.image.description="rask viewer service — FastAPI on :8888"

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app

RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',8888))" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# Note: --forwarded-allow-ips MUST be set to the nginx network CIDR at deploy time,
# never '*' (header-spoofing risk). --workers is intentionally unset — orchestrator scales.
CMD ["uvicorn", "viewer.app:app", \
     "--host", "0.0.0.0", "--port", "8888", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1", \
     "--no-access-log", \
     "--loop", "uvloop", \
     "--http", "httptools"]
```

> **Note on the `RUN --network=none ... cp -a` pattern:** The conventional `COPY --link --from=builder` runs in BuildKit's frontend without an exec shell, so `--network=none` can't be applied to it. To honor the spec's "no-network on final COPY" rule, we use a bind-mount from the builder stage inside a `RUN --network=none`, which preserves both the `--link`-like behavior (cache-friendly, no rewrite of earlier layers) and the no-network guarantee. If your buildx is older than v0.18 and `--network=none` on `RUN --mount=from=` fails, fall back to plain `COPY --from=builder --link /opt/venv /opt/venv` and accept the residual network exposure (file the deviation per the hadolint override-comment workflow).

- [ ] **Step 2: Install the template into `.docker/` for a real build**

```bash
cp .claude/skills/dockerfile/templates/viewer.dockerfile .docker/viewer.dockerfile
ls -la .docker/viewer.dockerfile
```

- [ ] **Step 3: `docker buildx build --check`**

```bash
docker buildx build --check -f .docker/viewer.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  .
echo "check-exit=$?"
```

Expected: `check-exit=0`. No `SecretsUsedInArgOrEnv`, `InvalidDefinitionDescription`, or other lints. If lints fire, fix the dockerfile inline and re-run.

- [ ] **Step 4: Hadolint**

First, install the `.hadolint.yaml`:

```bash
cp .claude/skills/dockerfile/templates/hadolint.yaml .hadolint.yaml
hadolint --config .hadolint.yaml .docker/viewer.dockerfile
echo "hadolint-exit=$?"
```

Expected: `hadolint-exit=0`. Address any warning by either fixing the line or adding an `# hadolint ignore=DLxxxx` + `# Reason: ...` comment.

- [ ] **Step 5: Build the image (first build — populates caches)**

First, install the `.dockerignore`:

```bash
cp .claude/skills/dockerfile/templates/dockerignore .dockerignore
time docker buildx build -f .docker/viewer.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  -t viewer:dev \
  --load .
echo "build1-exit=$?"
```

Expected: `build1-exit=0`. Build completes; expect 1-3 minutes on first run.

- [ ] **Step 6: Build a second time — verify cache hits**

```bash
time docker buildx build -f .docker/viewer.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  -t viewer:dev \
  --progress=plain --load . 2>&1 | grep -E '^#[0-9]+ \[builder|^#[0-9]+ \[stage|CACHED'
```

Expected: most layer rows show `CACHED`. Specifically, the two `uv sync` RUN steps and the COPY-from-builder should be `CACHED`. Total time < 10s.

- [ ] **Step 7: Smoke-test the image starts**

```bash
docker run --rm -d --name viewer-smoke -p 18888:8888 viewer:dev
sleep 3
curl -fsSL --max-time 3 http://127.0.0.1:18888/api/health 2>&1 || echo "no /api/health (acceptable — may need RASK_VIEWER_INPUT/OUTPUT env)"
docker logs viewer-smoke 2>&1 | tail -20
docker stop viewer-smoke
```

Expected: container starts; either `/api/health` returns 200 (if defaults work) or the container starts and logs show uvicorn ready. If uvicorn crashes due to missing env, that's an app-config issue, not a dockerfile bug — note it but proceed.

- [ ] **Step 8: Commit**

```bash
git add .claude/skills/dockerfile/templates/viewer.dockerfile
git commit -m "feat(skill/dockerfile): add templates/viewer.dockerfile (verified build + check + hadolint)"
```

---

## Task 11: Write `templates/runner.dockerfile` + verify

**Files:**
- Create: `.claude/skills/dockerfile/templates/runner.dockerfile`

**Spec reference:** spec section **"`templates/runner.dockerfile`"** + `references/gpu-cuda.md`.

- [ ] **Step 1: Write the file**

```dockerfile
# syntax=docker/dockerfile:1.11
# rask runner image — Python 3.13 + Ray + PyTorch on nvidia/cuda:12.4.0-runtime-ubuntu22.04.
# Install to repo root as: cp .claude/skills/dockerfile/templates/runner.dockerfile .docker/runner.dockerfile
# Build:
#   docker buildx build -f .docker/runner.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t runner:dev .
# Runtime requirements (paired with this image — set in deploy manifest):
#   --shm-size >= 30% of RAM   (Ray object store; #13619, #14535)
#   --ulimit nofile=65535      (Ray Serve FD limit; #13045)
#   --read-only --tmpfs /tmp   (image is built to support this)
#   GPU device exposure        (nvidia-container-toolkit)

# ---- builder stage: uv-managed Python + uv sync ----------------------------
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04@sha256:<DIGEST-FOR-CUDA> AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=only-managed \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    DEBIAN_FRONTEND=noninteractive

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:<DIGEST-FOR-UV> /uv /usr/local/bin/uv

# git is for htrflow (git-source dep), kept in builder ONLY.
# hadolint ignore=DL3008
# Reason: apt-get version pinning on nvidia/cuda base images is impractical — versions
# drift faster than the digest pin can keep up; we accept this for the builder stage.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git tini libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Step 1: install workspace deps (frozen).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=projects/runner/pyproject.toml,target=projects/runner/pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=components,target=components \
    uv sync --frozen --no-install-workspace --package runner --no-editable

# Step 2: COPY sources and resolve (locked).
COPY packages    packages
COPY components  components
COPY projects/runner projects/runner
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package runner --no-editable

# Optional gated-model fetch step. Uncomment if rask pulls licensed weights at build time.
# RUN --mount=type=secret,id=hf_token \
#     HF_TOKEN=$(cat /run/secrets/hf_token) \
#     /opt/venv/bin/python -m runner.fetch_models

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final stage: minimum CUDA runtime + venv + uv-Python -----------------
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04@sha256:<DIGEST-FOR-CUDA>

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-runner" \
      org.opencontainers.image.description="rask runner — Ray Data HTR pipelines"

ENV DEBIAN_FRONTEND=noninteractive
# hadolint ignore=DL3008
# Reason: see builder stage. Runtime needs libgomp1 (PyTorch openmp), tini, ca-certs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates tini libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app \
 && mkdir -p /cache/hf /app && chown -R app:app /cache /app

RUN --network=none --mount=from=builder,source=/opt/uv/python,target=/tmp/uvpy \
    cp -a /tmp/uvpy /opt/uv/python
RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

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

USER app
WORKDIR /app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "runner"]
```

- [ ] **Step 2: Install and `--check`**

```bash
cp .claude/skills/dockerfile/templates/runner.dockerfile .docker/runner.dockerfile
docker buildx build --check -f .docker/runner.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  .
echo "check-exit=$?"
```

Expected: `check-exit=0`.

- [ ] **Step 3: Hadolint**

```bash
hadolint --config .hadolint.yaml .docker/runner.dockerfile
echo "hadolint-exit=$?"
```

Expected: `hadolint-exit=0`. The two `# hadolint ignore=DL3008` comments handle the apt-pin exception.

- [ ] **Step 4: Build (this image is large — ~3-5 GB with CUDA + PyTorch; allow 5-10 min on first build)**

```bash
time docker buildx build -f .docker/runner.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  -t runner:dev --load .
echo "build1-exit=$?"
```

Expected: `build1-exit=0`. **If torch/htrflow wheels fail to find a wheel and try to compile from source**, the build will fail with a gcc error — that means we need to switch the builder stage to `nvidia/cuda:...-devel-...` for compilation tools, then keep `-runtime` for the final stage. Document the deviation and re-run.

- [ ] **Step 5: Verify second-build is cached**

```bash
time docker buildx build -f .docker/runner.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  -t runner:dev --progress=plain --load . 2>&1 | grep -E 'CACHED|^#[0-9]+ \[' | head -30
```

Expected: both `uv sync` steps and both `cp -a` final copies show `CACHED`. Total time < 30s.

- [ ] **Step 6: Smoke-test (no GPU available is fine — just verify the image launches Python)**

```bash
docker run --rm runner:dev python -c "import ray, torch, htr; print('ray:', ray.__version__, 'torch:', torch.__version__, 'cuda-available:', torch.cuda.is_available())"
```

Expected: prints versions. `cuda-available` will be `False` on a non-GPU host — that's fine. If imports fail, dockerfile or workspace deps are wrong.

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/dockerfile/templates/runner.dockerfile
git commit -m "feat(skill/dockerfile): add templates/runner.dockerfile (verified build + check + hadolint)"
```

---

## Task 12: Write `templates/frontend.dockerfile` + verify

**Files:**
- Create: `.claude/skills/dockerfile/templates/frontend.dockerfile`

**Spec reference:** spec section **"`templates/frontend.dockerfile`"** + `references/static-nginx.md`.

- [ ] **Step 1: Write the file**

```dockerfile
# syntax=docker/dockerfile:1.11
# rask frontend image — SvelteKit adapter-static built with Bun, served by nginx-unprivileged.
# Install to repo root as: cp .claude/skills/dockerfile/templates/frontend.dockerfile .docker/frontend.dockerfile
# Build:
#   docker buildx build -f .docker/frontend.dockerfile \
#     --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg VERSION=$(git describe --always) \
#     -t frontend:dev .

# ---- builder stage: bun install (workspace) + bun build --------------------
FROM oven/bun:1-debian@sha256:<DIGEST-FOR-BUN> AS builder

WORKDIR /src

# Bind-mount the bun workspace metadata (root + every member's package.json).
# `bun install` from repo root resolves the workspace graph.
RUN --mount=type=cache,target=/root/.bun/install/cache \
    --mount=type=bind,source=package.json,target=package.json \
    --mount=type=bind,source=bun.lock,target=bun.lock \
    --mount=type=bind,source=components/apps/frontend/package.json,target=components/apps/frontend/package.json \
    --mount=type=bind,source=packages/ui/package.json,target=packages/ui/package.json \
    bun install --frozen-lockfile

# COPY workspace sources, then build the frontend.
COPY components/apps/frontend components/apps/frontend
COPY packages/ui   packages/ui
COPY package.json bun.lock    ./

RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun --cwd components/apps/frontend run build

# ---- final stage: nginx-unprivileged serves the static build --------------
FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:<DIGEST-FOR-NGINX>

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-frontend" \
      org.opencontainers.image.description="rask SvelteKit SPA served by nginx-unprivileged"

# nginx-unprivileged already runs as UID 101 and supports --read-only --tmpfs /tmp.

# nginx config (replaces the default — must include `http {}` block since it's the full config).
COPY templates/frontend.nginx.conf /etc/nginx/nginx.conf

# Static assets — RUN --network=none preserves "no exfil" on the final copy.
RUN --network=none --mount=from=builder,source=/src/components/apps/frontend/build,target=/tmp/build \
    cp -a /tmp/build/. /usr/share/nginx/html/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1:8080/ >/dev/null 2>&1 || exit 1

# nginx-unprivileged already has its own init; no tini needed.
```

> **nginx.conf path note:** The default `nginxinc/nginx-unprivileged` image looks for `/etc/nginx/nginx.conf` as the main config and includes `/etc/nginx/conf.d/*.conf` from within. Our `frontend.nginx.conf` is written as a *full* config (with top-level `pid`, `http {}`, `events {}`) so it can be installed at `/etc/nginx/nginx.conf` and own the whole policy. If you'd rather have it as a drop-in under `conf.d/`, strip the top-level `pid`/`http`/`events` blocks and copy to `/etc/nginx/conf.d/default.conf` — but then read-only-rootfs `pid` override has to live elsewhere.

- [ ] **Step 2: Install and `--check`**

```bash
cp .claude/skills/dockerfile/templates/frontend.dockerfile .docker/frontend.dockerfile
docker buildx build --check -f .docker/frontend.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  .
echo "check-exit=$?"
```

Expected: `check-exit=0`.

- [ ] **Step 3: Hadolint**

```bash
hadolint --config .hadolint.yaml .docker/frontend.dockerfile
echo "hadolint-exit=$?"
```

Expected: `hadolint-exit=0`.

- [ ] **Step 4: Build (frontend bundle build can take 30s-2min)**

```bash
time docker buildx build -f .docker/frontend.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  -t frontend:dev --load .
echo "build1-exit=$?"
```

Expected: `build1-exit=0`. SvelteKit prerender may surface warnings — they're app concerns, not dockerfile bugs.

- [ ] **Step 5: Second-build cache verification**

```bash
time docker buildx build -f .docker/frontend.dockerfile \
  --build-arg BUILD_DATE=$(date -u +%FT%TZ) \
  --build-arg VCS_REF=$(git rev-parse HEAD) \
  --build-arg VERSION=$(git describe --always 2>/dev/null || echo dev) \
  -t frontend:dev --progress=plain --load . 2>&1 | grep -E 'CACHED|^#[0-9]+ \[' | head -20
```

Expected: `bun install` and `bun run build` steps both `CACHED`. Total time < 10s.

- [ ] **Step 6: Smoke-test**

```bash
docker run --rm -d --name frontend-smoke -p 18080:8080 frontend:dev
sleep 2
curl -fsSL --max-time 3 http://127.0.0.1:18080/ | head -10
echo "---/_app/version.json cache-control---"
curl -sI --max-time 3 http://127.0.0.1:18080/_app/version.json | grep -i 'cache-control'
echo "---/_app/immutable cache-control sample (path may vary)---"
SAMPLE=$(curl -fsSL http://127.0.0.1:18080/ | grep -oE '/_app/immutable/[^"]+' | head -1)
test -n "$SAMPLE" && curl -sI --max-time 3 "http://127.0.0.1:18080${SAMPLE}" | grep -i 'cache-control'
echo "---dotfile block---"
curl -sI --max-time 3 http://127.0.0.1:18080/.env | head -1
docker stop frontend-smoke
```

Expected:
- `/` returns HTML.
- `/_app/version.json` returns `Cache-Control: no-cache`.
- `/_app/immutable/...` returns `Cache-Control: public, max-age=31536000, immutable`.
- `/.env` returns `HTTP/1.1 403 Forbidden`.

If any of these fail, fix the nginx.conf template and rebuild.

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/dockerfile/templates/frontend.dockerfile
git commit -m "feat(skill/dockerfile): add templates/frontend.dockerfile (verified build + check + hadolint + nginx headers)"
```

---

## Task 13: End-to-end skill activation check + cleanup

**Files:**
- Verify: `.claude/skills/dockerfile/` complete
- Cleanup: remove the temporary `.docker/*.dockerfile`, `.dockerignore`, `.hadolint.yaml` installed during verification

- [ ] **Step 1: Verify the skill structure is complete**

```bash
find .claude/skills/dockerfile -type f | sort
```

Expected output (11 files):

```
.claude/skills/dockerfile/SKILL.md
.claude/skills/dockerfile/references/gpu-cuda.md
.claude/skills/dockerfile/references/principles.md
.claude/skills/dockerfile/references/python-uv.md
.claude/skills/dockerfile/references/static-nginx.md
.claude/skills/dockerfile/templates/dockerignore
.claude/skills/dockerfile/templates/frontend.dockerfile
.claude/skills/dockerfile/templates/frontend.nginx.conf
.claude/skills/dockerfile/templates/hadolint.yaml
.claude/skills/dockerfile/templates/runner.dockerfile
.claude/skills/dockerfile/templates/viewer.dockerfile
```

- [ ] **Step 2: SKILL.md frontmatter parses**

```bash
head -4 .claude/skills/dockerfile/SKILL.md
```

Expected: starts with `---`, has `name: dockerfile` and `description:` lines, closes with `---` on line 4.

- [ ] **Step 3: Hard-rule self-check across templates**

```bash
echo "--- syntax frontend ---"
grep -L "^# syntax=docker/dockerfile:1.11" .claude/skills/dockerfile/templates/*.dockerfile
echo "(expected: empty — all dockerfiles have the syntax line)"
echo "--- digest pinning ---"
grep -L "@sha256:" .claude/skills/dockerfile/templates/*.dockerfile
echo "(expected: empty — every FROM is digest-pinned)"
echo "--- non-root user ---"
grep -L "useradd -r --no-create-home --shell /usr/sbin/nologin" .claude/skills/dockerfile/templates/viewer.dockerfile .claude/skills/dockerfile/templates/runner.dockerfile
echo "(expected: empty — both Python dockerfiles use the hardened useradd)"
echo "--- OCI labels ---"
grep -L "org.opencontainers.image.created" .claude/skills/dockerfile/templates/*.dockerfile
echo "(expected: empty — every dockerfile emits OCI labels)"
echo "--- tini PID 1 (Python only) ---"
grep -L 'ENTRYPOINT \["/usr/bin/tini"' .claude/skills/dockerfile/templates/viewer.dockerfile .claude/skills/dockerfile/templates/runner.dockerfile
echo "(expected: empty — viewer + runner use tini)"
```

If any check returns a non-empty file list, fix that file and re-run.

- [ ] **Step 4: Clean up the dev-time install artefacts (do NOT commit them)**

```bash
git status .docker/ .hadolint.yaml .dockerignore
# .docker/.gitkeep stays; remove the three test dockerfiles, the hadolint.yaml, and the .dockerignore
rm -f .docker/viewer.dockerfile .docker/runner.dockerfile .docker/frontend.dockerfile
rm -f .hadolint.yaml .dockerignore
git status
```

Expected: working tree clean (or shows only files unrelated to this task). The dagger pipeline (when written separately) will be what installs these for real — the skill is documentation, not deployment.

- [ ] **Step 5: Final commit (only if step 4 produced any new tracked changes — usually none)**

If `git status` after step 4 is clean, skip. Otherwise:

```bash
git add -A
git commit -m "chore(skill/dockerfile): clean up dev-time install artefacts"
```

- [ ] **Step 6: Verify the skill is discoverable**

In Claude Code, restart or in a new session check that the skill appears via:

```text
/skills
```

Expected: `dockerfile` skill listed with its description. (Manual step — agent confirms by reading the next session's available skills list.)

---

## Self-review summary

After completing all 13 tasks:

- All spec **Hard rules** (Contract section, 10 items) are enforced in templates: ✓ via Task 13 step 3 grep checks.
- All four **references/*.md** files have spec coverage: ✓ via the per-task `grep -c "^## "` count.
- All three **dockerfile templates** are TDD-verified: build + `--check` + hadolint + smoke test: ✓ via Tasks 10-12.
- Two repo-level **install templates** (`dockerignore`, `hadolint.yaml`) ship under `templates/`: ✓ via Tasks 6, 7.
- Two CSS/nginx-config items (`/_app/version.json` cache-control, dotfile block) are actively tested via `curl`: ✓ via Task 12 step 6.

If a spec requirement is missing a task implementing it, the gap was: write a new task here and re-run self-review.

---

## Out of scope (deliberately deferred to follow-up plans)

- `.dagger/main.go` — the Go module that consumes these dockerfiles.
- CI workflow that runs `docker buildx build --cache-to=type=gha` + hadolint + buildx --check as gates.
- Adding `hadolint` to `make check` — flagged in spec.
- Wiring `make` targets for `make image-viewer` / `image-runner` / `image-frontend`.
- Multi-arch builds (linux/amd64,linux/arm64) — belongs in dagger.
