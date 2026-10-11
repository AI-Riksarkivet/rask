# chart/templates/otel-collector.yaml

Rationale for `chart/templates/otel-collector.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $o := .Values.observability }}`

OTel-first telemetry hub. A single OpenTelemetry Collector (contrib) that:
  - RECEIVES app OTLP (the apps point OTEL_EXPORTER_OTLP_ENDPOINT here — see lance.otlpEndpoint),
  - TAILS infra-pod stdout via the filelog receiver (the SINGLE log shipper — the Vector subchart is
    retired, owner ruling 2026-07-27), dropping from the app pods (lance.dev/logs=otlp) ONLY the lines
    their own SDK also exported — the uvicorn/crash/daprd tier on those pods has no OTLP twin and is kept,
  - SCRAPES the Dapr sidecars' :9090 via the prometheus receiver + k8s SD (the DaprConsumerWedge source),
and exports all three signals over OTLP/HTTP to `observability.export.endpoint`, the platform's backend
(ADR 0153; in development the observability fixture's GreptimeDB). The backend's ingestion headers come from
values: `export.headers` on every signal, `export.traceHeaders` on traces only and `export.auditHeaders` on the
audit trail, each a separate exporter because the others must NOT carry them. The endpoint is `required`: a
Collector with nowhere to send fails the render rather than queueing into nothing.

Deploys whenever observability.enabled. Single Deployment (not a DaemonSet): filelog reads the node it lands
on, which is correct on a single node. Under NetworkPolicy its egress to an endpoint outside the namespace
needs a `networkPolicy.extraEgress` entry.

## `{{- $daprState := and .Values.dapr.enabled .Values.stateStore.enabled (or .Values.age.enabled .Values.age.e...`

The Dapr state store's own database is only worth querying when there IS one: the state store
component must be enabled (nothing writes workflow history otherwise) and Postgres must exist,
either in-cluster or as age.externalHost. Gated rather than always-on because a receiver that
cannot connect retries forever and fills the log with a failure that is not one.

## `apiVersion: rbac.authorization.k8s.io/v1`

k8sattributes needs pod/namespace/replicaset/node reads for enrichment; the prometheus receiver's k8s SD
needs pod discovery. Cluster-scoped (SD + attributes are cluster-wide), read-only.

## `sqlquery/daprstate:`

DAPR WORKFLOW HISTORY — measured by QUERY, because there is nothing to scrape.

Every other metric in this file arrives from a target that already exports it. This one cannot:
enumerated live on 2026-08-26 against the `ingest` sidecar, daprd's ENTIRE metric surface is
dapr_error_code_total, dapr_grpc_io_*, dapr_http_*, dapr_runtime_component_{init_total,loaded}
and go_*. No workflow metric, no actor metric, no state-store metric — and
`dapr_runtime_workflow_*` (already documented as unreliable for this codebase in
deploy/alerting/rules.yml) is absent outright. A fourth scrape job would have had no target.

So the state store is read directly. `spec.workflow.stateRetentionPolicy` in observability.yaml
is the ONLY thing that removes workflow history — there is deliberately no application-side
purge (DaprWorkflowClient has no list-instances API, so a sweep could not name what to purge
after a restart) — and if it stops collecting, the first symptom is a full disk. These two
gauges are what deploy/alerting/rules.yml's `dapr-workflow-state` group alerts on, so a rename
here has to reach those rules in the same change.

A COLLECTOR RECEIVER RATHER THAN A postgres_exporter SIDECAR: no second image to vendor, scan
and size, no second credential path, and the query lives in the file that already owns "get
this series into GreptimeDB". Cost measured on the live store: 4.7ms over a 7.5MB table.

THE AGE METRIC READS HISTORY ROWS ONLY, and that filter is load-bearing rather than tidy. A
workflow's `metadata` row keeps its original insertdate across `continue_as_new`, so a
long-running recurring workflow — which this estate has, in the stage runners' watch loops — would
look arbitrarily old forever and the alert would fire on a perfectly healthy store. History
rows are rewritten per turn, so their age is the property the retention policy actually
governs.

DO NOT "CORRECT" `%||history-%` TO `wf-history-%`, however authoritative the source. Dapr's own
protocol reference (contributing/protocol-reference/workflow-protocol/…-state-and-history) states
the history key format is `wf-history-<instance_id>-<index>`. That is the LOGICAL key; it is not
what the Postgres state store holds. Measured on the live store 2026-08-26:
key like '%wf-history-%'  ->     0 rows
key like '%||history-%'   ->  5551 rows
The real shape is `<app>||dapr.internal.default.<app>.workflow||<id>||history-000006`. A filter
taken from the doc matches nothing, the gauge is a permanent 0, and DaprWorkflowHistoryNotCollected
can never fire — which is indistinguishable from a healthy estate. Same class of defect as
`outbox_oldest_age`.

The password is an ENV REFERENCE, not a literal: the Collector expands ${env:...} at load, and
the value arrives by secretKeyRef on the Deployment below, so no credential is in this ConfigMap.

## `- metric_name: dapr_workflow_state_rows`

Volume. Read by DaprWorkflowStateMetricsMissing and by nothing else — its job is to be
PRESENT, so that the age rule going quiet can be told apart from the age rule going blind.

## `- metric_name: dapr_workflow_history_oldest_age_seconds`

The property. NO `unit:` field, and the name carries `_seconds` itself — declaring a unit
would make the OTLP->Prometheus convention APPEND the suffix, which is exactly how
`outbox_oldest_age` shipped as a rule that could never fire (see rules.yml:22-27).

## `filter/drop_app_file_logs:`

The app pods already export OTLP logs (trace-correlated) — drop the file-tailed duplicate. Mirrors
what the retired Vector agent's `lance.dev/logs!=otlp` selector did. error_mode ignore: a pod without the label is kept.

BOTH CLAUSES ARE LOAD-BEARING, and the second was missing until 2026-08-15. The pod label alone
cannot tell the two copies apart, because it is a property of the POD and both copies come from
the same pod: `k8sattributes` associates filelog records by `k8s.pod.uid` and OTLP records by
`{from: connection}`, stamping `lance.dev/logs=otlp` on both. So the label-only condition dropped
the OTLP ORIGINAL along with its duplicate — the entire application log tier, silently, while
every pipeline reported healthy. Measured live: `opentelemetry_logs` held 60.4M rows and not one
with a `medallion`/`lineage`/`catalog` scope; only unlabelled infra pods survived.

`log.file.path` is the discriminator: the filelog receiver sets it (`include_file_path: true`
above) and OTLP records never carry it. Keyed on the SOURCE, the filter drops what it was
written to drop and nothing else.

## `filter/audit_only:`

THE AUDIT SPLIT. `lance.audit` is the governance trail, not telemetry, and it rode in
`opentelemetry_logs` under the database's 14d TTL — measured 2026-09-18: 8,590,872 audit rows
among 114,938,016. These two filters are COMPLEMENTARY on purpose, so each record lands in
exactly one table: a split that copied rather than routed would double the store and leave the
14-day copy authoritative. The filter processor DROPS what its conditions match, which is why
the audit pipeline's condition is the negation.

Keyed on `body`, the same discriminator `audit-core.ts` used to select these rows out of the
shared table, so the routing and the reader cannot disagree about what an audit record is.

## `filter/drop_keepalive:`

The workflow engine's liveness ping to its own sidecar, dropped before it reaches the store.
RE-MEASURED 2026-08-23 rather than inherited (two audits put its share 40 points apart):
3,828 spans in three hours, ninth-largest span name in the estate, carrying nothing anyone
reads. Scoped to the EXACT name, not a prefix, so a real TaskHubSidecarService call still
traces. `error_mode: ignore` so a span with no name cannot wedge the pipeline.

Proportion matters here: this is the SMALL half of the trace noise. The kubelet probe spans
fixed in the same change were 31,916 over the same window, and those are excluded at the SDK
(rask.otelEnv's OTEL_PYTHON_FASTAPI_EXCLUDED_URLS) because the app owns them — dropping them
here instead would pay to serialize and ship spans only to discard them at the far end.

## `otlphttp/traces:`

Traces carry `export.traceHeaders` (GreptimeDB's trace-pipeline header → opentelemetry_traces); metrics/logs
must NOT, so they get a separate exporter with only `export.headers`. Both hit the same /v1/otlp base; the
otlphttp exporter appends /v1/{traces,metrics,logs} per signal (same as the app SDK did).

DURABILITY. Both exporters queue and retry; `batch` alone is not durability — it holds a
batch in memory and forgets it if the process dies. That was not theoretical: the store these
write to OOMKilled 13 times on 2026-08-23 and every one was a hole a queue would have ridden out.

`storage: file_storage` makes the queue survive a Collector restart, and the id MUST appear in
`service.extensions` below — it does. A queue naming an unregistered extension renders fine,
passes `otelcol validate` with exit 0, and then kills the Collector at startup with
`no storage client extension found`, taking the whole telemetry plane down to fix a
durability gap. That trap is why this was deferred once; the gate now checks the reference
resolves, not merely that a queue exists.

BOUNDED, because the backing emptyDir is shared with filelog's read offsets: an unbounded
queue would trade dropped telemetry for a filled node. `queue_size` is explicit here and the
volume carries a `sizeLimit`. `max_elapsed_time` is finite for the same reason — past five
minutes the queue, not the retry loop, is what carries the data.

## `otlphttp/audit:`

The audit trail's own table, named by `export.auditHeaders`. `x-greptime-log-table-name` is what
GreptimeDB's OTLP log ingestion routes on — verified against the running v1.1.1 rather than taken from docs: a
protobuf record carrying the header created the named table. Same endpoint, same durability
settings as the shared exporter; only the destination table differs.

## `traces:`

THE WORKFLOW KEEPALIVE IS FILTERED, by `filter/drop_keepalive` in the traces pipeline
below. `/TaskHubSidecarService/Hello` is the engine's health ping to its own sidecar and
carries nothing anyone reads. Two earlier audits put its share of the grpc scope 40
points apart, so it was re-measured before acting rather than taken on trust: 3,828 spans
in three hours, the ninth-largest span name in the estate.
Worth knowing for proportion — it was never the big one. The kubelet probe spans fixed in
the same change were 31,916 over the same window, and those are excluded at the SDK
(`rask.otelEnv`'s OTEL_PYTHON_FASTAPI_EXCLUDED_URLS) because the app owns them.

## `logs/audit:`

OTLP ONLY. An audit record is emitted by a service through the SDK; a `body == "audit"` line
arriving from `filelog` is a container log that happens to read that way, and writing it to
the governance table would make the trail forgeable by anything that can print.

## `{{- include "lance.rootlessSecurityContext" 65534 | nindent 10 }}`

The same uid the pod already pins. Everything the Collector writes goes to the
`offsets` volume below (the file_storage extension's checkpoints); the config is a
read-only mount, so a read-only rootfs takes nothing away from it.

## `env:`

The Dapr state store's Postgres password, for the sqlquery/daprstate receiver. By
secretKeyRef so it stays out of the ConfigMap, `helm get manifest` and `kubectl describe`;
the config references it as ${env:DAPRSTATE_PASSWORD}, expanded by the Collector at load.

## `livenessProbe: { httpGet: { path: /, port: 13133 }, initialDelaySeconds: 30, periodSeconds: 30, failureThre...`

[[XC-059]] READINESS GATES TRAFFIC; ONLY LIVENESS RESTARTS. Kubernetes answers a
persistent Ready-false by routing around the pod, and there is nothing to route around a
singleton every signal in the estate flows through — a wedged Collector would simply stay
Ready-false forever while every trace, metric and log stopped. Same `health_check`
endpoint, so this costs no new surface, only a second probe that acts on the answer.
DELIBERATELY SLACKER THAN READINESS: restarting is the heavier action, so it must be the
later one. 30s before the first check and 3 failures at 30s apart means a genuinely
wedged Collector is restarted inside ~2 minutes, while a slow start or a single blip is
not — a liveness probe that fires before readiness turns a slow start into a crash loop,
which is how these end up net-negative.

## `- { name: varlogpods, mountPath: /var/log/pods, readOnly: true }`

filelog reads the node's container logs (RO). /var/log/pods symlinks into /var/lib/docker or
containerd, so mount both roots (the second is where the real files live on most nodes).

## `- { name: offsets, mountPath: /var/lib/otelcol/file_storage }`

filelog offset checkpoints (file_storage extension) — writable via fsGroup 65534.

## `- { name: offsets, emptyDir: { sizeLimit: 2Gi } }`

sizeLimit because this dir now backs the exporters' persistent sending_queue as well as
filelog's read offsets — without it a store outage trades dropped telemetry for a full node.

## `- { name: export-credential, secret: { secretName: {{ $sa }}-export, defaultMode: 0440 } }`

0440 under the pod's fsGroup 65534, the group the Collector's uid reads its SA token through already.
