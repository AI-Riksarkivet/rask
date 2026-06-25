# rask

> ⚠️ **Work in progress — don't use.**

Polyglot monorepo for Riksarkivet HTR + search infrastructure. Python managed with [uv]; JS/TS managed with [Bun]; documentation built with [Zensical]; Dagger is wired for build/release.

## Layout

- `packages/` — reusable bricks (workspace members; polyglot):
  - `htr`, `storage` — Python libraries (uv workspace members)
  - `@rask/ui` — Svelte 5 + Bits UI + Tailwind 4 component library with Storybook (Bun workspace)
- `components/` — runnable bricks:
  - `cli/runner` — Python CLI driving Ray Data HTR pipelines
  - `frontends/` — seven SvelteKit 2 + Svelte 5 **SSR** apps (`svelte-adapter-bun`): `home` (catch-all, owns `/`) + six domain zones (`overview`/`compute`/`discover`/`storage`/`train`/`studio`), composed by the Turborepo `:3024` proxy in dev / k3s Ingress in prod
  - `services/` — FastAPI services: `gateway` (reverse proxy `:8888`) + the `core` brick (composed by `core-api` + `orchestrator`) + `volumes-api` + `search-api` + `ray-api`
  - `scripts/` — standalone Python utilities (indexing, EAD harvesting, IIIF downloads, Ray job submission, …)
- `projects/` — code-less composition pyprojects (one per deployable):
  - `core-api`, `gateway`, `orchestrator`, `ray-api`, `runner`, `search-api`, `volumes-api`
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

## Local deploy (k3s)

```bash
make k3s-install      # one-time: k3s + helm + NVIDIA device-plugin + KubeRay (sudo)
make k3s-build        # build fleet + frontend + ray images as :dev
make k3s-import       # side-load images into k3s
make k3s-up           # helm upgrade --install rask ./chart --wait
# UI: http://rask.local/   API: http://rask.local/api/health
# (add "127.0.0.1 rask.local" to /etc/hosts)
make k3s-down         # uninstall   |   make k3s-purge  # + delete PVCs
```

## Component library workflow

```bash
bun run --cwd packages/ui dev          # svelte-package -w
bun run --cwd packages/ui storybook    # localhost:6006
```

Or from the root:

```bash
make storybook
```

## Common Make targets

| Target                                                          | What it does                                           |
| --------------------------------------------------------------- | ------------------------------------------------------ |
| `make install`                                                  | `bun install` + `uv sync`                              |
| `make build`                                                    | Build everything (uv + bun, plus cargo if present)     |
| `make test`                                                     | Run pytest + bun test (plus cargo test if present)     |
| `make check`                                                    | `fmt` + `lint` + `typecheck`                           |
| `make viewer`                                                   | Run the viewer FastAPI on `:8888`                      |
| `make home`                                                     | Run the `home` catch-all frontend (vite proxies /api → `:8888`) |
| `make ray-up` / `make ray-down`                                 | Start / stop a local Ray head node                     |
| `make serve-up` / `make serve-down`                             | Deploy / tear down Ray Serve apps                      |
| `make search-index` / `make catalog-index` / `make harvest-ead` | Indexing & EAD pipelines                               |

See the `Makefile` for the complete list.

[uv]: https://docs.astral.sh/uv/
[Bun]: https://bun.sh
[Zensical]: https://zensical.com
