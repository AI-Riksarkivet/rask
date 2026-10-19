# chart/templates/route.yaml

Rationale for `chart/templates/route.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.
The decision itself is recorded in `docs/adr/0165-the-edge-is-the-platforms-gateway-api.md`.

## `{{- if .Values.route.enabled }}`

The north-south edge is the PLATFORM's Gateway (Kubernetes Gateway API), not a rask process. The chart
renders two HTTPRoutes that attach to it, and every service behind them authenticates each request
itself: nothing at the edge holds a token, a session or an authorization decision. Only Core path
matches and the `URLRewrite` filter are used, so any conformant implementation serves these routes —
kgateway (the intended production edge), Envoy Gateway, Istio, Cilium, and on k3s Traefik's Gateway
provider, which `deploy/k3s/traefik-gateway.yaml` switches on (`scripts/k3s-install.sh` installs it).

What an implementation sizes for itself is the Gateway's policy, not a route's, and the platform's
Gateway must allow it: response headers of ~16 KB (a chart-heavy zone sends ~4 KB of modulepreload
`Link`), request bodies of 64 MB (page images, Arrow imports), and an idle stream of an hour (a
`query.live` generator yields only on change and SvelteKit's SSE transport sends no keepalive; it pairs
with the zones' IDLE_TIMEOUT in frontends.yaml, and the smaller of the two always wins).

## `{{- if not .Values.route.parentRefs }}{{ fail ... }}`

A route with no parent is published nowhere and Helm would install it silently. `route.parentRefs` names
the Gateway (and optionally its listener as `sectionName`): the platform's in production, Traefik's
`traefik-gateway` in kube-system (listener `web`, the node's port 80) on k3s, set by
`deploy/values-local.yaml`.

## `name: {{ include "rask.fullname" . }}-api`

`<release>-api`: one rule per public `/api/*` prefix, ranged from the `rask.apiRoutes` helper
(`_helpers.tpl`). The helper is the ONE route table — the NetworkPolicy edge rules
(`network-policy.yaml`, `<release>-edge-<component>`) range the same list, so a prefix the route
publishes is always a port the policy admits. Gateway API matches the longest prefix first, so
`/api/explorer/search` wins over `/api/explorer` regardless of rule order.

| Public prefix | Service | Path at the service |
| --- | --- | --- |
| `/api/explorer/search` | `<fullname>-search` | `/api/search…` |
| `/api/explorer` | `<fullname>-viewer` | `/api…` |
| `/api/catalog` | `<fullname>-catalog` | `/…` |
| `/api/lineage` | `<fullname>-lineage` | `/…` |
| `/api/flows` | `<release>-flows` | unchanged |
| `/api/notifications` | `<release>-notifications` | unchanged |

The explorer rows render only with `explorer.enabled`; flows and notifications only when they are
front-door (`services.<name>.frontDoor`) or the install is single-tenant. Compute's `/api/ray` and
`/api/serve` are not here: the compute fixture chart renders its own HTTPRoute
(`deploy/fixtures/compute/chart/templates/route.yaml`).

An unmatched `/api/*` path is the Gateway's own 404: no rule, no backend. A 401 or 403 always comes from
the service the prefix names.

The offline gate is `tests/unit/test_the_cluster_route_names_one_backend_per_api_prefix.py` (the render);
the live one is `tests/e2e-py/test_the_cluster_route_reaches_each_service.py` (marker `edge`,
`make e2e-edge RASK_E2E_EDGE_URL=…`). A new public prefix is a row in `rask.apiRoutes` plus a row in that
render test.

## `- type: URLRewrite`

`ReplacePrefixMatch` maps a public prefix to the path the service mounts, so the route owns the
public→internal mapping and the services, their tests and their in-cluster callers keep their own paths
(the catalog serves `/v1/...` at its root; the media trio mount under `/api`). A row with an empty
`rewrite` forwards the path unchanged.

## `name: {{ include "rask.fullname" . }}-web`

`<release>-web`: the zones and Dex on the same origin as `/api`. Each domain zone owns its base path
`/<zone>`; the catch-all `home` zone owns `/`. A zone's path comes from its `path` in
`.Values.frontend.apps` and MUST equal the `paths.base` pinned in that zone's `svelte.config.js`, or
SvelteKit serves assets from a prefix the edge never routes. Frontend object names are
`<fullname>-web-<zone>` (the `annotator` ZONE renders here and the annotator's own release, deploy/fixtures/annotator,
renders `<fullname>-annotator` in the same namespace, so the frontend tier carries a `web-` prefix). That release publishes
`/api/explorer/annotations` on the same Gateway with its own HTTPRoute, which Gateway API's longest-prefix rule puts ahead
of `/api/explorer`.

## `- path: { type: PathPrefix, value: /dex }`

Dex under the same origin as the zones, so the whole OIDC round-trip (authorize, login form, token,
JWKS) rides one public origin: no second hostname, no extra tunnel. Dex already serves under `/dex` (its
issuer path). Split-horizon: pods fetch via the internal Service URL (OIDC_INTERNAL_ISSUER), browsers via
this route. Only with `dex.enabled`: without the development Dex nothing legitimate answers `rask-dex`,
and a route to it would hand the app's own origin, under `/dex`, to whatever workload takes that name
([[XC-148]]).

## `{{- with .Values.route.hostnames }}`

Empty hostnames match every host the Gateway's listener accepts (any Host, a bare IP, a forwarded
localhost). A route with no hostname is a claim on every host that listener accepts, so two releases
attached host-less to one listener are rival claims on `/` and `/api` and the Gateway picks between them;
a second install sets its own `hostnames` or `route.enabled=false`.
