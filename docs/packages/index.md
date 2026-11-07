# Packages

Reusable libraries with **no entrypoints**: building blocks the runnable components import. Each language plane keeps
its own: Python libraries in the root `packages/`, TS/Svelte libraries in `frontend/packages/`.

| Package | Language | Imported by | Docs |
|---|---|---|---|
| `packages/lakehouse` | Python | catalog, lineage and maintenance, and no client: the lakehouse's one library (the app factory, the auth plane, the naming and guards, the data plane, the events, the lineage emission and signing). `lakehouse.storage` is its S3 client, STS and URI seam | [Storage](storage.md) · [API reference](../reference/storage.md) |
| `frontend/packages/ui` (`@rask/ui`) | TS / Svelte | the console (design system, Storybook) | — |
| `frontend/packages/api` (`@rask/api`) | TS | the console: the typed client generated from `docs/catalog-openapi.json` and `docs/lineage-openapi.json`, and the lineage client | — |
| `frontend/packages/dockview` (`@rask/dockview`) | TS / Svelte | the console's workbench | — |
| `frontend/packages/flow` (`@rask/flow`) | TS / Svelte | the console's governance views (the project hierarchy, the access graph) | — |

## Conventions

- **No entrypoints.** A package never defines a CLI or service; it exposes functions and classes via its top-level
  `__init__`.
- **Membership is globbed, per plane.** The uv workspace takes `members = ["packages/*", "services/*"]` in the root
  `pyproject.toml`; the Bun workspace takes `["console", "packages/*"]` in `frontend/package.json`. Both work only
  because each directory is language-pure: a new package is picked up by dropping it in the right plane with its own
  manifest.
