# 0233. Erasure and the namespace and warehouse cascade are modelled in TLA+; eight defects found (2026-11-17)

**Status:** accepted; the catalog's third and fourth models (ADR 0221, ADR 0227, ADR 0229). Both describe the Python
catalog as it runs today, before it is ported (ADR 0217 §5).

## Context

An erasure is an obligation the catalog verifies, not a rewrite it runs (ADR 0163). Between the obligation and the
moment the subject's bytes leave storage there are four actors that share no transaction: the bring-your-own job, the
verification door, the release tick (ADR 0138) and maintenance's cleanup. The code argues their order in comments.

A namespace cascade and a warehouse delete are the catalog's own loops over a subtree, because the `dir` backend drops a
namespace Restrict whatever `behavior` says. Nothing fences the subtree as a whole, and each loop is documented as
finished by its re-issue. ADR 0227 left "namespaces and cascades" unmodelled.

## Decision

1. **`specs/catalog/Erasure.tla` models one obligation from its record to the cleanup after its release.** Main has a
   version a read answers the subject from (tagged `t0`), one holding it behind a deletion vector (tagged `t1`, the
   shape of `test_erasure_releases_a_pinning_branch_after_its_notice.py`), the job's rewrite and a restore's commit.
   Branch B is cut from the second; another caller may cut C from B or from main. Each action is one effect, in the
   code's order: the job's rewrite of each head; the verification's head judgement, tag drop, reclaim (its reading of
   the refs, then its deletes), verify (`erasure.verify`), the tag announcements, the release's record and its
   notices (`_schedule_release`), and `erasure_obligations.settle`; the release tick's plan and claim, deletes,
   "recreating", recreates and finish (`erasure_release._release`); maintenance's cleanup; a recoverable drop and its
   undrop; and the obligation that follows this one on a derived table. Any verification and any tick may die between
   two effects.

2. **`specs/catalog/Cascade.tla` models warehouse W, namespace P in it, C in P, table T in C, and table N that
   another caller creates (or renames in) in P.** The doors: P's cascade drop, recoverable (`_trash_subtree`) or
   destroying (`_destroy_subtree`); the create of N; the namespace undrop of P (`undrop_namespace`); the table door's
   undrop of T; the purge; and the warehouse delete with `?cascade=true` (`delete_warehouse`). Each may fail or die
   between two effects and is issued again, as each door documents.

3. **What the gate proves** (`make tla-check`, four workers):
   - `Erasure.cfg` (one verification, branch B, a derived table, a branch cut from B, a drop and undrop, two
     failures): 25,020 states.
   - `ErasureConcurrent.cfg` (two verifications at once, no failure): 55,743 states.
   - `ErasureWide.cfg`, ON DEMAND (two verifications, one failure, a branch cut from B): 381,972 states, two minutes.
   - `Cascade.cfg` (the recoverable cascade beside a create, the namespace undrop and the purge, no failure): 166 states.
   - `CascadeFaults.cfg` (the same with up to two failures and a warehouse delete, held to `NoOrphan` and
     `LiveHasData`): 1,120,299 states, about 10 s.
   - `CascadeTableFaults.cfg` (the recoverable cascade ending at any table step, twice): 824 states.
   - `CascadeDestroy.cfg` (the destroying cascade beside a create and a warehouse delete, two failures): 14,067 states.

   The erasure invariants and properties, each a claim the code makes:
   - `PinsNameRetainedVersions`: no ref names a deleted version (the history fence).
   - `NothingDeletedComesBack`: a deleted version never returns, through an undrop or anything else.
   - `ReleasedOnEvidence`: an obligation is released only when every head was clean, every residual was held by a ref
     and by no head, and every derived table was followed (`erasure.verify`'s `settled`).
   - `ReleasedStaysReleased`: a released obligation is never written open again.
   - `ADerivedTableIsFollowed`: an obligation is released only once one of its own follows it on each derived table.
   - `OnlyTagsOverTheSubjectAreDropped`: a tag is dropped only once every head is clean, and only over a version a read
     answers the subject from.
   - `NoticeOnlyForARecordedRelease`: nobody is told of a release the catalog did not record.
   - `EveryReleasedBranchComesBack`: the release recreates every branch it deleted.
   - `ErasedOnceReleased` (liveness): once released, the release and cleanup leave the subject in no version and no
     branch head.
   - `EveryObligationIsReleased` and `EveryReleaseFinishes` (liveness), whatever request dies.

   The cascade invariants and properties:
   - `NoOrphan`: no row outlives its parent: a table its namespace, a namespace its parent, the warehouse's row its
     record.
   - `LiveHasData`: a registered table's data is there.
   - `NothingIsLost`: whenever no request is in flight, every table's data is registered, restorable (its record
     stands, its warehouse is routed to, and every namespace above it stands or has a record), deleted, or detached by
     the warehouse delete on purpose, its bytes kept for a register.
   - `NoTrashOutlivesItsWarehouse`: the warehouse delete ends its trash before its record goes.
   - `TheCascadeFinishes`, `TheUndropFinishes` (liveness): a cascade or an undrop cut short is finished by its re-issue.

4. **Each assumption the design rests on is a config that must fail** (ADR 0221 §5):
   - Erasure A1, `ErasureNoReread.cfg`: the release reads the branches again when it runs (`erasure_release._plan`,
     lines 244-252). Without it, a branch cut from a pinning branch in the grace makes Lance refuse the parent's
     delete on every tick, and the release never finishes (`EveryReleaseFinishes`).
   - Erasure A2, `ErasureNoPlanFirst.cfg`: the release records where each branch comes back before it deletes one
     (`_claim` 278-279, before `_delete`). Without it, a tick that dies after a delete loses the branch.
   - Erasure A3, `ErasureNoFence.cfg`: a reclaim and a ref write never overlap (`history_fences.TableHistory`, held at
     `erasure.py` 584, `dataplane.create_tag` 3978, `create_branch` 4111). Without it, a tag names a deleted version.
   - Erasure A4, `ErasureNoCAS.cfg`: the obligation's record changes only under its ETag, and a released one stays
     released (`erasure_obligations._settling` 298-299, `records.mutate_json`). Without it, a slower verification's
     open write lands over a release.
   - Erasure A5, not a config: maintenance's cleanup runs on a registered table, and a table in the trash is undropped
     in the end (both fair in the model). A table left in the trash for good is never cleaned.
   - Cascade A2, `CascadeNoRestrict.cfg`: `drop_namespace` refuses a namespace holding a row (spec.yaml
     `DropNamespaceRequest.behavior`; measured on the `dir` backend, `namespaces.py` 615-620). Without it, a child
     created after the cascade's listing outlives its parent.
   - Cascade A3, `CascadeNoPurgeCheck.cfg`: the purge refuses a table registered again (`purge.check`
     STILL_REGISTERED). Without it, a cascade that died between a record and its detach loses the table's bytes.
   - Cascade A1, that a child's add commits only while its parent's row stands, does NOT hold (finding 1 below). The
     gates keep it, as the behaviour a fix gives, and `CascadeParentNotChecked.cfg` is the backend as measured.

5. **The models were shown to bite.** Each defect was planted alone in a copy and reported by a gate config:
   - Erasure: tags dropped while a head still holds the subject (`OnlyTagsOverTheSubjectAreDropped`, 4 states); a
     settle that ignores the derived table (`ADerivedTableIsFollowed`, 13); a release that finishes without recreating
     its branches (`EveryReleasedBranchComesBack`, 17); a cleanup that ignores pins (`PinsNameRetainedVersions`, 2);
     a settle that releases whatever the report says (`ADerivedTableIsFollowed`, 12).
   - Cascade: the recoverable cascade detaching a table before its record (`NothingIsLost` on `CascadeTableFaults`, 5);
     a namespace drop that ignores Restrict (`NoOrphan`, 8); the namespace undrop registering a table whose namespace
     is not back (`NoOrphan` on `CascadeFaults`, 14); a destroy that deletes the bytes and keeps the row
     (`LiveHasData` on `CascadeDestroy`, 3); a warehouse delete that drops its record before its cascade (`NoOrphan` on
     `CascadeFaults`, 5); an undrop that clears a namespace's record without rebuilding it (`NothingIsLost`, 12).

6. **Eight findings, each a config whose first line reads `FINDING` and `EXPECT VIOLATION`, seven CONFIRMED against
   the running catalog on 2026-11-17.** Each confirmed one is shown by an integration test that drives the real app on
   real pylance, makes the step the trace names fail once (or lands the other door at it), and reads the harm through
   the doors. Each test is `xfail(strict=True, raises=AssertionError)`, so the suite stays green while the defect
   stands and turns red the day it is fixed. The fixes are the owner's to schedule. In the order of their harm:
   1. **`CascadeParentNotChecked.cfg`** (`NoOrphan`, 9 states): CONFIRMED,
      `tests/integration/test_a_cascade_keeps_what_it_answered.py::test_a_namespace_created_beside_its_parents_drop_never_stands_without_it`.
      A create of `p2$n` and a Restrict drop of `p2`, sent to the backend together, both answer 200, and `p2$n` stands
      under no `p2` (5 of 5 runs; 50 of 50 bare races of a namespace and 30 of 30 of a declared table on the installed
      pylance). The `dir` backend checks a parent and a Restrict drop's emptiness each against the `__manifest` it read,
      and `lance_docs/ns_catalog/catalog/dir/index.md` "Manifest Table Commits" promises conflicts only for one entry.
      Smallest fix: the catalog serialises a namespace's drop with the adds beneath it (a fence on the parent's
      record, taken by the drop and checked by every create, register and rename into it), or the backend checks the
      parent row inside the add's commit.
   2. **`CascadeNamespaceRecordAfterDrop.cfg`** (`NothingIsLost`, 6 states) and **`CascadeRootRecordAfterDrop.cfg`**
      (liveness, 11): CONFIRMED,
      `test_a_cascade_keeps_what_it_answered.py::test_a_cascade_finished_by_its_reissue_leaves_every_table_it_detached_restorable`.
      `_trash_subtree` drops each namespace (`namespaces.py` 534) before it files that namespace's record (546), where
      a table's record is filed before its detach (594-595). The record of `p$c` fails after the drop; the re-issue
      lists only `p` and answers 200; the undrop of `p` answers 404 "parent namespace 'p$c'" (it has rebuilt `p` and
      cleared its record by then), and the undrops of `p$c` and of the table answer 404. The table's bytes stay until
      the purge takes them. At `p` itself the re-issue answers 404 and the cascade can never finish. Smallest fix:
      file the namespace's record before its drop, as the table's is.
   3. **`CascadeWarehouseLateTrash.cfg`** (`NoTrashOutlivesItsWarehouse`, 13 states): CONFIRMED,
      `tests/integration/test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py::test_a_warehouse_deleted_beside_a_recoverable_drop_leaves_none_of_its_trash`.
      `delete_warehouse` reads the warehouse's trash once (`warehouses.py` 1077) and ends what it read (1118); a
      recoverable cascade of a namespace in it landing after the read files records for the namespace and its table;
      the delete finds both gone, tolerates it (1139, 1149) and deletes the warehouse's record (1171), answering 200.
      Both records stand for a warehouse no door routes to. Smallest fix: read the trash again after the cascade, and
      end what it finds, before the record goes.
   4. **`ErasureCopyTag.cfg`** (liveness, 21 states) and **`ErasureCutBranch.cfg`** (22): CONFIRMED for the tag,
      `services/catalog/tests/test_an_erasure_keeps_what_it_answered.py::test_a_released_erasure_whose_release_ran_leaves_the_subject_on_no_ref`.
      The obligation is released on `pinned_by: [tag:trained]` over main 2, which holds the subject behind a deletion
      vector; in the grace another caller tags main 2 `copy` through the tag door (200); the release deletes `trained`
      (`erasure_release._plan` takes the tags it names and those on its subtree's branches, 253-258) and finishes. A
      new obligation's verification then reports `residual_versions: [main@2]`, pinned by `tag:copy`, and
      `complete: false`, with the first obligation released and its release run. The branch config is the same with a
      branch cut from main 2 (the release re-reads only the subtree of the branches it names); it was not run against
      the app. Smallest fix: the release reads every ref standing on a residual version again when it runs, as it
      reads the subtree, or the ref doors refuse a ref on a version a scheduled release names.
   5. **`ErasureNoticeLost.cfg`** (liveness, 29 states): CONFIRMED,
      `test_an_erasure_keeps_what_it_answered.py::test_everyone_a_release_costs_is_told_before_it_runs`. The
      verification writes the release's record (`erasure.py` endpoint 360) and fails before its notices (371-381); the
      next verification meets the record (361-363), takes it as one whose people were told, releases the obligation,
      and nobody receives `erasure_release_scheduled`. Smallest fix: file the notices with the record (an outbox, as
      `announcement_intents` does), or send them again from the record until it notes they were sent.
   6. **`ErasureTagEventLost.cfg`** (liveness, 25 states): CONFIRMED,
      `test_an_erasure_keeps_what_it_answered.py::test_every_tag_an_erasure_drops_is_announced`. The verification drops
      the tag (`erasure.py` 514), and the door fails reading the ref for its announcement (endpoint 233); the next
      verification finds the tag gone and announces nothing. `table_tag_deleted` is never sent. Smallest fix: record
      the dropped tags on the obligation before the drop and announce from that record.
   7. **`CascadeUndropNotResumable.cfg`** (liveness, 14 states): CONFIRMED,
      `test_a_cascade_keeps_what_it_answered.py::test_an_undrop_cut_short_is_finished_by_its_reissue`.
      `undrop_namespace` clears each namespace's record right after rebuilding it (`namespaces.py` 1086), before it
      registers the tables (1106); a register that fails leaves the table records, and the re-issue answers 404 (1042-1044)
      where the docstring says a rerun finishes the job. The table door's undrop still restores each table one by one.
      Smallest fix: clear the namespace records after the tables are registered, root last.
   8. **`CascadeUndropClearsRedrop.cfg`** (`NothingIsLost`, 15 states): ADR 0227's confirmed `UndropClearsRedrop`,
      reached through a cascade's re-issue: the table door's undrop of T lands between two attempts of the cascade, and
      its unconditional `trash.clear` deletes the record the re-issue filed. Not tested again here.

   `ErasureRestore.cfg` (liveness, 23 states) is a limit, not a finding: a restore of a residual version commits its
   bytes as main's head, which no cleanup reaches. `erasure_release.completion_bound` states in words that restoring a
   retained pre-erasure version brings the rows back; the model shows it also leaves them past the release, with the
   obligation released.

## The four facts

- **GitOps:** nothing here is declared in git; the obligations, releases and trash are records on the control root.
  Findings 2 and 3 leave records that no reconcile of git ends.
- **Cloud native:** each loop is resumable by re-issue, the cloud-native answer to a dying pod; findings 2, 5, 6 and 7
  are steps whose order makes that re-issue unable to finish.
- **Lance-namespace/table-idiomatic:** the erasure rests on Lance's own refs and cleanup, and finding 1 is the namespace
  spec's `dir` catalog promising less than the catalog relies on.
- **Zero trust:** finding 4 lets any caller holding a tag write keep an erased subject for good, which the release
  exists to prevent ("an erasure that can be blocked is not an erasure", `erasure_release.py`).

## Not modelled

The predicate and its digest; index segments (one more residual kind a head must clear); external bases and MemWAL
shards; the retention window; a registered clone's tag; late derived tables (a ruling: listed and paged); grants and
their revoke; protection and `force`; claims and fences beyond `fence_listed`'s skip (TableLifecycle.tla); the
announcements of a cascade; projects; `?purge_bucket`.

## Consequences

- Each FINDING config turns into a plain gate config once its fix lands in the model and the code, and its test loses
  its xfail marker in the same change. The catalog's port follows these models' steps.
- The cascade gates assume A1, which the installed backend does not give; until finding 1 is fixed they say what the
  doors do once it is.
- `make tla-check` runs the four models. Measured on four cores, the gate configs added here take about 40 s, and with the
  configs that must fail about 70 s, most of it one JVM start per config.
