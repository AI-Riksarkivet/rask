# open-gateway — kgateway becomes the edge, then the Python gateway dissolves

Plan, 2026-08-03. Two phases, **strictly sequenced**; a fresh session picks up Phase 1
(the footprint is `chart/`, which
it already owns). Phase 2 starts only after Phase 1 is proven with a browser. Do not
stack the two: they both change the only path between the browser and every backend, and
stacked changes there make nothing attributable.

**Why:** `values-prod.yaml` already declares kgateway "the intended future edge"; today's
edge is ingress-nginx. Separately, `services/gateway` (`:8888`) is a hand-rolled Python
reverse proxy — longest-prefix routing, hop-by-hop stripping, streaming, path
normalization, a 403 blocklist — almost all of which Envoy (kgateway's data plane) does
natively, with retries/timeouts/observability we don't get from httpx. Once kgateway IS
the edge, in-cluster traffic is *edge proxy → Python proxy → service*: two stacked
proxies where the second mostly forwards bytes. End-state: the edge routes `/api/*`
straight to services and `services/gateway` is deleted.

**What does NOT change in either phase** (the four-layer rule — one job each):

- **Auth** — the OIDC BFF stays in the home zone (`/auth/*`); the browser holds only the
  httpOnly session cookie; zone Bun servers hold the bearer and mint `Authorization` on
  east-west calls. The edge only forwards. Same-origin for all zones under one host must
  be preserved (the cookie depends on it).
- **Authz** — enforcement stays in the services (FGA checks per resource); OpenFGA stays
  ClusterIP-only (`openfga:8080`), never at the edge. No ext-authz at the edge: a second
  enforcer that can disagree with the first is the Cedar+OpenFGA mistake in new clothes.
- **BFF/remote functions** — zone-server → service calls are east-west (direct service
  URLs: `CATALOG_API`, `LINEAGE_API`, …) and never transit the edge. Only north-south
  (page loads, `/_app/remote/*`, client `/api/*` fetches) rides it.
- **Dapr** — pub/sub (NATS), state store, secret store are sidecar planes; the edge never
  sees them. Subscription-delivery endpoints (`/bronze-arrival`, sidecar-only lineage
  routes) must remain unreachable from outside (see Phase 2 §"structural allowlist").

> **CORRECTION, 2026-08-03 (verified against the running cluster).** The line above is true of
> pub/sub, state and secrets — and it omits **service invocation**, which is the gateway's entire
> job. The north-south chain today is FOUR hops, not two:
>
> ```
> browser → Traefik(Ingress) → rask-gateway:8888 → the gateway's OWN daprd :3500 → service
> ```
>
> `gateway/__init__.py:155-157` builds `http://127.0.0.1:{DAPR_HTTP_PORT}/v1.0/invoke/{app_id}/method`,
> and `chart/templates/dapr-resiliency.yaml:60-62` says so explicitly: traffic "leaves the gateway
> through its own daprd … and Dapr applies invocation resiliency on the CALLING sidecar — so this
> policy is scoped to the gateway and targets every app-id the gateway routes to".
>
> Phase 2's table ("HTTPRoute rules per service") therefore does not merely re-home routing — it
> **removes Dapr service invocation from the north-south path**, and with it three things the table
> does not name:
>
> 1. **mTLS** between caller and service, issued by dapr-sentry. An edge→Service `backendRef` is
>    plaintext unless something replaces it.
> 2. **Invocation resiliency** — the retries, timeouts and `invokeBreaker` circuit breakers scoped to
>    the gateway app-id. These are not theoretical: three were found LATCHED on 2026-08-03
>    (`compute`, `viewer`, `controlplane` — see `docs/architecture/edge-baseline.md`).
> 3. **`dapr-api-token`** — `service_kit/governed/dapr_auth.py` rejects a sidecar-delivered request
>    whose token does not match the app's `APP_API_TOKEN`. This is the guard Phase 2 calls "the
>    load-bearing guard" that survives the blocklist's removal. An edge calling a Service directly
>    sends no such header, so the guard either rejects every call or stops guarding. **Phase 2 cannot
>    proceed until this is decided**, because the structural-allowlist argument rests on it.
>
> None of this affects Phase 1, which does not touch the gateway, Dapr, or any service. It is
> recorded here so Phase 2 starts from the real topology.

- **Authz stays Dapr-invisible either way** — FGA checks run INSIDE each service on the request it
  receives, whatever transport delivered it. Removing Dapr from the path does not move an FGA check;
  it changes who may reach the endpoint that runs one. That distinction is the whole of item 3 above.

---

## Phase 1 — nginx → kgateway at the edge (chart only)

The rask gateway, every service, and all app code are untouched. Goal block for the
executing session:

> **/goal** Migrate the cluster edge from ingress-nginx to kgateway (Gateway API), as
> `values-prod.yaml:167` already declares. Condition:
> 1. kgateway installs behind a values toggle following the `cnpg.enabled` pattern — the
>    toggle gates operator AND resources; nginx remains the default; both edges render
>    cleanly (`helm template` with the toggle in each position).
> 2. The Ingress rules become `Gateway` + `HTTPRoute` with identical routing: `/api` →
>    `rask-gateway:8888`, `/<zone>` → `rask-web-<zone>:3000` specific-first, `/` → home
>    last, NO path rewriting (pods receive the full path — `paths.base` consumes it).
> 3. The live-stream timeout survives: `chart/values.yaml:975` sets
>    `proxy-read-timeout: 3600` because every zone holds `query.live` streams open
>    (the bell, the admin console feed, the FGA live canvas) and nginx's 60s default
>    counts stream silence as death. The kgateway equivalent (HTTPRoute
>    timeouts/policy) must be set and PROVEN by watching a notification-bell stream stay
>    connected >90s through the new edge.
> 4. The Dapr helper comments' "routes become HTTPRoutes" note is honoured or explicitly
>    deferred with a comment at the site.
> 5. Verified like it ships: `make k3s-up` green with the toggle ON, and a
>    real browser through the new edge reaches `/`, `/lakehouse`, `/compute`, and
>    `/api/catalog` — not just `kubectl get gateway`.
> 6. OpenFGA stays ClusterIP-only and the rask gateway Deployment is untouched — if
>    either changes, the change is wrong.
> 7. Footprint is `chart/` only; commit own paths only (concurrent sessions
>    are active in this repo).

**Phase-2 readiness (free now, saves a round later):** shape the HTTPRoutes per-service
(one rule per future backend, named after the service) rather than one big `/api` rule,
so absorbing the gateway's rows later is adding `backendRefs`, not restructuring.

## Phase 2 — dissolve `services/gateway` (own session, after Phase 1 is browser-proven)

Everything the Python gateway does either moves to the edge, moves to a service, or dies:

| Gateway behaviour | New home |
|---|---|
| Longest-prefix `/api/*` routing (`gateway/__init__.py::_routes()`) | `HTTPRoute` rules per service — Gateway API is specific-first natively |
| Hop-by-hop stripping, streaming, path normalization | Envoy, natively (normalization: `merge_slashes` etc. — verify parity with `_normalize_path`, incl. `..` handling) |
| 502-with-detail on unreachable upstream | Envoy 503 semantics — a behaviour change; frontends branch on ok/not-ok so cosmetic, but NAME it |
| Lineage sidecar-only 403 blocklist (`lineage_sidecar_guard`) | **Structural allowlist**: HTTPRoutes route only public paths, so an unrouted sidecar path simply does not exist at the edge (better than a blocklist). The services' app-api-token check remains the load-bearing guard |
| Merged `/docs` + fleet-wide `openapi.json` | Re-home to a tiny endpoint on one service, or retire if unused — decide, don't drop silently |
| `RASK_*_URL` env-overridable upstreams | HTTPRoute `backendRefs` in-cluster; the dev proxy (below) keeps the env overrides locally |

**The blocker that shapes everything: `make dev-micro` must survive.** The no-k8s dev
loop is a hard requirement (fleet as plain processes, one stable `/api` origin).
HTTPRoutes only exist in a cluster. The cure is to **derive,
don't duplicate**: a thin dev-only proxy whose route table is RENDERED from the chart's
HTTPRoutes, plus a contract test proving the two agree. Two hand-kept tables is the
disease this plan exists to avoid.

Also in scope, and each is a decision to make explicitly, not en passant:

- **SSR base URLs**: zones reach `rask-gateway:8888` server-side today
  (`RASK_GATEWAY_URL` / `LANCE_GATEWAY_URL` — note the two-env-var split documented in
  `rask-frontend`). Post-dissolution they point at services directly (the admin plane
  already does: `CATALOG_API`) or at the Gateway's in-cluster address. Unifying the env
  split is in scope for this phase.
- **Observability**: the gateway emits OTLP spans today; edge-level telemetry replaces
  them (kgateway/Envoy access logs + metrics into the Vector→Greptime pipe). RED
  dashboards must not go dark.
- **The zones' dev-proxy split** (`VIEWER_BACKEND` :8888 vs `LANCE_BACKEND` :8001 —
  `rask-services-fleet` gotcha): the derived dev proxy is the moment to kill this split.

**Exit criteria for Phase 2:** `services/gateway` deleted from `services/`, `.docker/`,
the chart, and `dev-micro.sh`; every zone's `/api/*` path works through the edge
in-cluster AND through the derived proxy in `make dev-micro`; the parity contract test
exists; merged-docs decision recorded; `docs/architecture/system-overview.md` +
`deployment.md` + the `rask-services-fleet` skill updated in the same commits.

---

Delete this file when Phase 2 lands (plan docs are working documents; `docs/` carries
only settled architecture).


## Carried from `open_lakehouse_diff_left.md` (2026-09-05)

Two rows from that register's section D are edge concerns, not lakehouse ones, and they belong to
whoever owns the edge. Both are unaffected by which proxy serves it — a body cap and a blocklist are
required of nginx, kgateway and the Python gateway alike — so they are stated here rather than folded
into either phase above.

### D2 · Sidecar-only blocklist is a partial hand-list
**What.** Only `lineage-events` and `lineage-reconcile-cron` are blocked; the root rewrites expose
`/api/lineage/lineage-dlq`, `/api/catalog/control-events`, both `/dapr/subscribe`, `/ui/*`, `/demo/*`;
with `APP_API_TOKEN` unset the route guards no-op. **Closes it.** Invert to an allowlist per
root-rewritten row (catalog `/v1/*`; lineage `/runs`, `/events`, `/v1/*`).

### D3 · No body cap, rate limit, request-id, forwarded-for, access log, or coded errors at the edge
**Status 2026-09-02.** Half stale: the gateway now mounts `RequestIDMiddleware`
(`gateway/__init__.py:513`) so one id reaches every hop. It still runs neither `register_middleware`
nor a body cap, rate limit, access line or coded 404/502 — those stand.
**Where.** `gateway/__init__.py:280,332,341,347`. **Closes it.** Streaming body-size middleware,
token bucket per subject/IP, `RequestIDMiddleware`, strip inbound `X-Forwarded-*` and inject at the edge,
one structured access line per request, problem+json with `code` for 404/413/429/502.

