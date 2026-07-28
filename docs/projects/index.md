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
| `ray` | `services/ray_api` (uv member `ray-api`) | [Services](../components/services.md) |
| `controlplane` | `services/controlplane` | [Services](../components/services.md) |
| `runner` | `runners/htr` (+ `htr`, `storage`, `htrflow` from git) | [Runner](runner.md) |

There is no `viewer` deployable in the fleet — the old viewer monolith was
dissolved (June 2026), and the R6/R20 wave (2026-07-28) retired
core-api/search-api/volumes-api into the media plane (which builds from the
`lance-rest-catalog` image).

## Building a deployable

One dockerfile per deployable under `.docker/`:

- `rask-gateway` ← `.docker/gateway.dockerfile` (slim Python, `:8888`).
- `ray` ← `.docker/ray.dockerfile` (slim Python, `:8804`).
- `controlplane` ← `.docker/controlplane.dockerfile` (slim Python, `:8820`).
- `ray-cluster` ← `.docker/ray-cluster.dockerfile` (CUDA base — the Ray head/Serve image).
- `rask-runner` ← `.docker/runner.dockerfile` (CUDA base, GPU).

The seven SvelteKit SSR apps under `frontend/microfrontends/` (`home` — the catch-all — plus `overview`/`compute`/`discover`/`storage`/`train`/`studio`)
all build from one parametrized `.docker/frontend.dockerfile`
(`--build-arg APP=<dir>`, Bun server).

See [Deployment](../architecture/deployment.md) for the image and cluster
details.
