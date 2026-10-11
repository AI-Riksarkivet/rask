# chart/templates/maintenance-worker.yaml

Rationale for `chart/templates/maintenance-worker.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.dedicatedWorkers.enabled .Values.maintenance.wor...`

DEDICATED MAINTENANCE WORKERS (docs/adr/0049-maintenance-leaves-the-planner-pod-2026-09-04.md "Maintenance leaves the planner pod").
The planner's own Deployment, with the cron binding names BLANKED and its own sizing. It
consumes `maintenance.work.v1` as a competing consumer (the Component carries
`queueGroupName: maintenance`), so replicas share the queue with no code change and the
broker single-flights each unit — no lock, which is why this half may scale while the
planner may not.

Rendered from the SAME block as the planner so the two cannot drift in their storage,
credential or lineage wiring; only the role switches and the resources differ.

## `matchLabels: { app.kubernetes.io/component: maintenance-worker, app.kubernetes.io/instance: {{ .Release.Nam...`

ITS OWN COMPONENT, not the planner's ([[LH-183]]). Sharing `component: maintenance` put these
pods behind the PLANNER'S SERVICE (maintenance.yaml `selector: { app.kubernetes.io/component:
maintenance }`), which would route health, metrics and invocation onto pods that do not serve
that role — and it made the two indistinguishable to every selector the chart has, including
the PodDisruptionBudget this deployment needs at `replicas: 2` and the planner must NOT have at
one (`tests/unit/test_prod_ha_posture.py` gates both directions).

## `- { name: MAINTENANCE_AUDIT_ENABLED, value: {{ hasKey $root.Values.auth "audit" | ternary $root.Values.auth...`

#41. The SAME `auth.audit` key every other lakehouse service reads, so the
operator's one lever reaches the component that REWRITES BYTES too — it had no
setting at all until 2026-09-09, so there was nothing here to render (§ Q17-27).
hasKey+ternary for the reason recorded at the catalog's copy: `| default` makes an
explicit false un-settable, and a bare reference renders "" on a `--reuse-values`
upgrade, which is a bool_parsing crash at boot.

## `- { name: MAINTENANCE_MAX_CONCURRENT_UNITS, value: {{ .Values.maintenance.dedicatedWorkers.maxConcurrentUni...`

The execution ceiling this pod actually applies to its threadpool, and the number the
work component's `maxAckPending` is derived from. Passed rather than assumed so the two
cannot drift: a delivery bound computed from a ceiling the process does not enforce is a
bound nobody applies.

## `- { name: MAINTENANCE_MAX_SOURCE_BYTES, value: {{ int64 .Values.maintenance.maxSourceBytes | quote }} }`

The per-unit BYTE bound the pod's memory is sized against, beside the concurrency it
multiplies with. A measured ~1.7x of this is resident per unit.

## `- { name: MAINTENANCE_MAX_CONCURRENT_COMPACTIONS, value: {{ .Values.maintenance.dedicatedWorkers.maxConcurr...`

The MEMORY bound, held around the rewrite. Separate from the units ceiling above,
which is throughput: one knob for both throttled the whole lane.

## `- { name: MAINTENANCE_RECYCLE_AFTER_PASSES, value: {{ required "maintenance.dedicatedWorkers.recycleAfterPa...`

REQUIRED, NOT QUOTED-THROUGH. `helm upgrade --reuse-values` keeps the previous
release's values and IGNORES this chart's defaults, so a key added after that release
arrives nil and `| quote` renders it as `""` — which pydantic refuses at boot. Measured
2026-09-25: the worker CrashLoopBackOff'd on exactly that for the fraction below. A
missing value must fail the RENDER, where the operator sees it, never a pod. The correct
upgrade flag is `--reset-then-reuse-values`, which applies chart defaults first.

## `- { name: MAINTENANCE_TRASH_PURGE_ENABLED, value: {{ hasKey .Values.maintenance "trashPurge" | ternary .Val...`

#79 reclamation. OFF unless explicitly asked for: with it on, a drop past
LANCE_TRASH_GRACE_DAYS becomes permanently unrecoverable, and the purge is gated on
the reconcile tick's drift report running clean — so an estate that never reports
clean never reclaims, which is the designed direction, not a bug to route around.

## `- { name: MAINTENANCE_TRASH_PURGE_DRY_RUN, value: {{ hasKey .Values.maintenance "trashPurgeDryRun" | ternar...`

THE PREVIEW, and it is the step BEFORE spending the delete permission rather than an
alternative to it. With reclamation off the pass returns before the trash prefix is even
read, so the only way to learn what a purge would do was to let it do it — the wrong
order for the one irreversible operation this service performs. It runs the REAL pass
(same due-record selection, same liveness and shallow-clone pre-passes, same refusal
check) and skips only the three mutations plus the refusal annotation. It can only
SUBTRACT: with both flags on the tick previews and reclaims nothing.

## `- { name: MAINTENANCE_FLOOR_RAISE_ENABLED, value: {{ hasKey .Values.maintenance "floorRaise" | ternary .Val...`

[[LH-094]]. The floor raise writes ONE metadata-only commit to a dataset whose orphans
sit above its Lance listing floor, so the next ordinary sweep can enumerate them. It
deletes nothing — the delete stays `cleanup_old_versions`'s, under Lance's own rule.
Rendered unconditionally for the same reason the orphan scan below is: a setting with
no path from values is one no deployed estate can turn on, which is how the orphan
scan sat permanently SKIPPED. TWO FLAGS, because "does this estate want the
behaviour" and "does this tick act" are different questions, and the dry run defaults
TRUE here where the purge's defaults false — the purge is gated behind a clean report
and this pass is what MAKES the report clean, so it has no such gate in front of it.

## `- { name: MAINTENANCE_TUPLE_REBUILD_ENABLED, value: {{ hasKey .Values.maintenance "tupleRebuild" | ternary ...`

[[LH-061]]. The ADDITIVE tuple rebuild: re-assert authorization tuples the
control-plane registries still justify, for objects the drift report found
ungoverned. It WRITES AND NEVER DELETES, so it cannot widen access beyond what the
control plane already recorded — which is what makes it safe against a live store.
Off by default because it writes to the AUTHORIZATION store, and its dry run
defaults TRUE so turning it on to SEE what it would grant does not grant it.
The bound is real: project records carry `created_by` and warehouse records do not,
so a tenant's admin seed and its warehouses' tenancy pointers come back, and every
grant made after creation does not.

## `- { name: MAINTENANCE_DRIFT_REPAIR_ENABLED, value: {{ hasKey .Values.maintenance "driftRepair" | ternary .V...`

[[LH-061]]'s two DESTRUCTIVE halves, rendered here for the reason stated at the top of
this file: the worker is generated from the same block as the planner so the two
cannot disagree about what the service is configured to do. The worker's cron
bindings are blanked, so it never fires the reconcile route that reads these — which
is exactly why they must still be present and identical. A pod whose environment
describes a different service from its sibling is one an operator cannot reason
about from `kubectl describe`, and the gap would only be discovered by whichever
change first gives the worker a reconcile path.

## `- { name: MAINTENANCE_ORPHAN_SCAN_ENABLED, value: {{ hasKey .Values.maintenance "orphanScan" | ternary .Val...`

#128a: the orphan scan's lever. Rendered unconditionally rather than behind an `if`,
because the whole defect was that the setting existed in config.py and had NO path
from values — an env var that appears only when enabled is one nobody can discover
from `kubectl describe`, and this one gates whether the purge can certify at all.

## `- { name: MAINTENANCE_CONTROL_EMIT_ENABLED, value: "true" }`

The purge announces each reclamation on the catalog's control topic, through maintenance's own
component (dapr-component.yaml): one the sidecar is not scoped to refuses the publish SILENTLY.

## `{{- $scoped := .Values.minio.maintenanceAccessKey }}`

THE SCOPED PAIR WINS, BOTH HALVES OR NEITHER. `minio.accessKey` is the TENANT ROOT, and
maintenance compacts with it — a write — so it also reaches `_projects/`, `_protection/`
and `_policies/`, the records that govern maintenance itself. A provisioned scoped user
(minio-scoped-users) replaces it; empty leaves today's behaviour exactly, because
repointing a live service at a credential nobody created would take the sweep down on
upgrade. Requiring BOTH halves is the Ray pair's measured lesson: setting one gives
`SignatureDoesNotMatch` on every operation.

The ACCESS KEY alone scopes the plane; the secret is derived in-cluster
(`lance.scopedSecretFn`), so declaring the identity needs no secret anywhere.

## `{{- $extra := list }}`

The #50 policy registry root defaults to this bucket, matching the catalog's
LANCE_REST_ROOT (also .Values.minio.bucket, services.yaml). If LANCE_CONTROL_ROOT ever
moves the catalog's registry, set MAINTENANCE_POLICY_ROOT to the same value — a wrong root
lists cleanly as "no policies" (the sweep logs compaction_policies_loaded per tick).

EVERY bucket that can hold Lance data must be swept, or its tables accumulate superseded
versions + small fragments FOREVER (audit 2026-07-14: the sweep saw exactly ONE bucket,
so the medallion zone buckets and every #3-B multi-base data bucket leaked storage
silently). Union of: the medallion storage zones (gold can live in its own bucket) and
the approved multi-base data bases. Per-warehouse (#3-A) buckets are provisioned at
RUNTIME, so they are appended by the operator as tenants are onboarded.

## `- { name: MAINTENANCE_S3_PLATFORM_BUCKETS, value: {{ include "lance.maintenancePlatformBuckets" . | quote }} }`

PLATFORM buckets — infrastructure the estate creates for ITSELF, which no warehouse
record will ever claim. Without them `orphan_buckets` reports each one as drift on
every tick forever, and a report whose contract is "clean certifies the estate" can
then never be clean. Measured 2026-08-16: `rask-observability`, created by this
chart's own minio-mkbucket Job for GreptimeDB's object store, sat in that list.
Sourced from `lance.maintenancePlatformBuckets`: the Job's platform set plus the
multibase bases.

## `- { name: MAINTENANCE_WAREHOUSES_ENABLED, value: {{ .Values.catalog.warehouses.enabled | default false | qu...`

The drift reconciler's inputs. It only ever READS: OpenFGA says who, the control-root
registries say what exists, object storage holds the bytes. Every one of these mirrors a
CATALOG setting and must match it — a divergent fga_root_object reports the estate root as
a ghost forever, and a divergent warehouses_enabled reports every namespace as unbound.

## `- { name: MAINTENANCE_POLICY_ROOT, value: {{ .Values.maintenance.policyRoot | default "" | quote }} }`

The #50 POLICY registry root. Rendered nowhere until 2026-08-16, so an operator who
followed values.yaml's "set both together" had no second key to set and moved only
half the reads. A policy registry at the wrong root lists cleanly as EMPTY, so every
retention override and legal-hold opt-out silently stops applying.

## `{{- with .Values.auth.fgaStoreId }}`

Store/model ids are PINNED, never provisioned here — the SAME auth.fgaStoreId the catalog
reads and the bootstrap-admin Job grants into, so the store this reports on can never
diverge from the store the catalog checks against. A maintenance job that provisioned its
own store would read an EMPTY one and report every real tenant as a ghost; unset, the four
authz categories report unavailable-with-a-reason, which is the honest answer.

## `resources: {{- toYaml $root.Values.maintenance.dedicatedWorkers.resources | nindent 12 }}`

SIZED FOR THE WORK, not for the planner ([[LH-183]]). `lance.resources` keyed on
"maintenance" hands back the PLANNER's tier — 512Mi — which is the exact sizing that
OOMKilled the pod this split exists to unload, so reading it here would move the OOM onto
a new pod name rather than remove it. `dedicatedWorkers.resources` is the tier the values
file already declares for this container ("Sized for the WORK, unlike the planner's
512Mi").
