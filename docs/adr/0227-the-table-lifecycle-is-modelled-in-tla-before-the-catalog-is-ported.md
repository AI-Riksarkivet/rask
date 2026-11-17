# 0227. The table lifecycle is modelled in TLA+ before the catalog is ported (2026-11-17)

**Status:** accepted; the second model of the port (ADR 0217 §5, ADR 0221). ADR 0221 left the table lifecycle for "a
model of its own before the catalog is ported".

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
     `holds_a_version`, the declared path's `table_claims.take`), `dataplane.declare_table` (claim, grants, marker,
     RegisterTable), `_write_blob_into` (the write and its rollback: DropTable by name, the claim's release,
     `GrantsBeforeWrite.take_back_unless_standing`), `_unreserve`;
   - drop: `endpoints/tables.drop_table` and `_drop` (`require_table_unprotected`, `table_claims.fence`), then
     `dataplane.destroy_table` (residue record, DeregisterTable by name, delete, clear, release, `end_object_records`,
     `revoke_ownership`) or the trash branch (record, DeregisterTable, lift the fence, clear protection);
   - undrop: `endpoints/tables.undrop_table` (admitted trash, standing, `table_claims.take`, RegisterTable, its
     `except TableAlreadyExistsError`, `trash.clear`);
   - rename: `endpoints/tables.rename_table`, `dataplane.rename_table` (protection, `holds_a_version`, destination free,
     `hand_over`, deregister, register, compensation, hand back, lift);
   - protect: `endpoints/tables.set_table_protection`;
   - maintenance: the deadline, `endpoints/trash_reclaim.release_trash` (`_standing_refusal`), and the purge
     (`purge._purge_one`: the snapshot and last-instant liveness, the vend, the delete, `end_trash`).
   Failures are explicit actions counted against a bound: a RegisterTable that fails, a Lance write that fails, a
   destroy's delete that fails, a rename's register that fails, a purge delete that fails, and a process that dies
   between any two effects.

2. **What it proves** (`make tla-check`, four workers):
   - `TableLifecycle.cfg` (a written table, protected or not; a drop of either kind, against an undrop, a rename or a
     second recoverable drop, against a create of the other name; two failures): 129,431 states, every invariant below
     and the liveness property, about 22 s.
   - `TableLifecycleCreate.cfg` (the name free; a create against either drop or a rename, against a create of the other
     name; two failures): 27,771 states, the same, about 21 s.
   - `TableLifecycleRename.cfg` (a destroying drop, a rename, and a create of the name the rename frees; two failures):
     26,655 states, the same, about 5 s.
   - `TableLifecycleWide.cfg`, ON DEMAND (three failures, the wider mix it names): 279,034 states, the same, about 80 s.

   Each gate config leaves out the mixes the findings below need, and says so in its header. The whole of
   `make tla-check`, lineage's model included, takes about 48 s.

   The invariants, each a claim the code makes:
   - `LiveHasData`: no row names deleted bytes (`destroy_table`'s row-before-bytes order; `purge.check` STILL_REGISTERED).
   - `NameLiveOnce`: a name is never live twice (RegisterTable put-if-not-exists).
   - `OneNamePerLocation`: no location is named by two tables (`location_claims`, `test_concurrent_renames_leave_one_id_per_location.py`).
   - `ProtectedNeverDestroyed`: a protected table's data is never deleted (`require_table_unprotected`, `force` aside).
   - `PurgeNeverRemovesLive`: the purge deletes no bytes a row names (`test_a_table_recovered_AFTER_the_liveness_snapshot_is_still_refused`).
   - `AnsweredCreateStands`: a create that answered 200 stands with its data until a drop or rename door that resolved
     it acts on it.
   - `RecoverableUntilPurged`: a recoverable drop's record stays until an undrop registers the table again or the purge
     ends it (`_drop`'s "RECORD FIRST, DETACH SECOND").
   - `UndropRestoresExactly`: an undrop that answered 200 restored that uuid at its location under the name, with its
     data and grants (`test_an_undrop_restores_the_trashed_table_its_caller_names.py`).
   - `EverythingComesToRest` (liveness): once failures stop, every request ends, every fence is lifted or lapses, and
     every trash record is ended or carries a noted refusal. What else a run leaves (bytes no row names, a row over no
     bytes, grants on no table, a table with none) is one of reconcile's categories.

3. **An assumption the design rests on is a config that must fail** (ADR 0221 §5):
   - A1, `TableLifecycleNoNameCAS.cfg`: RegisterTable adds a name unconditionally; two creates both win (`NameLiveOnce`).
   - A2, `TableLifecycleNoFence.cfg`: a drop acts without its fence and a rename hands over a fenced claim; the rename
     deregisters by name a table created under the name since (`AnsweredCreateStands`).
   - A3, `TableLifecycleNoSettle.cfg`: the release lands while an undrop admitted before the deadline still runs; the
     purge deletes the bytes the undrop registers (`LiveHasData`). Held by `trash.RELEASE_SETTLE`, a timing argument.

   Each holds, as a control, with its assumption restored (292, 1,374 and 230 states).

4. **The model was shown to bite.** Seven defects were planted one at a time in a copy, and the gate reported each:
   the trash branch detaching before it records (`RecoverableUntilPurged`, 6 steps), a destroy deleting the bytes
   before the row (`LiveHasData`, 7), a drop skipping its protection read (`ProtectedNeverDestroyed`, 8), the purge and
   the release skipping every liveness and standing check (`LiveHasData`, 13), an undrop clearing its record before it
   registers (`RecoverableUntilPurged`, 13), `end_trash` leaving the record (`EverythingComesToRest`, 30), and a rename
   handing over without its ETag or another request's fence (`LiveHasData` on `TableLifecycleRename.cfg`, which was
   added because the first two gate configs missed it).

5. **Six findings, each kept as a config whose first line reads `FINDING` and `EXPECT VIOLATION`:**
   - `TableLifecycleCreateRace.cfg` (`AnsweredCreateStands`, 14 steps, no failure): two creates of one name. The second
     probes while the first is only declared (`dataplane.create_table` 788-789), takes the declared path
     (832-836), and both write `mode="create"` at the first's location. Lance commits one; the other raises "Dataset
     already exists" (489) and its rollback calls the backend's DropTable **by name** (1024), which removes the row and
     deletes the directory (`lance-dir.md` § DropTable, V2), and releases the claim (1033). The create that answered
     200 is gone, bytes and row, with its grants left on no table.
   - `TableLifecycleUndropRace.cfg` (`UndropRestoresExactly`, 21 steps, no failure): an undrop checks the name free
     (`tables.py` 1546), a create registers the name, the undrop's RegisterTable answers TableAlreadyExists (1573-1574),
     which the door reads as "already recovered", clears the trash record (1599) and answers 200. The dropped table is
     restored nowhere, and with its record gone no undrop or purge reaches it.
   - `TableLifecycleUndropClearsRedrop.cfg` (`RecoverableUntilPurged`, 19 steps, no failure): between the undrop's
     register and its clear, a recoverable drop of the restored table files its record under the same uuid and
     deregisters; the undrop's `trash.clear`, an unconditional delete (`record_store.delete_record`), deletes it.
   - `TableLifecycleUndropOverDestroyed.cfg` (`LiveHasData`, one failure): a recoverable drop dies between its record
     and its deregister; an undrop admitted on the record takes the claim, which writes nothing and fences nothing; a
     destroying drop fences, deletes and deregisters; the undrop registers the name over the deleted bytes.
   - `TableLifecycleSoftDropWrongRow.cfg` (`RecoverableUntilPurged`, 23 steps, two failures): the trash branch of
     `_drop` deregisters by name (`tables.py` 891) and never compares the row it removed with the location it fenced, as
     `destroy_table` does. A rename detaches the table and its register fails; a create of the freed name registers,
     its write fails and its rollback removes its row; the drop resolves and fences the created table and files its
     record; the rename's compensation registers the original table under the name again; the drop then detaches the
     original table, which no record keeps.
   - `TableLifecycleProtectRace.cfg` (`ProtectedNeverDestroyed`, 11 steps): protection set after a destroying drop
     read it (`tables.py` 799) and before its delete answers 200, and the drop deletes the table anyway. The protection
     door takes no fence and the drop reads the record once.

   The three undrop findings share one cause: the undrop holds nothing across its register and its clear. Its take of a
   claim its name holds writes nothing, its register's conflict is read as its own success, and its clear deletes
   whatever record the uuid has by then.

## Not modelled

Namespaces and cascades, the deregister and register doors, Overwrite and ExistOk creates, idempotency keys, the
announcements, base records, data directories and clone endings, `force`, a lease's clock (a claim held under another
name refuses here, where the code lets a gone holder's be taken past `LEASE`), a deregister whose answer was lost
(`_still_described`), and a purge refusal retried on a later tick.

## Consequences

- The findings are the catalog's to fix before it is ported, and each config turns into a plain gate config once its
  fix lands in the model and the code. The catalog's port follows this model's steps.
- The settle (A3) is a timing argument, not a write the store arbitrates: a request that outlives
  `RELEASE_SETTLE` breaks it.
- `make tla-check` runs both lineage's and the catalog's models; the catalog's configs add about 40 s.
