# open_dapr_otel.md — the Dapr plane is fully instrumented, and exports nothing

**Working plan, 2026-08-22.** Delete this file when the slices in §10 have landed. Companion:
`open_ray_otel.md` (the Ray half of the same audit). Both exist because `TODO.md:26` says
"missing telemetry all over the places" and nobody had measured where.

**The one-line state:** every daprd sidecar in this estate creates a span for every hop, samples it
at **100%**, propagates the context — and hands the span to a **NullExporter**. `spec.tracing` in the
`lance-tracing` Configuration names `samplingRate` and **no exporter at all**. Not one Dapr span has
ever reached a backend, and three files in this repo assert the opposite.

That is not the whole finding. The estate is **two telemetry planes wearing one name**, and the
half that carries the front door is the less instrumented of the two.

**Method.** 15 agents: 3 read the upstream Dapr contract against **dapr/dapr v1.18.1** (the version
`chart/Chart.yaml:63` pins) rather than the docs, because the docs demonstrably drift; 6 read one
repo subsystem each; 6 adversarially tried to refute what the first six wrote. **96 findings survived,
0 were refuted, 50 were corrected on the way through** — mostly severity deflation and cite fixes.
Every claim below is `file:line`, and every absence claim is a negative grep that was re-run by a
second agent. Nothing here is remembered.

---

## 1. The two planes — read this table first, everything else follows from it

Thirteen first-party Python services. They are instrumented by **two mutually exclusive mechanisms**,
and no file in the repo says so.

| | **Lance plane** (7 pods) | **Fleet plane** (6 pods) |
| --- | --- | --- |
| Services | catalog, lineage, medallion-producer, 3 movers, maintenance (+ viewer/search/annotator under `explorer.enabled`) | gateway, compute, controlplane, ingest, flows, notifications |
| Rendered by | `services.yaml`, `medallion.yaml`, `maintenance.yaml`, `explorer.yaml` | `fleet.yaml`, `controlplane.yaml` |
| Process command | `["opentelemetry-instrument", "uvicorn"]` (`services.yaml:46`, `:258`; `medallion.yaml:43`, `:275`; `maintenance.yaml:74`; `explorer.yaml:98`) | `["uvicorn"]` (`fleet.yaml:72`, `controlplane.yaml:34`) |
| SDK activation | the **launcher** — auto-loads *every* installed `opentelemetry-instrumentation-*` entry point | `service_kit.setup_otel` — exactly **three** instrumentors (`otel.py:84-86`) |
| Env helper | `lance.otelEnv` (`_helpers.tpl:605-632`) | `rask.otelEnv` (`_helpers.tpl:231-258`) |
| Export target | the **OTel Collector** (`lance.otlpEndpoint`) | **direct to GreptimeDB**, hardcoded (`_helpers.tpl:242`) |
| Traces | ✅ | ✅ |
| Metrics | ✅ every 5 s (`_helpers.tpl:626`) | ✅ every **60 s** (SDK default, unset) |
| **Logs over OTLP** | ✅ `OTEL_LOGS_EXPORTER=otlp` (`_helpers.tpl:624`) | ❌ **no exporter, no `LoggerProvider`** |
| gRPC / aiohttp / requests client spans | ✅ (launcher loads what the image has) | ❌ none |
| Probe spans excluded | ✅ (`_helpers.tpl:630`) | ❌ (except `notifications`, which worked around it in code) |
| `k8s.*` resource attributes | ✅ (Collector's `k8sattributes`) | ❌ (bypasses the Collector) |
| `service.version` | ✅ | ❌ |
| `deployment.environment` key | `deployment.environment.name` (current semconv) | `deployment.environment` (**deprecated**) |
| `service.namespace` | `lance-ns` | `rask` |

Two consequences worth stating plainly, because they are the shape of most of §5–§8:

- **A dependency added to a fleet service is instrumented in the lance plane and not in the fleet,
  from the same lockfile, with nothing to tell you which.** `services/lineage/pyproject.toml:22`
  declares `opentelemetry-instrumentation-psycopg` that **no line of Python ever calls** — and gets
  it anyway, because the launcher loads entry points. The fleet cannot do that.
- **No single resource query selects the whole estate.** `service.namespace="rask"` returns 6
  services and silently drops the lakehouse. Filtering on `deployment.environment.name` drops the
  fleet. Grouping by `service.namespace` shows a partition that corresponds to nothing real.

---

## 2. The crux — `spec.tracing` names no exporter, and daprd says nothing about it

`chart/templates/observability.yaml:54-64` renders, in full:

```yaml
spec:
  tracing:
    samplingRate: "1"
  workflow:
    stateRetentionPolicy: { completed: 168h, failed: 720h, terminated: 720h }
```

Verified by `helm template`: that is the entire object. `grep -n "otel:\|zipkin\|stdout\|endpointAddress" chart/templates/observability.yaml` → **no matches**.

**Upstream contract (Dapr 1.18.1).** `spec.tracing` accepts exactly three exporter shapes:

```yaml
spec:
  tracing:
    samplingRate: "1"
    stdout: true                 # dump to the sidecar's own stdout
    otel:
      endpointAddress: "host:port"   # REQUIRED. bare host:port — Dapr appends no URL path
      isSecure: false                # DEFAULTS TO TRUE — omit it and the sidecar attempts TLS and fails
      protocol: grpc                 # exactly "http" or "grpc"; anything else is a fatal startup error
      headers:                       # arbitrary {name,value} or {name,secretKeyRef} pairs
        - { name: "x-api-key", value: "…" }
      timeout: "30s"
    zipkin:
      endpointAddress: "https://…"
```

`samplingRate` is a **sampling probability, not an enable switch**. With no exporter block Dapr
registers its NullExporter: spans are created, sampled at 100%, `traceparent` still propagates —
and `ExportSpans` returns `nil`. **No log line. No error. No metric.** It looks exactly like a
collector that is down.

**Three files assert this works.** They are wrong and should be corrected in the same commit:

- `chart/templates/observability.yaml:56-61` — *"There is NO otel exporter here … which is what
  makes the app-level distributed trace span catalog → (Dapr/NATS) → lineage end to end."*
- `chart/values.yaml:785` — *"Dapr propagates the trace context across every hop"*
- `docs/MEDALLION.md:209-211` — *"a single `trace_id` spans … the event followed across every Dapr hop
  (the gRPC publish injects `traceparent`; each subscriber continues the trace)"* — and `:224` —
  *"the whole flow is observable as one trace without any glue code"*

**The stated reason for omitting it is also false on its own terms.** The comment says Dapr's spans
*"can't carry GreptimeDB's required db-name/pipeline headers"*. Two independent refutations:

1. Dapr's `otel.headers` accepts arbitrary pairs (Dapr ≥1.14; this estate pins 1.18.1). It can carry them.
2. It does not need to. **The Collector exists**, is deployed by this chart, listens on both 4317
   and 4318 (`otel-collector.yaml:73-76`), already has `traces.receivers: [otlp]` (`:197`), and adds
   the GreptimeDB headers itself (`:184-188`). The receiving half is **already built and idle.**

### The fix

Add the block inside the existing `{{- if include "lance.otelEnabled" . }}` guard. **Target the
Collector, not GreptimeDB** — Dapr sets no URL path, so `protocol: http` posts to
`<endpointAddress>/v1/traces` while GreptimeDB ingests at `/v1/otlp/v1/traces`, a prefix Dapr cannot
express. A new helper yields the bare `host:port`:

```
{{- define "lance.daprOtlpTarget" -}}
{{- $c := .Values.observability.otelCollector | default dict -}}
{{- if $c.externalEndpoint -}}{{ $c.externalEndpoint | trimPrefix "https://" | trimPrefix "http://" | trimSuffix "/v1/otlp" | trimSuffix "/" }}
{{- else if $c.enabled -}}{{ include "lance.fullname" . }}-otel-collector:4317
{{- end -}}
{{- end -}}
```

then, in `observability.yaml` after `:63`:

```yaml
  tracing:
    samplingRate: {{ .Values.observability.samplingRate | quote }}
{{- with include "lance.daprOtlpTarget" . }}
    otel:
      endpointAddress: {{ . | quote }}
      isSecure: false      # load-bearing: defaults TRUE upstream, and the in-cluster Collector is plaintext
      protocol: grpc
      timeout: "30s"
{{- end }}
```

**And pin it**, because the reason this defect survived is that nothing tests for it. The invariant
suite has three tests over `lance-tracing` (`tests/unit/test_invariants.py:2128-2191`) — all three are
about workflow retention and the non-dangling `dapr.io/config` reference. One of them even reads the
tracing key (`assert "tracing" not in spec` at `:2182`), so the suite knows the stanza exists and
never checks that it can export. Add:

```python
def test_dapr_sidecar_spans_actually_have_an_exporter() -> None:
    """samplingRate is a SAMPLING knob, not an enable switch. With no otel:/zipkin:/stdout,
    daprd registers a NullExporter whose ExportSpans returns nil — no log, no error, and every
    sidecar span dropped while traceparent still propagates. Indistinguishable from a dead collector."""
    spec = _lance_tracing_config(_helm_template())
    tracing = spec["tracing"]
    assert {"otel", "zipkin", "stdout"} & set(tracing), "tracing configured with no exporter"
```

The chart already has a render-time `fail` guard against a *malformed workflow retention value*
(`observability.yaml:74-81`). It has none against a tracing stanza that exports into a black hole —
a strictly more total silent failure.

---

## 3. The other half of the same break — the fleet sends its sidecar no `traceparent`

Fixing §2 alone gets you the sidecar's own spans. It does **not** join them to the app's, on the
fleet plane, because of a second independent gap.

`setup_otel` installs FastAPI + HTTPX + Logging and nothing else (`otel.py:84-86`).
`grep -rn "GrpcInstrumentor\|GrpcAioInstrumentor\|AioHttpClientInstrumentor\|RequestsInstrumentor" --include=*.py packages/ services/ scripts/ runners/` → **no matches, anywhere in the repo.**

Every fleet Dapr call rides an uninstrumented transport:

| Transport | Used for | Call sites | Instrumented? |
| --- | --- | --- | --- |
| `grpc.aio` (`dapr.aio.clients.DaprClient`) | publish, state, bindings, workflow schedule | `notifications/proxies.py:189`, `medallion/producer.py:25`, `catalog/core/lineage_emit.py:37`, `flows/lifespan.py:232`, `ingest/__init__.py:280` | ❌ fleet only¹ |
| `aiohttp` (`ActorProxy` → `DaprActorHttpClient`) | **every actor call in the estate** | `notifications/proxies.py:110-118`, `annotator/api/v1/endpoints/tasks.py:131-134`, `service_kit/governed/actor_warmup.py:43-45` | ❌ **both planes** — the package is not in `uv.lock` |
| `aiohttp` (`openfga_sdk`) | **every authorization check** | `service_kit/governed/fga.py:38-40` | ❌ **both planes** |
| `requests` (lineage-kit HTTP emitter) | the non-Dapr lineage lane | `packages/lineage-kit` | ❌ **both planes** |

¹ `medallion` and `catalog` declare `opentelemetry-instrumentation-grpc` in their own pyprojects
(`uv.lock:351/:369`, `:2000/:2027`) and get it via the launcher. The six fleet pyprojects declare
**no opentelemetry deps at all**.

**Why this is worse than one missing span.** The break is at the *first* hop. The sidecar receives
no `traceparent`, so its producer span **roots a new trace**; that fresh id is what gets stamped into
the CloudEvent envelope and persisted as `ExecutionStartedEvent.ParentTraceContext`. Every activity,
every lineage event and every notification downstream inherits the orphan. **A severed subtree, not
a missing node.** Concretely: a POST to `flows` and the durable workflow it starts are two unrelated
traces. Same for `ingest`. `inbox.fanout` (`notifications/api/fanout.py:130-146`) shows N recipients
and **zero children**.

Dapr's own SDK tries to help and cannot: `dapr/ext/workflow/_durabletask/client.py:39-44` calls
`GrpcInstrumentorClient().instrument()` inside `except ImportError: pass` — a no-op in images that
do not ship the instrumentor.

### The fix — at the seam, once

`packages/service-kit/pyproject.toml` base deps gain three packages; `otel.py` gains ~10 lines after `:86`:

```python
from contextlib import suppress

with suppress(ImportError):
    from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient, GrpcInstrumentorClient

    # dapr.aio.clients.DaprClient rides grpc.aio (dapr/clients/grpc/_channel.py:102,108);
    # dapr-ext-workflow's DaprWorkflowClient rides SYNC grpc. Both, or it looks wired and is not.
    GrpcAioInstrumentorClient().instrument(tracer_provider=tracer_provider)
    GrpcInstrumentorClient().instrument(tracer_provider=tracer_provider)

with suppress(ImportError):
    from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor

    AioHttpClientInstrumentor().instrument(tracer_provider=tracer_provider)  # ActorProxy + openfga_sdk

with suppress(ImportError):
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    RequestsInstrumentor().instrument(tracer_provider=tracer_provider)  # lineage-kit HTTP lane
```

`BaseInstrumentor` is a per-class singleton, so this is safe alongside the SDK's own import-time call.
Dapr accepts the W3C `traceparent` metadata OTel injects (its `grpc-trace-bin` → `traceparent`
fallback), so no app-side header plumbing is needed. Add one test asserting
`grpc.aio.insecure_channel` is patched after `setup_otel(..., otel_enabled=True)`.

**Then give the hops names the estate owns**, so a future missing instrumentor degrades to
"unparented span" rather than "no span":

- `dapr_publish.publish_event` (`service_kit/dapr_publish.py:49-70`) is the **single funnel for all
  15 publish sites** and emits no span, no counter, no histogram. One `PRODUCER` span +
  `messaging.*` attributes there covers the whole bus.
- `TypedActorProxy.__getattr__` (`notifications/proxies.py:70-107`) already holds `actor_type` and
  the wire method name — wrap the invoke in a `CLIENT` span named `CallActor/{actor_type}/{method}`.
- The FGA check helper (`service_kit/governed/fga.py`) — a per-request dependency on the read path of
  every governed door, currently with no span and no counter on its fail-closed 503 path (`:103`).

---

## 4. Dapr Workflow is the least observable thing in the estate

`grep -rn "start_as_current_span\|get_tracer" --include=*.py packages/ services/ | grep -v tests/`
returns **five modules**: `transform.py`, `produce.py`, `media_produce.py`, `fanout.py`, `sweep.py`.
**No workflow module anywhere** — not `medallion/workflow.py`, not `flows/`, not `ingest/workflow.py`.
`services/flows/src/flows` has no metrics module at all.

The only workflow spans that exist come from the SDK's `_activity_span`
(`dapr/ext/workflow/_durabletask/worker.py:982-990`), which extracts
`req.parentTraceContext.traceParent` — i.e. it inherits the orphan trace from §3. There is **no
orchestrator span** in the SDK at all.

So a durable run — the thing whose entire purpose is surviving crashes and taking minutes to hours —
is unobservable: you cannot get from a run id to a trace, or from the request a person filed to the
orchestration that executed it. In medallion, the stage workflow's `ctx.create_timer` poll loop (the
thing that decides whether a Ray job finished) emits nothing, so *"the cascade is stuck"* has no
signal beyond the absence of a downstream event.

Meanwhile **15 `dapr_runtime_workflow_*` metric families are already being scraped into GreptimeDB
and read by nothing** — no panel, no alert.

**Fix, in order:** (a) land §3, which makes `create_orchestration` a child of the caller for free;
(b) wrap each schedule call in a `CLIENT` span stamping `lance.workflow.instance_id`; (c) open an
`INTERNAL` span at the top of each `@wfr.activity` body — **not** in the orchestrator body, which
replays, making a span-per-replay both wrong and unbounded; (d) add a `workflows.json` Perses
dashboard over the families already collected.

---

## 5. Logs — three routes, and the one that deletes

The log tier is where the audit found its one **critical**.

### 5a. On the 7 lance pods, crash logs are deleted outright — and the chart says the opposite

uvicorn's `LOGGING_CONFIG` sets `propagate: False` on the `uvicorn` and `uvicorn.access` loggers
(`.venv/…/uvicorn/config.py:108`, `:110`), and defines `uvicorn.error` with a level only (`:109`).
So `callHandlers` stops at `uvicorn` and **no `uvicorn.error` record ever reaches the root logger** —
which means it never reaches the OTLP `LoggingHandler`. That covers:

- `"Application startup failed. Exiting."` (`uvicorn/lifespan/on.py:59`)
- the lifespan traceback (`:37`)
- `"Exception in ASGI application"` (`h11_impl.py:421`)

Their only copy is stdout. And on the 7 lance pods, `filter/drop_app_file_logs`
(`otel-collector.yaml:174-178`) **deletes exactly that copy** — because the `lance.dev/logs: otlp`
discriminator is a **pod-spec label** (`services.yaml:26`, `:229`; `medallion.yaml:26`, `:258`;
`maintenance.yaml:56`; `explorer.yaml:79`), present from pod creation, and `k8sattributes` stamps it
on every container in the pod.

**Net: a crash-on-import, a failed lifespan, or an ASGI-level exception on catalog / lineage /
medallion-producer / the three movers / maintenance produces ZERO rows in GreptimeDB.** The one class
of failure where the OTLP exporter provably cannot exist is the class the filter deletes.

`chart/templates/_helpers.tpl:697-702` claims the opposite — *"the Collector's filelog tail still
catches it (it's not labelled until the pod is up, so it's not dropped)"*. It is labelled from
creation. The repo already knows this elsewhere: `maintenance/core/lance_trace.py:10-15` states the
drop as fact and routes Lance's stderr around it.

The same label-scoped filter also **deletes the daprd sidecar's own logs on those 7 pods** — the
container where pub/sub retries, component-init failures, mTLS problems and actor placement churn are
reported.

### 5b. The fleet exports no OTLP logs at all

`setup_otel` builds a `TracerProvider` and a `MeterProvider` and **no `LoggerProvider`**
(`otel.py:53-63`, `:70-72`, `:80-82`). It then calls
`LoggingInstrumentor().instrument(set_logging_format=True)` at `:86`, which at 0.65b0 *does* install
an OTLP `LoggingHandler` — bound to a `ProxyLoggerProvider` whose `ProxyLogger` falls back to
`_noop_logger`. The handler's `emit` skips only on `NoOpLogger`, and a `ProxyLogger` is not one.

**Measured**, not inferred: after `setup_otel(FastAPI(), 'svc-test', Settings())` with
`RASK_OTEL_ENABLED=true`, root handlers are `[('rask-stdout', StreamHandler), (None, LoggingHandler)]`,
`get_logger_provider()` → `ProxyLoggerProvider`, `hasattr(lp, 'force_flush')` → `False`.

So the fleet **translates every log record into an OTel record and then drops it** — paying the cost
for nothing. Its logs reach GreptimeDB only as unparsed stdout text via the filelog tail.

Three knock-on effects:

- **The compliance audit trail is emptied for `ingest` and `flows`.** `audit()` puts every field in
  `extra={...}` (`service_kit/governed/audit.py:57-65`); the only formatter on that route is
  `service_kit/__init__.py:65` — `"%(asctime)s %(levelname)-7s %(name)s — %(message)s"` — which
  references no extra key. An `ingest_service_token` DENY lands as the bare line `… INFO lance.audit — audit`.
- **`set_logging_format=True` is inert** for every `make_service_app` service: `setup_logging()` runs
  first (`__init__.py:119`) and installs a fixed formatter at `:65`, so the instrumentor's
  `basicConfig` is a no-op. No fleet log line carries a trace id on any path.
- The notification plane's 14 structured events lose their attributes before leaving the pod.

**Fix — ~8 lines in `otel.py`, between the metric block and `:86`:**

```python
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
set_logger_provider(logger_provider)
```

then pass it explicitly so the handler cannot bind to a proxy:
`LoggingInstrumentor().instrument(set_logging_format=True, logger_provider=logger_provider)`.
The exporter package is **already** in `pyproject.toml:15`, and `_helpers.tpl:245-248` already renders
the db-name-only header GreptimeDB's `/v1/otlp/v1/logs` wants. **No chart change required.**

Then add `lance.dev/logs: otlp` to the fleet pod labels so the existing dedupe filter stops
double-ingesting them.

### 5c. Nothing parses anything

`otel-collector.yaml:87-88` — the complete operator list is `operators: [- type: container]`.
`grep -nE "multiline|recombine|severity|json_parser|regex_parser|logfmt|transform" chart/templates/otel-collector.yaml` → **no matches**.

So on the file-tailed route (6 fleet services + **every daprd sidecar** + all infra pods):
`SELECT … WHERE severity_text = 'ERROR'` matches **nothing**, and a 40-line Python traceback becomes
40 unrelated rows. And `dapr.io/log-as-json` is set nowhere in the chart
(`grep -rn "log-as-json\|logAsJson" chart/` → no matches), so daprd ships plain logrus text:
a component-init failure and a routine startup line are the same shape.

**Fix:** add `dapr.io/log-as-json: "true"` beside `dapr.io/log-level` in `rask.daprAnnotations`
(`_helpers.tpl:202`), and give the filelog receiver a parser chain. daprd's JSON schema is fixed at
8 fields (`time, level, type, msg, scope, instance, app_id, ver`), so `scope` and `app_id` become
queryable columns for free:

```yaml
        operators:
          - type: container
          - type: recombine
            combine_field: body
            is_first_entry: 'body matches "^[0-9]{4}-"'
            source_identifier: attributes["log.file.path"]
          - type: json_parser
            if: 'body matches "^\\{"'
            parse_from: body
            severity: { parse_from: attributes.level }
            timestamp: { parse_from: attributes.time, layout_type: gotime, layout: '2006-01-02T15:04:05.999999Z' }
```

Also: `k8sattributes` never extracts `k8s.deployment.name` (`:154`), so a file-tailed log row can only
be found by **pod name** — two lines fix it (add the key, plus a `transform` that sets
`service.name` from it when absent).

---

## 6. `spec.metrics` is absent — and the default mints a series per object id

`grep -n "  metrics:" chart/templates/observability.yaml` → **rc=1**.
`grep -rn "increasedCardinality\|pathMatching\|recordErrorCodes\|latencyDistributionBuckets\|excludeVerbs\|apiLogging" chart/ services/ packages/` → **no matches**.

At Dapr 1.18.1, `spec.metrics.http.increasedCardinality` still **defaults to `true`**: the sidecar
stamps the **raw request path** onto its HTTP metrics.

This estate addresses actors and user state through the sidecar's HTTP API **with the subject in the
URL path**:

- `notifications/proxies.py:42-60` — `inbox_actor_id = encode_subject(subject)`
- `service_kit/governed/user_state.py:137-149` — `encode_subject`/`decode_subject` are **exact
  base64url inverses**; `:140` says outright *"The key travels in the sidecar's URL path"*

So every sidecar emits, into a live scrape (`otel-collector.yaml:91-109` → the metrics pipeline at `:200-203`):

```
dapr_http_server_request_count{path="/v1.0/actors/InboxActor/<base64url-of-the-OIDC-sub>/method/Deliver"}
```

> **CORRECTED 2026-08-23, before landing — this paragraph was WRONG.** It claimed the sidecar
> published the OIDC subject as a metric label. It does not, and never did. daprd 1.18.1 **already**
> templates actor ids (`path="/v1.0/actors/InboxActor/{id}/method"`) and drops state keys
> (`path="/v1.0/state/lance-statestore"`) **at the default**, verified three ways: a passive scrape of
> a live sidecar, an active probe with a subject-shaped key, and a real non-templated actor id present
> in app logs but absent from the metrics body. The rule at `notifications/api/metrics.py:7-11` is
> correct; this was not an instance of it, and shipping it as one would have attached a sound rule to a
> false case. The block below still earns its place — see the real hazard immediately following.

The real hazard is the **gateway's service-invocation path**, which no templating covers. Measured
live: `dapr_http_server_request_count{app_id="gateway",path="/v1.0/invoke/compute/method/api/ray/jobs"}`
— one unbounded series per table, namespace and project id, forever, in a store nothing authorizes
reads against. Bounded cardinality, not subject protection, is the justification.

The gateway is the other big emitter: `gateway/__init__.py:226-233` builds every upstream call as
`http://127.0.0.1:3500/v1.0/invoke/{app_id}/method` + the full public path, so every table id,
namespace and project id mints its own series.

### The fix

Add a `metrics:` block to `observability.yaml` as a sibling of `tracing:`, **inside the
`dapr.enabled` guard but NOT inside the `otelEnabled` guard** — cardinality is a data-protection
concern, not a telemetry one.

```yaml
  metrics:
    enabled: true
    recordErrorCodes: true
    http:
      increasedCardinality: false
      pathMatching:
        - /v1.0/actors/{actorType}/{actorId}/method/{method}
        - /v1.0/actors/{actorType}/{actorId}/reminders/{name}
        - /v1.0/actors/{actorType}/{actorId}/timers/{name}
        - /v1.0/state/{storeName}
        - /v1.0/state/{storeName}/{key}
        - /v1.0/publish/{pubsubName}/{topic...}
        - /v1.0/bindings/{name}
        - /v1.0/secrets/{storeName}/{key}
    latencyDistributionBuckets: [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000]
```

**Four traps, all found in v1.18.1 source and documented nowhere:**

1. **Spell it `metrics:` (plural), only.** Both `spec.metric` and `spec.metrics` are valid CRD keys
   and **merge field-by-field**, so a config split across the two half-applies while a grep for
   either finds half of it.
2. **Do not hand-write a `/v1.0/invoke/{id}/method/{method...}` catch-all.** Dapr auto-registers a
   service-invocation twin for every `pathMatching` entry; patterns go into a real `http.ServeMux`,
   and Go's ServeMux **panics on conflicting patterns** — which would take daprd down at startup
   rather than degrade.
3. **`increasedCardinality: false` silently unregisters `dapr_http_server_response_count`.**
   AUDITED AND CLEARED before landing (2026-08-23): no alert rule, dashboard panel, runbook, test or
   script reads any `dapr_http_server_*` family — the dashboards use the component/actor/scheduler
   families and the app SDK's own `http_server_duration_milliseconds_*`. Confirmed in v1.18.1 source:
   the view is appended only under `if h.legacy`, and `pathMatching` cannot re-register it.
4. **`latencyDistributionBuckets` is global, not per-family.** Workflow execution latency shares the
   HTTP distribution. The default 34 buckets top out at ~100 s, so **every workflow longer than that
   lands in `+Inf`**.

Add `spec.logging.apiLogging` in the same edit, with all three keys at once — `obfuscateURLs` and
`omitHealthChecks` both default `false`, and this estate's Dapr paths carry table, project and
**user-subject** identifiers, while the injector's own probes produce ~12 lines/min/sidecar of noise.
Note the precedence trap: `dapr.io/enable-api-logging` on a pod **wins in both directions** and
nothing in the Configuration can force it back on.

---

## 7. Metrics that exist and are read by nobody

The instrument inventory itself is **good** (see §9). The problem is downstream.

| Collected today | Consumed by |
| --- | --- |
| 15 `dapr_runtime_workflow_*` families | nothing |
| `dapr_component_pubsub_egress_count{success="false"}` / `ingress_count{process_status="drop"}` | nothing — **publish failures and undeliverable drops are invisible** |
| `dapr_runtime_service_invocation_res_recv_latency_ms_*{app_id,src_app_id,status}` | nothing — this is the per-upstream gateway latency the estate lacks |
| `dapr_component_input_binding_count_total` | nothing — **6 cron bindings, none measured** |
| sentry cert expiry, placement health, `dapr_runtime_component_init_fail_total` | nothing (the control-plane scrape job was added for one metric and the rest ride in unused) |
| `lance_object_store_*` (S3/RustFS latency, errors, throttles) — already emitted by 5 services | nothing |
| 19 first-party instruments incl. `notifications_feed_gaps_total` and `outbox_events_poison_dropped_total` — **two silent data-loss counters** | neither panel nor alert |

And four defects in what *is* wired:

- **`outbox_oldest_age` names a series that does not exist.** Two instruments declare `unit="s"`;
  one is queried **with** the Prometheus unit suffix (`lineage_ingest_duration_seconds_bucket`,
  `perses-dashboards.yaml:129`) and the other **without** (`max(outbox_oldest_age)`,
  `perses-dashboards.yaml:237` and `rules.yml:23`). The suffix *is* applied on this ingest path —
  `docs/DEPLOY.md:82` records `http_server_duration_milliseconds_*` for an instrument declared
  `unit="ms"`. So **`LineageOutboxBacklogAging` can never fire**, and
  `docs/runbooks/RUNBOOK-oncall.md:34`, `:132` send the on-call to a no-data panel.
- **Three "went quiet" alerts go silent exactly when the emitting pod dies.** `rules.yml:172` and
  `:210` use `== 0` over a range, which evaluates over an *empty vector* once the series ages out.
  The repo already solved this for one metric (`absent(dapr_scheduler_sidecars_connected)`, `:281`)
  and never generalised it. Both need `or absent(...)`.
- **The seven SSR zones export nothing.** `grep -n "OTEL\|otel" chart/templates/frontends.yaml` →
  rc=1. Every browser request enters through a SvelteKit/Bun server *before* the gateway, and the
  RED dashboard's unfiltered `sum by (service_name)` renders their absence as *"these services do not
  exist"* rather than *"these services are unmonitored"*.
- **The alert-name gate covers 3 of 15 rules and 0 of 34 panel queries** (`test_invariants.py:3018`),
  and its mapping comment knows only "dots→underscores, counters gain `_total`" — not the UCUM unit
  suffix, which is the rule the `outbox_oldest_age` defect violates.

**All of it sits on top of `alerting.enabled: false`** (`chart/values.yaml:2083`). That is a recorded
open decision — `open_alert.md` owns it and is not re-litigated here. What this audit adds is a
scoping fact: **if that decision goes to "turn it on", the `absent()` guards and the unit-suffix name
fix must land first, or two of the fifteen will be armed and still unable to fire.** Land those two
regardless of which option wins — they are correctness fixes to a file that is checked in either way.

---

## 8. Two configurations in which the estate exports nothing at all

Both are latent, both are in the chart's own documented prod posture (`values-prod.yaml:218-220`).

1. **`rask.otelEnv` gates on `observability.enabled` alone** (`_helpers.tpl:235`), while
   `lance.otelEnabled` also honours `externalOtlpEndpoint` — and its own comment says
   *"otherwise externalize silently emits nothing"*. **Proven by render:** with
   `--set observability.enabled=false --set observability.externalOtlpEndpoint=…`, all six fleet
   deployments come back with **zero `OTEL_*` env** (gateway and compute render a bare `env: null`),
   so `setup_otel` returns `False` at `otel.py:50-51`. The entire request-facing half goes dark while
   the other half keeps exporting — so the gap reads as *"those services are idle"*.
2. **`lance.otlpEndpoint` prefers the in-cluster Collector over `externalOtlpEndpoint`**
   (`_helpers.tpl:493-497`, with `otelCollector.enabled: true` at `values.yaml:2061`) — but the
   Collector only renders under `and $o.enabled $c.enabled …` (`otel-collector.yaml:20`). **Proven by
   render:** the same command aims all seven lakehouse pods at `http://rask-otel-collector:4318`
   while `otel-collector objs: []` and `greptime objs: []`. It fails loudly nowhere: the SDK retries
   a refused connection with exponential backoff **in-process** — the exact pathology `otel.py:34-42`
   records costing ~2.7 s per unit test in CI.

**Combined, that configuration exports nothing, anywhere, from any of the 13 services**, and every
pod reports healthy.

**Fix:** point `rask.otelEnv` at `lance.otelEnabled` + `lance.otlpEndpoint` (which also collapses the
two-target split, the two resource schemas, and gives the fleet `k8sattributes` enrichment), and
reorder `lance.otlpEndpoint` so an explicitly-set external target always wins.

Two smaller chart-side items in the same area:

- **The Collector's OTLP exporters declare no `sending_queue` and no `retry_on_failure`**, and the
  `file_storage` extension that exists is wired to filelog only. A Collector restart during a
  GreptimeDB blip loses whatever is in flight. (The `otel` skill's collector reference is explicit:
  *"use the exporter's `sending_queue` + `file_storage`, not the `batch` processor"* — this chart uses
  bare `batch: {}` and no queue.)
- **The Collector runs one replica on the generic request-pod tier** (50m/128Mi req, 1cpu/512Mi lim)
  behind a 400 MiB `memory_limiter`, while being the sole path for all Dapr metrics, every infra
  pod's logs and 7 apps' three signals. Give it its own `resources.otel-collector` tier. Do **not**
  add a PodDisruptionBudget at `replicas: 1`.

---

## 9. What is already right — do not redo this

Stated so nobody spends a day re-deriving it:

- **The enable/disable decision is correct and defended.** An explicit `Settings` decides; the
  ambient endpoint is only a fallback (`otel.py:49-51`, pinned by `test_otel.py:31-51`). The old `or`
  — which made Dagger's injected endpoint an unturnoffable override that put the test suite to sleep
  in exporter backoff — is genuinely gone. **Do not re-open this.**
- **SDK imports are lazy** (`otel.py:53-63`), so a disabled service pays no import cost.
- **The signal-scoped OTLP headers are right, and subtly so:** generic headers carry db-name only;
  `OTEL_EXPORTER_OTLP_TRACES_HEADERS` adds GreptimeDB's pipeline. Metrics correctly do **not** carry it.
- **Propagation defaults need no code.** Measured: the global propagator is already
  `CompositePropagator[TraceContext, W3CBaggage]` with fields `['baggage','traceparent','tracestate']`.
  Setting `OTEL_PROPAGATORS` would only risk narrowing it.
- **Instrument naming is OTel-conformant across all 40+ `create_*` sites** — dotted, lower-case, no
  `_total` on the instrument, `unit=` always set.
- **Metric cardinality discipline is deliberate and documented as a security rule**, with label
  vocabularies closed by `StrEnum`. §6 is a hole *underneath* that discipline, not a violation of it.
- **Histogram buckets are tuned, not defaulted** (`lineage/core/metrics.py:31-40`).
- **The Collector's log-dedupe filter is keyed on the right discriminator** (`log.file.path`, not the
  label alone) — the 2026-08-15 fix that recovered the entire application log tier is correct. §5a is
  a *residual* of that design, not a regression of it.
- **`Resource.create` does absorb `OTEL_RESOURCE_ATTRIBUTES`** and auto-populates
  `service.instance.id`. No detector wiring is missing — only the values the chart renders.
- **The Collector's receiving half for §2 is already built**: `otlp` on 4317 **and** 4318, wired into
  the traces pipeline. The Dapr fix is a chart-side addition with no new infrastructure.

---

## 10. The work, ordered

Each slice is independently shippable and independently verifiable.

| # | Slice | Files | Effort | Why first |
| --- | --- | --- | --- | --- |
| **1** | `otel:` block on `lance-tracing` + the invariant that pins it | `chart/templates/observability.yaml`, `_helpers.tpl`, `tests/unit/test_invariants.py` | S | Turns a whole absent layer on; the receiving half already exists |
| **2** | grpc + aiohttp + requests instrumentors in `setup_otel` | `service-kit/pyproject.toml`, `otel.py`, one test | S | Without it, slice 1 gives disconnected sidecar spans on the fleet |
| **3** | `LoggerProvider` in `setup_otel` | `otel.py` (~8 lines) | S | Third signal for 6 services; no chart change; fixes the audit trail |
| **4** | `spec.metrics` + `spec.logging.apiLogging` | `chart/templates/observability.yaml` | S | **Stops publishing user subjects as metric labels.** Arguably slice 1 |
| **5** | Narrow the log-drop filter; `log-as-json`; filelog parser chain | `otel-collector.yaml`, `_helpers.tpl` (+ fix the false comment at `:621-623`) | M | Recovers crash logs on 7 pods and gives the whole file-tailed tier a severity |
| **6** | Converge `rask.otelEnv` onto `lance.otlpEndpoint`/`lance.otelEnabled`; one resource-attribute helper; reorder `lance.otlpEndpoint` | `_helpers.tpl`, `tests/unit/test_invariants.py` | M | Closes both §8 dark configurations, both resource schemas, and the k8sattributes gap in one edit |
| **7** | Spans + metrics at the four unnamed seams: publish funnel, actor proxy, FGA check, cron ticks | `dapr_publish.py`, `proxies.py`, `governed/fga.py`, `reconcile_cron.py` | M | Domain names that survive a future missing instrumentor |
| **8** | Workflow tracing + a `workflows.json` dashboard | `flows/`, `ingest/`, `medallion/`, `perses-dashboards.yaml` | M | Depends on slice 2 |
| **9** | Alert correctness: `absent()` guards, the `outbox_oldest_age` name, generalise the name gate | `chart/alerting/`, `test_invariants.py` | S | Land regardless of how `open_alert.md` resolves |
| **10** | Dashboards for what is already collected: storage, workflows, pub/sub health, cron, control plane | `perses-dashboards.yaml` | M | Zero instrumentation cost — the series exist today |
| **11** | Shutdown flush; sampler env; probe exclusion; metric-interval symmetry; `service.version` | `otel.py`, `service_kit/__init__.py`, `_helpers.tpl` | S | Polish, but cheap |
| **12** | SSR zones export nothing | `chart/templates/frontends.yaml` + a Node SDK preload | M | The first hop of every real user request |

**Slices 1–4 are the ones that change what can be seen.** They are all small, and three of the four
are chart-only.

---

## 11. What is NOT in this file

- **The alerting decision.** `open_alert.md` owns it. §7 only records the dependency.
- **Ray.** `open_ray_otel.md`. The one overlap is recorded there: `rayservice.yaml:27-33` is a
  **fourth** hand-rolled OTel wiring with a hardcoded release name and a workload literal.
- **The frontend zones' internals** beyond "they export nothing" — that is a `rask-frontend` concern.
- **`.claude/skills/rask-architecture` says `make_service_app` is used by 3 services.** It is used by
  **5** (controlplane, compute, ingest, flows, notifications) plus the gateway calling `setup_otel`
  directly. Per CLAUDE.md, fix the skill in whichever commit touches this area first.

---

**Sources.** Upstream contract read 2026-08-22 and verified against **dapr/dapr v1.18.1**, the version
`chart/Chart.yaml:63` pins — not against the docs, which drift (four published control-plane metric
names do not exist in v1.18.1, and the error-code metric is named differently in two Dapr docs and a
third way in source).

- <https://docs.dapr.io/operations/observability/tracing/otel-collector/open-telemetry-collector/>
- <https://docs.dapr.io/operations/observability/tracing/setup-tracing/>
- <https://docs.dapr.io/reference/resource-specs/configuration-schema/>
- <https://docs.dapr.io/operations/observability/metrics/metrics-overview/>
- <https://docs.dapr.io/operations/observability/logging/logs/>
- <https://oneuptime.com/blog/post/2026-03-31-dapr-opentelemetry-distributed-tracing/view>
- Skill references: `otel/references/{signals,attributes,python-sdk,collector}.md`,
  `python-infrastructure/references/observability.md`, `fastapi/references/{observability,microservices}.md`
