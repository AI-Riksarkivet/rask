# Monorepo Layout

rask is a monorepo with two **language-pure planes** — Python at the repo root, JS/TS under `frontend/`. Each plane is
its own workspace root (uv for Python, Bun for JS/TS), and inside each plane the library/runnable boundary is
deliberate — don't blur it. Deployables build straight from the root uv workspace, one `.docker/<name>.dockerfile` per
deployable running `uv sync --frozen --package <name>` against the root `uv.lock`.

The repository holds the lakehouse and its console, the lakehouse's own UI. Every client left it and is recovered from
git history at `64ef118a9`, the last commit that holds every client, when it moves to its own repository; the
notifications inbox is retired ([ADR 0193](../adr/0193-the-inbox-is-retired-and-the-console-joins-the-product.md)).

```mermaid
flowchart TD
    subgraph py["Python plane · repo root · uv workspace"]
        subgraph services["services/ · runnable code"]
            lake["the lakehouse: catalog · lineage · maintenance"]
        end
        subgraph packages["packages/ · reusable libraries (no entrypoints)"]
            plh["lakehouse (the lakehouse's one library, with lakehouse.storage and lakehouse.lineage)"]
        end
        cx["scripts/ · dev + ops tools"]
    end
    subgraph js["frontend/ · JS/TS plane · bun workspace"]
        app["console/ · one single-page app"]
        fpk["packages/ · ui · api · flow"]
    end
    lake --> plh
    app --> fpk
```

## `packages/` — reusable libraries, no entrypoints

There are **two** `packages/` directories, one per plane: the root one is Python-only, `frontend/packages/` is TS-only.
Each is unambiguous from inside its plane, and language purity is what lets both workspaces glob their members.

| Package | Language | Purpose |
|---|---|---|
| `packages/lakehouse` | Python | The lakehouse's one library ([ADR 0195](../adr/0195-the-three-kits-fold-into-one-lakehouse-library.md)), imported by catalog, lineage and maintenance and by no client: the lance app factory and what it installs (exceptions, the request-id middleware, logging, the probes and the drain, the body cap), the auth plane (`lakehouse.governed`), the naming and the input and credential guards (`lakehouse.arrow_ipc`, `lakehouse.storage_options`), the catalog's records, registries and governance guards, the namespace clients (`lakehouse.lancekit`), the events (`lakehouse.control_events`, `lakehouse.lineage_events`, `lakehouse.governed.subjects`, `lakehouse.signature_doors`, the outbox), the OpenLineage emission and Ed25519 signing seam (`lakehouse.lineage`), the store session, and [`lakehouse.storage`](../packages/storage.md), the S3 client, STS and URI seam. |
| `frontend/packages/ui` | TS / Svelte | `@rask/ui`: Svelte 5 + Bits UI + Tailwind 4 component library with Storybook; `@rask/ui/shell` exports the console's `AppShell`. |
| `frontend/packages/api` | TS | `@rask/api`: the typed client generated from `docs/catalog-openapi.json` and `docs/lineage-openapi.json` and the lineage client, calling same-origin `/api/*` with the signed-in person's bearer. |
| `frontend/packages/flow` | TS / Svelte | `@rask/flow`: the graph canvas the governance views use (the project hierarchy, the access graph). |

## `services/` · `frontend/console` · `scripts/` — runnable code

| Path | Type | Purpose |
|---|---|---|
| `services/{catalog,lineage,maintenance}` | FastAPI | The lakehouse: the governed Lance Namespace catalog with tier governance, OpenLineage intake, the durable `GET /events` feed and edge list (Lance tables the catalog governs) and the forwarder to a graph backend, and maintenance (expiry, purge, orphan scan, reconcile). |
| `frontend/console` | SvelteKit 3 + Svelte 5 (single-page app) | The console, the lakehouse's own UI, shipped by the product chart (`chart/templates/console.yaml`): static files with no backend of its own, served by its own small Bun server (`serve.ts`) with the runtime `config.json` the chart renders. The browser signs in to Dex with PKCE on the lakehouse's public `rask-console` client, keeps its tokens in memory under a strict Content Security Policy, and calls the catalog and lineage APIs with its bearer. |
| `scripts/` | Python + shell | Every dev/ops tool in one place: `k3s-install.sh`, the e2e stack drivers, the estate seed, … No production-state-changing CLIs — data enters and moves only through the catalog's namespace doors. |

## Deployables — workspace members with a dockerfile

There is **no `projects/` layer**. One image, `.docker/rest-catalog.dockerfile`, runs `uv sync --frozen` against the
**root** `uv.lock` and serves the lakehouse's `catalog`, `lineage` and `maintenance`. The console builds from `.docker/frontend.dockerfile`
(`make console-image`).

## Workspace membership is globbed

Both planes glob their members — `[tool.uv.workspace] members = ["packages/*", "services/*"]` in the root
`pyproject.toml`, `workspaces = ["console", "packages/*"]` in `frontend/package.json`. Globs are safe here **only
because the directories are language-pure**: uv errors on a member without a `pyproject.toml`, and bun silently skips a
directory without a `package.json`, so a mixed directory would force either an enumerated member list or an exclude
list. Adding a member is therefore just creating the directory with its manifest — no root file to edit.

## Toolchain

- **Python** via [uv](https://docs.astral.sh/uv/) (3.13), linted/formatted with **Ruff** (line length 160) and
  type-checked with **ty** (`uv run ty check`).
- **JS/TS** via **Bun** exclusively — `npm`/`npx`/`pnpm` are not on PATH. **oxlint** + **oxfmt** (tabs, single quotes,
  `printWidth: 100`) with `@rsvelte/fmt` for `.svelte`, plus `svelte-check`, each a plain bun script run from
  `frontend/`: `bun --cwd=frontend run lint`.
- **Images** are built by Dagger (`scripts/dagger-image.sh`), never by docker.
- Identifiers and env vars carry **no `ra-` prefix** — env vars are `RASK_*`.
