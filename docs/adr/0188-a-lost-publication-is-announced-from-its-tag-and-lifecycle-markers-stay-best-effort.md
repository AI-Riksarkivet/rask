# 0188. A lost publication is announced from its tag; a table's lifecycle markers stay best-effort (2026-11-01)

**Reopened 2026-11-09** by [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md): best-effort lifecycle markers lose to cloud native; they are to ride the outbox, decided after the store requests per door are counted.

**Status:** accepted. Source: LH-552 (#743), admitted by the owner on 2026-11-01 from that day's design audit. The owner
chose its scope the same day through the multi-question tool ("Publication only"), after the research below.

## Context

ADR 0177 makes each table's version log the outbox for a commit's announcement. Two kinds of announcement name no
version: a move of a table's `published` tag (`publish_table`, ADR 0161), and a table coming into the catalog or leaving
it (`declare_table`, `register_table`, `drop_table`, `deregister_table`, and a rename's pair). Each is announced after
its act and staged in the lineage outbox, each under a fresh run id, so a crash between the act and the stage lost it,
and no log held it to be found again.

The two references recover neither kind. Lakekeeper `81d1b25` commits a drop's transaction
(`crates/lakekeeper/src/server/tables.rs:1002`) and then spawns its event (`tables.rs:1015`,
`crates/lakekeeper/src/service/events/types/table.rs:114`) through a channel with a 50 ms send timeout
(`crates/lakekeeper/src/service/events/publisher.rs:548`), and nothing finds a lost one. Polaris `3c0725e` buffers its
events in memory and flushes them to the metastore later
(`runtime/service/src/main/java/org/apache/polaris/service/events/listeners/inmemory/InMemoryBufferEventListener.java:81`,
`flush` at `:167`), so a crash loses the buffer.

The two losses cost different things. A lost `publish_table` stalls a pipeline that starts the next tier on it
(`docs/COMMIT-EVENTS.md` § A publication) until the table is published again, and nothing tells anyone. A lost
lifecycle marker heals the next time a follower asks the catalog about the table: DescribeTable answers 404 for a
dropped table and describes a declared one.

Lance keeps a tag as a file of its own, `_refs/tags/<name>.json`, naming the branch and the version, and the docs add
`createdAt` and `updatedAt` (`lance_docs/lance/format/table/branch_tag.md` § Tag File Format). Measured on pylance
13.0.0: both are written, `updatedAt` moves with every `tags.update`, and `tags.list()` answers them as `created_at` and
`updated_at`; `tags.create` and `tags.update` take no metadata. The file names no author, and a maintain-tier
credential can write it.

Considered and rejected: staging every versionless act's announcement on the control root before the act, and having
the tick send it once the store showed the act landed. It covered the class, but it touched every lifecycle door and the
three cascades, and it went well beyond both references for losses that heal on their own (owner, 2026-11-01).

## Decision

- **A lost publication's announcement is found again from the tag.** The commit reconcile tick reads the move each live
  table's `published` tag names now: the version and the time the tag file records. When the feed holds no run with
  that move's id, the tag moved before the grace, and the catalog recorded the caller it authorized the move for, the
  tick announces the publication for that caller (`catalog.services.commit_reconcile`, `catalog.api.commit_reconcile`).
- **The publish and approve doors record the mover right before the tag moves**, at
  `_commit_authors/<table uuid>/tags/published/<version>.json` (`service_kit.lakehouse.commit_records.record_tag_move`),
  and only for a move that advances the tag; a publication of the version already published leaves the tag file as it
  is, since setting it again restamps its move time. A store that refuses the record refuses the move, 503 code 17. The tick
  signs only for that record, as ADR 0187 asks, never for whoever can write the tag file: a `published` the catalog
  recorded no mover for is counted unauthored once and announced for nobody.
- **A publication's run id is derived from the move**: a UUIDv8 of HMAC-SHA256 under `commit-id-key` over the table's
  uuid, the operation, the version and the tag file's `updatedAt` (`catalog.core.commit_ids.tag_move_run_id`). The door
  reads that time right after the move and the tick reads it later, so the original, a late copy from the outbox and the
  re-announcement land one row. The version alone does not name a move: an erasure drops `published`, and a later
  publication may name a version it named before.
- **The tick keeps a publication mark** beside the refs' marks (`tag:published` in the table's `_commit_marks/` record):
  the move it settled, by its `updatedAt`. A move is looked at until the feed confirms it, it is past the horizon, or it
  is counted unauthored.
- **A table's lifecycle markers stay best-effort**, as every event is in Lakekeeper and Polaris. A follower that must
  not miss a table coming or going reads ListTables and DescribeTable as the truth, and the marker as its cue
  (`docs/COMMIT-EVENTS.md` § Delivery).

## Consequences

- A publication whose announcement a crash lost after the tag moved reaches the feed within one tick past the grace,
  exactly once.
- A publication the tag moved past before a tick read it is not announced: readers read the newer one, whose
  announcement the tick looks for.
- A `published` moved straight at the store pages once as unauthored; the next move through the door resumes the
  reconciliation.
- At the first tick after this change, a publication made before it, inside the horizon, has no mover record and its
  announcement carries a fresh run id: it pages once as unauthored.
- Two callers publishing one version at once each record themselves; if the later record's move fails after the
  earlier move landed and its announcement was lost, the tick signs that announcement for the later caller, who was
  authorized to publish and did not move the tag.
- A move whose time the door cannot read right after it is announced under a fresh run id; the tick, reading the time
  later, announces it again under the derived one, and the feed holds both rows.
- Each advancing publication costs one control-root write before the tag moves. Each tick reads one tag file and one
  record listing per live table, and deletes a record once its publication is confirmed or superseded.
- A drop, deregister, declare, register or rename whose announcement a crash lost stays missing from the feed.
