# chart/templates/maintenance.yaml

Rationale for `chart/templates/maintenance.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- define "lance.maintenanceEnv" -}}`

The worker's environment. Maintenance is one process under one identity (its ServiceAccount, its store role and its bus
users), and the worker's drain (`MAINTENANCE_DRAIN_SECONDS`) is passed in from the Deployment, where the render checks
it against the pod's grace.

## `name: {{ include "lance.fullname" . }}-maintenance-worker`

The worker pods: `python -m maintenance.worker`, maintenance's only process, one worker per replica on the shared work
queue (ADR 0198). It takes its tasks, its fired schedules, the store's change events and, under
`maintenance.arrivalEnabled`, the commit announcements off the bus on its queue connection (bus user
`maintenance-worker`), and serves nothing but its probes. Rendered with `maintenance.enabled`: the bus is named to the
pod for its work queue and for the connection it publishes what a pass emits on, with the pod's projected bus token and
the session length (`lance.busEnv`, `lance.busIdentity`). It holds no bus credential: each connection presents the
token, and its signing key it reads from the secret store under its own login. Its schedules are NATS message schedules on its own
work queue. RollingUpdate with no pod
unavailable: the queue holds one task per table and hands it to one worker at a time, so a surge pod shares the work and
repeats none.

## `terminationGracePeriodSeconds: {{ $grace }}`

`lifecycle.maintenanceTerminationGracePeriodSeconds`, longer than the fleet's: a stopping worker lets the pass it is
running finish for `maintenance.queue.drainSeconds` rather than leave it to be delivered again. The render refuses a
drain the grace cannot hold after preStop and the teardown, since a replica killed mid-pass is what the drain exists to
avoid. Held by `tests/chart/test_invariants.py::test_a_queue_value_the_worker_cannot_run_with_fails_the_render`.

## `- { name: MAINTENANCE_CATALOG_URL, value: {{ printf "http://%s-catalog:%v" (include "lance.fullname" $root)...`

THE REWRITE IS SIGNED PER TABLE ([[LH-219]]). A version expiry deletes files, so each one is signed by a `maintain`-tier
credential the catalog vends for that one table prefix, expiring in 900 s; maintenance's own session never signs it
(`maintenance.services.credentials`). The catalog refuses that tier 409 code 19 for a table whose maintenance policy
keeps its whole history (`cleanup_enabled` false), and maintenance's probe asks no vend for such a table. The trash
is the catalog's to end ([[XC-084]]): maintenance asks it to release a record past its deadline
(`POST /management/v1/trash/{kind}/{uuid}/release`; the record's own trash task, and every audit and scan), the daily
scan's purge asks for a session that deletes that one table's bytes (`POST .../credentials`), and the purge after its
deletes, like the audit's and the scan's tombstone sweep, asks it to end the record once its place holds nothing
(`DELETE /management/v1/trash/{kind}/{uuid}`).

GATED by `maintenance.vendWriteCredentials` (on by default). Off, the chart wires no door, the catalog takes
maintenance's maintainer grant back, and every rewrite is refused (`VendingNotDirect`): reclamation pauses while
each table's pass, the due list and the drift report run, since maintenance's own session writes only its own records.

What its own session signs are reads no per-table credential could express: each table's pass reading its datasets and
the catalog's records, the daily scan's storage walk, each table's orphan check, and the purge's liveness read of every maintained
root's `__manifest`.

## `- { name: MAINTENANCE_AUDIT_ENABLED, value: {{ hasKey $root.Values.auth "audit" | ternary $root.Values.auth...`

#41. The SAME `auth.audit` key every other lakehouse service reads, so the
operator's one lever reaches the component that REWRITES BYTES too — it had no
setting at all until 2026-09-09, so there was nothing here to render (§ Q17-27).
hasKey+ternary for the reason recorded at the catalog's copy: `| default` makes an
explicit false un-settable, and a bare reference renders "" on a `--reuse-values`
upgrade, which is a bool_parsing crash at boot.

## `- { name: MAINTENANCE_ARRIVAL_ENABLED, value: {{ $arrival | quote }} }`

The write-event lane: a commit's announcement holds its table's pass a delay out (ADR 0198). `$arrival` is
`maintenance.arrivalEnabled`, rendered on and off, so the worker reads the chart's decision rather than its own default.
On, the worker pulls the durable consumer `maintenance-arrival` on LINEAGE, which the stream Job declares under the
same value (`lance.busConsumers`), so its replicas compete for each announcement and one of them holds the task it asks
for. The signature door settings (`lance.doorEnv`) render only with the lane on, because only then does the worker
check an announcement's signature.

## `- { name: MAINTENANCE_LINEAGE_OUTBOX_URI, value: "s3://{{ .Values.objectStore.bucket }}/_lineage_outbox" }`

Stage the emit so a bus blip does not lose it. A maintenance run is emitted ONLY when a
table's pass does MATERIAL work (`table_pass._did_material_work` gates on versions or
data-directory files removed), so a dropped publish loses the record of the one pass that
actually deleted bytes — the graph then shows a dataset whose files changed with nothing
saying what changed them.

The SAME prefix the catalog names, because `lineage`'s relay tick (its own fixed timer,
`lineage.api.relay`) is the only thing that reads it. A private prefix here
would stage objects nothing ever collects, which reads as durability and is not.

## `- { name: MAINTENANCE_TRASH_PURGE_ENABLED, value: {{ hasKey .Values.maintenance "trashPurge" | ternary .Val...`

#79 reclamation. OFF unless explicitly asked for: with it on, a drop past
LANCE_TRASH_GRACE_DAYS becomes permanently unrecoverable, and the purge is gated on
the reconcile tick's drift report running clean — so an estate that never reports
clean never reclaims, which is the designed direction, not a bug to route around.

## `- { name: MAINTENANCE_TRASH_PURGE_DRY_RUN, value: {{ hasKey .Values.maintenance "trashPurgeDryRun" | ternar...`

THE PREVIEW, and it is the step BEFORE spending the delete permission rather than an
alternative to it. With reclamation off the purge returns before the trash prefix is even
read, so without it the only way to learn what a purge would do is to let it do it — the
wrong order for the one irreversible operation this service performs. It runs the REAL
purge of the daily scan (same due-record selection, same liveness read, same refusal
check) and skips only the mutations plus the refusal annotation. A registered clone's
source needs no pass of its own: the catalog vends no session to delete its bytes while
the clone's tag stands. It can only SUBTRACT: with both flags on the scan previews and
reclaims nothing.

## `- { name: MAINTENANCE_FLOOR_RAISE_ENABLED, value: {{ hasKey .Values.maintenance "floorRaise" | ternary .Val...`

[[LH-094]]. The floor raise writes ONE metadata-only commit to a dataset whose orphans
sit above its Lance listing floor, so the cleanup of the table's next pass can enumerate
them. It runs in each table's orphan check, when the check found stranded files, and the
worker then holds a pass of the table, so its cleanup runs on the raised floor
(`maintenance.services.orphan_check`). It deletes nothing —
the delete stays `cleanup_old_versions`'s, under Lance's own rule.
Rendered unconditionally: a setting with no path from values is one no deployed estate
can turn on. TWO FLAGS, because "does this estate want the behaviour" and "does this
check act" are different questions, and the dry run defaults TRUE here where the purge's
defaults false: nothing gates this writer but these two flags, and the safe default for
an ungated writer is to plan only.

## `{{- $check := required "maintenance.orphanCheck is unset — upgrade with --reset-then-reuse-values, not --reuse-values" $root.Values.maintenance.orphanCheck }}`

Each table's orphan check (owner, 2026-11-09/10) is a task the worker runs. Each of its three values is `required`, so a release upgraded with `--reuse-values` from one
that predates them fails the render rather than starting a worker on a setting pydantic would refuse; `enabled: false`
is a value, not an absent one, and renders. The schema holds `ceilingDays` to at least the one-day floor and
`maxSeconds` to the 60 s the worker's settings accept.

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

## `- { name: MAINTENANCE_CONTROL_EMIT_ENABLED, value: "true" }`

The purge announces each reclamation on the catalog's control topic, on the worker's own connection as the bus user
`maintenance`, whose row is granted that topic only while this flag is on (`nats.auth.flagged`).

## `{{- include "lance.objectStoreSessionEnv" (list $root "MAINTENANCE" "maintenance") | nindent 0 }}`

MAINTENANCE'S STORE IDENTITY IS ITS SERVICEACCOUNT ([[XC-084]], ADR 0170). The pod projects a token for
`objectStore.identity.audience` beside its `rask-catalog` one (the `lance.identityTokens` mount below), and maintenance
exchanges it through STS `AssumeRoleWithWebIdentity` for the sessions its own reads and records sign with
(`lakehouse.store_session`). It holds no key pair: the chart renders none and no acknowledgement of one, without which a
service with no token refuses to boot (`lakehouse.store_session.require_env_pair`), and a pair beside the token is
refused (`require_one_store_identity`).

THE SESSION READS HISTORY, NEVER DATA, AND WRITES ONLY ITS OWN RECORDS ([[LH-575]]). Its canned policy
(`lance.objectStorePolicy`, component `maintenance`, rendered into `<release>-object-store-identities`; each grant in
`_helpers.tpl.md`) allows `ListAllMyBuckets`; list, `GetBucketLocation` and `GetBucketNotification` on every bucket;
get on each table's manifests, refs and transactions, each root's `__manifest/` index and the control records it judges
by, and on no data, deletion, index or backup file; and put and delete only on `<control bucket>/_maintenance_due/*`,
`<control bucket>/_maintenance_files/*` (its copy of the commit records it has read, which no erasure or hold reads),
`<control bucket>/_policies/state/*` and `<control bucket>/_lineage_outbox/*`. Owner, 2026-11-03: a compromised
maintenance pod must not be able to defeat an erasure or a hold. Every other write is the catalog's, or signed by a
catalog vend:

- a cleanup's deletes by the table's `maintain`-tier credential
  (`POST /management/v1/table/{id}/credentials?tier=maintain`, [[LH-219]]): get and delete over the table's prefix
  except its `_refs/`, the put of its history fence and the delete of its committed-file records, and no put of the
  table's (ADR 0204); the catalog refuses it for a table whose policy keeps its whole history. A pass's two commits, the
  disarm of Lance's commit-path cleanup and the listing-floor raise, are the catalog's, at
  `POST /management/v1/table/{id}/maintenance/disarm` and `.../maintenance/floor`;
- the trash purge's deletes by a reclaim session the catalog vends for one due table record
  (`POST /management/v1/trash/{kind}/{uuid}/credentials`): list and delete over that record's place and its data
  directories, and nothing on the control root. The record's release stamp and its end are the catalog's own writes
  (`POST .../release`, `DELETE ...`). Every trash door asks `can_reclaim_trash`, which only the `reclaimer` estate role
  `lance.fgaEstateRoles` declares for maintenance's ServiceAccount opens.

## `{{- include "lance.secretStoreEnv" (list $root .Values.maintenance.appId) | nindent 0 }}`

MAINTENANCE LOGS IN TO THE SECRET STORE AS ITSELF ([[XC-199]]). The store's address, its role `<release>-maintenance`, the
mounts and the projected token's path (`lance.secretStoreEnv`, `_helpers.tpl.md`): its signing key comes ONLY
from the store, read under the process's own login at boot and again while it runs ([[XC-082]], ADR 0166), never from
pod env. Its work queue's connection holds no stored credential: it presents the pod's bus token as the bus user
`maintenance-worker`. A store that does not serve them fails the start closed.

## `{{- $extra := list }}`

Maintenance reads the catalog's records, its policy records among them, at the catalog's own root: this bucket, as the
catalog's `LANCE_REST_ROOT` names it (services.yaml), where maintenance's store policy grants its own writes
(`lance.objectStorePolicy`). No value moves either, so the reads and the grant cannot point at different roots.

A table's pass runs on a table in a bucket maintenance governs (`table_pass.governs`), or
its tables would accumulate superseded versions FOREVER. This list names the buckets the
chart itself knows of: the approved multi-base data bases. A warehouse's bucket is created by an API call at
runtime, so it is governed from the warehouse registry instead, read per task through the
warehouse the first segment of the table's name names: no value names it, and a deactivated warehouse's tables are
not passed.

## `- { name: MAINTENANCE_S3_PLATFORM_BUCKETS, value: {{ include "lance.maintenancePlatformBuckets" . | quote }} }`

PLATFORM buckets — infrastructure the estate creates for ITSELF, which no warehouse
record will ever claim. Without them `orphan_buckets` reports each one as drift on
every tick forever, and a report whose contract is "clean certifies the estate" can
then never be clean. Measured 2026-08-16: `rask-observability`, a platform bucket the
estate made for GreptimeDB's object store, sat in that list.
Sourced from `lance.maintenancePlatformBuckets`: `objectStore.buckets` (the set the
provisioning hook, `object-store-provisioning.yaml`, creates on the dev store) plus the multibase bases.

## `args:`

No `command:`, so the image's tini, its ENTRYPOINT, stays the container's PID 1 and runs the worker's command line from
`args` ([[LH-313]]; `services.yaml.md` § `args:`).

## `lance.spreadConstraints`

The worker spreads its replicas over the nodes by its own component label, as the catalog and lineage do
(`services.yaml.md`): two worker replicas on one node are one node's loss away from nobody pulling a task, which pages
only once `MaintenanceQueueStalled` or `MaintenanceTaskMetricsMissing` has waited out its window. `ScheduleAnyway`, so a
one-node estate still schedules both.

## `resources:` on the worker

The worker names the `maintenanceWorker` tier (`values.yaml` `resources.maintenanceWorker`), sized from its measured
working set (ADR 0198, 2026-11-13).
