# 0240. The commit path is modelled in TLA+ (2026-11-17)

**Status:** accepted; a model of the port (ADR 0217 §5, ADR 0221). `CommitAnnounce.tla` (ADR 0229) covers what follows a
commit, its author record and its announcement; nothing modelled what decides whether a write lands, which one, how often,
and what its writer is told. Five defects were found, each confirmed against the running catalog and fixed model first
(owner, 2026-11-17: "Fix in Python now").

## Context

A version of a Lance table is a put-if-not-exists of the slot after the one its writer planned on. Around that one
conditional write the catalog adds: the doors' stamped commit (`dataplane.commit_stamped`: plan on the latest version,
judge, finalize exactly the next slot, plan again on a lost race), a stock client's commit through `version/create`
(ADR 0157), the primary key judged at every commit (`primary_keys`, LH-243, LH-540), the ref's commit turn
(`lakehouse.commit_turns`, LH-259), and the `Idempotency-Key` that makes a resent write converge (`lakehouse.idempotency`,
`catalog.api.idempotency`, LH-576, LH-577). The request deadline answers 503 at 300 s and lets the door run on
(`lakehouse.deadline`, ADR 0203), and a key's claim lapses after its lease, also 300 s (`idempotency.LEASE`). The code
argued each of these in comments; nothing checked them against each other.

## Decision

1. **`specs/catalog/CommitPath.tla` models one table's history under its writers.** Each action is one effect, in the
   code's order, and cites its function:
   - a keyed `/insert` (`endpoints/data.insert_into_table`), each attempt one process: the claim
     (`idempotency.claim`: a binding held for another request refused, a done key replayed, a live claim 409, a lapsed one
     taken over, a released or free one taken), the takeover's search for the attempt's commit
     (`dataplane.committed_under_key`), the plan and the key judged against the version planned on
     (`primary_keys.plan_new_keys`, `refuse_repeats_in_version`), the claim's renewal (`idempotency.renew`, through
     `DoorTable.finalize_version`'s `before_write`), the put, and the answer recorded (`idempotency.record_outcome`, written
     over whatever the record holds); a refusal gives back only the attempt's own claim (`idempotency.release`,
     `keyed_door`). An attempt starts once the one before it was answered, was told 503, or died;
   - an unkeyed `/insert` of one key value, a `/delete` of it (`dataplane.delete_from_table`), a stock client's commit
     (`versions.create_table_version`, `managed_versions.stage`, `judge`, `finalize`), a compaction through `version/create`
     that changes no row (`_refuse_a_rewrite_that_changes_rows`), and an `/insert` to a table whose key is a field inside
     a struct (`primary_keys.joinable`), all stamped commits but the last, which is Lance's own commit with its rebase
     when its guard is removed;
   - the commit turn: a writer that lost its race takes it, every other put waits while it is held
     (`TableTurns.take`, `wait`, `DoorTable.finalize_version`), it is released when its writer ends and lapses at its
     hold, its holder alive or not (`commit_turns._kept`);
   - the environment: a writer dies at any step; a keyed attempt's caller is told 503 at any step while it runs on
     (`RequestDeadlineMiddleware`); the key's lease lapses at any step but between a renewal and its put (A2).
   Every exclusion is the store's: the slot's put-if-not-exists, the claim's create-if-absent and replace under its ETag.
   Two catalog replicas are two processes of the model; the only in-process state the code keeps is the turn record's
   shared read and the thread seats (`commit_turns._SharedReads`, `THREAD_WAITERS`), which order waiting and decide no
   write.

2. **What it proves** (`make tla-check`; measured 2026-11-17 on two workers, four cores shared with other work):
   - `CommitPath.cfg` (a keyed insert sent three times with one body on a table declaring a key, beside an unkeyed insert
     of another key value; two deaths, two replans): 116,663 states, 10 s.
   - `CommitPathLane.cfg` (the same on a table declaring no key, beside a compaction): 155,185 states, 11 s.
   - `CommitPathRebody.cfg` (a key sent with one body, again with another, again with the first): 3,351 states, 1 s.
   - `CommitPathKeys.cfg` (one key value written by an insert, a stock client and a delete, beside a compaction; one
     death, one replan): 12,733 states, 2 s.
   - `CommitPathNested.cfg` (an append of a held value of a key nested in a struct, beside a delete of it): 27 states,
     1 s.

   Together about 25 s. The invariants, each a claim the code makes:
   - `KeyAppliedOnce`: the write a key names lands at most once, however often its caller sends it.
   - `AnsweredWriteLanded`: a write answered success is in the history at the version answered (a keyed attempt's at the
     commit carrying its key): the versions the catalog answers are the versions Lance holds.
   - `RefusedWriteAbsent`: a write answered 400 or 409 is not in the history. An answer the deadline already answered
     503 for is dropped and constrains nothing: 503 says the outcome is unknown, and the key is how it is learnt.
   - `KeyedRefusalIsTrue`: a keyed request is refused for a key value its table holds only while the write its key names
     is not in the history.
   - `SuccessIsForItsBody`: a keyed request answered success is answered for its own body.
   - `SuccessCountsWhatLanded`: a success answers the rows the commit it reports added.
   - `OneRowPerKey`: insert-if-absent; no version adds a key value the version before it holds (two inserts of one value,
     an insert beside a delete of it, a stock client's commit beside a catalog insert).
   - `EveryWriterEnds` (liveness): once failures stop, every writer is answered or gone; no turn is held for ever.

3. **What the model assumes, each a config that removes it and must fail:**
   - **A1.** A version slot is filled put-if-not-exists, one name per slot: the `dir` backend's `create_table_version`,
     and Lance's own commit on the table's V2 manifest names (`dataplane.py:478` creates every table with
     `enable_v2_manifest_paths=True`; `managed_versions.py:238` refuses a stock commit naming another scheme; measured
     here, a stamped commit and a Lance commit both write `_versions/18446744073709551613.manifest` for version 2).
     `CommitPathNoCAS.cfg`: an insert and a stock client of one value both land it (`OneRowPerKey`).
   - **A2.** A keyed attempt's put ends within the key's lease (`idempotency.py:71`, 300 s) of its renewal: the renewal
     runs after the turn wait, right before the put (`table_claims.py:251`), so one write is left to it.
     `CommitPathLeaseInCommit.cfg`: the lease lapses between the renewal and the put, a resend takes the key over, finds
     no commit and appends, and the first lands too (`KeyAppliedOnce`).
   - **A3.** A turn lapses at its hold whether its holder lives or not (`commit_turns.py:153-157`, `HOLD` 30 s).
     `CommitPathNoTurnLapse.cfg`: a writer that lost its race dies holding the turn and every other writer waits for ever
     (`EveryWriterEnds`).

   **A turn lapsing under a live holder is not a defect here.** ADR 0227 §8 found a create fence lapse under a live
   holder and a commit landing over another's; a commit turn has the same lapse but decides nothing: every version is
   still a put-if-not-exists of the slot after its plan, so a writer the lapse lets in races the holder and the store
   picks one. The gate configs let `TurnLapse` fire whenever a turn is held, holder alive or not, and every safety
   invariant holds. A turn is fairness: a writer that loses `_STAMPED_COMMIT_ATTEMPTS` (10) races is answered 409 code 14
   (`commit_stamped`), never left waiting; the model bounds that to `MaxTries`.

4. **The model was shown to bite.** Eight defects were planted one at a time in a copy, and the gate reported each:
   a takeover that ignores the commit it finds (`KeyedRefusalIsTrue`), a done key run again instead of replayed
   (`KeyedRefusalIsTrue`), a renewal that does not check the claim is its own (`KeyedRefusalIsTrue`), a live claim taken
   over (`KeyedRefusalIsTrue`), a stamped commit that rebases instead of planning again (`OneRowPerKey` on
   `CommitPathKeys.cfg`), a commit answering the version before its own (`AnsweredWriteLanded`), a refusal releasing a
   claim that is not its own (`KeyedRefusalIsTrue`), and a stamped insert and a stock commit skipping their key judgement
   (`OneRowPerKey` on `CommitPathKeys.cfg`). Two planted changes were not reported, and are not defects of the design:
   a recorded answer naming the attempt's own body (with `KeyBindsBody` a takeover's body is the commit's), and a writer
   keeping its turn after it ends (the turn lapses, A3).

5. **Five findings, each confirmed against the running catalog on 2026-11-17 and each FIXED.** Each guard is a step of
   the model with a config that removes it and must fail, and a test that was red before the fix and is red again with
   the guard removed. The race tests drive the model's trace step by step through the real catalog app on real pylance:
   the first attempt held after its claim and before its write, the resend run whole under a lease driven to zero (the
   lease having passed), the first released, and the harm read over HTTP. They run on the local root and, in
   `make lifecycle-rustfs-check`, on RustFS 1.0.3's conditional writes (`race_ns_client`'s `s3` parameter).
   - **KeyTakenOver, applied twice** (`KeyAppliedOnce`). The deadline answers 503 at 300 s and the door runs on; the
     lease is 300 s; so a resend under the key takes the key over (`idempotency.py` claim, base line 254) while the first
     attempt still runs, looks for its commit (`data.py` base line 274), finds none, and appends; the first then appends
     too (`dataplane.py` base line 2906, Lance's own insert). Both answered 200 and the table held the rows twice.
     Shortest trace (`CommitPathTakeover.cfg`, 12 states): attempt 1 claims and plans; it is told 503; attempt 2 starts;
     the lease lapses; attempt 2 takes the key over and finds no commit; attempt 1 commits; attempt 2 plans and commits.
     Guard **KeyRenews**: every keyed insert is a stamped commit, whatever its table declares (`dataplane.py:2934`, Lance's
     own insert captured as `update` and `delete` are, `dataplane.captured`), and right before each put, after its turn
     wait, renews its claim: a replace under the ETag of the read that found the claim its own, `claimed_at` moved to the
     store's clock (`idempotency.renew`, `idempotency.py:307`; `Converge.renewal`; `commit_stamped(before_finalize=…)`;
     `DoorTable.finalize_version(before_write=…)`). Of the renewal and a takeover the store admits one: a takeover finds
     the lease renewed, or the renewal finds the claim another attempt's and the commit stops with
     `dataplane.SupersededError`, 409 code 14, nothing finalized. Config `CommitPathTakeover.cfg`. Test
     `tests/integration/test_a_keyed_write_lands_once_beside_its_resend.py::…[no-key]`: before the fix both answered 200
     and the table held 7 rows at version 3; now the first answers 409 code 14, the resend 200, a third send the resend's
     200, and the table 5 rows at version 2; with the guard removed it fails, on the local root and on RustFS.
   - **RefusalTakenOver** (`KeyedRefusalIsTrue`). On a table declaring a key the same interleaving does not land twice:
     the first attempt's rows meet the values the resend landed and it is refused 400, "the table already holds a row
     with primary key", for the write its own key landed. Guard **RefusalHoldsKey**: a refusal a keyed door raises after
     its claim gives the claim back only when it is still the attempt's own (`idempotency.release` now answers whether it
     was), and otherwise answers 409 code 14, the key's write maybe landed (`keyed_door`, `api/idempotency.py:261`).
     Config `CommitPathRefusalTakenOver.cfg`. Test `…[primary-key]`: 400 code 13 before, 409 code 14 now, red with the guard
     removed, on both stores.
   - **KeyBodyUnbound** (`SuccessIsForItsBody`). A key bound only its door and object (`idempotency.KeyBinding`, base
     lines 73-83), so a resend under the key with other rows, or a create with another mode, was answered the first
     attempt's 200 and its own body applied nowhere. Two existing tests asserted exactly that as intended. Guard
     **KeyBindsBody**: the binding carries a digest of the request's query, in the order of its names, and its body
     (`api.idempotency.fingerprint`, `KeyBinding.request`), so the same key with another request is refused 400 code 13
     (`KeyReusedError`), as a key on another door or object already was. Config `CommitPathKeyBody.cfg`. Tests
     `test_a_retried_create_does_not_rewrite_the_table.py::TestAReplayIsAnsweredNotReExecuted::test_the_same_key_never_runs_the_create_again[another-request]`
     and `TestARetriedAppendLandsOnce::test_the_same_key_appends_once_and_answers_the_first_response[other-rows]`,
     parametrized beside the same-request case; each answered 200 before and fails with the guard removed.
   - **TakeoverMiscounts** (`SuccessCountsWhatLanded`). A takeover answered the rows the found commit added off an
     `Append`'s `fragments` (base line 2846); an append to a table declaring a key commits Lance's `when_matched_fail`
     merge, an `Update`, so the answer said `num_inserted_rows: 0` for two rows. Guard **TakeoverCounts**: an `Update`'s
     added rows are its `new_fragments` (`dataplane._added_fragments`; measured on pylance 13.0.0, that merge writes every
     row it inserts there). Config `CommitPathTakeoverCount.cfg`. Test
     `test_an_append_whose_answer_was_lost_past_the_lease_is_found_in_its_commit[primary-key]`: 0 before, 2 now, red with
     the guard removed.
   - **NestedKeyAppended** (`OneRowPerKey`). An append whose key is a field inside a struct, which Lance's merge cannot
     join on, was Lance's own insert (base line 2894: `joinable` false sent it there) and judged nowhere against the table,
     though `primary_keys`' module note said every commit is: an append of a held value answered 200 and the table held it
     twice. Guard **AppendJudged**: an append to a table declaring any key is a stamped commit, Lance's own insert
     captured, so `refuse_repeats_in_version` judges its rows against the rows the version it lands on carries.
     Config `CommitPathNestedUnjudged.cfg`. Test
     `test_a_repeated_primary_key_is_refused_at_every_door.py::test_a_write_landing_a_held_key_nested_in_a_struct_is_refused[insert]`,
     parametrized beside the merge case it shared a seam with: 200 before, 400 code 13 now, red with the guard removed.

6. **Designs chosen.** For a resend while its first attempt runs, a lease longer than the deadline was not taken: the
   door runs on past the deadline for as long as it runs, so no lease length bounds it, and it is a timing argument the
   store does not arbitrate. A heartbeat renewing the claim while the door runs was not taken for the reason ADR 0227 §8
   gives: a stalled process renews nothing and its commit lands anyway. The renewal right before the put is a conditional
   write the store arbitrates and leaves one write to A2. Making the keyed insert a stamped commit, rather than renewing
   before Lance's own insert, was chosen because Lance's insert writes its rows and commits, with its own retries, in one
   call, which would leave the whole write to A2; the stamped commit puts once, after the renewal, and a lost race plans
   again under the ref's turn. Lance's `commit_lock` hook was not taken: a custom commit lock replaces the store's
   conditional put for that writer (`lance_docs/lance/guide/object_store.md` § Custom commit locks), so every other writer
   of the table would have to share it. The request digest covers the query and the body, not the headers: the run a
   Context header names changes what lineage records, not which rows land. A stock client's path is unchanged: no field,
   no call.

7. **The four facts.** GitOps: no new record, object or step; the renewal rewrites the claim the key already writes, and
   an unrenewed claim lapses as before. Cloud native: every exclusion is a conditional write on the object store (the
   renewal and the takeover under the claim's ETag, the slot's put-if-not-exists), so two replicas arbitrate as two
   threads do; the RustFS lane runs the race tests on a real store's conditional writes; no in-process lock, no new store.
   Lance-namespace/table-idiomatic: a stock client's path is the spec's exactly; the catalog's doors commit through
   Lance's own operations, planned and finalized with the backend's `create_table_version`; every refusal is a spec code
   (ConcurrentModification 14, InvalidInput 13). Zero trust: an attempt commits only while the store confirms its claim is
   its own, and a key binds the request a caller sent, so no answer stands for a request it was not.

## What a client observes that differs

- A resend under an `Idempotency-Key` with another body or other query parameters answers 400 code 13, where it answered
  the first request's 200.
- A keyed `/insert` whose key a resend took over while it ran answers 409 code 14 and commits nothing; the resend's
  answer is the key's. A keyed refusal (4xx) after its key was taken over answers 409 code 14 instead of the refusal.
- A takeover of a keyed append to a table declaring a key answers the rows it inserted, not 0.
- An `/insert` to a table whose key is a field inside a struct is refused 400 code 13 when a row repeats a held value.
- A keyed `/insert` to a table declaring no key commits stamped: under contention a writer losing ten races answers 409
  code 14 where Lance's own retries would have rebased it, and a resend under its key converges.

## The flaky turn test

`test_writers_waiting_on_a_held_commit_turn_leave_the_catalog_answering_other_tables` failed under load on its wall-clock
bound (`slowest < 2`): measured with six busy loops on four cores, a describe of another table took 2.6 s and 2.7 s while
fifty writers waited, and every turn assertion (no write lands while the turn is held, the shared read's rate) held. It
is the test's timing, not an ordering defect. A describe starved of request threads waits for a waiter to hand one back,
which happens only when the waiter's wait ends: the turn is released after the test's loop, so not before `HOLD` (30 s).
The bound is now a third of `HOLD`: it passes under the same load, and with the bulkhead removed (`THREAD_WAITERS` at
100, so fifty threads wait) the describe took 30.5 s and the test fails.

## Not modelled

The lifecycle doors and their fences (`TableLifecycle.tla`): every write door opens the table under the claim its
resolution read (`DoorTable.open`, `require_held`), and a finalize naming the table asks again right before its put
(`require_named`); a first version holds its own fence (`first_version_fenced`, ADR 0227 §9); an unkeyed Lance append to a
table declaring no key checks only at its open, so a destroying drop between that open and its commit leaves its manifest
at a deleted location, a residue reconcile reports and no client reads (the append and the drop overlap, and the append
is read as having come first). Erasure's release and the history fence (`Erasure.tla`, `Fence.tla`): a data commit does
not read the history fence, which holds ref writes and reclaims. Branches (each ref a history of its own, judged alike).
Merge-insert's and update's own shapes beyond one key value. The create door's key: a keyed create whose first attempt
outlives its lease is the same shape as KeyTakenOver; a `mode=Create` resend meets the first's create fence (409 code
14), and a `mode=Overwrite` resend could overwrite a second time; it is UNDECIDED, settled by the race test above driven
through `/create?mode=Overwrite`, and its fix is the renewal before the create's commit (`_write_blob`'s
`before_create_commit`, in code other work is changing now). A stock client's retry after a 503 (pylance reads its
version back, LH-491). The shed (`WriteConcurrencyLimitMiddleware`) sits inside the deadline (`lance_app.py`), so a door
that runs on keeps its seat.

## Consequences

- The Rust catalog keeps the renewal before the put, the request digest in the key's binding, and the refusal checked
  against the claim, or changes this model first.
- `make tla-check` runs the five gate configs and eight that must fail, about 40 s on two workers.
