# 0227. The table lifecycle is modelled in TLA+ before the catalog is ported (2026-11-17)

**Status:** accepted; the second model of the port (ADR 0217 §5, ADR 0221). ADR 0221 left the table lifecycle for "a
model of its own before the catalog is ported". The seven findings of §5 are fixed in the model and in the catalog
(owner, 2026-11-17: "Fix in Python now"). An independent review of those fixes found four more, each fixed model first
(§8); one further race, a stock client's create beside a catalog create, is modelled and left open for a decision (§9).

## Context

A table's life is a set of doors, each a sequence of separate writes to four stores that share no transaction:
`__manifest` (the catalog's names), the bytes at the table's location, the control root (the location's claim, the
trash record, the protection record) and OpenFGA. Concurrent callers interleave those writes, a process can die between
any two, and maintenance's release and purge run at any time. The code argues each ordering in comments and pins a
handful of races in tests; nothing checks the doors against each other.

## Decision

1. **`specs/catalog/TableLifecycle.tla` models the lifecycle of two names and four locations under three callers.** A
   location stands for the table's uuid (a create mints both; a rename and an undrop keep both). Each action is one
   effect, in the code's order, and cites its function:
   - create: `table_create.create_governed_table`, `dataplane.create_table` (the probe of `_existing_location` and
     `holds_a_version`; on a declared name `_fence_the_declaration`), `dataplane.declare_table` (the claim, fenced for
     the create, grants, marker, RegisterTable), `_write_blob_into` (the write and its rollback: DropTable by name, the
     claim's release, `GrantsBeforeWrite.take_back_unless_standing`), `_unreserve`, and the fence's lift;
   - drop: `endpoints/tables.drop_table` and `_drop` (`require_table_unprotected`, `table_claims.fence`, protection read
     again), then `dataplane.destroy_table` (residue record, DeregisterTable by name, delete, clear, release,
     `end_object_records`, `revoke_ownership`) or the trash branch (record, DeregisterTable, lift the fence, clear
     protection);
   - undrop: `endpoints/tables.undrop_table` (admitted trash, standing, `table_claims.fence_kept`, `_register_kept`:
     the record read again, RegisterTable and its `except TableAlreadyExistsError`; `trash.clear`, the fence's lift);
   - rename: `endpoints/tables.rename_table`, `dataplane.rename_table` (protection, `holds_a_version`, destination free,
     `hand_over`, deregister, register, compensation, hand back, lift);
   - protect: `endpoints/tables.set_table_protection` (the record, then `table_claims.require_unfenced`);
   - a namespace cascade's table step: `endpoints/namespaces.drop_namespace`'s listing and
     `_require_descendants_unprotected`, then `_destroy_listed_table` or `_trash_listed_table` (resolve, `fence_listed`,
     protection read again, the drop's effects);
   - the namespace undrop's table step, which is the table undrop's own (`trash_doors.restore_kept_table`), admitted by
     the namespace's record with no standing check;
   - a stock client's create: the declare door (`dataplane.declare_table`, its claim unfenced), its own Lance write on
     vended credentials, and its first commit through `version/create` (put-if-not-exists);
   - maintenance: the deadline, `endpoints/trash_reclaim.release_trash` (`_standing_refusal`), and the purge
     (`purge._purge_one`: the snapshot and last-instant liveness, the vend, the delete, `end_trash`).
   Failures are explicit actions counted against a bound: a RegisterTable that fails, a Lance write that fails, a
   destroy's delete that fails, a rename's register that fails, a purge delete that fails, and a process that dies
   between any two effects. A fence lapses at once once its request has ended, and, in every gate config, while its
   request still runs a step that can outlast `LEASE`: a create's Lance write and a destroy's delete (A5).

2. **What it proves** (`make tla-check`; measured 2026-11-17 with two workers on four cores shared with other work):
   - `TableLifecycle.cfg` (a written table, protected or not; a drop of either kind, against an undrop, a rename or a
     second recoverable drop, against a create of the other name; two failures): 243,416 states, 100 s.
   - `TableLifecycleCreate.cfg` (the name free; a create against a second create of the name, either drop or a rename,
     against a create of the other name; two failures): 167,698 states, 43 s.
   - `TableLifecycleRename.cfg` (a drop of either kind, a rename, and a create of the name the rename frees; two
     failures): 99,365 states, 32 s.
   - `TableLifecycleUndrop.cfg` (a table, protected or not, dropped to the trash; a destroying or recoverable drop or an
     undrop; an undrop, a create of the name or a protect; two failures): 234,695 states, 75 s.
   - `TableLifecycleCascade.cfg` (a table, protected or not, under a namespace a cascade drops, destroying it or to the
     trash; a protect, a rename or the table undrop; nothing, a create of the name or the namespace undrop's table step;
     two failures): 234,291 states, 73 s.
   - `TableLifecycleWide.cfg`, ON DEMAND (three failures, the wider mix it names): 546,845 states, 115 s on four workers.

   Every invariant below holds in every gate config, and so does the liveness property, with fences lapsing under live
   requests as A5 states. The gate configs together run in about five and a half minutes on two workers.

   The invariants, each a claim the code makes:
   - `LiveHasData`: no row names deleted bytes (`destroy_table`'s row-before-bytes order; `purge.check` STILL_REGISTERED).
   - `NameLiveOnce`: a name is never live twice (RegisterTable put-if-not-exists).
   - `OneNamePerLocation`: no location is named by two tables (`location_claims`, `test_concurrent_renames_leave_one_id_per_location.py`).
   - `ProtectedNeverDestroyed`: a table whose protection answered 200 is never deleted (`require_table_unprotected`,
     `force` aside).
   - `PurgeNeverRemovesLive`: the purge deletes no bytes a row names (`test_a_table_recovered_AFTER_the_liveness_snapshot_is_still_refused`).
   - `AnsweredCreateStands`: a create that answered 200 stands with its data until a drop or rename door that resolved
     it acts on it.
   - `OneCreateCommitsPerTable`: of the creates writing one table, one commits; a Create never lands over rows another
     create committed (`CreateMode.CREATE`: a written table is the create's conflict).
   - `RecoverableUntilPurged`: a recoverable drop's record stays until an undrop registers the table again or the purge
     ends it (`_drop`'s "RECORD FIRST, DETACH SECOND").
   - `UndropRestoresExactly`: an undrop that answered 200 restored that uuid at its location under the name, with its
     data and grants (`test_an_undrop_restores_the_trashed_table_its_caller_names.py`).
   - `EverythingComesToRest` (liveness): once failures stop, every request ends, every fence is lifted or lapses, and
     every trash record is ended or carries a noted refusal. What else a run leaves (bytes no row names, a row over no
     bytes, grants on no table, a table with none) is one of reconcile's categories.

3. **An assumption the design rests on is a config that must fail** (ADR 0221 §5). Each holds, as a control, with its
   assumption restored, in the gate configs above.
   - A1, `TableLifecycleNoNameCAS.cfg`: RegisterTable adds a name unconditionally; two creates both win (`NameLiveOnce`).
   - A2, `TableLifecycleNoFence.cfg`: a drop acts without its fence and a rename hands over a fenced claim; the rename
     deregisters by name a table created under the name since (`AnsweredCreateStands`).
   - A3, `TableLifecycleNoSettle.cfg`: the release lands while an undrop admitted before the deadline still runs; the
     purge deletes the bytes the undrop registers (`LiveHasData`). Held by `trash.RELEASE_SETTLE`, a timing argument.
   - A5, `TableLifecycleCommitOutlastsLease.cfg`: a step that renewed its fence ends within `LEASE` of the renewal; here
     the create's commit, one manifest write after its renewal (§8). Without it, the fence lapses inside the commit, a
     second create fences, probes and commits, and the first's commit lands over it (`OneCreateCommitsPerTable`). Every
     other fence a request holds lapses in the gate configs whenever its holder sits in a step that can outlast
     `LEASE`; this config is the one place the lapse lands between a renewal and the effect it was made for. The A5 of
     the first version of this ADR, "no request outlives its `LEASE`", was not held by anything (`RequestDeadlineMiddleware`
     answers 503 at 300 s and lets the door run on) and is gone.
   - Each guard of §5 and §8 is one more: the model's constant `Off` names the guard a config removes, and each such
     config must fail.

   A4, that Lance's create of a dataset commits only where none is, does not hold, and is modelled as Lance does it:
   a create refuses a location already holding a dataset before it writes a row (`lance.fragment.write_fragments` with
   `mode="create"`, as `write_dataset` does), and its commit (`LanceDataset.commit` of the transaction the write
   returns) is not conditional on that check, so a create whose check passed before another committed lands as the next
   version (`CWrite`, `CCommit`, `CPut`; measured on pylance 13.0.0: a create's transaction committed onto a dataset
   written since lands as version 2). The create's fence (§5), renewed before its commit (§8), is what keeps a second
   create from writing there.

4. **The model was shown to bite.** Seven defects were planted one at a time in a copy, and the gate reported each:
   the trash branch detaching before it records (`RecoverableUntilPurged`, 6 steps), a destroy deleting the bytes
   before the row (`LiveHasData`, 7), a drop skipping its protection read (`ProtectedNeverDestroyed`, 8), the purge and
   the release skipping every liveness and standing check (`LiveHasData`, 13), an undrop clearing its record before it
   registers (`RecoverableUntilPurged`, 13), `end_trash` leaving the record (`EverythingComesToRest`, 30), and a rename
   handing over without its ETag or another request's fence (`LiveHasData` on `TableLifecycleRename.cfg`).

5. **Seven findings, each confirmed against the running catalog on 2026-11-17 and each FIXED.** Each was shown by
   `tests/integration/test_a_lifecycle_door_keeps_what_it_answered_beside_a_concurrent_door.py` (the real catalog app on
   a real pylance `dir` namespace, one request held at the step the trace names while the other door runs whole, the
   claim judged by what a client then reads over HTTP), and each test, strict-xfail while the defect stood, now passes.
   The fix is a guard: a step of the design the model now holds, and a config that removes it and must fail. Every
   guard is a conditional write on the object store (the claim's put-if-not-exists or its ETag) or a read ordered after
   one; nothing new is stored and no process holds a lock another replica would not see.
   - **CreateRace** (`AnsweredCreateStands`) and **CreateBothAnswered** (`OneCreateCommitsPerTable`): a second create of
     a name only declared by a first wrote at the first's location, as though the declaration were its own retry; its
     rollback, a DropTable by name, dropped the first's table, or its commit landed over the first's rows and both
     answered 200. Guard `CreateFence`: a create holds its table's claim fenced from its claim until its data is
     committed (`location_claims.take(fenced=True)` through `table_claims.claim_new_table(fence_for=…)`, from
     `dataplane.declare_table`; lifted in `dataplane.create_table`'s `finally`). A create into a table an earlier request
     declared fences that claim first (`table_claims.fence_declared`, under the ETag it reads) and probes again under the
     fence (`dataplane._fence_the_declaration`): the name still describes that location, which holds no version. The
     losing create answers 409 ConcurrentModification (14) while the first writes, and 409 TableAlreadyExists (5) once
     it has written; nothing is rolled back. A declaration at rest (a bare declare, or a create whose process died and
     whose fence lapsed after `LEASE`) is written into as before. Configs `TableLifecycleCreateRace.cfg` and
     `TableLifecycleCreateBothAnswered.cfg` remove it. Tests `test_a_create_answered_200_stands_when_a_second_create_of_its_name_fails`,
     `test_of_two_creates_of_one_name_only_the_one_whose_rows_the_table_holds_answers_200`.
   - **SoftDropWrongRow** (`RecoverableUntilPurged`): a recoverable drop fenced a table still being created, whose
     rollback then removed its row; a rename's compensation put another table under the name, and the drop's
     DeregisterTable by name detached that one, which no record kept. Guard `CreateFenceHoldsDoors`: the create's fence is
     a fence every door respects (`location_claims.fence`, `hand_over`), so a drop of a table still being written
     answers 409 ConcurrentModification (14) at its fence and removes nothing. Config
     `TableLifecycleSoftDropWrongRow.cfg` lets drops ignore a create's fence. Test
     `test_a_recoverable_drop_answered_200_leaves_every_table_it_detached_restorable`, whose drop now answers 409 where
     the trace needed 200; its harm assertion is unchanged.
   - **UndropOverDestroyed** (`LiveHasData`): an undrop's take of the claim the drop kept wrote nothing and held
     nothing, so a destroying drop deleted the bytes between it and the undrop's register, which then answered 200 over
     them. Guard `UndropFence`: the undrop fences that claim before it registers (`table_claims.fence_kept`: held by the
     name with the record's uuid, under the ETag it reads), and reads its trash record again under the fence
     (`tables._register_kept`: still there, the `dropped_at` it admitted, no residue), since a destroying drop that
     fenced first and died leaves its residue record over it. A destroying drop beside it answers 409
     ConcurrentModification (14) at its fence; the undrop answers 409 ConcurrentModification (14) when the claim is
     fenced or its record changed, and 409 InvalidTableState (19) when the claim is released or another's. Config
     `TableLifecycleUndropOverDestroyed.cfg`. Test `test_an_undrop_answered_200_never_names_destroyed_bytes`.
   - **UndropClearsRedrop** (`RecoverableUntilPurged`): a recoverable drop of the restored table between the undrop's
     register and its unconditional `trash.clear` filed its record under the same uuid, and the clear deleted it. Guard
     `UndropHoldsFence`: the undrop lifts its fence only after the clear (`tables.undrop_table`, `finally`), so the record
     it clears is the one it admitted: no drop files another while the fence stands, and the redrop answers 409
     ConcurrentModification (14). The clear stays a plain delete: the estate requires no conditional delete of its store
     (`location_claims`' module note), and the fence makes the record's writers wait. Config
     `TableLifecycleUndropClearsRedrop.cfg` lifts the fence before the clear. Test
     `test_a_recoverable_drop_answered_200_can_be_undropped`.
   - **UndropRace** (`UndropRestoresExactly`): the undrop read any TableAlreadyExists from its register as "already
     recovered", cleared the record of a table a create had just put a new table's name over, and answered 200. Guard
     `UndropOwnConflict`: TableAlreadyExists is the undrop's own success only while the name describes the record's uuid
     at its location (`tables._register_kept`, through `table_claims.resolve`); otherwise it answers 409
     TableAlreadyExists (5) and the record stays for an undrop once the name is free. Config
     `TableLifecycleUndropRace.cfg`. Test `test_an_undrop_answered_200_restores_the_dropped_table_under_its_name`.
   - **ProtectRace** (`ProtectedNeverDestroyed`): protection set after a destroying drop read it answered 200, and the
     drop destroyed the table. Two guards, each a write followed by a read of the other's write, so of a drop and a
     protect one sees the other: `DropRereadsProtection`, the drop reads protection again once it holds its fence
     (`tables._drop`), and `ProtectChecksFence`, the protection door writes its record and then answers 409 unless the
     name still describes the location under the claim its resolution read and no request fences it
     (`table_claims.require_unfenced`). The losing protect answers 409 ConcurrentModification (14) and leaves its record,
     which protects; a drop that reads it answers 409 InvalidTableState (19). Configs `TableLifecycleProtectRace.cfg`
     and `TableLifecycleProtectUnchecked.cfg`, one per guard. Test
     `test_a_table_whose_protection_answered_200_is_not_destroyed_by_a_drop_without_force`.

   The model showed two more steps each fix needs, which the findings' configs did not: a create probing again under its
   fence must find the name still at the declared location (a dead create's rollback may have removed it), and an
   undrop must read its record again under its fence (`TableLifecycleCreate.cfg` and `TableLifecycleUndrop.cfg` with
   two failures each found one).

6. **The four facts.** GitOps: no step is imperative or live-only; every fence is a field of a record the doors already
   write, and a fence its request never lifted lapses after `LEASE` with nothing to clean up. Cloud native: the fences
   are conditional writes on the object store every replica shares, so two replicas arbitrate as two threads do; no
   in-process lock and no new store. Lance-namespace idiomatic: the doors keep the spec's operations and codes (409
   ConcurrentModification, TableAlreadyExists and InvalidTableState, `lance_docs/ns_catalog/spec.yaml` ErrorCode), and
   the arbitration is the object store's put-if-not-exists and If-Match, the primitives Lance's own manifest commit
   stands on (`lance_docs/ns_catalog/catalog/dir/index.md` "Manifest Table Commits"). Zero trust: no request is trusted
   by position; each acts only on the claim it fenced under the ETag it read, and a door that cannot prove its claim
   answers 409 rather than acting.

7. **Designs chosen.** The create's mark is a fence every door respects (`CreateFenceHoldsDoors`), not a token only
   creates read: a token would leave a drop free to fence a table still being written, and SoftDropWrongRow's trace
   stands. A recoverable drop comparing the row it deregistered with its fenced location, as `destroy_table` does, was
   not taken: DeregisterTable removes by name, so the comparison comes after the wrong row is gone, and putting it back
   is a register that can itself lose the name; under the create's fence no door removes a fenced table's row, which
   the gate proves. For protection, the protect door writing a fence of its own was not taken: it would change the
   claim's ETag and answer every data door in flight 409; the write-then-read pair touches only the protection record.
   A create's rollback still drops by name: under its fence the name's row is its own, which the gate proves.

8. **Four problems an independent review of §5's fixes found, each FIXED model first (2026-11-17).** The review drove
   the seven guards through the table doors on the local root and on RustFS 1.0.3 and found them holding there, then
   found the doors the fixes did not reach and the guards no test removed. Each fix below is a guard in
   `TableLifecycle.tla`, with a config that removes it and must fail, a test in
   `tests/integration/test_a_lifecycle_door_keeps_what_it_answered_beside_a_concurrent_door.py` that was red before the
   fix (or, for a guard already in the code, red with the guard removed), and the RustFS lane (§10).
   - **B1, CascadeProtectRace** (`ProtectedNeverDestroyed`). A namespace cascade read every table's protection when it
     listed the subtree (`_require_descendants_unprotected`), before it fenced any; a protect landing after that read
     answered 200, since no request fenced the table, and the cascade then fenced the table and destroyed it. Guard
     `CascadeRereadsProtection`: each table's protection is read again once the cascade holds that table's fence
     (`endpoints/namespaces._destroy_listed_table` and `_trash_listed_table`, through the door's `unprotected`, which is
     `fga_deps.require_table_unprotected` with the door's `force`). A table found protected stops the cascade with the
     table drop's answer, 409 InvalidTableState (19), its fence lifted: what the cascade removed before it stays removed
     and announced, nothing of it or after it changed, and the request is re-issuable. Config
     `TableLifecycleCascadeProtectRace.cfg`. Test
     `test_a_table_whose_protection_answered_200_is_not_destroyed_without_force[cascade-before-its-fence]`.
   - **B2 and B3, NsUndropRace and NsUndropClearsRedrop** (`UndropRestoresExactly`, `RecoverableUntilPurged`). The
     namespace undrop's table step was the table undrop as it stood before §5: an unfenced `table_claims.take`, every
     TableAlreadyExists read as its own registration, the record never read again, `trash.clear` with no fence. So a
     create of the name landing while it registered was read as its own success and the trashed table's record cleared
     (B2), and a recoverable drop of the restored table landing before its clear had its record cleared (B3). Fix: one
     implementation, `catalog.services.trash_doors.restore_kept_table`, called by both doors (`endpoints/tables.undrop_table`
     and `endpoints/namespaces.undrop_namespace`): `table_claims.fence_kept`, `_register_kept` (the record read again,
     a TableAlreadyExists its own only while the name holds the record's uuid at its location), the door's
     announcement, `trash.clear`, then the fence lifted. The namespace undrop holds its namespace's fence for an add
     the whole while (ADR 0233 G1); the table step's register takes its parent's for an add too, which an add shares, so
     the two compose. A namespace undrop meeting a table another request acts on answers 409 ConcurrentModification
     (14), one whose name another table took 409 TableAlreadyExists (5), one whose location is no longer kept 409
     InvalidTableState (19); each keeps the table's record and every namespace record, so the undrop is re-issued once
     the conflict is gone (ADR 0233 G3). Configs `TableLifecycleNsUndropRace.cfg` here, and in `Cascade.tla`,
     `CascadeUndropClearsRedrop.cfg`, now the config that removes the fence (G5, ADR 0233), its property in the cascade
     gate (`Cascade.cfg` runs the table undrop between the cascade's attempts). Tests
     `test_an_undrop_answered_200_restores_the_dropped_table_under_its_name[namespace]` and
     `test_a_recoverable_drop_answered_200_can_be_undropped[namespace]`.
   - **B4, CreateLapse** (`OneCreateCommitsPerTable`). A fence lapses `LEASE` (15 min) after it is written and the
     create held its fence across its whole Lance write; the model's `Lapse` fired only for an ended request (the old
     A5). A create still writing 15 minutes after its fence lost it to a second create, which fenced, probed the
     declaration empty and committed; the first then committed over it, both answered 200, and its `lift_fence` did
     nothing. Reproduced by driving `LEASE` to zero while the first create was held inside its write: both answered
     200 and the table held the first's rows. Guard `CreateRenewsFence`: the create writes its rows
     (`lance.fragment.write_fragments`) and then, right before the commit (`LanceDataset.commit`), renews its fence
     (`table_claims.renew_fence`, `lakehouse.location_claims.renew_fence`: one write under the ETag it reads,
     conditional on the fence being its own token). A fence another request holds by then answers 409
     ConcurrentModification (14, `table_claims.FenceLostError`) and nothing is committed; the create's rollback renews
     the same way first, and a lost fence rolls nothing back (`dataplane._write_blob_into`, `_still_fenced`), since a
     DropTable by name would drop what the other request writes and a release would free its claim. The rows the loser
     wrote are files no version names, which Lance's cleanup of old versions deletes once they pass its age for
     unverified files (`lance_docs/lance/guide/read_and_write.md`). Config `TableLifecycleCreateLapse.cfg`; the assumption
     the renewal leaves, that the commit ends within `LEASE` of it, is A5 (§3). Test
     `test_of_two_creates_of_one_name_only_the_one_whose_rows_the_table_holds_answers_200[fence-lapsed-in-the-write]`.
     The rule, and why: a fence is renewed, conditional on its own token, right before every effect its holder cannot
     take back once it may have outlived `LEASE`, and a refused renewal stops the holder with nothing more done. A
     heartbeat renewing the fence while the write runs (what `lakehouse.fences.Fence.held` does) was not taken for the
     claim: it is a timing argument the store does not arbitrate (a stalled process renews nothing, and its next effect
     lands anyway), while the renewal before the commit is a conditional write the store does arbitrate, and leaves only
     the single manifest write after it to A5. A create that outlives its lease beside no other request still commits:
     no one wrote its claim, so its renewal lands. The renewal before the rollback also closes §5's SoftDropWrongRow on
     its own (a drop that fenced a table still being created leaves the create a fence that is not its own, so the
     create removes no row): `TableLifecycleSoftDropWrongRow.cfg` now removes both `CreateFenceHoldsDoors` and
     `CreateRenewsFence`, and with `CreateFenceHoldsDoors` alone removed `TableLifecycleCreate.cfg` and
     `TableLifecycleRename.cfg` hold (205,418 and 113,289 states). Both guards stay in the code: the fence every door
     respects keeps a drop from acting on a table still being written at all.
     The other fence holders, each judged by the same rule: a drop holds its fence over short steps and its long one,
     the destroy's delete, comes after its deregister, where nothing can act on the table (the name is gone, the
     residue record refuses an undrop); the model lapses its fence there (`LongSteps`) and every invariant holds. An
     undrop, a rename and a protect are a few backend and control-root calls each. A cascade fences each table only for
     that table's step, as a drop does, and holds its namespace's fence (ADR 0233 G1), the warehouse delete its
     warehouse's, and an erasure's release its table's history fence for longer: those three are `lakehouse.fences.Fence`
     holds, renewed every third of their hold while their work runs, and lapse only after two renewals in a row fail.
     A lost hold there is logged at ERROR (`<name>_lost`) and the work goes on; whether it should stop instead is not
     decided here (§10, Not modelled).
   - **The two guards no test removed.** `UndropFence`'s re-read of the trash record under the fence had no test going
     red without it, and neither had `DropRereadsProtection`: the protection test held the drop after its fence, where
     only the protect door's check (`ProtectChecksFence`) decides. Each existing test is parametrized at the seam
     instead of a test beside it: `test_an_undrop_answered_200_never_names_destroyed_bytes[its-fence]` holds the undrop
     before its fence while a destroying drop fences, files its residue record over the trash record, deletes the bytes
     and dies, its fence lapsing; `test_a_table_whose_protection_answered_200_is_not_destroyed_without_force[drop-before-its-fence]`
     holds the drop between its first protection read and its fence. Each fails with its guard removed and passes with it.

9. **A finding left open: StockCreateRace** (`OneCreateCommitsPerTable`, `TableLifecycleStockCreateRace.cfg`, a
   `FINDING` config that fails as expected; test
   `tests/integration/test_a_stock_create_and_a_catalog_create_of_one_name_never_both_land.py`, strict xfail). A stock
   client creates a table by `DeclareTable`, whose claim takes no fence, a Lance write on the credential the catalog
   vends, and its first commit through `version/create`, which finalizes version 1 put-if-not-exists (ADR 0157). A
   catalog create of the name meanwhile finds the declaration empty, fences it (`fence_declared` admits an unfenced
   claim) and writes; the client's version 1 lands after the create's probe, and the create's commit, not conditional on
   its check (A4), lands over the client's rows as version 2. Both answer success (measured through the real catalog
   and stock pylance 13.0.0: the client's declare held before it answers, the create held right before its commit; both
   succeed and the table holds the create's rows). What the code has closes only part of it: `version/create` acts
   through `DoorTable`, which refuses a commit when the claim's ETag changed since the door resolved the table, so a
   client resolved before the create's fence answers 409 (measured); one resolved after it reads the fenced claim as its
   resolution and finalizes, since nothing there reads the fence, and the create's renewal (§8) finds its own fence.
   Two designs close it, each meeting the four facts, and the choice is the owner's:
   - **(a) A declaration is its declarer's.** `DeclareTable` takes the claim fenced for a token it answers to no one,
     so a catalog create of a declared name answers 409 ConcurrentModification until the fence lapses after `LEASE` or
     the client's first `version/create` lifts it. Cost: a bare declare that is never written wedges its name for one
     `LEASE`, and a create retrying a crashed declare waits as long; a stock client's version/create that outlives the
     lease meets the same lapse §8 handles, with a renewal in `version/create`.
   - **(b) version/create respects a create's fence, and a create commits only onto an empty location.**
     `version/create` answers 409 for a version 1 while another request fences the claim (`DoorTable.require_held`
     reading the fence), which closes every order but one: the client's check passing before the create's fence and its
     put landing after the create's probe. Closing that one needs the create's commit conditional on no version
     existing; measured on pylance 13.0.0, `LanceDataset.commit(..., max_retries=0)` of a create's transaction onto a
     dataset written since is refused, but with an error naming a missing `0.manifest`, not a conflict, a behaviour
     Lance does not document. Cost: without that, a timing window between `version/create`'s check and its put; with
     it, a dependence on undocumented Lance behaviour.
   (a) is the GitOps- and zero-trust-neater answer (a declaration names who may write it, as a fence does everywhere
   else); (b) keeps a declare free for anyone to write into. Not chosen here.

10. **A real store.** The race tests of this ADR and ADR 0233 run on the local root's file lock in the offline suite, and
    on a real S3 store's conditional writes in the lane `make lifecycle-rustfs-check`
    (`scripts/lifecycle_on_rustfs_check.sh`): the pinned RustFS (1.0.3) as a process, the catalog's root and control root
    in a bucket of each test's own (`tests/integration/conftest.py::race_ns_client`, its `s3` parameter collected only
    where the lane names the store).

## Not modelled

The namespaces' own rows (`Cascade.tla` models them), the deregister and register doors, Overwrite and ExistOk creates,
idempotency keys, the announcements, base records, data directories and clone endings, `force`, a lease's clock beyond
the lapse (a claim held under another name refuses here, where the code lets a gone holder's be taken past `LEASE`), a
deregister whose answer was lost (`_still_described`), a purge refusal retried on a later tick, and what a holder of a
`lakehouse.fences.Fence` (a namespace, a warehouse, a table's history) does once its renewals fail.

## Consequences

- Every door that acts on a table now holds the claim fenced while it acts, the create and the undrop included, so two
  doors acting on one table at once answer one of them 409 code 14, which a client retries after describing the table.
  A drop, rename or undrop of a table still being created answers 409; so does a protect while another request fences
  the claim.
- The settle (A3) is a timing argument, not a write the store arbitrates: a request that outlives
  `RELEASE_SETTLE` breaks it. A request that outlives `LEASE` loses its fence and, at its next effect it cannot take
  back, answers 409 code 14 with nothing more done (§8); only the one write after a renewal is assumed to fit (A5).
- What a client sees change with §8: a namespace cascade answers 409 code 19 when a table under it was protected after
  the cascade listed it, having removed what came before that table; a namespace undrop answers 409 (code 14, 5 or 19)
  where the table undrop would, keeping the records of what it has not restored; a create whose write outlived its
  fence's lease answers 409 code 14 when another request acted on the table meanwhile, and commits nothing.
- A guard removed from the code is a config that fails: the catalog's port follows the model's steps, guards included.

## The four facts, for §8

- **GitOps:** no new record or object; a renewal rewrites the fence the claim already carries, and a lapsed one still
  ends on its own.
- **Cloud native:** every exclusion added is a conditional write on the object store (the renewal under the claim's
  ETag, the fences the shared undrop step takes); no in-process lock, no new store; the RustFS lane checks them on a real
  store's conditional writes.
- **Lance-namespace/table-idiomatic:** the create writes and commits through Lance's own split (`write_fragments`, then
  `LanceDataset.commit` of its transaction, `lance_docs/lance/guide/distributed_write.md`), and every refusal is the
  spec's (ConcurrentModification 14, TableAlreadyExists 5, InvalidTableState 19).
- **Zero trust:** a door acts on a table only while the store confirms its fence is its own; one that cannot prove it,
  a create whose lease ran out or a cascade meeting a protection set after its listing, answers 409 instead of acting.
