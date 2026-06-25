# Rust observability stack (Vector + GreptimeDB + Perses) + OTLP app instrumentation

Date: 2026-06-25
Status: Approved (design) — pending implementation plan
Branch: `feat/observability-stack`
Inspired by: [The Rust Renaissance in Observability (ITNEXT, Greptime, Nov 2025)](https://itnext.io/the-rust-renaissance-in-observability-lessons-from-building-at-scale-cf12cbb96ebf)

## Goal

Add an in-cluster, Rust-centric observability stack to the rask Helm chart and
instrument the application so the HTR platform emits and stores metrics, logs,
and traces:

- **Collection**: Vector (node log shipper).
- **Storage**: GreptimeDB (unified metrics + logs + traces), object-storage backed.
- **Dashboards**: Perses (dashboards-as-code).
- **App instrumentation**: OpenTelemetry on the FastAPI fleet + Ray workers,
  exporting OTLP directly to GreptimeDB.

The stack is gated (like cnpg/rustfs/kuberay) so it can be turned off, and the
app instrumentation is a no-op unless an OTLP endpoint is configured.

## Background & key findings

rask has **no observability today** — `service_kit/middleware.py` explicitly
defers logging "until structured logging / OTel lands," and there are no OTel
deps. `service_kit.make_service_app` (`packages/service-kit/src/service_kit/__init__.py:89`)
is the single app factory every fleet service uses — the one hook point for
instrumentation.

Two findings from grounding the article's stack against current tool support
**change the article's recommendation** and are baked into this design:

1. **OTel-Arrow is not implementable here.** The Python OpenTelemetry SDK cannot
   emit OTel-Arrow, and Vector has **no** OTel-Arrow support (it is currently a
   Go collector↔collector optimization only). The app→store hop therefore uses
   **standard OTLP/gRPC**. OTel-Arrow is dropped with a documented note.
2. **Vector has no GreptimeDB *traces* sink** (only `greptimedb_metrics` and
   `greptimedb_logs`). So app traces cannot flow app→Vector→GreptimeDB.
   Resolution: **app signals (traces+metrics+logs) export OTLP-direct to
   GreptimeDB**; Vector is the node/pod **log shipper** only.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Stack | Vector (logs) + GreptimeDB (store) + Perses (dashboards) |
| Wire protocol | Standard **OTLP/gRPC** (OTel-Arrow dropped — unsupported) |
| App telemetry routing | Apps export OTLP-direct to GreptimeDB; Vector ships pod logs only |
| GreptimeDB storage | **RustFS S3** (`rask-rustfs-io:9000`, bucket `rask-observability`) |
| App instrumentation scope | FastAPI fleet **and** Ray workers |
| Backend packaging | Gated remote subchart deps, master `observability.enabled` |
| Instrumentation style | Programmatic in `make_service_app` (off unless OTLP endpoint set) |

## Architecture & data flow

```
FastAPI fleet (6 svcs) ─OTLP/gRPC :4001─┐
Ray workers (htrflow/runner) ───────────┼──▶ GreptimeDB standalone ──S3──▶ RustFS
                                        │     :4001 gRPC / :4000 HTTP       rask-rustfs-io:9000
k8s pod/container logs ─▶ Vector Agent ─┘     traces+metrics+logs           bucket rask-observability
                         (kubernetes_logs → greptimedb_logs sink :4000)
                                              ▲
                          Perses ──Prometheus datasource :4000/v1/prometheus─┘
```

## Component versions (pinned)

| Component | Helm repo | Chart | Version |
|---|---|---|---|
| GreptimeDB | `https://greptimeteam.github.io/helm-charts/` | `greptimedb-standalone` | 0.4.5 (app 1.1.1) |
| Vector | `https://helm.vector.dev` | `vector` | 0.56.0 |
| Perses | `https://perses.github.io/helm-charts` | `perses` | 0.22.0 |

## Design

### 1. Chart dependencies (`chart/Chart.yaml`)

Add three remote subchart deps, each `condition: observability.enabled`
(mirrors `kuberay`/`nats`/`dapr`). Add the three repos to `Makefile`
`K3S_DEP_REPOS`; `helm dependency update` regenerates `Chart.lock`.

### 2. GreptimeDB (standalone) on RustFS S3

Subchart value overrides (under the `greptimedb-standalone` key):

- `objectStorage`: `s3` with `endpoint: http://rask-rustfs-io:9000`,
  `bucket: rask-observability`, `region: us-east-1`,
  `root: /greptimedb`, `enableVirtualHostStyle: false` (path-style for RustFS),
  and `credentials.accessKeyId` / `credentials.secretAccessKey` sourced from the
  RustFS keys (`secrets.minioAccessKey` default `raskadmin` / `secrets.minioSecretKey`).
- Storage class for the (small) local WAL/cache PVC: `local-path`.
- Services: `4000` (HTTP: OTLP, Prometheus query/write, SQL) and `4001` (gRPC: OTLP, Vector metrics sink).

**Bucket provisioning**: add `rask-observability` to the RustFS Tenant's
`spec.buckets` (`chart/templates/rustfs-tenant.yaml`, via `rustfs.buckets`), so
the operator creates it. GreptimeDB tolerates a brief RustFS-not-ready window via
its local WAL + retries; an init-wait on `rask-rustfs-io:9000` is added if needed.

### 3. Vector (Agent DaemonSet — pod log shipper)

Subchart values (under the `vector` key):

- `role: Agent` (DaemonSet; 1 pod on the single node).
- `customConfig`: a `kubernetes_logs` source → `greptimedb_logs` sink with
  `endpoint: http://<greptimedb-svc>:4000`, `dbname: public`,
  `table: rask_logs`, `compression: gzip`.
- No OTLP source (apps go direct to GreptimeDB).

### 4. Perses (dashboards-as-code)

- Subchart deploys Perses; a `GlobalDatasource` (Prometheus plugin,
  `directUrl: http://<greptimedb-svc>:4000/v1/prometheus`) + a starter
  `Dashboard` are provisioned via a ConfigMap (Perses sidecar/provisioning).
- New template `chart/templates/perses-provisioning.yaml` holds the datasource +
  starter dashboard ConfigMap.

### 5. App instrumentation — FastAPI fleet (`packages/service-kit`)

- Add deps: `opentelemetry-distro`, `opentelemetry-exporter-otlp`,
  `opentelemetry-instrumentation-fastapi`, `-httpx`, `-sqlalchemy`, `-logging`.
- New module `service_kit/otel.py`: `setup_otel(app, service_name, settings)` that,
  **only when `settings.otel_enabled` (or `OTEL_EXPORTER_OTLP_ENDPOINT` is set)**,
  builds Tracer/Meter/Logger providers with OTLP/gRPC exporters, a `Resource`
  with `service.name`, and calls `FastAPIInstrumentor.instrument_app(app)` +
  httpx/sqlalchemy/logging instrumentation. No-op otherwise (dev/tests unaffected).
- `make_service_app` calls `setup_otel(...)` after building the app.
- `config.py`: add `otel_enabled: bool = False` and OTLP settings (the standard
  `OTEL_*` env vars are honored by the SDK directly; `RASK_OTEL_ENABLED` gates it).

### 6. App instrumentation — Ray workers

- Initialize the same OTel setup in the Ray Serve app (`runner.htrflow_service`)
  and the runner entrypoint, reading `OTEL_*` from the environment.
- The RayService `runtime_env.env_vars` (in `chart/templates/rayservice.yaml`)
  carries `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME=ray-htrflow`, etc.,
  gated by `observability.enabled`.

### 7. Chart wiring

- `chart/templates/fleet.yaml`: when `observability.enabled`, inject per-service
  `OTEL_EXPORTER_OTLP_ENDPOINT` (→ `http://<greptimedb-svc>:4001`),
  `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, `OTEL_SERVICE_NAME=<component>`,
  `OTEL_EXPORTER_OTLP_HEADERS=x-greptime-db-name=public`,
  `OTEL_RESOURCE_ATTRIBUTES`, and `RASK_OTEL_ENABLED=true`.
- `chart/values.yaml`: `observability:` block (`enabled`, `greptimedbService`,
  `bucket`, sub-toggles if needed) + subchart override keys
  (`greptimedb-standalone:`, `vector:`, `perses:`).

### 8. Files

- **Chart**: `Chart.yaml`, `Chart.lock`, `values.yaml`,
  `templates/perses-provisioning.yaml` (new), `templates/fleet.yaml`,
  `templates/rayservice.yaml`, `templates/rustfs-tenant.yaml` (bucket),
  `templates/secrets.yaml` (GreptimeDB S3 creds if not reused),
  `templates/NOTES.txt`, `Makefile`, `chart/README.md`, `docs/architecture/deployment.md`.
- **App**: `packages/service-kit/src/service_kit/otel.py` (new),
  `service_kit/__init__.py`, `service_kit/config.py`,
  `packages/service-kit/pyproject.toml`; Ray serve/runner entrypoints
  (`components/apps/runner/...`), their `pyproject.toml` deps.

## Verification

- `helm lint` + `helm template` render clean with `observability.enabled` **on**
  and **off**.
- App: a unit test that `setup_otel` is a no-op when disabled and wires providers
  when an endpoint is set (no network).
- Live (greenfield k3s): the `rask-observability` bucket is created on RustFS;
  GreptimeDB writes objects there; Vector ships pod logs (queryable in
  GreptimeDB `rask_logs`); a fleet HTTP request produces a trace in GreptimeDB
  (queryable via SQL/Prometheus API); Perses shows the GreptimeDB datasource and
  a populated dashboard panel.

## Risks / to verify at implementation

- **OTel-Arrow dropped** — standard OTLP/gRPC instead (Python SDK + Vector lack
  OTAP). Documented; revisit if a Rust/Python OTAP SDK ships.
- **RustFS coupling** — GreptimeDB object storage depends on RustFS health
  (accepted; WAL buffers briefly). Confirm GreptimeDB's S3 `enableVirtualHostStyle:
  false` works against RustFS path-style and that the `rask-observability` bucket
  exists before GreptimeDB needs it.
- **GreptimeDB OTLP traces ingestion** — confirm the OTLP gRPC (`:4001`) path and
  the `x-greptime-db-name` header land traces correctly; adjust HTTP vs gRPC if needed.
- **Ray instrumentation** is the most involved piece (actor/Serve process
  boundaries, propagation across the runtime_env) — likely its own task, and may
  be reduced to Serve-app-level spans first.
- **Perses provisioning** — confirm the chart's datasource/dashboard
  provisioning mechanism (sidecar ConfigMap vs perses-operator) for 0.22.0.
- **Two-layer scope** — backend (chart) and app (instrumentation) are
  independently testable; the plan sequences backend first, then fleet, then Ray.
