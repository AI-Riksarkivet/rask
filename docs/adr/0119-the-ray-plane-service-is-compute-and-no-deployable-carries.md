# 0119. The Ray-plane service is `compute`, and no deployable carries `-api` (R20, R22, 2026-07-28)

Source: `docs/architecture/lance-ns-merge.md:455,457` (at `44b354f3`) (owner rulings R20, 2026-07-27, executed 2026-07-28, and R22, 2026-07-28, which supersedes R20's name choice).

## Context

rask's services carried an `-api` suffix (`core-api`, `search-api`, `volumes-api`, `ray-api`). Renaming
`search-api` to `search` collided with the media plane's `search` service until P7 retired the old one, and
`ray-api` could not take the bare name `ray` as a Python package without shadowing the PyPI `ray` that
`ray-kit` and the runners import.

## Decision

- **R20.** The `-api` suffix is removed, executed WITH P7, because P7 is what makes the renames collision-free:
  `search-api`/`volumes-api` die into the media plane, `core-api`/the orchestrator dissolve, and the Ray
  cluster image renames `ray` → `ray-cluster` (`.docker/ray-cluster.dockerfile`) to free the bare name.
- **R22 (supersedes R20's `ray`).** The Ray-plane service is named `compute`, aligning service, zone and
  plane, and removing R20's PyPI-shadow exception: `import compute` is safe where `import ray` was not.

## Consequences

- `compute` is the name on every surface (uv member, import, k8s Service, Dapr app-id, image, gateway); the
  public paths stay `/api/ray` and `/api/serve`, because the URL namespace names the Ray cluster, not the
  service (`CLAUDE.md`, `services/compute`).
- The Ray cluster image is `.docker/ray-cluster.dockerfile`; the compute service image is
  `.docker/compute.dockerfile`.
