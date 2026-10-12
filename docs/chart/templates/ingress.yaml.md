# chart/templates/ingress.yaml

Rationale for `chart/templates/ingress.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: networking.k8s.io/v1`

The north-south edge. ONE Ingress for the whole release: it path-routes the SvelteKit
micro-frontend ZONES (each domain zone owns its base path /<zone>; the catch-all `home`
owns "/"), plus /api → the fleet gateway and — auth on — /dex → the IdP, so the whole
estate rides ONE public origin. Controllers match longest-prefix, so the specific rules
win over the catch-all "/" regardless of the order below; they are still emitted
specific-first because that is the order a reader checks them in.

Frontend object names are `<fullname>-web-<zone>` (see frontends.yaml: the `annotator`
ZONE and the `annotator` BACKEND service share one release, so the frontend tier carries
a `web-` prefix). A zone's path comes from its `path` in .Values.frontend.apps and MUST
equal the `paths.base` pinned in that zone's svelte.config.js, or SvelteKit serves assets
from a prefix the edge never routes.

The zones' own /<zone>/api/* BFF routes (bearer-forwarding proxies to the media
viewer/search/annotator Services) are covered by the zone prefix rules — there are
deliberately NO root-absolute /api/explorer|/api/search rules: that side door let
unauthenticated edge traffic reach the media services directly, bypassing the BFF gate.

k3s ships Traefik and the annotations are ingress-nginx's; both are keyed defaults in
values.yaml, and an unknown annotation is inert to a controller that does not own it.
The proxy-read-timeout one is load-bearing, not decoration: ingress-nginx's 60s default
counts IDLE time on the upstream, so a `query.live` generator that yields only on change
is severed after a minute of quiet and SvelteKit's SSE transport sends no keepalive to
refresh that clock. It pairs with the zones' IDLE_TIMEOUT (frontends.yaml) — both hops
must hold, and the SMALLER one always wins.

## `- {{- with .Values.ingress.host }}`

Empty host => no host rule => catch-all (any Host / bare IP / VS Code-forwarded localhost).

## `- path: /api`

/api is the FLEET gateway (services.gateway, uvicorn `gateway:app` on
:8888): it path-routes to flows, notifications and the lance planes, and
it is what the zones' relative /api/* client fetches
resolve to (SSR uses the in-cluster URL instead). It renders in every
install (fleet.yaml is not gated), so this rule never dangles.

RESOLVED (docs/adr/0117-nginx-is-retired-and-the-fastapi-gateway-is-the-in-cluster.md): lance-ns's nginx gateway
(top-level `gateway:`, :8080, templates/gateway.yaml) is deleted, so
`<fullname>-gateway` names exactly one object — the fleet gateway — and
every gateway route below names the fleet gateway's port. kgateway is
the intended future edge (not implemented on this branch).

## `- path: /dex`

Dex under the SAME origin as the zones: /dex → the dex Service, so the whole
OIDC round-trip (authorize, login form, token, JWKS) rides the one public
origin — no second hostname, no extra tunnel, no DNS tricks. Dex already
serves under the /dex path (its issuer path). Split-horizon: pods fetch via
the internal Service URL (OIDC_INTERNAL_ISSUER), browsers via this route.

## `{{- range .Values.frontend.apps }}`

The catch-all home zone LAST: "/" and anything that is not a domain zone
(host carries the project; path carries the domain).

## `- path: /`

Frontend off (the headless `--set frontend.enabled=false` stacks in
scripts/e2e_stack.sh): fall back to the fleet gateway at "/" so the edge
still resolves to something that exists.
