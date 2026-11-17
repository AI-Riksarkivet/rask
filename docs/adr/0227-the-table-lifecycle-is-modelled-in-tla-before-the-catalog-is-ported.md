# 0227. The table lifecycle is modelled in TLA+ before the catalog is ported (2026-11-17)

**Status:** accepted; the second model of the port (ADR 0217 §5, ADR 0221). ADR 0221 left the table lifecycle for "a
model of its own before the catalog is ported". The seven findings of §5 are fixed in the model and in the catalog
(owner, 2026-11-17: "Fix in Python now").

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
   - maintenance: the deadline, `endpoints/trash_reclaim.release_trash` (`_standing_refusal`), and the purge
     (`purge._purge_one`: the snapshot and last-instant liveness, the vend, the delete, `end_trash`).
   Failures are explicit actions counted against a bound: a RegisterTable that fails, a Lance write that fails, a
   destroy's delete that fails, a rename's register that fails, a purge delete that fails, and a process that dies
   between any two effects.

2. **What it proves** (`make tla-check`, four workers on four cores; each time as measured with the host near idle, then under a load average of 7 to 8 from other work):
   - `TableLifecycle.cfg` (a written table, protected or not; a drop of either kind, against an undrop, a rename or a
     second recoverable drop, against a create of the other name; two failures): 169,744 states, 22 s / 46 s.
   - `TableLifecycleCreate.cfg` (the name free; a create against a second create of the name, either drop or a rename,
     against a create of the other name; two failures): 66,181 states, 8 s / 14 s.
   - `TableLifecycleRename.cfg` (a drop of either kind, a rename, and a create of the name the rename frees; two
     failures): 59,585 states, 7 s / 9 s.
   - `TableLifecycleUndrop.cfg` (a table, protected or not, dropped to the trash; a destroying or recoverable drop or an
     undrop; an undrop, a create of the name or a protect; two failures): 160,843 states, 16 s / 24 s.
   - `TableLifecycleWide.cfg`, ON DEMAND (three failures, the wider mix it names): 369,203 states, 138 s under load.

   Every invariant below holds in every gate config, and so does the liveness property. The gate configs together run in
   about 53 s, 93 s under load; the whole of `make tla-check`, the lineage, cascade, erasure and work-queue models
   included, in 246 s under load.

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
   - Each guard of §5 is one more: the model's constant `Off` names the guard a config removes, and seven configs, one
     per guard, must fail (§5).

   A4, that Lance's create of a dataset commits only where none is, does not hold, and is modelled as Lance does it:
   `write_dataset(mode="create")` refuses a location already holding a dataset before it writes a row, and its commit is
   not conditional on that check, so a create whose check passed before another committed lands as the next version
   (`CWrite`, then `CCommit`; measured on pylance 13 through the catalog). The create's fence (§5) is what keeps a second
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

## Not modelled

Namespaces and cascades, the deregister and register doors, Overwrite and ExistOk creates, idempotency keys, the
announcements, base records, data directories and clone endings, `force`, a lease's clock (a claim held under another
name refuses here, where the code lets a gone holder's be taken past `LEASE`), a deregister whose answer was lost
(`_still_described`), and a purge refusal retried on a later tick. The namespace undrop's table step
(`endpoints/namespaces.py`) still takes the kept claim without a fence and reads every TableAlreadyExists as its own
registration, the shape UndropRace and UndropOverDestroyed had; it belongs to the cascade model (ADR 0221), which models
the namespace's records and not the table step, and is not changed here. That model's `CascadeUndropClearsRedrop.cfg`
(ADR 0233) reaches UndropClearsRedrop through a cascade's re-issue; it holds no fences, so it still shows the trace,
and whether the undrop's fence closes that path too (the re-issue's `fence_listed` refused while the undrop holds it)
is that model's to show.

## Consequences

- Every door that acts on a table now holds the claim fenced while it acts, the create and the undrop included, so two
  doors acting on one table at once answer one of them 409 code 14, which a client retries after describing the table.
  A drop, rename or undrop of a table still being created answers 409; so does a protect while another request fences
  the claim.
- The settle (A3) is a timing argument, not a write the store arbitrates: a request that outlives
  `RELEASE_SETTLE` breaks it, as one that outlives `LEASE` loses its fence (A5).
- A guard removed from the code is a config that fails: the catalog's port follows the model's steps, guards included.
