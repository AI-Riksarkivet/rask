# Python + uv in Docker (rask)

Patterns that apply to every Python image in this repo. Loaded when authoring a Python dockerfile.

## Two-step `uv sync`: `--frozen` then `--locked`

Installing dependencies for a uv workspace project inside Docker requires two separate `uv sync` invocations because uv distinguishes between resolving deps from the lockfile versus verifying that the lockfile matches the actual resolved dependency graph. The problem is that on the first `RUN` step the workspace member source directories (e.g. `packages/htr`, `components/services/viewer`) are not yet present in the image filesystem — only their `pyproject.toml` metadata has been bind-mounted. Using `--locked` at this point causes uv to attempt resolving workspace members as sources and fail because the source trees are absent. This is documented in uv issues #16758, #16200, #12984, and #15459 as an explicit limitation for Dockerised workspace projects.

The fix is `--frozen` on step 1 (trust the lockfile, skip re-resolution, tolerate missing workspace sources) and `--locked` on step 2 after the real sources have been `COPY`-ed in (verify that the lockfile is still consistent now that uv can see the actual source trees). The `--no-install-workspace` flag on step 1 skips installing the workspace member itself — only its transitive pip dependencies are installed, which is exactly what is cached in that layer.

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=projects/viewer/pyproject.toml,target=projects/viewer/pyproject.toml \
    --mount=type=bind,source=packages/htr/pyproject.toml,target=packages/htr/pyproject.toml \
    --mount=type=bind,source=packages/storage/pyproject.toml,target=packages/storage/pyproject.toml \
    --mount=type=bind,source=components/apps/runner/pyproject.toml,target=components/apps/runner/pyproject.toml \
    --mount=type=bind,source=components/services/viewer/pyproject.toml,target=components/services/viewer/pyproject.toml \
    uv sync --frozen --no-install-workspace --package viewer --no-editable

COPY packages packages
COPY components/services/viewer components/services/viewer
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package viewer --no-editable
```

The bind-mount list in step 1 must include every `pyproject.toml` that uv reads during workspace resolution: the root `pyproject.toml`, the `uv.lock`, the project-under-build (`projects/viewer/pyproject.toml`), and every workspace member declared in `[tool.uv.workspace] members` regardless of whether the selected `--package` depends on it (currently `packages/htr`, `packages/storage`, `components/apps/runner`, `components/services/viewer`). Missing any one causes uv to fail with a workspace-member-not-found error even in `--frozen` mode.

## uv environment variables

Three environment variables should be set in the builder stage of every Python dockerfile:

```dockerfile
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
```

`UV_LINK_MODE=copy` is required when using `--mount=type=cache`. The uv cache and the venv target live on different mount points inside the build container; uv defaults to hard-linking wheel files from the cache into the venv, but hard links cannot cross mount-point boundaries. Without `copy` mode, uv silently falls back to a slower copy anyway — setting it explicitly avoids the warning and makes the intent clear.

`UV_COMPILE_BYTECODE=1` pre-compiles all installed `.py` files to `.pyc` at install time. This shifts the bytecode compilation cost from the first container cold start to build time. For images that start frequently (autoscaling, spot teardown) this is a meaningful latency improvement.

`UV_PYTHON_DOWNLOADS` controls whether uv may auto-download a managed Python distribution. The supported values (as of uv 0.5) are `auto`/`true`, `manual`, and `never`/`false` — `only-managed` is NOT a valid value for this variable (it belongs to `UV_PYTHON_PREFERENCE`). For `python:3.13-slim`-based images, Python is already provided by the base image — set `UV_PYTHON_DOWNLOADS=never` so uv never tries to fetch its own. For CUDA-based images (e.g. `nvidia/cuda:12.x-runtime-ubuntu22.04`) where Python is not pre-installed, leave it at `auto` (or set explicitly) and pair with `UV_PYTHON_PREFERENCE=only-managed` so uv installs its own Python and never falls back to a system one.

## `UV_PROJECT_ENVIRONMENT=/opt/venv`

By default, uv places the project virtualenv under the current working directory (`.venv/`). For containerised deployments this interacts badly with dev-compose bind-mounts: if the host mounts the source tree at `/app`, the bind-mount shadows `/app/.venv` and the venv disappears at runtime. Setting `UV_PROJECT_ENVIRONMENT=/opt/venv` relocates the venv to a path that is never in the bind-mount path.

A second benefit is that `/opt/venv` is a stable, well-known path across builder and final stages. The final stage copies the venv with:

```dockerfile
COPY --from=builder --link /opt/venv /opt/venv
```

`--link` is appropriate here because `/opt/venv` is a large, pre-built artefact that moves between stages without modification — exactly the use case `--link` is optimised for.

## `PYTHONDONTWRITEBYTECODE=1` caveat

`PYTHONDONTWRITEBYTECODE=1` is commonly added to Python dockerfiles as a hygiene flag. Set it for clarity, but understand the interaction with `UV_COMPILE_BYTECODE=1`: uv has already written all `.pyc` files for installed packages at build time. `PYTHONDONTWRITEBYTECODE` only suppresses runtime bytecode generation for Python files that uv did not pre-compile — typically application source files that were `COPY`-ed into the image after `uv sync` ran, and any `.py` files in directories that uv does not manage.

The two flags are not contradictory. `UV_COMPILE_BYTECODE=1` ensures installed packages are pre-compiled; `PYTHONDONTWRITEBYTECODE=1` prevents the interpreter from writing `.pyc` files at runtime next to any source files that land in the image, which keeps the filesystem clean. Both should be present.

## Workspace handling for `projects/<name>/`

Rask's deployable projects live in `projects/<name>/pyproject.toml` and compose workspace members rather than containing code. During the step 1 `uv sync --frozen` call, uv needs to read every `pyproject.toml` in the workspace to construct the dependency graph, but the source files must not be present yet (they would bust the layer cache on every source change).

Bind-mount the following `pyproject.toml` files:
- `pyproject.toml` (root workspace manifest)
- `uv.lock` (lockfile)
- `projects/<name>/pyproject.toml` (the deployable being built)
- Every member declared in `[tool.uv.workspace] members` — currently `packages/htr/pyproject.toml`, `packages/storage/pyproject.toml`, `components/apps/runner/pyproject.toml`, `components/services/viewer/pyproject.toml`

After the first sync, `COPY` only the source trees the deployable actually needs (e.g. `packages/` + the relevant `components/<layer>/<name>/`) before running `uv sync --locked`. Use `--package <name>` consistently in both sync steps to scope the install to the correct deployable. Switching deployables (viewer ↔ runner) is a one-flag change; the workspace-wide bind-mount list does not need to change unless `[tool.uv.workspace] members` itself changes.

## arm64 cache-mount is load-bearing

On `linux/arm64` builds — which includes all Apple silicon developer machines running Docker Desktop — many packages that ship as `manylinux` wheels on `linux/amd64` must be compiled from source on `arm64` because compatible wheels are absent from PyPI. This affects packages with Rust or C extensions: `cryptography`, `pydantic-core`, `numpy`, `tiktoken`, and `htrflow`'s ML dependencies.

Without the `--mount=type=cache,target=/root/.cache/uv` mount, every `docker build` recompiles these from source. A clean build with compilation can take 10+ minutes on Apple silicon. With the cache mount, the compiled wheels survive between builds and subsequent runs complete in 30 seconds or less. Never remove or comment out the cache mount to "simplify" a dockerfile — on arm64 it is the single biggest factor in developer build time.

The cache mount is a build-time side-channel: it is never included in image layers and never reaches the final image.

## `htrflow` from git

`htrflow` is declared as a git-source dependency in `components/apps/runner/pyproject.toml`:

```toml
[tool.uv.sources]
htrflow = { git = "https://github.com/AI-Riksarkivet/htrflow.git" }
```

Cloning a git dependency requires `git` to be installed in the builder stage. Add it there — and only there:

```dockerfile
# ── builder ────────────────────────────────────────────────────────────────────
FROM base AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
```

The final stage copies only the compiled venv (`COPY --from=builder --link /opt/venv /opt/venv`) and never needs `git`. Do not install `git` in the `base` stage or the final stage — it is a build-time tool that adds surface area to the runtime image for no benefit.

## `--no-editable` installs as wheel

The `--no-editable` flag tells uv to install workspace members as proper wheels rather than editable installs (`.pth` pointer files back to the source tree). With `--no-editable`, the workspace member is compiled into a wheel and unpacked into `.venv/lib/python3.13/site-packages/<name>/` exactly like any third-party package.

The consequence is that the final image stage does not need the Python source files on disk. The venv copy (`COPY --from=builder --link /opt/venv /opt/venv`) is sufficient for the application to run. This keeps the final image smaller and removes the build-context source tree from the runtime filesystem, which reduces the attack surface.

## Final-stage venv copy

The final stage receives the venv from the builder via a single `COPY --link` and activates it by prepending its `bin/` directory to `PATH`:

```dockerfile
COPY --from=builder --link /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
```

The `uv` binary is not present in the final stage and is not needed — all packages are already installed in the venv. Do not install uv in the final stage. The `PATH` `ENV` line ensures that `python`, `gunicorn`, `uvicorn`, and any other console-scripts installed by the packages resolve to the venv binaries without requiring an explicit activation step in `CMD` or `ENTRYPOINT`. Keeping uv out of the final image also removes a potential supply-chain vector: uv itself is a Rust binary that fetches from the network, and there is no reason to ship that capability in a production runtime image.
