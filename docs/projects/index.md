# Projects

`projects/<name>/pyproject.toml` is a **deployable composition** — it contains no
code, only a pin of the workspace members that make up one shippable artifact.

| Project | Composes | Docs |
|---|---|---|
| `gateway` | `gateway` + `service-kit` | [Services](../components/services.md) |
| `core-api` | `core` + `service-kit` + `storage` | [Services](../components/services.md) |
| `orchestrator` | `core` + `service-kit` + `storage` | [Services](../components/services.md) |
| `volumes-api` | `service-kit` + `storage` | [Services](../components/services.md) |
| `search-api` | `service-kit` + `storage` + `lancedb` | [Services](../components/services.md) |
| `ray-api` | `service-kit` + `ray-kit` | [Services](../components/services.md) |
| `runner` | `runner` + `htr` + `storage` (+ `htrflow` from git) | [Runner](runner.md) |

Each carries its own `uv.lock` and `.venv`, so a deployable resolves
independently of the rest of the workspace. There is no `projects/viewer` — the
viewer was dissolved (June 2026) into the gateway + per-domain services above.

!!! warning "HCP is not a project"
    The nav lists an [HCP](hcp.md) page, but there is **no `projects/hcp`**. "HCP"
    is the Hitachi Content Platform — the S3 storage backend — documented there
    for completeness because the codebase and older docs reference it as if it
    were a deployable.

## Building a deployable

The container images mirror these compositions:

- `rask-gateway` ← `projects/gateway` (slim Python, `:8888`).
- `rask-core-api` ← `projects/core-api` (slim Python, `:8801`).
- `rask-orchestrator` ← `projects/orchestrator` (slim Python, `:8810`).
- `rask-volumes-api` ← `projects/volumes-api` (slim Python, `:8803`).
- `rask-search-api` ← `projects/search-api` (slim Python, `:8802`).
- `rask-ray-api` ← `projects/ray-api` (slim Python, `:8804`).
- `rask-runner` ← `projects/runner` (CUDA base, GPU).
- `rask-frontend` ← `components/apps/frontend` (SvelteKit SSR, Bun server).
- `rask-storage-frontend` ← `components/apps/storage-frontend` (SSR, base `/storage`).
- `rask-compute-frontend` ← `components/apps/compute-frontend` (SSR, base `/compute`).

See [Deployment](../architecture/deployment.md) for the image and cluster
details.
