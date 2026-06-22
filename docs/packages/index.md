# Packages

`packages/` holds reusable libraries with **no entrypoints** — pure building
blocks imported by the runnable components.

| Package | Language | Imported by | Docs |
|---|---|---|---|
| `htr` | Python | runner, scripts | [HTR](htr.md) · [API reference](../reference/htr.md) |
| `storage` | Python | runner, core, search-api, scripts | [Storage](storage.md) · [API reference](../reference/storage.md) |
| `service-kit` | Python | core, core-api, orchestrator, search-api, volumes-api, ray-api | — |
| `ray-kit` | Python | ray-api, core | — |
| `ui` (`@rask/ui`) | TS / Svelte | frontend, storage-frontend, compute-frontend | [UI Components](../components/ui.md) |
| `api` (`@rask/api`) | TS | frontend, storage-frontend, compute-frontend | — |

## Conventions

- **No entrypoints.** A package never defines a CLI or service; it exposes
  functions and classes via its top-level `__init__`.
- **Picklable by design.** Anything shipped into a Ray actor (storage sources,
  HTR actors) drops live clients in `__getstate__` and rebuilds them lazily from
  a factory, so it survives pickling across the cluster.
- **Explicit workspace membership.** A new package must be added to both the root
  `pyproject.toml` workspace members and (for JS) the root `package.json`
  workspaces.

!!! note "`packages/control` is gone"
    The former `control` package (sync + chunk submission) was absorbed into the
    core brick (`components/services/core`) service layer
    (`services/{sync,submission}`). Don't reference it — only stale bytecode
    remains.
