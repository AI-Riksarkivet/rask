# 0186. A commit that lost its race takes its ref's turn (2026-10-31)

**Status:** accepted. Source: LH-259 (#186), the owner's decisions of 2026-10-31: every stamped door takes the turn after
its first lost race, a stock writer that lost its finalize is handed the next one, the 409 stays a plain code 14, and a
handed turn that lapses unused simply lapses, with no cooldown held against its writer. The research behind the waiting
decisions is `docs/architecture/commit-turn-waiting-findings.md`.

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
- **Every other writer of the ref waits while a catalog door's commit holds the turn**: a door's finalize
  (`DoorTable.finalize_version`), a stock writer's `version/create`, the `/insert` and the create door's `mode=Overwrite` of
  an existing table, the last two right before Lance commits them (`DoorTable.wait_for_turn`). A waiter waits `HOLD` at
  most in all, however many commits take the turn one after another, then races as it would with no turn.
- **A turn handed to a stock writer holds the catalog's own writers, not the writer's peers.** The doors and the `/insert`
  commit in-process, far faster than a stock writer's round trip, so they wait it out; other stock writers race it as
  Lance's own backoff spreads them (`TableTurns.wait`'s `handed`). Measured with 16 stock writers on one table at the
  estate's 2.5 s round trip: waiting on each other's handed turns, they landed 4 of 32 appends, and racing them 10 of 32,
  as many as with no turn.
- **A waiter holds no request thread where it can avoid one.** The catalog serves every sync door from anyio's default
  limiter, 40 threads, and a token is held for the whole time a function runs, sleeping included
  (`anyio/_backends/_asyncio.py:2603, 3093-3099` at 4.14.2): measured, 50 writers waiting on a held turn in threads made a
  describe of another table wait 26.8 s, and 20 left it answering in 0.02 s. So the `/insert` and `version/create`, both
  `async` doors, wait in the event loop before they enter the pool (`DoorTable.wait_for_turn_in_loop`), as Lakekeeper's
  commit back-off waits on a tokio timer (`crates/lakekeeper/src/server/tables.rs:1438`); and a wait that only a thread can
  make, a door's finalize inside its commit loop or the Overwrite create, takes one of `THREAD_WAITERS` (20) seats per
  process, past which its commit is refused at once with the retryable code 14 (`TurnWaitRefusedError`), as a bulkhead
  rejects when full (resilience4j's `SemaphoreBulkhead`, Polly's `ConcurrencyLimiter`) and as Lakekeeper answers a lock it
  will not wait for with a retriable conflict rather than a 503 (`crates/lakekeeper-storage-postgres/src/grant.rs:126-136`).
- **A stock writer that lost its race is handed the ref's next turn** (`DoorTable.hand_turn`, `TableTurns.hand`), whether
  `version/create` found the version it staged already taken (the spec's 409 for a version that exists, code 14, where a
  version beyond the next stays a 400) or its finalize lost. Measured with the catalog's `/insert` appending in a tight
  loop, 26 of a stock writer's 41 losses were found taken before the finalize. The turn is held under the writer's verified
  caller and its transaction's uuid (`commit_turns.Writer`), which pylance keeps when it stages the commit again on the
  version that won (measured on pylance 13.0.0: two calls, one uuid), so no other caller's commit can claim it. Its retry
  finalizes as the holder and releases the turn; a transaction naming no uuid is handed none, and neither is a Merge
  (`managed_versions.writer_turn`): Lance answers a Merge that lost to any write of data as a conflict its application
  re-runs (`lance_docs/lance/format/table/transaction.md` § Merge Compatibility, where every other operation is compatible
  with an Append or rebased over it), so pylance never stages it again. Measured with one handed, a writer appending
  through `/insert` waited 30 s for a retry that never came.
- **A handed turn that lapses unused simply lapses.** Nothing is held against its writer's identity, as a Kubernetes
  Lease (client-go `leaderelection.go`), an etcd lease or a ZooKeeper session lets expiry be the whole consequence of a
  lapse; a penalty keyed by caller would withhold turns from every worker of a pipeline that shares one workload
  identity.
- **A turn lapses after 30 s** (`HOLD`), timed on the store's clock, whether a door's commit took it, which its own loop
  releases at the end, or a stock writer was handed it, which its retry releases. Lance's own commit retries give up after
  30 s (`DEFAULT_COMMIT_RETRY_TIMEOUT`, `rust/lance/src/io/commit.rs` at v13.0.0), and each follows its conflict by a
  random 0 to 3 slots of 1.1 times the first attempt's duration, then 0 to 7 (`SlotBackoff`,
  `rust/lance-core/src/utils/backoff.rs`), so only that budget covers every retry a stock writer makes. Measured on the
  estate at helm rev 20: a stock writer's retries came 6.2, 6.7 and 11.7 s after its 409s, a turn handed for 5 s lapsed
  before each, and the writer failed its commit after four attempts beside 57 appends through `/insert`; offline, a writer
  reaching the catalog 2.5 s late, as on the estate, lands every append with the turn handed for the budget.
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
- A writer waiting in the event loop holds a connection and a task, not a thread, and stops waiting when its request is
  cancelled. Past the 20 seats a thread waits in, a door's commit on a contended ref is refused 409 code 14 for its caller
  to retry, so a fan-out of door commits on one table can see refusals it would not with no turn.
- One commit knowingly does not wait: the config-only commit a door's own attempt makes to disarm Lance's commit-path
  cleanup on an armed table, before it plans. It lands at most once inside a holder's window, which the holder's next
  attempt absorbs.
- A stock writer that lost its race holds the ref's other writers for the gap before its retry, which Lance's backoff
  makes up to 3.3 times its first attempt: 6.2 to 11.7 s on the estate, for each of its commits that loses once.
- A writer of a table can slow that table's other writers on purpose, by losing races on purpose: each loss holds the
  catalog's own writers of the ref for up to `HOLD`. A writer with write permission could already starve them without the
  turn, by appending in a tight loop, and each loss is audited under its subject.
- Many stock writers of one table still contend as Lance's own commit retries let them: measured at the estate's round
  trip, 16 writers of 2 appends each landed 10 of 32 with or without the turn, the rest failing Lance's own 30 s budget
  ("Too many concurrent writers"). One version per commit is managed versioning's ceiling, and the turn does not raise it.

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
- **A short handed turn renewed by a heartbeat**, as Iceberg's lock managers expire a lease 15 s after its holder's last
  heartbeat, sent every 3 s (`LOCK_HEARTBEAT_TIMEOUT_MS_DEFAULT`, `LOCK_HEARTBEAT_INTERVAL_MS_DEFAULT` in apache/iceberg
  `CatalogProperties`). A handed turn's holder is a stock client that knows nothing of it and sends nothing between its
  attempts, so its lease has to cover the client's whole retry window: rejected.
- **A cooldown on the writer whose handed turn lapsed**, fixed (3 min) or doubling as the kubelet's container restart
  back-off does (10 s to a 5 min cap, reset after 10 min without a problem; Kubernetes, Pod Lifecycle § Container restarts).
  No lease system read for this keys a penalty by the holder's identity, and keyed by caller it withholds turns from every
  worker of a pipeline sharing one identity, one of which lapsed: the owner chose to drop it.
- **Raising anyio's thread limiter** (`current_default_thread_limiter().total_tokens`, anyio `docs/threads.rst:281-295`)
  moves the point at which waiters exhaust the pool, at the memory and performance cost Starlette warns of
  (`docs/threadpool.md:16-32`), and bounds nothing: rejected.
- **Letting a waiter past the seats commit without waiting**, the turn advisory past N waiters. The commit would stay
  correct (the finalize's put-if-not-exists decides it), but a bulkhead rejects when full and never lets a call through
  unprotected (resilience4j `BulkheadConfig`, `maxWaitDuration` 0; Polly's `ConcurrencyLimiter`, queue 0), and the holder
  would lose the protection it was given: rejected.
- **`Retry-After` on the 409.** Neither the Iceberg REST spec nor the lance-namespace spec names one on a 409, both put it
  on 503 only (`rest-catalog-open-api.yaml` ServiceUnavailableResponse; `spec.yaml` ServiceUnavailableErrorResponse),
  Lakekeeper and Polaris send it on 503s alone, and neither Iceberg's HTTP client nor LanceDB's reads it on a 409: the owner
  chose not to add it.
