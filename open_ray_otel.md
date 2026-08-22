# open_ray_otel.md — Ray runs the cascade, and the estate cannot see it

**Working plan, 2026-08-22.** Delete this file when the slices in §11 have landed. Companion:
`open_dapr_otel.md` (the microservice half of the same audit). Both exist because `TODO.md:26` says
"missing telemetry all over the places" and nobody had measured where.

**The one-line state:** **not one Ray series is collected anywhere.** No `ray_*`, no `ray_serve_*`,
no `ray_data_*`, no `autoscaler_*`. The Collector's prometheus receiver has exactly two scrape jobs
and both are keyed on Dapr pod annotations that a Ray pod does not carry, so no Ray pod can ever be a
target. Ray's application logs are written to files inside the pod that nothing mounts and nothing
tails. Ray's own distributed tracing is unwired on both of its two independent switches. And
`chart/alerting/rules.yml` has fifteen alerts, none about Ray.

**But the headline is not an observability gap — it is a correctness bug the audit fell over.**
`§1`. In any deployed estate with the Ray lane on, **the stage workflow polls a submission id the
submitter never posted**, so every stage job is reported `abandoned` on its *first* poll and emits a
**fabricated FAIL** over data that is being written correctly. The cascade's success path cannot
complete.

**Method.** 16 agents: 4 read the upstream contract against the **ray-2.56.1 tag** (what
`chart/values.yaml:1450` pins) rather than the docs, because the docs drift by 10× on at least one
threshold; 6 read one repo subsystem each; 6 adversarially tried to refute the first six. **95
findings survived, 0 were refuted, 58 were corrected** — mostly severity deflation and scoping. Every
claim is `file:line`; every absence claim is a negative grep re-run by a second agent.

---

## 0. Read this before costing anything — the Ray cluster is not in this chart

`chart/values.yaml:1428-1432`, verbatim:

> *The Ray dashboard `compute` talks to. Set => used verbatim and NO in-cluster Ray is needed; empty
> => fall back to the in-cluster head Service, **which exists only under singleTenant**. **rask's Ray
> is managed outside this repo, so an external address is the normal case.***
> `dashboardUrl: "https://dev-kuberay.ra.se"`

And `chart/templates/rayservice.yaml:1` is `{{- if and .Values.ray.enabled .Values.singleTenant.enabled }}`,
with `singleTenant.enabled: false` at `chart/values.yaml:50` — not set by `make k3s-up`.

**So on a default install this chart deploys no Ray at all.** That splits every fix below into two
buckets, and conflating them is how this audit would produce work that changes nothing:

| Bucket | What it covers | Where it lands |
| --- | --- | --- |
| **A — this repo** | the job scripts, `ray-kit`, `compute`, the medallion Ray lane, the runners, the alert rules, the dashboards, and the `rayservice.yaml` template for when `singleTenant` *is* on | ordinary PRs here |
| **B — wherever the external KubeRay is managed** | `rayStartParams`, container env, the metrics scrape target, the log sidecar, `RAY_SERVE_TRACING_*` | **not in this repo.** This file is the specification to hand over |

Two further scoping facts that bound bucket A:

- The Collector's k8s service discovery is **namespace-scoped** (`namespaces: { names: [{{ .Release.Namespace }}] }`,
  `otel-collector.yaml:97`), so it cannot reach a Ray cluster elsewhere even in-cluster.
- In the documented prod posture (`observability.otelCollector.externalEndpoint` set),
  `chart/templates/otel-collector.yaml` **renders nothing at all** (`:20`) — so a `scrape_config`
  added there is skipped in exactly the environment that matters. **The scrape must be specified as a
  contract, not only as a chart edit.**

---

## 1. CRITICAL — every deployed Ray stage is reported "abandoned", 30 s after it starts

This is a correctness defect, found while auditing telemetry, and it is the worst kind: **the job's
true outcome is not merely unreported, it is misreported.**

**The mechanism.** The submission id is derived twice, from two different argument lists:

| | derivation | file:line |
| --- | --- | --- |
| **submitter** (posts the job) | `stage_submission_id(stage, token, from_uri, to_uri, code=code_version)` | `medallion/services/ray_submit.py:97` (`code_version` at `:90`) |
| **workflow** (returns the id to poll) | `stage_submission_id(spec.stage, spec.token, spec.from_uri, spec.to_uri)` — **no `code=`** | `medallion/workflow.py:262` |

`ray_kit/submit.py:84-85` appends `-<sha256[:8]>` **only when `code` is non-empty** — and it is
non-empty in every deployment, because `chart/templates/medallion.yaml:383` renders
`MEDALLION_RAY_CODE_VERSION` **outside** the `if $root.Values.medallion.ray` block (which opens `:353`,
closes `:365-366`), i.e. on every mover. `config.py:248` defaults it to `""` only when unset.

So the two ids always differ, and the consequence chain is exact:

```
poll GET /api/jobs/<wrong-id>  →  404
  ray_kit/submit.py:131-132     →  job_status returns None
  workflow.py:187               →  `status is not None` is False, so the retry branch is skipped
  workflow.py:191 → :195        →  verdict = "abandoned"   ← on the FIRST poll
  workflow.py:346-350           →  report_stage_outcome emits a FAIL RunEvent:
                                   "the watch was abandoned after 1 poll(s) with the job still UNKNOWN"
```

`publish_stage_ready` (`workflow.py:220`) never runs. So the mover's pass-2 never happens: **no
COMPLETE OpenLineage event, no next-tier trigger, no catalog registration of the written version** —
while the Ray job runs to completion and writes its data.

**There is already a test for exactly this, its docstring describes the bug precisely, and it is
defeated by its own fake.** `services/medallion/tests/test_stage_workflow.py:362-378`,
`test_submit_returns_THE_SAME_id_it_submitted_under`:

> *"The poll watches whatever this returns. If it derives a DIFFERENT id from the one
> `submit_stage_job` used, the poll reads `None` forever, the watch abandons at the ceiling, and a
> perfectly healthy job is reported as never finishing — with no error anywhere."*

It passes because the fake at `:372` calls
`ray_submit.stage_submission_id(stage, token, from_uri, to_uri)` — **without `code=`** — reproducing
the bug on *both* sides of the assertion. Any fix must repair that fake, not merely add a new test.
This is the sharpest instance of the pattern both audit files keep finding: **a thing that reads as
coverage and is not.**

### Fix — stop deriving the id twice

```python
# services/medallion/src/medallion/services/ray_submit.py
async def submit_stage_job(...) -> str:          # was -> None
    ...
    await rk.submit_or_reattach(client, submission_id, body)
    log.info("ray_stage_job_submitted", extra={...})
    return submission_id

# services/medallion/src/medallion/workflow.py::submit_stage — replace line 262
    return _run_async(submit_stage_job(settings, from_uri=spec.from_uri, ...))
```

Delete the second `stage_submission_id(...)` call from `workflow.py` entirely. (Keep it at
`transform.py:104` for the workflow **instance** id — a different namespace, harmless.) Then fix
`test_stage_workflow.py:372` so the fake returns what the submitter actually posted, and assert the
activity's return equals `captured[0]["submission_id"]` against the existing `_capture_submits`
MockTransport (`tests/unit/test_ray_trace_continuity.py:78-95`).

**This is the one item in this file that should not wait for a telemetry slice.**

---

## 2. Nothing scrapes Ray — the enabling gap for everything else

`chart/templates/rayservice.yaml:96-100` declares exactly four ports: `gcs`, `dashboard`, `client`,
`serve`. `grep -n "8080\|metrics" chart/templates/rayservice.yaml` → **no matches**.

The Collector's prometheus receiver has two jobs and **both drop a Ray pod at the first relabel step**:

- `dapr-sidecars` (`otel-collector.yaml:94-109`) — `keep` on `__meta_kubernetes_pod_annotation_dapr_io_enabled == "true"`
- `dapr-control-plane` (`:127-144`) — `keep` on `__meta_kubernetes_pod_annotation_dapr_io_control_plane =~ ".+"`

`grep -n "dapr" chart/templates/rayservice.yaml` → **no matches**. And there is no PodMonitor path
either: `grep -rn "monitoring.coreos.com" chart/` finds nothing outside a CNPG CRD schema field, and
`chart/Chart.lock` has no prometheus-operator dependency — a PodMonitor would be an object nothing
reconciles.

**What is therefore uncollected**, on the cluster the entire bronze→silver→gold cascade runs on:
GCS liveness, `ray_tasks`/`ray_actors` by state, OOM kills (`ray_memory_manager_worker_eviction_total`),
node CPU/GPU/GRAM, object-store spill, every `ray_serve_*` (QPS, latency, replica health, queued
queries), every `ray_data_*` (throughput, backpressure), and the `autoscaler_*` family.

**A Ray head that is alive-but-wedged, a Serve app with zero healthy replicas, and a cascade stalled
on backpressure are all indistinguishable from an idle, healthy cluster.**

### Fix — one scrape job, keyed on the label the chart already knows

Do **not** invent a selector. `chart/templates/network-policy.yaml:240-243` already documents the
right one and says why: *"The chart's head template stamps no labels of its own; KubeRay stamps its
`ray.io/is-ray-node: "yes"` marker on every Ray pod, so select that (component-label style can't reach
operator-created pods)."*

```yaml
            - job_name: ray-pods
              scrape_interval: 30s
              kubernetes_sd_configs:
                - role: pod
                  namespaces: { names: [{{ .Release.Namespace }}] }
              relabel_configs:
                - source_labels: [__meta_kubernetes_pod_label_ray_io_is_ray_node]
                  action: keep
                  regex: "yes"
                - source_labels: [__meta_kubernetes_pod_container_port_name]
                  action: keep
                  regex: metrics
                - source_labels: [__meta_kubernetes_pod_label_ray_io_cluster]
                  target_label: ray_io_cluster
                - source_labels: [__meta_kubernetes_pod_label_ray_io_node_type]
                  target_label: ray_node_type
                - source_labels: [__meta_kubernetes_namespace]
                  target_label: namespace
                - source_labels: [__meta_kubernetes_pod_name]
                  target_label: pod
```

Three notes that will otherwise cost a day each:

- **KubeRay auto-injects the `metrics`/8080 containerPort but not the autoscaler (44217) or dashboard
  (44227) ones.** Declare them in `rayservice.yaml:96-100` only if you want `autoscaler_*` — and today
  you do not: `workerGroupSpecs: []` (`:124`) and there is no in-tree autoscaling, so **half the
  upstream autoscaler contract is inapplicable here.** Say so rather than building for it.
- **Autoscaler metrics do not carry the `ray_` prefix.** The namespace is `autoscaler`, giving
  `autoscaler_pending_nodes{NodeType,SessionName}`. A dashboard written with a blanket `ray_`
  assumption silently shows nothing.
- **Verify with auth ON before trusting any GCS rule.** `chart/values-prod.yaml:132-134` sets
  `ray.auth.enabled=true`, and ray-project/ray#59361 reports token auth + the OTel metrics backend
  dropping the entire `ray_gcs_*`/`ray_object_store_*` family with *"Authentication required but no
  authorization header provided"*. Curl the head's `:8080/metrics` in-cluster with auth on and confirm
  `ray_gcs_update_resource_usage_time_bucket` is present.

**Everything in §7 (dashboards) and §8 (alerts) is blocked on this one item.** Landing them first
produces green gates over nothing — the exact failure mode `chart/alerting/rules.yml:257-261` already
documents.

---

## 3. The one OTLP block the chart does set reaches almost nothing

`chart/templates/rayservice.yaml:27-33` sets five `OTEL_*` variables — **inside `serveConfigV2`'s
`runtime_env.env_vars`**. That scope is one Serve application's build task and replica actors. It does
**not** reach:

- the `ray-head` container, GCS, the raylet, or the dashboard agent
- the Serve controller or proxy actors
- any Ray **Job** submitted later (the medallion cascade's entire lane)
- any future worker group

And the values are **hardcoded string literals** — `http://rask-greptimedb-standalone:4000/v1/otlp`,
`x-greptime-db-name=public`, `x-greptime-pipeline-name=greptime_trace_v1` — while every other telemetry
consumer derives them from `lance.greptimeHost` + `observability.{greptimePort,dbName,tracePipeline}`.

**Proven by render:** `helm template myrelease ./chart --set singleTenant.enabled=true …` emits
`myrelease-greptimedb-standalone` at **every** telemetry site *except* the RayService, which still says
`rask-greptimedb-standalone`. It is the only divergent value in the whole render.

This is precisely the defect `rask.otelEnv`'s own comment records having fixed elsewhere
(`_helpers.tpl:227-228`: *"the release-derived Greptime host (was hardcoded …, which ignored the
release name)"*). It regressed in a fourth copy.

It also **names a workload in the platform chart**: `OTEL_SERVICE_NAME: "ray-htrflow"` (`:32`).

### Fix

Add a `rask.rayOtelEnv` helper beside the existing pair in `_helpers.tpl` — deriving from
`lance.otlpEndpoint` / `lance.otelViaCollector` / `lance.otelEnabled`, exactly as `lance.otelEnv` does
— then **move the block out of `serveConfigV2` into the ray-head container `env:`** (after `:95`), and
add the identical include to every future workerGroupSpec container. Set `OTEL_SERVICE_NAME` from the
platform (`ray`), not from a workload.

Add a render invariant beside `tests/unit/test_invariants.py:953` asserting no template contains the
literal `rask-greptimedb-standalone`. *(The only other occurrence is `chart/values.yaml:2176` — the
Perses **subchart** datasource, which is static subchart values that cannot take a Helm helper. Leave it,
and say why.)*

Also: the block is gated on `observability.enabled`, while the lance plane uses `lance.otelEnabled`
(which also honours `externalOtlpEndpoint`). **Externalising telemetry silently drops Ray.** Same
one-line fix as the fleet's, and the same root cause as `open_dapr_otel.md` §8.

---

## 4. Ray's logs reach nothing, and the pruner deletes the only copy

Ray writes task, actor and **job-driver** output to files under `/tmp/ray/session_*/logs/` **inside the
container**, not to container stdout. The Collector's filelog receiver tails
`/var/log/pods/*/*/*.log` (`otel-collector.yaml:80-82`). The Ray pod mounts only `dshm` and `hf-cache`
(`rayservice.yaml:113-115`, volumes `:116-123`), has **one** container, and no sidecar.

`grep -rn "RAY_LOG_TO_STDERR\|RAY_LOGGING_CONFIG_ENCODING\|RAY_BACKEND_LOG_JSON\|RAY_DEDUP_LOGS\|RAY_ROTATION" chart/`
→ **no matches**.

So for the flagship cascade, `RAY-STAGE OK stage=… rows=… version=…` (`scripts/ray_stage_job.py:448-451`)
— the only record of what a run actually produced — exists in exactly one place, is readable only
through a live Ray dashboard, and:

**the retention cron deletes it by recency alone.** `compute/pruner.py:31` keeps the newest 500 jobs;
`ray_kit/prune.py:83-101` sorts by `start_time` and deletes everything terminal beyond that,
**regardless of outcome**. A burst of successes evicts the failures. Post-mortem is therefore
time-bounded by submission volume.

*(Correction worth carrying: Ray Serve **replica** logs default to stderr, so those already reach the
collector via container stdout — unparsed. The gap is the core/driver/task half.)*

### Fix — three pieces, all workload-neutral

1. **Ship the files.** Add a `/tmp/ray` emptyDir plus a log-shipping sidecar (the KubeRay-documented
   pattern) — an `otel/opentelemetry-collector-contrib` container with a filelog receiver over
   `/tmp/ray/session_latest/logs/**/*.{log,out,err}`, exporting otlphttp to `lance.otlpEndpoint`. Do
   **not** take the `RAY_LOG_TO_STDERR=1` shortcut: Ray's own docs warn it stops Ray writing log files
   entirely, which breaks `ray-kit`'s `job_logs` reader (`dashboard.py:544-556`).
   The existing double-ingest filter is **safe** here — `otel-collector.yaml:174-178` drops only records
   that carry `lance.dev/logs=otlp` **and** a `log.file.path`, and Ray pods carry no such label.
2. **Make them parseable.** `RAY_LOGGING_CONFIG_ENCODING: "JSON"` (core: emits
   `job_id`/`worker_id`/`node_id`/`actor_id`/`task_id`/`task_func_name`) and
   `RAY_SERVE_LOG_ENCODING: "JSON"`. Note the Serve half is **not** blocked on piece 1 — it structures
   logs that are being ingested unparsed *right now*. Two documented traps: the emitted key is
   `task_func_name` (not `task_function_name`), and `RAY_BACKEND_LOG_JSON=1` converts only the Job
   Supervisor — Dashboard, Dashboard Agent, Log Monitor and Autoscaler Monitor stay plain text.
   Consider `RAY_DEDUP_LOGS=0`: Ray buffers repeated patterns for 5 s and batches them, reordering
   records relative to their timestamps.
3. **Persist the outcome before it can be pruned** (§5), and make retention outcome-aware in
   `ray_kit/prune.py:88` — keep the newest N FAILED/STOPPED in addition to the newest N overall.

---

## 5. The cascade's Ray hop: no lineage, no in-flight signal, no failure reason

Three independent holes on the estate's flagship flow.

**(a) The Ray stage job emits no lineage.** The setting that would wire one —
`MEDALLION_STAGE_LINEAGE_URL` (`medallion/core/config.py:429`) — is rendered by **no chart template**.
The chart's pre-staged `lance.lineageEmitEnv` on the Ray head (`rayservice.yaml:95`) is inert for one
reason it states correctly (lineage-kit is not in the image) — the `RASK_LINEAGE_*` names it renders
*are* the names lineage-kit reads (`lineage_kit/config.py:23`), so the chart comment is right about the
what and wrong about the why.

The **only** lineage naming a Ray-executed stage is written by *other* processes: the mover's pass-2
COMPLETE (`transform.py:580`) and the watcher's FAIL (`workflow.py:351-352`, best-effort **suppressed**).
Any failure that loses the watcher — the mover pod replaced mid-flight before rehydration,
`report_stage_outcome` exhausting `ACTIVITY_RETRY` (`workflow.py:71-76`), the suppressed publish
swallowing a lineage outage — produces a run that emitted **nothing**.

Per `.claude/skills/rask-notifications`, that is the difference that matters: an absent event is not
under-delivered, it is **undeliverable**, and `notifiable()` acks a miss as SUCCESS. Nothing reports
the loss.

**And the proxy emitters do not rescue it either.** `author_subject()` reads `author.sub` and nothing
else — deliberately, so no producer can put a row in a named person's inbox. Every emitter on this
path authors with a **chart role literal**: `chart/values.yaml:977` `author: ray` (the producer's
bronze write, `operation: lance_ray_ingest`), `:997` `data_eng`, `:998` `analyst`, `:1005` `data_eng`.
So a failed Ray cascade addresses an inbox actor literally named `ray`. **No person is told about a Ray
stage today, by any path** — trap 1 in `rask-notifications`, and the reason it is trap 1 is that the
event looks perfectly well-formed. Closing §5(a) without also carrying a verified human sub through
the call graph buys nothing a person can see.

**(b) A dispatched stage is invisible in PromQL until it terminates.** `medallion/core/metrics.py:19-51`
is six counters and **zero histograms**; the Ray branch returns at `transform.py:431`, before
`record_transition` at `:807`. There is no in-flight series and no START event, so *"is the cascade
running or wedged?"* is unanswerable from deployed telemetry. *(It is not invisible in **all**
telemetry: durabletask emits `activity: poll_stage` spans, and the instance is queryable by id — the
hole is in the metric/alerting plane. Note the flip side: a 24-hour watch at `samplingRate: 1`
produces up to **2880 activity spans per job**.)*

**(c) A FAILED job's reason is never captured.** `workflow.py:346-350` builds the reason from
submission id + status + poll count only, and that becomes the `errorMessage` facet at `:406`.
`ray_kit.submit.job_status` (`submit.py:135`) reads only the `status` key of a response that **also
carries Ray's `message` and `error_type`** — and `ray_kit.schemas.RayJob:46-48` already declares
`error_type`, `message` and `driver_exit_code`. The plumbing exists on the read side; the workflow path
does not use it. Combined with §4, a failed Ray stage is undiagnosable from the deployed estate: no
traceback in the log store, no error text in the lineage event, no Ray metrics to correlate.

### Fix

- Thread `message` / `error_type` / `driver_exit_code` from `job_status` into `StageJobOutcome` and into
  the `errorMessage` facet. This is the cheapest high-value item in the file after §1.
- Add to `medallion/core/metrics.py`, labelled only by the existing bounded `lance.medallion.transition`:
  `medallion.stage.duration` (Histogram, `unit="s"`, **second-scale** `explicit_bucket_boundaries_advisory`
  — `[1,5,15,60,300,900,1800,3600,7200,21600,86400]`; the SDK's millisecond default is the exact mistake
  `lineage/core/metrics.py:35-38` documents), `medallion.stage.rows` (`{row}`), `medallion.stage.bytes`
  (`By`). Cover the **in-process** branch too (`transform.py:451-467`), which is the default path.

  **The duration must be a MEASURED `time.perf_counter` delta, and the same number must land in the
  lineage run facet.** That is `open_batch_process.md` B10, which names the Ray stage explicitly:
  *"Every duration — coordinator activity, Ray stage, commit — uses `time.perf_counter` … and the
  **same number** lands in the lineage run facet so the graph and the metric cannot disagree."*
  An earlier draft of this section proposed deriving it from `outcome.polls * spec.poll_interval_seconds`
  (already carried in `StageJobOutcome`, `workflow.py:122`). **That is wrong and must not be built:** it
  is a poll-quantised estimate, it is not what the lineage facet carries, and B10 exists precisely to
  stop the graph and the metric disagreeing. `perf_counter` cannot cross the `continue_as_new` boundary,
  so carry a monotonic start stamp on `StageJobSpec` the way `submission_id` and `polls_done` already
  ride it (`workflow.py:108-114`) — `open_medallion_workflow.md` §12 states the rule: *"Anything added
  to this workflow that must survive a turn goes in that spec."*
- Either render `MEDALLION_STAGE_LINEAGE_URL` and put lineage-kit in the Ray image, or **state plainly
  in the chart that the Ray lane emits no lineage of its own** and that the mover's pass-2 is the sole
  record. A half-wired carrier is worse than none.

---

## 6. Tracing — two independent switches, both unwired; and what is already solved

**Do not start here.** The job-level trace continuity that actually matters is **already built by hand
and works** (see §9). Ray's native tracing is incremental on top of it.

| Switch | What it gives | State |
| --- | --- | --- |
| `headGroupSpec.rayStartParams: {tracing-startup-hook: "mod:fn"}` | PRODUCER/CONSUMER span pair on every `.remote()` | **unwired** — `grep` finds it nowhere |
| `RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH` + `_SAMPLING_RATIO` | Serve proxy → router → replica spans, **honouring an inbound `traceparent`** | **unwired** |

**The Serve switch is the higher-value one**, and it is the piece Ray's own monitoring docs never
mention. Serve already accepts an inbound `traceparent`, so wiring it is what lets a gateway-originated
trace continue **into the model call** — the highest-value trace segment the estate does not have.
Today that trace dies at the Serve door.

**Honest ceiling on the core hook**, stated so nobody over-invests: **Ray Data, Ray Train and Ray Tune
contribute zero spans of their own.** The payoff is generic per-task spans named after Ray's internal
module functions — not a dataset or operator span. Ray Core tracing is also documented upstream as
*"an Alpha feature and no longer under active development/being maintained"*. **Do not build the
estate's Ray trace story on it.**

### Fix — one new module, two entrypoints, zero new dependencies

The Ray image **already ships the OTel SDK and the OTLP/HTTP exporter**:
`packages/ratch/pyproject.toml:10` depends on `service-kit[lancekit]`;
`packages/service-kit/pyproject.toml:14-15` pin `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`;
`.docker/ray-cluster.dockerfile:62,69` runs `uv sync --package ratch`. So `service_kit` is importable in
every Ray Python process on the cluster.

New file `packages/service-kit/src/service_kit/ray_tracing.py`, workload-neutral, configured purely
from `OTEL_*`:

```python
def setup_tracing() -> None:                      # core hook — takes no args, returns None
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "ray")}),
        sampler=ParentBasedTraceIdRatio(float(os.environ.get("RASK_RAY_TRACE_SAMPLE_RATIO", "1.0"))),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

def serve_span_processors() -> list[SpanProcessor]:   # Serve hook — DIFFERENT contract
    return [BatchSpanProcessor(OTLPSpanExporter())]   # Serve builds the TracerProvider itself
```

Four non-negotiables, each of which costs a day if missed:

1. **`BatchSpanProcessor`, never `SimpleSpanProcessor`.** Both hooks Ray ships use the synchronous one,
   which puts an export round-trip on the task-submission hot path.
2. **Do not call `ray.get_runtime_context()` inside `setup_tracing`.** It runs at `worker.py:2777`,
   three lines before `set_is_connected(True)` at `:2781`, and `get_job_id()` raises there. Ray stamps
   `ray.job_id`/`node_id`/`task_id` itself at span time.
3. **The two contracts differ and must not be copy-pasted.** The core hook takes no args and returns
   `None`; the Serve hook takes no args and **returns a list of `SpanProcessor`**.
4. **`RAY_SERVE_TRACING_SAMPLING_RATIO` defaults to `0.01`.** A ten-request smoke test against the
   default produces **zero spans** and looks broken. And Serve tracing **fails soft** — a bad import is
   caught and logged, nothing goes unhealthy — so verify by observing spans in `opentelemetry_traces`,
   never by checking health.

`tracing-startup-hook` goes on the **head group only** (`rayservice.yaml:54-56`). The same key on a
`workerGroupSpec` is a **silent no-op**: it is persisted to GCS internal KV by `start_head_processes()`
and every connecting process reads it from there. The Serve env goes on **every** container.

Then **delete `_init_otel()` from `runners/htr/src/runner/htrflow_service.py:281-297`** — it builds a
TracerProvider that emits **zero spans** (no span is opened anywhere in `runners/`, and Serve's own
tracing is a separate unset switch), it is the **only** OTel code in any of the nine runners, and it is
one workload's private telemetry lane inside a sealed environment. Inert twice over, and a
CLAUDE.md dead-code removal.

Also: `scripts/ray_dummy_job.py` and `scripts/ray_lance_job.py` carry no `_traced_root`, so the
`TRACEPARENT` the submitter injects into the dummy lane (`ray_submit.py:122`) is **discarded**. Four
lines each once a shared `service_kit.ray_obs` exists — `service_kit` is importable from `/home/ray/jobs/`
in both images, and it is a platform package, so this does not breach the sealed-runner rule. Extend
the parity assertion at `tests/unit/test_ray_trace_continuity.py:225-229` to all four scripts.

---

## 7. `compute` is the estate's only Ray window, and it is unmeasured and structurally blind

`grep -rnE "get_meter|create_counter|create_histogram|create_up_down|ray.util.metrics" packages/ray-kit/ services/compute/`
→ **exit 1, no matches.** The only `opentelemetry` import in either tree is
`ray_kit/submit.py:36` (`from opentelemetry import propagate`), which forwards context and emits nothing.

Three compounding problems:

- **Every Ray failure is returned as HTTP 200 with `ok=false`.** So `FastAPIInstrumentor`'s
  `http.server.*` series — the one automatic signal compute has — is **structurally blind** to it. A
  totally dead Ray head produces a service that looks 100 % healthy on the only dashboard the estate
  ships: request rate normal, **error rate 0 %**, p95 fine, readiness green. The compute zone polls
  every 5 s (`ray-status.svelte:16`), so the failure is on screen continuously and in no time series
  at all. *(The `/api/serve/*` proxy is the exception — it returns 502, `dashboard.py:611-615`, tested.)*
- **The Ray Job SDK path is entirely uninstrumented.** `JobSubmissionClient` uses `requests`, not
  httpx, and `opentelemetry-instrumentation-requests` is a dependency nowhere in the repo. So the two
  most expensive and most failure-prone upstream calls have zero telemetry: `list_jobs` — the call that
  measured **164.7 MB / 81,155 jobs and OOM-killed the pod** — and the pruner's one-HTTP-DELETE-per-job
  loop. Meanwhile the raw-httpx routes **do** get client spans, so the trace view is actively
  misleading: the cheap reads are traced and the expensive ones look instantaneous.
- **A Ray auth failure is reported as "Ray dashboard unreachable".** On the `build_client → None` path
  (the dominant one under token auth, since the constructor round-trips) an `AuthenticationError` is
  logged at INFO and replaced by that fixed literal. Token rotation, a missing `RASK_RAY_AUTH_TOKEN`,
  or a scope mistake presents as a cluster outage — and the httpx routes report the *same* incident
  differently (`HTTPStatusError: 401`), so one incident yields two different causes on two routes.

**Fix:** add `opentelemetry-instrumentation-requests` at the `service_kit` seam (the same edit
`open_dapr_otel.md` §3 already needs for the lineage HTTP lane); add domain instruments in
`ray-kit`/`compute` — cluster reachability, jobs listed/submitted/failed, submission latency, prune
outcomes, job-history size (an **observable gauge**, not an UpDownCounter); and surface the auth
distinction rather than collapsing it. Also stop discarding what is already fetched: the State API's
truncation/partial-failure envelope, `/api/cluster_status`'s `autoscalingError`, the cluster's own Ray
version, and the run identity deliberately stamped into Ray `metadata` (which `compute` strips before
it can reach any surface).

Two upstream traps to encode while doing it:

- **State API truncation is applied at the data source, *before* filtering.** `list_tasks(filters=[('job_id','=',X)])`
  on a busy cluster can return nothing for job X because the first 10 000 rows contained none of them.
  A filter narrows the *output*, never the truncation window. And the docs say "> 100K" while the code
  defaults to `10 * 1000` — **a 10× disagreement**; size against 10 000.
- **The Python State SDK raises by default on any missing output**; pass `raise_on_missing_output=False`
  or one unavailable raylet turns a dashboard panel into a 500.

---

## 8. No Ray dashboard, no Ray alert — and a workload's name is already in the label values

`chart/templates/perses-dashboards.yaml` has six dashboards (`:40`, `:105`, `:170`, `:280`, `:334`,
`:416`) and **zero `ray_*` queries**. `chart/alerting/rules.yml` has fifteen alerts across seven groups
and **not one about Ray** (the only occurrence of "Ray" is prose at `:158`).

Both are worth doing and both are **strictly blocked on §2**.

When they land, port Ray's own shipped PromQL rather than inventing it — and note the traps:

- `ray_node_cpu_utilization` is a **percent (0–100)**, so cores-in-use is
  `sum(ray_node_cpu_utilization * ray_node_cpu_count / 100) by (instance)`.
- Object-store **capacity** lives in `ray_resources{Name="object_store_memory"}`, not in `ray_node_*`.
- `ray_tasks` is a **gauge** and must never be `rate()`d; Ray's own panel pairs
  `max_over_time(...[14d])` for terminal states with `clamp_min(...)` for live ones, because these
  gauges are eventually consistent.
- **Ray Data metric names in the docs are not the PromQL names.** The tables list
  `num_inputs_received`; it registers as `data_num_inputs_received` and the exporter prepends `ray_`,
  so the query is `ray_data_num_inputs_received`. **A rule written off the doc table verbatim can never
  fire.**
- **Counters are absent, not zero, until first recorded** — every Ray alert needs `absent()` or
  `or vector(0)`, the same lesson `open_dapr_otel.md` §7 records for Dapr.
- Start the group with `RayMetricsMissing: absent(ray_node_cpu_utilization)`, mirroring
  `DaprSchedulerMetricsMissing` (`rules.yml:280`) — without it every rule below is a green gate over
  nothing.

**And the agnosticism trap, which is load-bearing.** `chart/values.yaml:1458-1459` hardcodes
`serveRoutePrefix: "/htrflow"` and `importPath: "runner.htrflow_service:htrflow_app"`, so **a workload
name is already a label value on every Serve series** the moment §2 lands. The audit found the name
baked into platform-level config in **nine** places: `rayservice.yaml:12`, `:13-14`, `:32`;
`values.yaml:1458-1459`; `configmap.yaml:17`; `_helpers.tpl:138`; `gpu-coherence.yaml:6`, `:27`;
`frontends.yaml:144`, `:147`, `:279`; `.docker/ray-cluster.dockerfile:89` — plus a replica/GPU env
contract (`rayservice.yaml:17`, `:25`) that only `runners/htr` reads. **The chart cannot express a
second Serve application at all.**

That directly contradicts the platform's own ruling that no service, schema or chart may know a
workload's name. So: **every Serve and Data panel must group `by (application, deployment)` and every
rule must be written over those labels — never with a hardcoded filter.** Copy Ray's own discipline:
its shipped dashboards template `$Application`/`$Deployment`/`$DatasetID` and name nothing.

Model-level signals split cleanly by owner, and the split decides whether a fix is chart-side or
runner-side:

- **Free once §2 lands** (already emitted, currently unscraped): `ray_serve_deployment_processing_latency_ms`,
  `ray_serve_deployment_queued_queries`, `ray_serve_replica_processing_queries`,
  `ray_serve_replica_startup_latency_ms` (cold start), `ray_serve_deployment_error_counter_total{exception_type}`,
  `ray_memory_manager_worker_eviction_total` (OOM kills), and `ray_serve_actual_batch_size` /
  `ray_serve_batch_wait_time_ms` once `@serve.batch` is used.
- **Must be emitted by a runner** (nothing upstream provides it): per-model signals below the request
  boundary. Establish it as a workload-**neutral** template in `runners/dummy` — the honest lane
  prover — using `ray.util.metrics`, with the modality carried in **tag values**, never in the name.
  Two API rules to document in `runners/README.md`: every declared `tag_key` **must** be supplied at
  every record call (a missing tag raises `ValueError` inside the task — it does not drop the label),
  and `Counter.inc(0)` raises while `Gauge.set(None)` is a silent no-op.
- **GPU:** none at all today. DCGM is a comment in `values-prod.yaml`, and Ray's own GPU series are
  unscraped along with everything else.

---

## 9. What is already right — do not redo this

The Ray **job** lane is the best-instrumented Ray surface in the estate, by a distance, and several of
its details are subtle enough to be worth protecting:

- **Trace continuity across the Ray boundary genuinely works** on the two production paths.
  `trace_env()` injects **only when a span is active** and returns `{}` otherwise, so a trace is
  *continued* and never *fabricated* (`ray_kit/submit.py:98-100`, tested).
- **`_traced_root` handles the trap almost every implementation misses.** `SystemExit` is a
  `BaseException` that the SDK's `use_span` does **not** record — so a job failing its own verification
  would export a **green UNSET span for a failed job**. `scripts/ray_stage_job.py:356-364` explicitly
  records it and sets ERROR, and there is a test.
- **The two byte-identical inlined copies are drift-pinned by source comparison**
  (`test_ray_trace_continuity.py:225-229`), so the self-contained-job convention cannot silently diverge.
- **Telemetry fails soft everywhere** — a missing endpoint, a garbage traceparent, or an unimportable
  exporter degrades to running untraced rather than killing the job, with the garbage case parametrised
  over three malformed shapes.
- **Both submitters forward the pod's full OTLP config into `runtime_env`**, including the
  traces-specific GreptimeDB pipeline header the generic headers do not carry — so the job can actually
  export the span it opens. This is the correct workaround for Ray Jobs not inheriting container env.
- **The train job owns a complete OpenLineage lifecycle** START → RUNNING(progress) → COMPLETE|FAIL
  entirely from inside the job, with the output version facet on COMPLETE only and a version-less
  output plus an `errorMessage` facet on FAIL. It authenticates as the service it already is and keeps
  the HTTP status in the failure line, so a 401 stays distinguishable from an outage.
- **`emit_metrics` bounds label cardinality to `{model}` deliberately** and force-flushes + shuts down
  inline, because a short-lived process would otherwise drop its final export.
- **Run identity rides both channels for the right reasons** — `runtime_env.env_vars` so the job can
  stamp its own events, Ray `metadata` so the identity survives in `GET /api/jobs/<id>` after the pod
  is gone.
- **`list_jobs` sorts and caps before validating** — the fix for the measured 81,155-job OOMKill — and
  surfaces `total`/`truncated` so the cap is visible rather than silent. `job_logs` cuts the tail
  server-side with Ray's `lines=` rather than pulling the whole driver log.
- **The chart already knows the right Ray pod selector** and says why (`network-policy.yaml:240-243`),
  the NetworkPolicy already admits a Ray scrape without editing, the Collector's filelog **offsets are
  persisted** across restarts, the log-dedupe filter is keyed on the source (so a Ray sidecar will not
  be swallowed), GreptimeDB retention is applied database-wide, and the GPU posture is single-sourced
  and render-guarded.
- **The Ray image already carries the OTel SDK and OTLP exporter**, so §6 costs no new dependency.
- **Ray job history is bounded** by a Dapr cron, closing the listing that OOM-killed compute.
- **`ray_kit.schemas.RayJob` already declares `error_type`, `message`, `driver_exit_code`** — §5(c) is
  a wiring gap, not a plumbing gap.
- **The dummy runner drops non-personal principals from `originator`** rather than carrying a role
  literal into an inbox actor, and documents why.

---

## 10. Version skew — three answers in one estate

`chart/values.yaml:1450` pins `rayVersion: "2.56.1"`. The root `uv.lock` resolves **ray 2.57.0**, and
`packages/ratch` **requires** `ray>=2.57` — while `.docker/ray-cluster.dockerfile` builds ratch from
that lock. So the chart tells KubeRay one version and runs an image containing another.

It is `low` severity today (the two are close), but it is exactly the kind of drift that turns a
metric-name change into a silent dashboard outage. Pin it from one place, or add an invariant asserting
`ray.rayVersion` matches the lock.

---

## 11. The work, ordered

| # | Slice | Bucket | Effort | Why here |
| --- | --- | --- | --- | --- |
| ~~**1**~~ | ~~Fix the submission-id divergence (§1) + repair the defeated fake~~ **LANDED 2026-08-22** — `submit_stage_job` returns the posted id; `submit_stage` returns it; second derivation site deleted; two tests pin the contract | A | S | **Was a correctness bug, not telemetry.** `open_medallion_workflow.md` §12 corrected in the same change |
| **2** | Carry Ray's `message`/`error_type`/`driver_exit_code` into the FAIL facet (§5c) | A | S | The read-side plumbing already exists; turns a ticket into a diagnosis |
| ~~**3**~~ | ~~Specify + land the `ray-pods` scrape job (§2)~~ **REPO HALF LANDED / HANDED OVER 2026-08-22** — `ray-pods` job keyed on `ray.io/is-ray-node`, `metrics` port on the head, two render invariants; external half specified in `open_ray_handover.md` §1 | A + **B** | M | **Blocks §8 until the external cluster applies the handover** |
| **4** | `rask.rayOtelEnv` helper; move OTEL_* to container env; `lance.otelEnabled` gate; drop the workload literal (§3) | A | S | One helper deletes a fourth hardcoded copy and the externalize hole |
| **5** | Stage duration / rows / bytes instruments, covering the in-process branch too (§5b) | A | S | The cascade's own latency, currently unmeasurable |
| **6** | `requests` instrumentor + Ray-domain metrics in `ray-kit`/`compute`; stop returning 200 on Ray failure; separate auth from unreachable (§7) | A | M | The estate's only Ray window, currently unmeasured and misleading |
| **7** | `service_kit/ray_tracing.py`; wire the **Serve** switch first; delete `runners/htr`'s inert `_init_otel` (§6) | A + **B** | M | Serve tracing is the segment that joins the gateway trace to the model call |
| **8** | Ray log sidecar + `RAY_*_LOG_ENCODING=JSON`; outcome-aware prune (§4) | A + **B** | M | The Serve/JSON half needs no sidecar and helps immediately |
| **9** | A `ray.json` Perses dashboard, grouped `by (application, deployment)` (§8) | A | M | Blocked on 3 |
| **10** | A `ray` alert group, starting with `RayMetricsMissing` (§8) | A | S | Blocked on 3; and on `open_alert.md`'s decision |
| **11** | Trace continuity for `ray_dummy_job` / `ray_lance_job` (§6) | A | S | The smoke lane should prove the thing it smoke-tests |
| **12** | De-workload the nine chart sites; make a second Serve app expressible (§8) | A | M | A platform ruling, not a preference |
| **13** | Pin the Ray version once (§10) | A | S | Cheap |

**Slice 1 has landed (2026-08-22).** Slice 2 is the remaining non-observability item and should not wait for the telemetry work either.

---

## 12. How this sits against the other open plans — one correction and one false premise

Checked against `open_batch_process.md`, `open_ingest_design.md` and `open_medallion_workflow.md`
after the fact, because "consistent with the existing plans" is a claim, not a default.

**The plain result: this is the missing chapter, not a re-statement of theirs.**

| Doc | Lines | Telemetry mentions |
| --- | --- | --- |
| `open_medallion_workflow.md` | 453 | **0** |
| `open_batch_process.md` | 434 | 2 (B10, B11) |
| `open_ingest_design.md` | 1143 | 3, none about Ray telemetry |

`open_batch_process.md` §3 is a sixteen-point *"Ray checklist — tick before the transform ships"* and
**not one point concerns observability** — it is entirely driver lifetime, resource hygiene and
`runtime_env` discipline. So nothing here contradicts those plans by omission; they simply never
covered this ground, which is what `TODO.md:26` says.

**Two real points of contact, though, and they cut in opposite directions.**

**(a) B10 and B11 underwrite two of the fixes — and B10 corrected one of them.**
`open_batch_process.md` **B11** splits config into BOOT-ENV (*"S3 endpoint, Lance cache caps, **OTLP
endpoint**, FGA store ids — worker-derivable, restart to change"*) versus LIVE-SPEC. §3's fix — moving
`OTEL_*` out of `serveConfigV2.runtime_env` and into container env — is exactly that column, so it is
underwritten rather than invented. **B10** did the opposite: it names the Ray stage explicitly and
demands a measured `perf_counter` duration that also lands in the lineage facet, which invalidated an
earlier draft of §5's duration fix. That is corrected in place above.

**(b) `open_medallion_workflow.md` §12's sign-off rests on a premise §1 disproves.**
That review (2026-08-16) passed `stage_run` on all fifteen determinism rules and justified having **no
workflow management surface** with: *"its three exits are already distinguished (`succeeded` /
`abandoned` / `unnotified`)"*. In a deployed estate **`abandoned` is the only exit that ever fires** —
so the distinction the sign-off leans on does not exist in practice.

Be precise about what is and is not wrong there. §12 says the workflow's terminal-state literals are
*"test-pinned against `ray_kit`'s so the duplication cannot drift silently"* — and that pin is real and
holds. The defect is a **different** duplication: the submission-id derivation, whose own pin
(`test_stage_workflow.py:362-378`) is defeated by its fake. §12 is not wrong about the thing it pinned;
it reads as though both duplications were covered, and one is not. **When §1 lands, §12 needs a
correction note** — otherwise the design record continues to certify an operator surface on a
distinction that never held.

---

## 13. What is NOT in this file

- **Ray Event Export.** `RAY_enable_export_api_write` writes protobuf-derived JSON lines to files under
  the session dir — i.e. it lands in exactly the place §4 establishes nothing collects, so it is
  blocked on the log shipper and buys little over what `ray_*` metrics plus lineage already give.
  Designed in `docs/` with zero implementation; **recorded, not scheduled.**
- **Autoscaler observability.** The cluster has **no autoscaler**: `workerGroupSpecs: []` and no in-tree
  autoscaling. Half the upstream contract is inapplicable. **State it rather than build for it.**
- **The alerting decision.** `open_alert.md` owns it; `alerting.enabled: false` means slice 10 lands in
  a file nothing evaluates.
- **The Dapr/fleet half.** `open_dapr_otel.md`. Two shared items are recorded there and not duplicated
  here: the missing `requests`/`grpc`/`aiohttp` instrumentors at the `service_kit` seam, and the
  `observability.enabled` vs `lance.otelEnabled` gate divergence.
- **Anyscale's Grafana dashboard set.** `https://docs.anyscale.com/monitoring/grafana-dashboards` was
  **unreachable** from here (three attempts, TLS chain failure). The panel enumeration in §8 comes from
  Ray's own shipped dashboard modules at the pinned tag instead — which is what Anyscale re-serves.
  **Treat any Anyscale-only panel as unverified.**

---

**Sources.** Upstream contract read 2026-08-22 and verified against the **ray-2.56.1 git tag** — the
version `chart/values.yaml:1450` pins — not against the docs, which drift (the State API truncation
threshold differs by 10× between doc and code; Ray Data metric names in the doc tables are not the
PromQL names).

- <https://docs.ray.io/en/latest/ray-observability/user-guides/ray-tracing.html>
- <https://docs.ray.io/en/latest/ray-observability/user-guides/add-app-metrics.html>
- <https://docs.ray.io/en/latest/ray-core/api/utility.html#custom-metric-api-ref>
- <https://docs.ray.io/en/latest/ray-observability/user-guides/ray-event-export.html>
- <https://docs.ray.io/en/latest/ray-observability/user-guides/configure-logging.html>
- <https://docs.ray.io/en/latest/ray-observability/user-guides/cli-sdk.html>
- <https://github.com/ray-project/ray/tree/master/src/ray/observability> ·
  <https://github.com/ray-project/ray/tree/master/src/ray/protobuf>
- <https://docs.anyscale.com/monitoring/tracing> · <https://docs.anyscale.com/monitoring/tracing-jobs-workspaces>
- <https://docs.anyscale.com/monitoring/grafana-dashboards> — **unreachable, see §12**
- Skill references: `otel/references/{signals,attributes,python-sdk,collector}.md`,
  `python-infrastructure/references/observability.md`
