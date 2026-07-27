# rask

> ⚠️ **Work in progress — don't use.**

Polyglot monorepo for Riksarkivet HTR + search infrastructure. Python managed with [uv]; JS/TS managed with [Bun]; documentation built with [Zensical]; Dagger is wired for build/release.

## Layout

Two language-pure planes: Python at the repo root, all JS/TS under `frontend/`.

- `packages/` — reusable Python libraries (uv workspace members): `htr`, `storage`, `service-kit`, `ray-kit`, `tracker`, `validate`
- `services/` — runnable Python code:
  - `runner` — Python CLI driving Ray Data HTR pipelines
  - FastAPI services: `gateway` (reverse proxy `:8888`) + the `core` package (composed by `core-api` + `orchestrator`) + `volumes-api` + `search-api` + `ray-api`
- `frontend/` — the JS/TS plane; its own Bun + Turborepo workspace root (lint/format via oxlint + oxfmt):
  - `microfrontends/` — seven SvelteKit 2 + Svelte 5 **SSR** apps (`svelte-adapter-bun`): `home` (catch-all, owns `/`) + six domain zones (`overview`/`compute`/`discover`/`storage`/`train`/`studio`), composed by the Turborepo `:3024` proxy in dev / k3s Ingress in prod
  - `packages/` — `@rask/ui` (Svelte 5 + Bits UI + Tailwind 4 component library with Storybook), `@rask/api` (shared typed gateway client), `@rask/zone-contract` (the cross-zone link guard)
- `scripts/` — dev/ops scripts, shell + Python (indexing, EAD harvesting, IIIF downloads, Ray job submission, the local fleet and k3s installers)
- `docs/` — Zensical documentation source; deployed by `.github/workflows/docs.yml`
- `.claude/` — project-local Claude Code config (skills, commands, hooks)

Workspace membership is **globbed per plane** — `pyproject.toml` `[tool.uv.workspace] members = ["packages/*", "services/*"]` (uv) and `frontend/package.json` `workspaces = ["microfrontends/*", "packages/*"]` (Bun). A new directory joins its plane automatically, provided it ships a `pyproject.toml` / `package.json`. Deployables build from the root workspace via `uv sync --package <name>` (one dockerfile per deployable in `.docker/`).

## Quick start

```bash
bun --cwd=frontend install
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
bun --cwd=frontend run dev:ui          # svelte-package -w
bun --cwd=frontend run storybook       # localhost:6006
```

Or from the root:

```bash
make storybook
```

## Common Make targets

| Target                                                          | What it does                                           |
| --------------------------------------------------------------- | ------------------------------------------------------ |
| `make install`                                                  | `bun --cwd=frontend install` + `uv sync`               |
| `make build`                                                    | Build everything (`uv sync` + the `frontend` turbo build) |
| `make test`                                                     | Run pytest                                             |
| `make check`                                                    | `fmt` + `lint` + `typecheck` + `knip`                  |
| `make viewer`                                                   | Run the viewer FastAPI on `:8888`                      |
| `make home`                                                     | Run the `home` catch-all frontend (vite proxies /api → `:8888`) |
| `make ray-up` / `make ray-down`                                 | Start / stop a local Ray head node                     |
| `make serve-up` / `make serve-down`                             | Deploy / tear down Ray Serve apps                      |
| `make search-index` / `make catalog-index` / `make harvest-ead` | Indexing & EAD pipelines                               |

See the `Makefile` for the complete list.

[uv]: https://docs.astral.sh/uv/
[Bun]: https://bun.sh
[Zensical]: https://zensical.com
