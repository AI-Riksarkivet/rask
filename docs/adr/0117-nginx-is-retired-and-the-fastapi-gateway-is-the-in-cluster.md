# 0117. nginx is retired, and the FastAPI gateway is the in-cluster edge (merge decision 4, R14, 2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:257` (at `44b354f3`) (naming rule 4, "Gateway", 2026-07-24) and `:449` (owner ruling R14, 2026-07-27).

**Citation note.** Chart and script comments that cite "lance-ns-merge.md decision 4" (often as "decision
4/P4") mean naming rule 4, "Gateway: rask's FastAPI gateway wins; lance-ns's nginx gateway retires (P1/P4)",
which R14 calls "R-decision 4". The merge plan's list of five proposed decisions also has a number 4, "extend
rask's tests/e2e"; that is a different decision, recorded in
[0126](0126-the-merge-s-other-four-decisions-dex-stays-zone-names-stay.md).

## Context

lance-ns fronted its services with an nginx gateway rendered from helm templates (including a
`lance.lineageSidecarOnlyRoutes` 403 blocklist); rask had a Dapr-aware FastAPI gateway on `:8888`. Two edges
would mean two routing tables.

## Decision

- **Naming rule 4.** rask's FastAPI gateway (`:8888`, Dapr-aware) wins. lance-ns's nginx gateway retires: its
  routes become rows in the gateway (P1) and its `gateway.yaml` template is deleted (P4). The nginx 403
  blocklist ports as gateway middleware.
- **R14 — nginx is gone everywhere**, including the remnants (`frontend.nginx.conf`, dockerfile and helper
  comments). Zones serve through their Bun SSR servers; the FastAPI gateway is the in-cluster edge; adopting
  kgateway is its own future project (Ingress template, zone Service exposure, gateway Deployment are the
  touchpoints).
- `ingress.className: nginx` in `values-prod` names the CLUSTER's Ingress controller class, operator-set per
  cluster, and is unrelated to the retired gateway.

## Consequences

- The gateway path-routes `/api/*` longest-prefix-first with no `/api` catch-all
  (`services/gateway/src/gateway/__init__.py:219-224`).
- The ported blocklist is the gateway's `lineage_sidecar_only_routes` setting
  (`services/gateway/src/gateway/config.py:70-96`), rendered from `lance.lineageSidecarOnlyRoutes`.
- `scripts/prod_render_check.sh:131-137` fails the render if an nginx location block reappears.
- `chart/templates/ingress.yaml:22-24` still carries ingress-nginx annotations as keyed defaults for the
  cluster's controller; that is the controller, not a rask gateway.
