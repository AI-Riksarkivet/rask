# 0177. The commit is the outbox: every version names its author, and a lost announcement is made from the version log (2026-10-25)

**Status:** accepted. Source: LH-497 (#641), admitted by the owner on 2026-10-20 with LH-498 (#643) folded into it; the
design was settled with the owner on 2026-10-25 before implementation, and two rulings were made during it the same day
(the issue's comments record each).

## Context

The catalog announces a write after its commit: it stages the event in its object-store outbox, then publishes it, and
lineage's relay drains what a crash left staged. A crash between the commit and the stage loses the announcement while
the version stays committed, so the commit feed misses the write and a pipeline following `/events?since=` never starts
its next hop. The same holds for a stock client's commit through `version/create`, whose audit record and announcement
both follow the finalize. Lineage's drift sweep used to detect the loss and was deleted with LH-468; nothing detected it
since.

Lakekeeper does not solve this: it mints a fresh UUIDv7 per event, sends it into an in-memory channel with a 50 ms
timeout, and recovers nothing (`crates/lakekeeper/src/service/events/publisher.rs` at `7e5fd6a5`).

Measured on pylance 13.0.0:

- A commit's `transaction_properties` persist with it, and `read_transaction(v)` reads them back. Most operations take
  none: `update`, `delete`, `add_columns`, `alter_columns`, `drop_columns`, both metadata updates, `drop_index` and
  `MergeInsertBuilder.execute`.
- A schema operation's transaction cannot be built by hand: `LanceSchema.from_pyarrow` renumbers field ids, and a
  `Project` built from it read one column's data as another's.
- A handle opened through a namespace that serves managed versioning stages its commit and hands the staged path to
  `create_table_version`, for every operation, on main and on a branch.
- A stock compaction (`Compaction.commit`) commits `ReserveFragments` then `Rewrite` with no properties, and pylance
  models no Python class for `ReserveFragments`.
- Lineage binds a run id to the author of its first event for ever (`RUNS`, LIN-5), and any producer holding
  `can_write_data` on some table may post a RunEvent with any run id.

## Decision

- **Every version names its author.** Each catalog door stamps `rask.author` (the verified caller) and `rask.operation`
  (the lineage operation it announces) on its commit. For an operation that takes no properties, the door's plan is
  Lance's own call on a handle opened through an in-process namespace under managed versioning, which keeps the staged
  manifest instead of finalizing it; the transaction read off it is committed detached with the stamp and finalized as
  exactly the next version through the backend's put-if-not-exists (`dataplane.commit_stamped`, `dataplane.captured`),
  as keyed merges already were. A stock commit carrying no author has its verified caller recorded by `version/create`
  before the finalize (`_commit_authors/<table uuid>/<transaction uuid>.json`); one naming another author is refused.
- **A versioned announcement's run id is derived from its commit, keyed by a catalog secret.** A UUIDv8 of HMAC-SHA256
  under `commit-id-key`, read by the catalog alone, over the table's uuid, the ref, its incarnation and the version;
  `eventTime` is the commit's own time. The original and every re-announcement are then one run, which lineage's feed
  records once. A key derived from public facts would let one table's writer pre-bind another table's next commit ids.
  The key is never rotated; a stack with no secret store derives under the empty key, as it emits unsigned.
- **A catalog cron announces every committed version the feed lacks.** `catalog-commit-reconcile-cron` lists every held
  location claim whose name still describes to it, reads each ref's versions after its mark that are older than the
  grace, finds each on `provenance$events` by the table's uuid, ref and version, and announces each one missing from the
  version log alone, signed for its author, with an audit record. Each find counts on a paging counter, and so does a
  version of an announced operation that names no author.

## Consequences

- An announcement lost between a commit and its staging reaches the feed within one tick past the grace, exactly once.
- Every door's commit goes through one more detached write and finalize, and an operation captured through the
  namespace pays pylance's outcome check, measured at 0.1 to 0.2 s, before the capture surfaces.
- A racing column operation no longer answers 409: it plans again on the version that won, as a keyed merge does.
- A branch write may carry a run: the finalize names the branch it planned on.
- A version committed past every catalog door, by a credential that can write a final manifest, is found and paged on,
  and announced by nobody.
