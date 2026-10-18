# Backup & restore runbook

How the estate is backed up, and the **order-sensitive** procedure to restore it. The Postgres leg covers the
`rask-age` server's two databases, OpenFGA's datastore and the Dapr state store; lineage keeps no database (its
commit feed and edge list are Lance tables in the catalog's `provenance` namespace, so they come back with the
object store). Neither the Postgres leg nor the object-store `VolumeSnapshot` leg is drilled: rehearse the
pg_dump→drop→restore below on a throwaway database, and validate the snapshot leg on your cluster's CSI driver,
before trusting either in an incident.

> The in-cluster backups are the SELF-CONTAINED-prod path. If you externalize to CloudNativePG (Postgres)
> and a managed S3 (object store), use THEIR PITR + snapshots instead — set
> `backups.pgDump.enabled=false` + `backups.volumeSnapshot.enabled=false`. This runbook is for the
> in-cluster path.

## What is backed up (and what is NOT)

| Artifact | What | Where | Template |
|---|---|---|---|
| **pg_dump** (gzip) | the `openfga` DB (authz tuples) **and** the `daprstate` DB (Dapr state store: inbox read state + workflow/actor instances) | the object store, `_backups/pg/<UTC>/` | `backup-pg.yaml` (CronJob, `backups.pgDump`) |
| **VolumeSnapshot** | the RustFS fixture's data PVC = the **Lance lakehouse** (every tier + registry data, and lineage's provenance tables) | cluster CSI VolumeSnapshot | `backup-snapshot.yaml` (`backups.volumeSnapshot`) |
| **control-root records** | the governance objects: `_projects/`, `_warehouses/` (registry + namespace bindings), `_trash/`, `_protection/`, `_policies/`, `_gates/`, `_transforms/`, `_tasks/` | a timestamped prefix, `_backups/control/<UTC>/` by default | `scripts/control_root_backup.py` |

> **The VolumeSnapshot row needs a cluster that can take one.** Measured 2026-09-14 on the k3s dev
> cluster: `kubectl get volumesnapshotclass` answers *"the server doesn't have a resource type"*, and
> `backups.volumeSnapshot` is off in the live release — so on this cluster the registry data in that row
> is covered by **nothing**. That is why the control-root row exists: it needs no CSI, and it restores
> RECORDS rather than a whole store, which is the right granularity for "somebody deleted the warehouse
> bindings".

**Not backed up** (accept the loss window or externalize — see [docs/adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md "P4/P7"](../adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md) and
[DURABILITY.md](../DURABILITY.md)): GreptimeDB local WAL/metadata (metrics — reconstructable; the secret store
is the platform's and backs itself up), and note the pg_dump lands on the same object
store, so a **total store loss loses both the Lance data AND the DB dumps** unless you also ship the dumps
off-cluster (the "fate-sharing" gap).

## Consistency: restore the dumps + the object store as a pair

OpenFGA's tuples name the tables and namespaces the object store holds, and the notifications inbox (in
`daprstate`) points at rows of lineage's commit feed, which lives in the object store. Restoring one far from the
other leaves grants on objects that do not exist, or inboxes pointing past the feed. **Restore the pg_dump and the
object-store snapshot from the SAME backup window.** An inbox cursor ahead of a restored feed does not stall: the
notifications reconciler re-reads the feed from its oldest row when the feed answers a cursor beyond its head.

## Restore — Postgres (OpenFGA + Dapr state)

Every database on the `rask-age` server carries the `age` extension: the server preloads AGE into every backend,
and AGE's hook needs `ag_catalog` to exist, so a database without it fails every `DROP` (`age-postgres.yaml`). The
target must therefore have the extension available. Do NOT `psql < dump` into a database that already holds data —
drop/recreate it first.

1. **Fetch the dumps** from S3 with the backups' identity (`backup-s3` in the store), through the estate's S3 seam:
   `uv run python -c "from storage import s3_client; s3_client().download_file('<bucket>', '_backups/pg/<UTC>/openfga.sql.gz', 'openfga.sql.gz')"`
   with `RASK_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` set to that pair (and the same for
   `daprstate.sql.gz`).
2. **Recreate the target DB** clean (managed PG or the in-cluster `age-postgres`):
   `DROP DATABASE IF EXISTS openfga; CREATE DATABASE openfga;`
3. **Ensure the extension is installable** in the target (the AGE image ships it):
   `psql -d openfga -c "CREATE EXTENSION IF NOT EXISTS age;"`
4. **Restore**: `gunzip -c openfga.sql.gz | psql -d openfga`.
5. **`daprstate` — restore it LAST, and only after deciding you want it.** Same procedure as `openfga`, but it is
   the one dump whose restore is a judgement call rather than a reflex, because Dapr owns this schema and the rows
   describe *in-flight* work:

   - **What is lost without it:** every subject's inbox READ state. The inbox *contents* come back on
     their own — the notifications reconciler replays lineage's durable `GET /events` — but "which of
     these have I already seen" lives nowhere else, so skipping this dump marks the entire estate
     unread for everyone. Dapr workflow instances and actor state are lost with it too; in-flight
     workflows do not resume.
   - **Why LAST, and from the same window:** the rows point at feed rows in the object store and at objects
     OpenFGA's tuples govern. Restore both dumps from the SAME `_backups/pg/<UTC>/` directory — they are dumped in
     one CronJob run, so a single timestamp directory is internally consistent.
   - **When to skip it:** if you are restoring far enough back that in-flight work is stale anyway,
     dropping `daprstate` and letting Dapr recreate its schema is legitimate and safe. Everyone starts
     with a full unread inbox, which is noisy but not wrong. Say so in the incident notes — an inbox
     that silently re-alerts on months of history reads as an outage to the people receiving it.
   - **Check its size before assuming it is small.** Most of `daprstate` is Dapr workflow history, and
     that grows without bound unless `dapr.workflowStateRetention` is on — measured live 2026-08-10,
     before retention landed: 1367 instance rows for `dapr.internal.default.ingest.workflow` alone,
     `count(expiredate) = 0`, oldest 7 days back. Retention bounds it (7d completed / 30d failed),
     but a cluster restored from a dump taken with retention OFF carries the whole history.
6. **App recovery is automatic**: OpenFGA and the Dapr sidecars reconnect on their own.

## Restore — the object store (the Lance lakehouse)

1. Provision a new PVC from the VolumeSnapshot (`dataSource: {kind: VolumeSnapshot, name: <snap>}`).
2. Point the RustFS fixture's StatefulSet at it (or restore into the existing PVC per your CSI driver's clone flow).
3. Restart the store; then `catalog`/`lineage`/`maintenance` reconnect (their `/readyz` gates hold them out until the
   object store answers). Lineage's commit feed and edge list come back with the store: they are Lance tables in
   the catalog's `provenance` namespace, restored like any other table.

## Restore — the control root (projects, warehouses, bindings, trash)

The control root holds the estate's governance as small JSON objects. The Lance data can survive intact
while every statement about **who may touch it** is gone, and that is a different failure from losing a
dataset — so it has its own backup and its own restore.

```bash
# Back up. --dest ANOTHER BUCKET for real DR; the default lands under the control root itself.
uv run python scripts/control_root_backup.py backup --root s3://lance-catalog

# Is the backup intact? (ask this BEFORE an incident)
uv run python scripts/control_root_backup.py verify --from s3://lance-catalog/_backups/control/<UTC>

# Rehearse into a scratch prefix — the DEFAULT, because it risks nothing.
uv run python scripts/control_root_backup.py restore \
    --from s3://lance-catalog/_backups/control/<UTC> --into s3://lance-catalog/_restore_probe

# Replace the live records. Refused without --force, deliberately.
uv run python scripts/control_root_backup.py restore \
    --from s3://lance-catalog/_backups/control/<UTC> --into s3://lance-catalog --force
```

**EXERCISED 2026-09-14 against the live estate**, which is the only reason this section is here rather
than in a plan: 1,454 records backed up, `verify` intact with 0 missing and 0 changed, and a restore into
a scratch prefix returning `verified: true` with 0 mismatched.

Three things that exercise taught, each of which had already produced a wrong answer once:

- **The outboxes are NOT restored.** `_control_outbox/` and `_lineage_outbox/` are queues, not records:
  putting them back re-publishes events the estate already acted on — a replayed grant, a replayed drop
  announcement. A queue's correct recovery is to be empty.
- **A backup of a live root is not a point-in-time snapshot.** Three `_tasks/` records changed between
  the listing and the copy on a single run. The manifest records the ETag of what was COPIED, so `verify`
  answers "is this backup intact", and the records that moved are reported separately as `raced`.
- **Clear a failed restore's residue before retrying.** A store that keeps objects as files can be
  left by a partial run with a zero-byte object named `<prefix>` that blocks the correct run from creating
  `<prefix>/...`: `head_object` finds the child and `list_objects_v2` does not (measured on MinIO; unmeasured on
  RustFS, which also lays objects out on a filesystem). Delete every key under the target prefix first, and the
  `<prefix>` key itself (boto3 `list_objects_v2` then `delete_objects`), and confirm with the same API the tool uses.

## After restore

- Watch `/readyz` on catalog + lineage flip to 200 (lineage's gates on its feed table answering through the catalog).
- Drive one read (`GET /api/lineage/events`, and `GET /api/lineage/tables/<name>/derived` for a table you know has
  derived tables) to confirm provenance resolves against the restored data.
- A forwarder cursor in `provenance$cursors` restored behind the graph backend's state re-sends events the backend
  already took: the forwarder promises at least once, never exactly once.
- See [RUNBOOK-oncall.md](RUNBOOK-oncall.md) for the failure modes you may hit during recovery.

## Planned migration — bumping AGE across a Postgres major

**The AGE image tag is not a routine bump.** `age.image` (chart/values.yaml) pins
`apache/age:release_PG16_1.6.0` — the tag encodes **both** the Postgres major and the AGE build, because the
extension is compiled per major (the `release_PG16_*` line vs a `release_PG17_*` line). Moving to a
PG17-based tag is a data migration, not an upgrade. (On `age.externalHost` — a managed Postgres — use the
provider's major-upgrade path instead; this section is the in-cluster StatefulSet.)

**How the naive bump presents**: after `helm upgrade` with a PG17-based tag, the `<release>-age-0` pod goes
`CrashLoopBackOff` with `FATAL: database files are incompatible with server` — the retained PVC
(`data-<release>-age-0`, from the volumeClaimTemplate in `age-postgres.yaml`) still holds a PG16-format data
directory, and a Postgres major changes the on-disk format. Downstream: OpenFGA loses its datastore and the Dapr
state store goes with it, so every governed API fails closed with 503 — see the
[CrashLoop section of RUNBOOK-oncall.md](RUNBOOK-oncall.md#crashloop-on-boot) for the triage shape.

**Why there is no in-place path**: `pg_upgrade` needs the old *and* new majors' binaries side by side plus a
second data directory — the AGE image ships only one major. And a plain `postgres:17` image can't bridge it
either: the restore replays `CREATE EXTENSION age`, which needs an AGE build for that major. The only
supported route is logical: dump on the old major → fresh data directory on the new major → restore.

**Procedure** (rehearse it once on a throwaway install before touching a real estate):

1. **Quiesce writers**: `kubectl scale deploy <release>-openfga <release>-notifications --replicas=0`.
   Governed APIs 503 (authz fail-closed) for the window — expected and the point: the dump you take next is
   final. `notifications` is in that list because it is the writer of `daprstate` (its `InboxActor`s persist
   read state through the Dapr state store); leaving it up means dumping a database that is still being
   written, and the restored inboxes disagree with the OpenFGA dump taken beside them.
2. **Take a fresh dump of both DBs**: trigger the backup CronJob out of schedule —
   `kubectl create job --from=cronjob/<release>-pg-backup pg-migrate-dump` (`backup-pg.yaml`; it dumps
   `openfga` + `daprstate` gzipped to `_backups/pg/<UTC>/` on the object store) — or run the equivalent manual
   `pg_dump` per database. Manually, do NOT write it as `pg_dump | gzip`: the pipeline reports only gzip's
   status, so a dump that dies mid-stream leaves a valid gzip and a zero exit — see `backup-pg.yaml` for the
   form that catches it. Then copy the dumps somewhere off the estate too; don't let the only copy
   fate-share with the cluster you're about to operate on. (Postgres recommends dumping with the *new*
   major's `pg_dump`; if you want that, run a one-off pod on the new image pointed at `<release>-age:5432`
   before cutover.)
3. **Keep a rollback for the datadir**: take a manual CSI VolumeSnapshot of `data-<release>-age-0`
   (`backup-snapshot.yaml` only snapshots the object store's PVCs, so this one is by hand). No snapshotter? Then the
   step-2 dumps are your only rollback — verify they exist and gunzip cleanly before proceeding.
4. **Cut over**: `kubectl scale sts <release>-age --replicas=0`, delete the PVC `data-<release>-age-0`, then
   `helm upgrade` with the new `age.image`. The fresh PVC makes initdb run the `age-postgres.yaml` ConfigMap
   scripts on the new major: the extension and the `openfga` + `daprstate` databases.
5. **Restore both dumps** per [Restore — Postgres](#restore--postgres-openfga--dapr-state) above —
   including its "drop/recreate the DB first" rule, which applies unconditionally: initdb just
   pre-created both databases empty in step 4.
6. **Prove the restore on the new major before re-opening writes**: list the tuples of a store you know
   (`fga tuple read`, or OpenFGA's `/stores/<id>/read`) and compare the count with the old major's.
7. **Un-quiesce**: scale openfga + notifications back up, then run the [After restore](#after-restore) checks.

**Rollback**: until step 6 matches *and* the estate has been driven on the new major, keep the old image
tag, the step-3 snapshot, and the step-2 dumps. Reverting = `helm upgrade` back to the old tag + a PVC
restored from the snapshot (or delete the PVC and restore the dumps under the old image — they were made by
the old major's `pg_dump` and load clean there). Do not prune any of the rollback artifacts in the same
maintenance window.

**OpenFGA migrates in the same window** — it has no Postgres of its own: `age.openfgaDb` lives in this same
server and the openfga subchart's datastore DSN points at it. Skipping `openfga.sql.gz` in step 5 means every
governed call keeps failing closed after an otherwise-successful cutover.

## Resizing `age.storage` (volumeClaimTemplate immutability)

StatefulSet volumeClaimTemplates are immutable, so bumping `age.storage` in values does not resize anything —
the chart's upgrade-time guard (chart/templates/age-postgres.yaml) fails the upgrade up-front with the exact
commands instead of letting the apply die deep in the StatefulSet patch. The resize path (no data loss, no
restore needed, requires a storage class with `allowVolumeExpansion: true`):

1. Patch the PVC directly: `kubectl patch pvc data-<release>-age-0 -p '{"spec":{"resources":{"requests":{"storage":"<new-size>"}}}}'`.
2. Delete the StatefulSet WITHOUT touching the pod or PVC: `kubectl delete sts <release>-age --cascade=orphan`.
3. Re-run the `helm upgrade` with the new `age.storage` — the recreated StatefulSet's template now matches
   the resized PVC and the guard passes.

Write the size in the same unit the chart installed (`Gi`): the guard compares textually, so `1024Mi` vs a
deployed `1Gi` trips it even though the quantities are equal.
