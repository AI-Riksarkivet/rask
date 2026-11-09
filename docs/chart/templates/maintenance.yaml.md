# chart/templates/maintenance.yaml

Rationale for `chart/templates/maintenance.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

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

THE REWRITE IS SIGNED PER TABLE ([[LH-219]]). A version expiry deletes files, so each one is signed by a `maintain`-tier
credential the catalog vends for that one table prefix, expiring in 900 s; maintenance's own session never signs it
(`maintenance.services.credentials`). The catalog refuses that tier 409 code 19 for a table whose maintenance policy
keeps its whole history (`cleanup_enabled` false), and maintenance's probe asks no vend for such a table. The trash
is the catalog's to end ([[XC-084]]): the purge asks it to release a record past its deadline
(`POST /management/v1/trash/{kind}/{uuid}/release`), for a session that deletes that one table's bytes
(`POST .../credentials`), and to end the record once its place holds nothing (`DELETE /management/v1/trash/{kind}/{uuid}`),
which the tombstone sweep asks too.

GATED by `maintenance.vendWriteCredentials` (on by default). Off, the chart wires no door, the bootstrap-admin hook
takes maintenance's maintainer grant back, and every rewrite is refused (`VendingNotDirect`): reclamation pauses while
discovery, the due list and the drift report run, since maintenance's own session writes only its own records.

What its own session signs: discovery and the whole-estate protection pre-pass, which must open every manifest in every
bucket and cannot be expressed by any per-table credential. Those are reads.

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

[[LH-061]]. The ADDITIVE tuple rebuild: for each warehouse the drift report found ungoverned, maintenance asks the
catalog to write the tenancy pointer the warehouse's registry record names (`POST /management/v1/grants/tenancy/rebuild`),
and the catalog derives the edge from its own read of the record ([[XC-084]]: maintenance holds no OpenFGA credential).
It writes and never deletes, and a structural edge grants nothing on its own, so it cannot widen access. A project
holding no tuples is reported, not re-granted: its record's `created_by` names who created the tenant, not who may
administer it now. Off by default because it writes to the AUTHORIZATION store, and its dry run defaults TRUE so turning
it on to SEE what it would write does not write it.

## `- { name: MAINTENANCE_DRIFT_REPAIR_ENABLED, value: {{ hasKey .Values.maintenance "driftRepair" | ternary .V...`

[[LH-061]]'s REPAIR half: maintenance asks the catalog to revoke the grants of each object the drift report found no
registry naming (`POST /management/v1/grants/ghosts/revoke`), and the catalog judges each one again on its own reads,
refusing one a project or warehouse record, a held location claim or a trash record names. Rendered unconditionally
beside the rebuild, and DRY-RUN defaults TRUE here while the trash purge's defaults false: a wrong revoke is felt by a
person holding a grant rather than by a reclaimer, so seeing what it would do must never be the same act as doing it.

## `- { name: MAINTENANCE_TOMBSTONE_SWEEP_ENABLED, value: {{ hasKey .Values.maintenance "tombstoneSweep" | tern...`

[[LH-061]]'s STORAGE tier: ending a trash record once the bytes it names are gone. It deletes no bytes and writes no
record: it asks the catalog to end each one (`DELETE /management/v1/trash/{kind}/{uuid}`), which refuses a record whose
place or data directories still hold objects, so it satisfies `repair.py`'s refusal of `orphaned_trash` rather than
overturning it. Capped because each end is one listing PER RECORD.

## `- { name: MAINTENANCE_ORPHAN_SCAN_ENABLED, value: {{ hasKey .Values.maintenance "orphanScan" | ternary .Val...`

#128a: the orphan scan's lever. Rendered unconditionally rather than behind an `if`,
because the whole defect was that the setting existed in config.py and had NO path
from values — an env var that appears only when enabled is one nobody can discover
from `kubectl describe`, and this one gates whether the purge can certify at all.

## `- { name: MAINTENANCE_CONTROL_EMIT_ENABLED, value: "true" }`

The purge announces each reclamation on the catalog's control topic, through maintenance's own
component (dapr-component.yaml): one the sidecar is not scoped to refuses the publish SILENTLY.

## `{{- include "lance.objectStoreSessionEnv" (list $root "MAINTENANCE" "maintenance") | nindent 12 }}`

MAINTENANCE'S STORE IDENTITY IS ITS SERVICEACCOUNT ([[XC-084]], ADR 0170). The pod projects a token for
`objectStore.identity.audience` beside its `rask-catalog` one (the `lance.identityTokens` mount below), and maintenance
exchanges it through STS `AssumeRoleWithWebIdentity` for the sessions its own reads and records sign with
(`lakehouse.store_session`). It holds no key pair: with `MAINTENANCE_SECRETS_FROM_DAPR` on it refuses to boot without
the token, and refuses a pair beside it (`lakehouse.store_session.require_one_store_identity`).

THE SESSION READS THE ESTATE AND WRITES ONLY ITS OWN RECORDS. Its canned policy (`lance.objectStorePolicy`, component
`maintenance`, rendered into `<release>-object-store-identities`) allows `ListAllMyBuckets`; list, `GetBucketLocation`
and get on every bucket and key; and put and delete only on `<control bucket>/_maintenance_due/*`,
`<control bucket>/_maintenance_files/*` (its copy of the commit records it has read, which no erasure or hold reads),
`<control bucket>/_policies/state/*` and `<control bucket>/_lineage_outbox/*`. Owner, 2026-11-03: a compromised
maintenance pod must not be able to defeat an erasure or a hold. Every other write is signed by a catalog vend:

- a rewrite (compaction, cleanup, index work) by the table's `maintain`-tier credential
  (`POST /management/v1/table/{id}/credentials?tier=maintain`, [[LH-219]]), which reaches the table's prefix except its
  `_refs/`, its history fence and its committed-file records, and which the catalog refuses for a table whose policy
  keeps its whole history;
- the trash purge's deletes by a reclaim session the catalog vends for one due table record
  (`POST /management/v1/trash/{kind}/{uuid}/credentials`): list and delete over that record's place and its data
  directories, and nothing on the control root. The record's release stamp and its end are the catalog's own writes
  (`POST .../release`, `DELETE ...`). Every trash door asks `can_reclaim_trash`, which only the `reclaimer` estate role
  `lance.fgaEstateRoles` declares for maintenance's ServiceAccount opens.

## `- { name: MAINTENANCE_SECRETS_FROM_DAPR, value: "true" }`

Maintenance's signing key comes ONLY from the secret store, read at boot and again while the service runs
([[XC-082]], ADR 0166); never in pod env. With it on, its store identity must be the ServiceAccount token above, so no
key pair can stand in for it. Fails closed.

## `{{- $extra := list }}`

Maintenance reads the catalog's records, its policy records among them, at the catalog's own root: this bucket, as the
catalog's `LANCE_REST_ROOT` names it (services.yaml), where maintenance's store policy grants its own writes
(`lance.objectStorePolicy`). No value moves either, so the reads and the grant cannot point at different roots.

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
provisioning hook, `object-store-provisioning.yaml`, creates on the dev store) plus the multibase bases.

## `- { name: MAINTENANCE_RECONCILE_BINDING_NAME, value: {{ .Values.maintenance.reconcileBindingName | quote }} }`

The drift reconciler's inputs. It only READS: the catalog's grant census says who
(`GET /management/v1/grants/census`, which leaves out the objects the deployment keeps for itself), the control-root
registries say what exists, object storage holds the bytes. `MAINTENANCE_WAREHOUSES_ENABLED` mirrors the catalog's
setting and must match it: a divergent one reports every namespace as unbound.

## `args:`

No `command:`, so the image's tini, its ENTRYPOINT, stays the container's PID 1 and runs uvicorn's command line from
`args` ([[LH-313]]; `services.yaml.md` § `args:`).
