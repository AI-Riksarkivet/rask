# Quick Start

This page gets you from a fresh clone to a running stack.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — Python 3.13 toolchain and workspace manager.
- **[Bun](https://bun.sh)** — the only JavaScript runtime/package manager used
  (`npm`/`npx`/`pnpm` are intentionally not on PATH).
- **Docker** — for the local Postgres used by core-api/orchestrator and migrations.
- An NVIDIA GPU (for real HTR) or use the CPU-only smoke pipeline.

## Install

```bash
make install        # = bun install + uv sync
```

!!! warning "Shared virtualenv"
    A plain `uv sync` prunes the shared venv to the root + dev members. To keep
    every workspace package available, use `uv sync --all-packages` and launch
    long-running processes with `uv run --no-sync` so they don't re-prune it.

## Run the stack locally

```bash
make ray-up            # local Ray head on :6379, dashboard :8265
make serve-up          # deploy /transcribe + /htrflow on Ray Serve
make dev-micro         # the fleet: gateway :8888 + core-api :8801 + search :8802 +
                       #   volumes :8803 + ray :8804 + orchestrator :8810 (via dev-micro.sh)
make dev-frontends     # all 7 SvelteKit SSR apps + the Turborepo microfrontends
                       #   proxy on :3024 (single origin); each app proxies /api -> :8888
```

`make home` runs just the catch-all app on :5173 (serves `/` +
`/<project>/overview`); `make dev-frontends` brings up the full microfrontend zone
set behind the :3024 proxy.

Alternatively, `make viewer` runs the `core.main:app` monolith on :8888 as a
single-process dev convenience (no fleet needed). Each frontend app's Vite proxy
targets `:8888` either way.

Tear down with `make serve-down` / `make ray-down`.

!!! tip "Frontend host binding"
    The Vite dev server binds loopback-only by default. Start it with `--host`
    (e.g. `bun --cwd components/frontends/home run dev -- --host`) to reach it
    over IPv4 `localhost` or the LAN.

## Local Postgres + migrations

The core brick defaults to SQLite (`.cache/batches.db`) but uses Postgres in
production. To run Postgres locally:

```bash
make pg-up                  # docker postgres:16 at localhost:5432 (rask/rask/rask)
make pg-migrate             # alembic upgrade head
make pg-revision MSG="..."  # autogenerate the next migration
```

Connect with `postgresql://rask:rask@localhost:5432/rask`.

## Common commands

| Goal | Command |
|---|---|
| Build everything | `make build` |
| Run all tests | `make test` |
| Format + lint + typecheck | `make check` |
| Single Python test | `uv run pytest packages/htr/tests/test_geometry.py::test_name` |
| Frontend type-check | `bun --cwd components/frontends/home run check` |
| Storybook (@rask/ui) | `make storybook` (→ `:6006`) |
| Build the search index | `make search-index` |
| Build the catalog index | `make catalog-index` |

Next: read **[Concepts](concepts.md)** for the data model, then
**[Configuration](configuration.md)** for the environment variables that wire it
all to storage and clusters.
