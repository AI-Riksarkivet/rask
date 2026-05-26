# OpenTelemetry Collector

A short reference for the Collector that sits between Python services and a backend. Not a deep deployment guide — see [official docs](https://opentelemetry.io/docs/collector/) for that.

## Why run a Collector

Python apps could export OTLP directly to a backend, but a Collector in the middle gives you:

- **Sampling decisions made centrally** with full request outcome (tail sampling).
- **Sensitive-data redaction** before telemetry leaves your network.
- **Resilience** — local buffer if the backend is down; apps keep running.
- **Cross-cutting enrichment** (`k8sattributes`, `resourcedetection`) without per-service config.
- **Format and protocol bridging** (Prometheus scrape, file logs, multiple OTLP variants).

For local dev, you can skip the Collector and point the SDK exporter at `OTEL_TRACES_EXPORTER=console` or directly at a backend. For production, always go through a Collector.

## Minimal config

OTLP receiver → memory limiter → OTLP exporter:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 400
    spike_limit_mib: 100

  resourcedetection:
    detectors: [env, system]

  k8sattributes: {} # populates k8s.* on every signal

exporters:
  otlp:
    endpoint: <backend-otlp-endpoint>:4317
    headers:
      Authorization: 'Bearer ${env:BACKEND_TOKEN}'
    sending_queue:
      enabled: true
      storage: file_storage

  debug:
    verbosity: detailed # use during validation, then remove

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resourcedetection, k8sattributes]
      exporters: [otlp]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, resourcedetection, k8sattributes]
      exporters: [otlp]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, resourcedetection, k8sattributes]
      exporters: [otlp]
```

## Key principles

1. **`memory_limiter` first in every pipeline.** Without it, a burst of telemetry can OOM-kill the Collector.
2. **One pipeline per signal type.** Don't mix traces, metrics, and logs in a single pipeline — it breaks processing and throws runtime errors.
3. **Enrich resources on every pipeline.** If `k8sattributes` runs on traces but not on metrics, signals can't be correlated.
4. **Every declared component must appear in a pipeline.** The Collector rejects orphan declarations.
5. **Use the exporter's `sending_queue` + `file_storage`, not the `batch` processor.** Persistent queue survives Collector restarts; the batch processor is being phased out for OTLP.
6. **Validate locally:** `otelcol validate --config=config.yaml` catches structural errors before deployment.

## Processor ordering

Typical order in a production pipeline:

```
memory_limiter        (always first)
→ k8sattributes       (adds k8s metadata)
→ resourcedetection   (adds host/cloud metadata)
→ transform / filter  (OTTL — redaction, normalization)
→ tailsampling        (after enrichment, before export)
→ [exporter sending_queue + retry]
```

Wrong order = silent data loss or dropped enrichment.

## Deployment shapes

| Shape                      | When                                                                       | How                                                 |
| -------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------- |
| **DaemonSet (per node)**   | Default for K8s. Apps export to `localhost:4317` (node IP).                | Helm chart with `mode: daemonset` or raw manifests. |
| **Sidecar (per pod)**      | Apps need their own buffer/redaction; multi-tenant cluster.                | OTel Operator `Sidecar` mode.                       |
| **Gateway (cluster-wide)** | Tail sampling needs all spans of a trace at one node; centralized routing. | Deployment + Service.                               |
| **Agent + Gateway**        | DaemonSets enrich at the edge, forward to a gateway that samples/routes.   | Both above.                                         |

For Docker Compose dev, a single Collector container alongside the app works fine.

## Validating a pipeline

Wire in the `debug` exporter temporarily and inspect stdout to confirm telemetry is flowing. Remove it before production — `verbosity: detailed` is expensive.

```yaml
exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter]
      exporters: [debug] # swap back to [otlp] once verified
```

## Sampling

| Strategy               | Where         | Notes                                                                                       |
| ---------------------- | ------------- | ------------------------------------------------------------------------------------------- |
| `AlwaysOn`             | SDK (default) | **Project default.** Every span exported.                                                   |
| `probabilisticsampler` | Collector     | Head sampling — random subset based on trace ID.                                            |
| `tailsampling`         | Collector     | Decides after all spans of a trace arrive — can keep errors and slow traces, drop the rest. |

Tail sampling needs all spans of a trace at the same Collector instance — use a load balancer that hashes by trace ID, or run a gateway tier. The standard way: a gateway Collector with the `loadbalancing` exporter fanning out to a pool of tail-sampling Collectors.

```yaml
# Gateway pipeline — routes spans to the right sampler by trace ID
exporters:
  loadbalancing:
    routing_key: traceID
    protocol:
      otlp:
        tls: { insecure: true }
    resolver:
      # Headless K8s Service backing the tail-sampling Deployment
      dns:
        hostname: otel-tail-sampler-headless.observability.svc.cluster.local
        port: 4317
        interval: 10s # re-resolve to pick up new replicas
```

All spans with the same `trace_id` land on the same sampler replica, so `tail_sampling` sees the complete trace before deciding.

**If you sample, materialize RED metrics before the sampling step.** The `spanmetricsconnector` derives `traces.span.metrics.*` (rate, error, duration) from spans and feeds them into the metrics pipeline — that needs to happen before tail sampling drops anything.

## Sensitive-data redaction

Use OTTL in a `transform` processor to redact attributes before export:

```yaml
processors:
  transform:
    error_mode: ignore
    log_statements:
      - context: log
        statements:
          - replace_pattern(log.body.string, "(?i)password=\\S+", "password=REDACTED")
    trace_statements:
      - context: span
        statements:
          - delete_key(span.attributes, "db.statement.parameters")
          - delete_key(span.attributes, "http.request.header.authorization")
```

Set `error_mode: ignore` in production so a bad expression doesn't drop the whole batch.

## References

- [Collector docs](https://opentelemetry.io/docs/collector/)
- [Configuration](https://opentelemetry.io/docs/collector/configuration/)
- [Contrib components](https://github.com/open-telemetry/opentelemetry-collector-contrib)
- [Collector Helm chart](https://github.com/open-telemetry/opentelemetry-helm-charts/tree/main/charts/opentelemetry-collector)
- [OTel Operator](https://github.com/open-telemetry/opentelemetry-operator)
