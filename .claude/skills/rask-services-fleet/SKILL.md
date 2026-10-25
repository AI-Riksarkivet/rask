---
name: rask-services-fleet
description: The cluster route across the rask backend fleet — which service a public `/api/*` prefix reaches through the platform's Gateway (Gateway API HTTPRoutes rendered from `rask.apiRoutes`), the prefix rewrite, ports, and how the dev loops reach an edge. Use when an `/api/*` call returns a 404, 401, 403 or 5xx through the edge; when adding or moving an endpoint or a public prefix; when changing a port or `RASK_API_PREFIX`; when wiring a service into the fleet; or when reading `scripts/dev-micro.sh`.
---

# rask services fleet (the cluster route + per-domain backends)

The day-to-day backend map. **rask runs no edge process.** The edge is the platform's Gateway (Kubernetes Gateway
API): `chart/templates/route.yaml` renders two HTTPRoutes on the Gateway `route.parentRefs` names, and each service
authenticates every request itself (ADR 0165). `<release>-api` holds one rule per public `/api/*` prefix, generated
from the `rask.apiRoutes` helper in `chart/templates/_helpers.tpl`; `<release>-web` routes the zones (`/<zone>`),
`/dex`, and `/` to `web-home`. `scripts/dev-micro.sh` is the source of truth for the local process list and ports.

For FastAPI app/router/lifespan idioms see `fastapi`. This skill is *only* the topology and its invariants.

## When to use

- Adding or moving an endpoint: pick the owning service and confirm a route row reaches it.
- Debugging a 404, 401, 403 or 5xx seen through the SPA or the edge.
- Changing a port or `RASK_API_PREFIX`, or wiring a new service into the fleet.
- Reading or editing `scripts/dev-micro.sh`.

## The route table

| Public prefix | Service (`<release>-…`) | Port | Rewritten to (`URLRewrite` `ReplacePrefixMatch`) |
|---|---|---|---|
| `/api/explorer/annotations` | `annotator` | 8103 | `/api/annotations`; from the annotator fixture chart's own HTTPRoute |
| `/api/catalog` | `catalog` | 2333 | unchanged; the catalog strips the prefix from the bytes as sent (`LANCE_PUBLIC_PATH_PREFIX`), since a rewrite would turn `%2B` and `%2F` into `+` and `/` ([[LH-526]]) |
| `/api/lineage` | `lineage` | 8000 (dev: 8001) | `/` |
| `{RASK_API_PREFIX}/notifications` | `notifications` | 8850 | unchanged |
| `/api/ray`, `/api/serve` | `compute` | 8804 | unchanged; from the compute fixture chart's own HTTPRoute |

Viewer and search are clients the product chart renders no row for; notifications' row renders only with
`services.notifications.frontDoor` or `singleTenant.enabled`. Compute's and the annotator's rows are not rask's: the compute
fixture (`deploy/fixtures/compute/chart/templates/route.yaml`) renders its own HTTPRoute on the same parentRefs plus a
NetworkPolicy (`<release>-compute-edge`) admitting its port, so a bring-your-own engine companion publishes itself, and
the annotator fixture (`deploy/fixtures/annotator/chart/templates/route.yaml`, ADR 0171) does the same for
`/api/explorer/annotations` (HTTPRoute `rask-annotator`, NetworkPolicy `rask-annotator-edge` under
`networkPolicy.enabled`).

Only Core `PathPrefix` matches and the `URLRewrite` filter are used, so any conformant implementation serves the
routes: kgateway (the intended production edge), Envoy Gateway, Istio, Cilium. On k3s it is the bundled Traefik
with its Gateway provider switched on by `deploy/k3s/traefik-gateway.yaml` (a HelmChartConfig that
`scripts/k3s-install.sh` drops into k3s's manifests dir): GatewayClass `traefik`, Gateway `traefik-gateway` in
`kube-system`, listener `web` on the node's port 80. `deploy/values-local.yaml` attaches to it, sets
`route.edgeUrl: http://traefik.kube-system.svc.cluster.local` and `forwardedAllowIps: 10.42.0.0/16`.

**Adding a public prefix** is one row in `rask.apiRoutes` (`prefix`, `component`, `service`, `port`, `rewrite`;
an empty `rewrite` forwards the path as is) plus its case in the offline render gate
`tests/unit/test_the_cluster_route_names_one_backend_per_api_prefix.py`. The same row also renders that
component's `<release>-edge-<component>` NetworkPolicy, so the route and the admission cannot drift apart. The live
proof that every row reaches its service is `tests/e2e-py/test_the_cluster_route_reaches_each_service.py` (marker
`edge`, `make e2e-edge RASK_E2E_EDGE_URL=…`).

## Fixed port map

| Service | Port | Public prefix | Lifespan builds |
|---|---|---|---|
| **compute** | 8804 | `/api/ray`, `/api/serve` | dashboard httpx client + Ray Job SDK client. Ships in its own fixture chart (`deploy/fixtures/compute`), not the product chart; `dev-micro.sh` starts it on :8804 |
| **annotator** | 8103 | `/api/explorer/annotations` → `/api/annotations` | annotations plane and its Dapr actor plane (projects, tasks, roles) on its own state store. A client of the lakehouse with its own release (`deploy/fixtures/annotator`, ADR 0171), not the product chart; the annotator zone reaches its Service at `frontend.annotatorUrl` |
| **notifications** | 8850 | `/api/notifications` (paths unchanged; rendered with `services.notifications.frontDoor` or `singleTenant.enabled`) | the per-subject inbox behind the bell (`/api/notifications/inbox{,/unread,/seen,/dismiss}`): one Dapr **InboxActor** per subject holding claim-check pointers, fed by **two** ingresses: a `lineage.events.v1` subscription on its own pubsub component, and — because a user's pipeline and every external OpenLineage producer emit over HTTP only and never reach the topic — a `bindings.cron` tick (`notifications-reconcile-cron`, `@every 30s`) that follows lineage's durable run feed (`GET /events/projection?since=`) forward from a persisted cursor, the last row it handled. **Lineage prunes that feed on its relay tick** (`lineage-relay-cron`, past `services.lineage.eventsRetentionDays`), and the prune cannot consult this cursor — it lives in notifications' Dapr state store, which lineage is not scoped to — so the feed reports `oldest_seq` (the floor it still retains) and the reconciler compares it against its own mark. A mark below that floor means rows may have been deleted before this lane read them: `ReconcileResult.gapped`, an ERROR (`lineage_feed_pruned_below_cursor`) and the `notifications.feed.gaps` counter, reported once as the mark moves past it. A feed that answers the cursor with a `next_cursor` BELOW it (a mark taken on another feed) is re-read from its oldest row. Distinct from `behind`, which is a pass using its page budget before catching up: not a loss, the next tick continues from the mark. That cron route is **root-mounted**, not under the api prefix: Dapr delivers an input binding to `POST /<component name>` at the pod root, so the Component name, `RASK_NOTIFICATIONS_BINDING_NAME` and the served path are one string (all three rendered from `services.notifications.reconcileBindingName`, and pinned together by `tests/unit/test_invariants.py`). `dev-micro.sh` DOES start it, but **with no sidecar** — `require_actor_plane` then answers every inbox route `503` with the reason rather than 500ing, so the badge is honestly empty, not broken. In-cluster it needs four separate values entries or it is silently useless: `notifications` in `stateStore.scopes` (no actor state store → actor hosting disabled → a healthy pod with a permanently empty bell), `daprIngest: true` (no `APP_API_TOKEN` → the bus handler's `assert_app_token_configured` **crash-loops the pod**), its row in `dapr-resiliency.yaml` (no sidecar retry, no dead-lettering), and the subject its pod's ServiceAccount is (`kubernetes~system:serviceaccount:<ns>:<release>-sa-notifications`, `lance.saSubject`, ADR 0156 C5), which the bootstrap hook grants `event_reader` on the estate root. The reconciler presents its pod's projected `rask-lineage` token (`identityAudiences`) and names nobody ([[LH-220]]); lineage admits any token the cluster's issuer signs and OpenFGA alone authorizes it, so a missing grant 403s every feed tick. Admission is only half: the projection is gated on `can_read_event_feed` at the estate root, which the bootstrap hook grants that subject as `event_reader` ([[CTL-021]]), so a subject that is mapped but not granted 403s every tick |

The lakehouse rows are `/api/catalog` and `/api/lineage`. There is no ingest row and no medallion row: the
lakehouse runs no ingest service, no stage and no training (ADR 0154, ADR 0155), and data enters through the
catalog's namespace doors on `/api/catalog`. A user's pipeline that moves data between tiers writes through those
doors on its own compute.

## Dev loops and the edge

There is no local router. `make dev-micro` (`scripts/dev-micro.sh`) runs the fleet only: compute, flows,
notifications and the explorer trio, each on its own port, with no edge in front. The zones' `/api` needs an edge
on `RASK_EDGE_URL` (default `http://localhost:8888`):

- **`make dev-frontends-k3s`** port-forwards k3s Traefik's Service (`kube-system/traefik`, the Gateway that serves
  the chart's HTTPRoutes) to :8888 and runs every zone with Vite HMR against it (`VIEWER_BACKEND` and
  `RASK_EDGE_URL` both :8888). Needs `make k3s-up`.
- **`make dev-zone ZONE=<z>`** runs one zone against its own mock upstreams, with no cluster and no fleet.

The Vite dev `/api` proxies are per zone: `compute`/`studio`/`models` → `VIEWER_BACKEND` (:8888);
`home`/`lakehouse` → `LANCE_BACKEND` (:8001, lineage's dev port, which `dev-micro.sh` does not start);
`explorer`/`annotator` have no `/api` proxy and reach :8101/:8102/:8103 through their own BFF. The zones' SSR
reads one `RASK_EDGE_URL`. See `rask-frontend`.

## Load-bearing invariants

1. **No fleet service owns relational state.** The only database server is the chart-managed `rask-age` Postgres, serving OpenFGA and the Dapr state store. Lineage keeps no database: its commit feed, edge list, outbox refusals and forwarder cursors are Lance tables in the catalog's provenance namespace, written and read through the catalog's doors (ADR 0158), and the lineage graph is bring-your-own, fed by its forwarder. Never add a DB engine to a fleet lifespan.
2. **Each service builds only its own `app.state` subset in its own lifespan.** The compute service opens only the dashboard/job clients. Don't widen a lifespan to grab resources the service doesn't use.
3. **Longest prefix wins, and there is NO catch-all.** Gateway API resolves overlapping `PathPrefix` matches longest-first and matches path elements, so `/api/explorer/search` beats `/api/explorer`, and `/api/flows` never matches `/api/flowsx`. There is no bare `/api` row: an unmatched `/api/*` falls through to the `<release>-web` route's `/` rule and reaches `web-home`, which answers it 404 (a host with no `-web` route gets the Gateway's own 404). A new public prefix needs its own row in `rask.apiRoutes`.
4. **`/api/serve` and `/api/ray` both go to the compute service** (the URL namespace names the Ray cluster, not the service, R22). It ships in its own fixture chart beside a bring-your-own Ray engine (ADR 0155), with app-id `compute` and Service `rask-compute`, and publishes its rows with its own HTTPRoute. On the service itself, domain routers mount under `RASK_API_PREFIX` while its `proxy_router` mounts at the **root** so `/api/serve/*` reaches the Ray Serve status API; routers vs proxy_router is the `make_service_app` distinction.
5. **A 5xx with no rask body is the Gateway's.** A backend Service with no ready endpoint, or a port the Service does not expose, is answered by the Gateway implementation (Traefik, kgateway, …) with its own status and body, never a problem+json. Use that to tell "backend down" from "the service refused": every rask service answers with its own body.
6. **Every 401 and 403 comes from the service.** The route authenticates and strips nothing: `Authorization` reaches the service as the BFF or client attached it, and the service verifies it (`GovernedAuthSettings`, the FGA checker). A person's token passes only when its `aud` names the lakehouse's own audience, `auth.oidc.audience` (`rask-lakehouse`, never the zones' sign-in client `auth.oidc.clientId`): the zones and the live lanes ask Dex for it with `audience:server:client_id:<audience>`, which Dex grants only to a client the audience client lists in `trustedPeers` ([[LH-515]]), so a token for the sign-in client alone is a 401. A request through the Gateway reaches the app port directly and never carries a Dapr sidecar token, so `require_dapr_token` alone refuses it on a sidecar-only door; any verified service-account token authenticates at any door (ADR 0156 C5), through the edge included. The FGA denials read `<subject> lacks <relation> on <object>`: the viewer gates `/api/datasets` + `/api/pages` on `can_get_metadata`, `/api/page` (image bytes) on `can_read_data`, and `/api/object{,s,/download}` and `/api/stores` on the lakehouse's estate-admin verb `can_observe_events` against `RASK_FGA_ROOT_OBJECT` (`viewer/api/security.py`), as compute and flows gate theirs: a client asks a lakehouse verb through the catalog's decision door, which answers only `can_*` verbs and `member`; the annotator gates its task plane on the project roles it keeps in its own actor state.
7. **Forwarded headers are trusted from the Gateway's data plane only.** Every routed pod runs uvicorn with `--proxy-headers --forwarded-allow-ips=<forwardedAllowIps>` (the Gateway data plane's pod CIDR; `*` is refused), so the client address behind the edge is the real one and anonymous rate-limit keys stay per client.
8. **NetworkPolicy follows the route.** Each routed service admits ingress on its app port only, from any peer (`<release>-edge-<component>`, one per `rask.apiRoutes` row): the Gateway's data plane lives in the platform's namespace, and the service authenticates each request itself, while daprd's ports on the same pod stay intra-namespace; the zones get egress to the Gateway's data plane through `route.edgeEgress`, which is required under `networkPolicy.enabled` because the data plane's pods are the platform's to name.
9. **The storage browser's chain is BFF-shaped, and its backend is a client:** lakehouse zone `/lakehouse/api/explorer/*` (SvelteKit route) → the edge `/api/explorer/*` → viewer `/api/*`. The product chart renders no viewer and no `/api/explorer` row, so in-cluster the browser needs the media plane's own release with its own route; dev needs the viewer running (`dev-micro.sh` starts it). With `auth.enabled` the viewer answers only a bearer whose subject holds `can_observe_events` on `RASK_FGA_ROOT_OBJECT`, through the catalog's decision door.
10. **The annotator mounts two planes, and the route publishes ONE ROW of one of them.** Edge-reachability is the route table's call, never the path's shape: the only annotator row is `/api/explorer/annotations` → `/api/annotations`, so `/api/assist` and `/api/jobs` carry the `/api` prefix and are **in-cluster only** (`jobs.py` states this). `/projects` and `/tasks` are the actor plane, with no row, deliberately, because the annotator zone's SSR calls them directly in-cluster on `ANNOTATOR_PROJECTS_API` (`frontend/microfrontends/annotator/src/lib/server/doors.ts`). `require_actor_plane` is attached to the `/tasks` router alone (`tasks.py:82`), so an unregistered actor plane is a 503 there and a 500 on `/projects`. Publishing the actor plane at the edge is a separate decision a prefix move would not make: a route at a new prefix stays unreachable from the edge until the annotator chart's HTTPRoute names it.
11. **No merged `/docs`.** Each service serves its own `/docs`, `/redoc` and `/openapi.json`, and only under `docs.enabled` (off by default). Reached through the edge, a service's docs sit under its public prefix after the row's rewrite.
12. **The Gateway's limits are its policy, not the route's.** The platform's Gateway must allow response headers of ~16 KB (a chart-heavy zone sends ~4 KB of modulepreload `Link`), request bodies of 64 MB (page images, Arrow imports) and an idle stream of an hour (a `query.live` generator yields only on change and SvelteKit's SSE transport sends no keepalive). A route cannot set them.

## Gotchas

- **`RASK_API_PREFIX`'s code default is `/api/v1`, and nothing uses it.** Every deployment sets `/api` (`chart/values.yaml` under `config:`; `scripts/dev-micro.sh`; `.env.example`). Leave it unset and `/api/notifications` silently moves to `/api/v1/...`, off the path every frontend client hardcodes. `rask.apiRoutes` builds the notifications row from the same `config.RASK_API_PREFIX`, so the route and the mount move together in-cluster; keep `.env` in step for dev.
- **A route with no hostname claims every host its Gateway's listener accepts.** Two releases attached host-less to one listener are rival claims on `/` and `/api`, and the oldest route wins the tie. A real deployment (`rask.isRealDeployment`) must name `route.hostnames`, and the render refuses an empty list ([[XC-146]]); a second development install sets its own `route.hostnames` or `route.enabled=false`.
- **`scripts/dev-micro.sh` deliberately does NOT bash-source `.env`.** Each service loads it via `python-dotenv` so JSON-list settings like `RASK_CORS_ORIGINS=["..."]` parse correctly; bash sourcing strips the quotes. Export only vars *not* in `.env`.
