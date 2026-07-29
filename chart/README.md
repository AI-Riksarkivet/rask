# rask Helm chart

Deploys the full rask fleet to Kubernetes — the **single deploy artifact** for
both local k3s and production. In-cluster CloudNativePG (Postgres), RustFS
(object store), and KubeRay are optional: gate them with `*.enabled` toggles.

## Fleet

| Component | Port | Notes |
|---|---|---|
| `gateway` | 8888 | Reverse proxy; path-routes `/api/*` to per-domain services |
| `compute` | 8804 | Ray dashboard proxy + `/api/serve/*` (R22 — `compute` on every surface; public paths stay `/api/ray` + `/api/serve`) |
| `frontend` | 3000 | SvelteKit SSR (svelte-adapter-bun) |
| migration | — | pre-install/pre-upgrade Job: `alembic upgrade head` |
| Ingress (Traefik) | 80 | `/api` → gateway:8888, `/` → rask-home:3000 |

## In-cluster dependencies (optional)

Each toggle gates **both** the operator subchart and the custom resource it manages.

| Toggle | Operator | What it provisions | Service |
|---|---|---|---|
| `cnpg.enabled=true` | CloudNativePG (`cloudnative-pg` 0.28.3) | `Cluster` named `rask-postgres` (instances, storage, image all under `cnpg.*`) | `rask-postgres-rw:5432` |
| `rustfs.enabled=true` | RustFS operator (vendored at `third_party/rustfs-operator/`, refreshed via `scripts/vendor-rustfs-operator.sh`) | `Tenant` named `rask-rustfs` — 1 pod / 4 PVCs (erasure-coding minimum); buckets provisioned natively via `spec.buckets` | `rask-rustfs-io:9000` (S3), `rask-rustfs-console:9001` (console) |
| `ray.enabled=true` | — | KubeRay `RayService` (head + GPU worker) | — |

Set all three to `true` for local k3s. Leave them `false` for production and
supply credentials via `existingSecret`.

## Observability (optional)

Toggle: `observability.enabled` (default `false`). When enabled, two subcharts are
installed, the first-party OTel Collector renders, and all fleet + Ray OTLP wiring is
activated. The Collector is the **single log shipper** (Vector retired, owner ruling
2026-07-27): its filelog receiver tails infra-pod logs into `opentelemetry_logs`.

| Component | Version | Service | Notes |
|---|---|---|---|
| OTel Collector (first-party template, `templates/otel-collector.yaml`) | contrib image | `rask-otel-collector` | Telemetry hub: receives app OTLP, tails infra-pod logs (filelog → table `opentelemetry_logs`), scrapes Dapr sidecars; exports OTLP → GreptimeDB `:4000/v1/otlp` |
| GreptimeDB (`greptimedb-standalone` 0.4.5, app 1.1.1) | `rask-greptimedb-standalone` | Unified metrics/logs/traces store; `:4000` HTTP (OTLP at `/v1/otlp`, Prometheus query/write, SQL), `:4001` gRPC |
| Perses (`perses` 0.22.0) | `rask-perses:8080` | Dashboard UI; a GreptimeDB Prometheus `GlobalDatasource` pointing at `http://rask-greptimedb-standalone:4000/v1/prometheus` is pre-configured |

**Storage:** GreptimeDB persists to the in-cluster RustFS S3 (`rask-rustfs-io:9000`,
bucket `rask-observability`). The bucket is auto-provisioned by the RustFS Tenant's
`spec.buckets` — no manual setup required.

**App instrumentation:** the FastAPI fleet (via `service_kit.setup_otel` — called
automatically from `make_service_app`, and directly by the gateway proxy app) and the
Ray Serve htrflow app export OTLP/HTTP **traces and RED metrics** (the FastAPI/HTTPX
instrumentation emits `http.server.*`/`http.client.*` count/duration/active-request
metrics with no per-endpoint code) **directly to GreptimeDB `:4000/v1/otlp`** (`OTEL_*`
env vars injected by the chart when `observability.enabled=true`). Headers split by
signal: traces carry `x-greptime-pipeline-name=greptime_trace_v1` (required for trace
ingestion), metrics use db-name only. Traces land in `opentelemetry_traces`; metrics
become PromQL series. Gateway spans root each distributed trace. The chart also
provisions a Perses **"Fleet — RED"** dashboard (rate / 5xx errors / p95 latency /
in-flight, per service). Instrumentation is opt-in — no-op unless the env vars are
present. Standard OTLP is used throughout (OTel-Arrow is not used — the Python SDK
lacks OTAP support).

## Local k3s quickstart

```bash
make k3s-install      # one-time: k3s + helm + NVIDIA device-plugin + KubeRay (sudo)
make k3s-build        # build fleet + frontend + ray images as :dev
make k3s-import       # side-load images into k3s (no registry needed)
make k3s-up           # helm upgrade --install rask ./chart --wait
# UI: http://rask.local/   API: http://rask.local/api/health
# (add "127.0.0.1 rask.local" to /etc/hosts)
make k3s-down         # uninstall   |   make k3s-purge  # + delete PVCs
```

## Production install

```bash
helm upgrade --install rask chart/ \
  --set existingSecret=rask-app \
  --set config.RAY_DASHBOARD_URL=http://<ray-head>:8265 \
  --set ingress.host=rask.example.org \
  --set cnpg.enabled=false \
  --set rustfs.enabled=false \
  --set ray.enabled=false
```

## Config and secrets

Sensitive config (database URL, S3 credentials, HF token) comes from an
operator-created Secret. The default expected name is `rask-app`; override with
`existingSecret=<name>`.

Non-sensitive config (service URLs, feature flags, orchestrator settings) flows
from `values.yaml` into a ConfigMap mounted by each Deployment.

## Critical constraints

- **`orchestrator` must be a singleton** (`replicas: 1`, `strategy: Recreate`)
  — the in-process reconcile loop must run in exactly one pod.
- The orchestrator loop starts only when `config.RASK_ORCHESTRATOR_AUTOSTART=true`
  or an operator calls `POST /api/v1/orchestrator/start`.

See `docs/architecture/deployment.md` for the full topology.
