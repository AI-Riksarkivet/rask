# deploy/fixtures/observability/chart/templates/collector.yaml

Rationale for the observability fixture's `templates/collector.yaml`, kept beside the fixture rather than in its
templates. Each section is headed by the template line it explains.

## `{{- $c := .Values.collector }}`

The development stand-in for the platform's OTLP gateway (ADR 0215). rask's product chart renders no Collector: each
lakehouse service (catalog, lineage, maintenance-worker) exports OTLP/HTTP straight to `observability.export.endpoint`,
here `http://otel-collector.observability:4318`, presenting its pod's projected ServiceAccount token for
`observability.identity.audience` (`rask-otlp`) as a bearer. This Collector:

- ADMITS an export on OTLP/HTTP only with such a token (the `oidc` extension),
- ROUTES the `lance.audit` instrumentation scope to its own GreptimeDB table (`store.auditTable`, `lance_audit`) and
  every other signal to the shared tables,
- TAILS every pod's stdout with the filelog receiver (the single log shipper), dropping from rask's pods only the lines
  their SDK also exported,
- SCRAPES OpenFGA's metrics port through the prometheus receiver and Kubernetes service discovery,
- TAKES OpenFGA's traces on an unauthenticated OTLP/gRPC receiver (`otlp/platform`, rask's
  `openfga.telemetry.trace.otlp.endpoint` names `otel-collector.observability:4317`).

A platform's gateway must do the first two; how it ships logs and scrapes is its own. Single Deployment (not a
DaemonSet): filelog reads the node it lands on, which is correct on a single development node.

## `apiVersion: rbac.authorization.k8s.io/v1`

k8sattributes needs pod/namespace/replicaset/node reads for enrichment; the prometheus receiver's Kubernetes service
discovery needs pod discovery. Cluster-scoped (both are cluster-wide), read-only. No `nodes/proxy`: every scrape is
`role: pod` and no receiver reads the kubelet, and the subresource hands its holder the kubelet's API through the API
server, exec into every pod on the node included (a websocket upgrade is a GET) ([[XC-202]]).

## `- name: cluster-keys`

The `oidc` extension verifies a token's signature against the issuer's keys. It reads them from
`public_keys_file` rather than by discovery, because the in-cluster issuer (`https://kubernetes.default.svc.cluster.local`)
serves its discovery document and keys from the API server, behind the cluster's own CA and authentication. This init container reads `/openid/v1/jwks` from the API server with the Collector's own
ServiceAccount token and CA: every ServiceAccount may read it through the default `system:service-account-issuer-discovery`
ClusterRoleBinding (bound to `system:serviceaccounts`), so the fixture grants nothing extra for it.

The keys are read once, at pod start. A rotated cluster signing key is therefore refused until the Collector restarts
(`kubectl -n observability rollout restart deploy/otel-collector`); a platform gateway that fetches the issuer's
discovery itself has no such step.

## `oidc:`

The token check. `issuer_url` must equal the token's `iss` and `audience` its `aud`, so a ServiceAccount token minted
for the API server (or any other audience) is refused: a service's export proves which pod sent it, and nothing
reaches the store by where it stands on the network.

## `otlp/platform:`

OTLP/gRPC with no authenticator, for the platform's own traces: OpenFGA exports its spans here and has no token of the
audience to present. It feeds the `traces` pipeline ONLY: no log pipeline takes it, so nothing unauthenticated can
write a log line, let alone the audit trail.

## `exclude: [/var/log/pods/{{ .Release.Namespace }}_otel-collector-*/*/*.log]`

The Collector does not tail its own log: an export error it logs would be shipped through the same failing path and
logged again.

## `filter/drop_app_file_logs:`

rask's pods already export OTLP logs (trace-correlated); drop the file-tailed duplicate. error_mode ignore: a pod
without the label is kept.

BOTH CLAUSES ARE LOAD-BEARING. The pod label alone cannot tell the two copies apart, because it is a property of the
POD and both copies come from the same pod: `k8sattributes` associates filelog records by `k8s.pod.uid` and OTLP
records by `{from: connection}`, stamping `lance.dev/logs=otlp` on both. A label-only condition drops the OTLP ORIGINAL
along with its duplicate, the entire application log tier, silently, while every pipeline reports healthy. Measured
live 2026-08-15: `opentelemetry_logs` held 60.4M rows and not one with a `lineage`/`catalog` scope; only unlabelled
infra pods survived.

`log.file.path` is the discriminator: the filelog receiver sets it (`include_file_path: true` above) and OTLP records
never carry it. Keyed on the SOURCE, the filter drops what it was written to drop and nothing else. The third
condition narrows the drop to lines that open with a date, the services' own log format, so the uvicorn/crash tier on
those pods, which has no OTLP twin, is kept.

## `filter/audit_only:`

THE AUDIT SPLIT. `lance.audit` is the governance trail, not telemetry, and in the shared `opentelemetry_logs` it would
sit under the database's 14d TTL (measured 2026-09-18: 8,590,872 audit rows among 114,938,016). `filter/audit_only`
and `filter/drop_audit` are COMPLEMENTARY on purpose, so each record lands in exactly one table: a split that copied
rather than routed would double the store and leave the 14-day copy authoritative. The filter processor DROPS what its
conditions match, which is why the audit pipeline's condition is the negation.

ADMITTED BY SUBJECT. Any ServiceAccount of the cluster can ask the kubelet for a token of the gateway's audience, so the
audience admits telemetry and never the governance trail. `attributes/sender` stamps each record with the subject the
oidc extension verified (`from_context: auth.subject`), and the second condition drops an audit-scope record whose
`rask.sender` is absent or outside `collector.identity.auditSubjects`, the lakehouse's own ServiceAccounts. The filter
runs with `error_mode: propagate`: a condition that cannot be evaluated refuses the batch, where `ignore` would keep the
record.

Keyed on the record's instrumentation scope, never its text. The SDK's `LoggingHandler` names the scope after the
Python logger that made the record (`opentelemetry-sdk` 1.45.2, `_logs/_internal/__init__.py:711`), so
`instrumentation_scope.name == "lance.audit"` selects exactly that logger's records: a line on another logger whose
message reads `audit` stays out of the trail, and a record `audit()` writes is in it whatever its message. What keeps
a second writer off that logger is `make lint`: ruff's TID251 bans `AUDIT_LOGGER` and `_log` outside
`lakehouse.governed.audit`, and `scripts/one_audit_writer.py` refuses the logger's name as a string.

## `otlphttp/traces:`

Traces carry GreptimeDB's trace-pipeline header (`x-greptime-pipeline-name: greptime_trace_v1`, into
`opentelemetry_traces`); metrics and logs must NOT, so each destination is its own exporter. All three hit the same
`/v1/otlp` base; the otlphttp exporter appends `/v1/{traces,metrics,logs}` per signal.

DURABILITY. Every exporter queues and retries; `batch` alone is not durability: it holds a batch in memory and forgets
it if the process dies. The store these write to OOMKilled 13 times on 2026-08-23, and every one was a hole a queue
rides out.

`storage: file_storage` makes the queue survive a Collector restart, and the id MUST appear in `service.extensions`
below; it does. A queue naming an unregistered extension renders fine, passes `otelcol validate` with exit 0, and then
kills the Collector at startup with `no storage client extension found`.

BOUNDED, because the backing emptyDir is shared with filelog's read offsets: an unbounded queue would trade dropped
telemetry for a filled node. `queue_size` is explicit and the volume carries a `sizeLimit`. `max_elapsed_time` is
finite for the same reason: past five minutes the queue, not the retry loop, carries the data.

## `otlphttp/audit:`

The audit trail's own table, named by `x-greptime-log-table-name`, what GreptimeDB's OTLP log ingestion routes on,
verified against the running v1.1.1 rather than taken from docs: a protobuf record carrying the header created the
named table. Same endpoint and durability settings as the shared exporter; only the destination table differs.

## `traces:`

Every span rask's services and OpenFGA export, with no filter: the noise worth dropping is the kubelet's probe spans,
31,916 in three hours (measured 2026-08-23), and those are excluded at the SDK (rask's `lance.otelEnv`,
`OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`) because the app owns them.

## `logs/audit:`

The token-checked `otlp` receiver ONLY. An audit record is emitted by a service through the SDK, under the
`lance.audit` scope, on an export that proved its ServiceAccount; a line arriving from `filelog` is a container log
and carries no scope a service set, and `otlp/platform` admits anyone on the network, so admitting either here would
make the trail forgeable by anything that can print or dial.

## `checksum/config: {{ include "observability.collectorConfig" . | sha256sum }}`

The Collector rolls when its own config changes, and on nothing else: the hash is over the ConfigMap this template
renders (`observability.collectorConfig`, defined at the end of the file so the ConfigMap and this annotation read one
rendering).

## `livenessProbe: { httpGet: { path: /, port: 13133 }, initialDelaySeconds: 30, periodSeconds: 30, failureThreshold: 3 }`

[[XC-059]] READINESS GATES TRAFFIC; ONLY LIVENESS RESTARTS. Kubernetes answers a persistent Ready-false by routing
around the pod, and there is nothing to route around a singleton every signal in the estate flows through: a wedged
Collector would stay Ready-false while every trace, metric and log stopped. Same `health_check` endpoint, so this
costs no new surface, only a second probe that acts on the answer. DELIBERATELY SLACKER THAN READINESS: restarting is
the heavier action, so it must be the later one. 30s before the first check and 3 failures 30s apart restarts a
genuinely wedged Collector inside ~2 minutes, while a slow start or a single blip is not restarted; a liveness probe
that fires before readiness turns a slow start into a crash loop.

## `- { name: varlogpods, mountPath: /var/log/pods, readOnly: true }`

filelog reads the node's container logs (read-only). `/var/log/pods` symlinks into `/var/lib/docker` or containerd, so
both roots are mounted (the second is where the real files live on most nodes).

## `- { name: offsets, emptyDir: { sizeLimit: 2Gi } }`

The file_storage extension's directory: filelog's read offsets and the exporters' persistent sending queues, writable
through fsGroup 65534. The `sizeLimit` keeps a store outage from trading dropped telemetry for a full node.
