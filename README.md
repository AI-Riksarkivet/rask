<h1 align="center">LA<img src="docs/assets/lagom-peepo.png" alt="" height="42" align="absmiddle">GOM</h1>

<p align="center"><strong>rask</strong> — an agnostic multimodal lakehouse: Lance tables governed by a catalog, with lineage and authorization.</p>

> ⚠️ **Work in progress — don't use.**

*Lagom* is the Swedish word for "just the right amount" — not too little, not too much. It is the
design rule for this platform: every tier governed, nothing over-built, and no capability that cannot
say where its data came from.

A **Lance lakehouse** under Lance Namespace (a governed REST catalog that enforces the bronze → silver → gold
tiers in its write path, and OpenLineage provenance), **events** on Dapr + NATS JetStream, and **governance** over
all of it — Dex OIDC for identity, OpenFGA for authorization, S3-compatible storage (RustFS in development, the
platform's S3 in production). The lakehouse runs no ingest, no pipeline stage, no training and no workflow engine:
compute and orchestration are the user's, and a pipeline writes every tier through the catalog's Lance Namespace
doors like any other client (ADRs 0154 and 0155).

**The platform is workload-agnostic by design.** It stores and governs blobs and multimodal data;
no workload is its identity. Anything that reads bytes, writes a governed table and emits lineage fits the same
shape — text, image, audio, video, embeddings. A workload's model, stage graph and output format are its own and live
outside this repository; no shared seam (the catalog and its tier governance, the maintenance sweep, lineage, the
events) names one. The test for any shared seam is *would this still be right for audio?*

This repository holds the lakehouse and its console, nothing else. Every other client (flows, compute and the Ray
engine fixtures, the annotator, the viewer and search with the explorer, table-maintenance, the sealed runners) left
it and is recovered from git history at `64ef118a9`, the last commit that holds every client, when it moves to its
own repository. The notifications inbox is retired, not moved: the lakehouse emits signed events and publishes their
targeting contract for any notifier a client builds (ADR 0193).

Polyglot monorepo: Python via [uv], JS/TS via [Bun] (oxlint + oxfmt), docs via [Zensical], hermetic builds via
[Dagger] (`.dagger/` — **every image**, local and CI).

## Layout

Two language-pure planes: Python at the repo root, all JS/TS under `frontend/`.

- `packages/` — reusable Python libraries (uv workspace members, no entrypoints): `service-kit` (the platform
  package a client pins by lakehouse tag: the app factory, the auth plane, the naming, and the contract the
  lakehouse's events publish), `lakehouse-kit` (the lakehouse's kernel, with `lakehouse_kit.storage`, its S3 client,
  STS and URI seam; no client imports it) and `lineage-kit` (OpenLineage emission and the Ed25519 signing seam)
- `services/` — runnable Python services (uv workspace members):
  - **the lakehouse**: `catalog` (governed Lance REST catalog — projects → warehouses → namespaces →
    tables, and the bronze→silver→gold tier governance a pipeline's writes pass through), `lineage`
    (the OpenLineage intake, the commit feed and edge list as Lance tables, and the durable `GET /events` log; the
    lineage graph is bring-your-own, ADR 0158), `maintenance` (version expiry, trash purge, the orphan scan,
    cross-store reconcile, and the list of tables due for a bring-your-own compaction or index optimize; ADR 0162)
  - **the edge** is the platform's Gateway (Kubernetes Gateway API), not a rask process: the chart's HTTPRoutes
    (`chart/templates/route.yaml`) send each public `/api/*` prefix to its service, and every service authenticates
    each request itself (ADR 0165)
- `frontend/` — the JS/TS plane; its own Bun workspace root:
  - `console/` — the console, the lakehouse's own UI: ONE SvelteKit 3 + Svelte 5 single-page app with no backend of
    its own, shipped by the product chart (ADR 0193). The browser signs in to Dex with PKCE on the lakehouse's public
    client `rask-console`, keeps its tokens in memory under a strict Content Security Policy, and calls the catalog and
    lineage APIs with its bearer. Its bell lists the reader's own writes, kept in the tab. A small Bun server
    (`serve.ts`) serves the built files and the runtime `config.json`
  - `packages/` — `@rask/ui` (Svelte 5 + Bits UI + Tailwind 4 design system, Storybook), `@rask/api` (the typed
    client generated from the OpenAPI specs), `@rask/dockview` (the workbench), `@rask/flow` (the governance graphs)
- `scripts/` — all dev/ops scripts (the e2e stack drivers, the estate seed, the chart and store tooling)
- `tests/` — cross-service suites: `unit`, `integration` and `e2e-py` (**live**, `-m e2e`, needs a
  deployed stack)
- `chart/` — the product chart: the lakehouse's services, its console and their configuration, with OpenFGA as its only
  (optional) subchart. Dapr, NATS JetStream, the CloudNativePG operator, External Secrets, Dex, the object store
  and observability are prerequisites, installed in development from `deploy/fixtures/` (ADR 0153)
- `.docker/` — one dockerfile per deployable; `.dagger/` — the build + CI module; `docs/` — Zensical
  source (the depth lives here)

Workspace membership is **globbed per plane**: `[tool.uv.workspace] members = ["packages/*",
"services/*"]` and `frontend/package.json` `workspaces = ["console", "packages/*"]`. A new
directory joins its plane automatically — provided it ships a `pyproject.toml` / `package.json`;
without one it is silently skipped. Deployables build from the root lock via `uv sync --package <name>`.

## Quick start

On a new computer, `scripts/new-machine.sh` walks through all of it: the toolchain, the clone, a handover kit
from the old machine (memory, `.env`, local branches), and the k3s estate installed from empty.

```bash
make install     # bun --cwd=frontend install + uv sync --all-packages + git hooks + make k3s-deps + make claude-bootstrap
make build
make test        # offline suites (deselects the live `e2e` + long-running `slow` marks)
make check       # fmt + lint + lint-imports + typecheck + knip + fga-test + go-fmt + chart-docs-check
```

## Run locally

```bash
make dev         # the console with Vite HMR, /config.json from frontend/console/dev/config.json, /api → RASK_EDGE_URL (default :8080)
make dev-k3s     # the same against the in-cluster Gateway and Dex, k3s Traefik on :8080 (reused when it already serves /dex)
```

The lakehouse services (catalog, lineage, maintenance) need Dapr, NATS, OpenFGA and an object store, so they run on
k3s, and the console reaches them through the cluster's Gateway (`make dev-k3s`).

## Clusters

The chart in `chart/` is the single deploy artifact for both.

- **k3s** — the long-lived local/single-node deploy, and where the live estate runs:

  ```bash
  make k3s-install                       # one-time host bootstrap (sudo)
  make k3s-build k3s-import              # build every image and side-load it into k3s
  make k3s-up K3S_REGISTRY=              # the fixtures, then rask's release (the lakehouse and its console) from git's values
                                         # (by default k3s-up pulls from K3S_REGISTRY, localhost:5000, at the pinned tags)
  make seed-dev                          # projects, warehouses, namespaces and grants through the catalog
  make k9s                               # inspect it
  make k3s-down / k3s-purge
  ```

- **kind** — throwaway and CI-shaped, used by the live-proof jobs:

  ```bash
  make e2e-ci        # governed kind stack + the live e2e suites (== CI e2e-stack job): every image the render side-loads
                     # is a Dagger-built tarball loaded with `kind load image-archive`, no docker
  make kind-up / kind-down                         # create or delete the bare kind cluster
  ```

> Both clusters can exist at once, and a bare `kubectl` may reach the **kind** one. The k3s release
> lives at `KUBECONFIG=/etc/rancher/k3s/k3s.yaml` — the Makefile already sets it.

**Dagger builds every image — never `docker build`.** `dagger call image --name=<stem>` for anything in `.docker/`
(`frontend` is the console's image, `make console-image`); `scripts/dagger-image.sh` is the single seam. The
dockerfile stays the source of truth, so a build cannot behave one way locally and another in CI.

## Security scanning

```bash
make audit          # osv-scanner over every lockfile + .dagger/go.mod
make scan-config    # trivy misconfig + secret detection over dockerfiles + chart
make scan-secrets   # trufflehog over git history (gates on VERIFIED credentials only)
make scan-image NAME=rest-catalog
```

## CI

`.github/workflows/ci.yml` calls the Dagger module's real functions — `lint`, `typecheck`, `openapi`,
`test`, `charts`, `frontend`, `scan-*` — plus the kind-backed live jobs (`make e2e-ci`) and the console's
Playwright suite. Everything hermetic runs identically on your machine: `dagger call <fn>` == the CI step.

## Common Make targets

| Target                                  | What it does                                                         |
| --------------------------------------- | -------------------------------------------------------------------- |
| `make install` / `make build`           | Install both planes / build everything                               |
| `make test` / `make test-slow`          | pytest offline (`-m "not slow and not e2e"`) / + slow                |
| `make check` / `make ci`                | fmt, lint, lint-imports, typecheck, knip, fga-test, go-fmt, chart-docs-check / + tests |
| `make dev` / `make dev-k3s`             | The console with Vite HMR / against the in-cluster Gateway           |
| `make console-image`                    | The console's image through Dagger                                   |
| `make k3s-up`                           | The long-lived k3s deploy, from the dev registry at the pinned tags (`K3S_REGISTRY=` after `k3s-build k3s-import` for side-loaded images) |
| `make kind-up` / `make kind-down`       | Throwaway kind cluster lifecycle                                     |
| `make e2e-ci`                           | The guarded live proofs (kind)                                       |
| `make storybook`                        | `@rask/ui` Storybook on `:6006`                                      |
| `make dev-gc`                           | Reclaim the dev loop's two disk leaks (Dagger cache + dev registry)   |

See the `Makefile` (`make help`) for the complete list; `docs/` carries the depth
(`docs/architecture/layout.md`, `docs/architecture/deployment.md`).

[uv]: https://docs.astral.sh/uv/
[Bun]: https://bun.sh
[Zensical]: https://zensical.com
[Dagger]: https://dagger.io
