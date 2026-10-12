# chart/templates/medallion.yaml

Rationale for `chart/templates/medallion.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: apps/v1`

─────────────────────────────────────────────────────────────────────────────────────────────────
medallion-producer — the HEAD of the medallion pipeline, and it writes no tier (ADR 0154: data enters only
through the catalog's namespace doors). Its /bronze-arrival subscription reacts to the catalog's COMPLETE
announcement of a write to `<project>-bronze$events` and publishes the first trigger (medallion.bronze) the
bronze→silver stage runner consumes. Its one write door is POST /train.
─────────────────────────────────────────────────────────────────────────────────────────────────

## `- { name: LANCE_AUDIT_ENABLED, value: {{ hasKey $root.Values.auth "audit" | ternary $root.Values.auth.audit...`

#41. The SAME `auth.audit` key the catalog reads, because this service declares the
same `LANCE_AUDIT_ENABLED` alias and the chart rendered it into the catalog alone —
measured live 2026-09-09, the lever reached ONE service of ten while every other pod
ran on its field default (§ Q17-27). hasKey+ternary for the reason recorded at the
catalog's copy: `| default` makes an explicit false un-settable, and a bare reference
renders "" on a `--reuse-values` upgrade, which is a bool_parsing crash at boot.

## `- { name: MEDALLION_DAPR_ENABLED, value: {{ .Values.dapr.enabled | quote }} }`

Fail-closed at boot if behind a sidecar but the app-token is unset — /bronze-arrival would
otherwise be an open forged-trigger path (symmetric with the stage runners).

## `{{- include "lance.appTokenEnv" (list $root $root.Values.medallion.producer.daprAppId) | nindent 12 }}`

verify the Dapr-stamped header on /bronze-arrival (forged-trigger guard)

## `- { name: MEDALLION_PUBSUB, value: {{ include "lance.subPubsub" (list $root .Values.medallion.producer.dapr...`

medallion-producer's OWN subscriber component (queueGroupName=medallion-producer, deliverPolicy=new) — it
subscribes on /bronze-arrival AND publishes through the same handle (same NATS subjects).

## `- { name: MEDALLION_BRONZE_NAMESPACE, value: {{ .Values.medallion.producer.bronzeNamespace | quote }} }`

The IIIF page lane's bronze dataset name (P7a, re-tiered by R23) — a values knob beside
bronzeDataset; /bronze-arrival fires the cascade for either name.

## `- { name: MEDALLION_CONTROL_PUBSUB, value: "{{ required "pubsub.controlName must be set when catalog.contro...`

§ D2 B8 — the publication head. Set ONLY when the catalog is actually emitting control
events, because an empty value disables the subscription: a deployment without the
emitter must register no route rather than subscribe to a topic nothing publishes on.

## `- { name: MEDALLION_CONTROL_ROOT, value: "s3://{{ .Values.minio.bucket }}" }`

#84 per-tenant routing (opt-in): the catalog's warehouse-registry root, through which a
project's ACTIVE warehouse is resolved; project-less work keeps the shared URIs below.

ALSO required whenever ANY stage runner declares a `lane`, which is why `$transformsDeclared` widens the
condition. The cascade HEAD consults declarations to decide whether an unrecognised table is
a lane's entry point (`ingest_trigger.py::_has_declared_lane` -> `list_specs`), and that read
goes through this root. Gated on `projectsEnabled` alone, the head could not see a single
declaration, so a declared lane's table arrived and published nothing — the whole record was
unreachable from the deployed chart.

## `- { name: MEDALLION_TRAIN_TOPIC, value: {{ .Values.medallion.train.topic | quote }} }`

Ray TRAIN head (#115): values-wired passthrough (docs/RAY-TRAIN.md; defaults mirror core/config.py).

## `- { name: MEDALLION_LINEAGE_URL, value: {{ printf "http://%s-lineage:%v" (include "lance.fullname" .) .Va...`

The release's own lineage Service, which a training run's job emits its RunEvents to.

## `- { name: MEDALLION_ENGINES, value: {{ include "lance.medallionEngines" (list . true) | quote }} }`

CP-056: the engines the producer invokes (`lance.medallionEngines`), with the tasks it registers into
`<control_root>/_tasks/` at boot, so the catalog's transform door refuses a declaration naming a task no engine runs.
The training head is enabled when an engine registers task `train` (ADR 0139). The producer names no engine: each is
an app it invokes by Dapr (`compute`, ADR 0140).

## `- { name: MEDALLION_OUTCOME_URL_BASE, value: "http://{{ include "lance.fullname" . }}-medallion-producer:{{...`

CP-029: a training run's job reports to this Service; its plan lives in MEDALLION_CONTROL_ROOT.

## `- { name: MEDALLION_INPROCESS_TASKS, value: {{ .Values.medallion.inprocessTasks | toJson | quote }} }`

OUTSIDE the `ray` guard on purpose: this is what the estate runs when it brings no Ray
cluster, so gating it on Ray would leave exactly that estate with an empty registry and a
catalog door refusing every declaration.

## `- { name: RASK_FGA_ENABLED, value: "true" }`

Rendered under fgaEnabled OR auth.enabled: the #64 human door below NEEDS the FGA client
(can_administer) — auth-on without these envs left the client on its unreachable default URL,
503-fail-closing every human trigger (found by the fresh-redeploy drive 2026-07-24; the
long-lived release had accumulated fgaEnabled=true via --reuse-values, masking it).
SECURITY (found live 2026-07-13): the FGA envs were rendered ONLY inside the stage runners range,
but this app also hosts the TRAINING consumer (#115 D5) — so `handle_train_trigger` got
`fga_client=None` and its can_read_data/can_create_table gate silently no-op'd: a revoked
trainer still trained (deny-test submitted a Ray job on a DENIED grant). The trainer has its
OWN identity + rung (reader on the feature namespaces, writer on models ONLY): the consumer
checks as `settings.trainer_identity` (MEDALLION_TRAINER_IDENTITY, set above), so the stage runners'
SERVICE_IDENTITY/REQUIRED_ACTION pair is deliberately NOT rendered here — it is theirs, and a
copy on this app would only mislead. These two envs are all the gate needs to come alive.

## `- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }`

Operator-pinned FGA store id — the same auth.fgaStoreId every consumer + the bootstrap-admin
Job use (see the catalog twin, services.yaml). Unset = provision-by-name at boot.

## `{{- include "lance.governedOidcEnv" . | nindent 12 }}`

#64 human trigger door: a signed-in project admin (OIDC bearer + can_administer) may fire
/train WITHOUT the service token. The web BFF forwards the user's bearer here; the
service-to-service app-token path (APP_API_TOKEN, above) is unchanged. Same OIDC construction
shape as catalog/lineage; the admin gate resolves against project:<produceAdminProject>.

## `- { name: MEDALLION_DLQ_TOPIC, value: "dlq.medallion-producer" }`

Dapr-native DLQ (ships with the Resiliency CRD): the producer's bronze-arrival + train
subscriptions park exhausted deliveries here, ERROR-logged by /dlq-event.

## `{{- $routes := dict }}`

SOURCE NAMESPACE -> the topic whose stage runner consumes it. DERIVED from the declared
DAG, so a stage runner cannot exist that the publication head is unable to route — which
is what it did before: it stamped the bronze namespace and topic on every trigger,
so a silver publication fired a bronze trigger no stage runner matched and was dropped.

Keyed on the namespace, not the tier: lanes are named (`bronze-media`), and
reducing one to its tier merges two cascades onto a topic neither owns.

## `{{- $moverUrls := dict }}`

Where each stage runner answers, for the operator proxy above. Built from the SAME list that
renders the stage runners, so a deployment that adds one gets its routes with no second edit.

## `{{- $moverGates := dict }}`

Each edge's AUTHORIZATION, keyed by its SOURCE namespace — what the re-run verb gates
on. Built from the SAME stage runner list, so an added stage runner gets its gate with no second
edit, and the rung is the stage runner's OWN `requiredAction` rather than /train's
`can_administer` (which is coarser AND different, and would lock out exactly the
non-admin validator `can_promote` exists for).

## `- { name: MEDALLION_CATALOG_URL, value: {{ printf "http://%s-catalog:%v" (include "lance.fullname" .) .Valu...`

The producer needs the catalog for the PROMOTION RESUME, not for a stage of its own:
an approved hold publishes the version it was taken on, which is a call to this door.

GATED ON `qualityReview`, which is what creates promotions — it used to be gated on
`cascadeViaPublish`, and that flag is DEAD: the app deleted it when the second
promotion door was removed, and `extra="ignore"` meant the env var it rendered was
silently dropped rather than refused. So on the DEFAULT chart the producer had no
catalog URL at all, and `publish_promotion` — which no longer skips terminal tiers —
would fail its resume with nothing having asked for it.

UNCONDITIONAL, and it must stay that way: `/bronze-arrival` asks this catalog where an
arrived bronze table lives (`ingest_trigger._vended_upstream`) and names that location on the
trigger, and the stage runners carry the same address unconditionally for the same reason.
Gating a service's governance calls on an unrelated feature flag is how a tier becomes
ungovernable without anything failing.

## `{{- $lanes := dict }}`

WHERE EACH LANE'S OUTPUT LANDS, derived from the stage runner declarations rather than kept
as a second list. `stageRunners[]` and `mediaStageRunners[]` already state fromNamespace ->
toNamespace for every lane, so a lane added there is measured with no second edit and
a renamed lane cannot half-move. Without this the lag edge is `<source>->?`, which
matches no lineage run: the detector runs cleanly and measures nothing.

## `- { name: MEDALLION_LANE_SOURCES, value: {{ toJson $laneSources | quote }} }`

THE TABLES, from the same declarations and in the same loop. The detector asks the
catalog for a TABLE and matches a lineage run on a full table id; a namespace answers
neither. `<project>-<namespace>` named an id no create door will accept, so every tick
404'd into "never published", and matching a bare namespace hit every tenant's output
as a substring. Derived here rather than listed so a lane added to `stageRunners[]` is
measured with no second edit and a renamed dataset cannot half-move.

## `- { name: MEDALLION_CASCADE_LAG_BINDING_NAME, value: {{ $.Values.medallion.cascadeLag.bindingName | quote }} }`

ONE STRING with the Component's `metadata.name` and the path the producer serves at its
pod root — Dapr delivers an input binding to `POST /<component name>`, so any two of the
three agreeing while the third does not is a cron firing into a 404 forever.

## `- { name: MEDALLION_FGA_SERVICE_IDENTITY, value: {{ .Values.medallion.producer.serviceIdentity | quote }} }`

WHO THIS APP IS WHEN IT AUTHORS A RUN, which lineage authorizes. Absent, the app claims a
code default (`service-stage-runner`) that holds no tuple, and lineage refuses the run while
the request answers 202: measured 2026-09-10, every run this app authored emitted `sub='ray'`,
every one was refused, and its provenance was discarded with nothing red.

## `- { name: MEDALLION_QUALITY_REVIEW_ENABLED, value: "true" }`

S3/S4 — this app HOSTS `promotion_review` and serves the door that answers it. The
stage runners hold promotions; only this app-id can raise the decision event into the instance,
and only this app has a gateway row. The flag is what starts the workflow runtime here.

## `- { name: MEDALLION_COMPUTE_ENABLED, value: "true" }`

Real compute for the producer's training door: `MEDALLION_BRONZE_URI` and the stage base below are where
`/train` resolves a feature dataset by convention (`services/train.py`). S3 secret follows the catalog's
two-tier rule (plaintext only when OpenBao is off — compute + OpenBao is not a shipped combo).

## `- { name: MEDALLION_STAGE_BASE_URI, value: "s3://{{ include "lance.stageBucket" (list . "silver") }}/medall...`

The medallion (project) bucket base — where bronze/silver + the model registry live. The
trainer resolves feature + model URIs from HERE (via silver's zone), so it stays in the project
bucket even when gold (sink) is zoned out. `silver` is unmapped by default →
minio.bucket, so this is s3://<project-bucket>/medallion, unchanged on the single-bucket path.

## `- { name: MEDALLION_S3_ENDPOINT, value: "{{ include "lance.s3Endpoint" . }}" }`

The one S3 endpoint the estate names (`lance.s3Endpoint`), so the producer opens the same store the
catalog governs when `/train` resolves a feature dataset and when the task registry is written at boot.

## `{{- $medScoped := .Values.minio.medallionAccessKey }}`

THE MEDALLION PLANE'S SCOPED IDENTITY (Q17-5). Empty values keep the tenant ROOT
credential, which is what every install ran until this landed. `rask-medallion` reaches
data in any warehouse, WRITES `_tasks/` because this plane registers them, and is
write-denied on every other control prefix — see `minio-scoped-users.yaml`.

The ACCESS KEY alone scopes the plane; the secret is derived in-cluster
(`lance.scopedSecretFn`), so declaring the identity needs no secret anywhere.

## `- { name: MEDALLION_SECRETS_FROM_DAPR, value: "true" }`

The S3 secret comes ONLY from the secret store at boot; never in pod env. Fails closed.

## `- { name: MEDALLION_DAPR_SECRET_S3_FIELD, value: "medallion-s3-secret-key" }`

THE OTHER HALF OF THE PAIR. `dapr_secret_s3_field` defaults to `minio-secret-key` — the
tenant ROOT's secret — so a scoped access key left on that default is signed with a
mismatched pair and every operation fails `SignatureDoesNotMatch`. The Ray and maintenance
pairs both paid for this; the field was already configurable, only the chart had to use it.

## `- { name: LANCE_AUDIT_ENABLED, value: {{ hasKey $root.Values.auth "audit" | ternary $root.Values.auth.audit...`

#41. The SAME `auth.audit` key the catalog reads, because this service declares the
same `LANCE_AUDIT_ENABLED` alias and the chart rendered it into the catalog alone —
measured live 2026-09-09, the lever reached ONE service of ten while every other pod
ran on its field default (§ Q17-27). hasKey+ternary for the reason recorded at the
catalog's copy: `| default` makes an explicit false un-settable, and a bare reference
renders "" on a `--reuse-values` upgrade, which is a bool_parsing crash at boot.

## `- { name: MEDALLION_DAPR_ENABLED, value: {{ $root.Values.dapr.enabled | quote }} }`

Fail-closed at boot if behind a sidecar but the app-token is unset (open forged-trigger path).

## `- { name: MEDALLION_PUBSUB, value: {{ include "lance.subPubsub" (list $root .daprAppId) | quote }} }`

This stage runner's OWN subscriber component (queueGroupName=<app-id>, deliverPolicy=new) — it
subscribes to its stage trigger AND publishes lineage + the next trigger through it.

## `{{- include "lance.appTokenEnv" (list $root .daprAppId) | nindent 12 }}`

verify the Dapr-stamped header on /medallion-event (forged-trigger guard)

## `- { name: MEDALLION_TRANSFORM, value: {{ .transform | quote }} }`

WHICH DECLARED LANE this stage runner runs. OPT-IN per row, and deliberately absent by default:
`core/config.py` documents that an estate which has declared nothing must behave
byte-for-byte as before, and that a named-but-UNDECLARED lane is a REFUSAL rather than a
fallback to the env below. Defaulting this to `.name` would therefore break every stage runner
the moment the chart shipped.

Set it, and `entrypoint`/`params`/`code_version` come from the catalog's admin-gated,
audited record instead of this Deployment — so WHAT a lane runs changes through the door
with no helm upgrade. The binding of a POD to an EDGE stays deploy-time, which is the same
place every orchestrator binds a code location; only the program is runtime-governed.

Until 2026-08-24 this variable was rendered by NO template, so `settings.transform` was always
"" in-cluster, every stage runner took the env branch, and `/compute/lanes` wrote records that
nothing read. The door was live and its property was not obtained.

## `{{- if or $root.Values.medallion.projectsEnabled .transform $engine }}`

Also for an out-of-process engine: its runs' plans live in this root (CP-029).

## `- { name: MEDALLION_CONTROL_ROOT, value: "s3://{{ $root.Values.minio.bucket }}" }`

#84 per-tenant routing (opt-in): a `project`-carrying stage trigger resolves this stage's
from/to URIs off the warehouse registry (<project-root>/medallion/<namespace>) instead of
the env URIs below; project-less triggers keep them byte-identically. Without this env a
project trigger is DROPPED fail-closed.

A lane-bound stage runner needs it for a second reason: `services/lane.py` reads the declaration
straight off this root (never through a catalog client), so without it resolution raises
UndeclaredLaneError naming an empty control root.

## `- { name: MEDALLION_GOLD_WAREHOUSE_ENABLED, value: "true" }`

Gold SERVING warehouse (docs/adr/0034-medallion-tiers-hybrid-physical-layout-2026-07-24.md "Medallion tiers"): ONLY the terminal silver→gold
stage runner retargets — its tenant target root becomes the project's serving=="gold" registry
record when one exists; absent → work-warehouse behavior, byte-identical.

## `- { name: MEDALLION_DLQ_TOPIC, value: "dlq.{{ .daprAppId }}" }`

Dapr-native DLQ (ships with the Resiliency CRD — see values.yaml dapr.resiliency): retry
exhaustion parks the trigger on this per-app topic, ERROR-logged by /dlq-event.
Keyed by the stage runner's APP-ID, not its subTopic: two stage runners can share one subTopic, and a
per-subTopic name had both declaring AND
subscribing the same DLQ — every dead letter delivered to both, parked and counted twice,
attributed to neither. The producer's dlq.medallion-producer is the precedent.

## `- { name: MEDALLION_REQUIRED_COLUMNS, value: {{ .requiredColumns | quote }} }`

Declared consumer dependencies (data-contract gap #1): the quality gate blocks a
promotion whose written schema dropped/renamed any of these (the breaking-change detector).

## `- { name: MEDALLION_COMPUTE_ENABLED, value: "true" }`

Real fake-Ray compute: read the upstream stage's Lance dataset, transform, write the downstream
one — so the emitted lineage carries the REAL version + rows/bytes and the cascade produces
actual data. FROM/TO URIs derive from the stage namespaces, so the chain lines up. S3 secret
follows the catalog's two-tier rule (plaintext only when OpenBao is off).

## `{{- $medScoped := $root.Values.minio.medallionAccessKey }}`

THE MEDALLION PLANE'S SCOPED IDENTITY (Q17-5). Empty values keep the tenant ROOT
credential, which is what every install ran until this landed. `rask-medallion` reaches
data in any warehouse, WRITES `_tasks/` because this plane registers them, and is
write-denied on every other control prefix — see `minio-scoped-users.yaml`.

The ACCESS KEY alone scopes the plane; the secret is derived in-cluster
(`lance.scopedSecretFn`), so declaring the identity needs no secret anywhere.

## `- { name: MEDALLION_SECRETS_FROM_DAPR, value: "true" }`

The store is the sole source at boot (see the producer block above). Fails closed.

## `- { name: MEDALLION_DAPR_SECRET_S3_FIELD, value: "medallion-s3-secret-key" }`

The pair's other half — see the producer block. A scoped key on the default field signs
with the root's secret and fails `SignatureDoesNotMatch` on every call.

## `- { name: MEDALLION_ENGINES, value: {{ include "lance.medallionEngines" (list $root false) | quote }} }`

CP-056: the engines this stage runner may plan a run on, and `MEDALLION_DEFAULT_ENGINE` the one a runner that declares
no transform runs on (`medallion.defaultEngine`, else the first enabled engine). `MEDALLION_STAGE_TASK` names the task
its order asks the engine for. A planned run is a work order sent to `compute` by Dapr invocation; its job reports
back through this runner's outcome door.

## `- { name: MEDALLION_OUTCOME_URL_BASE, value: "http://{{ include "lance.fullname" $root }}-{{ .name }}:{{ $p...`

CP-029: a planned run's job reports to this runner's Service; its plan is announced on the control
lane; the sweep's cron is one string with the Component's name and the route.

## `- { name: MEDALLION_CODE_VERSION, value: {{ include "lance.catalogImage" $root | quote }} }`

The catalog's address on EVERY stage runner: governance belongs to the cascade, and a runner without it writes bytes no
table record, grant or retention policy can name (measured 2026-08-17: `describe gold$catalog` answered 403). The
lakehouse image is the build an in-process run names in its key.

## `- { name: MEDALLION_QUALITY_ENABLED, value: "true" }`

Validator gate: after the compute write, assert the stage's data is good enough to promote
(row_count > 0, key column non-null); a failure emits the dataQualityAssertions facet but does
NOT trigger the next stage. Needs medallion.compute on (it opens the just-written dataset).

## `- { name: MEDALLION_QUALITY_REVIEW_ENABLED, value: "true" }`

A hold becomes a QUESTION rather than a verdict. This stage runner publishes what its gate saw
and stops; the producer's `promotion_review` splits corrupt (BLOCK) from unusual (ask).
The approver + deadline ride the published spec, so they are read HERE at dispatch — a
workflow body that read them would replay against whatever the value is now.

## `- { name: MEDALLION_PROMOTION_REVIEW_BAND, value: {{ $root.Values.medallion.promotionReviewBand | quote }} }`

The band is read HERE, by the stage runner, for the same reason the approver is: the stage runner
decides at DISPATCH whether this promotion is unusual. The producer never sees it.

## `- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }`

Operator-pinned FGA store id — one auth.fgaStoreId across every consumer (see services.yaml).

## `- { name: MEDALLION_FGA_SERVICE_IDENTITY, value: {{ .serviceIdentity | quote }} }`

THE IDENTITY IS WHO THIS RUNNER IS, so it renders whether or not FGA is on: it is the author
`sub` of every run this runner emits. Gated on `fgaEnabled` it was ABSENT from all three
running stage runners (measured 2026-09-08), so a run claimed a code default and lineage
refused it while the job exited SUCCEEDED. Only the required action above belongs to the
authorization toggle.

## `apiVersion: v1`

A stage runner is BUS-ONLY: no gateway row, no Ingress. This ClusterIP has two callers, each admitted by its
projected token on its own routes: the producer, proxying the operator routes onto the runs this runner planned,
and the Ray head's jobs, reporting their outcome (CP-029).
