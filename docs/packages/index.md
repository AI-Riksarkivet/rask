# Packages

Reusable libraries with **no entrypoints** — pure building blocks imported by
the runnable components. Each language plane keeps its own: Python libraries in
the root `packages/`, TS/Svelte libraries in `frontend/packages/`.

| Package | Language | Imported by | Docs |
|---|---|---|---|
| `runners/htr` (the sealed HTR runner) | Python | runner, scripts | [HTR](htr.md) · [API reference](../reference/htr.md) |
| `packages/storage` | Python | runner, core, search-api, volumes-api, scripts | [Storage](storage.md) · [API reference](../reference/storage.md) |
| `packages/service-kit` | Python | core, core-api, orchestrator, search-api, volumes-api, ray-api | — |
| `packages/ray-kit` | Python | ray-api, core | — |
| `packages/tracker` | Python | (standalone; not yet wired into a component) | — |
| `packages/validate` | Python | (standalone; not yet wired into a component) | — |
| `frontend/packages/ui` (`@rask/ui`) | TS / Svelte | all 7 frontend apps | [UI Components](../components/ui.md) |
| `frontend/packages/api` (`@rask/api`) | TS | overview, compute, discover | — |
| `frontend/packages/zone-contract` (`@rask/zone-contract`) | TS | the cross-zone-reload gate (a test, not a lint rule) | — |

## Conventions

- **No entrypoints.** A package never defines a CLI or service; it exposes
  functions and classes via its top-level `__init__`.
- **Picklable by design.** Anything shipped into a Ray actor (storage sources,
  HTR actors) drops live clients in `__getstate__` and rebuilds them lazily from
  a factory, so it survives pickling across the cluster.
- **Membership is globbed, per plane.** The uv workspace takes
  `members = ["packages/*", "services/*"]` in the root `pyproject.toml`; the Bun
  workspace takes `["microfrontends/*", "packages/*"]` in `frontend/package.json`.
  Both work only because each directory is language-pure — a new package is
  picked up by dropping it in the right plane with its own manifest.

!!! note "`packages/control` is gone"
    The former `control` package (sync + chunk submission) was absorbed into the
    core package (`services/core`) service layer (`services/{sync,submission}`).
    Don't reference it — only stale bytecode remains.
