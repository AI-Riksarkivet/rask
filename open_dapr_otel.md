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

> **RE-MEASURED 2026-08-23, AFTER slices 2/3/6 DEPLOYED (helm rev 44) — THE THREE BULLETS BELOW ARE
> LARGELY OBSOLETE.** They were written against a pre-slice-2 estate. Live traces now show one unbroken
> tree: HTTP door -> FGA -> publish -> bus -> 3-way fan-out -> actor invoke -> actor state. The sidecar
> already emits `CallActor/InboxActor/Deliver` under the EXACT name bullet 2 proposes; the publish hop
> carries two spans (grpc instrumentor + sidecar) with `messaging.system`/`messaging.destination.name`
> already populated; and the FGA hop has aiohttp CLIENT spans in both planes plus the SDK's own
> `fga_client_request_duration_milliseconds_*`. See §10b for what actually survived.

**Then give the hops names the estate owns**, so a future missing instrumentor degrades to
"unparented span" rather than "no span":

- `dapr_publish.publish_event` (`service_kit/dapr_publish.py:49-70`) is the **single funnel for all
  13 Dapr publish sites** (11 under `services/`, 2 under `packages/`; raw JetStream publishes bypass it by invariant I3) and emits no span, no counter, no histogram. One `PRODUCER` span +
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
| `dapr_component_input_binding_count_total` | nothing — **5 rendered cron bindings, all measured; the gap is that nothing READS the series** |
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
| **5** | Narrow the log-drop filter; `log-as-json`; filelog parser chain | `otel-collector.yaml`, `_helpers.tpl` (+ fix the false comment at `:697-702`) | M | Recovers crash logs on 10 pods and gives the whole file-tailed tier a severity |
| **6** | Converge `rask.otelEnv` onto `lance.otlpEndpoint`/`lance.otelEnabled`; one resource-attribute helper; reorder `lance.otlpEndpoint` | `_helpers.tpl`, `tests/unit/test_invariants.py` | M | Closes both §8 dark configurations, both resource schemas, and the k8sattributes gap in one edit |
| **7** | **MOSTLY RULED OUT — see §10b.** 3 of 4 seams are already covered by the sidecar + slice 2; what survived is 7a (the cron overlap-skip + the gap series) and 7b (the pre-I/O publish refusal) | `notifications/api/{metrics,reconcile_cron}.py`, `service_kit/bus_metrics.py`, `dapr_publish.py` | S+XS | see §10b |
| **8** | **RESPECIFIED — see §10a.** Eight slices (8.0, 8a–8g); two are CORRECTNESS bugs, one is blocked on a measurement | `flows/`, `ingest/`, `medallion/`, `perses-dashboards.yaml`, `alerting/` | — | see §10a |
| **9** | **LANDED (the correctness half).** `absent()` guards on both `== 0` rules, `outbox_oldest_age` -> `outbox_oldest_age_seconds`, and the name gate taught UCUM units + multi-source. **RULED OUT:** anything gated on turning `alerting.enabled` on — that is `open_alert.md`'s decision, not this audit's | `chart/alerting/`, `test_invariants.py` | S | — |
| **10** | **LANDED.** storage.json + messaging.json over series already collected, every query proven against the live store; workflows.json shipped with 8g. Panel queries are now GATED, which caught a phantom the alert fix had missed | `perses-dashboards.yaml`, `test_invariants.py` | M | — |
| **11** | Shutdown flush; sampler env; probe exclusion; metric-interval symmetry; `service.version` | `otel.py`, `service_kit/__init__.py`, `_helpers.tpl` | S | Polish, but cheap |
| **12** | SSR zones export nothing | `chart/templates/frontends.yaml` + a Node SDK preload | M | The first hop of every real user request |

**Slices 1–4 are the ones that change what can be seen.** They are all small, and three of the four
are chart-only.

---

## 10a. Slice 8, respecified — the per-workflow audit

**Supersedes the single row `| 8 | Workflow tracing + a workflows.json dashboard | flows/, ingest/, medallion/, perses-dashboards.yaml | M | Depends on slice 2 |`.** That row was one blob answering *"did a workflow run"*. The measurement below says the free tier already answers that, and that the questions actually unanswered are per-activity, per-outcome, and — in two places — not telemetry questions at all.

**Re-measured 2026-08-23 09:00 UTC** against k3s + GreptimeDB and against `.venv/…/dapr/ext/workflow/_durabletask/`. Registered surface is **6 workflows / 21 activities / 34 `call_activity` sites**, not 8/22: `flows/workflow.py:45` (`flow_run`) + `flows/activities.py:24` (`run_node`); `ingest/workflow.py:1119` `WORKFLOWS = (ingest_run, chunk_run)` + `:1175` 10 activities; `medallion/workflow.py:1091` `WORKFLOWS = (stage_run, train_run, promotion_review)` + `:1093` 10 activities. `grep -cE "start_as_current_span|get_tracer|get_meter|add_event|record_exception|create_counter|create_histogram"` over all four files → **0, 0, 0, 0**.

---

### 8.1 What is already free — do not instrument any of this

Four things arrive with no code change. Three of them were specced as work in the audits that fed this section, and that work is struck.

**(a) Ten sidecar metric families, 22 GreptimeDB tables, bounded labels.** Verified by `desc table`, not by memory:

| family | labels beyond `app_id` + k8s/infra tags |
| --- | --- |
| `dapr_runtime_workflow_execution_count_total` / `_latency_{bucket,count,sum}` | `workflow_name`, `status` |
| `dapr_runtime_workflow_scheduling_latency_*` | `workflow_name` (no status) |
| `dapr_runtime_workflow_payload_size_ratio_*` | `workflow_name` |
| `dapr_runtime_workflow_operation_count_total` / `_latency_*` | `operation`, `status` — `create_workflow`, `purge_workflow`, … |
| `dapr_runtime_workflow_activity_execution_count_total` / `_latency_*` | `activity_name`, **`status`** |
| `dapr_runtime_workflow_activity_operation_count_total` | `activity_name`, `status` |
| `dapr_runtime_workflow_activity_payload_size_ratio_*` | `workflow_name`, `activity_name` |

The `status` label on the **activity** families genuinely records failure. Measured today:

```
ingest | ensure_dataset | failed | 4      ← a full ACTIVITY_RETRY exhaustion, counted, by name
ingest | emit_start / resolve_limits / emit_terminal | success | 1
flows  | run_node | success | 258
```

**(b) A per-app orchestration-turn counter and a lost-reminder signal.** `dapr_runtime_actor_reminders_fired_total{app_id, actor_type, success}` carries the workflow engine's *own* internal actor types:

```
flows            | dapr.internal.default.flows.workflow            | true | 260   (vs 2 flow_run executions)
bronze-to-silver | dapr.internal.default.bronze-to-silver.workflow | true |  15   (vs 3 stage_run executions)
ingest           | dapr.internal.default.ingest.workflow           | true |  11
```

Fires climbing while `execution_count_total` stays flat **is** a wedged `continue_as_new` loop. Alongside it: `dapr_scheduler_jobs_undelivered_total`, `dapr_scheduler_trigger_latency_{bucket,count,sum}`, `dapr_scheduler_jobs_triggered_total` — a lost durable reminder and timer drift, free, at app granularity.

**(c) One span per activity execution.** `.venv/lib/python3.13/site-packages/dapr/ext/workflow/_durabletask/worker.py:982-995` is the only `start_as_current_span` in the SDK: `activity: <name>`, parented on `req.parentTraceContext.traceParent`, attributes `…task.instance_id`, `…task.id`, `…activity.name`. **The workflow instance id is already on a span today.**

**(d) gRPC client spans to the sidecar**, instrumented twice: `_durabletask/client.py:38-44` calls `GrpcInstrumentorClient().instrument()` at import, and `service-kit/src/service_kit/otel.py:143-152` already documents that the second call is a no-op.

**STRUCK from the surviving gap list, because (a)–(d) cover them:**

| struck claim | what actually exists |
| --- | --- |
| "no activity retry/failure count; nothing can be alerted on" | `activity_execution_count_total{activity_name,status="failed"}` — a `MaintenanceDatasetsFailing`-shaped rule is writable today |
| "per-workflow/activity execution counts and latency must be built" | 22 tables, live |
| "scheduling latency / payload size must be built" | free, and non-obvious to reimplement |
| "`continue_as_new` turn count is unobservable" | (b) — app-granular, real |
| "a reminder lost by the scheduler is invisible; timer drift invisible" | `dapr_scheduler_jobs_undelivered_total` + `_trigger_latency_*` |
| "an operator cannot pivot from a slow cascade to the instance responsible" | the instance id is on every `activity:` span (c) |
| "add a span around each activity body" | one already exists — set attributes on it, never open a second |

**One caveat, and it is not a licence to reimplement.** These families are **lazily registered**. Scraped today from inside `rask-ingest-6f654c65bc-zs945`: **563** live `dapr_` series, **0** `dapr_runtime_workflow_*`, **0** `dapr_runtime_actor_reminders_fired`. Absence is not zero — it is "no workflow event since this pod started". Every rule written against these families needs the `absent()` guard slice 9 already owns.

---

### 8.2 The gaps that survive, ranked

| # | gap | the operational question it answers | sev |
| --- | --- | --- | --- |
| G1 | **The run-level `status` label is false for this codebase.** All three services convert failure into a *returned value*: `flows/workflow.py:103-107` returns `RunState(status="failed")`; `ingest/workflow.py:441` opens an error boundary that returns `RunOutcome(status="FAILED")`; medallion returns outcome dicts. Measured: `execution_count_total` holds `status="success"` **and nothing else** for all four app_ids — including `ingest_run`, whose `ensure_dataset` has only a `failed` row. | "How many runs failed?" The free metric answers 0 forever. An alert on `status="failed"` reads green while every run dies. | critical |
| G2 | **No orchestration span — but UNVERIFIED post-slice-1.** See the blocker in §8.7. | "How long did this run take, which branch did it take, where did it stall?" | critical (blocked) |
| G3 | **The free activity span can never be ERROR.** `worker.py:1086` puts `try/except Exception → _build_activity_failure_response` *inside* `with self._activity_span(...)`; `grep -rn "record_exception\|set_status" _durabletask/` → **nothing**. Measured: 327/327 `activity: *` spans `STATUS_CODE_UNSET`, including the 4 the sidecar metric labels `failed`. | "Show me error spans in the last hour." Clean estate, always. | high |
| G4 | **No domain identity on any workflow signal.** The free span carries 3 SDK attributes; the free metrics carry names and a status. No run_id, dataset, project, stage, submission_id, node id. | "Did the bronze→silver stage for project *acme* fail?" — only "a `stage_run` failed somewhere". | high |
| G5 | **`flows` has no failure signal at all.** `grep -rnE "publish_event\|lineage\|emit_control\|pubsub\|notif" services/flows/src/` → nothing; no metrics module; `workflow.py` has no logger. The only failure record in the service is `activities.py:51 log.info("node %s failed: %s", …)` — INFO, no run id. | "Did any flow run fail in the last hour, and whose?" | high |
| G6 | **The medallion outcome counter its own docstring promises does not exist.** `workflow.py:379`: *"The record is the log line and the counter."* `workflow.py` imports no metrics module; `core/metrics.py` has no stage-job-outcome counter. | "Is the cascade failing more than usual?" | high |
| G7 | **Four bare `suppress(Exception)` on the reporting path** — `workflow.py:414, 461, 707, 1073` — against the same service's `transform.py:80-99 _best_effort`, which logs. | "The stage failed — why is there no FAIL in the graph?" — nothing distinguishes *published* from *dropped*. **CORRECTNESS, see §8.7.** | high |
| G8 | **ingest fan-out progress is frozen.** `set_custom_status` only at `workflow.py:525` (before fan-out) and `:650` (after fan-in); `chunk_run` (`:704`) sets none; `ChunkResult` (`:308-314`) has no `units_done`. | "Is this 4-hour harvest progressing, or are all 500 children wedged?" API says "0 of N" throughout. | medium |
| G9 | **The measured duration is discarded on every non-success.** `workflow.py:231` and `:239` both set `duration_seconds=_watch_seconds(...)`; `report_stage_outcome` (`:374-427`) never reads it. The histogram is fed only via `publish_stage_ready` → the success path. | "Is p95 stage latency degrading?" — survivorship-biased by construction. | medium |
| G10 | **A `flows` run does not record which lane executed it.** The lane is decided per request at `routes.py:170 if scheduler is not None:` and announced once per process at `lifespan.py:114/:119/:123`. Only the degrade branch (`routes.py:200`) logs per run. | "Did *this* run execute durably or inline?" Unanswerable; latency inverts (a durable schedule may burn `5.0 + 2.0 s` at `lifespan.py:40/45` while an inline text graph returns in 4 ms). | medium |
| G11 | **No workflow panel, no workflow alert.** `grep -cE "workflow\|activity\|orchestrat" chart/alerting/rules.yml` → **0** across 20 rules; `chart/templates/perses-dashboards.yaml` declares 8 dashboards and matches nothing. | "Page me when the cascade stops." Even the free families are read by nobody. | medium |

---

### 8.3 The work — seven slices, and why it cannot be one

Splitting is not stylistic. **8a and 8b are correctness fixes that change what "add a span" would even mean** — instrumenting a producer whose event the consumer discards buys a span on a message nobody receives. **8d is blocked on a measurement that has not been possible since slice 1 landed.** **8c and 8g are shippable today with no cluster.** One blob makes the shippable half wait on the blocked half.

| # | slice | files | size | depends |
| --- | --- | --- | --- | --- |
| **8.0** | Exercise each lane once, re-measure, then decide 8d | runbook only + one docstring repair | S | slice 1 (landed) |
| **8a** | The train-watcher's FAIL names nobody (**CORRECTNESS**) | `medallion/workflow.py`, 2 tests | S | — |
| **8b** | The reporting path swallows its own failures (**CORRECTNESS**) | `medallion/workflow.py` | S | — |
| **8c** | One outcome metric per lane, at the terminal activity | `medallion/core/metrics.py` + `workflow.py`; new `flows/metrics.py`; new `ingest/metrics.py` | M | — |
| **8d** | One orchestration span at the **schedule** site | 5 schedule sites | M | **8.0** |
| **8e** | Domain attributes + ERROR status on the activity's own span | 3 modules | M | — |
| **8f** | ingest fan-out progress | `ingest/workflow.py` | S | — |
| **8g** | `workflows.json` panel + 3 alert rules | `perses-dashboards.yaml`, `alerting/rules.yml`, `test_invariants.py` | S | 8c |

---

#### 8.0 — MEASURE FIRST (S)

Trigger one `flow_run` (`POST /api/flows/runs`), one `ingest_run` (`POST /v1/ingests`), and one `stage_run` (needs §8.7 blocker 3 resolved, or record it UNMEASURED). Then re-run three queries: `span_name LIKE '%||%'`, `scope_name='durabletask'`, and the `dapr-diagnostics` span-name census.

**Decision gate.** If daprd 1.18.1 now exports `orchestration||<name>` / `create_orchestration||<name>` / `activity||<name>` / `timer`, **8d is cancelled** and G2 shrinks to "the run's *domain verdict* is on no span" (covered by 8e). If it exports none, 8d proceeds and the upstream cause is `dapr/durabletask-go backend/orchestration.go:418 if ptc == nil { return helpers.NoopSpan() }` plus `endWorkflowSpan`'s `helpers.CancelSpan(span)`.

**RED test:** none — this is a measurement, and writing a test for an unmeasured absence is exactly what this section is replacing.
**REPAIR:** `tests/unit/test_invariants.py:2247` `test_dapr_sidecar_spans_actually_have_an_exporter` — its docstring lists *"workflow steps"* among the hops that "produced no span anywhere". Untrue: the Python SDK's 327 `activity: *` spans reached the store through the **app's** exporter, never daprd's. Fix the sentence in whichever commit lands 8.0.

---

#### 8a — the train-watcher's FAIL names nobody (S) — CORRECTNESS

`medallion/workflow.py:716-762` hand-builds its event (`:735 "eventType": "FAIL"`) with `facets = {lance, errorMessage}` and **no `author`**. It publishes to the **bus** via `outbox.publish_lineage_with_outbox(... topic_name=settings.lineage_topic)`, and the bus path applies neither `enforce_author` nor the HTTP door's checks (`lineage/api/fga_deps.py:176-178` says so in terms). `notifications/api/lineage_events.py:197-198` returns `None` before `originator_subject()` is read at `:208`.

**Executed today against the real modules, both shapes:**

```
train-watcher FAIL  -> None
with author facet   -> author= data_eng originator= alice project= acme
```

The `originator` and `project` this lane threads from `/train` all the way down are discarded at the last link. Per `.claude/skills/rask-notifications`, the plane then **SUCCESS-acks** it, so nothing reports the loss.

**Fix:** route `_publish_train_fail` through `build_run_event(...)` (`medallion/schemas/events.py:159`), which stamps `run_facets["author"]` at `:256-257`. This also drags the site into the AST scan below, closing both holes in one edit.

**RED test (new, `services/medallion/tests/test_train_workflow.py`):** build the event exactly as `_publish_train_fail` does, feed it through the real `notifiable()`, assert non-`None`. Verified to fail today (output above). Cross-plane import works in the root venv (`from notifications.api.lineage_events import notifiable` alongside `from medallion...`).

**REPAIR, two — both are green tests asserting the wrong thing:**

1. `services/medallion/tests/test_train_workflow.py:134` `test_the_watcher_NAMES_the_person_the_run_was_for`. Its docstring is exactly right — *"a FAIL that names nobody is discarded by the plane's own rule, so the whole lane would be a producer whose output the consumer is designed to drop"* — and then it asserts only `reported["spec"]["originator"] == "alice"`: the **input handed to the activity**, never the **event the activity builds**. It proves the wrong half of its own docstring.
2. `services/medallion/tests/test_producer_targeting_contract.py:67-83` `_emit_sites()` walks only `ast.Call` nodes whose func id is `build_run_event`. `workflow.py:735` is the **only** hand-built lineage event in the service (`grep -rn '"eventType"' services/medallion/src/` → that line and `ingest_trigger.py:51`, a reader), so both targeting gates (`:86`, `:131`) are structurally blind to the one site that fails them. Worse, the non-vacuity guard `:159 test_the_targeting_scan_sees_every_hop_of_the_cascade` asserts `"workflow.py" in files` and **passes**, because `workflow.py`'s other two emits (`:491`, `:1042`) do use the helper. Widen the scan to any dict literal carrying an `"eventType"` key, or assert the module has no hand-built events.

---

#### 8b — the reporting path swallows its own failures (S) — CORRECTNESS

Two defects, one file.

**(i) Four bare suppresses.** `medallion/workflow.py:414, 461, 707, 1073` — no log, no counter, no re-raise. The same service already solved this: `services/transform.py:80-99 _best_effort`, whose own comment reads *"What WAS a defect is that `with suppress(Exception)` threw the diagnosis away with the exception"*, logging at `:99`. `emit_promotion_outcome` (`:1026-1074`) is the worst case — it logs nothing at all, and its docstring calls workflow history *"a cache; lineage is the durable record"*.

**(ii) The unguarded reporting sites.** `ACTIVITY_RETRY` (`:71-76`, 5 attempts) exhaustion raises into the generator at `:194, :235, :263, :265` (`report_stage_outcome`), `:665` (`report_train_outcome`) and every promotion site (`:810, :825, :860`, …). The guarded sites are `:189, :213, :255` only.

**A nuance worth writing down before anyone alerts on it:** these unguarded sites are the *only* paths on which the free `execution_count_total{status="failed"}` is reachable at all. So that label does not mean "the run failed" — it means "an activity exhausted its retries at a site with no boundary". That is a second reason G1 needs a rask-owned outcome metric.

**RED test (new, `services/medallion/tests/test_stage_workflow.py`):** monkeypatch `_publish_fail_event` to raise; assert a log record is emitted naming the suppressed emit. Fails today — the suppress is bare.
**REPAIR:** `services/medallion/tests/test_stage_workflow.py:373` `test_a_lineage_OUTAGE_does_not_fail_the_reporting_activity` already drives the outage and asserts only that the activity does not raise. Extend it to assert the outage is *reported*, rather than adding a parallel test.

---

#### 8c — one outcome metric per lane (M)

The single highest-value item, and it is one counter per service, not a tracing project. Emit at the **terminal activity** (never the orchestrator body — see §8.6):

| service | site | metric |
| --- | --- | --- |
| medallion | `report_stage_outcome` (`workflow.py:374`), `report_train_outcome` (`:686`), `emit_promotion_outcome` (`:1026`) | `medallion.stage.outcome{verdict}`, `medallion.train.outcome{verdict}`, `medallion.promotion.outcome{decision}` in the existing `core/metrics.py` |
| medallion | `:231`/`:239` already compute `_watch_seconds` and throw it away | record `medallion.stage.duration` on the **non-success** paths too, reusing `_STAGE_DURATION_BUCKETS` (`core/metrics.py:57`) — closes G9 without a new histogram |
| flows | `routes.py:205` (durable) and `:208` (inline) | new `flows/metrics.py`: `flows.runs{lane}`; plus `flows.nodes{status}` at `activities.py:44` — **one counter closes G5 and G10** |
| ingest | `emit_terminal` (`workflow.py:1053`) | new `ingest/metrics.py`: `ingest.runs{status}`, `ingest.units{outcome}` |

`verdict` / `status` / `lane` / `decision` are closed sets read from the source (`StageJobOutcome.verdict` ∈ {succeeded, failed, abandoned, unnotified}), which is the cardinality rule in §8.6. Follow `medallion/core/metrics.py` exactly — the `lance.*` meter name, the `{transition}`-style units, the second-scale buckets whose header records having been bitten by the SDK's 10 s default.

Ride along: `medallion/mover.py:71-88` has no `else` on `if settings.ray_enabled:`, so a mover hosting **zero** workflow workers logs neither the start line nor the fallback — unlike `flows/lifespan.py:123`, which announces its negative case. One `else: log.info(...)`.

**RED test (new, one per service):** `services/flows/tests/test_routes.py` — drive a run with `scheduler=None` and assert `flows.runs{lane="inline"}` incremented (in-memory `MetricReader`). `services/medallion/tests/test_stage_workflow.py` — assert `report_stage_outcome` records a `failed` verdict and a duration.
**REPAIR:** `services/medallion/tests/test_stage_workflow.py:169` `test_a_TERMINAL_BAD_job_does_NOT_wake_the_mover` and `:356` `test_an_ABANDONED_watch_also_reaches_the_graph` both already drive the exact paths; extend them rather than duplicating the fixtures. Also fix `workflow.py:379`'s docstring, which promises a counter that does not exist.

---

#### 8d — one orchestration span at the SCHEDULE site (M) — GATED ON 8.0

Only if 8.0 shows daprd still exports nothing. A `CLIENT` span at the five schedule sites, so the HTTP door and the run share a trace:

`ingest/__init__.py:292`, `medallion/services/transform.py:146`, `medallion/services/train.py:331`, `medallion/api/promotions.py:145`, `flows/lifespan.py:232`.

**Not in the orchestrator body.** It replays; a span per replay is both wrong and unbounded. This is why the site is the scheduler, not the workflow.

**RED test:** an in-memory span exporter asserting a `CLIENT` span carrying `lance.workflow.instance_id` exists after a schedule call, and that the run's activity spans resolve their parent inside it.
**REPAIR:** none — no test covers this today.

---

#### 8e — domain attributes + ERROR on the activity's own span (M)

Do **not** open a second span. Take `trace.get_current_span()` inside each activity body — it is the SDK's `activity: <name>` span — and set the domain attributes the SDK cannot know, plus `record_exception` / `set_status(ERROR)` on the failure path. Precedent, already in this service: `medallion/services/transform.py:429-432` writes `lance.medallion.transition`, `lance.lineage.run_id`, `lance.lineage.chain_depth`.

Per lane: `lance.medallion.stage` / `lance.medallion.submission_id` / `lance.medallion.verdict`; `lance.ingest.run_id` / `lance.ingest.chunk_id` / `lance.dataset`; `lance.flows.node_id` / `lance.flows.node_kind`. G3's fix belongs here and not in the SDK — `flows/activities.py:46-52` and the medallion outcome paths *deliberately* return failure rather than raising, so the exception never reaches the SDK's `with` block to be recorded.

**RED test:** in-memory exporter — assert a failing `run_node` produces a span with `StatusCode.ERROR` and `lance.flows.node_id`. Fails today (327/327 spans `UNSET`).
**REPAIR:** none.

---

#### 8f — ingest fan-out progress (S)

`set_custom_status` inside `chunk_run` (`workflow.py:704`) plus a `units_done` field on `ChunkResult` (`:308-314`) so the parent can aggregate live. Today the API reports "0 of N" for the whole fan-out because `runs.py:369` derives `units_done` from the terminal output's `rows`, which does not exist until `finalize` returns — while `workflow.py:486-489` claims the opposite ("*the API could say '4 done' and never '4 of 500'*").

Custom status, not a metric: it is per-instance state, read by the run's own GET.

**RED test:** drive a fan-out with a fake context; assert the parent's status advances between the first and last child.
**REPAIR:** `services/ingest/tests/test_run_status.py` — it already covers the status surface.

---

#### 8g — the panel and three rules (S)

`workflows.json` in `chart/templates/perses-dashboards.yaml` over the free families (§8.1a/b) plus 8c's outcome counters. Three rules in `chart/alerting/rules.yml`, all `absent()`-guarded per slice 9:

1. `sum(increase(dapr_runtime_workflow_activity_execution_count_total{status="failed"}[30m])) > 0` — writable **today**, no instrumentation, exactly the shape of the existing `MaintenanceDatasetsFailing`.
2. `<lane>.outcome{verdict!="succeeded"}` rising (needs 8c).
3. Reminder fires climbing while `execution_count_total` is flat — the wedged `continue_as_new` detector from §8.1b.

**REPAIR:** the alert-name gate in `tests/unit/test_invariants.py` (slice 9 generalises it) must cover the new rules.

---

### 8.0 RESULT — measured 2026-08-23, after a lane was exercised

**8d IS CANCELLED.** daprd now exports every workflow span the audit said it did not, because slice 1
gave the sidecar an exporter and no workflow had run since. All four predicted names are live:

| scope | span | kind |
| --- | --- | --- |
| `durabletask` | `create_orchestration\|\|<workflow>` | CLIENT |
| `durabletask` | `orchestration\|\|<workflow>` | SERVER |
| `durabletask` | `activity\|\|<activity>` | SERVER |
| `durabletask` | `timer` | INTERNAL |

Hand-rolling a CLIENT span at the five schedule sites would now duplicate
`create_orchestration||<workflow>` exactly — the same waste slice 7 was cut for. The audit's "zero
orchestration spans across 18,547,561 spans" was a PRE-EXPORTER measurement and is retired.

**8e SURVIVES, NARROWED.** Status codes in the same window:

```
durabletask                            STATUS_CODE_UNSET  73    STATUS_CODE_ERROR  1
dapr.ext.workflow._durabletask.worker  STATUS_CODE_UNSET  46    STATUS_CODE_ERROR  0
```

The single ERROR is on `orchestration||promotion_review`, message *"Activity task #13 failed: catalog
refused the publish of 'acme-silv…"*. So daprd DOES mark the ORCHESTRATION span when an activity
**raises** — that half needs nothing. Two halves remain, and neither can come from the sidecar:

* **A RETURNED failure marks nothing, anywhere.** All three services convert failure into a returned
  value, so daprd sees an activity that completed and every span stays UNSET. That is the common case
  here, not the exceptional one.
* **No span carries a domain attribute.** The SDK span holds `task.instance_id`, `task.id` and
  `activity.name`; daprd's holds its own. Neither knows the dataset, stage, run or node — so a trace
  answers "an activity ran" and never "which stage of which project".

Do NOT add `record_exception` for the raising path (daprd owns it) and do NOT open a second span
(three already exist on that hop).

---

### 8.4 DROP — unverified, free, or not worth the cardinality

- **The `timer` span, a `dapr_runtime_workflow_timer_*` metric, and any per-timer telemetry.** No such family exists upstream; the drift and lost-reminder questions are answered free by `dapr_scheduler_trigger_latency_*` and `_jobs_undelivered_total`.
- **A `continue_as_new` turn counter.** Free (§8.1b).
- **Any per-activity duration histogram or execution counter.** Free, with a working `status` label.
- **Any rule over `dapr_runtime_workflow_activity_operation_latency`.** Declared in `dapr/dapr pkg/diagnostics/workflow_monitoring.go`, **no table in GreptimeDB** — never recorded here. Do not write against it.
- **A "children outstanding" gauge for `chunk_run`.** 8f's custom status answers it with no series and no cardinality.
- **Suppressing the `/TaskHubSidecarService/Hello` keepalive.** Real (34,393 of 35,196 grpc-scope spans) but it is a **collector** concern — hand it to slice 5/10, not to workflow instrumentation. The two audits that measured its share disagree by 40 points; whoever takes it should re-measure.
- **"How many promotions are waiting on a human right now."** Needs a listing endpoint (`promotions.py` exposes only per-token `show`) — a product decision, not telemetry. Defer.
- **A guard rejecting non-personal inbox principals** (`team:eng`, `service-web` holding inboxes). `.claude/skills/rask-notifications` owns it; out of scope here.
- **Treating the movers' missing workflow runtime as critical.** `use_ray = settings.ray_enabled` (`transform.py:433`) gates the **dispatch** with the same flag that gates the runtime (`mover.py:72`), so nothing is orphaned — the lane is coherently OFF. Only the missing `else:` log survives, and it rides in 8c.

---

### 8.5 What none of this touches

The estate has never run a `chunk_run`, `train_run` or `promotion_review` to completion — none appears in `execution_count_total` — so 12 of 21 activities have never emitted an observation of any kind. **That is a coverage gap, not an instrumentation gap.** Say so in the slice notes rather than letting the dashboard's empty panels read as "instrumented and healthy".

---

### 8.6 Cardinality — the rule, and the one that has already burned this estate

**§6 of this file records the estate minting a metric series per object id** through daprd's default `spec.metrics`. Slice 4 fixed that at the sidecar. **Do not reintroduce it one layer up.**

| signal | carries | rule |
| --- | --- | --- |
| **Metrics** | closed enums only: `workflow_name`, `activity_name`, `verdict`, `status`, `lane`, `transition`, `decision` | A label's domain must be enumerable **from the source**, not from the data. If you cannot write the full value list in the metric's `description`, it is not a label. |
| **Spans** | every identifier: `lance.workflow.instance_id`, `lance.ingest.run_id`, `lance.medallion.submission_id`, `lance.dataset`, `lance.project`, `lance.flows.node_id` | Attributes are per-span, not per-series — unbounded values are correct here. Precedent: `transform.py:429-432`. |
| **Logs** | unbounded text: error messages, Ray driver output, the `errors_total` dict, tracebacks | `extra=` survives to `log_attributes` (measured: `polls`, `status`, `submission_id`, `topic`). |

**NEVER a metric label, no exceptions:** a workflow instance id, a run id, a submission id, a dataset/table URI, a node or chunk id, a token, a subject or originator, an error message, or **any part of an activity input payload**.

**The replay rule, which is a cardinality rule too.** Never emit a metric or open a span from an **orchestrator body** — it replays, so a counter there over-counts by the replay factor and a span there is unbounded. Metrics go in **activities**; the orchestration span goes at the **schedule site**. Any log inside a body stays behind `if not ctx.is_replaying`, as `medallion/workflow.py` already does at `:191, :216, :233, :240, :261, :643, :655, :661` — and as `flows/workflow.py` does zero times, because it has no logger at all.

---

### 8.7 Blockers, and the two gaps that are correctness bugs

**BLOCKER 1 — no workflow has executed since the sidecar exporter landed. This gates 8d.**
`kubectl get configuration lance-tracing --show-managed-fields` shows `helm` owns `spec.tracing.otel`, last written **2026-08-23T08:19:49Z**; the older `kubectl-patch` entry (2026-08-15) owns **only** `spec.workflow.stateRetentionPolicy`. Meanwhile the newest workflow-worker span is **2026-08-17 22:55** and the newest workflow metric **2026-08-19 06:00** — and every live sidecar carries 0 workflow families against 563 `dapr_` series. daprd *is* exporting right now (scope `dapr-diagnostics`, 11,454 spans, newest 2026-08-23 08:56: `CallLocal/…`, `pubsub/lineage.events.v1`, `bindings/…`). **Therefore every "daprd emits no orchestration span" measurement in the feeding audits is pre-exporter and cannot be relied on.** Run 8.0 before writing 8d.

**BLOCKER 2 — the telemetry store is unhealthy and nothing alerts on it.** `rask-greptimedb-standalone-0`: `OOMKilled`, exit 137, restarts **8 → 9 during this session**, 8 Gi limit, container lifetimes as short as 26 s. Queries died mid-measurement. Assume unquantified loss behind every count in this section, and treat "the series is missing" as ambiguous until the store is stable. No rule in `chart/alerting/rules.yml` covers it.

**BLOCKER 3 (soft) — `stage_run` cannot be exercised here.** `core/config.py:225 ray_enabled: bool = Field(default=False, alias="MEDALLION_RAY_ENABLED")` and the variable is unset on all three movers, so neither the runtime (`mover.py:72`) nor the dispatch (`transform.py:433`) is on. 8.0 either enables it deliberately or records `stage_run` as UNMEASURED — it must not be inferred from the bronze→silver metrics, which are four days old.

**CORRECTNESS, not telemetry — fix before instrumenting, because "add a span" would otherwise mean "instrument a message nobody receives":**

1. **8a — the train-watcher's FAIL is undeliverable by construction.** The lane exists precisely so a person hears about a dead GPU job (`workflow.py:585-588`: *"Ray knows; nobody else does; and the person who asked is told nothing, ever"*), and it emits an event `notifiable()` returns `None` for — then SUCCESS-acks. Proven by execution today.
2. **8b — the FAIL emits are suppressed without a trace.** A lineage or NATS outage destroys every workflow failure in the window, and afterwards nothing indicates a gap exists. `report_stage_outcome`'s own docstring promises *"THE FAILURE REACHES THE GRAPH, not just this log line"* — when the suppress fires, the `log.error` at `:417` still prints identically, so the log asserts a graph write that never happened.

Both are provable in unit tests **today**, with no cluster, and both have an existing green test asserting the adjacent-but-wrong thing (`test_train_workflow.py:134`; `test_stage_workflow.py:373`). Repair those rather than adding parallel ones.

---

## 10b. Slice 7, re-measured after slices 2/3/6 deployed

**3 of the 4 seams are RULED OUT.** An 11-agent verification against the live estate (helm rev 44) found
the audit's premise stale on every one: it was written before the grpc + aiohttp instrumentors (slice 2)
and the Collector convergence (slice 6) landed.

| Proposed | Already emitted by | Ruling |
| --- | --- | --- |
| a `CallActor/{actor_type}/{method}` CLIENT span in `proxies.py` | the sidecar, **verbatim that name** (`CallActor/InboxActor/Deliver`, `CallActor/WatchIndexActor/ListWatchers`, carrying `dapr.actor`), plus an aiohttp span whose URL already holds actor type + wire method | **CUT** — it would be the third span on a one-hop call |
| a PRODUCER span + `messaging.*` in `dapr_publish.py` | the grpc instrumentor AND the sidecar, 1:1 per service; `messaging.system` / `messaging.destination.name` are already materialised columns | **CUT** — a third span, sitting between two existing ones |
| a publish counter + histogram | `dapr_component_pubsub_egress_count_total{success,topic}` + `_latencies_*`, plus three first-party duplicates already in code | **CUT** |
| FGA "no span and no counter" | aiohttp CLIENT spans in both planes, `http_client_duration_milliseconds_*`, the SDK's own `fga_client_request_duration_milliseconds_*`, and `governed/audit.py`'s ALLOW/DENY rows | **CUT** — the cite is wrong too: `:103` is a comment |
| a cron tick counter | `dapr_component_input_binding_count_total{component,success}` + `_latencies_*`; the binding span is the trace ROOT parenting the FastAPI span; and since slice 3 the whole `ReconcileResult` lands as queryable structured fields | **CUT** — a rask-side counter would be strictly WORSE: it dies with the app, the sidecar's survives a crash loop |

**What survived, and shipped:**

- **7a, the cron overlap-skip.** The overlap branch returns HTTP **200**, so Dapr's binding counter records
  `success="true"` and the FastAPI span looks clean. A lane whose passes permanently outrun their 30s
  period is indistinguishable from a healthy one on every PromQL surface — the only surface vmalert can
  page from. `NotificationsReconcilerStalled` concedes in its own description that it cannot separate
  stalled from idle. Now counted by `notifications.feed.passes`, with **both** outcomes given a series on
  the first tick so a ratio alert can evaluate before the first skip ever occurs.
- **7a, the gap series.** `notifications.feed.gaps` was declared, wired, and had **never created a table**
  — verified absent from the live store's `information_schema.tables`. Only ever incremented on loss, so
  until the first gap it read "no data" forever: a silent-data-loss counter that is itself silent reads as
  coverage. A clean pass now adds 0.
- **7b, the pre-I/O publish refusal.** The claim-check guard raises BEFORE the gRPC call, so there is no
  span, no egress row and no latency sample — every free surface that can see a publish failure sits
  downstream of a call this branch never makes. Now `bus.publish.refused{topic,reason}`, named `bus.*`
  rather than `dapr.*` so it cannot masquerade as a sidecar metric.
- **7b, the guard scope.** Both publish invariants scanned `services/` only, leaving the shared plane
  unguarded — a direct `.publish_event(` under `packages/` would have shipped green.

**Still open from this seam, deliberately NOT carried on the refuted justification:** nothing READS
`dapr_component_input_binding_count_total` or `dapr_component_pubsub_egress_count_total` in any panel or
alert (slice 10 owns dashboards over already-collected series), and no series separates an FGA `allow`
from a `deny` — both are HTTP 200. That is an authorization-visibility item and needs re-justifying from
scratch.

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
