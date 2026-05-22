# rask

Polyglot monorepo for Riksarkivet HTR + search infrastructure. Python managed with [uv]; JS/TS managed with [Bun]; documentation built with [Zensical]; Dagger is wired for build/release.

## Layout

- `packages/` — reusable bricks (workspace members; polyglot):
  - `htr`, `storage` — Python libraries (uv workspace members)
  - `oxen_componets` — Svelte 5 + Bits UI + Tailwind 4 component library with Storybook (Bun workspace)
- `components/` — runnable bricks:
  - `apps/runner` — Python CLI driving Ray Data HTR pipelines
  - `apps/frontend` — Vite + Svelte viewer frontend
  - `services/viewer` — FastAPI viewer backend (serves images + ALTO from object storage)
  - `scripts/` — standalone Python utilities (indexing, EAD harvesting, IIIF downloads, Ray job submission, …)
- `projects/` — code-less composition pyprojects (one per deployable):
  - `hcp`, `runner`, `viewer`
- `docs/` — Zensical documentation source; deployed by `.github/workflows/docs.yml`
- `contributions/` — contribution guidelines
- `.claude/` — project-local Claude Code config (skills, commands, hooks)

Workspace membership is **explicit** — see `pyproject.toml` `[tool.uv.workspace] members` (uv) and root `package.json` `workspaces` (Bun). Adding a new brick under `packages/` or `components/` requires adding the path to the relevant list; globs are deliberately not used.

## Quick start

```bash
bun install
uv sync
make build
```

## Component library workflow

```bash
bun run --cwd packages/oxen_componets dev          # svelte-package -w
bun run --cwd packages/oxen_componets storybook    # localhost:6006
```

Or from the root:

```bash
make storybook
```

## Common Make targets

| Target | What it does |
| --- | --- |
| `make install` | `bun install` + `uv sync` |
| `make build` | Build everything (uv + bun, plus cargo if present) |
| `make test` | Run pytest + bun test (plus cargo test if present) |
| `make check` | `fmt` + `lint` + `typecheck` |
| `make viewer` | Run the viewer FastAPI on `:8888` |
| `make viewer-frontend` | Run the viewer SvelteKit frontend (proxies to `:8888`) |
| `make ray-up` / `make ray-down` | Start / stop a local Ray head node |
| `make serve-up` / `make serve-down` | Deploy / tear down Ray Serve apps |
| `make search-index` / `make catalog-index` / `make harvest-ead` | Indexing & EAD pipelines |

See the `Makefile` for the complete list.

[uv]: https://docs.astral.sh/uv/
[Bun]: https://bun.sh
[Zensical]: https://zensical.com
