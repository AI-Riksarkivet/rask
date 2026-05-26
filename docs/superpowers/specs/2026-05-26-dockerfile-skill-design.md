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
- **Multi-stage.** Builder stage(s) carry toolchain; final stage is minimum runtime surface, non-root UID ≥ 10000.
- **Digest pinning.** Every `FROM` reference pinned by `@sha256:<digest>`, not a floating tag.
- **BuildKit cache mounts.** `--mount=type=cache` for `uv` and `bun` package caches; caches never ship in image layers.
- **PID 1.** `tini --` as ENTRYPOINT for Python processes (forks, signal forwarding). `nginx-unprivileged` already has its own init.
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
    ├── frontend.nginx.conf           # SPA config: try_files fallback + /_app/immutable cache.
    └── dockerignore                  # Repo-root .dockerignore (renamed on install).
```

## SKILL.md content shape

Sections, in order:

1. **Frontmatter** (`name`, `description`). Description carries the enforcement contract verbatim — it's what surfaces in skill triggers.
2. **When to use this skill.** Three triggers: new image, modifying existing `.docker/*.dockerfile`, reviewing.
3. **Decision tree.** Python+GPU → `runner.dockerfile` + `gpu-cuda.md`; Python no-GPU → `viewer.dockerfile` + `python-uv.md`; static bundle → `frontend.dockerfile` + `static-nginx.md`.
4. **Hard rules** (6-8 items from the Contract section above, restated tersely).
5. **When to load each reference** (one line per reference).
6. **After authoring** — `hadolint` invocation; second-build cache-hit verification; digest-bump workflow.

## Reference file contents (summary; full content authored in implementation)

### `references/principles.md` — applies to every dockerfile

- Multi-stage discipline; `AS builder` consistent.
- Layer cache order: lockfile + metadata first, sources second, generated artefacts last.
- `COPY --link` applied **only** to coarse inter-stage and final-stage asset copies; **not** on every dependency-cache COPY (measurements show it loses there).
- BuildKit cache mounts (`--mount=type=cache,target=/root/.cache/uv` and `target=/root/.bun/install/cache`).
- BuildKit bind mounts for lockfiles read but not retained (`--mount=type=bind,source=uv.lock,target=uv.lock`).
- `.dockerignore` excludes: `.git`, `.venv`, `node_modules`, `**/__pycache__`, `**/*.pyc`, `dist`, `build`, `.svelte-kit`, `.ruff_cache`, `.pytest_cache`, `.docker/`, `.dagger/`, `*.log`, model weights, `.env*`, `.claude/`, `.cursor*`, IDE artefacts.
- Non-root final stage; `useradd -r -u 10001 app`; `USER app` before `CMD`.
- `tini` as PID 1 for Python entrypoints.
- `HEALTHCHECK` only when no orchestrator probe owns readiness; `--start-period` set; stdlib HTTP check.
- Digest pinning + bump workflow (`docker buildx imagetools inspect <ref>`).
- Hadolint rules to obey: DL3007, DL3008, DL3009, DL3015, DL3042, DL3059, DL4006.

### `references/python-uv.md` — every Python image

- Official two-step `uv sync`: deps first (`--no-install-workspace --package <name>`), sources second.
- `UV_LINK_MODE=copy`, `UV_COMPILE_BYTECODE=1`, `UV_PYTHON_DOWNLOADS` set per base image.
- Workspace handling for rask's `projects/<name>/pyproject.toml` shape — bind-mount the relevant pyproject set during first sync; COPY real sources for second sync.
- `htrflow` from git → keep `git` in builder only.
- `--no-editable` installs workspace member as wheel; final stage doesn't need source tree.
- Venv copied out via `COPY --from=builder --link /app/.venv /app/.venv`; PATH includes `.venv/bin`; no `uv` binary in final image.

### `references/gpu-cuda.md` — runner-class images

- CUDA variant matrix: `-base` / `-runtime` / `-devel`. Default `-runtime`; switch to `-devel` only if a wheel compiles CUDA C++ at install.
- uv-managed Python on the CUDA Ubuntu base (`UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_PYTHON_PREFERENCE=only-managed`), not deadsnakes PPA.
- System libs Ray + PyTorch actually need: `libgomp1`, `ca-certificates`, `tini`. Nothing else.
- **Model-weight strategy — prescribed default: runtime-download to `$HF_HOME=/cache/hf` (persistent volume).** Bake-at-build only when deploy target has no volume. Sidecar init-container is K8s territory and out of scope.
- Optional build-arg-gated CUDA smoke test (`python -c "import torch; assert torch.cuda.is_available()"`).

### `references/static-nginx.md` — frontend-class images

- Rationale for nginx over FastAPI for static assets (sendfile, immutable headers, ~5 MB RAM).
- Builder: `oven/bun:1-debian`, cache mount on `/root/.bun/install/cache`, `bun install --frozen-lockfile`, `bun run build`. adapter-static output dir: `build/`.
- Build context is repo root; `bun install` runs from root (resolves bun workspace deps including `packages/component-lib`); build runs via `bun --cwd components/apps/frontend run build`.
- Runtime: `nginxinc/nginx-unprivileged:1.27-alpine` — listens on 8080, UID 101, PID file in `/tmp`, no `USER` directive needed.
- SPA nginx config essentials: `try_files $uri $uri.html $uri/index.html /index.html;` fallback; `/_app/immutable/` location with `Cache-Control: public, max-age=31536000, immutable`; root location `no-cache`; gzip on for text MIMEs.
- HEALTHCHECK via wget (alpine ships it).
- Rationale for separate frontend container vs. serving from viewer: independent deploy lifecycles.

## Templates

Each template is a complete, working dockerfile (or config file). All `FROM` references digest-pinned at time of authoring; the bump workflow documented in `principles.md` covers updating them.

### `templates/viewer.dockerfile`
- Base: `python:3.13-slim-bookworm@sha256:...`
- Builder: `uv` from `ghcr.io/astral-sh/uv` pinned; bind-mount `uv.lock` + relevant `pyproject.toml`s; `uv sync --locked --no-install-workspace --package viewer --no-editable`; COPY sources; `uv sync --locked --package viewer --no-editable`.
- Final: same slim base, `tini` only; `COPY --from=builder --link /app/.venv /app/.venv`; UID 10001 `app` user; `EXPOSE 8888`; `HEALTHCHECK` on `/health`; `ENTRYPOINT ["/usr/bin/tini","--"]`; `CMD ["uvicorn","viewer.app:app","--host","0.0.0.0","--port","8888"]`.

### `templates/runner.dockerfile`
- Base: `nvidia/cuda:12.4.0-runtime-ubuntu22.04@sha256:...`
- Builder: install `libgomp1 git tini ca-certificates`; `uv` binary; `UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_PYTHON_PREFERENCE=only-managed`; two-step sync `--package runner`; bind-mount lockfile + `projects/runner/pyproject.toml` + `packages/` + `components/`.
- Final: same runtime base, `libgomp1 tini ca-certificates`; `COPY --from=builder --link /opt/uv/python /opt/uv/python` and `/app/.venv`; PATH `/app/.venv/bin`; `HF_HOME=/cache/hf`; non-root user; `ENTRYPOINT ["/usr/bin/tini","--"]`; `CMD ["python","-m","runner"]`.

### `templates/frontend.dockerfile`
- Builder: `oven/bun:1-debian@sha256:...`; cache mount on `/root/.bun/install/cache`. To resolve bun workspaces, bind-mount **root `package.json` + root `bun.lock` + every workspace member's `package.json`** (`components/apps/frontend/package.json`, `packages/component-lib/package.json`); run `bun install --frozen-lockfile` from repo root. Then COPY `components/apps/frontend/` + `packages/component-lib/` sources; build via `bun --cwd components/apps/frontend run build`.
- Final: `nginxinc/nginx-unprivileged:1.27-alpine@sha256:...`; COPY `frontend.nginx.conf` → `/etc/nginx/conf.d/default.conf`; `COPY --from=builder --link build/ → /usr/share/nginx/html`; `EXPOSE 8080`; `HEALTHCHECK` via wget.

### `templates/frontend.nginx.conf`
- `listen 8080;`, `root /usr/share/nginx/html;`, gzip on for `text/css application/javascript application/json image/svg+xml`.
- `/_app/immutable/` location → `Cache-Control: public, max-age=31536000, immutable`.
- Root location → `try_files $uri $uri.html $uri/index.html /index.html;` + `Cache-Control: no-cache`.

### `templates/dockerignore`
- File ships under `templates/dockerignore` (without leading dot) to avoid being silently applied to the skill itself. The skill instructs the user to `cp` it to repo-root `.dockerignore` on install.

## Out of scope

- `.dagger/main.go` Go module that consumes the dockerfiles.
- Image registry, tagging, signing, SBOM generation.
- Compose / Kubernetes / Helm manifests.
- Multi-arch builds (belongs in dagger code).
- Runtime config injection (env vars, secrets).

## Acceptance criteria

The skill is "done" when:

1. `.claude/skills/dockerfile/` exists with `SKILL.md`, four `references/*.md`, and five `templates/*` files.
2. SKILL.md frontmatter `description` clearly states the trigger conditions and contract.
3. All three templates build successfully against the actual rask repo (verified by running `docker buildx build -f .docker/<name>.dockerfile .` after copying the template into `.docker/`).
4. Each template's second build is dominated by cache hits (lockfile/source layer cache reuse verifiable in build output).
5. `hadolint` passes on all three templates with the rule set listed in `principles.md`.
6. The `dockerignore` template is installable to repo root via straight copy.
7. The skill is invoked successfully when a future Claude session is asked to "add a new image to .docker".

## Non-goals (deliberately deferred to future work)

- Automating template instantiation (a `scripts/new-dockerfile.sh` was offered and declined).
- Auto-copying `templates/dockerignore` to repo root — user does it deliberately.
- Adding `hadolint` to `make check` — flagged but not part of this skill's scope.

## References cited in the skill body

- [Docker Build best practices](https://docs.docker.com/build/building/best-practices/)
- [Dockerfile reference](https://docs.docker.com/reference/dockerfile/)
- [uv: Using uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/)
- [uv: Installing Python](https://docs.astral.sh/uv/guides/install-python/)
- [nvidia/cuda Docker Hub](https://hub.docker.com/r/nvidia/cuda)
- [Red Hat: PyTorch + NVIDIA containers](https://next.redhat.com/2025/08/26/a-developers-guide-to-pytorch-containers-and-nvidia-solving-the-puzzle/)
- [Ray installation](https://docs.ray.io/en/latest/ray-overview/installation.html)
- [nginx-unprivileged](https://github.com/nginx/docker-nginx-unprivileged)
- [SvelteKit kit#15150 (adapter-static + nginx)](https://github.com/sveltejs/kit/issues/15150)
- [Hadolint rules](https://github.com/hadolint/hadolint/wiki)
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html)
- [Yelp/dumb-init](https://github.com/Yelp/dumb-init) + [Peter Malmgren: PID 1](https://petermalmgren.com/signal-handling-docker/)
- [Docker blog: Advanced Dockerfiles & BuildKit](https://www.docker.com/blog/advanced-dockerfiles-faster-builds-and-smaller-images-using-buildkit-and-multistage-builds/)
- [Depot: Why you should avoid COPY --link](https://depot.dev/blog/why-you-should-avoid-copy-link-in-your-dockerfile)
