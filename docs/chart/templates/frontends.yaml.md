# chart/templates/frontends.yaml

Rationale for `chart/templates/frontends.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if .Values.frontend.enabled }}`

SvelteKit SSR micro-frontend zones. One Deployment + Service per .Values.frontend.apps
entry, all built from the ONE parametrized .docker/frontend.dockerfile (image
"<zone>:<tag>", `make k3s-build`). The catch-all (catchAll: true) serves "/"; every
domain zone is pinned to its base path /<zone> in its svelte.config.js and is
TCP-probed — a zone's "/" is a 404 under its base path, so no httpGet. The Ingress
path-routes /<zone> -> this Service (ingress.yaml); the gateway sits BEHIND these Bun
servers and owns /api only — it never serves the SPA shell.

NAMING — the live collision class of the lance-ns merge: every frontend object is
`<fullname>-web-<zone>`, NOT the bare zone name. The merged estate has an `annotator`
ZONE and an `annotator` BACKEND service (media.yaml) under ONE release: bare names
would render two Deployments/Services called `rask-annotator` (an undeployable
release), and a shared `app.kubernetes.io/component: annotator` selector would
cross-wire the backend Service onto the SvelteKit pods. The `web-` prefix keeps the
frontend tier disjoint from every backend component. Since R22 the IMAGE name is
`web-<zone>:<tag>` too (the Makefile builds `web-home:dev`, …): the fleet's Ray-plane
service image took the bare `compute` name, which the compute ZONE image previously
held — the web- prefix extends the same disjointness invariant to the image namespace.
ha.yaml (zone PDBs) and network-policy.yaml (the front-door rule) already select on
`app.kubernetes.io/component: web-<zone>` — the three must stay in step.

Every zone shares ONE env block (the "auth/secret similar in every MFE" seam):
the backend URLs the zones' BFF proxies target, the gateway URL the SSR /api rewrite
uses, and — auth on — the service-cred read fallback + the OIDC config, so a login on
any zone is a login on every zone (one origin, one sealed cookie). The media/annotator
zones additionally get the media plane's upstreams, scoped per zone.

## `{{- $gwPort := $root.Values.services.gateway.port -}}`

The zones' gateway is the RASK FLEET gateway (services.gateway, uvicorn `gateway:app`
on :8888) — the process that path-routes /api/* to ray/controlplane/the lance planes,
and the target the zones' relative /api/* fetches resolve to through
the Ingress. lance-ns's own nginx gateway (top-level `gateway:`, :8080,
templates/gateway.yaml) is RETIRED and deleted (docs/adr/0117-nginx-is-retired-and-the-fastapi-gateway-is-the-in-cluster.md),
so `<fullname>-gateway` names exactly one object — the fleet gateway. kgateway is
the intended future edge (not implemented on this branch).

## `{{- $expected := list (printf "LINEAGE:%s" $root.Values.services.lineage.daprAppId) -}}`

Dead-subscription detector (the lakehouse zone's admin /streams view): the comma
list of "STREAM:service" consumer groups the estate EXPECTS on JetStream, rendered
from the SAME values (and the same medallion.enabled / catalog.controlEmit /
dapr.resiliency.enabled gates) that dapr-component.yaml renders the real
subscriptions from — so the expectation cannot drift from the topology. Gated on
dapr.enabled too: components may render, but with no injected sidecar nothing
subscribes and every group would read as a false dead subscription.

## `{{- $tag := .tag | default $fe.image.tag -}}`

Per-zone tag wins over the shared default: independent deploy is the ONLY thing a
zone boundary buys, and one tag across every zone silently cancels it.

## `{{- $tier := (index $root.Values.resources $comp) | default $fe.resources -}}`

Per-zone resource tier if values(-prod) define one (resources.web-<zone>), else the
shared frontend tier.

## `{{- $ro := $root.Values.security.readOnlyRootFilesystem -}}`

rask's frontend.securityContext keys WIN; lance-ns's restricted-PodSecurity
hardening (runAsNonRoot + readOnlyRootFilesystem + RuntimeDefault seccomp) fills
the rest. A read-only rootfs is feasible because /tmp is an emptyDir below.

NB: this action closes with `}}`, not `-}}` — its trailing newline is what puts the
`---` below on its own line. Right-trimming it welds the previous zone's Service onto
this Deployment's separator (`component: web-home---`), silently merging two objects
into one document.

## `- name: BODY_SIZE_LIMIT`

--- the shared zone env (every zone gets these) ---------------------------
The Bun server's request-body ceiling, and it MUST be set: `svelte-adapter-bun`
defaults BODY_SIZE_LIMIT to 512K (see a zone's own build/index.js), which is
smaller than anything this estate actually uploads — a page scan is megabytes.
Over the limit the SERVER answers 413 before the route runs, with a body that is
not our problem+json, so the UI could only report an opaque "HTTP 413". Measured
2026-08-06: a 2.25 MB scan into studio's /api/infer.

Deliberately ABOVE the BFF's own 25 MB guard (`@rask/api/serve-proxy`) so the
guard is what speaks: it names the limit and the reason, whereas the adapter's
413 is anonymous. The zone-level knob is the outer bound, not the policy.

## `{{- include "rask.zoneOtelEnv" (list $root (printf "web-%s" .name)) | nindent 12 }}`

TELEMETRY. The zones are the FIRST hop of every browser request — a page is served by
this Bun server before anything reaches the gateway — and they exported nothing, so a
slow page with a healthy gateway had no span anywhere to explain it. Worse than a gap:
the RED dashboard's unfiltered `sum by (service_name)` rendered their absence as "these
services do not exist" rather than "these services are unmonitored".

The SAME helper the fleet uses, so the endpoint, protocol, headers and resource identity
come from one derivation rather than a fourth hand-rolled copy. `OTEL_SERVICE_NAME` is
the zone's own name, which is what makes it appear in the dashboard as itself.

Verified before wiring, not assumed: the OTel Node SDK runs under Bun — spans created,
correctly parented, and AsyncLocalStorage surviving an await, which is exactly what a
per-request server span needs. Only MANUAL instrumentation is used; the
auto-instrumentation packages depend on Node loader hooks Bun implements incompletely,
and nothing here needs them.

## `{{- with include "rask.rayServeOrigin" $root }}`

DERIVED from the one Ray plane (`rask.rayServeOrigin` over `rask.rayDashboardUrl`): discovery
and INVOCATION must name the same cluster or the builder lists apps it cannot call.
Verified 2026-08-06: an external dashboard host also serves the Serve apps
(`dev-kuberay.ra.se/gemma-31b/v1/...` answers). `frontend.serveUrl` wins when Serve
genuinely lives somewhere else; a render naming no Ray leaves the env unset.

Ray Serve's HTTP INGRESS — a different endpoint from the dashboard the compute service
reads for `/api/serve` discovery, and nothing routes to it: inference is the one call the
gateway cannot carry (its `/api/serve` row is GET-only by design, because Ray's dashboard
exposes a deploy API). So the ONE zone that offers inference reaches it directly, and
defaults to `http://localhost:8000` in code — which inside a pod is the pod. Studio takes
the ORIGIN, because it appends `?app=` per request.

## `- name: RASK_GATEWAY_URL`

NO `COMPUTE_SERVE_URL`, and its absence removed a whole class of trap with it.

It fed the compute zone's inference playground, deleted 2026-08-07 (a single-app,
HTR-only duplicate of studio's flow canvas — see compute/src/lib/nav.ts). With no reader
left, injecting it would be dead config: set on the pod and read by nothing.

It also took TWO renderings to express, and they collided: an ungated row here plus a
ray-enabled row further down set the SAME name on the SAME zone, so the env list carried
it twice, and Kubernetes' strategic-merge-patch keys env by `name` — helm refused the
upgrade with `The order in patch list … doesn't match $setElementOrder` AFTER partially
applying, taking ~20 deployments to ImagePullBackOff on 2026-08-06.
`test_no_env_var_is_rendered_TWICE_on_any_workload` (parametrized over `ray.enabled`,
because that toggle is what produced the overlap) still guards the class.
SSR load/remote functions reach the gateway server-side via an absolute base
URL; client code uses the relative /api/* Ingress route. Both names are live:
the api package's gateway client reads RASK_GATEWAY_URL, the BFF reads
LANCE_GATEWAY_URL — one Service, two readers, so both must be injected.

## `- name: CATALOG_API`

The backend planes the zones' BFF proxies target directly.

## `- name: GREPTIME_API`

NO `MEDALLION_API`. Its ONLY frontend reader was the models zone's `/pipeline` trigger
door (`medallion.remote.ts` → POST /produce, /train), deleted 2026-08-07 — so injecting it
now would be dead config: a plane fully "configured" and read by
nobody. The medallion is still reachable SERVER-side, through the gateway's own
`RASK_MEDALLION_API_URL` row, which is a different variable with a live reader.

## `- name: GREPTIME_AUDIT_TABLE`

The table the governance trail lives in, `observability.query.auditTable`. It must name the table
`observability.export.auditHeaders` routes the trail to, or the viewer reports an empty audit log while
the rows land elsewhere. Both default to `lance_audit`; the two values are the platform's to keep equal,
since the export header is the backend's contract, not a name rask can derive.

## `- name: NATS_MONITOR_API`

JetStream visibility (admin /streams): the NATS HTTP monitor port —
unauthenticated by design and ClusterIP-only, consumed strictly server-side
behind the zone BFF's admin gate, never by the browser. The HEADLESS Service
is the one carrying :8222 (the plain Service exposes only 4222).

## `- name: PORT`

--- the Bun server's own transport knobs ---------------------------------

## `- name: PROTOCOL_HEADER`

Behind Traefik/nginx: derive the request origin from forwarded headers so
adapter-bun's CSRF/origin check matches however the cluster is reached (node
IP, localhost, VS Code-forwarded port) — no fixed ORIGIN.

## `- name: IDLE_TIMEOUT`

IDLE_TIMEOUT is read by svelte-adapter-bun's bootstrap (Bun.serve idleTimeout,
SECONDS) and it is the FIRST thing that severs a live query, well before the
edge: at the default 10 a `query.live` generator that yields only on change
became a ~12s reconnect loop. Bun caps it at 255, and a stream still died at
256.8s over a 290s hold — for a streaming response it behaves as a maximum
connection LIFETIME that outbound SSE writes do not refresh, so no keepalive
can beat it. 0 (disabled) is the default; the wedged-client objection is
answered by the app-level 20s keepalive, whose write fails on a vanished peer.
`hasKey`, never `| default`: Helm treats 0 as empty, so `| default 255`
rendered 255 for an explicit 0 and the change looked applied while nothing
moved. Pair with ingress.annotations' proxy-read-timeout — both hops must
hold, and the SMALLER one always wins.

## `- name: LINEAGE_SERVICE_TOKEN_FILE`

Governed READ fallback: with no user session the BFF authenticates to lineage
as a SERVICE (bounded by frontend.serviceIdentity's FGA READER rung), so the
read-only UI works without a per-user browser login.

The zone's projected `rask-lineage` identity ([[LH-220]]): the BFF re-reads the file per
request, so the kubelet's rotation reaches a running pod with no restart.

## `{{- if not $fe.oidc.publicIssuer }}{{ fail "frontend.oidc.enabled requires frontend.oidc.publicIssuer (a br...`

Per-user OIDC login (opt-in; needs a browser-reachable IdP). Every zone reads
the sealed session cookie (ISSUER+CLIENT_ID+REDIRECT_URI make authEnabled
true; SESSION_SECRET decodes it); the home zone also presents the confidential
client secret at the token exchange. Both ride `<release>-frontend-session`, which
the external-secrets operator writes from the store (templates/external-secrets.yaml).

## `- name: OIDC_INTERNAL_ISSUER`

Split-horizon OIDC: the zones' SERVER-side fetches (discovery doc, token POST)
go to the in-cluster Dex Service — the public issuer may only resolve for
browsers. The BROWSER-facing authorize URL and the `iss` expectation keep the
public OIDC_ISSUER; the BFF rewrites the discovered endpoints onto this base.

## `{{- if and $root.Values.explorer.enabled (or (eq .name "explorer") (eq .name "annotator")) }}`

NO per-zone ray block here any more. It set the compute zone's `COMPUTE_SERVE_URL`
to the IN-CLUSTER head (`rask-ray-head-svc:8000<serveRoutePrefix>`), and that address
is the second half of why the playground it fed never worked: rask's Ray is managed
OUTSIDE this repo (`ray.dashboardUrl`, measured live as https://dev-kuberay.ra.se),
so the in-cluster head is the opt-in case and this block pointed the one inference
surface at the wrong cluster — a CPU htrflow — whenever ray.enabled was set.
Inference now has exactly one door, studio's, which takes `$serveOrigin` above and is
therefore derived from the same ONE signal as discovery. The reasoning for why
inference bypasses the gateway at all (its `/api/serve` row is GET/HEAD-only on
purpose, because Ray's dashboard can PUT a deployment) lives with STUDIO_SERVE_URL.

## `- name: VIEWER_API`

--- the media plane's upstreams for these two zones' BFF proxy routes -------
(/<zone>/api/* -> the media Services). Scoped PER ZONE, not handed to every
zone: one should not hold a service URL it never routes to. explorer searches
and reads the viewer, and reaches the annotator only for the workflow's tag
write + batch-job submit; the annotator reads the viewer for the media it
draws on the canvas.

SEARCH_API used to sit behind an `eq .name "explorer"` guard, on the claim that
"the annotator never searches". That stopped being true when /annotator/browse
gained "more like this" — a k-NN query over the corpus's declared vector
binding, which is `GET /api/search/similar` on the SEARCH service and cannot be
answered by the viewer. Without this the zone's `findSimilar` remote falls back
to its localhost dev default and every neighbour lookup fails in-cluster, with
nothing in the browser to say the URL was never injected.

## `- name: MEDIA_ASSIST_URL`

Mirror of the annotator SERVICE's runner seams (media.yaml) onto the zone's
web pod: its /api/config endpoint reads these to render the honest
"mocked — needs runner" chip, so the chip's truth tracks the same values
switch as the service env (found live 2026-07-24: runner deployed, service
wired, chip still claiming mock).

## `{{- include "lance.tcpProbes" "http" | nindent 10 }}`

TCP probes (not httpGet): a zone has no index route at its base path, so GET "/"
404s. An open port proves the adapter-bun server is alive.
