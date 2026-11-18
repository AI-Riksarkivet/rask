# 0240. The commit path is modelled in TLA+ (2026-11-17)

**Status:** accepted; a model of the port (ADR 0217 §5, ADR 0221). `CommitAnnounce.tla` (ADR 0229) covers what follows a
commit, its author record and its announcement; nothing modelled what decides whether a write lands, which one, how often,
and what its writer is told. Five defects were found, each confirmed against the running catalog and fixed model first
(owner, 2026-11-17: "Fix in Python now"). An independent review of that fix found three more and a regression in the
commonest write path; each was reproduced, modelled and fixed (§8 to §10). A joint review of the merged keyed-commit and
seal fixes found a key that lands twice after a chain of takeovers or a refused one, and a rule pinned by no test;
each was reproduced, modelled and fixed (§11). A third review found a key landing twice after the version it landed as
was removed, a search costing the table's whole history, a rule nothing needed, and a reconciler finish never retried;
each was reproduced, modelled and fixed (§12). A fourth review found that search refusing a key whose write still
stands, and the reconciler's mark held for ever after a cleanup; each was reproduced, modelled and fixed (§13). The
seal's steps are modelled here too (ADR 0242 § Found by review, second, third and fourth rounds).

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
   - a keyed write, each attempt one process, standing for every keyed door that lands a version (§8): an `/insert` to a
     table declaring a key (a stamped commit, judged), one to a table declaring none (Lance's own commit, which rebases:
     mix `lane`), a `/create` mode=Overwrite (an Overwrite, Lance's own commit: mix `overwrite`) and a `/restore` (a stamped
     Restore: mix `restore`). The claim (`idempotency.claim`: a binding held for another request refused, a done key
     replayed, a live claim 409, a lapsed one taken over, a released or free one taken), the takeover's search for the
     attempt's commit (`dataplane.landed_under_key`), the plan and the key judged against the version planned on
     (`primary_keys.plan_new_keys`, `refuse_repeats_in_version`), the claim's renewal before each put
     (`idempotency.renew`, through `DoorTable.finalize_version` or `dataplane.land_keyed`), the put, and the answer
     recorded over the attempt's own claim only (`idempotency.record_outcome`); a refusal gives back only the attempt's own
     claim (`idempotency.release`, `keyed_door`). An attempt starts once the one before it was answered, was told 503, or
     died;
   - an unkeyed `/insert` of one key value, a `/delete` of it (`dataplane.delete_from_table`), a stock client's commit
     (`versions.create_table_version`, `managed_versions.stage`, `judge`, `finalize`), a compaction through `version/create`
     that changes no row (`_refuse_a_rewrite_that_changes_rows`), and an `/insert` to a table whose key is a field inside
     a struct (`primary_keys.joinable`), all stamped commits but the last, which is Lance's own commit with its rebase
     when its guard is removed; an unkeyed `/insert` to a table declaring no key is Lance's own commit, refused when its
     rebase crosses an Overwrite or a Restore (`lance_docs/lance/format/table/transaction.md` § Append Compatibility);
   - the commit turn: a writer that lost its race takes it, every other put waits while it is held
     (`TableTurns.take`, `wait`, `DoorTable.finalize_version`), it is released when its writer ends and lapses at its
     hold, its holder alive or not (`commit_turns._kept`);
   - the environment: a writer dies at any step; a keyed attempt's caller is told 503 at any step while it runs on
     (`RequestDeadlineMiddleware`); the key's lease lapses at any step but between a renewal and its put (A2).
   Every exclusion is the store's: the slot's put-if-not-exists, the claim's create-if-absent and replace under its ETag.
   Two catalog replicas are two processes of the model; the only in-process state the code keeps is the turn record's
   shared read and the thread seats (`commit_turns._SharedReads`, `THREAD_WAITERS`), which order waiting and decide no
   write.

2. **What it proves** (`make tla-check`, or `scripts/tla_check.sh CommitPath` for this model alone; measured 2026-11-17 on all four cores, shared with other work at a load average of 10.6: the nineteen CommitPath configs, the seven below that hold and the twelve that must fail, in 95 s):
   - `CommitPath.cfg` (a keyed insert sent three times with one body on a table declaring a key, beside an unkeyed insert
     of another key value; two deaths, two replans): 126,808 states.
   - `CommitPathLane.cfg` (the same on a table declaring no key, Lance's own commit renewed before each put, beside a
     compaction): 131,298 states.
   - `CommitPathRebody.cfg` (a key sent with one body, again with another, again with the first): 3,423 states.
   - `CommitPathKeys.cfg` (one key value written by an insert, a stock client and a delete, beside a compaction; one
     death, one replan): 12,733 states.
   - `CommitPathNested.cfg` (an append of a held value of a key nested in a struct, beside a delete of it): 27 states.
   - `CommitPathOverwrite.cfg` (a keyed `/create` mode=Overwrite sent twice, beside an unkeyed append; one death): 2,722
     states.
   - `CommitPathRestore.cfg` (a keyed `/restore` sent twice, beside an unkeyed append; one death, one replan): 3,659
     states.

   The invariants, each a claim the code makes:
   - `KeyAppliedOnce`: the write a key names lands at most once, however often its caller sends it.
   - `AnsweredWriteLanded`: a write answered success is in the history at the version answered (a keyed attempt's at the
     commit carrying its key): the versions the catalog answers are the versions Lance holds.
   - `RefusedWriteAbsent`: a write answered 400 or 409 is not in the history. An answer the deadline already answered
     503 for is dropped and constrains nothing: 503 says the outcome is unknown, and the key is how it is learnt.
   - `KeyedRefusalIsTrue`: a keyed request is refused for a key value its table holds only while the write its key names
     is not in the history.
   - `SuccessIsForItsBody`: a keyed request answered success is answered for its own body.
   - `AnswerStands`: once a key's answer is recorded, every later send under the key is answered with it (§9).
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
   - **A2.** Each put of a keyed attempt ends within the key's lease (`idempotency.py` `LEASE`, 300 s) of the renewal before
     it: the renewal runs after the turn wait, right before the put (`DoorTable.finalize_version`, `dataplane.land_keyed`,
     which renews again before each of its puts), so one write is left to it.
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
   write the store arbitrates and leaves one write to A2. Lance's own one-call insert was not kept for a keyed write: it
   writes its rows and commits them, with its own retries, in one call, which leaves the whole write to A2. The first fix
   made every keyed insert a stamped commit instead; review measured it serializing every keyed append (§10), and it is
   replaced by Lance's two-step form (`lance_docs/lance/guide/distributed_write.md` § Append data: `write_fragments`, then
   `LanceDataset.commit` of the transaction it returns): the rows are written once, and each put of the commit is one Lance
   commit with no retry of its own, preceded by the renewal (`dataplane.land_keyed`, §10). Lance's `commit_lock` hook was
   not taken: a custom commit lock replaces the store's conditional put for that writer
   (`lance_docs/lance/guide/object_store.md` § Custom commit locks), so every other writer of the table would have to share
   it. The request digest covers the query and the body, not the headers: the run a Context header names changes what
   lineage records, not which rows land. A stock client's path is unchanged: no field, no call.

7. **The four facts.** GitOps: no new record, object or step; the renewal rewrites the claim the key already writes, and
   an unrenewed claim lapses as before. Cloud native: every exclusion is a conditional write on the object store (the
   renewal and the takeover under the claim's ETag, the slot's put-if-not-exists), so two replicas arbitrate as two
   threads do; the RustFS lane runs the race tests on a real store's conditional writes; no in-process lock, no new store;
   the claim a request holds rides the request's own context (`idempotency.holding`), which no other request and no other
   replica reads. Lance-namespace/table-idiomatic: a stock client's path is the spec's exactly; the catalog's doors commit
   through Lance's own operations: a judged write planned and finalized with the backend's `create_table_version`, an
   append or an overwrite through Lance's own commit, which rebases it as Lance rebases any append; every refusal is a spec
   code (ConcurrentModification 14, InvalidInput 13). Zero trust: an attempt lands its write only while the store
   confirms its claim is its own, a key binds the request a caller sent, so no answer stands for a request it was not, and
   an answer is recorded only over the attempt's own claim; a cached store client signs with the one credential it was
   built for, and a rotated credential is another client.

8. **Every door that takes an `Idempotency-Key`, what it lands, and how its once-ness is held.** One mechanism for every
   door that lands a version: the keyed dependency (`catalog.api.idempotency.keyed_door`, `KeyedDoor.begin`) puts the
   attempt's claim, a `lakehouse.idempotency.HeldKey`, in the request's context, and every seam a version of the key's
   object lands through reads it (`dataplane.held_key`): it stamps the key on the commit (`rask.idempotency_key`) and
   renews the claim right before each put (`dataplane.renew_held`). The seams are `DoorTable.finalize_version` (every
   stamped commit, through `commit_stamped`) and `dataplane.land_keyed` (every Lance commit a keyed request makes:
   `_land_keyed_rows` for an insert, `_write_blob` for a create). A door passes nothing down, so a new keyed door cannot land
   a version unrenewed by forgetting to; this was chosen over handing the door the only finalizer, which would have had
   every service function between the door and the seam take and pass it, and over a finalize that refuses an unrenewed
   keyed request, which needs the same context and then only fails what the context lets it fix. A takeover of every door
   that lands a version looks for the version a lapsed attempt landed under the key (`dataplane.landed_under_key`) and
   answers it. Each case below is a case of
   `tests/integration/test_a_keyed_write_lands_once_beside_its_resend.py::test_a_keyed_door_resent_while_its_first_attempt_runs_acts_once`
   unless named: the first attempt held after its claim, the resend run whole past the lease, another write between, the
   first released.

   | Door | What the keyed write lands | Once-ness held by | Case, config |
   | --- | --- | --- | --- |
   | `/insert`, append, a table declaring a key | a stamped commit, judged (`plan_new_keys`, or Lance's insert captured for a nested key) | KeyRenews at `finalize_version`; RefusalHoldsKey; takeover scan | `test_an_append_resent…[primary-key]`; `CommitPath.cfg`, `CommitPathRebody.cfg`, `CommitPathRefusalTakenOver.cfg` |
   | `/insert`, append to a table declaring none, or overwrite | Lance's own commit of fragments written first (`land_keyed`) | KeyRenews before each put; RenewIsOwn; the lost put read for its own transaction | `test_an_append_resent…[no-key]`, `test_a_resend_that_took_the_key_over…`, `test_a_keyed_append_whose_landed_put_was_answered_lost_lands_once`; `CommitPathLane.cfg`, `CommitPathTakeover.cfg`, `CommitPathRenewNotOwn.cfg` |
   | `/create` mode=Overwrite of a table | Lance's own commit of an Overwrite (`_write_blob`, `land_keyed`) | KeyRenews before each put; takeover scan (`create_table`) | `[create-overwrite]`; `CommitPathOverwrite.cfg`, `CommitPathOverwriteUnrenewed.cfg` |
   | `/create` mode=Create, or ExistOk of a new name | the table's first version (`_write_blob`, `land_keyed`, after the create fence's renewal) | KeyRenews before the put; the create fence (ADR 0227) | `[create]`: a create landed again would bring back the table dropped since |
   | `/create` mode=ExistOk of a standing table | nothing | RecordOwnClaim: its true answer is not recorded over the resend's | `[create-exist-ok]`; `CommitPathAnswerReplaced.cfg` |
   | `/restore` | a stamped commit of Lance's Restore (`dataplane.restore`) | KeyRenews at `finalize_version`; takeover scan (the door) | `[restore]`; `CommitPathRestore.cfg`, `CommitPathRestoreUnrenewed.cfg` |
   | `/register` | the backend's row and the location's claim, no version | a renewal right before the backend's `register_table` (the door) | `[register]`: found by this case, see §9 |
   | `/drop` | the backend's delete or the trash record, no version | the door's resolution: the claim's ETag (ADR 0173 §7) | `[drop]`: the table created since stands |
   | `/deregister` | the backend's row, no version | the door's resolution | `[deregister]`: the table created since stands |
   | `/rename` | the pointer's move, no version | the door's resolution: the renames between write the claim | `[rename]`: renamed back since, it stays |

   The model's keyed actor stands for the four that land a version (`specs/catalog/CommitPath.tla`'s header maps them);
   the four that land none are held by the lifecycle's own fences and resolution (`TableLifecycle.tla`), and their race
   cases turn what review read as reasoned into a run.

9. **Found by review and FIXED (2026-11-17).** An independent review blocked the first fix for three problems; each was
   reproduced here first, on the local root and on RustFS 1.0.3, before its fix.
   - **B1, a keyed `/create?mode=Overwrite` whose key was taken over still committed: applied twice** (`KeyAppliedOnce`).
     Nothing renewed the claim before the create's commit. Trace: v1 holds 1, 2, 3; the first attempt is held; the resend
     overwrites (200, v2); an append of 9 lands (v3); the first is released and overwrites again (200, v4): the table held
     two rows, the 9 erased, and a third send answered the first attempt's v4, `record_outcome` having written over the
     resend's answer. Guard **KeyRenews** at the create door: `_write_blob` writes the rows first and lands them through
     `land_keyed` when the request holds a key (`dataplane.py` `_write_blob`, `land_keyed`). A superseded attempt answers 409
     code 14 and lands nothing. Config `CommitPathOverwriteUnrenewed.cfg`; case `[create-overwrite]`: 200 before, 409 code
     14 now; with the renewal removed from `land_keyed` it fails, as do `[create]`, `[no-key]` and the RenewIsOwn test.
   - **B2, a keyed `/restore` whose key was taken over still committed: applied twice** (`KeyAppliedOnce`). `dataplane.restore`
     called `commit_stamped` without the renewal. Trace: v1 holds 1, 2, 3; v2 adds 4; the first attempt of a restore to v1
     is held; the resend restores (v3); 9 is appended (v4); the first restores again (200, v5, three rows). Guard
     **KeyRenews** at every stamped commit: `commit_stamped` reads the held key itself, so restore, like every stamped door,
     renews at `finalize_version` without being told (`dataplane.py` `commit_stamped`). Config
     `CommitPathRestoreUnrenewed.cfg`; case `[restore]`: 200 before, 409 now; with `finalize_version`'s renewal removed it
     fails.
   - **B3, the renewal's owner check was pinned by no test** (REPRODUCED by mutation: dropping
     `current.state != "in_flight" or current.attempt != attempt` from `idempotency.renew` left every test green). Guard
     **RenewIsOwn** (`idempotency.py` `renew`). The interleaving that needs it: the first attempt held before its write;
     the resend takes the key over, finds no commit, and is held before its write; the first is released and reaches its
     commit; the resend is released and commits. Config `CommitPathRenewNotOwn.cfg` (`KeyAppliedOnce`); test
     `test_a_resend_that_took_the_key_over_keeps_it_while_the_first_attempt_reaches_its_commit`: the first answers 409 and
     the resend 200 with five rows; with the check removed both answered 200 and the table held seven.
   - **An answer written over the key's** (B1's third send). Guard **RecordOwnClaim**: `record_outcome` replaces the record
     only under the ETag of a read that found the attempt's own claim, and otherwise records nothing (`idempotency.py`
     `record_outcome`, which now takes the attempt). Invariant `AnswerStands`: the answer a key serves, once recorded, is
     the one it serves from then on. With every other guard on no interleaving of the model lands a second write to
     record (checked: the lane gate holds with RecordOwnClaim removed), so it is defence in depth; with KeyRenews removed
     as well `CommitPathAnswerReplaced.cfg` fails, and with KeyRenews alone removed `AnswerStands` still holds. Case
     `[create-exist-ok]`: an ExistOk that finds the table answers its version, 2, which is true; the third send under the
     key answered 2 before and answers the resend's 1 now; with the rule removed it fails.
   - **A keyed `/register` superseded while it ran registered again** (found by its race case, not by review, which read it
     as held by a name that no longer resolves): the first attempt held; the resend registers the dataset; the table is
     deregistered; the first attempt registered it again, 200. Guard: a renewal right before the backend's
     `register_table` (`endpoints/tables.py` `register_table`, `dataplane.renew_held`). Case `[register]`: 200 before, 409
     now; with the renewal removed it fails. A keyed `/create` of a new name (mode=Create) was the same before its first
     version renewed: the resend creates, the table is dropped, and the first attempt created it again; case `[create]`.
   - **The look-back of a takeover's search was coupled to the renewal** (the time search this item describes was
     replaced by the version search of §11, which a chain of takeovers or a released key can no longer move). The renewal moves `claimed_at`, and a takeover
     searched from the lapsed claim's `claimed_at` less 10 minutes, so a write longer than that between its plan and its
     renewal would have been missed. Removed: a claim keeps `since`, when its attempt first claimed the key, which no
     renewal moves (`idempotency._Claimed.since`, `TakenOver.since`), and every write is planned after its claim, so the
     search from `since` less the clock skew (`dataplane._CLOCK_SKEW`) finds it however long the write took.
   - **A takeover of a keyed `/create` or `/restore` landed the write again** (found by the model, whose claim of a lapsed
     key goes on to the search for every keyed writer, where only the insert door searched): a first attempt that landed
     and was lost before it recorded its answer was overwritten or restored a second time by the resend that took its key
     over. Guard: every door that lands a version searches (`dataplane.landed_under_key`, in `create_table` for a table
     standing under the name and in the restore door), and both now record their answer right after the commit, before the
     lineage emit, as the insert does. Test
     `test_a_retried_create_does_not_rewrite_the_table.py::TestARetriedAppendLandsOnce::test_a_write_whose_answer_was_lost_past_the_lease_is_found_in_its_commit`,
     the insert's case parametrized with `[create-overwrite]` and `[restore]`: each red with the search removed (the write
     landed again, at the version after).
   - **A store that answers a put that landed as a lost race** (a conditional put retried after its answer was lost meets
     its own object): Lance reports a retryable conflict for the version it tried, and committing the same transaction
     again lands it a second time (measured on pylance 13.0.0). `land_keyed` reads that version for its own transaction
     before it puts again; test `test_a_keyed_append_whose_landed_put_was_answered_lost_lands_once`, red without the read
     (the append answered v3 and landed twice).

10. **The cost of once-ness, measured, and what was removed.** Review measured the first fix serializing every keyed
    append to a table declaring no key. Reproduced here with the reviewer's shape: the catalog app under `TestClient`,
    pylance 13.0.0 from the locked environment, a table of 1M rows (an `int64` and a `string` column), one-row inserts;
    RustFS 1.0.3 run as a process (`scripts/rustfs_server.sh`); each phase timed by wrapping the function that makes it.
    A keyed append before (stamped): Lance's insert run through the capturing namespace (183 ms on the local root, 200 ms on
    RustFS, the handle opened through a namespace answering each of Lance's version probes), the staged manifest copied,
    re-read and deleted (231 ms on RustFS), a detached commit (txn and manifest puts), four judges, the committed files
    sealed and recorded, the manifest copied for the finalize, the turn wait, the renewal, the name's check, the backend's
    put, and two deletes of the detached manifests (131 ms); and under contention every loser planned again under the
    ref's turn, so sixteen appends landed one by one. After, for a table declaring no key: the fragments written once
    (`write_fragments`), their data files sealed and recorded, then per attempt the turn wait, the renewal and one Lance
    commit (its rebase reads and its put), a lost put followed by one read of the version it tried; once landed, the
    transaction file sealed and recorded and the author recorded, as the unkeyed append records them. Nothing is judged
    that has nothing to judge: no key, no contract read, no encoding or label check, since an append keeps the table's
    schema (Lance refuses one that does not). A table declaring a key keeps its judged, stamped commit.

    Every control-root record, claim, seal and fence built a new boto3 client per call, about 6 ms of the interpreter
    lock each and a fresh connection: sixteen keyed appends built some 350. `lakehouse.storage_options.object_client` now
    keeps one per seam, endpoint, credential and region (32 at most), which every path shares; a store read under that
    load fell from 80 ms to 7 ms.

    The cost, measured 2026-11-17 on four cores shared with other agents' suites (the load average beside each row: the
    machine was never idle), three samples each (the first insert of each kind a warm-up, not counted); "keyed before"
    is the first fix, "after" this one:

    | Store | Writers | Unkeyed (s) | Keyed before (s) | Keyed after (s) | Load |
    | --- | --- | --- | --- | --- | --- |
    | local root | 1 | 0.037, 0.039, 0.036 | 0.140, 0.210, 0.227 | 0.034, 0.040, 0.049 | 3.0 before, 10.8 after |
    | RustFS 1.0.3 | 1 | 0.434, 0.158, 0.251 | 0.703, 0.711, 0.896 | 0.207, 0.230, 0.272 | 2.7 before, 10.8 after |
    | local root | 16, p50 | 0.373, 0.337, 0.414 | 2.038, 2.291, 2.161 | 0.438, 0.446, 0.869 | 3.0 before, 10.6 after |
    | RustFS 1.0.3 | 16, p50 | 1.471, 1.430, 1.804 | 12.778, 13.675, 12.337 | 1.255, 1.581, 2.431 | 2.6-4.5 before, 11.0 after |

    Unkeyed under the after load: 0.028, 0.027, 0.057 (local, 1); 0.118, 0.165, 0.169 (RustFS, 1); 0.458, 0.436, 0.434
    (local, 16); 1.784, 1.300, 1.373 (RustFS, 16; walls 3.5, 2.2, 5.3 s against keyed 2.1, 3.1, 4.0 s). The unkeyed path
    gained the shared client too. No 409 in any run.

    One keyed append of one row on RustFS, after (272 ms): the door's claim (a read and a create, 23 ms), the coercion and
    its open (14 ms), the checked open (31 ms over two), the fragments written (19 ms), their data file sealed and recorded
    (15 ms), the turn wait (cached), the renewal (a read and a replace, 15 ms), one Lance commit (28 ms), the transaction
    file sealed and recorded (15 ms), the answer recorded (a read and a replace, 23 ms), the lineage emit (12 ms). Against
    the unkeyed append (169 ms) a keyed one pays the renewal (one round-trip pair), and what a key cost before this fix
    too: the claim and the answer (two pairs), and the data file sealed before its version lands, as a stamped commit's
    were, which adds one record write over the unkeyed append's single after-landing record. At sixteen writers the
    remaining cost is the renewal before every put: a writer that lost its put renews before the next (a pair a loss), and
    the slot wait after a loss (`_KEYED_RETRY_SLOTS`). What would remove it upstream: a Lance commit that takes a
    pre-commit hook run between its rebase and its put (it has only `commit_lock`, which replaces the conditional put),
    or a conditional put a client can make over two objects at once (the claim and the manifest).

11. **Found by a joint review of the merged fixes, and FIXED (2026-11-18).**
    - **A keyed write landed twice after a chain of takeovers, or after a takeover refused before its search**
      (`KeyAppliedOnce`; REPRODUCED on the local root first, the review's scratch tests answering 7 rows where 5 were
      written). A takeover wrote its new claim with `since` set to the lapsed claim's own `since`, so a takeover of a
      takeover searched from the second attempt's claim: A1 appends v2 and loses its answer; A2 takes over, finds v2,
      loses its answer too; A3 takes over from A2, searches from A2's claim less ten minutes, misses v2 (it was older),
      and appends again. And a released key was answered "nothing was written" even when the claim released was a
      takeover of a lapsed attempt that had landed: A1 lands and loses its answer; A2 takes over and is refused 409
      before its search (a busy turn, a table that changed), which gives the key back; A3 claims the released key and
      appends again. The model missed both: its search had no bound and nothing could refuse between a takeover and its
      search. Guards **KeyKeepsFirst** and **ReleaseKeepsWritten**: a key's record keeps, for its whole life, the lowest
      version any attempt under it planned a put on (`idempotency._Claimed.written_from`), which the renewal right before
      each put records (`idempotency.renew(planned_on=…)`, `HeldKey.renew`, `dataplane.renew_held`: `land_keyed` passes
      the transaction's read version, `commit_stamped` the version it planned on, at `finalize_version`), and which no
      takeover and no release moves (`idempotency.claim` carries it onto the new claim). Every attempt claiming a key
      whose record says a put may have landed is told so (`TakenOver(written_from)`), a released key's included, and its
      door searches before it writes. The search is by the key's stamp on the ref's versions after `written_from`
      (`dataplane.landed_under_key`), so no clock bounds it: the ten-minute skew is gone, and with it the time
      search's coupling of a pod's clock to the store's. Chosen over the time of the first claim (the brief's first form)
      because the history read is bounded by what the time search read, a subset of it: exactly the versions committed
      since the first put under the key was planned, each one a version the ref's listing names and whose transaction
      Lance keeps until a cleanup removes the version. The version is recorded at the renewal, not at the claim, because
      the claim is made before the door opens the table, and a door that lands no version opens none; the renewal
      precedes every put, so a record that names no version proves no put was made, and a takeover of an attempt that
      never reached one searches nothing, as nothing it did can land (its renewal would find the claim another's).
      Model: the record's `wfrom` (one past the version, 0 for none) and `at` (the claim's own time, the version the
      table was at), the search bounded by them, and `ScanRefused`, a refusal between a takeover and its search.
      Configs `CommitPathTakeoverChain.cfg` (KeyKeepsFirst off: the search starts at the lapsed claim's own claim;
      `KeyAppliedOnce` fails, the shortest trace a takeover of a takeover before the first searched, measured also with
      `ScanRefused` removed in a copy, a chain of two takeovers and nothing refused) and
      `CommitPathReleasedUnsearched.cfg` (ReleaseKeepsWritten off: a released key's claim plans without a search). Test
      `test_a_retried_create_does_not_rewrite_the_table.py::TestARetriedAppendLandsOnce::test_a_write_whose_answer_was_lost_past_the_lease_is_found_in_its_commit`,
      its four doors (both inserts, `/create` mode=Overwrite, `/restore`) now crossed with `one-takeover`,
      `a-chain-of-takeovers` and `a-takeover-refused-before-its-search`: on the base the review's chain and release
      answered 7 rows; now 5. With the takeover writing no `written_from` onto its claim (KeyKeepsFirst removed) 8 of the
      12 cases fail, every chain and every refused case; with a released key's record taken as unwritten
      (ReleaseKeepsWritten removed) the 4 refused cases fail.
    - **RecordOwnClaim was pinned by no test** (removing the attempt check from `record_outcome` left every test green;
      only `CommitPathAnswerReplaced.cfg` failed). The interleaving it exists for is a case of the race test now:
      `test_a_keyed_write_lands_once_beside_its_resend.py::test_a_keyed_door_resent_while_its_first_attempt_runs_acts_once[create-exist-ok-before-the-resend-records]`:
      the first attempt is held after its claim; the resend takes the key over, creates the table and is held before it
      records its answer (`_interleave(resend_hold=…)`, `catalog.api.idempotency:Converge.remember`); an append lands;
      the first attempt's ExistOk finds the table and answers version 2, true, and reaches its record while the resend
      still holds the claim. With the rule its record writes nothing and the key answers the resend's version 1; with
      the attempt check removed the first's answer is written over the resend's claim, the resend's own record then
      finds the key answered, and a third send is answered 2: the case fails (1 of 13 on the local root).
    - **The model now has the seal's steps** (ADR 0242 § Found by review, second round): each landed version records
      whether its data files (`ds`) and its transaction file (`ts`) are sealed and recorded; a stamped commit lands with
      both, a stock commit and Lance's own commit with the data files and seal the transaction file in a step after
      landing (`SealLanded`), at which a writer can die; the commit reconciler (`Reconcile`, A4) finishes any landed
      version. Invariant `LandedDataSealed` (safety: no landed version names an unsealed data file) and property
      `EveryFileSealed` (liveness: every file of every landed version is sealed in the end). Configs
      `CommitPathSealAfterLanding.cfg` (guard **SealBeforeLanding** off: data files sealed once landed; `LandedDataSealed`
      fails) and `CommitPathNoReconciler.cfg` (A4 removed: a writer dying after landing leaves its transaction file
      unsealed for good; `EveryFileSealed` fails). The reconciler changes only the seal of a version, which no writer
      reads and a writer's own seal writes again, so it commutes with every writer step and is let run once every writer
      has ended, which keeps the gate's states down without hiding an interleaving.
    - **The gate** then, measured 2026-11-18 on four cores shared with other agents at a load average of 8
      (`scripts/tla_check.sh CommitPath`): seven configs that hold, sixteen that must fail, one on demand, in 89 s; §13
      gives the gate as it stands.

12. **Found by a third review, and FIXED (2026-11-18).** Each was reproduced first against the real catalog where it could
    be, modelled, and its guard removed again after the fix to watch its config and its test fail. The reconciler's finish
    and the seal probe's verdict are ADR 0242 § Found by review, third round (B1, B2); the model's part of B1 is here.
    - **B3, a cleanup or `version/delete` removed the version a key's write landed as, and its resend landed it again**
      (`KeyAppliedOnce`; REPRODUCED on the local root with the earlier search: the first attempt lands at v2 and loses its
      answer, an append lands v3, the owner deletes v2 through `version/delete`, and the resend past the lease takes the
      key over and searches `versions()`, which no longer names v2: an `/insert` to a table declaring no key answered 200
      at v4, the rows landed twice; a keyed `/create` mode=Overwrite answered 200 at v4, overwriting the append; an
      `/insert` to a table declaring a key answered 400 "the table already holds a row with primary key id = 7", for its
      own write). Within a key's 24 h a policy keeping `retain_versions` with no age bound, or the owner tier's
      `version/delete`, can remove any version but the head. **Decision: fail closed.** Guard **SearchWhole**: the
      search reads the versions after `written_from` up to the ref's head from one listing of the ref's `_versions/`, and
      when a version of that window is not listed it cannot prove the write did not land there: the door refuses 409
      InvalidTableState (code 19), naming the versions it could not read, and never applies the write again
      (`dataplane.landed_under_key`, `dataplane._listed_versions`). The refusal is a 4xx after the claim, so the key is
      given back with its `written_from`, and every later send under it searches the same window and is refused the same
      way. A window that is whole behaves as before. Model: `RemoveVersion` (a version other than the head marked gone,
      once a behaviour; it stays in the history, so `KeyAppliedOnce` still counts it), `Scan` reads no gone version, and
      `ScanUnwhole` refuses a window that holds one. Config `CommitPathSearchUnwhole.cfg` (SearchWhole off: the first
      attempt appends and dies unanswered, a compaction lands after it, a cleanup removes the append, the resend searches,
      finds nothing and appends again). Test
      `test_a_retried_create_does_not_rewrite_the_table.py::TestARetriedAppendLandsOnce::test_a_write_whose_answer_was_lost_past_the_lease_is_found_in_its_commit`,
      its four doors crossed with `its-version-deleted-since` as well: each answers 409 code 19 naming the version and
      leaves the table at its version; with the earlier search each landed the write again as above, and with the window
      check removed from the new one each fails (the read of the removed version raises). A version removed between the
      listing and its read is refused the same way: the read's failure (Lance raises OSError "was not found", measured on
      pylance 13.0.0) reads the listing again and judges the window on it (`dataplane._require_whole_window`), where the
      failure answered as the store's; the same test crossed with `its-version-deleted-as-it-is-searched`, which serves
      the search the listing as it stood before the delete, fails on each door without the second listing.
      **Not taken: a pass that keeps every version a live key may have landed as.** A table's pass (ADR 0243) could keep
      the versions newer than the oldest live key's `written_from`; but a key's record is filed by its caller's scope and
      key (`_idempotency/<scope>/<key>.json`), not by table, so a pass would list and read every live key on the estate to
      find the ones naming its table, on every pass of every table; it would keep up to a day of versions past what the
      table's policy declares, so the history git declares would not be the history applied; and `version/delete` is the
      owner's explicit act, which a key held by another caller would silently override. The refusal costs the pass
      nothing and is the floor either way.
    - **B4, the search read every manifest of the ref, whatever `written_from` said.** `landed_under_key` walked
      `dataset.versions()`, which reads each version's manifest. Measured on pylance 13.0.0, a local root of 3,001 versions,
      a window of 3, three samples each at a load average of 3.3: `versions()` 6.89, 6.85, 7.01 s; now one listing of
      `_versions/` (which reads no manifest, a request per thousand objects on S3) and `read_transaction` of each version
      in the window, 5 to 15 ms each, 0.038, 0.033, 0.043 s in all. `LanceDataset.read_transaction(version)` reads that
      version's transaction alone, a version the handle was opened before included (measured: a handle at v1 read v4's),
      so no second reader of the manifest was needed.
    - **The lowest planned version was pinned by nothing, and nothing needs it.** The renewal kept `min(planned_on,
      written_from)`; with "latest planned_on" every takeover, race and replay test stayed green. The model shows it
      cannot matter: with the renewal recording the latest plan (`Renew`: `wfrom = plan + 1`) every gate config holds and
      every config that must fail fails, as with the lowest. The reason: an attempt renews only after its takeover's
      search found none of the key's writes (finding one, it answers instead), and a put of its own it lost did not land
      (`land_keyed` reads the version it tried for its own transaction before it renews again; a stamped finalize is a
      put-if-not-exists of the slot it lost), so no write under the key is at or before what a renewal plans on. The
      `min()` is removed (`idempotency.renew`); the record keeps the latest plan, which narrows the next search's window.
    - **The reconciler can fail and is retried** (ADR 0242 B1). `ReconcileFinish` reads the version after the
      reconciler's mark (`rmark`) and finishes it; `ReconcileFails` (once a behaviour) is a finish the store refused, which
      holds the mark before its version under guard **FinishHoldsMark** and moves it past without. Config
      `CommitPathFinishPassed.cfg` (FinishHoldsMark off: an append's writer dies after landing, the reconciler's first
      finish of it fails, and `EveryFileSealed` fails). `EveryFileSealed` is over retained versions: a removed version
      names no file anyone reads.
    - **The gate** then, measured 2026-11-18 on two of four cores shared with other agents, at a load average of 3 to 4
      (`taskset -c 0,1 scripts/tla_check.sh CommitPath`): seven configs that hold, eighteen that must fail, one on demand,
      in 129 s; the whole `make tla-check`, every model, 54 that hold and 112 that must fail, in 14 min 17 s. `CommitPath.cfg` 243,536 states; `CommitPathKeys.cfg` 89,745; `CommitPathLane.cfg` 367,019
      (`CommitPathLaneWide.cfg`, `ON DEMAND`, 732,046 states in 66 s); `CommitPathNested.cfg` 47;
      `CommitPathOverwrite.cfg` 12,215; `CommitPathRebody.cfg` 3,927; `CommitPathRestore.cfg` 12,115. The removal and the
      failed finish are in every gate config.

13. **Found by a fourth review, and FIXED (2026-11-18).** Each was reproduced first against the real catalog, modelled, and
    its guard removed again after the fix to watch its config and its test fail. The reconciler's part (C1) and the seal
    probe's (C3) are ADR 0242 § Found by review, fourth round; the model's part of C1 is here.
    - **C2, a removed version ABOVE the key's write refused a key whose write was still readable, for good**
      (REPRODUCED against the real catalog on each of the four doors: a keyed write whose answer was lost lands as
      version 2, two unkeyed appends land as 3 and 4, `version/delete` removes 3, and the resend past the lease answered
      409 code 19 though version 2 carries the key's stamp, as did every later send under the key). §12's search refused
      as soon as any version of its window was unlisted, before it read the versions that stand. Guard
      **StampBeforeGap**: the search reads every version of the window the ref's listing names, newest first, a read that
      fails because the version went since the listing (the listing read again no longer names it) counting as one more
      gone; it answers the version carrying the key's stamp wherever a gap stands, and refuses 409 (code 19) only when no
      stamp was found and a version of the window is gone; a read failing for any other reason is the store's, as before
      (`dataplane.landed_under_key`, `dataplane._window_gone`). The refusal says the LATEST write under the key was planned
      on `written_from`, which §12 made the latest plan, where it said the first; and its advice is the door's: an append
      reads the table and sends its rows under a new key if they are missing; a write that replaces the table's rows (an
      overwrite through `/create` or `/insert?mode=overwrite`, a `/restore`) is told that sent again under a new key it
      also replaces every row written since, which "send it if it is missing" did not say and a caller could not tell
      from the table (`replaces`, passed by each door). Model: `Scan` answers a standing stamped version whatever else is
      gone, and `ScanUnwhole` refuses (answer "gone") only where none stands; invariant `GoneIsTrue` (a key refused for a
      gap holds no standing write); mix `gap` (a keyed append sent twice beside a compaction and an unkeyed append, so a
      removed version can stand between the write and the head), gate `CommitPathGap.cfg`, and
      `CommitPathGapRefused.cfg` (StampBeforeGap off: the first attempt appends and dies unanswered, a compaction and an
      append land, a cleanup removes the compaction, and the resend is refused for the gap though its write stands).
      Test `test_a_retried_create_does_not_rewrite_the_table.py::TestARetriedAppendLandsOnce::test_a_write_whose_answer_was_lost_past_the_lease_is_found_in_its_commit`,
      its four doors crossed with `a-later-version-deleted-since`: each answers 200 with the version the write landed as
      and leaves the table as it was; on the base each answered 409 code 19 (4 failed). Both `its-version-deleted…`
      cases still answer 409. Removing each part fails it: refusing before reading (the old first window check) fails
      the 4 `a-later` cases; the second listing, the 4 `as-it-is-searched` cases; the refusal, the 8 `deleted` cases; the
      read of only the listed versions (each unlisted one then read, failed, listed again and named twice) fails the 4
      `its-version-deleted-since` cases on the version the refusal names.
      **A version whose transaction FILE a reclaim removed while its manifest stands: measured, nothing to change.**
      On pylance 13.0.0, a table's `.txn` files deleted under a 2.0 and a 2.2 table, `read_transaction` answered every
      version, the key's stamp included, from the manifest's inline copy, which every manifest `version/create` admits
      and Lance writes under 20 MiB carries (ADR 0242 S2); the search reads it as it reads any version.
    - **C1, the model's part: the reconciler's mark held for ever after a cleanup.** `ReconcileFinish` of a version whose
      predecessor was removed (`PredecessorReclaimed`) is enabled only with guard **FinishFromOwn** (ADR 0242 C1:
      `committed_files.added_by`); liveness `MarkReachesHead` (once failures stop, the mark reaches the head, a cleanup in
      the behaviour) holds in every gate config, and fails in `CommitPathFinishNeedsPredecessor.cfg` (FinishFromOwn off:
      a keyed append lands, a compaction lands after it, a cleanup removes the append, and the compaction's version is
      never finished, its mark held). A version removed itself never holds the mark (`ReconcileFinish` moves past it,
      `ReconcileFails` requires it to stand).
    - **The gate**, measured 2026-11-18 on two of four cores shared with other agents (`TLC_WORKERS=2
      scripts/tla_check.sh CommitPath`): eight configs that hold, twenty that must fail, one on demand, in 3 min 52 s at a load average of 2 to 4 (the reconcile and lost-answer suites running beside it). `CommitPath.cfg` 251,641 states; `CommitPathGap.cfg` 440,234; `CommitPathKeys.cfg` 89,745; `CommitPathLane.cfg` 392,280; `CommitPathNested.cfg` 47; `CommitPathOverwrite.cfg` 12,481; `CommitPathRebody.cfg` 3,927; `CommitPathRestore.cfg` 12,293 (each a little above §12, the "gone" answer now told from a 409 conflict). `GoneIsTrue` and `MarkReachesHead` are in every gate config.

## What a client observes that differs

- A keyed `/insert`, `/create` or `/restore` resent after its first attempt landed unanswered, when a version committed
  since the latest write under the key was planned has been removed (a cleanup, `version/delete`) and no version that
  stands carries the key, answers 409 InvalidTableState (code 19) naming the versions, and writes nothing; every later
  send under the key answers the same (§12). It landed the write a second time, or, on a table declaring a key,
  answered 400 for its own write. The refusal's advice names the door: an append's rows sent under a new key if
  missing; an overwrite or a restore warned that sent again it replaces every row written since (§13).
- The same resend when the version its write landed as still stands answers that version, 200, whatever version since
  was removed (§13), where it answered 409 code 19 for good.
- A takeover's search costs the versions committed since the key's first write, not the table's history (§12).
- A keyed write whose earlier attempts under its key all lost their answers, however many took the key over or were
  refused between, answers the version the first of them landed, never a second landing (§11).
- A takeover of a keyed attempt that never reached a put no longer searches the table's versions: nothing it did can
  have landed.
- A resend under an `Idempotency-Key` with another body or other query parameters answers 400 code 13, where it answered
  the first request's 200, at every keyed door (`/create`, `/insert`, `/restore`, `/register`, `/rename`, `/drop`,
  `/deregister`; each door's OpenAPI description says so).
- A keyed write whose key a resend took over while it ran answers 409 code 14 and lands nothing, at `/insert`,
  `/create` (every mode that lands a version), `/restore` and `/register`; the resend's answer is the key's. A keyed
  refusal (4xx) after its key was taken over answers 409 code 14 instead of the refusal.
- An attempt superseded after its write was answered true (an ExistOk that found the table) answers its own answer and
  no longer replaces the key's: a later send under the key is answered what the attempt holding it answered.
- A takeover of a keyed `/create` or `/restore` from an attempt lost unanswered answers the version that attempt landed,
  as an `/insert` takeover did, rather than landing the write again.
- A takeover of a keyed append to a table declaring a key answers the rows it inserted, not 0.
- An `/insert` to a table whose key is a field inside a struct is refused 400 code 13 when a row repeats a held value.
- A keyed `/insert` to a table declaring no key lands as an unkeyed one does, rebased by Lance onto whatever landed
  since; under contention a writer losing twenty puts answers 409 code 14.

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
is read as having come first). The keyed doors that land no version (§8): their race cases stand for them. Erasure's
release and the history fence (`Erasure.tla`, `Fence.tla`): a data commit does not read the history fence, which holds ref
writes and reclaims. Branches (each ref a history of its own, judged alike). Merge-insert's and update's own shapes beyond
one key value. A store answering a put that landed as a lost race (§9, its test). A stock client's own retry after a 503:
pylance, after a failed attempt, reads the version it staged back and keeps the commit as its own only when that version
holds its transaction (LH-491, `test_a_commit_whose_answer_is_lost_lands_once_and_the_client_gets_its_version_back`), and
otherwise stages the same transaction again on the latest version; the first finalize and the restaged one name one slot
whenever no other writer took it in between, and a slot another writer took is one the first finalize cannot land in
either, so of the two the store lands one. It was not added to the model: it needs a stock actor told 503 and a restage,
which more than doubles the keys gate, and the argument is the slot's put-if-not-exists (A1), which the model already
holds. The shed (`WriteConcurrencyLimitMiddleware`) sits inside the deadline (`lance_app.py`), so a door that runs on
keeps its seat.

## Consequences

- The Rust catalog keeps the renewal before every put of a keyed write, the claim held in the request's context and read
  by the seams that land a version, the request digest in the key's binding, the refusal checked against the claim, an
  answer recorded only over the attempt's own claim, the latest planned version kept on the key's record across every
  takeover and release and searched from by every claim after an unanswered put, the search refused where a version of
  its window is gone and no standing version carries the key, the seal of every data file before its version lands, a
  reconciler finish that fails holding the mark, and a finish past a reclaimed predecessor read off the version's own
  transaction, or changes this model first.
- `make tla-check` runs the eight CommitPath gate configs and twenty that must fail; `make tla-check-full` adds the
  wide lane.
