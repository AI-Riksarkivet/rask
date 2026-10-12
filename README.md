<h1 align="center">LA<img src="docs/assets/lagom-peepo.png" alt="" height="42" align="absmiddle">GOM</h1>

<p align="center"><strong>rask</strong> — an agnostic multimodal lakehouse: Lance tables governed by a catalog, with lineage and authorization.</p>

> ⚠️ **Work in progress — don't use.**

*Lagom* is the Swedish word for "just the right amount" — not too little, not too much. It is the
design rule for this platform: every tier governed, nothing over-built, and no capability that cannot
say where its data came from.

A **Lance lakehouse** under Lance Namespace (a governed REST catalog that enforces the bronze → silver → gold
tiers in its write path, and OpenLineage provenance), **events** on Dapr + NATS JetStream, and **governance** over
all of it — Dex OIDC for identity, OpenFGA for authorization, S3-compatible storage (MinIO in development, the
platform's S3 in production). The lakehouse runs no ingest, no pipeline stage, no training and no workflow engine:
compute and orchestration are the user's, and a pipeline writes every tier through the catalog's Lance Namespace
doors like any other client (ADRs 0154 and 0155).

**The platform is workload-agnostic by design.** It stores and governs blobs and multimodal data;
no workload is its identity. Anything that reads bytes, writes a governed table and emits lineage fits the same
shape — text, image, audio, video, embeddings.

**The platform itself knows no modality, and that is enforced rather than aspirational.** Modality
lives in exactly one place: the sealed `runners/<workload>` projects. It must not leak into a shared seam —
the catalog and its tier governance, the maintenance sweep, lineage, notifications — because a seam that knows
one modality makes the next one a second-class citizen. The test for any shared seam is *would this still be
right for audio?*

Polyglot monorepo: Python via [uv], JS/TS via [Bun] + Turborepo (oxlint + oxfmt), docs via
[Zensical], hermetic builds via [Dagger] (`.dagger/` — **every image**, local and CI).

## Layout

Two language-pure planes: Python at the repo root, all JS/TS under `frontend/`.

- `packages/` — reusable Python libraries (uv workspace members, no entrypoints): `lineage-kit`,
  `ray-kit`, `service-kit`, `storage`, `validate`, plus `ray-cluster-env` (deps-only, ships no code)
- `services/` — runnable Python services (uv workspace members):
  - **lakehouse**: `catalog` (governed Lance REST catalog — projects → warehouses → namespaces →
    tables, and the bronze→silver→gold tier governance a pipeline's writes pass through), `lineage`
    (OpenLineage → AGE, and the durable `GET /events` log), `maintenance` (version expiry, trash purge, the orphan
    scan, cross-store reconcile; it still compacts and rebuilds indices until LH-467 #587 moves those rewrites out)
  - **media**: `viewer`, `search`, `annotator`
  - **fleet**: `gateway` (reverse proxy `:8888`), `flows` (the studio flow-builder's server half),
    `notifications` (the per-subject inbox), and `compute` (Ray introspection + Serve proxy), the companion of a
    bring-your-own Ray engine, which ships in its own chart (`deploy/fixtures/compute`) and has no gateway row
- `runners/` — **sealed model environments, not workspace members** (each owns its `pyproject.toml`
  and `uv.lock`, so torch/model pins never enter the fleet's resolution): `htr`, `asr`, `diarize`,
  `voiceprint`, `topics`, `kg`, `assist`, `insid3`, `dummy`
- `frontend/` — the JS/TS plane; its own Bun + Turborepo workspace root:
  - `microfrontends/` — **seven SvelteKit 2 + Svelte 5 zones**: `home` (catch-all, owns `/`,
    `/projects`, `/settings` and the OIDC BFF) + `lakehouse`, `explorer`, `annotator`, `compute`,
    `models`, `studio` — composed by the Turborepo `:3024` proxy in dev, by the Ingress in-cluster
  - `packages/` — `@rask/ui` (Svelte 5 + Bits UI + Tailwind 4 design system, Storybook), `@rask/api`,
    `@rask/explorer-api`, `@rask/dockview`, `@rask/flow`, `@rask/engine`, `@rask/labeling`,
    `@rask/config`, `@rask/zone-contract` (the estate's shape gates, a vitest suite)
- `scripts/` — all dev/ops scripts (indexing, harvesting, the dev fleet, the e2e stack drivers)
- `tests/` — cross-service suites: `unit`, `integration`, `e2e-py` (**live**, `-m e2e`, needs a
  deployed stack), `e2e` (Playwright, its own lockfile)
- `chart/` — the product chart: rask's services, zones and their configuration, with OpenFGA as its only
  (optional) subchart. Dapr, NATS JetStream, the CloudNativePG operator, External Secrets, Dex, the object store
  and observability are prerequisites, installed in development from `deploy/fixtures/` (ADR 0153)
- `.docker/` — one dockerfile per deployable; `.dagger/` — the build + CI module; `docs/` — Zensical
  source (the depth lives here)

Workspace membership is **globbed per plane**: `[tool.uv.workspace] members = ["packages/*",
"services/*"]` and `frontend/package.json` `workspaces = ["microfrontends/*", "packages/*"]`. A new
directory joins its plane automatically — provided it ships a `pyproject.toml` / `package.json`;
without one it is silently skipped. Deployables build from the root lock via `uv sync --package
<name>`; runners build from their own.

## Quick start

On a new computer, `scripts/new-machine.sh` walks through all of it: the toolchain, the clone, a handover kit
from the old machine (memory, `.env`, local branches), and the k3s estate installed from empty.

```bash
make install     # bun --cwd=frontend install + uv sync --all-packages + git hooks + make claude-bootstrap
make build
make test        # offline suites (deselects the live `e2e` + model-bound `slow` marks)
make check       # fmt + lint + typecheck + knip
```

## Run locally

```bash
make dev-zone ZONE=lakehouse   # ONE zone + its own mock upstreams — no cluster, no containers
make dev-frontends             # all seven zones behind the :3024 proxy
make dev-micro                 # gateway :8888 + compute, flows, notifications and the explorer trio
```

**`make dev-zone` is the leanest loop and the only one that needs nothing else** — it starts a zone
plus the seed-driven mock upstreams its own Playwright suite uses, so it also runs in a cloud sandbox
or CI. Auth is off and cross-zone links 404, because exactly one zone is listening. Open **`:3024`**,
not a zone's own port, when running the full composition. The lakehouse services (catalog, lineage,
maintenance) need Dapr, NATS, OpenFGA and an object store, so they run on k3s, not in `make dev-micro`.

## Clusters

The chart in `chart/` is the single deploy artifact for both.

- **k3s** — the long-lived local/single-node deploy, and where the live estate runs:

  ```bash
  make k3s-install                       # one-time host bootstrap (sudo)
  make k3s-build k3s-import k3s-up       # build, side-load, helm install + wait
  make k9s                               # inspect it
  make k3s-down / k3s-purge
  ```

- **kind** — throwaway and CI-shaped, used by the live-proof jobs:

  ```bash
  make kind-up kind-images kind-load kind-deploy
  make e2e-ci        # governed kind stack + the live e2e suites   (== CI e2e-stack job)
  ```

> Both clusters can exist at once, and a bare `kubectl` may reach the **kind** one. The k3s release
> lives at `KUBECONFIG=/etc/rancher/k3s/k3s.yaml` — the Makefile already sets it.

**Dagger builds every image — never `docker build`.** `dagger call image --name=<stem>` for anything
in `.docker/`, `dagger call zone-image --zone=<zone>` for a micro-frontend; `scripts/dagger-image.sh`
is the single seam. The dockerfile stays the source of truth, so a build cannot behave one way
locally and another in CI.

## Security scanning

```bash
make audit          # osv-scanner over every lockfile + .dagger/go.mod
make scan-config    # trivy misconfig + secret detection over dockerfiles + chart
make scan-secrets   # trufflehog over git history (gates on VERIFIED credentials only)
make scan-image NAME=gateway
```

## CI

`.github/workflows/ci.yml` calls the Dagger module's real functions — `lint`, `typecheck`, `openapi`,
`test`, `test-pg`, `charts`, `frontend`, `test-lineage`, `scan-*` — plus the kind-backed live jobs
(`make e2e-ci`), the per-zone Playwright suites, and the zone-image build+smoke.
Everything hermetic runs identically on your machine: `dagger call <fn>` == the CI step.

## Common Make targets

| Target                                  | What it does                                                         |
| --------------------------------------- | -------------------------------------------------------------------- |
| `make install` / `make build`           | Install both planes / build everything                               |
| `make test` / `make test-slow`          | pytest offline (`-m "not slow and not e2e"`) / + slow (needs a GPU)   |
| `make check` / `make ci`                | fmt + lint + typecheck + knip / + tests                              |
| `make dev-zone ZONE=<z>`                | One zone + its mocks, no cluster                                     |
| `make dev-micro` / `make dev-frontends` | Local backend fleet / all zones on `:3024`                           |
| `make k3s-build k3s-import k3s-up`      | The long-lived k3s deploy                                            |
| `make kind-up … kind-deploy`            | Throwaway kind cluster lifecycle                                     |
| `make e2e-ci`                           | The guarded live proofs (kind)                                       |
| `make e2e`                              | Playwright browser e2e against a running deploy                      |
| `make storybook`                        | `@rask/ui` Storybook on `:6006`                                      |
| `make dev-gc`                           | Reclaim the dev loop's two disk leaks (Dagger cache + dev registry)   |

See the `Makefile` (`make help`) for the complete list; `docs/` carries the depth
(`docs/architecture/layout.md`, `docs/architecture/deployment.md`).

[uv]: https://docs.astral.sh/uv/
[Bun]: https://bun.sh
[Zensical]: https://zensical.com
[Dagger]: https://dagger.io
