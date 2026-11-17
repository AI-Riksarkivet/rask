# 0233. Erasure and the namespace and warehouse cascade are modelled in TLA+; eight defects found (2026-11-17)

**Status:** accepted; the catalog's third and fourth models (ADR 0221, ADR 0227, ADR 0229). Both describe the Python
catalog as it runs, before it is ported (ADR 0217 §5). The eight findings are fixed, model first (§6); the eighth by ADR 0227 §8, and a ninth, found by review in the cascade's table step, with it.

## Context

An erasure is an obligation the catalog verifies, not a rewrite it runs (ADR 0163). Between the obligation and the
moment the subject's bytes leave storage there are four actors that share no transaction: the bring-your-own job, the
verification door, the release tick (ADR 0138) and maintenance's cleanup. The code argues their order in comments.

A namespace cascade and a warehouse delete are the catalog's own loops over a subtree, because the `dir` backend drops a
namespace Restrict whatever `behavior` says. Each loop is documented as finished by its re-issue. ADR 0227 left
"namespaces and cascades" unmodelled.

## Decision

1. **`specs/catalog/Erasure.tla` models one obligation from its record to the cleanup after its release.** Main has a
   version a read answers the subject from (tagged `t0`), one holding it behind a deletion vector (tagged `t1`, the
   shape of `test_erasure_releases_a_pinning_branch_after_its_notice.py`), the job's rewrite and a restore's commit.
   Branch B is cut from the second; another caller may cut C from B or from main, and tag any retained version. Each
   action is one effect, in the code's order: the job's rewrite of each head; the verification's head judgement, the
   filing of the tags it drops, the drops, reclaim (its reading of the refs, then its deletes), verify
   (`erasure.verify`), the tag announcements, the filing of the release's notices, the release's record and the
   notices (`_schedule_release`), and `erasure_obligations.settle`; the release tick's plan and claim, deletes,
   "recreating", recreates, reclaim and finish (`erasure_release._release`); the finish of what an ended verification
   filed; maintenance's cleanup; a recoverable drop and its undrop; and the obligation that follows this one on a
   derived table. Any verification and any tick may die between two effects.

2. **`specs/catalog/Cascade.tla` models warehouse W, namespace P in it, C in P, table T in C, and table N that
   another caller creates (or renames in) in P.** The doors: P's cascade drop, recoverable (`_trash_subtree`) or
   destroying (`_destroy_subtree`); the create of N; the namespace undrop of P (`undrop_namespace`); the table door's
   undrop of T; the purge; and the warehouse delete with `?cascade=true` (`delete_warehouse`). Each may fail or die
   between two effects and is issued again, as each door documents. Each door is fair on its own, strongly, since a
   fence's refusal makes one door's next step wait on another's.

3. **What the gate proves** (`make tla-check`, four workers):
   - `Erasure.cfg` (one verification, branch B, a derived table, a branch cut from B, a drop and undrop, two
     failures): 28,935 states, about 15 s.
   - `ErasureConcurrent.cfg` (two verifications at once, no failure): 88,773 states, about 30 s.
   - `ErasureRefsInTheGrace.cfg` (a tag and a branch another caller makes on any retained version, one failure):
     47,797 states, about 10 s.
   - `ErasureWide.cfg`, ON DEMAND (two verifications, one failure, a branch cut from B): 620,114 states, about six
     minutes.
   - `Cascade.cfg` (the recoverable cascade beside a create on the backend as measured, the namespace undrop and the
     purge, no failure): 192 states.
   - `CascadeFaults.cfg` (the same with up to two failures and a warehouse delete, held to every invariant):
     507,750 states, about 10 s.
   - `CascadeTableFaults.cfg` (the recoverable cascade ending at any table or namespace step and the undrop at any
     step, twice between them): 4,281 states.
   - `CascadeDestroy.cfg` (the destroying cascade beside a create and a warehouse delete, two failures): 13,221 states.

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
     branch head, whatever ref another caller made.
   - `EveryObligationIsReleased`, `EveryReleaseFinishes`, `EveryoneIsTold` and `EveryDroppedTagIsAnnounced`
     (liveness), whatever request dies.

   The cascade invariants and properties:
   - `NoOrphan`: no row outlives its parent: a table its namespace, a namespace its parent, the warehouse's row its
     record.
   - `LiveHasData`: a registered table's data is there.
   - `NothingIsLost`: whenever no request is in flight, every table's data is registered, restorable (its record
     stands, its warehouse is routed to, and every namespace above it stands or has a record), deleted, or left by the
     warehouse delete on purpose, its bytes kept for a register.
   - `NoTrashOutlivesItsWarehouse`: the warehouse delete ends its trash before its record goes.
   - `TheCascadeFinishes`, `TheUndropFinishes` (liveness): a cascade or an undrop cut short is finished by its re-issue.

4. **Each assumption the design rests on is a config that must fail** (ADR 0221 §5):
   - Erasure A1, `ErasureNoReread.cfg`: the release reads the branches again when it runs (`erasure_release._plan`).
     Without it, a branch cut from a pinning branch in the grace makes Lance refuse the parent's delete on every tick,
     and the release never finishes (`EveryReleaseFinishes`). G1 is removed with it: here every branch cut from a
     pinning one stands on a residual, which G1 takes too.
   - Erasure A2, `ErasureNoPlanFirst.cfg`: the release records where each branch comes back before it deletes one
     (`_claim`, before `_delete`). Without it, a tick that dies after a delete loses the branch.
   - Erasure A3, `ErasureNoFence.cfg`: a reclaim and a ref write never overlap (`history_fences.TableHistory`). Without
     it, a tag names a deleted version.
   - Erasure A4, `ErasureNoCAS.cfg`: the obligation's record changes only under its ETag, and a released one stays
     released (`erasure_obligations._settling`, `records.mutate_json`). Without it, a slower verification's open write
     lands over a release.
   - Erasure A5, not a config: maintenance's cleanup runs on a registered table, and a table in the trash is undropped
     in the end (both fair in the model).
   - Cascade A1 is a fact, not an assumption: the `dir` backend checks a new row's parent, and a Restrict drop's
     emptiness, each against the `__manifest` it read (`lance_docs/ns_catalog/catalog/dir/index.md` "Manifest Table
     Commits" promises conflicts only for one entry; on pylance 13.0.0 an add sent beside its parent's drop and the drop
     both land, 50 of 50 races of a namespace and 30 of 30 of a declared table). Sent one after the other from two
     connections, each reads the other's commit and refuses (measured on pylance 13.0.0). Every gate runs on the
     backend as measured (`ParentAtomic = FALSE`); G1 is what holds the two apart.
   - Cascade A2, `CascadeNoRestrict.cfg`: `drop_namespace` refuses a namespace holding a row (spec.yaml
     `DropNamespaceRequest.behavior`). Without it, and without G1, which also holds a create off a namespace while its
     cascade runs, a child created after the cascade's listing outlives its parent.
   - Cascade A3, `CascadeNoPurgeCheck.cfg`: the purge refuses a table registered again (`purge.check`
     STILL_REGISTERED). Without it, a cascade that died between a record and its detach loses the table's bytes.

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
   These plants were run against the models before the fixes; they were not run again.

6. **Eight findings, and a ninth found by review; all FIXED.** The seven confirmed against the running catalog on 2026-11-17 first. Each fix is a guard in
   the model first; the config that found the finding now removes that guard and must fail, and the property it
   violated holds in a gate. Each fix's test drives the real app on real pylance, makes the step the trace names fail
   once (or lands the other door at it), reads the harm through the doors, and passes, its `xfail` marker gone. In the
   order of their harm:
   1. **A child created beside its parent's Restrict drop stood under no parent. FIXED, guard G1.** An add beneath a
      namespace and that namespace's drop never run together. `lakehouse.namespace_fences.NamespaceFence` keeps one
      record per namespace name on the control root; holders of one kind share it, an add and a drop refuse each other
      409 code 14 (`NamespaceFencedError`, the spec's ConcurrentModification). Every request's backend connection takes
      it (`catalog.core.fenced_namespace.FencedNamespace`, which `NamespaceDep` hands out through
      `api/dependencies.get_fenced_namespace`): each `create_namespace`, `create_table`, `create_empty_table`,
      `declare_table`, `register_table` and `rename_table` holds the fence of the namespace it adds into for an add,
      each `drop_namespace` its namespace's for a drop. The namespace drop door holds its namespace's fence for a drop
      over its whole run (`endpoints/namespaces.drop_namespace`), the namespace undrop for an add
      (`undrop_namespace`), so a create beside a cascade, and an undrop beside one, answer 409. Proved needed by
      `CascadeParentNotChecked.cfg` (`NoOrphan`, 9 states); tested by
      `tests/integration/test_a_cascade_keeps_what_it_answered.py::test_a_namespace_created_beside_its_parents_drop_never_stands_without_it`.
   2. **A recoverable cascade dropped a namespace before its trash record. FIXED, guard G2.** `_trash_subtree` files
      each namespace's record before its drop, as it files a table's before its detach. A cascade cut short between the
      two leaves a record on a namespace that still stands, which the re-issue files again and drops; the undrop of
      the cascade's namespace then rebuilds every namespace under it. Proved needed by
      `CascadeNamespaceRecordAfterDrop.cfg` (`NothingIsLost`, 6 states) and `CascadeRootRecordAfterDrop.cfg`
      (`TheCascadeFinishes`); tested by
      `test_a_cascade_keeps_what_it_answered.py::test_a_cascade_finished_by_its_reissue_leaves_every_table_it_detached_restorable`.
   3. **A warehouse delete left trash a concurrent drop filed after its read. FIXED, guard G4.** `trash.put` files a
      record only while it holds its warehouse's namespace fence for an add (`lakehouse.trash.put`), and the warehouse
      delete holds that fence for a drop from before it reads its trash until its record is gone
      (`endpoints/warehouses.delete_warehouse`). A recoverable drop landing beside the delete is refused 409 before it
      files anything, or filed before the read, which ends it. Proved needed by `CascadeWarehouseLateTrash.cfg`
      (`NoTrashOutlivesItsWarehouse`, 13 states); tested by
      `tests/integration/test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py::test_a_warehouse_deleted_beside_a_recoverable_drop_leaves_none_of_its_trash`.
   4. **A tag made in the grace kept an erased subject past its release. FIXED, guard G1 of the erasure model.** The
      release record keeps the residual versions the verification found (`ReleaseRecord.residual`) and the obligation's
      retention (`retain_days`). Every tick reads the refs again (`erasure_release._plan`): before its deletes it takes
      what the verification named and every tag on, and every branch cut from, a residual, whoever made it; once its
      deletes are done, only what stands on a residual since. Branches come back at main's head as before. Then the
      release reclaims main at the obligation's retention (`erasure_release._reclaim`, through
      `erasure.reclaim_refs`), all under one hold of the table's history as a release
      (`HistoryWork.RELEASE`, which excludes every kind, its own included), so no ref is written between the tick's
      read and its reclaim, and after the reclaim no residual is left to name. Proved needed by `ErasureCopyTag.cfg`
      and `ErasureCutBranch.cfg` (`ErasedOnceReleased`); tested by
      `services/catalog/tests/test_an_erasure_keeps_what_it_answered.py::test_a_released_erasure_whose_release_ran_leaves_the_subject_on_no_ref`
      (for the branch, the model alone; not run against the app).
   5. **A verification cut short lost the release's notices. FIXED, guard G2 of the erasure model.** The verification
      files the notices before it writes the release's record (`endpoints/erasure._schedule_release`, through
      `Announcer.announcing` on `erasure_release.notice_target`), each landed while the record stands
      (`RecordHeld`) and sent under an `event_id` fixed by the release and the person (`erasure_release.notice`). A
      verification that meets the record tells its people again under those ids; the catalog's finish sends what a
      dead one filed; and the release tick leaves a release alone while its notices are filed
      (`erasure_release.release_due`). Proved needed by `ErasureNoticeLost.cfg` (`EveryoneIsTold`); tested by
      `test_an_erasure_keeps_what_it_answered.py::test_everyone_a_release_costs_is_told_before_it_runs`.
   6. **A verification cut short lost the dropped tags' announcements. FIXED, guard G3 of the erasure model.**
      `erasure.verify` judges every tag before it deletes any and hands the ones it will delete to `before_untag`,
      which files their `table_tag_deleted` events (`endpoints/erasure.verify_erasure`, `_file_untags`), each landed
      once its tag is gone (`announcement_intents.TagGone`). The door sends them after the drops
      (`Announcer.send_filed`); the next verification of the obligation sends what an earlier one filed
      (`Announcer.finish_on`), and maintenance's hourly finish does too. A filing that fails keeps every tag. Proved
      needed by `ErasureTagEventLost.cfg` (`EveryDroppedTagIsAnnounced`); tested by
      `test_an_erasure_keeps_what_it_answered.py::test_every_tag_an_erasure_drops_is_announced`, its failure moved
      from the read of the tag's ref (now before the drop) to the door's sending of the filed announcements.
   7. **A namespace undrop cut short could not be re-issued. FIXED, guard G3 of the cascade model.**
      `undrop_namespace` clears the namespaces' records only once every table is registered, deepest first and its
      own last, so a re-issue finds the namespace's record and finishes. Proved needed by
      `CascadeUndropNotResumable.cfg` (`TheUndropFinishes`); tested by
      `test_a_cascade_keeps_what_it_answered.py::test_an_undrop_cut_short_is_finished_by_its_reissue`.
   8. **ADR 0227's `UndropClearsRedrop` through a cascade. FIXED, guard G5 of the cascade model.** The table door's
      undrop of T landing between two attempts of the cascade cleared, unconditionally, the record the re-issue filed
      (`NothingIsLost`). The table undrop now holds T's claim fenced from before its register until its record is
      cleared (ADR 0227 §5 `UndropFence`, `UndropHoldsFence`), so the re-issue's `fence_listed` of T answers 409 while
      it runs and files T's record only once the undrop's clear is done. The namespace undrop's table step is the same
      step (`trash_doors.restore_kept_table`, ADR 0227 §8), which a table undrop holding T's fence answers 409 too
      (`UTable`). `CascadeUndropClearsRedrop.cfg` removes the fence and must fail; `Cascade.cfg` runs the table undrop
      between the cascade's attempts and holds. Tested by ADR 0227's
      `test_a_recoverable_drop_answered_200_can_be_undropped`, whose `[namespace]` case drives the namespace undrop.
   9. **A cascade destroyed a table protected after its listing. FIXED in the table step (ADR 0227 §8,
      `CascadeRereadsProtection`).** `_require_descendants_unprotected` read protection before any table was fenced,
      and neither `_destroy_listed_table` nor `_trash_listed_table` read it again; each now does under the table's fence.
      Modelled in `TableLifecycle.tla`, which models the cascade's table step; this model still leaves protection out.

   `ErasureRestore.cfg` (liveness) is a limit, not a finding: a restore of a residual version made once the obligation
   is released, before the release runs, commits its bytes as main's head, which no cleanup reaches.
   `erasure_release.completion_bound` says so in words.

7. **What a client sees change.** A create, declare, register, rename in or undrop of a table or namespace answers 409
   code 14 while its namespace is being dropped, and a namespace's drop answers 409 code 14 while something is being
   added beneath it or its undrop runs; a recoverable drop, or any drop filing trash, answers 409 code 14 while its
   warehouse is being deleted. A tag or branch write on a table answers 409 while an erasure's release runs on it. The
   release deletes, and recreates, refs it was not named, and reclaims the residual versions itself; `report.release.bound`
   says so. A notice may arrive again under the same `event_id`. Each costs a few control-root round trips per add or
   drop.

## The four facts

- **GitOps:** nothing here is declared in git; the obligations, releases, trash and fences are records on the control
  root, and each fix leaves no record a reconcile of git would have to end: a fence lapses on the store's clock, and the
  trash no longer outlives its warehouse.
- **Cloud native:** each loop stays resumable by re-issue, the cloud-native answer to a dying pod: G2 and G3 of the
  cascade order its writes so a re-issue finishes, and G2 and G3 of the erasure file what a dying request owes before it
  acts, finished by whoever comes next. Every exclusion is a conditional write on the object store, which every replica
  sees; no lock lives in a process.
- **Lance-namespace/table-idiomatic:** the erasure rests on Lance's own refs and cleanup, and the namespace fence holds
  apart two `__manifest` commits the `dir` catalog does not order, as measured, leaving each to Lance's own check.
- **Zero trust:** G1 of the erasure means no caller holding a tag or branch write can keep an erased subject, which the
  release exists to prevent ("an erasure that can be blocked is not an erasure", `erasure_release.py`). The fences are
  written under the catalog's own credential; no new grant.

## Not modelled

The predicate and its digest; index segments (one more residual kind a head must clear); external bases and MemWAL
shards; the retention window (the model reclaims at retention 0); a registered clone's tag; late derived tables (a
ruling: listed and paged); grants and their revoke; protection and `force` (the cascade's table step, protection included, is `TableLifecycle.tla`'s); claims and fences beyond `fence_listed`'s
skip and the table undrop's fence (G5) (TableLifecycle.tla); the announcements of a cascade; projects; `?purge_bucket`; a fence a dead holder keeps until
its hold lapses (the model lapses it at once); the release's own ref announcements.

## Consequences

- The catalog's port follows these models' steps, the guards included.
- `make tla-check` runs the four models. Measured on four cores shared with other work, the gate configs added here take
  about 85 s, and with the configs that must fail about two minutes; the whole `make tla-check` about four.
