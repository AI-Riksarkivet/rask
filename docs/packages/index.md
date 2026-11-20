# Packages

Reusable libraries with **no entrypoints**: building blocks the runnable components import, in the root `packages/`.
The console's design system, `@riksarkivet/ui`, is a dependency from the `AI-Riksarkivet/ui` repository, pinned to a
commit.

| Package | Language | Imported by | Docs |
|---|---|---|---|
| `packages/lakehouse` | Python | catalog, lineage and maintenance, and no client: the lakehouse's one library (the app factory, the auth plane, the naming and guards, the data plane, the events, the lineage emission and signing). `lakehouse.storage` is how a process reaches its object store and holds its session there | [Storage](storage.md) · [API reference](../reference/storage.md) |

## Conventions

- **No entrypoints.** A package never defines a CLI or service; it exposes functions and classes via its top-level
  `__init__`.
- **Membership is globbed, per plane.** The uv workspace takes `members = ["packages/*", "services/*"]` in the root
  `pyproject.toml`; the Bun workspace takes `["console", "packages/*"]` in `frontend/package.json`. Both work only
  because each directory is language-pure: a new package is picked up by dropping it in the right plane with its own
  manifest.
