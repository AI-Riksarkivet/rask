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
     second recoverable drop, against a create of the other name; two failures): 135,635 states, every invariant below
     and the liveness property, about 22 s.
   - `TableLifecycleCreate.cfg` (the name free; a create against either drop or a rename, against a create of the other
     name; two failures): 30,905 states, the same, about 21 s.
   - `TableLifecycleRename.cfg` (a destroying drop, a rename, and a create of the name the rename frees; two failures):
     28,192 states, the same, about 5 s.
   - `TableLifecycleWide.cfg`, ON DEMAND (three failures, the wider mix it names): 290,265 states, the same, about 60 s.

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
   - `OneCreateCommitsPerTable`: of the creates writing one table, one commits; a Create never lands over rows another
     create committed (`CreateMode.CREATE`: a written table is the create's conflict).
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

   Each holds, as a control, with its assumption restored (376, 1,515 and 230 states).

   A4, that Lance's create of a dataset commits only where none is, does not hold, and is modelled as Lance does it:
   `write_dataset(mode="create")` refuses a location already holding a dataset before it writes a row, and its commit is
   not conditional on that check, so a create whose check passed before another committed lands as the next version
   (`CWrite`, then `CCommit`; measured on pylance 13 through the catalog, finding 2 below).

4. **The model was shown to bite.** Seven defects were planted one at a time in a copy, and the gate reported each:
   the trash branch detaching before it records (`RecoverableUntilPurged`, 6 steps), a destroy deleting the bytes
   before the row (`LiveHasData`, 7), a drop skipping its protection read (`ProtectedNeverDestroyed`, 8), the purge and
   the release skipping every liveness and standing check (`LiveHasData`, 13), an undrop clearing its record before it
   registers (`RecoverableUntilPurged`, 13), `end_trash` leaving the record (`EverythingComesToRest`, 30), and a rename
   handing over without its ETag or another request's fence (`LiveHasData` on `TableLifecycleRename.cfg`, which was
   added because the first two gate configs missed it).

5. **Seven findings, each kept as a config whose first line reads `FINDING` and `EXPECT VIOLATION`, and each
   CONFIRMED against the running catalog on 2026-11-17.** A finding of the model is a defect of the catalog once the
   same steps are shown against the real app. Each was shown by
   `tests/integration/test_a_lifecycle_door_keeps_what_it_answered_beside_a_concurrent_door.py`: the real catalog app
   on a real pylance `dir` namespace, one request held at the step the trace names (a backend call through a wrapper of
   the namespace, the Lance write, `trash.clear`) while the other door runs whole, and the claim judged by what a client
   then reads over HTTP. A failure the trace needs is the backend or Lance raising at that step. Each test is marked
   `xfail(strict=True)`, so the suite stays green while the defect stands and turns red the day it is fixed. Every step
   of each trace maps to the code cited below, and none was refuted; the fixes are the owner's to schedule. In the
   order of their harm:
   - `TableLifecycleCreateRace.cfg` (`AnsweredCreateStands`, a trace of 15 states, no failure): CONFIRMED,
     `test_a_create_answered_200_stands_when_a_second_create_of_its_name_fails`. Two creates of one name. The second
     probes while the first is only declared (`dataplane.create_table` 788-789), takes the declared path (832-836;
     `table_claims.take` reads the first's claim, held under the same name, as its own retry, `location_claims._taken`
     273-274), and writes `mode="create"` at the first's location once the first has committed there. Lance refuses it,
     "Dataset already exists" (489), and its rollback calls the backend's DropTable **by name** (1024), which removes the
     first's row and deletes its directory, and releases the claim (1033). The first create answered 200 and its table
     describes 404 after.
   - `TableLifecycleCreateBothAnswered.cfg` (`OneCreateCommitsPerTable`, 14 states, no failure): CONFIRMED,
     `test_of_two_creates_of_one_name_only_the_one_whose_rows_the_table_holds_answers_200`. The same two creates, the
     second passing Lance's check for a dataset before the first commits (held inside `write_dataset` after its first
     row): its commit is not conditional on that check and lands as version 2 over the first's rows, and both answer 200.
     The table holds the second's rows; the first's are only in version 1. This is the observation that two concurrent
     creates of one name with identical bodies both answered 200 in 2 of 12 pairs: no idempotency rule on equal content
     exists (`api/v1/endpoints/data.py` 185 keys only on an `Idempotency-Key` the caller sends), it is this race, and
     with different bodies it hands one caller a 200 for rows the table does not hold. A Create that overwrites also
     skipped the `can_drop` and protection checks an Overwrite asks (`table_create.py` 233-238).
   - `TableLifecycleUndropOverDestroyed.cfg` (`LiveHasData`, 20 states, one failure): CONFIRMED,
     `test_an_undrop_answered_200_never_names_destroyed_bytes`. A recoverable drop whose deregister fails after its
     record (`tables.py` 890-891; a death there leaves the same state once its fence lapses) leaves the record on the
     live table; an undrop admitted on it finds the name standing under its own uuid (1546-1547) and takes the claim,
     which writes nothing and fences nothing (1566; `location_claims.take` 333-334); a destroying drop fences, files its
     residue record over the same uuid, deregisters and deletes the bytes (`dataplane.destroy_table`); the undrop's
     RegisterTable (1573) then finds the name free and answers 200. The name describes, and its rows cannot be read.
   - `TableLifecycleSoftDropWrongRow.cfg` (`RecoverableUntilPurged`, 23 states, two failures): CONFIRMED,
     `test_a_recoverable_drop_answered_200_leaves_every_table_it_detached_restorable`. The trash branch of `_drop`
     deregisters by name (`tables.py` 891) and never compares the row it removed with the location it fenced, as
     `destroy_table` does. A rename detaches the table and its register fails (`dataplane.rename_table` 1253); a create of
     the freed name registers, its write fails and its rollback removes its row (1024); the drop has resolved and fenced
     the created table and filed its record; the rename's compensation registers the original table under the name
     again (1261); the drop then detaches the original table and answers 200. Neither name describes it, the only trash
     record names the created table's uuid, and the undrop answers 500.
   - `TableLifecycleUndropRace.cfg` (`UndropRestoresExactly`, 21 states, no failure): CONFIRMED,
     `test_an_undrop_answered_200_restores_the_dropped_table_under_its_name`. The undrop checks the name free (1546), a
     create registers the name, the undrop's RegisterTable answers TableAlreadyExists (1573-1574), which the door reads
     as "already recovered", clears the trash record (1599) and answers 200. The name holds the created table; the
     dropped one is restored nowhere, and a second undrop answers 404.
   - `TableLifecycleUndropClearsRedrop.cfg` (`RecoverableUntilPurged`, 20 states, no failure): CONFIRMED,
     `test_a_recoverable_drop_answered_200_can_be_undropped`. Between the undrop's register and its clear, a recoverable
     drop of the restored table files its record under the same uuid and deregisters, answering 200; the undrop's
     `trash.clear` (1599), an unconditional delete (`record_store.delete_record`), deletes that record. The table is in
     no name and the next undrop answers 404.
   - `TableLifecycleProtectRace.cfg` (`ProtectedNeverDestroyed`, 11 states): CONFIRMED,
     `test_a_table_whose_protection_answered_200_is_not_destroyed_by_a_drop_without_force`. Protection set after a
     destroying drop read it (`tables.py` 799) and before its deregister answers 200 (1633, no fence, no claim read), and
     the drop, which reads the record once, deletes the table anyway.

   The earlier trial of the first finding (28 pairs of concurrent creates of one name, 20 MB each, through
   `scripts/port_catalog_fixture.py`) did not reach the modelled path. Its 409 ConcurrentModificationError is the
   answer of two creates that both probe the name free and race the backend's RegisterTable at two minted locations
   ("Object ... was concurrently created by another"): the loser's `_unreserve` touches only its own location, and the
   winner stands. Reproduced deterministically by holding both creates at `declare_table`: 6 of 6 pairs one 200 and one
   409 with that detail. The modelled path needs the second create's describe to land after the first registers and
   before it writes, a window a free-running pair rarely hits; the both-200 pairs were the second finding's.

   The three undrop findings share one cause: the undrop holds nothing across its register and its clear. Its take of a
   claim its name holds writes nothing, its register's conflict is read as its own success, and its clear deletes
   whatever record the uuid has by then. The two create findings share another: a create of a name declared by another
   request writes into that request's table, as though it were its own retry.

## Not modelled

Namespaces and cascades, the deregister and register doors, Overwrite and ExistOk creates, idempotency keys, the
announcements, base records, data directories and clone endings, `force`, a lease's clock (a claim held under another
name refuses here, where the code lets a gone holder's be taken past `LEASE`), a deregister whose answer was lost
(`_still_described`), and a purge refusal retried on a later tick.

## Consequences

- A finding shown against the running catalog is the catalog's to fix before it is ported; one that cannot be shown
  is a place where the model is corrected. All seven were shown (§5). Each config turns into a plain gate config once
  its fix lands in the model and the code, and its test loses its xfail marker in the same change. The catalog's port
  follows this model's steps.
- The settle (A3) is a timing argument, not a write the store arbitrates: a request that outlives
  `RELEASE_SETTLE` breaks it.
- `make tla-check` runs both lineage's and the catalog's models; the catalog's configs add about 40 s.
