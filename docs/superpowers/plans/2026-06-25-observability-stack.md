# Observability Stack (Vector + GreptimeDB + Perses) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-cluster Rust observability stack (Vector logs → GreptimeDB store on RustFS S3 → Perses dashboards) to the rask Helm chart, and instrument the FastAPI fleet + Ray workers to export OTLP directly to GreptimeDB.

**Architecture:** Three gated remote subcharts (`greptimedb-standalone`, `vector`, `perses`) join the umbrella chart like kuberay/cnpg, all behind a master `observability.enabled`. App services emit OTLP/gRPC straight to GreptimeDB `:4001`; Vector runs as a node Agent shipping pod logs to GreptimeDB `:4000`. Instrumentation lives once in `service_kit.make_service_app` and is a no-op unless an OTLP endpoint is configured.

**Tech Stack:** Helm 3 subcharts (GreptimeDB standalone 0.4.5 / app 1.1.1, Vector 0.56.0, Perses 0.22.0), OpenTelemetry Python SDK (`opentelemetry-distro` + OTLP gRPC exporter + FastAPI/httpx/sqlalchemy/logging instrumentation), GreptimeDB OTLP + Prometheus-compatible APIs, RustFS S3 object storage.

## Global Constraints

- **Engineering principles (CLAUDE.md):** root-cause fixes, no band-aids; verify end-to-end; no silent scope-cuts.
- **No `Co-Authored-By: Claude` trailer** on commits.
- **Python via uv** (3.13), Ruff (line length 160, `ANN` rules), `ty` typecheck (`error-on-warning`). Run tests with `uv run pytest`.
- **OTel-Arrow is NOT used** — standard OTLP/gRPC only (Python SDK + Vector lack OTAP support). Documented.
- **App telemetry routing:** FastAPI fleet + Ray export OTLP-direct to GreptimeDB; Vector ships pod logs only (Vector has no GreptimeDB traces sink).
- **Instrumentation is opt-in:** no-op unless `RASK_OTEL_ENABLED=true` or `OTEL_EXPORTER_OTLP_ENDPOINT` is set — dev/tests must be unaffected.
- **Gating:** all three subcharts use `condition: observability.enabled`; chart wiring (OTLP env, S3 secret, datasource) is gated by `observability.enabled`.
- **Pinned versions/services:** GreptimeDB svc `rask-greptimedb-standalone` (4000 HTTP, 4001 gRPC); Vector svc `rask-vector` (DaemonSet); Perses svc `rask-perses` (8080). GreptimeDB S3 keys: `objectStorage.s3.{bucket,region,root,endpoint}` + `objectStorage.credentials.existingSecretName`; cred secret env keys `GREPTIMEDB_STANDALONE__STORAGE__ACCESS_KEY_ID` / `GREPTIMEDB_STANDALONE__STORAGE__SECRET_ACCESS_KEY`.
- **GreptimeDB bucket:** `rask-observability` on RustFS (`rask-rustfs-io:9000`).

---

## File Structure

- `chart/Chart.yaml` / `Chart.lock` — three new subchart deps.
- `chart/values.yaml` — `observability:` block + subchart override keys (`greptimedb-standalone:`, `vector:`, `perses:`) + `rask-observability` in `rustfs.buckets`.
- `chart/templates/observability-secret.yaml` — **new** GreptimeDB S3 creds Secret.
- `chart/templates/fleet.yaml` — gated OTLP env vars.
- `chart/templates/rayservice.yaml` — gated OTLP env in `runtime_env`.
- `chart/templates/NOTES.txt`, `chart/README.md`, `CLAUDE.md`, `docs/architecture/deployment.md` — docs.
- `Makefile` — three repos in `K3S_DEP_REPOS`.
- `packages/service-kit/src/service_kit/otel.py` — **new** instrumentation module.
- `packages/service-kit/src/service_kit/__init__.py` — call `setup_otel`.
- `packages/service-kit/src/service_kit/config.py` — `otel_enabled` setting.
- `packages/service-kit/pyproject.toml` — OTel deps.
- `packages/service-kit/tests/test_otel.py` — **new** unit tests.
- Ray serve/runner entrypoints (`components/apps/runner/src/runner/...`) + their `pyproject.toml`.

---

## Task 1: Subchart deps + observability values + GreptimeDB S3 wiring

**Files:**
- Modify: `chart/Chart.yaml`, `chart/Chart.lock` (regen), `chart/values.yaml`, `Makefile`
- Create: `chart/templates/observability-secret.yaml`

**Interfaces:**
- Produces: master toggle `observability.enabled`; subchart services `rask-greptimedb-standalone:4000/4001`, `rask-vector`, `rask-perses:8080`; GreptimeDB writes to RustFS bucket `rask-observability`; cred Secret `rask-observability-s3`.

- [ ] **Step 1: Add the three dependencies to `chart/Chart.yaml`**

Append to the `dependencies:` list:

```yaml
  - name: greptimedb-standalone
    version: "0.4.5"
    repository: https://greptimeteam.github.io/helm-charts/
    condition: observability.enabled
  - name: vector
    version: "0.56.0"
    repository: https://helm.vector.dev
    condition: observability.enabled
  - name: perses
    version: "0.22.0"
    repository: https://perses.github.io/helm-charts
    condition: observability.enabled
```

- [ ] **Step 2: Add the repos to `Makefile` `K3S_DEP_REPOS`**

Extend the list (keep backslashes) with:

```make
                greptime=https://greptimeteam.github.io/helm-charts/ \
                vector=https://helm.vector.dev \
                perses=https://perses.github.io/helm-charts
```

- [ ] **Step 3: Add the `observability:` block + subchart overrides to `chart/values.yaml`**

```yaml
# ---- observability stack (Vector + GreptimeDB + Perses) --------------------
# Gated by observability.enabled (the three subcharts' Chart.yaml condition AND
# the OTLP wiring below). App services + Ray export OTLP-direct to GreptimeDB;
# Vector ships pod logs. GreptimeDB stores to the in-cluster RustFS S3.
observability:
  enabled: true
  bucket: "rask-observability"          # RustFS bucket for GreptimeDB object storage

# GreptimeDB standalone subchart — unified metrics/logs/traces store on RustFS S3.
greptimedb-standalone:
  objectStorage:
    s3:
      bucket: "rask-observability"
      region: "us-east-1"
      root: "/greptimedb"
      endpoint: "http://rask-rustfs-io:9000"
    credentials:
      existingSecretName: "rask-observability-s3"

# Vector subchart — Agent DaemonSet shipping pod logs to GreptimeDB (Task 2 fills customConfig).
vector:
  role: "Agent"

# Perses subchart — dashboards-as-code (Task 3 fills the datasource).
perses: {}
```

- [ ] **Step 4: Add the GreptimeDB bucket to the RustFS Tenant buckets**

In `chart/values.yaml`, change the `rustfs.buckets` line to include the observability bucket:

```yaml
  buckets: ["images-batch", "images-batch-alto", "images-batch-search", "rask-observability"]
```

- [ ] **Step 5: Create `chart/templates/observability-secret.yaml`**

```yaml
{{- if .Values.observability.enabled }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "rask.fullname" . }}-observability-s3
  labels:
    {{- include "rask.componentLabels" (list . "observability") | nindent 4 }}
type: Opaque
stringData:
  GREPTIMEDB_STANDALONE__STORAGE__ACCESS_KEY_ID: {{ include "rask.minioAccessKey" . | quote }}
  GREPTIMEDB_STANDALONE__STORAGE__SECRET_ACCESS_KEY: {{ include "rask.minioSecretKey" . | quote }}
{{- end }}
```

Note: `include "rask.fullname" .` is `rask`, so the secret is `rask-observability-s3`, matching `existingSecretName` above.

- [ ] **Step 6: Vendor deps + regenerate the lock**

```bash
helm repo add greptime https://greptimeteam.github.io/helm-charts/ >/dev/null 2>&1 || true
helm repo add vector https://helm.vector.dev >/dev/null 2>&1 || true
helm repo add perses https://perses.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm dependency update ./chart
```
Expected: `chart/charts/` gains `greptimedb-standalone-0.4.5.tgz`, `vector-0.56.0.tgz`, `perses-0.22.0.tgz`; `chart/Chart.lock` lists all three.

- [ ] **Step 7: Verify render (on + off)**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'kind: (StatefulSet|DaemonSet|Deployment)' | grep -iE 'greptimedb|vector|perses' && echo OK-on
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  --show-only templates/observability-secret.yaml | grep -E 'ACCESS_KEY_ID|name: rask-observability-s3' && echo OK-secret
helm template rask ./chart --set observability.enabled=false --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -ciE 'greptimedb-standalone|rask-vector|rask-perses' | grep -q '^0$' && echo OK-off
helm lint ./chart
```
Expected: `OK-on`, `OK-secret`, `OK-off`, lint 0 failed.

- [ ] **Step 8: Commit**

```bash
git add chart/Chart.yaml chart/Chart.lock chart/values.yaml chart/templates/observability-secret.yaml Makefile
git commit -m "feat(obs): add Vector/GreptimeDB/Perses subcharts + GreptimeDB-on-RustFS wiring"
```

---

## Task 2: Vector Agent config (pod logs → GreptimeDB)

**Files:**
- Modify: `chart/values.yaml` (the `vector:` block)

**Interfaces:**
- Consumes: `rask-greptimedb-standalone:4000` (GreptimeDB HTTP, from Task 1).
- Produces: Vector DaemonSet shipping `kubernetes_logs` to GreptimeDB table `rask_logs`.

- [ ] **Step 1: Write a render assertion (expected to fail)**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'type: greptimedb_logs'
```
Expected: FAIL (no greptimedb_logs sink yet).

- [ ] **Step 2: Fill the `vector:` block in `chart/values.yaml`**

Replace `vector:\n  role: "Agent"` with:

```yaml
vector:
  role: "Agent"
  # customConfig fully REPLACES Vector's default config (chart contract), so it
  # must be complete: a kubernetes_logs source piped to the greptimedb_logs sink.
  customConfig:
    data_dir: /vector-data-dir
    api:
      enabled: false
    sources:
      pod_logs:
        type: kubernetes_logs
    sinks:
      greptimedb_logs:
        type: greptimedb_logs
        inputs: ["pod_logs"]
        endpoint: "http://rask-greptimedb-standalone:4000"
        dbname: "public"
        table: "rask_logs"
        compression: "gzip"
```

- [ ] **Step 3: Verify render**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'type: greptimedb_logs|type: kubernetes_logs|rask-greptimedb-standalone:4000' && echo OK-vector
helm lint ./chart
```
Expected: all three lines present (`OK-vector`); lint clean.

- [ ] **Step 4: Commit**

```bash
git add chart/values.yaml
git commit -m "feat(obs): configure Vector Agent to ship pod logs to GreptimeDB"
```

---

## Task 3: Perses datasource (GreptimeDB Prometheus API)

**Files:**
- Modify: `chart/values.yaml` (the `perses:` block)

**Interfaces:**
- Consumes: `rask-greptimedb-standalone:4000/v1/prometheus` (GreptimeDB Prometheus query API).
- Produces: a Perses `GlobalDatasource` named `greptimedb` (default), provisioned via the chart's `datasources:` values key.

- [ ] **Step 1: Write a render assertion (expected to fail)**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'PrometheusDatasource'
```
Expected: FAIL (no datasource yet).

- [ ] **Step 2: Fill the `perses:` block in `chart/values.yaml`**

Replace `perses: {}` with:

```yaml
perses:
  # Provision a GreptimeDB datasource via the chart's datasources values key
  # (renders a ConfigMap mounted at /etc/perses/datasources). GreptimeDB exposes
  # a Prometheus-compatible query API at :4000/v1/prometheus.
  datasources:
    - kind: GlobalDatasource
      metadata:
        name: greptimedb
      spec:
        default: true
        plugin:
          kind: PrometheusDatasource
          spec:
            directUrl: "http://rask-greptimedb-standalone:4000/v1/prometheus"
```

- [ ] **Step 3: Verify render**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'PrometheusDatasource|rask-greptimedb-standalone:4000/v1/prometheus' && echo OK-perses
helm lint ./chart
```
Expected: both lines present (`OK-perses`); lint clean.

- [ ] **Step 4: Commit**

```bash
git add chart/values.yaml
git commit -m "feat(obs): add Perses GreptimeDB Prometheus datasource"
```

---

## Task 4: Fleet OTLP env injection (chart)

**Files:**
- Modify: `chart/templates/fleet.yaml`

**Interfaces:**
- Consumes: `observability.enabled`; service `rask-greptimedb-standalone:4001` (OTLP gRPC).
- Produces: every fleet pod gets `OTEL_*` env (gated) so the app SDK (Task 5) exports to GreptimeDB.

- [ ] **Step 1: Write a render assertion (expected to fail)**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'OTEL_EXPORTER_OTLP_ENDPOINT'
```
Expected: FAIL.

- [ ] **Step 2: Add gated OTLP env to the fleet container in `chart/templates/fleet.yaml`**

In the container `env:` list (after the existing `RASK_ORCHESTRATOR_AUTOSTART` entry), add:

```yaml
            {{- if $root.Values.observability.enabled }}
            - name: RASK_OTEL_ENABLED
              value: "true"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "http://rask-greptimedb-standalone:4001"
            - name: OTEL_EXPORTER_OTLP_PROTOCOL
              value: "grpc"
            - name: OTEL_EXPORTER_OTLP_HEADERS
              value: "x-greptime-db-name=public"
            - name: OTEL_SERVICE_NAME
              value: {{ $name | quote }}
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: {{ printf "service.namespace=rask,deployment.environment=%s" $root.Release.Namespace | quote }}
            {{- end }}
```

(`$root` and `$name` are already in scope inside the `range $name, $svc` loop in fleet.yaml.)

- [ ] **Step 3: Verify render (on + off)**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'OTEL_EXPORTER_OTLP_ENDPOINT.*rask-greptimedb-standalone:4001' && echo OK-otel-env
helm template rask ./chart --set observability.enabled=false --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -c 'OTEL_EXPORTER_OTLP_ENDPOINT' | grep -q '^0$' && echo OK-off
helm lint ./chart
```
Expected: `OK-otel-env`, `OK-off`, lint clean.

- [ ] **Step 4: Commit**

```bash
git add chart/templates/fleet.yaml
git commit -m "feat(obs): inject gated OTLP env into the fleet pods"
```

---

## Task 5: service-kit OTel instrumentation (FastAPI fleet)

**Files:**
- Create: `packages/service-kit/src/service_kit/otel.py`, `packages/service-kit/tests/test_otel.py`
- Modify: `packages/service-kit/src/service_kit/config.py`, `packages/service-kit/src/service_kit/__init__.py`, `packages/service-kit/pyproject.toml`

**Interfaces:**
- Consumes: `OTEL_*` env (Task 4); `Settings` from `config.py`.
- Produces: `setup_otel(app: FastAPI, service_name: str, settings: Settings) -> bool` — returns `True` if instrumentation was wired, `False` if skipped (disabled). Called by `make_service_app`.

- [ ] **Step 1: Add OTel deps to `packages/service-kit/pyproject.toml`**

Add to `[project] dependencies`:

```toml
  "opentelemetry-sdk>=1.27",
  "opentelemetry-exporter-otlp-proto-grpc>=1.27",
  "opentelemetry-instrumentation-fastapi>=0.48b0",
  "opentelemetry-instrumentation-httpx>=0.48b0",
  "opentelemetry-instrumentation-logging>=0.48b0",
```

Then `uv sync` (or `uv lock`) so the env resolves.

- [ ] **Step 2: Add the `otel_enabled` setting to `config.py`**

In the `Settings` class add:

```python
    otel_enabled: bool = False
```

(Pydantic settings read `RASK_OTEL_ENABLED` per the existing `RASK_` env prefix convention; verify the prefix in the class config and match it.)

- [ ] **Step 3: Write the failing test `packages/service-kit/tests/test_otel.py`**

```python
from fastapi import FastAPI

from service_kit.config import Settings
from service_kit.otel import setup_otel


def test_setup_otel_noop_when_disabled():
    app = FastAPI()
    settings = Settings(otel_enabled=False)
    wired = setup_otel(app, "svc-test", settings)
    assert wired is False


def test_setup_otel_wires_when_enabled(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    app = FastAPI()
    settings = Settings(otel_enabled=True)
    wired = setup_otel(app, "svc-test", settings)
    assert wired is True
    # FastAPI instrumentation marks the app
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest packages/service-kit/tests/test_otel.py -v`
Expected: FAIL (`ModuleNotFoundError: service_kit.otel`).

- [ ] **Step 5: Implement `packages/service-kit/src/service_kit/otel.py`**

```python
"""OpenTelemetry wiring — opt-in OTLP/gRPC export for the fleet services.

No-op unless instrumentation is enabled (settings.otel_enabled) or an OTLP
endpoint is configured (OTEL_EXPORTER_OTLP_ENDPOINT). The OTLP exporter reads
OTEL_EXPORTER_OTLP_* from the environment (endpoint, protocol, headers), so this
module only constructs providers + instruments the app; it hardcodes no target.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from service_kit.config import Settings


def setup_otel(app: FastAPI, service_name: str, settings: Settings) -> bool:
    """Wire traces/metrics/logs OTLP export + FastAPI instrumentation.

    Returns True if instrumentation was applied, False if skipped.
    """
    enabled = settings.otel_enabled or bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if not enabled:
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    LoggingInstrumentor().instrument(set_logging_format=True)
    return True
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest packages/service-kit/tests/test_otel.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Call `setup_otel` from `make_service_app`**

In `packages/service-kit/src/service_kit/__init__.py`, after the `app = FastAPI(...)` block and middleware registration, add:

```python
    from service_kit.otel import setup_otel

    setup_otel(app, service_name=title or name, settings=settings)
```

(Use whatever the factory already has for the service's name/title and its `settings` instance — match the existing parameter names in `make_service_app`.)

- [ ] **Step 8: Run the full service-kit suite + typecheck**

Run: `uv run pytest packages/service-kit -q && uvx ty check packages/service-kit`
Expected: tests pass; no type errors.

- [ ] **Step 9: Commit**

```bash
git add packages/service-kit/pyproject.toml packages/service-kit/src/service_kit/otel.py packages/service-kit/src/service_kit/config.py packages/service-kit/src/service_kit/__init__.py packages/service-kit/tests/test_otel.py uv.lock
git commit -m "feat(obs): opt-in OpenTelemetry OTLP instrumentation in service-kit"
```

---

## Task 6: Ray worker instrumentation + RayService OTLP env

**Files:**
- Modify: the Ray Serve app entrypoint (`components/apps/runner/src/runner/htrflow_service.py` or wherever `htrflow_app` is defined) and `components/apps/runner/pyproject.toml`; `chart/templates/rayservice.yaml`

**Interfaces:**
- Consumes: `OTEL_*` env injected into the Ray runtime (this task adds it to `rayservice.yaml`).
- Produces: Ray Serve process exports OTLP traces to GreptimeDB (`service.name=ray-htrflow`).

- [ ] **Step 1: Add OTel deps to `components/apps/runner/pyproject.toml`**

Add to its `dependencies`:

```toml
  "opentelemetry-sdk>=1.27",
  "opentelemetry-exporter-otlp-proto-grpc>=1.27",
```

- [ ] **Step 2: Add a minimal OTel init in the Serve app**

In the htrflow Serve module, near app construction, add an idempotent setup that runs only when an endpoint is set (the exporter reads `OTEL_*` from env):

```python
import os


def _init_otel() -> None:
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return  # already initialised
    provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "ray-htrflow")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


_init_otel()
```

- [ ] **Step 3: Inject OTLP env into the RayService runtime in `chart/templates/rayservice.yaml`**

In the `serveConfigV2` `runtime_env.env_vars` block, add (gated):

```yaml
            {{- if .Values.observability.enabled }}
            OTEL_EXPORTER_OTLP_ENDPOINT: "http://rask-greptimedb-standalone:4001"
            OTEL_EXPORTER_OTLP_PROTOCOL: "grpc"
            OTEL_EXPORTER_OTLP_HEADERS: "x-greptime-db-name=public"
            OTEL_SERVICE_NAME: "ray-htrflow"
            {{- end }}
```

- [ ] **Step 4: Verify render**

```bash
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -E 'OTEL_SERVICE_NAME: "ray-htrflow"' && echo OK-ray-env
helm template rask ./chart --set observability.enabled=false --set secrets.postgresPassword=x --set secrets.minioSecretKey=y \
  | grep -c 'ray-htrflow' | grep -q '^0$' && echo OK-off
```
Expected: `OK-ray-env`, `OK-off`.

- [ ] **Step 5: Commit**

```bash
git add components/apps/runner/pyproject.toml components/apps/runner/src/runner chart/templates/rayservice.yaml uv.lock
git commit -m "feat(obs): instrument Ray Serve htrflow with OTLP traces"
```

---

## Task 7: Docs + render matrix

**Files:**
- Modify: `chart/templates/NOTES.txt`, `chart/README.md`, `CLAUDE.md`, `docs/architecture/deployment.md`

**Interfaces:** consumes everything from Tasks 1–6.

- [ ] **Step 1: Update `chart/templates/NOTES.txt`**

Add an observability section (gated on `.Values.observability.enabled`) listing: GreptimeDB `rask-greptimedb-standalone:4000` (HTTP/SQL/Prometheus) + `:4001` (OTLP gRPC), Perses `rask-perses:8080`, Vector DaemonSet shipping pod logs, storage in RustFS bucket `rask-observability`.

- [ ] **Step 2: Update `chart/README.md`**

Add an "Observability" subsection: the three subcharts + `observability.enabled` toggle, GreptimeDB-on-RustFS, apps export OTLP-direct, OTel-Arrow not used (standard OTLP), Perses datasource.

- [ ] **Step 3: Update `CLAUDE.md`**

In the architecture bullets, add a one-line observability entry: "Observability (optional, `observability.enabled`): Vector → GreptimeDB (on RustFS S3) → Perses; fleet + Ray export OTLP/gRPC to `rask-greptimedb-standalone:4001` via `service_kit.setup_otel`."

- [ ] **Step 4: Update `docs/architecture/deployment.md`**

Add an observability subsection mirroring the README, including the greenfield note that the `rask-observability` bucket is auto-provisioned by the RustFS Tenant.

- [ ] **Step 5: Verify the full render matrix + lint**

```bash
helm lint ./chart
helm template rask ./chart --set secrets.postgresPassword=x --set secrets.minioSecretKey=y >/dev/null && echo OK-on
helm template rask ./chart --set observability.enabled=false --set secrets.postgresPassword=x --set secrets.minioSecretKey=y >/dev/null && echo OK-off
```
Expected: lint clean; `OK-on`; `OK-off`.

- [ ] **Step 6: Commit**

```bash
git add chart/templates/NOTES.txt chart/README.md CLAUDE.md docs/architecture/deployment.md
git commit -m "docs(obs): document the Vector/GreptimeDB/Perses observability stack"
```

---

## Task 8: Live verification on k3s (run WITH the user)

**Files:** none (verification + fixups).

- [ ] **Step 1: Vendor deps + upgrade the release with observability on**

```bash
make k3s-deps
KUBECONFIG=/home/morgan/.kube/config helm upgrade --install rask ./chart --wait --timeout 12m \
  --force-conflicts --take-ownership --set ray.enabled=false --set dapr.sidecars=false \
  --set-string secrets.postgresPassword=raskpgpass1234 --set-string secrets.minioSecretKey=rasks3secretkey1234
```
Expected: install completes; `rask-greptimedb-standalone-0`, `rask-vector-*` (DaemonSet), `rask-perses-*` pods Running.

- [ ] **Step 2: Verify GreptimeDB created its bucket on RustFS + is writing**

```bash
# rask-observability bucket exists (provisioned by the RustFS Tenant)
AK=$(kubectl get secret rask-rustfs -o jsonpath='{.data.accesskey}' | base64 -d)
SK=$(kubectl get secret rask-rustfs -o jsonpath='{.data.secretkey}' | base64 -d)
kubectl run s3o --rm -i --restart=Never --image=amazon/aws-cli \
  --env AWS_ACCESS_KEY_ID="$AK" --env AWS_SECRET_ACCESS_KEY="$SK" --env AWS_EC2_METADATA_DISABLED=true --command -- \
  aws --endpoint-url http://rask-rustfs-io:9000 s3 ls s3://rask-observability/ --recursive | head
```
Expected: GreptimeDB objects appear under `s3://rask-observability/greptimedb/`.

- [ ] **Step 3: Verify Vector ships pod logs to GreptimeDB**

```bash
kubectl exec rask-greptimedb-standalone-0 -- \
  curl -s "http://localhost:4000/v1/sql?db=public" --data-urlencode "sql=SELECT count(*) FROM rask_logs" | head
```
Expected: a non-zero count (pod logs ingested into `rask_logs`).

- [ ] **Step 4: Verify a fleet request produces a trace**

```bash
kubectl exec deploy/rask-gateway -- python -c "import urllib.request; urllib.request.urlopen('http://localhost:8888/api/health',timeout=5)"
# traces table is created by GreptimeDB on first OTLP trace; check it exists + has rows
kubectl exec rask-greptimedb-standalone-0 -- \
  curl -s "http://localhost:4000/v1/sql?db=public" --data-urlencode "sql=SHOW TABLES" | grep -i trace
```
Expected: an OTLP traces table is present (GreptimeDB auto-creates `opentelemetry_traces` on first trace export).

- [ ] **Step 5: Verify Perses datasource**

```bash
kubectl port-forward svc/rask-perses 8080:8080 &
sleep 3
curl -fsS http://localhost:8080/api/v1/globaldatasources | grep -i greptimedb && echo OK-perses-ds
```
Expected: the `greptimedb` GlobalDatasource is registered (`OK-perses-ds`).

- [ ] **Step 6: Commit any fixups**

```bash
git add -A && git commit -m "fix(obs): adjustments from live k3s verification"   # only if changes were needed
```

---

## Self-Review

- **Spec coverage:** deps + GreptimeDB-on-RustFS + bucket + secret (T1) · Vector logs (T2) · Perses datasource (T3) · fleet OTLP env (T4) · service-kit instrumentation (T5) · Ray instrumentation (T6) · docs (T7) · live verify incl. S3 storage / logs / traces / datasource (T8). All spec sections map to a task.
- **Placeholder scan:** every step has concrete config/code/commands + expected output; no TBD/TODO. Two items are explicitly flagged to verify-at-runtime in the spec (GreptimeDB S3 path-style vs virtual-host against RustFS; GreptimeDB OTLP traces table name) and are checked in Task 8 — not placeholders, but live confirmations.
- **Type/name consistency:** service names `rask-greptimedb-standalone` (4000/4001), `rask-vector`, `rask-perses` (8080); secret `rask-observability-s3` with env keys `GREPTIMEDB_STANDALONE__STORAGE__ACCESS_KEY_ID/SECRET_ACCESS_KEY`; bucket `rask-observability`; `setup_otel(app, service_name, settings) -> bool`; toggle `observability.enabled` / `RASK_OTEL_ENABLED` — all used identically across tasks.
