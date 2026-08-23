# open_ray_handover.md — the Ray telemetry half this repo cannot merge

**Working spec, 2026-08-22.** Companion to the dapr/otel audit (landed and deleted 2026-08-23; the rask-side halves this file relies on are in `service_kit/ray_tracing.py`, the `ray-pods` scrape job in `chart/templates/otel-collector.yaml`, and `chart/templates/rayservice.yaml`). Delete when the external cluster has
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

## 0. BEFORE THE TELEMETRY — the dashboard API accepts unauthenticated requests

**This is not a telemetry item and it outranks all three below.** Found 2026-08-23 while checking
whether rask's Ray auth was ever exercised against a live cluster rather than only at render time.

Measured from inside rask's cluster, against `https://dev-kuberay.ra.se`:

```
GET /api/version      with a valid token  -> 200
GET /api/version      with NO token       -> 200
GET /api/version      with a WRONG token  -> 200

No Authorization header at all:
GET /api/jobs/          -> 200   [{"type":"SUBMISSION","submission_id":"htr_http-chunk-054-of-100-...
GET /api/cluster_status -> 200
GET /nodes?view=summary -> 200
```

`/api/jobs/` is the Ray Job Submission API — the same endpoint that SUBMITS (`POST`) and DELETES
jobs. Anything that can route to that host can enumerate the cluster's work and, through the same
door, run arbitrary code on it. Only read paths were exercised here; no write was attempted.

**rask's half is correct and is not the problem.** `ray.auth.enabled` is `true` in the live release,
the token secret exists and holds 32 bytes, and `compute` sends it as `Authorization: Bearer`. The
cluster does not check it. rask cannot fix that from this repo — which is precisely why it is in this
document.

**What this repo got wrong, and has now recorded:** `tests/unit/test_ray_auth.py` has eight tests and
every one is a `helm template` assertion. They prove the chart RENDERS auth — secret, env, fail-closed
prod guards — and they all pass. None of them makes an authenticated call, so the gap between "we send
a token" and "the token is honoured" was invisible to the entire suite. A render-time gate cannot
observe the far end of a network call, and this is what that costs.

**Two things worth establishing before ranking severity**, neither of which is answerable from inside
rask's cluster: whether `dev-kuberay.ra.se` is reachable from outside your network, and whether
anything else (network policy, VPN, ingress auth) fronts it. If it is internal-only the finding is
much smaller than it reads. If it is not, treat it as urgent.

## A working reference (added 2026-08-23)

Everything in §1 has now been exercised against a real KubeRay cluster, so you are not applying
untested instructions. That cluster is `rask-ray`, inside rask's own k3s estate — a RayService that
**the chart no longer renders** (`rayservice.yaml` gates on `singleTenant.enabled`, which is `false`)
and that is **orphaned from Helm**: it carries Helm labels from an older revision but appears in no
current release manifest. It is therefore not a template you can copy, but it IS a live cluster the
scrape config was proven against, and its being unmanaged is what made it safe to test on.

What that changes for you, section by section:

| | before | now |
| --- | --- | --- |
| §1 metrics scrape | proposed | **verified end-to-end** — series named and counted |
| §2 tracing | "rask half LANDED" | wiring exists, **reaches nothing**, no span ever observed |
| §3 log shipping | "Serve stderr already collected" | **measured**: 0 Ray rows reached the store in 6h |

The one decision this raises for rask, not for you: the chart ships Ray telemetry wiring that no
install renders. Either the RayService should render, or the orphan should be adopted, or the wiring
should be dropped as external-only. Today it is the third by accident rather than by choice.

## 1. Scrape Ray's metrics endpoint

**Status:** rask half LANDED **and now VERIFIED END-TO-END** against a real KubeRay cluster.
External half: **yours**.

> **VERIFIED 2026-08-23 on rask's own in-cluster KubeRay** (`rask-ray`, a RayService the chart no
> longer renders — see *A working reference* below). The scrape job works exactly as written here:
>
> ```
> up{job="ray-pods"} = 1
>   k8s_pod_name   = rask-ray-h5j5d-head-jpj2d
>   ray_io_cluster = rask-ray-h5j5d
>   ray_node_type  = head
>
> ray_tasks                         → 50 series
> ray_serve_num_http_requests_total →  2 series
> ray_node_cpu_utilization          →  1 series
> ray_cluster_active_nodes          →  1 series
> ```
>
> So this section is no longer a proposal — it is a procedure with a known-good outcome, and the
> block below is the config that produced it.
>
> **One thing to carry over that this section did not originally warn about.** Turning the scrape on
> more than DOUBLED the estate's series count in a single step — 3,250 → 7,349 — because Ray Data
> emits 113 `ray_data_*` families of per-dataset, per-operator series. rask's telemetry store began
> OOMKilling 3h30m later and died 13 times. Budget for that before you enable it, and consider the
> `metric_relabel_configs` drop rask now ships (`chart/templates/otel-collector.yaml`), which keeps
> the two `ray_data_` series a dashboard actually reads and drops the other 111.

### The problem

Nothing collected a single Ray series before this landed. Every `ray_*`, `ray_serve_*`, `ray_data_*` and
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

**Status:** rask half landed **in the chart, where it currently reaches nothing.** External half:
**yours** — and this section is UNVERIFIED, unlike §1.

> **Read that status precisely, because it was overstated here before.** `service_kit.ray_tracing`
> genuinely exists and exports `serve_span_processors`, and `chart/templates/rayservice.yaml:117,121`
> genuinely sets both env vars. But that template renders only under
> `ray.enabled AND singleTenant.enabled`, and `singleTenant.enabled` is `false` — so on a normal
> install the chart deploys no RayService at all. rask's own running `rask-ray` predates the current
> gate and is **orphaned from Helm** (it carries Helm labels but appears in no release manifest), so
> the chart's wiring does not reach it either: `RAY_SERVE_TRACING_*` appears **0 times** on the live
> RayService, verified 2026-08-23.
>
> Consequence for you: nobody has ever run this switch, here or anywhere. Treat the block below as
> untested — the code path is real and the env var names are correct, but no span has been observed
> arriving from it. §1 is the section with a proven outcome; this one is not.

### Wire the Serve switch first — it is the higher-value one

Ray has **two independent tracing planes**, configured in different places, and neither is on:

| | Where it is set | What it gives |
| --- | --- | --- |
| **Ray Core** | `headGroupSpec.rayStartParams: {tracing-startup-hook: …}` | PRODUCER/CONSUMER spans on `.remote()` |
| **Ray Serve** | `RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH` env | proxy → router → replica spans, **honouring an inbound `traceparent`** |

**Do Serve first.** It is the segment that joins a gateway-originated trace to the model call, and
Ray's own monitoring documentation never mentions the switch exists. Core tracing is documented
upstream as Alpha and *"no longer under active development"*, and Ray Data / Train / Tune contribute
**zero** spans of their own — so its payoff is generic per-task spans named after Ray's internal
functions, not dataset or operator spans.

### APPLY ON THE RAY CLUSTER

The functions already exist in the image: `packages/ratch` depends on `service-kit[lancekit]` and
`.docker/ray-cluster.dockerfile` installs ratch, so `service_kit` is importable in every Ray Python
process. No new dependency, no image change.

**On the head container env (and every worker group container — replicas are scheduled anywhere):**

```yaml
- name: RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH
  value: "service_kit.ray_tracing:serve_span_processors"
- name: RAY_SERVE_TRACING_SAMPLING_RATIO
  value: "1.0"
```

**On the head's `rayStartParams` — HEAD ONLY:**

```yaml
rayStartParams:
  tracing-startup-hook: "service_kit.ray_tracing:setup_tracing"
```

Both need the OTLP env from §1's exporter block to be present on the same containers, since the hooks
read `OTEL_EXPORTER_OTLP_ENDPOINT` from the process environment.

### Four traps, each of which costs a day

1. **The two contracts DIFFER and must not be copy-pasted.** `setup_tracing` takes no arguments and
   returns `None` — it sets the global provider. `serve_span_processors` takes no arguments and
   **returns a list of `SpanProcessor`** — Serve builds the provider. Point either env at the other's
   function and it fails **soft**: Serve catches the error, logs *"the proxy/replica will continue
   running"*, nothing goes unhealthy, and no span is ever produced.
2. **`RAY_SERVE_TRACING_SAMPLING_RATIO` defaults to `0.01`.** One request in a hundred. A ten-request
   smoke test against the default yields **zero spans** and reads as "tracing is broken".
3. **`tracing-startup-hook` on a worker group is a silent no-op.** The hook is persisted to GCS
   internal KV by `start_head_processes()` and every connecting process reads it from there — that is
   the entire propagation mechanism. Setting it per-worker looks like configuration and does nothing.
4. **Verify by observing spans, never by checking health.** Both planes fail soft by design. Query
   `opentelemetry_traces` for a span whose service name matches `OTEL_SERVICE_NAME`; a healthy pod
   proves nothing.

### How you know it worked

A request through the gateway to a Serve route produces a trace containing **both** the gateway's
span and a Serve proxy/replica span, joined by one `trace_id`. That single joined trace is the thing
the estate does not have today.

---

## 3. Ray log shipping

**Status:** rask half landed in the chart (same caveat as §2 — it renders for nobody). External half:
**yours**. The GAP here is now **measured**, and one claim in the table below was optimistic.

> **MEASURED 2026-08-23 on rask's own `rask-ray` head pod:**
>
> ```
> stdout lines since 2026-08-17 (6 days) :  28
> log files in /tmp/ray/session_latest/logs : 490
> ray-pod rows in opentelemetry_logs (6h)   :   0   (of 1,110,457 rows total)
> ```
>
> The Collector's filelog receiver includes `/var/log/pods/*/*/*.log` — every pod on the node, Ray
> included — and other pods' logs land fine over the same path. So this is not a selector bug on the
> shipper side. Ray simply does not write to stdout: 28 lines in six days against 490 files kept
> inside the container.
>
> **Correct the row below that says Serve replica logs are already collected.** That could not be
> confirmed: zero rows from any Ray pod reached the store in the measured window. It may be true when
> Serve is actively serving — `ray_serve_num_http_requests_total` carries only 2 series here, so this
> cluster's Serve app is effectively idle and may simply have had nothing to log. Either way, treat
> "YES" as unverified rather than as a fact you can build on.

### The problem, split in two — because only one half needs you

Ray has two log planes and they behave differently:

| plane | where it writes | already collected? |
| --- | --- | --- |
| **Serve replicas** | **stderr** | **YES** — the Collector's filelog receiver already tails Ray pod stdout/stderr. It has been ingesting them **unparsed**: one body string, no severity, no replica id. |
| **Core** (driver, tasks, actors) | files under `/tmp/ray/session_*/logs/` **inside the container** | **NO.** Nothing mounts that path and nothing tails it. |

So `RAY_SERVE_LOG_ENCODING=JSON` pays off with no shipper work at all, while the core half needs a
sidecar. Both env vars are set on the rask-rendered RayService already; replicate them on yours.

### APPLY ON THE RAY CLUSTER

**1. Structure the logs** — on the head container env, and every worker group container:

```yaml
- { name: RAY_LOGGING_CONFIG_ENCODING, value: "JSON" }   # core: driver, tasks, actors
- { name: RAY_SERVE_LOG_ENCODING,      value: "JSON" }   # Serve replicas
```

Must be set **before `import ray`**, which a container env satisfies by construction.

**2. Ship the core logs** — a shared volume plus a log-shipping sidecar, the KubeRay-documented
pattern:

```yaml
volumes:
  - name: ray-logs
    emptyDir: { sizeLimit: 2Gi }
containers:
  - name: ray-head
    volumeMounts:
      - { name: ray-logs, mountPath: /tmp/ray }
  - name: ray-log-agent
    image: otel/opentelemetry-collector-contrib:0.157.0
    args: ["--config=/etc/otelcol/agent.yaml"]
    volumeMounts:
      - { name: ray-logs, mountPath: /tmp/ray, readOnly: true }
      - { name: ray-log-agent-config, mountPath: /etc/otelcol, readOnly: true }
```

The sidecar config is a `filelog` receiver over
`/tmp/ray/session_latest/logs/**/*.{log,out,err}` with `include_file_path: true`, `start_at: end`, a
`json_parser` (the records are JSON once step 1 is applied), exporting `otlphttp` to the same
GreptimeDB as §1. Poll frequently at first: the logs directory does not exist until Ray creates it.

### Three traps

1. **Do NOT use `RAY_LOG_TO_STDERR=1` as the shortcut.** Ray's own docs warn it stops Ray writing log
   files entirely — which breaks the driver-log reader `ray-kit` uses to serve
   `/api/ray/jobs/{id}/logs`. You would trade a post-mortem gap for a live-debugging one.
2. **`RAY_BACKEND_LOG_JSON=1` converts only the Job Supervisor** among the Python system components.
   The Dashboard, Dashboard Agent, Log Monitor and Autoscaler Monitor stay plain text regardless, so
   setting it reads as though it structured the lot and does not.
3. **Consider `RAY_DEDUP_LOGS=0`.** Ray buffers repeated log patterns for five seconds and batches
   them, which REORDERS records relative to their timestamps in a collected stream.

### What rask already did, so you do not repeat it

The prune cron was deleting failures by recency: `prune_jobs` sorted purely on `start_time`, so a busy
afternoon of successful jobs pushed every failure past the 500-job window and deleted it — bounding
post-mortem by submission **volume** rather than by time, on exactly the rows worth keeping. It now
keeps an independent floor of the newest terminal-bad jobs (`RASK_PRUNE_KEEP_FAILED_JOBS`, default
100). That is a floor, not an exemption: failures beyond it are still deleted, because bounding the
parameterless listing is what stopped the OOM in the first place.

### How you know it worked

A Serve replica exception appears in `opentelemetry_logs` with a populated `severity_text` and a
queryable `deployment`/`replica` field — not as an unparsed body string. For the core half, a
`job-driver-*.log` line from a medallion stage job is queryable at all.

---

**Provenance.** Every claim here was verified against the rask repo at `main` on 2026-08-22 and
against the `ray-2.56.1` git tag — not against the Ray docs, which drift (the State API truncation
threshold differs by 10× between doc and code, and Ray Data metric names in the doc tables are not the
PromQL names).
