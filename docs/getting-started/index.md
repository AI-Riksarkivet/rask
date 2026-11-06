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
make install        # = bun --cwd=frontend install + uv sync --all-packages + git hooks + make k3s-deps + make claude-bootstrap
make test           # offline suites
make check          # fmt + lint + import contracts + typecheck + knip + fga-test
```

!!! warning "Shared virtualenv"
    A plain `uv sync` prunes the shared venv to the root + dev members. To keep
    every workspace package available, use `uv sync --all-packages` and launch
    long-running processes with `uv run --no-sync` so they don't re-prune it.

## Run the lakehouse (k3s)

The lakehouse services (catalog, lineage, maintenance) need Dapr sidecars, NATS JetStream, OpenFGA and
an object store, so they run in-cluster. Development installs those prerequisites from `deploy/fixtures`, each its own
release, before rask's:

```bash
make k3s-install      # one-time: k3s + helm (sudo)
make k3s-build        # build every image as :dev, through Dagger
make k3s-import       # side-load the :dev images into k3s
make k3s-up           # the fixtures, then helm upgrade --install rask ./chart --wait
make k3s-clients      # the console's release (web + the notifications inbox), after the lakehouse
make seed-dev         # projects, warehouses, namespaces and grants through the catalog's doors
make k9s              # inspect it
```

See [Deployment](../architecture/deployment.md) for the prerequisites, the images and the in-cluster loop.

## Run the console locally

```bash
make dev-k3s          # the console with Vite HMR against the in-cluster Gateway and Dex (k3s Traefik on :8080)
make dev              # the same, with /api pointed at RASK_EDGE_URL (default :8080) and /config.json from frontend/console/dev/config.json
make dev-micro        # the notifications inbox on the host (:8850, scripts/dev-micro.sh); no edge
```

The console is a single-page app with no backend of its own: the browser signs in to the estate's Dex and calls the
lakehouse's `/api/*` with its bearer, so it needs the k3s estate (or another edge on `RASK_EDGE_URL` that also serves
the issuer `dev/config.json` names) behind it. A missing or malformed `/config.json` answers a 503 that says why.
Its Playwright suite runs against mocked upstreams instead: `bun --cwd=frontend run test:e2e`.

!!! tip "Frontend host binding"
    The Vite dev server binds loopback-only by default. Start it with `--host`
    (e.g. `bun --cwd=frontend/console run dev -- --host`) to reach it over IPv4 `localhost` or the LAN. Signing in
    from another origin needs that origin's `/auth/callback` among `rask-console`'s redirect URIs and the origin in
    Dex's `allowedOrigins`, both in `deploy/fixtures/console/dex-values.yaml`; only `http://localhost:5173` is there.

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
