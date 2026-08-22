# open_ray_handover.md — the Ray telemetry half this repo cannot merge

**Working spec, 2026-08-22.** Companion to `open_ray_otel.md`. Delete when the external cluster has
applied everything below and it has been observed working.

**Who this is for:** whoever operates the KubeRay cluster at `dev-kuberay.ra.se`. You should not need
to read `open_ray_otel.md`, the rask chart, or anything else to apply this. Each section is
self-contained: what to add, where, why, and how to tell it worked.

---

## Why a hand-over exists at all

`chart/values.yaml:1428-1432` in the rask repo says it plainly:

> *The Ray dashboard `compute` talks to. Set => used verbatim and NO in-cluster Ray is needed; empty
> => fall back to the in-cluster head Service, which exists only under singleTenant. **rask's Ray is
> managed outside this repo, so an external address is the normal case.***
> `dashboardUrl: "https://dev-kuberay.ra.se"`

And `chart/templates/rayservice.yaml:1` renders only under
`{{- if and .Values.ray.enabled .Values.singleTenant.enabled }}`, with `singleTenant.enabled: false`
by default. So on a normal install **the rask chart deploys no Ray at all**, and three telemetry fixes
have no merge target in it.

Two further constraints that decide the shape of everything below:

- **The rask Collector's service discovery is namespace-scoped** — `namespaces: { names: [<release ns>] }`
  (`chart/templates/otel-collector.yaml`). It cannot discover pods in another cluster, and would not
  discover another namespace even in the same one.
- **In the documented production posture** (`observability.otelCollector.externalEndpoint` set),
  `chart/templates/otel-collector.yaml` renders **nothing at all**. So a scrape config added there is
  skipped in exactly the environment that matters.

The rask-side halves of these three items **are landed** and are described here only so you can see
what they are; your work is the "APPLY ON THE RAY CLUSTER" block in each section.

---

## 1. Scrape Ray's metrics endpoint

**Status:** rask half LANDED (a `ray-pods` job in the in-cluster Collector, for the `singleTenant`
case). External half: **yours**.

### The problem

Nothing collects a single Ray series today. Every `ray_*`, `ray_serve_*`, `ray_data_*` and
`autoscaler_*` metric is uncollected, so a Ray head that is alive-but-wedged, a Serve application with
zero healthy replicas, and a cascade stalled on backpressure are all indistinguishable from an idle,
healthy cluster.

Ray metrics are **per-node pull endpoints**. This gap does not self-heal and cannot be pushed — it
must be scraped.

### APPLY ON THE RAY CLUSTER

Add this scrape job to whatever Prometheus-compatible collector runs beside that cluster (an OTel
Collector's `prometheus` receiver, a Prometheus `scrape_config`, or a Grafana Alloy river block — the
relabel semantics are identical in all three):

```yaml
- job_name: ray-pods
  scrape_interval: 30s
  kubernetes_sd_configs:
    - role: pod
      namespaces: { names: [<the namespace KubeRay runs in>] }
  relabel_configs:
    # KubeRay stamps this marker on EVERY Ray pod — head, workers, autoscaler sidecar.
    # Do not key on a component label: the pods are operator-created and carry none.
    - source_labels: [__meta_kubernetes_pod_label_ray_io_is_ray_node]
      action: keep
      regex: "yes"
    - source_labels: [__meta_kubernetes_pod_container_port_name]
      action: keep
      regex: metrics
    # Ray's own shipped Grafana dashboards template these. Carry them or those panels show nothing.
    - source_labels: [__meta_kubernetes_pod_label_ray_io_cluster]
      target_label: ray_io_cluster
    - source_labels: [__meta_kubernetes_pod_label_ray_io_node_type]
      target_label: ray_node_type
    - source_labels: [__meta_kubernetes_namespace]
      target_label: namespace
    - source_labels: [__meta_kubernetes_pod_name]
      target_label: pod
```

Then confirm the head and every worker group container declares the metrics port, because the job
above keeps on the port **name**:

```yaml
ports:
  - { containerPort: 8080, name: metrics }
```

KubeRay injects that containerPort itself, but a relabel cannot see a port the rendered pod spec does
not advertise. If your RayCluster/RayService manifest lists ports explicitly, add it.

### Where the data should go

Point the collector's exporter at the same GreptimeDB the rask estate uses, so Ray series sit beside
the fleet's:

```
endpoint: http://<greptimedb-host>:4000/v1/otlp
headers:
  x-greptime-db-name: public
```

Metrics take the db-name header **only**. The `x-greptime-pipeline-name` header is for traces and must
not be set on the metrics exporter.

### Four things that will cost you a day each if missed

1. **Autoscaler metrics do not carry the `ray_` prefix.** The namespace is `autoscaler`, giving
   `autoscaler_pending_nodes{NodeType,SessionName}`. A dashboard written with a blanket `ray_`
   assumption silently shows nothing.
2. **The autoscaler (:44217) and dashboard (:44227) ports are separate** and KubeRay does *not*
   auto-inject them. Only add them once you actually run a worker group or in-tree autoscaling —
   otherwise you are scraping a producer that does not exist.
3. **Verify with token auth ON before trusting any GCS rule.** `chart/values-prod.yaml:132-134` sets
   `ray.auth.enabled=true`, and ray-project/ray#59361 reports token auth plus the OTel metrics backend
   dropping the entire `ray_gcs_*` / `ray_object_store_*` family with *"Authentication required but no
   authorization header provided"*. Curl the head's `:8080/metrics` in-cluster with auth enabled and
   confirm `ray_gcs_update_resource_usage_time_bucket` is present before writing any GCS alert.
4. **`ray_tasks` is a gauge, not a counter.** Never `rate()` it. Ray's own panel pairs
   `max_over_time(...[14d])` for terminal states with `clamp_min(...)` for live ones, because these
   gauges are eventually consistent.

### How you know it worked

`ray_node_cpu_utilization` returns a series in GreptimeDB's PromQL endpoint
(`:4000/v1/prometheus`). Until it does, every Ray dashboard and alert rask adds is a green gate over
nothing.

---

## 2. Ray distributed tracing — the Serve switch first

**Status:** not started. Tracked as `open_ray_otel.md` §11 slice 7. This section will be filled in
when that slice is worked; the rask half is a new `service_kit.ray_tracing` module, and the external
half is two `rayStartParams` / container-env entries.

---

## 3. Ray log shipping

**Status:** not started. Tracked as `open_ray_otel.md` §11 slice 8. The rask half is an
outcome-aware prune; the external half is a log sidecar plus `RAY_LOGGING_CONFIG_ENCODING=JSON`,
because Ray writes task/actor/driver logs to files inside the pod that a container-stdout tail never
sees.

---

**Provenance.** Every claim here was verified against the rask repo at `main` on 2026-08-22 and
against the `ray-2.56.1` git tag — not against the Ray docs, which drift (the State API truncation
threshold differs by 10× between doc and code, and Ray Data metric names in the doc tables are not the
PromQL names).
