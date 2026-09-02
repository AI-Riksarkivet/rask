Scope note: per the owner's mid-task instruction, `services/flows` is excluded; the material I had gathered on it is discarded. Everything below is from reading the code (read-only), not from a live cluster; where a claim rests on a runtime behaviour I could not observe, it is marked as such.

# Audit: controlplane, compute, gateway

Shared facts used throughout:
- Deployed prefix is `RASK_API_PREFIX=/api` (chart configmap; `scripts/dev-micro.sh:26`), so `{prefix}/…` rows below read as `/api/…`.
- Edge: the Ingress sends `/api` → gateway (`chart/templates/ingress.yaml:66-72`); the `front-door` NetworkPolicy admits gateway ingress from anywhere (`chart/templates/network-policy.yaml:251-275`); every other non-store pod accepts ingress from any pod in the namespace (`network-policy.yaml:95-112`, rule 3a) — so compute and controlplane are reachable directly by any in-namespace pod and publicly only via the gateway.

## 1. How each touches the lakehouse

**controlplane** — none. No Lance, catalog, lineage, or events. It reads the Kubernetes API per request: cluster-wide `list_cluster_custom_object(platform.rask.io/v1alpha1, projects)` and per-namespace `list_namespaced_ingress(label_selector="platform.rask.io/project")` (`services/controlplane/src/controlplane/k8s.py:35,40`). Credentials: the pod ServiceAccount via `load_incluster_config()`, kubeconfig fallback (`k8s.py:27-30`); ClusterRole grants get/list/watch on `projects` and `ingresses` cluster-wide (`chart/templates/controlplane.yaml:87-98`).

**compute** — none. No Lance/catalog/lineage. Its only upstream is the Ray dashboard at `settings.ray_dashboard_url` (`packages/service-kit/src/service_kit/config.py:35`), via the Ray Job SDK client (`packages/ray-kit/src/ray_kit/dashboard.py:146-179`) plus a raw httpx client. Credentials: an optional Ray bearer from `RASK_RAY_AUTH_TOKEN`/`RAY_AUTH_TOKEN` (`ray_kit/auth.py:28-45`), attached as httpx default headers (`services/compute/src/compute/lifespan.py:28`) and SDK headers (`dashboard.py:160`); rendered in-cluster only when `rayClient: true` (`chart/values.yaml:236`). `ray_kit.submit.lineage_env()` forwards `RASK_LINEAGE_*` into a job's runtime_env (`ray_kit/submit.py:129-135`) but compute never calls it — the only `ray_kit.submit` consumer is the medallion (`services/medallion/src/medallion/services/ray_submit.py:30,205`).

**gateway** — none directly; it forwards to the catalog/lineage/medallion/explorer rows. It holds no credential of its own: inbound `Authorization` is forwarded verbatim (`services/gateway/src/gateway/__init__.py:340`, pinned by `tests/test_spoofable_headers.py:83-102`). With `RASK_DAPR_ENABLED=true` it invokes upstreams through its own sidecar (`__init__.py:222-232`), which stamps `dapr-api-token` and `dapr-caller-app-id: gateway` on every proxied request — the estate's doors must therefore treat "gateway" as public (`service_kit/governed/dapr_auth.py:28-67`).

## 2. Authorization

**controlplane** — endpoints: `GET /api/health` (`health.py:16`), `GET /api/projects/` (`routes.py:36-44`). No OIDC, no OpenFGA, no dependency of any kind; the chart does not mark it `governedAuth`. Any anonymous caller through `/api/projects` receives every Project CR's name, team, workload type, phase, namespace and ingress host (`service.py:24-37`). Minor: `RASK_PROJECT_URL_SCHEME` is read from `os.environ` per request (`routes.py:38`), contrary to the settings rule in `config.py:3-4`.

**compute** — endpoints: `GET /api/health`; `GET /api/ray/{health,jobs,jobs/{id}/logs,cluster,actors,tasks,overview,logs}` (`routes.py:24-67`); `GET|HEAD /api/serve` and `/api/serve/{path:path}` (`proxy.py:22,58-59`); `POST|OPTIONS /compute-prune-jobs-cron` (`pruner.py:67-68`). No OIDC/FGA on any of them; the chart comment states "the gateway and compute read no auth settings at all" (`chart/templates/fleet.yaml`, user-door block).
- Submit/kill Ray jobs? Not through compute: `/api/ray` is GET-only; `/api/serve` forwards only GET/HEAD (`proxy.py:22`), so Serve's `PUT /api/serve/applications/` (deploy = code execution) and `POST /api/jobs/` are unreachable via the proxy. `ray_kit.submit.submit_or_reattach`/`delete` exist as library code (`submit.py:174-200`) but compute exposes no route calling them.
- Reach the dashboard beyond Serve status? Through the gateway, no: `_normalize_path` collapses `..` before route matching (`gateway/__init__.py:191-208, 329-338`). Directly on :8804 (any in-namespace pod), `proxy.py:53` builds `api/serve/{path}` and `dashboard.py:674` concatenates it onto the dashboard URL with no dot-segment check; Starlette hands `../v0/logs/file` through `{path:path}` and httpx normalises the URL, so any GET on the dashboard (job list, node log files) is reachable. Untested and unobserved live — flagged as a code-reading finding.
- The prune binding route is guarded only by `require_dapr_token` (`pruner.py:67`), which is a no-op when `APP_API_TOKEN` is unset and treats an absent `dapr-caller-app-id` as non-public (`dapr_auth.py:65-66, 98-102`). Compute never calls `assert_app_token_configured` (no hits in `services/compute`), unlike notifications/catalog/lineage. In-cluster the token arrives from the injector via `dapr.io/app-token-secret` (`_helpers.tpl:210`, `dapr-app-token.yaml:2`); in dev, in a sidecar-less install, or in the silent-admit scenario `_helpers.tpl:170-185` documents, the route is open to any in-namespace pod and deletes terminal job rows beyond the keep window (`ray_kit/prune.py`, keep 500/100 failed, `pruner.py:32,38`). The gateway has no row for it, so it is not public.

**gateway** — enforces nothing: no authn, no authz, no rate limit, no body cap. The single middleware is `lineage_sidecar_guard` (`__init__.py:288-300`), a 403 blocklist over `/api/lineage/{lineage-events,lineage-reconcile-cron}` (env `RASK_LINEAGE_SIDECAR_ONLY_ROUTES`, `__init__.py:211-219`). It does strip client-supplied trust headers on every route — `dapr-caller-app-id`, `dapr-api-token`, `dapr-app-id`, `x-lance-service-identity` (`__init__.py:66-77, 340`), case-insensitively; pinned by `tests/test_spoofable_headers.py:51-80`. Every upstream row is reachable ungated through `/api/*`; whether a request is refused depends entirely on the upstream's own door.

## 3. Lineage / events

- **controlplane**: emits nothing, consumes nothing.
- **compute**: emits no OpenLineage and no control events (no `control_emit`/`openlineage` import). Consumes one Dapr input binding, the `compute-prune-jobs-cron` cron Component scoped to app-id `compute` (`chart/templates/compute-prune-cron.yaml:13,19-21`). Emits OTel metrics only (`ray.control.probes`, `ray.control.jobs_known`, via `ray_kit/metrics.py`; recorded at `dashboard.py:172,188,193,218,229-230`).
- **gateway**: emits and consumes nothing; it blocks two lineage sidecar routes from the edge (above).

## 4. State and Dapr coupling

- **controlplane**: stateless; one `lru_cache`d k8s reader (`routes.py:25-30`). Zero Dapr usage in code, yet with `dapr.sidecars` on it receives a sidecar and app-token annotation like every fleet pod (`fleet.yaml:43` → `_helpers.tpl:196-210`) — a sidecar that does nothing. Dapr-free already. No caching of the k8s reads: each `/api/projects` call is one cluster-wide list plus one ingress list per project (`service.py:40-46`) — an unauthenticated amplification path against the API server.
- **compute**: no durable state; `app.state.http` and `app.state.ray_client` only (`lifespan.py:24-29`), the SDK client rebuilt lazily (`dependencies.py:15-25`). Dapr coupling = the cron binding alone. Dapr-free replacement: a Kubernetes CronJob calling `prune_jobs`. Single-writer note: a Dapr cron fires in every replica's sidecar, so `replicas>1` would prune concurrently; safe only because a failed delete is counted, not raised (`ray_kit/prune.py`, test `test_a_failing_delete_is_counted_not_raised`), and the chart pins `replicas: 1` (`values.yaml:232`).
- **gateway**: no state; one `httpx.AsyncClient` (30 s connect / 300 s read, `__init__.py:269`). Dapr is optional (`_target_base`, `__init__.py:226-232`) with direct-URL fallback per row; Dapr-free already.

## 5. BYO-engine fit (compute)

What an external Temporal/Flyte worker would need vs. what exists:
- **Job submission with vended credentials** — absent. Compute exposes no submit route; the Ray token is pod env on whoever has `rayClient: true`, never vended or scoped. The only submitter in the estate is `ray_kit.submit.submit_or_reattach` (`submit.py:174-200`), used by the medallion in-process. An external worker today must hold the shared Ray token and talk to the dashboard directly, bypassing compute.
- **Idempotent outcome door** — absent on compute. The idempotency that exists is client-side: deterministic `submission_id(stage, token, work, code)` (`submit.py:56-88`) and reattach-or-resubmit-on-terminal-failure (`submit.py:183-197`) — library behaviour, not an HTTP contract.
- **Plan document on a control lane** — absent. Compute publishes no control or lineage events, so an engine cannot subscribe to "a job for you exists"; nothing carries a plan.
- **Status/outcome reads** — partially present, read-only and unauthenticated: `GET /api/ray/jobs` (newest 200, no id/status filter, `dashboard.py:61,215-281`), `GET /api/ray/jobs/{id}/logs` (`routes.py:34-36`). `job_status`/`job_failure` (`submit.py:91-112,138-162`) are the right single-read shape but are not exposed over HTTP.
Net: compute is an introspection shell; none of the three BYO seams exists on it.

## 6. Gateway route table

Source: `gateway/__init__.py:140-180`; upstream prefix replaces the public one (`__init__.py:338`). Gateway-side auth: none on every row.

| # | Public prefix | Upstream prefix | app-id / fallback | Gateway auth | Upstream door (as verified) |
|---|---|---|---|---|---|
| 1 | `/api/explorer/search` | `/api/search` | search :8102 | none | not audited |
| 2 | `/api/explorer/annotations` | `/api/annotations` | annotator :8103 | none | not audited |
| 3 | `/api/explorer` | `/api` | viewer :8101 | none | not audited |
| 4 | `/api/catalog` | `""` (root) | catalog :2333 | none | router-level `authorize` (`catalog/api/v1/router.py:40`); root routes outside it, see below |
| 5 | `/api/lineage` | `""` (root) | lineage :8000 | none (2-item blocklist) | route-level guards |
| 6 | `/api/produce` | `/produce` | medallion-producer :8002 | none | token door (per CLAUDE.md) |
| 7 | `/api/ingest` | `/api` | ingest :8830 | none | `authorize_ingest` |
| 8 | `/api/train` (deprecated) | `/train` | medallion-producer | none | token door |
| 9 | `/api/promotions` | `/promotions` | medallion-producer | none | not audited |
| 10 | `/api/ray` | `/api/ray` | compute :8804 | none | **none** |
| 11 | `/api/projects` | `/api/projects` | controlplane :8820 | none | **none** |
| 12 | `/api/flows` | `/api/flows` | flows :8840 | none | out of scope |
| 13 | `/api/notifications` | `/api/notifications` | notifications :8850 | none | governedAuth |
| 14 | `/api/serve` | `/api/serve` | compute | none | **none** (GET/HEAD only) |

Also gateway-served: `/api/openapi.json` (merges every upstream's OpenAPI, `__init__.py:243-264, 324-325`), `/api/docs` (`:326-327`), `/healthz` (`:303-314`). Unmatched `/api/*` → 404 (`:331-332`).

**Spec Lance client**: `/api/catalog/v1/namespace/…` → `http://catalog/v1/namespace/…` — correct (`__init__.py:338`; pinned literally by `tests/test_lance_routes.py:57,75-81`). Caveat: the catalog/lineage rewrites are pinned against hand-written literals only; the "checked against the upstream's own openapi" test covers only the three explorer rows (`test_lance_routes.py:123-127,154`). Second caveat: routing and rewriting operate on the decoded `request.url.path` and `_normalize_path` collapses `.`/`..`/`//` (`__init__.py:191-208, 329`), so a Lance identifier carrying a percent-encoded `/` or dot-segment would be re-interpreted; no test sends an encoded id through the catalog row.

**Management/extension routes exposed by the two root rewrites** (rows 4-5 map the whole upstream root):
- catalog: `GET /dapr/subscribe` (`catalog/api/dapr.py:11`), `POST /control-events` (`:84`, guarded by `require_dapr_token`, `:36`), `/livez`, `/readyz` (`service_kit/probes.py:45,51`), `/docs`, `/openapi.json`.
- lineage: `POST /lineage-dlq` (`lineage/api/dapr.py:94`, guarded `:42`), `GET /dapr/subscribe`, `/ui/*` static UI (`lineage/main.py:196-198`), `/demo/*` when `demo_data_enabled` (`:172-173`), probes, docs.
The gateway blocklist names only `lineage-events` and `lineage-reconcile-cron` (`__init__.py:218`); `/api/lineage/lineage-dlq` and `/api/catalog/control-events` pass the edge and rely on `require_dapr_token`. In-cluster that guard refuses caller `gateway` (`dapr_auth.py:93-97`); in direct-httpx mode (`RASK_DAPR_ENABLED=false`, dev/e2e) with `APP_API_TOKEN` unset the guard is a no-op (`dapr_auth.py:98-102`) and a forged CloudEvent lands.

## 7. Governance gaps

- **Read audit at the gateway**: none. The `proxy` handler logs nothing (`__init__.py:317-360`); the `lance.audit` stream (`service_kit/governed/audit.py`) is emitted only by downstream doors. The gateway adds no `X-Forwarded-For`/`Forwarded` and forwards any client-supplied one untouched (not in `_CLIENT_SPOOFABLE`, `:66-77`), so no downstream audit record can carry a trustworthy client address.
- **Request-id**: the gateway builds a bare `FastAPI` (`:280`) and never registers `service_kit.middleware.RequestIDMiddleware` (bound only via `make_service_app`, `service_kit/__init__.py:145`); it mints no id and forwards a client `X-Request-ID` untouched. Upstreams mint their own per hop (`middleware.py:42-47`), so one edge request has N uncorrelated ids. W3C trace propagation exists only when OTel is enabled (`service_kit/otel.py:50-52,106-107`).
- **Body-size**: none at the gateway — `await request.body()` buffers the whole body in memory before forwarding (`:341`). The only cap is the ingress-nginx annotation `proxy-body-size: 64m` (`chart/values.yaml`, ingress block), which the k3s default Traefik ignores (values.yaml comment: "k3s ships Traefik and the annotations are ingress-nginx's").
- **413/429 bodies**: the gateway emits neither status; there is no rate limit anywhere in the gateway or ingress annotations. Its own errors — 404 `no upstream` (`:332`) and 502 `upstream unreachable` (`:347`) — are bare `HTTPException`s rendered as `{"detail": …}`; the gateway registers no problem+json handlers, so no gateway-originated error carries the Lance `code` (or RFC 9457 `type/title/status`) a spec client parses.
- **Enumeration**: `/api/openapi.json` publicly merges every upstream's OpenAPI, fetched through the sidecar when Dapr is on (`:252-263`).
- **Controlplane**: anonymous tenant enumeration (section 2) with no caching, each call fanning out to the API server.
- **Compute**: `GET /api/ray/logs?node_id&filename` and `/jobs/{id}/logs` expose Ray node log files anonymously (`routes.py:34-36,59-67`; `dashboard.py:616-624,636-646`), bounded only by whatever Ray's log API restricts.

## 8. Tests: pinned vs untested

**gateway** (`services/gateway/tests/`) — pinned: trust-header strip incl. casing and the FastAPI first-duplicate behaviour (`test_spoofable_headers.py`); row ordering/rewrites for explorer, catalog, lineage, produce, train (`test_lance_routes.py`); explorer rewrites against upstream OpenAPI (`:154`); lineage blocklist incl. dot-segment normalisation (`test_lineage_guard.py`); no catch-all; Dapr vs direct base; flows/ingest rewrites against OpenAPI; notifications refusal pass-through and 502-vs-404 (`test_notifications_refusal_passthrough.py`); Location rewrite. Untested: catalog/lineage rewrite against the upstream's own OpenAPI; `/api/promotions` row; percent-encoded identifiers; `/api/openapi.json` merge; 404/502 body shape; streaming/body buffering; any request-id behaviour.

**compute** (`services/compute/tests/test_ray.py`) — pinned: health 200; offline `ok=False` for health/jobs/cluster; proxy 502 and trailing-slash restore; lazy client rebuild. ray-kit pins: auth headers and proxy credential strip both directions (`test_auth.py`), jobs cap/newest/metadata projection/token never leaks (`test_dashboard_bounds.py`), prune policy (`test_prune_jobs.py`), submission id determinism (`test_submission_id.py`), job_failure contract, control metrics. Untested: that `/api/serve` refuses POST/PUT/DELETE; the `/compute-prune-jobs-cron` route (token guard, public-caller refusal, OPTIONS ack); `submit_or_reattach` end-to-end (only referenced in docstrings); `/api/ray/{actors,tasks,overview,logs}` routes; dot-segment traversal via the proxy path.

**controlplane** (`tests/test_controlplane.py`) — pinned: DTO mapping and Pending defaults, sort by `created_at`, endpoint happy path, 503 on k8s errors, mapping bugs not masked, ingress-host URL, empty URL without ingress. Untested: `K8sProjectReader` (label selector, config fallback); the `_namespace` `project-<name>` fallback (`service.py:15-21`); RBAC rendering.

Chart invariants touching these: `test_every_dapr_annotated_pod_carries_the_injector_webhook_label` (`tests/unit/test_invariants.py:1351`), `test_every_pod_whose_app_fails_closed_on_the_app_token_is_given_one` (`:1407` — compute is not fail-closed, so it is outside that guard), ingress body/timeout (`:515,1453`).

## 9. Five most consequential findings

1. **HIGH — Two fleet services are fully open to the internet through the gateway.** The gateway enforces no authn/authz on any row (`gateway/__init__.py:317-360`), and both `controlplane` (`routes.py:36-44`: tenant list with namespaces and hosts) and `compute` (`routes.py:24-67`, `proxy.py:58-59`: cluster topology, job entrypoints, actors, node log files) carry no door of their own; the ingress routes `/api` to the gateway and the front-door policy admits from anywhere (`ingress.yaml:66-72`, `network-policy.yaml:251-275`). Fix: mount `make_auth_deps` (OIDC + FGA `reader` on `fga_root_object`) on the `projects` and `ray` routers and the Serve proxy, or add an edge bearer check at the gateway for rows whose upstream has no door.

2. **MEDIUM — The gateway's sidecar-only blocklist is a partial hand-list.** `lineage_sidecar_guard` denies only `lineage-events` and `lineage-reconcile-cron` (`gateway/__init__.py:218,296-299`) while the `""` rewrites expose `/api/lineage/lineage-dlq` (`lineage/api/dapr.py:94`), `/api/catalog/control-events` (`catalog/api/dapr.py:84`), both `/dapr/subscribe`, `/ui/*` and `/demo/*`; in direct-httpx mode with `APP_API_TOKEN` unset the route guards no-op (`dapr_auth.py:98-102`). Fix: invert the guard into an allowlist per root-rewritten row (catalog: `/v1/*`; lineage: `/runs`, `/events`, `/v1/*`) and 403 everything else, or at minimum add `lineage-dlq`, `control-events`, `dapr`, `ui`, `demo`.

3. **MEDIUM — No body cap, no rate limit, and gateway errors lack the Lance `code`.** `await request.body()` buffers unbounded bodies (`gateway/__init__.py:341`); the only cap is an nginx annotation Traefik ignores (`values.yaml` ingress block); 404/502 are bare `HTTPException`s (`:332,347`) rendered `{"detail": …}` and no 413/429 exist. Fix: add a streaming body-size middleware and a token-bucket per subject/IP at the gateway, and register problem+json handlers that render 404/413/429/502 as `{code, type, title, status, detail}`.

4. **MEDIUM — No edge identity: no request-id minting, no `X-Forwarded-For`, no access log.** The gateway is a bare `FastAPI` (`gateway/__init__.py:280`) without `RequestIDMiddleware` (`service_kit/__init__.py:145`), forwards client `X-Request-ID`/`X-Forwarded-*` untouched and logs nothing per request; downstream `audit()` records therefore have no trustworthy client address and no cross-hop correlation id. Fix: strip inbound `X-Forwarded-*`, inject `X-Forwarded-For`/`X-Request-ID` at the edge, and emit one structured access line per proxied request.

5. **MEDIUM-LOW — Compute hosts a destructive sidecar route without failing closed, and its Serve proxy does not bound the forwarded path.** `POST /compute-prune-jobs-cron` deletes terminal Ray jobs (`pruner.py:43-67`) behind `require_dapr_token`, which is inert with `APP_API_TOKEN` unset and treats an absent caller header as non-public (`dapr_auth.py:65-66,98-102`); compute never calls `assert_app_token_configured`, so in dev, a sidecar-less install, or the silent-injector case (`_helpers.tpl:170-185`) any in-namespace pod (`network-policy.yaml:95-112`) can trigger it. Separately, `proxy.py:53` + `dashboard.py:674` concatenate the caller path with no dot-segment check, so a direct :8804 caller can reach any GET on the Ray dashboard (the gateway's `_normalize_path` protects only the public path). Fix: call `assert_app_token_configured(dapr_enabled=settings.dapr_enabled)` in compute's lifespan and reject `..`/empty/absolute segments in `_canonical` before forwarding.