# Packages

Reusable libraries with **no entrypoints**: building blocks the runnable components import. Each language plane keeps
its own: Python libraries in the root `packages/`, TS/Svelte libraries in `frontend/packages/`.

| Package | Language | Imported by | Docs |
|---|---|---|---|
| `packages/service-kit` | Python | every service, the notifications inbox included: the platform package a client pins by lakehouse tag (the app factory, config, auth plane, naming and guards, and the contract the lakehouse's events publish: `control_events`, `lineage_events`, `governed.subjects`, `signature_doors`), and its pytest plugin | — |
| `packages/lakehouse-kit` | Python | catalog, lineage and maintenance: the lakehouse's kernel, which no client imports. `lakehouse_kit.storage` is its S3 client, STS and URI seam | [Storage](storage.md) · [API reference](../reference/storage.md) |
| `packages/lineage-kit` | Python | catalog, lineage, maintenance, and the inbox verifying the lakehouse's signed events | — |
| `frontend/packages/ui` (`@rask/ui`) | TS / Svelte | the console (design system, Storybook) | — |
| `frontend/packages/api` (`@rask/api`) | TS | the console: the typed client generated from `docs/catalog-openapi.json` and `docs/lineage-openapi.json`, and the inbox client | — |
| `frontend/packages/dockview` (`@rask/dockview`) | TS / Svelte | the console's workbench | — |
| `frontend/packages/flow` (`@rask/flow`) | TS / Svelte | the console's governance views (the project hierarchy, the access graph) | — |

## Conventions

- **No entrypoints.** A package never defines a CLI or service; it exposes functions and classes via its top-level
  `__init__`.
- **Membership is globbed, per plane.** The uv workspace takes `members = ["packages/*", "services/*"]` in the root
  `pyproject.toml`; the Bun workspace takes `["console", "packages/*"]` in `frontend/package.json`. Both work only
  because each directory is language-pure: a new package is picked up by dropping it in the right plane with its own
  manifest.
