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
5. **BuildKit cache mounts.** `--mount=type=cache,target=/root/.cache/uv` (uv) and `--mount=type=cache,target=/root/.bun/install/cache` (bun). Caches never ship in image layers.
6. **PID 1.** `tini --` as ENTRYPOINT for Python processes. `nginx-unprivileged` already has its own init.
7. **OCI labels.** Declare `ARG BUILD_DATE/VCS_REF/VERSION` and emit `org.opencontainers.image.*` labels (full set in `references/principles.md`).
8. **Read-only-rootfs ready.** Final image runs cleanly under `--read-only --tmpfs /tmp`. Writable paths are `/tmp` or explicit volumes.
9. **Secrets via `--mount=type=secret`, never `ARG`.** With `--provenance=mode=max` (SLSA), ARG values become public in the attestation.
10. **No build leakage.** Final image must not contain build toolchain (gcc, make), package managers (apt, the `uv` binary), `.git`, tests, or dev-dependencies.
11. **First line is `# syntax=docker/dockerfile:1.11`** — pins the BuildKit frontend version. Required for the cache/bind/secret mount syntax used elsewhere in these rules.

## When to load each reference

- `references/principles.md` — **every** dockerfile change. Cache, layer ordering, COPY --link tradeoff, HEALTHCHECK, hadolint, setuid-strip, OCI labels, CVE-2024-3094 bump-guard, CI cache export.
- `references/python-uv.md` — any Python image. uv two-step `--frozen`/`--locked` sync, `UV_PROJECT_ENVIRONMENT=/opt/venv`, workspace handling, arm64 cache-mount note.
- `references/gpu-cuda.md` — only when the image needs CUDA. Runtime vs devel, uv-managed Python on Ubuntu base, HF telemetry/transfer/secret patterns, thread-storm + PYTORCH_CUDA_ALLOC_CONF ENV defaults.
- `references/static-nginx.md` — only when serving static assets. bun build, nginx-unprivileged config, SPA fallback, /_app/version.json override, dotfile block, Svelte 5 CSP gotcha.

## After authoring

1. Run `docker buildx build --check -f .docker/<name>.dockerfile .` — catches `SecretsUsedInArgOrEnv`, missing stage-description comments.
2. Run `hadolint --config .hadolint.yaml .docker/<name>.dockerfile`. CI gates on this.
3. Build twice: `docker buildx build -f .docker/<name>.dockerfile --build-arg BUILD_DATE=$(date -u +%FT%TZ) --build-arg VCS_REF=$(git rev-parse HEAD) --build-arg VERSION=$(git describe --always) -t <name>:dev .`. The second build should be dominated by `CACHED` layers — that confirms the cache mount + bind mount + COPY-order discipline are correct.
4. When bumping a base image: `docker buildx imagetools inspect <ref>` → record digest. **Refuse digests older than ~90 days** (see bump workflow in `references/principles.md`); scan with Trivy/Grype/Docker Scout (CVE-2024-3094 was still found in pinned images in mid-2025).
