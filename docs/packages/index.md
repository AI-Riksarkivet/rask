# Packages

`packages/` holds reusable libraries with **no entrypoints** — pure building
blocks imported by the runnable components.

| Package | Language | Imported by | Docs |
|---|---|---|---|
| `htr` | Python | runner, viewer (schemas) | [HTR](htr.md) · [API reference](../reference/htr.md) |
| `storage` | Python | runner, viewer, scripts | [Storage](storage.md) · [API reference](../reference/storage.md) |
| `component-lib` | TS / Svelte | (standalone; Storybook) | [UI Components](../components/ui.md) |

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
    viewer's service layer. Don't reference it — only stale bytecode remains.
