# 0165. The edge is the platform's Gateway: HTTPRoutes with a prefix rewrite, one `RASK_EDGE_URL`, Traefik's Gateway provider in development (2026-10-16)

**Status:** accepted. Source: owner, 2026-10-16, three decisions on XC-150 (#602) through the multi-question tool:
the route resource and where the public→internal path mapping lives, how the zones' server side reaches `/api/*`,
and which Gateway development runs. Carries out the ADR 0156 line "the Python gateway retires".

## Context

ADR 0156 retired the Python gateway (`services/gateway`): a reverse proxy on `:8888` that path-routed each public
`/api/*` prefix to its service, through its own Dapr sidecar by service invocation, behind one Ingress v1 rule
`/api` → `rask-gateway:8888`. Every service already authenticated each request itself, so the gateway added a hop,
an image, a route table restated in Python, and one hazard: its sidecar stamped a valid Dapr app token on every
request it forwarded, anonymous ones included, so catalog and lineage carried a second check
(`service_kit.governed.dapr_auth.is_public_caller`, `RASK_PUBLIC_CALLERS`) that refused a service-account token
arriving from the gateway's app-id.

The services do not serve their public paths: the catalog and lineage mount at the root, the viewer at `/api`, search
at `/api/search`, the annotator at `/api/annotations`. Only flows and notifications serve the public path as is.
Production's edge is moving to kgateway, and ingress-nginx is retired upstream, so the Ingress v1 annotations that
would express a per-prefix rewrite (`nginx.ingress.kubernetes.io/rewrite-target`) have no future there.

## Decision

1. **The routes are Gateway API HTTPRoutes with `URLRewrite` `ReplacePrefixMatch`.** `chart/templates/route.yaml`
   renders `<release>-api`, one rule per public prefix generated from the `rask.apiRoutes` helper
   (`chart/templates/_helpers.tpl`), and `<release>-web` (the zones at `/<zone>`, `/dex`, `/` → `web-home`), both on
   the Gateway `route.parentRefs` names. The route owns the public→internal mapping: `/api/explorer/search` → search
   `/api/search`, `/api/explorer/annotations` → annotator `/api/annotations`, `/api/explorer` → viewer `/api`,
   `/api/catalog` and `/api/lineage` → `/`, `/api/flows` and `/api/notifications` unchanged. Compute's `/api/ray` and
   `/api/serve` come from the compute fixture chart's own HTTPRoute. Only Core `PathPrefix` matches and the
   `URLRewrite` filter are used, so any conformant implementation serves them (kgateway, Envoy Gateway, Istio,
   Cilium). Services, their tests and every in-cluster caller keep the paths they have.
2. **The zones' SSR reaches `/api/*` through the cluster route, at one `RASK_EDGE_URL`.** It names the Gateway's
   in-cluster address (`route.edgeUrl`) and replaces both `RASK_GATEWAY_URL` and `LANCE_GATEWAY_URL` (default
   `http://localhost:8888`). There is one route table, the cluster's. Development runs no router of its own:
   `make dev-micro` runs the fleet only, `make dev-frontends-k3s` port-forwards k3s Traefik's Gateway to `:8888` so
   the zones reach the same routes, and `make dev-zone ZONE=<z>` runs one zone against its mocks. The zones' Vite dev
   `/api` proxies stay per zone.
3. **Development's Gateway is k3s's bundled Traefik with its Gateway provider switched on.** `deploy/k3s/traefik-gateway.yaml`
   is a HelmChartConfig that `scripts/k3s-install.sh` installs into k3s's manifests directory: GatewayClass `traefik`,
   Gateway `traefik-gateway` in `kube-system`, listener `web` on the node's port 80. `deploy/values-local.yaml`
   attaches to it and sets `route.edgeUrl: http://traefik.kube-system.svc.cluster.local` and
   `forwardedAllowIps: 10.42.0.0/16`. Moving development to kgateway or Envoy Gateway is a fixture and a
   `route.parentRefs` value, with no chart change.

## Considered options

- **Services serve their public paths natively, behind a plain Ingress v1.** The fix travels with the code and needs
  no rewrite filter, but it is a large diff across the services, their tests and the in-cluster callers, all to fit
  an API production's edge is leaving. Rejected.
- **Ingress v1 with implementation annotations for the rewrite.** Ties the chart to one controller's annotations,
  and ingress-nginx's are the ones being retired. Rejected.
- **A chart-rendered `RASK_API_ROUTES` table the zones resolve pod-to-Service.** Skips the Gateway hop for SSR, but it
  is the gateway's route table reborn in TypeScript: a second table beside the cluster's, which can drift from it.
  Rejected.

## Consequences

- `services/gateway`, `.docker/gateway.dockerfile`, the `rask-gateway` Deployment and Service, `services.gateway`
  values, the `RASK_*_URL` upstream ConfigMap keys, `RASK_LINEAGE_SIDECAR_ONLY_ROUTES` and
  `chart/templates/ingress.yaml` are deleted. The fleet is nine Python services.
- **The public-caller checks are deleted.** `is_public_caller`, `public_callers()` and `RASK_PUBLIC_CALLERS` existed
  only because the gateway's sidecar stamped an app token on public traffic. A request through the Gateway reaches
  the app port directly and never carries a sidecar token, so `require_dapr_token` alone refuses it on a Dapr-delivered
  route, and any verified service-account token authenticates at any door (ADR 0156 C5), through the edge included.
- **The service-invocation Resiliency is re-scoped to its one caller.** It was scoped to the gateway, so it never
  covered notifications' feed read (`/v1.0/invoke/lineage/method`), the only service-invocation call left.
  `<release>-invocation-resiliency` now renders with notifications, targets app `lineage` with `invokeRetry`
  (constant 2 s ×3, `408,429,500-599`) and `invokeTimeout` 300 s, and is scoped to `notifications`
  (`tests/unit/test_invariants.py::test_the_notifications_pod_asks_for_a_sidecar_and_is_allowed_to_receive_one`). No policy targets the catalog: no
  service invokes it through Dapr.
- **No merged `/docs`.** Each service serves its own `/docs`, `/redoc` and `/openapi.json` under `docs.enabled`.
- **Each service enforces its own boundary.** Every 401 and 403 comes from the service. NetworkPolicy admits each
  routed service's app port only (`<release>-edge-<component>`, one per `rask.apiRoutes` row), and the zones' egress
  to the Gateway's data plane is `route.edgeEgress`, which the platform states because a policy sees the data
  plane's pod port. Catalog, lineage and the media trio now run uvicorn with
  `--proxy-headers --forwarded-allow-ips=<forwardedAllowIps>`, so anonymous rate-limit keys stay per client.
- **The Gateway's limits are its policy, not a route's.** The platform's Gateway must allow response headers of
  ~16 KB (a chart-heavy zone sends ~4 KB of modulepreload `Link`), request bodies of 64 MB (page images, Arrow
  imports) and an idle stream of an hour (a `query.live` generator yields only on change, and SvelteKit's SSE
  transport sends no keepalive).
- A route with no hostname claims every host its listener accepts, so a second release on one Gateway sets its own
  `route.hostnames` or `route.enabled=false`.
- The offline gate is `tests/unit/test_the_cluster_route_names_one_backend_per_api_prefix.py` (the render names one
  backend per prefix); the live proof is `tests/e2e-py/test_the_cluster_route_reaches_each_service.py` (marker `edge`,
  `make e2e-edge RASK_E2E_EDGE_URL=…`).
