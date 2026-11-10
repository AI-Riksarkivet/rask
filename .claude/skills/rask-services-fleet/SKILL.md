---
name: rask-services-fleet
description: The cluster route across rask's services — which service a public `/api/*` prefix reaches through the platform's Gateway (Gateway API HTTPRoutes the product chart renders: `rask.apiRoutes` and the console's `/`), the prefix rewrite, ports, and how the dev loops reach an edge. Use when an `/api/*` call returns a 404, 401, 403 or 5xx through the edge or from the console; when adding or moving an endpoint or a public prefix; when changing a port; or when wiring a service behind the Gateway.
---

# rask services fleet (the cluster route + the services behind it)

The day-to-day backend map. **rask runs no edge process.** The edge is the platform's Gateway (Kubernetes Gateway
API): HTTPRoutes attach to the Gateway `route.parentRefs` names, and each service authenticates every request itself
(ADR 0165). The product chart renders every route, and routes no client:

- `chart/templates/route.yaml`: `<release>-api` holds one rule per public `/api/*` prefix, generated from the
  `rask.apiRoutes` helper in `chart/templates/_helpers.tpl`; `<release>-dex` routes `/dex` to the development Dex
  (only with `auth.enabled` and `dex.enabled`).
- `chart/templates/console.yaml` (`console.enabled`, ADR 0193): `<release>-console` routes `/` to the console's web
  pod, the lakehouse's own UI. The `/api/*` rows and `/dex` are longer prefixes and keep winning.

For FastAPI app/router/lifespan idioms see `fastapi`. This skill is *only* the topology and its invariants.

## When to use

- Adding or moving an endpoint: pick the owning service and confirm a route row reaches it.
- Debugging a 404, 401, 403 or 5xx seen from the console or through the edge.
- Changing a port, or wiring a new service behind the Gateway.

## The route table

| Public prefix | Service | Port | Rewritten to (`URLRewrite` `ReplacePrefixMatch`) | Rendered by |
|---|---|---|---|---|
| `/api/catalog` | `<release>-catalog` | 2333 | unchanged; the catalog strips the prefix from the bytes as sent (`LANCE_PUBLIC_PATH_PREFIX`), since a rewrite would turn `%2B` and `%2F` into `+` and `/` ([[LH-526]]) | product chart (`rask.catalogPublicPrefix`) |
| `/api/lineage` | `<release>-lineage` | 8000 | `/` | product chart |
| `/dex` | `<release>-dex` | 5556 | unchanged | product chart, dev Dex only |
| `/` | `<release>-console` (the web pod) | 3000 (`console.port`) | unchanged | product chart (`console.enabled`) |

Only Core `PathPrefix` matches and the `URLRewrite` filter are used, so any conformant implementation serves the
routes: kgateway (the intended production edge), Envoy Gateway, Istio, Cilium. On k3s it is the bundled Traefik
with its Gateway provider switched on by `deploy/k3s/traefik-gateway.yaml` (a HelmChartConfig that
`scripts/k3s-install.sh` drops into k3s's manifests dir): GatewayClass `traefik`, Gateway `traefik-gateway` in
`kube-system`, listener `web` on the node's port 80. `deploy/values-local.yaml` attaches to it (`route.parentRefs`) and
sets `forwardedAllowIps: 10.42.0.0/16`.

**Adding a lakehouse prefix** is one row in `rask.apiRoutes` (`prefix`, `component`, `service`, `port`, `rewrite`;
an empty `rewrite` forwards the path as is) plus its case in the offline render gate
`tests/unit/test_the_cluster_route_names_one_backend_per_api_prefix.py`. The same row also renders that
component's `<release>-edge-<component>` NetworkPolicy, so the route and the admission cannot drift apart. The live
proof that every row reaches its service is `tests/e2e-py/test_the_cluster_route_reaches_each_service.py` (marker
`edge`, `make e2e-edge RASK_E2E_EDGE_URL=…`). A client publishes its own prefixes from its own release; the product
chart names no client.

## The services

| Service | Port | Public prefix | What it is |
|---|---|---|---|
| **catalog** | 2333 | `/api/catalog` | the Lance Namespace REST catalog and the management doors (`rask-lance-catalog`) |
| **lineage** | 8000 | `/api/lineage` | the OpenLineage intake, the commit feed (`GET /events`) and the forwarder (ADR 0158) |
| **maintenance** | 8000 | none | the work-queue worker (each table's pass, the hourly audit, the daily scan, ADR 0198) and the write-event lane; it pulls its tasks from NATS and is reached only by its Dapr subscription (`/maintenance-arrival`) and the probes |
| **console** | 3000 | `/` | the console's static-file server (`serve.ts`), the lakehouse's own UI; calls no door itself (`rask-frontend`) |

The lakehouse rows are `/api/catalog` and `/api/lineage`. There is no ingest row and no medallion row: the lakehouse
runs no ingest service, no stage and no training (ADR 0154, ADR 0155), and data enters through the catalog's
namespace doors on `/api/catalog`. A user's pipeline that moves data between tiers writes through those doors on its
own compute.

## Dev loops and the edge

There is no local router. The console's `/api` needs an edge on `RASK_EDGE_URL` (default
`http://localhost:8080`), and the dev server signs in at the Dex that edge serves under `/dex`
(`frontend/console/dev/config.json`):

- **`make dev-k3s`** uses :8080 when it already serves the issuer; otherwise it port-forwards k3s Traefik's Service
  (`kube-system/traefik`, the Gateway that serves the release's HTTPRoutes) to :8080, waits until `/dex` and
  `/api/lineage/livez` answer, and runs the console with Vite HMR against it. Needs `make k3s-up`.
- **`make dev`** runs the console against whatever edge `RASK_EDGE_URL` names.

The console has one Vite `/api` proxy (`frontend/console/vite.config.ts`), to `RASK_EDGE_URL`; it never addresses a
service directly. See `rask-frontend`.

## Load-bearing invariants

1. **No service owns relational state.** The only database server is the chart-managed `rask-age` Postgres, serving
   OpenFGA. Lineage keeps no database: its commit feed,
   edge list, outbox refusals and forwarder cursors are Lance tables in the catalog's provenance namespace, written and
   read through the catalog's doors (ADR 0158), and the lineage graph is bring-your-own, fed by its forwarder.
2. **Each service builds only its own `app.state` subset in its own lifespan.** Don't widen a lifespan to grab
   resources the service doesn't use.
3. **Longest prefix wins, and there is NO `/api` catch-all.** Gateway API resolves overlapping `PathPrefix` matches
   longest-first and matches path elements, so `/api/lineage` beats the console's `/`, and `/api/catalog` never matches
   `/api/catalogx`. An `/api/*` path no row names falls to the console's `/` rule, and the web pod's `serve.ts`
   answers `/api` and `/api/*` 404 `{"detail": "no route serves <path>"}` rather than the SPA, so a wrong prefix is
   visible. A new public prefix needs its own row.
4. **A 5xx with no rask body is the Gateway's.** A backend Service with no ready endpoint, or a port the Service does
   not expose, is answered by the Gateway implementation (Traefik, kgateway, …) with its own status and body, never a
   problem+json. Use that to tell "backend down" from "the service refused": every rask service answers with its own
   body.
5. **Every 401 and 403 comes from the service.** The route authenticates and strips nothing: `Authorization` reaches
   the service as the console attached it, and the service verifies it (`GovernedAuthSettings`, the FGA checker). A
   person's token passes only when its `aud` names the lakehouse's own audience, `auth.oidc.audience`
   (`rask-lakehouse`, never a client's own sign-in id): the console asks Dex for it with
   `audience:server:client_id:<audience>`, which Dex grants only to a client the audience client lists in
   `trustedPeers` ([[LH-515]]), so a token for the sign-in client alone is a 401. A token without `federated:id` is a
   401 too (`lakehouse.governed.idp_identity`). A request through the Gateway reaches the app port directly and never
   carries a Dapr sidecar token, so `require_dapr_token` alone refuses it on a sidecar-only door; any verified
   service-account token authenticates at any door (ADR 0156 C5), through the edge included. The FGA denials read
   `<subject> lacks <relation> on <object>`. A client asks a lakehouse verb through the catalog's decision door, which
   answers only `can_*` verbs and `member`, and only about objects inside the scope its own identity was granted at the
   access doors (`decider` on the estate root, a project or a warehouse, [[LH-563]]): anything else is answered refused.
6. **Forwarded headers are trusted from the Gateway's data plane only.** Every routed pod runs uvicorn with
   `--proxy-headers --forwarded-allow-ips=<forwardedAllowIps>` (the Gateway data plane's pod CIDR; `*` is refused), so
   the client address behind the edge is the real one in every service's logs and audit records.
7. **NetworkPolicy follows the route.** Each routed lakehouse service admits ingress on its app port only, from any peer
   (`<release>-edge-<component>`, one per `rask.apiRoutes` row): the Gateway's data plane lives in the platform's
   namespace, and the service authenticates each request itself, while daprd's ports on the same pod stay
   intra-namespace. The console's pod is admitted the same way on its one port (`<release>-edge-console`), and every
   egress allow leaves it out: its static server dials nothing.
8. **No merged `/docs`.** Each service serves its own `/docs`, `/redoc` and `/openapi.json`, and only under
   `docs.enabled` (off by default). Reached through the edge, a service's docs sit under its public prefix after the
   row's rewrite.
9. **The Gateway's limits are its policy, not the route's.** The platform's Gateway must allow request bodies of 64 MB
   (Arrow imports). A route cannot set it.

## Gotchas

- **A route with no hostname claims every host its Gateway's listener accepts.** Two releases attached host-less to one
  listener are rival claims on `/` and `/api`, and the oldest route wins the tie. A real deployment
  (`rask.isRealDeployment`) must name `route.hostnames`, and the render refuses an empty list ([[XC-146]]); a second
  development install sets its own `route.hostnames` or `route.enabled=false`. The console's route takes the same
  `route.hostnames`, so the page and the APIs it calls share one host.
