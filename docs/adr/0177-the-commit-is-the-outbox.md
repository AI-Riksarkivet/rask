# 0177. The commit is the outbox: every version names its author, and a lost announcement is made from the version log (2026-10-25)

**Amended 2026-11-24** (owner, XC-204 #802: "get the design correct and fix it in this row"): the reconciler no longer
lists every held claim each tick.

- **A door files an entry before a commit it announces can land** (`_commit_pending/<table uuid>/<entry>.json`,
  `lakehouse.records.commit_records`): each attempt of a commit the catalog finalizes files one once it is judged, a
  stock commit before its finalize, and a create or an unkeyed insert before the one call in which Lance writes and
  commits. A door that cannot file its entry answers 503 and commits nothing.
- **The `catalog-commit-reconcile` tick lists the entries and reads the tables they name**, so an idle table costs it
  nothing. Measured 2026-11-24, the real catalog over a counting S3 store at 205 tables: an idle tick is 5 store
  requests (it was 16 a table), a tick after one insert reads one table, and a tick right after every table was written
  is 21 a table.
- **An entry is removed once it is older than a door's lease (15 minutes) and the grace together**, by a tick that read
  every ref of its table, or once no claim records the table. A table a tick could not read (its store did not answer,
  its warehouse is deactivated, it is in the trash) keeps its entries. So a table is read on each tick of about
  twenty-five minutes after a write, two or three ticks, and the tick's cost follows the tables written in that time:
  at 0.2 s a table it reaches half its period near 1,500 tables written within twenty-five minutes, where it used to
  reach it near 1,500 tables held.
- **The walk of every claim is kept on a timer of its own**, `catalog-commit-walk`
  (`catalog.commitReconcile.walkPeriodSeconds`, an hour, which the render keeps inside the horizon). It files a table it
  could not read, so the tick retries that table every period.

What this costs, stated plainly:

- **A version committed past every door on a table no door wrote to lately is counted by the walk alone**, so
  `CatalogCommitsUnauthored` pages within the walk's period and the grace (about an hour and ten minutes) where it paged
  within one tick and the grace (about twenty minutes). No credential the catalog vends can put a final manifest (ADR
  0204), so the writer is the store's root credential or the catalog's own identity.
- **A commit whose single write call outlasts the lease** (a create or an unkeyed insert uploading for more than
  fifteen minutes) can land after its entry was removed; its lost announcement is then found by the walk.
- **While this change rolls out**, a replica of the previous revision commits without filing; an announcement it loses
  is found by the first walk.

Why: the tick cost about 16 store requests and 0.2 s a table whatever had changed, so it reached half its period near
1,500 tables (ADR 0185, measured 2026-11-23). The shape is the one ADR 0198 gave maintenance: work is found where the
catalog already sees it, and the sweep is the backstop. Lance's own commit through an external manifest store is only
an analogy: there too a record is written before the commit and the manifests in the object store stay authoritative
(`lance_docs/lance/format/table/transaction.md` § External Manifest Store), but that record reserves one version and
coordinates its writers, which an entry here does not. Lakekeeper (read at 4e05f79, 2026-11-23) writes a drop's cleanup
task in the drop's own Postgres transaction and picks tasks with `FOR UPDATE SKIP LOCKED`, so its tasks need no sweep;
its change events are sent after the commit through an in-process channel with no outbox, so it does not promise the
announcement this record exists to keep.

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
- A version committed past every catalog door, by a credential that can write a final manifest, is found and paged on
  within the walk's period (amendment above), and announced by nobody.
