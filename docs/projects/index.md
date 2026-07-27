# Projects

This section holds **narrative notes on rask's deployables and sub-projects**.

!!! note "The `projects/` directory is gone (July 2026)"
    rask used to keep a Polylith-style `projects/<name>/pyproject.toml`
    composition stub (with its own `uv.lock`) per deployable. That layer was
    removed: a deployable is now an **ordinary workspace member** built by its
    `.docker/<name>.dockerfile`, which runs `uv sync --frozen --package <name>`
    against the **root** `uv.lock` — one lock for dev, tests, and every image.

| Deployable | Workspace member | Docs |
|---|---|---|
| `gateway` | `services/gateway` | [Services](../components/services.md) |
| `core-api` | `services/core_api` (over `core`) | [Services](../components/services.md) |
| `orchestrator` | `services/orchestrator` (over `core`) | [Services](../components/services.md) |
| `volumes-api` | `services/volumes_api` | [Services](../components/services.md) |
| `search-api` | `services/search_api` | [Services](../components/services.md) |
| `ray-api` | `services/ray_api` | [Services](../components/services.md) |
| `runner` | `runners/htr` (+ `htr`, `storage`, `htrflow` from git) | [Runner](runner.md) |

There is no `viewer` deployable — the viewer was dissolved (June 2026) into the
gateway + per-domain services above.

## Building a deployable

One dockerfile per deployable under `.docker/`:

- `rask-gateway` ← `.docker/gateway.dockerfile` (slim Python, `:8888`).
- `rask-core-api` ← `.docker/core-api.dockerfile` (slim Python, `:8801`).
- `rask-orchestrator` ← `.docker/orchestrator.dockerfile` (slim Python, `:8810`).
- `rask-volumes-api` ← `.docker/volumes-api.dockerfile` (slim Python, `:8803`).
- `rask-search-api` ← `.docker/search-api.dockerfile` (slim Python, `:8802`).
- `rask-ray-api` ← `.docker/ray-api.dockerfile` (slim Python, `:8804`).
- `rask-runner` ← `.docker/runner.dockerfile` (CUDA base, GPU).

The seven SvelteKit SSR apps under `frontend/microfrontends/` (`home` — the catch-all — plus `overview`/`compute`/`discover`/`storage`/`train`/`studio`)
all build from one parametrized `.docker/frontend.dockerfile`
(`--build-arg APP=<dir>`, Bun server).

See [Deployment](../architecture/deployment.md) for the image and cluster
details.
