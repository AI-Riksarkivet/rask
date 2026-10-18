# chart/templates/maintenance.yaml

Rationale for `chart/templates/maintenance.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: dapr.io/v1alpha1`

Dapr cron INPUT binding — the sidecar POSTs to /<bindingName> on `schedule`. The schedule lives here
as component config, not in app code (no scheduler thread in the service). Scoped to the maintenance app.
Gated on dapr.enabled: without the Dapr control plane the Component CRD doesn't exist and the release
would fail to apply (the service still renders below; its cron route just never fires).

The RECONCILE cron — its own binding on its own schedule. Separate from the sweep above because the
two differ by an order of magnitude in cost: the sweep rewrites data files, this only reads three
stores and reports where they disagree. Sharing one binding would force the cheap drift report onto
the expensive rewrite's cadence, which is how a drift report becomes too stale to act on.

## `apiVersion: apps/v1`

Table maintenance service — runs uvicorn maintenance.service:app (shares the catalog image).

## `strategy:`

RECREATE, NOT ROLLINGUPDATE, and the one replica above is why. `bindings.cron` is stateless and
uncoordinated — each replica runs the schedule independently — and this service holds no lease, so
the pin IS the duplicate-tick guarantee. A rolling update spends it: Kubernetes rounds `maxSurge`
UP, so the default 25% of one replica is one surge pod, and for the length of every rollout there
are TWO maintenance pods each able to fire the sweep. Steady state was correct and the transition
was not, which is why `replicas: 1` alone never proved the invariant.

The cost is deliberate and small: a gap between the old pod stopping and the new one becoming
ready, on a service whose work is an hourly cron with no request path to interrupt.

## `terminationGracePeriodSeconds: {{ .Values.lifecycle.maintenanceTerminationGracePeriodSeconds }}`

Longer grace than the other apps: an in-flight sweep/Lance write must finish, not be SIGKILLed.

## `- { name: MAINTENANCE_DAPR_ENABLED, value: {{ .Values.dapr.enabled | quote }} }`

Fail-closed at boot if behind a Dapr sidecar but the app-token is unset (open forgery path) —
symmetric with the lineage service (the audit's consistency fix).

## `{{- include "lance.appTokenEnv" (list $root $root.Values.maintenance.daprAppId) | nindent 12 }}`

Gated on the sidecar because that is what it VERIFIES — the token exists to check the
`dapr-api-token` header Dapr stamps on delivery, so without a sidecar there is no header
to check and the reference dangles: the Secret is not rendered and the pod never starts.
verify the Dapr-stamped header on the cron route (forged-sweep guard)

## `- { name: MAINTENANCE_CATALOG_URL, value: {{ printf "http://%s-catalog:%v" (include "lance.fullname" $root)...`

THE REWRITE IS SIGNED PER TABLE, not by the root key. A compaction is a WRITE, so this
service was performing writes across every bucket in the estate with one long-lived
credential — the posture the vending door exists to end. With this set, each rewrite is
signed by a credential scoped to that one table prefix and expiring in 900s.

GATED, and off by default, because it needs BOTH halves to be true: the catalog must be
reachable AND the maintenance subject must hold `can_write_data` on the tables it
maintains (seeded by the bootstrap-admin hook — grep `maintenanceSubject`). Where either
is missing the service degrades to the ambient credential and says so per dataset, which
is correct but silent about WHY, so the flag makes the intent explicit rather than
leaving a permanently-degraded deployment looking configured.

What deliberately STAYS on the root key: discovery and the whole-estate protection
pre-pass, which must open every manifest in every bucket and cannot be expressed by any
per-table credential. Those are reads.

## `- { name: MAINTENANCE_AUDIT_ENABLED, value: {{ hasKey $root.Values.auth "audit" | ternary $root.Values.auth...`

#41. The SAME `auth.audit` key every other lakehouse service reads, so the
operator's one lever reaches the component that REWRITES BYTES too — it had no
setting at all until 2026-09-09, so there was nothing here to render (§ Q17-27).
hasKey+ternary for the reason recorded at the catalog's copy: `| default` makes an
explicit false un-settable, and a bare reference renders "" on a `--reuse-values`
upgrade, which is a bool_parsing crash at boot.

## `- { name: MAINTENANCE_ARRIVAL_ENABLED, value: {{ .Values.maintenance.arrivalEnabled | quote }} }`

The write-event lane: a write refreshes that table's due record between sweep ticks (ADR 0162).
Rendered on and off, so the service reads the chart's decision rather than its own default. Its
dead-letter topic and the signature door settings (`lance.doorEnv`, the `/maintenance-arrival`
door) render only with the lane on, because only then is the route registered.

## `- { name: MAINTENANCE_LINEAGE_PUBSUB, value: {{ include "lance.subPubsub" (list $ .Values.maintenance.daprA...`

The PER-SUBSCRIBER component, not the shared broadcast one. `queueGroupName` lives on the
COMPONENT, so subscribing through `pubsub.name` gives every replica every event instead of
a competing-consumer group — measured 2026-09-03, the arrival route bound to
`pubsub=lineage-pubsub` and would have planned the same dataset once per replica. Used for
publishing too (`lance.subPubsub`): the queue group affects
subscription only. With the write-event lane off it is a publish-only component, maintenance's own
connection either way (values.yaml `nats.auth`).

## `- { name: MAINTENANCE_LINEAGE_OUTBOX_URI, value: "s3://{{ .Values.objectStore.bucket }}/_lineage_outbox" }`

Stage the emit so a bus blip does not lose it. A maintenance run is emitted ONLY when the
sweep does MATERIAL work (`sweep.py::_did_material_work` gates on fragments or versions
removed), so a dropped publish loses the record of the one tick that actually rewrote
bytes — the graph then shows a dataset whose files changed with nothing saying what
changed them.

The SAME prefix the catalog and the drain name, because `lineage`'s reconcile
cron is the only thing that reads it. A private prefix here would stage objects nothing
ever collects, which reads as durability and is not.

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

[[LH-061]]'s REPAIR half — revoking tuples whose OBJECT no longer exists (the ghost
categories). Rendered unconditionally beside the rebuild, and DRY-RUN defaults TRUE
here while the trash purge's defaults false: this one writes to the authorization
store, where a wrong revoke is felt by a person holding a grant rather than by a
reclaimer, so seeing what it would do must never be the same act as doing it.

## `- { name: MAINTENANCE_TOMBSTONE_SWEEP_ENABLED, value: {{ hasKey .Values.maintenance "tombstoneSweep" | tern...`

[[LH-061]]'s STORAGE tier — removing a trash record once the bytes it names are
PROVEN gone. It deletes no bytes: the pass probes the location and refuses anything
that still holds objects, which is how it satisfies `repair.py`'s refusal of
`orphaned_trash` rather than overturning it. Capped because the probe is one
listing PER RECORD.

## `- { name: MAINTENANCE_ORPHAN_SCAN_ENABLED, value: {{ hasKey .Values.maintenance "orphanScan" | ternary .Val...`

#128a: the orphan scan's lever. Rendered unconditionally rather than behind an `if`,
because the whole defect was that the setting existed in config.py and had NO path
from values — an env var that appears only when enabled is one nobody can discover
from `kubectl describe`, and this one gates whether the purge can certify at all.

## `- { name: MAINTENANCE_CONTROL_EMIT_ENABLED, value: "true" }`

The purge announces each reclamation on the catalog's control topic, through maintenance's own
component (dapr-component.yaml): one the sidecar is not scoped to refuses the publish SILENTLY.

## `{{- $_ := required "objectStore.maintenanceAccessKey names maintenance's scoped store identity; …" .Values.objectStore.maintenanceAccessKey }}`

THE SCOPED PAIR, BOTH HALVES, and `required`. Maintenance compacts — a write — so the store's root would
also reach `_projects/`, `_protection/` and `_policies/`, the records that govern maintenance itself;
it presents the scoped user `object-store-scoped-users` provisions instead, and a blank access key fails the
render: no service presents the store's root, whose secret signs every STS session token on RustFS (ADR 0168), so
there is no pair to fall back to. Naming BOTH halves is the Ray pair's measured lesson: setting one gives
`SignatureDoesNotMatch` on every operation.

The ACCESS KEY alone scopes the plane; the secret is derived in-cluster
(`lance.scopedSecretFn`), so declaring the identity needs no secret anywhere.

## `- { name: MAINTENANCE_SECRETS_FROM_DAPR, value: "true" }`

The S3 key pair comes ONLY from the secret store, read at boot and again while the service runs
([[XC-082]], ADR 0166); never in pod env (the audit's fix — maintenance is a real S3 consumer that
GCs the lakehouse). Fails closed.

## `- { name: MAINTENANCE_DAPR_SECRET_KEY, value: "maintenance-s3" }`

Maintenance's own secret in the store, which only its Configuration allows and only its store login reads ([[XC-165]]).

## `- { name: MAINTENANCE_DAPR_SECRET_S3_ID_FIELD, value: "access_key" }`

The scoped identity's key id, read from the store with its secret: a rotation issues a new id, so the
id cannot be configuration.

## `- { name: MAINTENANCE_DAPR_SECRET_S3_FIELD, value: "secret_key" }`

THE OTHER HALF OF THE PAIR, named beside the key id because a scoped key signed with any other
secret fails `SignatureDoesNotMatch` on every operation (the Ray pair's measured lesson).
`dapr_secret_s3_field` defaults to the same field, so a service run without the chart reads its own pair too.

## `{{- $extra := list }}`

The #50 policy registry root defaults to this bucket, matching the catalog's
LANCE_REST_ROOT (also .Values.objectStore.bucket, services.yaml). If LANCE_CONTROL_ROOT ever
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
then never be clean. Measured 2026-08-16: `rask-observability`, a platform bucket the
estate made for GreptimeDB's object store, sat in that list.
Sourced from `lance.maintenancePlatformBuckets`: `objectStore.buckets` (the set the
scoped-users hook creates on the dev store) plus the multibase bases.

## `- { name: MAINTENANCE_RECONCILE_BINDING_NAME, value: {{ .Values.maintenance.reconcileBindingName | quote }} }`

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

## `args:`

No `command:`, so the image's tini, its ENTRYPOINT, stays the container's PID 1 and runs uvicorn's command line from
`args` ([[LH-313]]; `services.yaml.md` § `args:`).
