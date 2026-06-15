# Projects

`projects/<name>/pyproject.toml` is a **deployable composition** — it contains no
code, only a pin of the workspace members that make up one shippable artifact.

| Project | Composes | Docs |
|---|---|---|
| `runner` | `runner` + `htr` + `storage` (+ `htrflow` from git) | [Runner](runner.md) |
| `viewer` | `viewer` + `storage` | [Viewer](viewer.md) |

Each carries its own `uv.lock` and `.venv`, so a deployable resolves
independently of the rest of the workspace.

!!! warning "HCP is not a project"
    The nav lists an [HCP](hcp.md) page, but there is **no `projects/hcp`**. "HCP"
    is the Hitachi Content Platform — the S3 storage backend — documented there
    for completeness because the codebase and older docs reference it as if it
    were a deployable.

## Building a deployable

The container images mirror these compositions:

- `rask-runner` ← `projects/runner` (CUDA base, GPU).
- `rask-viewer` ← `projects/viewer` (slim Python, `:8888`).
- `rask-frontend` ← `components/apps/frontend` (nginx static).

See [Deployment](../architecture/deployment.md) for the image and cluster
details.
