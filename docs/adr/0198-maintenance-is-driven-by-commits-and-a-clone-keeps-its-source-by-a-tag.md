# 0198. Maintenance is driven by commits, and a clone keeps its source by a tag (2026-11-08)

**Status:** accepted. Owner, 2026-11-08, through the multi-question tool: "Registered, by tag" for how a clone keeps its
source, and "Ship it, then redesign" for the order (XC-084 shipped first, ADR 0197). Supersedes the discovery decision
of [ADR 0045](0045-the-lakehouse-cloud-native-cutover-2026-09-03-04.md) ("maintenance discovers work by listing
buckets, and must"), the whole-estate protection pre-pass of
[ADR 0049](0049-maintenance-leaves-the-planner-pod-2026-09-04.md) and the clone list of
[ADR 0162](0162-the-lakehouse-builds-nothing-maintenance-decides.md) § 3. Each row that builds a part below removes what
it supersedes; until then the superseded design runs.

## Context

ADR 0045 rejected a catalog-directed work queue because writers then committed without the catalog. Since
[ADR 0157](0157-stock-clients-commit-through-managed-versioning.md) every stock client commits through `version/create`,
and a vend cannot write a final manifest (`NeverAFinalManifest`), so the catalog sees every commit and the premise is gone.

What the sweep costs, measured 2026-11-08 (the catalog on a moto server, every store request counted on the wire, an
idle estate): one tick is about 30 requests per table, 24 of them listings, plus 6 per retained commit (a HEAD per
recorded file), every 30 minutes whatever changed: 247 requests at 5 tables of 3 commits, 482 at 10, 337 at 5 tables of
6 commits. The tick runs in the one maintenance replica the chart pins, and the catalog refuses every stock compaction
503 once the clone list the tick writes is an hour old (LH-562), so a slow tick or a stopped pod blocks compaction. The
list is also written under maintenance's own identity and trusted by the catalog's commit judge (ADR 0197, residuals).

## Decision

1. **A registered clone keeps its source by a Lance tag.** A shallow clone is a dataset of its own whose manifest names
   the source's files; the source records nothing of it (`lance_docs/lance/format/table/transaction.md` § Clone), and it
   relies on "continued source retention, such as a source tag" (`lance_docs/lance/format/table/blob.md:309-313`), which
   cleanup honours (`lance_docs/lance/guide/tags_and_branches.md:61-67`). pylance 13.0.0's `shallow_clone` writes its
   target directly, with no namespace client, and the write vend denies that final manifest, so a clone enters the
   estate only through `RegisterTable`, whose door reads its `base_paths`: there the catalog takes a tag on each source
   version a clone names, and ends it when the clone ends. A clone never registered keeps its own tag through the tag
   door, as Lance's contract asks. The whole-estate manifest walk, the clone list and its 503 ceiling go.
2. **Work is enqueued where the catalog already sees it.** At `version/create` the catalog knows the table's uuid,
   resolves its policy by key and records when the table is next due; at a drop it records the deadline. The work is a
   JetStream work queue that any number of maintenance replicas consume as competing consumers, each table's task
   idempotent; the backstop is a slow pass over the due records, one listing per thousand tables, not over buckets.
3. **An overwrite in place is caught from the store's change notifications.** A session policy cannot require a
   conditional write on RustFS 1.0.3 (it refuses `s3:if-none-match` as an unknown condition key, measured), so the
   store announces each write and only the keys a commit recorded are checked, instead of a HEAD per recorded file
   every tick.

## Evidence, measured 2026-11-08

- A shallow clone of an untagged version is broken by the source's `cleanup_old_versions` (`Not found`); a clone of a
  tagged version reads all its rows after it (pylance 13.0.0). A second cleanup removes nothing: it is idempotent.
- One commit through maintenance's existing commit lane (`arrival.act_on_write`) costs 11 store requests at 5 tables and
  at 20.
- On NATS 2.14.2 JetStream, a work-queue task held by a worker that died unacknowledged is redelivered to another after
  the consumer's ack wait (2 s), no task is in flight twice, and the queue is empty after every ack.
- RustFS 1.0.3 publishes `s3:ObjectCreated:Put` on NATS for a write and for an overwrite in place, each naming the
  bucket, the key and the writer's principal (`RUSTFS_NOTIFY_NATS_*_PRIMARY`, ARN `arn:rustfs:sqs::primary:nats`).

## Consequences

- Maintenance's cost follows the estate's change, and it scales out; a stopped replica blocks no compaction.
- Not yet measured: whether RustFS keeps its events through a NATS outage (`RUSTFS_NOTIFY_NATS_QUEUE_DIR`), and the
  event and task throughput at estate scale. Each row that builds a part measures it with the same harness.
- The rows, in order: clone tags at register and the clone list's removal; commit-time due records and the due-record
  backstop; the overwrite check from store notifications; maintenance's work queue and scale-out.
