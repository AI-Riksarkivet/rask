# Quick Start

This page gets you from a fresh clone to a running estate. On a new computer, `scripts/new-machine.sh` walks through
all of it: the toolchain, the clone, and the k3s estate installed from empty.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — Python 3.13 toolchain and workspace manager.
- **[Bun](https://bun.sh)** — the only JavaScript runtime/package manager used
  (`npm`/`npx`/`pnpm` are intentionally not on PATH).
- **[Dagger](https://dagger.io)** — builds every image; docker is never used.
- For the in-cluster estate: a host that can run **k3s** (`make k3s-install` installs it with helm).

## Install

```bash
make install        # = bun --cwd=frontend install + uv sync --all-packages + git hooks + make claude-bootstrap
make test           # offline suites
make check          # fmt + lint + import contracts + typecheck + knip + fga-test
```

!!! warning "Shared virtualenv"
    A plain `uv sync` prunes the shared venv to the root + dev members. To keep
    every workspace package available, use `uv sync --all-packages` and launch
    long-running processes with `uv run --no-sync` so they don't re-prune it.

## Run one zone, no cluster

```bash
make dev-zone ZONE=lakehouse   # one zone + its seed-driven mock upstreams
```

The leanest loop and the only one that needs nothing else: auth is off and cross-zone links 404, because one zone is
listening. `lakehouse`, `explorer`, `annotator` and `models` ship a seed; `home` renders its empty state.

## Run the lakehouse (k3s)

The lakehouse services (catalog, lineage, maintenance, notifications) need Dapr sidecars, NATS JetStream, OpenFGA and
an object store, so they run in-cluster. Development installs those prerequisites from `deploy/fixtures`, each its own
release, before rask's:

```bash
make k3s-install      # one-time: k3s + helm (sudo)
make k3s-build        # build every image as :dev, through Dagger
make k3s-import       # side-load the :dev images into k3s
make k3s-up           # the fixtures, then helm upgrade --install rask ./chart --wait
make k9s              # inspect it
```

See [Deployment](../architecture/deployment.md) for the prerequisites, the images and the in-cluster loop.

## Run the frontend and the outer fleet locally

```bash
make dev-micro         # gateway :8888 + compute :8804 + flows :8840 + notifications :8850 +
                       #   the explorer trio from :8101 (scripts/dev-micro.sh)
make dev-frontends     # all 7 SvelteKit SSR zones behind the Turborepo proxy on :3024
```

Open `:3024`, not a zone's own port. `make dev-micro` starts no catalog, lineage or maintenance, so lakehouse pages
need the k3s estate behind them.

!!! tip "Frontend host binding"
    The Vite dev server binds loopback-only by default. Start it with `--host`
    (e.g. `bun --cwd=frontend/microfrontends/home run dev -- --host`) to reach it
    over IPv4 `localhost` or the LAN.

## Common commands

| Goal | Command |
|---|---|
| Build everything | `make build` |
| Run the offline tests | `make test` |
| Format + lint + typecheck | `make check` |
| Single Python test | `uv run pytest services/catalog/tests/<file>.py::<test_name>` |
| Frontend type-check | `bun --cwd=frontend run check` |
| Storybook (@rask/ui) | `make storybook` (→ `:6006`) |

Next: read **[Configuration](configuration.md)** for the environment variables that wire the services together.
