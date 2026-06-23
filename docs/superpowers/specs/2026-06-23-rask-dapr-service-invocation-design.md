# Dapr in rask — sidecars + service invocation (design)

Date: 2026-06-23
Status: Approved (brainstorm) — ready for implementation plan
Scope: Phase 1 only. Orchestrator pub/sub over NATS is explicitly a follow-up spec.

## Context

Dapr's control plane is installed in the cluster (umbrella subchart, `dapr.enabled`),
but no rask service uses it. The custom backend fleet is six FastAPI/uvicorn
services composed from `service-kit`'s `make_service_app`:

| service | app-id | port | module |
| --- | --- | --- | --- |
| core-api | core-api | 8801 | `core_api:app` |
| search-api | search-api | 8802 | `search_api:app` |
| volumes-api | volumes-api | 8803 | `volumes_api:app` |
| ray-api | ray-api | 8804 | `ray_api:app` |
| orchestrator | orchestrator | 8810 | `orchestrator:app` |
| gateway | gateway | 8888 | `gateway:app` |

Today the **gateway** is an `httpx` reverse-proxy: it longest-prefix-routes
`/api/*` to the backends using env-var upstreams (`RASK_CORE_API_URL`,
`RASK_SEARCH_API_URL`, `RASK_VOLUMES_API_URL`, `RASK_RAY_API_URL`,
`RASK_ORCH_API_URL`) — see `components/services/gateway/src/gateway/__init__.py`.
No other intra-fleet HTTP calls exist (orchestrator talks to Ray via the Ray
SDK + Ray dashboard HTTP, not to rask services). All persistent state is
domain-owned (Postgres, MinIO/S3, LanceDB).

## Goals

- Dapr sidecars injected on all six backend services; mTLS between sidecars (Dapr default).
- A shared `DaprClient` available to every service (on `app.state`, via the service-kit lifespan) so any service *can* invoke another by app-id.
- The gateway routes to backends via **Dapr service invocation** instead of direct httpx env-URLs.
- A clean off-switch so non-Dapr runs (tests, `make viewer`, a Dapr-less prod) keep working unchanged.

## Non-goals (out of scope, future specs)

- Orchestrator pub/sub over NATS JetStream (replacing the polling loop). Deferred.
- Dapr state store, bindings, secrets, configuration building blocks — overkill: state is domain-owned, Ray stays on its SDK, secrets are bootstrap-time.
- Dapr on the SvelteKit SSR frontends — they are edge apps that reach the gateway; they stay as-is.
- OpenTelemetry / OpenObserve — its own spec, brainstormed next.

## Global constraints

- Dapr Python SDK: `dapr` (pin the latest stable at implementation time; record the pin in the plan).
- Dapr sidecar default ports: HTTP `3500`, gRPC `50001` (read from `DAPR_HTTP_PORT`/`DAPR_GRPC_PORT` env, which the injector sets).
- app-id == the service key in `values.yaml` `services` map (core-api, search-api, volumes-api, ray-api, orchestrator, gateway).
- Feature flag: `RASK_DAPR_ENABLED` (bool, default false in `service-kit` `Settings`; the chart sets it true via the ConfigMap). When false: no DaprClient is built, and the gateway uses the existing httpx env-URL path.
- API prefix stays `/api`; gateway longest-prefix routing semantics unchanged.
- No Claude co-author trailer on commits/PRs.

## Design

### 1. Sidecar injection (chart)

`chart/templates/fleet.yaml` adds pod-template annotations, driven by a new
`dapr` block in `values.yaml`:

```yaml
dapr:
  sidecars: true        # inject sidecars on the fleet (off => plain pods)
  logLevel: "info"
```

Per ranged service, when `.Values.dapr.sidecars`:

```yaml
template:
  metadata:
    annotations:
      dapr.io/enabled: "true"
      dapr.io/app-id: "{{ $name }}"
      dapr.io/app-port: "{{ $svc.port | quote }}"
      dapr.io/log-level: {{ $root.Values.dapr.logLevel | quote }}
```

`dapr.sidecars` is independent of the existing `dapr.enabled` (control-plane
subchart toggle): you need the control plane on to inject. The orchestrator is a
singleton (Recreate) — sidecar injection does not change that.

### 2. Shared DaprClient (service-kit)

`packages/service-kit`:

- `config.py` `Settings`: add `dapr_enabled: bool` (alias `RASK_DAPR_ENABLED`, default false) and optional `dapr_http_port`/`dapr_grpc_port` (aliases `DAPR_HTTP_PORT`/`DAPR_GRPC_PORT`).
- New `build_dapr_client(settings)` factory returning a `dapr.clients.DaprClient` (or `None` when `dapr_enabled` is false). Import `dapr` lazily inside the factory so the dependency isn't required for non-Dapr runs.
- The injectable lifespan puts it on `app.state.dapr` next to `app.state.http`. Services that use the shared lifespan (`core.lifespan.make_lifespan`, the default lifespan) get it; provide a `DaprClientDep` (`Annotated[DaprClient | None, Depends(get_dapr)]`) mirroring the existing `HttpDep` pattern for future call sites.
- Cleanup: close the client on lifespan shutdown if it holds connections.

This is plumbing — no service *must* call another yet (none do today except the gateway), but the client is uniformly available.

### 3. Gateway → Dapr service invocation

`components/services/gateway/src/gateway/__init__.py`:

- Keep the longest-prefix path→app-id table (search→search-api, volumes→volumes-api, ray→ray-api, orchestrator→orchestrator, else core-api).
- When `RASK_DAPR_ENABLED`: build the upstream as the local Dapr sidecar invoke URL — `http://127.0.0.1:{DAPR_HTTP_PORT}/v1.0/invoke/{app-id}/method/{path}` — preserving method, query, headers, body, and streaming. The gateway's own sidecar proxies to the target app-id's sidecar (mTLS, discovery handled by Dapr).
- When disabled: unchanged httpx env-URL path (current behaviour) — so non-Dapr deploys and local `make dev-micro` keep working.
- The OpenAPI-merge fetch (gateway pulls each backend's `/openapi.json`) goes through the same invoke path when enabled.
- Keep the existing `httpx.AsyncClient` (still used as the HTTP transport to the sidecar, and for the disabled path).

### 4. Dependencies & images

- Add `dapr` (Python SDK) to the workspace (`pyproject.toml` + the service projects that need it — at minimum `service-kit` and `gateway`; the SDK is light).
- Rebuild + reimport the six backend images (`make k3s-build`/`k3s-import` cover them).

### 5. Chart config

- `values.yaml` `config`: add `RASK_DAPR_ENABLED: "true"` so the ConfigMap exposes it to every service.
- `values.yaml`: add the `dapr` (sidecars) block from §1.

## Data flow (after)

```
frontend SPA / SSR ──HTTP──> gateway (:8888) [+ dapr sidecar]
                                   │  /api/* longest-prefix → app-id
                                   ▼
                       gateway sidecar :3500  ──mTLS──> target sidecar ──> target app (:88xx)
                                   (Dapr service invocation; no RASK_*_URL)
```

## Error handling

- Dapr sidecar unreachable (e.g., injection failed): invoke returns 500/connection error; the gateway surfaces it as a 502 (same shape as an httpx upstream failure today). Readiness of the app is unaffected (the sidecar is a separate container).
- `RASK_DAPR_ENABLED=false` with no sidecars: gateway uses env-URLs; identical to current behaviour.
- `build_dapr_client` import error when the SDK is absent but the flag is on: fail fast at startup with a clear message.

## Testing

- **service-kit unit:** `build_dapr_client` returns `None` when `RASK_DAPR_ENABLED` is false and a client when true (SDK import mocked); lifespan sets/clears `app.state.dapr`.
- **gateway unit:** with the flag on, each path prefix produces the correct `…/v1.0/invoke/<app-id>/method/<path>` URL (mock transport, assert request URL/method/body); with the flag off, it builds the env-URL upstream. Pristine output.
- **live verify (k3s):** pods show `dapr.io/sidecar-injected=true` and run 2/2 (app + daprd); `GET /api/health` and `GET /api/batches/` return 200 through the gateway via invoke; toggling `RASK_DAPR_ENABLED=false` + redeploy still serves (fallback path).

## Files to touch

- `chart/templates/fleet.yaml` — sidecar annotations (ranged).
- `chart/values.yaml` — `dapr.sidecars`/`logLevel` block + `config.RASK_DAPR_ENABLED`.
- `packages/service-kit/src/service_kit/config.py` — `dapr_enabled` + port settings.
- `packages/service-kit/src/service_kit/__init__.py` — `build_dapr_client`, lifespan wiring, `DaprClientDep`.
- `components/services/gateway/src/gateway/__init__.py` — invoke-based routing + fallback.
- `pyproject.toml` (+ relevant `projects/*/pyproject.toml`) — `dapr` dependency.
- Tests under `packages/service-kit/tests/` and `components/services/gateway/tests/`.

## Risks / notes

- Sidecar adds a container per pod (startup latency, memory). Acceptable for a dev/k3s cluster; the `dapr.sidecars` toggle disables it.
- Dapr invoke adds a hop (gateway→its sidecar→target sidecar→app). Negligible locally; the fallback path exists if needed.
- mTLS is on by Dapr default; no rask change required.
- This is the foundation the deferred orchestrator pub/sub spec builds on (same sidecars + NATS component).
