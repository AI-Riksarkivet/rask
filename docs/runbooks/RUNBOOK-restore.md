# Backup & restore runbook

How the estate is backed up, and the **order-sensitive** procedure to restore it. rask runs no database: OpenFGA's
datastore is the platform's Postgres, which the platform backs up and restores (in development, the `postgres` fixture,
which has no backup). Lineage keeps no database: its commit feed and edge list are Lance tables in the catalog's
`lance_catalog$provenance` namespace, so they come back with the object store. The object-store `VolumeSnapshot` leg is
not drilled: validate it on your cluster's CSI driver before trusting it in an incident.

> The in-cluster backups are the SELF-CONTAINED path. With a managed S3 (object store), use ITS PITR + snapshots
> instead and set `backups.volumeSnapshot.enabled=false`. This runbook is for the in-cluster path.

## What is backed up (and what is NOT)

| Artifact | What | Where | Template |
|---|---|---|---|
| **VolumeSnapshot** | the RustFS fixture's data PVC = the **Lance lakehouse** (every tier + registry data, and lineage's provenance tables) | cluster CSI VolumeSnapshot | `backup-snapshot.yaml` (`backups.volumeSnapshot`) |
| **control-root records** | the governance objects: `_projects/`, `_warehouses/` (registry + bucket claims), `_declared_grants/` (the tuples the catalog wrote from git's declaration), `_trash/`, `_protection/`, `_policies/`, `_contracts/`, `_labels/`, `_label_definitions/`, `_transforms/`, `_tasks/` | a timestamped prefix, `_backups/control/<UTC>/` by default | `scripts/control_root_backup.py` |

> **The VolumeSnapshot row needs a cluster that can take one.** Measured 2026-09-14 on the k3s dev
> cluster: `kubectl get volumesnapshotclass` answers *"the server doesn't have a resource type"*, and
> `backups.volumeSnapshot` is off in the live release — so on this cluster the registry data in that row
> is covered by **nothing**. That is why the control-root row exists: it needs no CSI, and it restores
> RECORDS rather than a whole store, which is the right granularity for "somebody deleted the warehouse
> records".

**Not backed up** (accept the loss window or externalize — see [docs/adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md "P4/P7"](../adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md) and
[DURABILITY.md](../DURABILITY.md)): GreptimeDB local WAL/metadata (metrics — reconstructable; the secret store
is the platform's and backs itself up), and OpenFGA's database, which is the platform's to back up. The
development `postgres` fixture has no backup.

## Consistency: restore OpenFGA's database and the object store as a pair

OpenFGA's tuples name the tables and namespaces the object store holds. Restoring one far from the other leaves
grants on objects that do not exist. **Restore OpenFGA's database (through the platform's Postgres restore) and the
object-store snapshot from the SAME backup window.** A follower's cursor ahead of a restored feed does not stall: the
feed answers a cursor beyond its head with its newest seq, and the follower re-reads it from its oldest row
([COMMIT-EVENTS.md](../COMMIT-EVENTS.md) § The cursor rule).

## Restore — the object store (the Lance lakehouse)

1. Provision a new PVC from the VolumeSnapshot (`dataSource: {kind: VolumeSnapshot, name: <snap>}`).
2. Point the RustFS fixture's StatefulSet at it (or restore into the existing PVC per your CSI driver's clone flow).
3. Restart the store; then `catalog`/`lineage`/`maintenance` reconnect (their `/readyz` gates hold them out until the
   object store answers). Lineage's commit feed and edge list come back with the store: they are Lance tables in
   the catalog's `lance_catalog$provenance` namespace, restored like any other table.

## Restore — the control root (projects, warehouses, trash)

The control root holds the estate's governance as small JSON objects. The Lance data can survive intact
while every statement about **who may touch it** is gone, and that is a different failure from losing a
dataset — so it has its own backup and its own restore.

Every run signs with a session a ServiceAccount token is exchanged for (`RASK_S3_*`: `RASK_S3_ENDPOINT_URL` and `RASK_S3_STS_ENDPOINT` at the
store, `RASK_S3_ASSUME_ROLE_ARN` at the identity's role and `RASK_S3_WEB_IDENTITY_TOKEN_FILE` at a token from
`kubectl create token <ServiceAccount> --audience <objectStore.identity.audience>`):
`backup` and `verify` with the control-root backup's (`<release>-sa-backup-control-root`, whose policy reads the records
and writes only the backups), and `restore`, which writes records, with the catalog's (`<release>-sa-catalog`), the one
identity whose policy writes them.

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
- A forwarder cursor in `lance_catalog$provenance$cursors` restored behind the graph backend's state re-sends events the backend
  already took: the forwarder promises at least once, never exactly once.
- A forwarder's cursor is kept under the uuid of the feed table it was taken on (`rask.uuid` of
  `lance_catalog$provenance$events`). To replace the feed and keep the cursors, drop `events` at the catalog and restart
  lineage, whose start creates it again: the new table has another uuid, so the forwarder follows it from its first row.
  A feed that comes back under another uuid for any other reason (registered again at its restored location) is
  forwarded from its first row too, and the backend is sent again what it already took.
- Putting the bytes of `lance_catalog$provenance$events` back from an older copy keeps its uuid and rewinds its row
  ids, so restore `lance_catalog$provenance$cursors` from the same point, or delete the forwarder's row there before
  lineage ticks. Without that, rows committed after the restore reuse seqs the cursor has passed and are not forwarded;
  the forwarder notices only while the feed's newest row is still behind the cursor (WARN
  `lineage_forward_cursor_ahead_of_feed`, after which it follows the feed from its oldest row).
- See [RUNBOOK-oncall.md](RUNBOOK-oncall.md) for the failure modes you may hit during recovery.
