# The Ray head, line by line

Why the ray-cluster fixture's head (`chart/templates/raycluster.yaml`) is shaped as it is. Each section is headed by
the rask-chart line that rendered the setting; the setting itself is in `chart/values.yaml` here (ADR 0153, XC-125).

## `authOptions:`

Token auth (gate 7 / R3), the NATIVE kuberay >= 1.6.0 wiring: authOptions.secretName
points the operator at the chart's pre-existing <release>-ray-auth-token Secret
(data key `auth_token` = the operator's RAY_AUTH_TOKEN_SECRET_KEY convention, so the
operator SKIPS generating its own Secret — raycluster_controller.reconcileAuthSecret
returns early when secretName is set). The operator then injects RAY_AUTH_MODE=token +
RAY_AUTH_TOKEN (secretKeyRef) into EVERY Ray container — head, workers, autoscaler —
and, critically, the RayService controller's dashboard client reads THIS secret to
authenticate its own /api/serve reconcile calls (utils.GetRayDashboardClientFunc);
the old manual head-env pattern never told the controller, so it 401-wedged at
WaitForServeDeploymentReady. Fleet consumers (compute) still get the pair via
rask.rayAuthEnv — that half stays chart-wired. Requires rayVersion >= 2.52.0
(operator validation); we pin {{ .Values.ray.rayVersion }}.

## `tracing-startup-hook: "service_kit.ray_tracing:setup_tracing"`

Ray CORE tracing. HEAD ONLY, and that is not a simplification: the hook is persisted to
GCS internal KV by `start_head_processes()` and every Python process that connects reads
it from there — that IS the propagation mechanism, so the same key on a workerGroupSpec
is a silent no-op.

The honest ceiling, so nobody over-invests: upstream documents core tracing as Alpha and
"no longer under active development", and Ray Data / Train / Tune contribute ZERO spans
of their own. The payoff is generic per-task spans, not dataset or operator spans. The
job-level continuity that actually matters is already built by hand — ray_kit.trace_env
injects TRACEPARENT and the job scripts parent on it.

## `serviceAccountName: {{ include "lance.fullname" . }}-sa-ray`

Its own tokenless SA: no autoscaler runs, so nothing in the head calls the API.

## `{{- if include "rask.gpuEnabled" . }}`

The nvidia RuntimeClass exists only on a GPU estate (the device plugin's install provides it;
in development deploy/fixtures/nvidia-device-plugin), and naming a RuntimeClass whose handler
containerd does not register makes every head pod fail to create. GPU-less => no runtimeClassName at all,
so the head runs under the node's default runtime.

## `- name: RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH`

Ray SERVE tracing — a SEPARATE switch from the core hook above, with a DIFFERENT
contract: this import path must return a list of SpanProcessor (Serve builds the
provider), while the core hook returns None and sets it. Cross-wiring them fails
SOFT — Serve catches the bad import, logs "the proxy/replica will continue
running", and nothing goes unhealthy — so verify by observing spans in
`opentelemetry_traces`, never by checking health.

This is the higher-value half of the two: Serve honours an INBOUND traceparent,
so it is what joins a gateway-originated trace to the model call. Ray's own
monitoring docs never mention the switch exists.

Container env, not serveConfigV2: replicas are scheduled wherever, and every
future workerGroupSpecs container needs these two lines too.

## `- name: RAY_SERVE_TRACING_SAMPLING_RATIO`

DEFAULT IS 0.01 — one request in a hundred. A ten-request smoke test against the
upstream default produces zero spans and reads as "tracing is broken".

## `- name: RAY_LOGGING_CONFIG_ENCODING`

STRUCTURED RAY LOGS. Two separate planes again, and the Serve half is the one
that pays off immediately: Serve replicas default to STDERR, which the Collector's
filelog receiver already tails, so these logs are being ingested UNPARSED right
now — one body string, no severity, no job or replica id to filter on. Setting the
encoding makes them queryable without any shipper work.

The CORE half (task/actor/driver) is different: Ray writes those to files under
/tmp/ray inside the container, which nothing mounts and nothing tails, so JSON
alone does not ship them — that needs the log sidecar in open_ray_handover.md
section 3. Set here anyway so the records are structured the moment a shipper
lands, rather than needing a second rollout to become useful.

Must be set BEFORE `import ray`, which a container env satisfies by construction.
Two documented traps: the emitted key is `task_func_name`, not the
`task_function_name` the prose promises, and there is no `node_ip_address` field
(it is `node_id`). RAY_BACKEND_LOG_JSON is deliberately NOT set — it converts only
the Job Supervisor among the Python system components, so it buys almost nothing
while reading as though it structured the lot.

## `- name: RAY_ENABLE_UV_RUN_RUNTIME_ENV`

Ray-container auth env is NOT set here: spec.authOptions above makes the
kuberay 1.6+ operator inject RAY_AUTH_MODE/RAY_AUTH_TOKEN into every Ray
container itself (SetContainerTokenAuthEnvVars, skip-if-already-set), head
AND any future workerGroupSpecs entry — one wiring, zero drift.
rask.rayAuthEnv remains the FLEET-consumer half (compute).

## `- name: S3_ENDPOINT`

THE JOB SECRETS LIVE ON THE POD, NOT IN THE SUBMISSION (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit"
P0, fixed 2026-08-28). They rode `runtime_env.env_vars`, and the Ray Jobs API
echoes runtime_env back on `GET /api/jobs/<id>` — an unauthenticated dashboard,
proxied by compute at /api/ray/*, published at the edge: one GET yielded the
estate's service credential. Ray merges runtime_env OVER the process env, so
holding them here keeps every job's `os.environ` contract byte-identical while
the submission body carries nothing worth echoing.
secretKeyRef onto Secrets the estate ALREADY owns — the same objects
ExternalSecrets syncs from OpenBao on the prod path. A literal value: here would
put the credential in `helm get manifest`, the exact class the infra-credentials
header documents for the Dex secret. Any future workerGroupSpecs container needs
these entries too (jobs run on workers).

BOTH HALVES FROM ONE OBJECT, and that is the fix rather than tidiness. The key
used to arrive only through the submitter's `runtime_env` while the secret came
from here, so the pair had two owners: repointing the pod alone gave every job
`SignatureDoesNotMatch`, and repointing the stage runner alone took the STAGE RUNNER down
(it does its own S3 work — `outbox.stage_event` issues a HeadBucket, which needs
an unconditioned ListBucket a scoped policy refuses). Both measured 2026-08-30.
`ray-compute-*` defaults to the root credential, so this is inert until an
operator provisions the scoped MinIO user. NOTHING credential-shaped rides the
submission any more, so this pod is the single source and there is no second
value to keep in agreement.

THE ADDRESS THE CREDENTIAL ABOVE IS USED AGAINST, owned by the same pod for the
same reason. `scripts/ray_stage_job.py:86` reads `os.environ["S3_ENDPOINT"]` with a
BRACKET, so a job that does not get it dies before reading a byte — and until this
row existed the only thing supplying it was the SUBMITTER, which injected it into
`runtime_env.env_vars` on every submission (measured 2026-09-18 on the deployed
head: `S3_KEY` and `S3_SECRET` present, `S3_ENDPOINT` absent).

An endpoint is NOT a secret — it is public routing information, and an inline
value is the right delivery for it where a `secretKeyRef` would only hide it from
the reader who needs it. That asymmetry is pinned by
`tests/unit/test_the_ray_pod_owns_every_name_its_jobs_require.py`.

It is also what unblocks [[LH-159]]: routing `workflow.py` through
`executor_for(RAY_ENGINE)` sends `WorkOrder.to_env()` and nothing else, and
`WorkOrder` is `extra="forbid"`, so a deployment fact cannot ride the order.

## `- name: S3_REGION`

The literal, matching `services.yaml:121` and both maintenance templates. A new
`minio.region` value would be a fourth spelling of one constant that nothing in
this estate varies, and a `| default` on it would keep an undeclared key from
ever reaching the manifest as nil, so nothing would flag the new spelling.
The convention is what keeps the spelling from multiplying. Inert today —
`scripts/ray_stage_job.py:89` defaults to the same string — and here so the pod
owns the whole pair rather than half of it.

## `{{- include "lance.lineageEmitEnv" (list .) | nindent 16 }}`

The lineage transport for the jobs that emit (ray_train_job.py through lineage-kit,
which packages/ray-cluster-env carries); the stage job emits nothing and reports its
outcome to its planner's door instead (CP-029). The head is ONE account, `-sa-ray`:
lineage maps its `rask-lineage` token, and a stage runner's outcome door its
`rask-medallion` one, to `trainerIdentity` ([[LH-220]] D1).

## `- {containerPort: 8080, name: metrics}`

Ray's Prometheus endpoint. KubeRay injects this containerPort itself, but the
Collector's `ray-pods` job keeps on the port NAME, and a relabel cannot see a port
the rendered pod spec does not advertise — so an operator-injected default would
have produced zero targets with nothing to indicate why. Declared here, the head's
telemetry surface is a property of this manifest rather than of the operator's
defaults.
Every future workerGroupSpecs container needs the same entry — workers serve their
own per-node metrics and are scraped by the same job.

## `{{- if include "rask.gpuEnabled" . }}`

Omitted when GPU-less rather than rendered as `nvidia.com/gpu: 0`: an extended
resource the cluster never advertises has no business appearing in the pod spec —
it makes the manifest claim a GPU plane that does not exist, which is exactly what
sent the live-proof debugging at the head pod instead of at the Serve actor.

## GCS fault tolerance: not in this fixture

The rask chart's `ray.cluster.gcsFaultTolerance` (an embedded RocksDB GCS store on a ReadWriteOnce claim, so a head
restart keeps the job records the cascade polls) is not carried here; it was off on the estate. An engine that wants a
known terminal state across a head restart adds it: Ray documents the embedded backend as alpha, Linux-only and
single-writer, and it persists cluster metadata, not the driver.

## `upgradeStrategy: { type: Recreate }`

A CHART CHANGE TO THE POD SPEC REACHES THE RUNNING PODS. KubeRay 1.6.2 recreates a RayCluster's
pods on a spec change only under `Recreate` (raycluster_controller.go shouldRecreatePodsForUpgrade);
otherwise the head keeps the spec it started with, its ServiceAccount included. The hash leaves out
replicas, so autoscaling recreates nothing. KubeRay refuses the field on a cluster a RayService creates
(validation.go ValidateRayClusterUpgradeOptions), so a RayService-shaped engine leaves it out.
