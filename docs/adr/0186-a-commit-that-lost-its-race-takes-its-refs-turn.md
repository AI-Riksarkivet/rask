# 0186. A commit that lost its race takes its ref's turn (2026-10-31)

**Status:** accepted. Source: LH-259 (#186), the owner's decisions of 2026-10-31: every stamped door takes the turn after
its first lost race, a stock writer that lost its finalize is handed the next one, the 409 stays a plain code 14.

## Context

Under managed versioning the catalog finalizes every version of a table (`lance_docs/ns_catalog/spec.yaml`
CreateTableVersion: put-if-not-exists, 409 if the version exists). A catalog door's commit is planned on the ref's latest
version and finalized as exactly the next one (`dataplane.commit_stamped`, [[LH-497]]); a stock writer's is finalized as
exactly the version it staged (`version/create`, ADR 0157). Neither rebases: the loser plans or stages again. A writer that
commits more often than another plans and finalizes therefore wins every version.

Measured on pylance 13.0.0 against a real `dir` root, with the catalog's `/insert` appending in a tight loop:

- a `cast(NULL as string)` add, a `Merge` adding no data file, answered 409 after all ten plans in 6 of 6 rounds (110-450
  appends a second);
- a drop, an update and a delete held their request 214, 253 and 302 s before answering 409: the capturing namespace
  showed Lance the versions committed after the one planned on, so Lance spent its own retries on every plan;
- a stock writer committing through `version/create` landed 0 appends beside 639, and failed its commit once Lance's 30 s
  of retries ran out ("Too many concurrent writers"), on HEAD before this change as after.

Lance cannot rebase a `Merge` over an Append at all (`lance_docs/lance/format/table/transaction.md` § Merge
Compatibility; `check_merge_txn` in `rust/lance/src/io/commit/conflict_resolver.rs` at v13.0.0), and its answer is to make
schema changes while no other write happens (`lance_docs/lance/guide/data_evolution.md`) and to quiesce data-modifying
writers around a single atomic merge commit (`lance_docs/lance/format/table/row_id_lineage.md` § Enabling Stable Row IDs).
A refused writer is no answer: measured on pylance 13.0.0, a stock writer whose finalize answered 409 with no version at
its target failed its commit as of unknown outcome, after one call.

## Decision

- **A catalog door's commit that lost its race takes its ref's turn** for its remaining attempts, whether Lance refused the
  plan for a version committed since or the finalize lost (`commit_turns.TableTurns.held`, entered on the commit's own
  `ExitStack`). The turn is one record per table on the control root, `_commit_turns/`, keyed by the table's uuid (ADR
  0173 §8), listing at most one turn per ref, taken by a create-if-absent or a replace under the ETag it read.
- **Every other finalize of the ref waits while a turn is held**: a door's (`DoorTable.finalize_version`), a stock
  writer's (`DoorTable.finalize_staged_version`), the unkeyed `/insert` and the create door's `mode=Overwrite` of an
  existing table, both of which Lance commits itself and which wait right before that commit (`DoorTable.wait_for_turn`).
  Writers wait; none is refused. A waiter waits `HOLD` at most in all, however many commits take the turn one after
  another, then races as it would with no turn.
- **A stock writer that lost its race is handed the ref's next turn** (`DoorTable.hand_turn`, `TableTurns.hand`), whether
  `version/create` found the version it staged already taken (the spec's 409 for a version that exists, code 14, where a
  version beyond the next stays a 400) or its finalize lost. Measured with the catalog's `/insert` appending in a tight
  loop, 26 of a stock writer's 41 losses were found taken before the finalize. The turn is held under the writer's verified
  caller and its transaction's uuid (`commit_turns.Writer`), which pylance keeps when it stages the commit again on the
  version that won (measured on pylance 13.0.0: two calls, one uuid), so no other caller's commit can claim it. Its retry
  finalizes as the holder and releases the turn; a transaction naming no uuid is handed none.
- **A lost race is no licence to hold the ref.** A writer can lose on purpose, staging a version already taken, so a
  writer whose handed turn lapsed unused is handed no other on that ref for 30 s (`COOLDOWN`): one that keeps losing on
  purpose holds the ref's other writers for 5 s of every 35 s, not throughout.
- **A turn lapses at its hold**, timed on the store's clock: 30 s for a door's commit, which its own loop releases at the
  end (`HOLD`, Lance's own 30 s commit-retry budget), and 5 s for a turn handed to a writer (`HANDED_HOLD`), so a writer
  that never retries, after a conflict Lance cannot rebase or a crash, holds the ref's other writers no longer than that.
- **The capturing namespace shows no version after the one planned on**, so Lance stages the next version, and the finalize
  decides the race: answered as found, such a version cost a drop, an update and a delete Lance's own retries on every plan,
  214-302 s before the door answered 409. A commit whose take lost to the turn record's other writers plans again after a
  jittered wait that doubles from 50 ms to a 400 ms cap (`commit_turns.backoff`, which a waiter's polls share).
- **The 409 stays a plain code 14.** A commit that still loses (more writers already past their wait than it has attempts,
  or a turn that lapsed under it) answers `ConcurrentModificationError`, with no `Retry-After`.
- **The record is the table's.** It ends with the table's other uuid-keyed records (`object_records`), and its prefix is a
  control prefix (`control_prefixes`).

## Consequences

- Every finalize reads the turn record once: one more control-root read per commit, a missing key in the common case. The
  control root is already on every finalize's path (the location claim the door reads first), so a control root that
  cannot be read fails the commit as it did.
- While a contended commit holds the turn, the ref's other writers wait, including while its data is written, for at
  most its hold. A computed add over a large table holds them for as long as its plan runs, up to 30 s; a plan longer than
  that loses the turn's protection.
- A table's other refs are not held: a turn is per ref.
- A waiting writer holds a request worker while it waits (the catalog's doors run blocking IO through Starlette's
  threadpool, anyio's default of 40 workers), so many writers of one ref waiting out a long turn can take the pool from
  every other request for up to the hold.
- One commit knowingly does not wait: the config-only commit a door's own attempt makes to disarm Lance's commit-path
  cleanup on an armed table, before it plans. It lands at most once inside a holder's window, which the holder's next
  attempt absorbs.
- A writer of a table can still slow that table's other writers on purpose, by up to `HANDED_HOLD` in every
  `HANDED_HOLD` plus `COOLDOWN`; without the turn, a writer appending in a tight loop starved them outright.

## Alternatives

- **Bounded optimistic retry with backoff only**, what the Iceberg REST family does. The Iceberg reference server re-applies
  a lost commit on the server four times, 100 ms doubling to 60 s within 30 minutes (`CatalogHandlers.commit` in
  apache/iceberg), Polaris runs the same loop (`CatalogHandlerUtils`, `ICEBERG_COMMIT_MAX_RETRIES` 4), and Lakekeeper twice,
  50 ms doubling with jitter (`MAX_RETRIES_ON_CONCURRENT_UPDATE` in `crates/lakekeeper/src/server/tables.rs`), each on a
  compare-and-swap of the metadata pointer with no lock. That suffices there because an Iceberg schema change still holds
  after a concurrent append (`assert-current-schema-id` against `assert-ref-snapshot-id`). A Lance `Merge` never does, so
  retry alone never lands one beside a tight writer: rejected.
- **A turn only for commits that write no data**, as Lakekeeper locks only its own metadata writes (a deliberate
  `SELECT … FOR UPDATE` on a view commit, `crates/lakekeeper-storage-postgres/src/tabular/view/mod.rs`) and Hudi only its
  validate-and-commit step (`BaseHoodieWriteClient`). It left the update, the delete, the merge and the computed add losing
  to a tight writer, and the row's own fix names a lease for the computed add: the owner chose the turn for every door.
- **Refusing other writers while a turn is held** fails a stock writer's commit as of unknown outcome (above): rejected.
- **`Retry-After` on the 409.** Neither the Iceberg REST spec nor the lance-namespace spec names one on a 409, both put it
  on 503 only (`rest-catalog-open-api.yaml` ServiceUnavailableResponse; `spec.yaml` ServiceUnavailableErrorResponse),
  Lakekeeper and Polaris send it on 503s alone, and neither Iceberg's HTTP client nor LanceDB's reads it on a 409: the owner
  chose not to add it.
