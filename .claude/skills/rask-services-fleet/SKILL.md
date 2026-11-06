---
name: rask-services-fleet
description: The cluster route across rask's services — which service a public `/api/*` prefix reaches through the platform's Gateway (Gateway API HTTPRoutes: the product chart's from `rask.apiRoutes`, the console release's own), the prefix rewrite, ports, and how the dev loops reach an edge. Use when an `/api/*` call returns a 404, 401, 403 or 5xx through the edge or from the console; when adding or moving an endpoint or a public prefix; when changing a port or `RASK_API_PREFIX`; when wiring a service behind the Gateway; or when reading `scripts/dev-micro.sh`.
---

# rask services fleet (the cluster route + the services behind it)

The day-to-day backend map. **rask runs no edge process.** The edge is the platform's Gateway (Kubernetes Gateway
API): HTTPRoutes attach to the Gateway `route.parentRefs` names, and each service authenticates every request itself
(ADR 0165). Two releases render routes:

- **The product chart** (`chart/templates/route.yaml`): `<release>-api` holds one rule per public `/api/*` prefix,
  generated from the `rask.apiRoutes` helper in `chart/templates/_helpers.tpl`; `<release>-dex` routes `/dex` to the
  development Dex (only with `auth.enabled` and `dex.enabled`). It routes no client.
- **The console's release** (`deploy/fixtures/console/chart/templates/route.yaml`): one HTTPRoute, `<rask>-console`, on
  the same parentRefs, routing `/api/notifications` to the inbox and `/` to the console's web pod. The lakehouse's
  `/api/*` rows and `/dex` are longer prefixes and keep winning.

For FastAPI app/router/lifespan idioms see `fastapi`. This skill is *only* the topology and its invariants.

## When to use

- Adding or moving an endpoint: pick the owning service and confirm a route row reaches it.
- Debugging a 404, 401, 403 or 5xx seen from the console or through the edge.
- Changing a port or `RASK_API_PREFIX`, or wiring a new service behind the Gateway.
- Reading or editing `scripts/dev-micro.sh`.

## The route table

| Public prefix | Service | Port | Rewritten to (`URLRewrite` `ReplacePrefixMatch`) | Rendered by |
|---|---|---|---|---|
| `/api/catalog` | `<release>-catalog` | 2333 | unchanged; the catalog strips the prefix from the bytes as sent (`LANCE_PUBLIC_PATH_PREFIX`), since a rewrite would turn `%2B` and `%2F` into `+` and `/` ([[LH-526]]) | product chart (`rask.catalogPublicPrefix`) |
| `/api/lineage` | `<release>-lineage` | 8000 | `/` | product chart |
| `/dex` | `<release>-dex` | 5556 | unchanged | product chart, dev Dex only |
| `/api/notifications` | `<rask>-notifications` (the inbox) | 8850 | unchanged; the inbox mounts its routes under `/api/notifications` itself (`RASK_API_PREFIX=/api`) | console release (`inbox.publicPrefix`) |
| `/` | `<rask>-console` (the web pod) | 3000 | unchanged | console release |

Only Core `PathPrefix` matches and the `URLRewrite` filter are used, so any conformant implementation serves the
routes: kgateway (the intended production edge), Envoy Gateway, Istio, Cilium. On k3s it is the bundled Traefik
with its Gateway provider switched on by `deploy/k3s/traefik-gateway.yaml` (a HelmChartConfig that
`scripts/k3s-install.sh` drops into k3s's manifests dir): GatewayClass `traefik`, Gateway `traefik-gateway` in
`kube-system`, listener `web` on the node's port 80. `deploy/values-local.yaml` and the console's
`deploy/fixtures/console/values.yaml` both attach to it (`route.parentRefs`) and set `forwardedAllowIps: 10.42.0.0/16`.

**Adding a lakehouse prefix** is one row in `rask.apiRoutes` (`prefix`, `component`, `service`, `port`, `rewrite`;
an empty `rewrite` forwards the path as is) plus its case in the offline render gate
`tests/unit/test_the_cluster_route_names_one_backend_per_api_prefix.py`. The same row also renders that
component's `<release>-edge-<component>` NetworkPolicy, so the route and the admission cannot drift apart. The live
proof that every row reaches its service is `tests/e2e-py/test_the_cluster_route_reaches_each_service.py` (marker
`edge`, `make e2e-edge RASK_E2E_EDGE_URL=…`). A client publishes its own prefixes from its own release, as the console
does; the product chart names no client.

## The services

| Service | Port | Public prefix | What it is |
|---|---|---|---|
| **catalog** | 2333 | `/api/catalog` | the Lance Namespace REST catalog and the management doors (`rask-lance-catalog`) |
| **lineage** | 8000 | `/api/lineage` | the OpenLineage intake, the commit feed (`GET /events`) and the forwarder (ADR 0158) |
| **maintenance** | 8000 | none | the sweep, the reconciler and the due list; reached in-cluster and by its Dapr crons only |
| **notifications** | 8850 | `/api/notifications` (console release) | the inbox behind the bell, a client of the lakehouse's events (below) |

The lakehouse rows are `/api/catalog` and `/api/lineage`. There is no ingest row and no medallion row: the lakehouse
runs no ingest service, no stage and no training (ADR 0154, ADR 0155), and data enters through the catalog's
namespace doors on `/api/catalog`. A user's pipeline that moves data between tiers writes through those doors on its
own compute.

**The inbox** (`services/notifications`, deployed by `deploy/fixtures/console`) serves
`/api/notifications/inbox{,/unread,/seen,/dismiss}`, `/watches`, `/watches/{project_id}` and `/prefs`: one Dapr
**InboxActor** per subject holding claim-check pointers, fed by **two** ingresses: a `lineage.events.v1` subscription
on its own pubsub component, and — because a user's pipeline and every external OpenLineage producer emit over HTTP
only and never reach the topic — a `bindings.cron` tick (`<rask>-notifications-reconcile-cron`) that follows lineage's durable
run feed (`GET /events/projection?since=`) forward from a persisted cursor. **Lineage prunes that feed on its relay
tick** (`lineage-relay-cron`, past `services.lineage.eventsRetentionDays`) and cannot consult this cursor, so the feed
reports `oldest_seq` and the reconciler compares it against its own mark: a mark below that floor is
`ReconcileResult.gapped`, an ERROR (`lineage_feed_pruned_below_cursor`) and the `notifications.feed.gaps` counter. That
reconciler has no retry of its own: its reads of lineage go through service invocation, and the console release's
invocation Resiliency (`<rask>-notifications-invocation-resiliency`, constant 2 s × 3 on `408,429,500-599`, target app
`lineage`, scoped to the inbox) is that call's one retry layer; a pass that still fails is walked again by the next tick
from the same mark. That cron route is **root-mounted**, not under the api prefix: Dapr delivers an input binding to `POST /<component name>` at
the pod root, so the Component name, `RASK_NOTIFICATIONS_BINDING_NAME` and the served path are one string.

What the lakehouse admits for it lives in the console's `deploy/fixtures/console/lakehouse-values.yaml`, layered onto
the lakehouse release by `make k3s-up`: its ServiceAccount in `auth.externalDeciders` (`decider`, `member_expander`,
`event_reader`), its bus user in `nats.auth.users`, and its store reads in `openbao.externalReaders`. The reconciler
presents its pod's projected `rask-lineage` token ([[LH-220]]); the projection is gated on `can_read_event_feed` at the
estate root ([[CTL-021]]), so an account without `event_reader` 403s every tick. Without an actor state store scoped to
its app-id (the console release renders one on a CloudNativePG Cluster by default, or names the platform's Postgres
through `state.external.connectionSecret`) actor hosting is disabled: a healthy pod with a
permanently empty bell.

## Dev loops and the edge

There is no local router. `make dev-micro` (`scripts/dev-micro.sh`) runs the inbox alone on :8850 with no sidecar and
no edge: `require_actor_plane` then answers every inbox route `503` with the reason, and the bus subscription is
declared but nothing delivers to it. The console's `/api` needs an edge on `RASK_EDGE_URL` (default
`http://localhost:8080`), and the dev server signs in at the Dex that edge serves under `/dex`
(`frontend/console/dev/config.json`):

- **`make dev-k3s`** uses :8080 when it already serves the issuer; otherwise it port-forwards k3s Traefik's Service
  (`kube-system/traefik`, the Gateway that serves both releases' HTTPRoutes) to :8080, waits until `/dex` and
  `/api/lineage/livez` answer, and runs the console with Vite HMR against it. Needs `make k3s-up` and `make k3s-clients`.
- **`make dev`** runs the console against whatever edge `RASK_EDGE_URL` names.

The console has one Vite `/api` proxy (`frontend/console/vite.config.ts`), to `RASK_EDGE_URL`; it never addresses a
service directly. See `rask-frontend`.

## Load-bearing invariants

1. **No service owns relational state.** The only database servers are the chart-managed `rask-age` Postgres, serving
   OpenFGA, and the inbox's own actor-state Postgres in the console release. Lineage keeps no database: its commit feed,
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
   401 too (`service_kit.governed.idp_identity`). A request through the Gateway reaches the app port directly and never
   carries a Dapr sidecar token, so `require_dapr_token` alone refuses it on a sidecar-only door; any verified
   service-account token authenticates at any door (ADR 0156 C5), through the edge included. The FGA denials read
   `<subject> lacks <relation> on <object>`. A client asks a lakehouse verb through the catalog's decision door, which
   answers only `can_*` verbs and `member`, and only for a ServiceAccount `auth.externalDeciders` names.
6. **Forwarded headers are trusted from the Gateway's data plane only.** Every routed pod runs uvicorn with
   `--proxy-headers --forwarded-allow-ips=<forwardedAllowIps>` (the Gateway data plane's pod CIDR; `*` is refused), so
   the client address behind the edge is the real one and anonymous rate-limit keys stay per client.
7. **NetworkPolicy follows the route.** Each routed lakehouse service admits ingress on its app port only, from any peer
   (`<release>-edge-<component>`, one per `rask.apiRoutes` row): the Gateway's data plane lives in the platform's
   namespace, and the service authenticates each request itself, while daprd's ports on the same pod stay
   intra-namespace. Under `networkPolicy.enabled` the console release renders the same shape for its two pods
   (`<rask>-console-edge`, `<rask>-notifications-edge`).
8. **No merged `/docs`.** Each service serves its own `/docs`, `/redoc` and `/openapi.json`, and only under
   `docs.enabled` (off by default). Reached through the edge, a service's docs sit under its public prefix after the
   row's rewrite.
9. **The Gateway's limits are its policy, not the route's.** The platform's Gateway must allow request bodies of 64 MB
   (Arrow imports). A route cannot set it.

## Gotchas

- **`RASK_API_PREFIX`'s code default is `/api/v1`, and only the inbox reads it.** `scripts/dev-micro.sh` and the
  console release's inbox Deployment set `/api`; leave it unset and `/api/notifications` silently moves to
  `/api/v1/notifications`, off the path the console calls. The product chart sets none, since no service it renders
  mounts under it.
- **A route with no hostname claims every host its Gateway's listener accepts.** Two releases attached host-less to one
  listener are rival claims on `/` and `/api`, and the oldest route wins the tie. A real deployment
  (`rask.isRealDeployment`) must name `route.hostnames`, and the render refuses an empty list ([[XC-146]]); a second
  development install sets its own `route.hostnames` or `route.enabled=false`. The console release takes
  `route.hostnames` too, and must name the same host as the lakehouse's routes or the browser's same-origin calls miss.
- **`scripts/dev-micro.sh` deliberately does NOT bash-source `.env`.** The inbox loads it via `python-dotenv` so
  JSON-list settings like `RASK_CORS_ORIGINS=["..."]` parse correctly; bash sourcing strips the quotes. Export only
  vars *not* in `.env`.
