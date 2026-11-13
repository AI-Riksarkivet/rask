# chart/templates/route.yaml

Rationale for `chart/templates/route.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.
The decision itself is recorded in `docs/adr/0165-the-edge-is-the-platforms-gateway-api.md`.

## `{{- if .Values.route.enabled }}`

The north-south edge is the PLATFORM's Gateway (Kubernetes Gateway API), not a rask process. The chart
renders the `/api` HTTPRoute that attaches to it, plus a `/dex` one with the development Dex, and every service behind them authenticates each request
itself: nothing at the edge holds a token, a session or an authorization decision. Only Core path
matches and the `URLRewrite` filter are used, so any conformant implementation serves these routes —
kgateway (the intended production edge), Envoy Gateway, Istio, Cilium, and on k3s Traefik's Gateway
provider, which `deploy/k3s/traefik-gateway.yaml` switches on (`scripts/k3s-install.sh` installs it).

What an implementation sizes for itself is the Gateway's policy, not a route's, and the platform's
Gateway must allow it: request bodies of 64 MB (Arrow imports).

## `{{- if not .Values.route.parentRefs }}{{ fail ... }}`

A route with no parent is published nowhere and Helm would install it silently. `route.parentRefs` names
the Gateway (and optionally its listener as `sectionName`): the platform's in production, Traefik's
`traefik-gateway` in kube-system (listener `web`, the node's port 80) on k3s, set by
`deploy/values-local.yaml`.

## `{{- if and (include "rask.isRealDeployment" .) (not .Values.route.hostnames) }}{{ fail ... }}`

A route with no hostname claims every host its Gateway's listener accepts (Gateway API `HTTPRoute.spec.hostnames`, CRD
v1.5.1): on a listener with no hostname it matches any Host, and on a hostname-scoped listener it inherits the
listener's host, and the chart cannot see which kind of listener the platform provides. Routes tied on hostname fall to
the match-precedence rules, which end at the oldest route, so two releases attached host-less to one listener with the
same prefixes are rival claims that one of them silently wins, `/dex` included. A route that
names its host takes precedence over a host-less one for that host.

So a real deployment names the hosts the platform's Gateway routes to it, and the render refuses one that names none
([[XC-146]]). The condition is `rask.isRealDeployment`, the one `prod-credentials.yaml` and the root-create lock read, so
the three cannot disagree about what "real" means. A development estate renders host-less: the k3s estate is reached on
whatever host its listener accepts (`localhost`, the node's address), and the kind and Dagger e2e lanes render no route.

## `name: {{ include "rask.fullname" . }}-api`

`<release>-api`: the doors each service publishes, ranged from the `rask.apiRoutes` helper (`_helpers.tpl`): one
rule holding every match of a service whose path is forwarded unchanged, and one rule per path of a service the route
rewrites, since a rule carries one rewrite. The helper is the ONE route table — the NetworkPolicy edge rules
(`network-policy.yaml`, `<release>-edge-<component>`) range the same list, so a service the route
publishes is always a port the policy admits. Gateway API matches the longest prefix first, regardless of
rule order.

| Public prefix | Service | Path at the service |
| --- | --- | --- |
| `/api/catalog/v1`, `/api/catalog/management/v1` | `<fullname>-catalog` | unchanged; the catalog strips `/api/catalog` |
| `/api/lineage/events`, `/api/lineage/api/v1/lineage`, `/api/lineage/admin/dlq`, `/api/lineage/tables` | `<fullname>-lineage` | `/events`, `/api/v1/lineage`, `/admin/dlq`, `/tables` |
| `<prefix>/docs`, `<prefix>/redoc`, `<prefix>/openapi.json`, under `docs.enabled` only | each of the two | as its row above |

No row publishes a service's root. A pod answers routes for its own platform (the kubelet's `/livez` and `/readyz`,
the sidecar's `/dapr/subscribe`, the Dapr deliveries and cron ticks), and a prefix over the root would publish those and
every route the service mounts later. The route keeps them off the edge; the pod's port still admits any peer
(`<release>-edge-<component>`), and each route authenticates or answers only what a sidecar needs. Anonymous, `/dapr/subscribe` and each tick's `OPTIONS` answer 2xx, so publishing
them hands the edge the estate's topics and binding names.

A client has no row here, and none is rendered: a client publishes its own prefixes from its own release. The console,
the lakehouse's own UI, is routed at `/` by `console.yaml` on the same parentRefs and hostnames, and every `/api/*` row
is a longer prefix, so it keeps winning.

An unmatched `/api/*` path falls to the console's `/` rule, whose server answers it 404 rather than the page; with
`console.enabled: false` it is the Gateway's own 404: no rule, no backend. A 401 or 403 always comes from
the service the prefix names.

The offline gate is `tests/unit/test_the_cluster_route_names_one_backend_per_api_prefix.py` (the render);
the live one is `tests/e2e-py/test_the_cluster_route_reaches_each_service.py` (marker `edge`,
`make e2e-edge RASK_E2E_EDGE_URL=…`). The render gate boots each app under the env its pod renders and fails when the
route publishes a route the pod answers only for its platform, leaves a door the app mounts unpublished, or names a
path the app mounts nothing under: a new root route of a service is a path in its `rask.apiRoutes` row, or a delivery
or tick the gate already reads off the app.

## `- type: URLRewrite`

`ReplacePrefixMatch` maps a public path to the path the service mounts, so the route owns the
public→internal mapping and the services, their tests and their in-cluster callers keep their own paths
(lineage serves at its root). A row with `rewrite: false` forwards the path unchanged. The catalog's row is one: a rewrite rebuilds the path from its decoded form, so the `%2B` and `%2F` the
catalog judges a name by would arrive as `+` and `/` (measured on Traefik 3.7.8), and the catalog strips its public
prefix itself (`rask.catalogPublicPrefix`, `LANCE_PUBLIC_PATH_PREFIX`, [[LH-526]]).

## `{{- if and .Values.auth.enabled .Values.dex.enabled }}`

`<release>-dex`: the development Dex under `/dex` on the same origin as `/api`, so the whole OIDC round-trip
(authorize, login form, token, JWKS) rides one public origin: no second hostname, no extra tunnel. Dex already serves
under `/dex` (its issuer path). Split-horizon: pods fetch discovery and keys from the internal Service URL, browsers via
this route. Rendered only with `auth.enabled` and `dex.enabled`. Without the development Dex nothing legitimate answers `rask-dex`, and a route to
it would hand the app's own origin, under `/dex`, to whatever workload takes that name ([[XC-148]]).

## `{{- with .Values.route.hostnames }}`

Both routes carry the hostnames the release names. Empty, which only a development estate renders (the refusal
above), they match every host the Gateway's listener accepts (any Host, a bare IP, a forwarded localhost), so two
releases attached host-less to one listener are rival claims on `/api` and `/dex`; a second development install sets its
own `hostnames` or `route.enabled=false`.
