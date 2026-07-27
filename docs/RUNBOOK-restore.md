# Backup & restore runbook

How the estate is backed up, and the **order-sensitive** procedure to restore it. Closes the "no restore
procedure anywhere" gap. The Postgres/AGE leg is authored from the backup artifacts + Apache AGE's
requirements **and proven by a drill**: `scripts/age_restore_drill.sh` (run inside the age-postgres pod
against a throwaway DB, wired into `e2e_stack.sh` behind `E2E_RESTORE_DRILL`) performs this runbook's exact
pg_dump→drop→restore and verifies the graph came back with its labels + a real vertex — green on the
pinned PG16 engine. The drill green does not transfer across a Postgres major (see the migration section
below). The RustFS `VolumeSnapshot` leg is **not** drilled — validate it on your cluster's CSI driver
before trusting it in an incident.

> The in-cluster backups are the SELF-CONTAINED-prod path. If you externalize to CloudNativePG (Postgres)
> and a managed S3 / rustfs-operator (object store), use THEIR PITR + snapshots instead — set
> `backups.pgDump.enabled=false` + `backups.volumeSnapshot.enabled=false`. This runbook is for the
> in-cluster path.

## What is backed up (and what is NOT)

| Artifact | What | Where | Template |
|---|---|---|---|
| **pg_dump** (gzip) | the `lineage` DB (AGE graph + events/reads tables) **and** the `openfga` DB (authz tuples) | RustFS S3 `_backups/pg/<UTC>/` | `backup-pg.yaml` (CronJob, `backups.pgDump`) |
| **VolumeSnapshot** | the RustFS data PVC = the **Lance lakehouse** (all medallion + registry data) | cluster CSI VolumeSnapshot | `backup-snapshot.yaml` (`backups.volumeSnapshot`) |

**Not backed up** (accept the loss window or externalize — see [DECISIONS.md "P4/P7"](DECISIONS.md) and
[DURABILITY.md](DURABILITY.md)): GreptimeDB local WAL/metadata (metrics — reconstructable), OpenBao's file
PVC (an open gap — back up the unseal material out-of-band), and note the pg_dump lands on RustFS, so a
**total RustFS loss loses both the Lance data AND the DB dumps** unless you also ship the dumps
off-cluster (the "fate-sharing" gap).

## Consistency: restore AGE + RustFS as a pair

The lineage graph (AGE) references Lance datasets that live in RustFS; the registry (RustFS) is described by
the catalog. Restoring one far from the other leaves dangling provenance. **Restore the pg_dump and the
RustFS snapshot from the SAME backup window**, and expect the reconcile sweep to converge minor drift
(storage↔graph) afterward.

## Restore — Postgres (AGE lineage graph + OpenFGA)

The critical AGE detail: a plain `pg_dump -d lineage` captures `ag_catalog`, the per-graph schema, and the
label tables, **but the target must have the `age` extension available and loaded**, and the graph catalog
must be consistent after load. Do NOT `psql < dump` into a database that already has a different `lineage`
graph — drop/recreate the DB first.

1. **Fetch the dump** from S3: `mc cp rfs/<bucket>/_backups/pg/<UTC>/lineage.sql.gz .` (and `openfga.sql.gz`).
2. **Recreate the target DB** clean (managed PG or the in-cluster `age-postgres`):
   `DROP DATABASE IF EXISTS lineage; CREATE DATABASE lineage;`
3. **Ensure the extension is installable** in the target (the AGE image ships it):
   `psql -d lineage -c "CREATE EXTENSION IF NOT EXISTS age;"`
4. **Restore**: `gunzip -c lineage.sql.gz | psql -d lineage`. (The dump recreates `ag_catalog`, the graph
   schema, and the label tables.)
5. **Verify the graph is real, not just present**:
   ```sql
   LOAD 'age'; SET search_path = ag_catalog, "$user", public;
   SELECT name FROM ag_graph;                    -- must list 'lineage'
   SELECT * FROM cypher('lineage', $$ MATCH (d:Dataset) RETURN count(d) $$) AS (n agtype);
   ```
   A non-zero Dataset count + the graph name confirms the labels restored, not just the metadata rows.
6. Repeat 1–4 for `openfga` (no AGE steps — it's plain relational).
7. **App recovery is automatic**: on next boot lineage runs `ensure_graph()` (a no-op if step 4 restored the
   graph) + `ensure_graph_constraints()`, and `/readyz` gates on the graph being queryable — so a
   half-restored graph fails readiness loudly rather than serving silently.

**If step 5 shows the graph metadata but zero labels** — the known plain-pg_dump-of-AGE hazard — restore is
NOT trusted: switch the backup to an AGE-aware dump (per-graph `SELECT * FROM ag_catalog.agtype ...` export
or CloudNativePG logical backup with the extension) and re-drill. **This is exactly why the drill is
mandatory before relying on this path.**

## Restore — RustFS (the Lance lakehouse)

1. Provision a new PVC from the VolumeSnapshot (`dataSource: {kind: VolumeSnapshot, name: <snap>}`).
2. Point the `rustfs` StatefulSet at it (or restore into the existing PVC per your CSI driver's clone flow).
3. Restart `rustfs`; then `catalog`/`lineage`/movers reconnect (their `/readyz` gates hold them out until the
   object store answers).

## After restore

- Watch `/readyz` on catalog + lineage flip to 200 (pool AND graph healthy).
- Drive one read (`/runs`, a dataset `/producers`) to confirm provenance resolves against the restored data.
- Let the reconcile sweep run once to converge any storage↔graph drift from a slightly-off backup pair.
- See [RUNBOOK-oncall.md](RUNBOOK-oncall.md) for the failure modes you may hit during recovery.

## Planned migration — bumping AGE across a Postgres major

**The AGE image tag is not a routine bump.** `age.image` (chart/values.yaml) pins
`apache/age:release_PG16_1.5.0` — the tag encodes **both** the Postgres major and the AGE build, because the
extension is compiled per major (the `release_PG16_*` line vs a `release_PG17_*` line). Moving to a
PG17-based tag is a data migration, not an upgrade. (On `age.externalHost` — a managed Postgres — use the
provider's major-upgrade path instead; this section is the in-cluster StatefulSet.)

**How the naive bump presents**: after `helm upgrade` with a PG17-based tag, the `<release>-age-0` pod goes
`CrashLoopBackOff` with `FATAL: database files are incompatible with server` — the retained PVC
(`data-<release>-age-0`, from the volumeClaimTemplate in `age-postgres.yaml`) still holds a PG16-format data
directory, and a Postgres major changes the on-disk format. Downstream: lineage `/readyz` goes red (graph
gate) and OpenFGA loses its datastore, so every governed API fails closed with 503 — see the
[CrashLoop section of RUNBOOK-oncall.md](RUNBOOK-oncall.md#crashloop-on-boot) for the triage shape.

**Why there is no in-place path**: `pg_upgrade` needs the old *and* new majors' binaries side by side plus a
second data directory — the AGE image ships only one major. And a plain `postgres:17` image can't bridge it
either: the restore replays `CREATE EXTENSION age`, which needs an AGE build for that major. The only
supported route is logical: dump on the old major → fresh data directory on the new major → restore.

**Procedure** (rehearse it once on a throwaway install before touching a real estate):

1. **Quiesce writers**: `kubectl scale deploy <release>-lineage <release>-openfga --replicas=0`. Governed
   APIs 503 (authz fail-closed) for the window — expected and the point: the dump you take next is final.
2. **Take a fresh dump of both DBs**: trigger the backup CronJob out of schedule —
   `kubectl create job --from=cronjob/<release>-pg-backup pg-migrate-dump` (`backup-pg.yaml`; it dumps
   `lineage` + `openfga` gzipped to `_backups/pg/<UTC>/` on RustFS) — or run the equivalent manual
   `pg_dump | gzip` per database. Then copy the dumps somewhere off the estate too; don't let the only copy
   fate-share with the cluster you're about to operate on. (Postgres recommends dumping with the *new*
   major's `pg_dump`; if you want that, run a one-off pod on the new image pointed at `<release>-age:5432`
   before cutover.)
3. **Keep a rollback for the datadir**: take a manual CSI VolumeSnapshot of `data-<release>-age-0`
   (`backup-snapshot.yaml` only snapshots the RustFS PVC, so this one is by hand). No snapshotter? Then the
   step-2 dumps are your only rollback — verify they exist and gunzip cleanly before proceeding.
4. **Cut over**: `kubectl scale sts <release>-age --replicas=0`, delete the PVC `data-<release>-age-0`, then
   `helm upgrade` with the new `age.image`. The fresh PVC makes initdb run the `age-postgres.yaml` ConfigMap
   scripts on the new major: extension, graph + label indexes, and the `openfga` database.
5. **Restore both dumps** per [Restore — Postgres](#restore--postgres-age-lineage-graph--openfga) above —
   including its "drop/recreate the DB first" rule, which now applies unconditionally: initdb just
   pre-created a fresh `lineage` graph in step 4.
6. **Prove the round-trip on the new major before re-opening writes**: copy `scripts/age_restore_drill.sh`
   into the new pod and run it (the same `kubectl cp` + `kubectl exec` shape as `e2e_stack.sh`'s
   `E2E_RESTORE_DRILL` step). The restore-drill green was proven on the pinned PG16 engine only — it does not
   transfer to a new major, and this is the moment the plain-pg_dump-of-AGE hazard would resurface.
7. **Un-quiesce**: scale lineage + openfga back up, then run the [After restore](#after-restore) checks.

**Rollback**: until step 6 is green *and* the estate has been driven on the new major, keep the old image
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
