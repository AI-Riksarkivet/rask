# rask

> ⚠️ **Work in progress — don't use.** (The lance-ns merge is landing; see `docs/architecture/lance-ns-merge.md`.)

Riksarkivet's data + AI platform: a **Lance lakehouse** (governed REST catalog + OpenLineage lineage into Apache AGE) with an **event-driven medallion cascade** (Dapr pub/sub over NATS: raw → bronze → silver → gold), a **media plane** (viewer / search / annotator over audio+image corpora), and a **compute plane** (Ray Data/Serve pipelines: HTR image→ALTO, ASR, training). Governance is Dex OIDC + OpenFGA; storage is S3-compatible (RustFS in-cluster). The event-driven cascade is the direction of travel — it replaces rask's legacy S3-sync orchestration (P7 of the merge plan).

Polyglot monorepo: Python via [uv], JS/TS via [Bun] + Turborepo (oxlint + oxfmt), docs via [Zensical], hermetic CI via Dagger (`.dagger/`).

## Layout

Two language-pure planes: Python at the repo root, all JS/TS under `frontend/`.

- `packages/` — reusable Python libraries (uv workspace members, no entrypoints): `common`, `ratch`, `ray-kit`, `service-kit`, `storage`, `tracker`, `validate`
- `services/` — runnable Python services (uv workspace members):
  - lakehouse: `catalog` (governed Lance REST catalog), `lineage` (OpenLineage → AGE), `medallion` (cascade movers), `compaction`
  - media: `viewer`, `search`, `annotator`
  - fleet: `gateway` (reverse proxy `:8888`), `ray` (Ray introspection + Serve proxy; uv member `ray-api`), `controlplane` — the core-api husk and search/volumes died in the R6/R20 media wave (their capabilities serve from the media plane)
- `runners/` — **sealed model environments, not workspace members** (own `pyproject.toml` + `uv.lock` each, so torch/model pins never enter the fleet's resolution): `htr`, `asr`, `diarize`, `voiceprint`, `topics`, `kg`, `assist`
- `frontend/` — the JS/TS plane; its own Bun + Turborepo workspace root:
  - `microfrontends/` — **seven SvelteKit 2 + Svelte 5 zones**: `home` (catch-all, owns `/`) + `lakehouse`, `media`, `annotator`, `compute`, `studio`, `train` — composed by the Turborepo `:3024` proxy in dev, Ingress in-cluster
  - `packages/` — `@rask/ui` (Svelte 5 + Bits UI + Tailwind 4 design system, Storybook), `@rask/api` + media data layers, `@rask/zone-contract` (cross-zone link guard, a vitest suite)
- `scripts/` — all dev/ops scripts (indexing, harvesting, the dev fleet, the e2e stack drivers)
- `tests/` — cross-service suites: `unit`, `integration`, `e2e-py` (**live** suites, `-m e2e`, need a deployed stack), `e2e` (Playwright)
- `chart/` — the single Helm deploy artifact (fleet + subcharts: CNPG, RustFS, KubeRay, Dapr, NATS, OpenFGA, observability)
- `.docker/` — one dockerfile per deployable; `.dagger/` — the CI module; `docs/` — Zensical source (the depth lives here)

Workspace membership is **globbed per plane**: `[tool.uv.workspace] members = ["packages/*", "services/*"]` and `frontend/package.json` `workspaces = ["microfrontends/*", "packages/*"]`. A new directory joins its plane automatically — provided it ships a `pyproject.toml` / `package.json`. Deployables build from the root lock via `uv sync --package <name>`; runners build from their own locks.

## Quick start

```bash
make install     # bun --cwd=frontend install + uv sync
make build
make test        # offline suites (deselects the live `e2e` + model-bound `slow` marks)
make check       # fmt + lint + typecheck + knip
```

## Run locally (no cluster)

```bash
make dev-micro       # backend fleet: gateway :8888 + per-domain services
make dev-frontends   # all zones behind the :3024 proxy
make viewer          # or: the core monolith alone on :8888
make ray-up          # local Ray head; make serve-up deploys the Serve apps
make pg-up pg-migrate
```

## Clusters — kind vs k3s

Two lifecycles, same chart (`chart/`), same `:dev` image set, same release name `rask`:

- **kind** — throwaway, CI-shaped (toolchain pinned into `.localbin` by `make bootstrap`):

  ```bash
  make kind-up kind-images kind-load kind-deploy   # up
  make kind-down                                   # gone
  make e2e-ci        # governed kind stack + the live e2e suites  (== CI e2e-stack job)
  make e2e-ray-ci    # ray-ON stack + real KubeRay + both Ray suites (== CI ray-e2e job)
  ```

- **k3s** — the long-lived local/single-node deploy (GPU-capable):

  ```bash
  make k3s-install                       # one-time host bootstrap (sudo)
  make k3s-build k3s-import k3s-up       # build, side-load, helm install + wait
  make k3s-down / k3s-purge
  make tilt-up                           # in-cluster hot-reload dev loop
  ```

## CI

`.github/workflows/ci.yml` calls the Dagger module's real functions — `lint`, `typecheck`, `openapi`, `test`, `test-pg`, `charts`, `frontend`, `test-lineage` — plus the kind-backed live jobs (`make e2e-ci` / `make e2e-ray-ci`), the Playwright zone e2e, and the zone-image build+smoke. Everything hermetic runs identically on your machine: `dagger call <fn>` == the CI step.

## Common Make targets

| Target                              | What it does                                                        |
| ----------------------------------- | ------------------------------------------------------------------- |
| `make install` / `make build`       | Install both planes / build everything                              |
| `make test` / `make test-slow`      | pytest offline (`-m "not slow and not e2e"`) / + slow (still no e2e) |
| `make check` / `make ci`            | fmt + lint + typecheck + knip / + tests                             |
| `make dev-micro` / `make dev-frontends` | Local fleet / all zones on `:3024`                              |
| `make kind-up … kind-deploy`        | Throwaway kind cluster lifecycle                                    |
| `make e2e-ci` / `make e2e-ray-ci`   | The guarded live proofs (kind)                                      |
| `make k3s-build k3s-import k3s-up`  | Long-lived k3s deploy                                               |
| `make e2e`                          | Playwright browser e2e against a running deploy                     |
| `make storybook`                    | `@rask/ui` Storybook on `:6006`                                     |

See the `Makefile` (`make help`) for the complete list; `docs/` carries the depth (`docs/architecture/system-overview.md`, `layout.md`, `lance-ns-merge.md`, `deployment.md`).

[uv]: https://docs.astral.sh/uv/
[Bun]: https://bun.sh
[Zensical]: https://zensical.com
