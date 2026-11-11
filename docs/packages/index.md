# Packages

Reusable libraries with **no entrypoints**: building blocks the runnable components import. Each language plane keeps
its own: Python libraries in the root `packages/`, TS/Svelte libraries in `frontend/packages/`.

| Package | Language | Imported by | Docs |
|---|---|---|---|
| `packages/lakehouse` | Python | catalog, lineage and maintenance, and no client: the lakehouse's one library (the app factory, the auth plane, the naming and guards, the data plane, the events, the lineage emission and signing). `lakehouse.storage` is its S3 client, STS and URI seam | [Storage](storage.md) · [API reference](../reference/storage.md) |
| `frontend/packages/ui` (`@rask/ui`) | TS / Svelte | the console (design system, Storybook), read from source; the one JS package (ADR 0201) | — |

## Conventions

- **No entrypoints.** A package never defines a CLI or service; it exposes functions and classes via its top-level
  `__init__`.
- **Membership is globbed, per plane.** The uv workspace takes `members = ["packages/*", "services/*"]` in the root
  `pyproject.toml`; the Bun workspace takes `["console", "packages/*"]` in `frontend/package.json`. Both work only
  because each directory is language-pure: a new package is picked up by dropping it in the right plane with its own
  manifest.
