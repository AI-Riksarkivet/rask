# 0233. Erasure and the namespace and warehouse cascade are modelled in TLA+; eight defects found (2026-11-17)

**Status:** accepted; the catalog's third and fourth models (ADR 0221, ADR 0227, ADR 0229), and a fifth, of the fence
both rest on. Both describe the Python catalog as it runs, before it is ported (ADR 0217 §5). The eight findings are
fixed, model first (§6); the eighth by ADR 0227 §8, and a ninth, found by review in the cascade's table step, with it.
A review of the erasure and cascade fixes found three more defects, a fourth by measurement, a fifth on reading, and
guards no test held, all fixed model first (§8). The owner decided two questions on 2026-11-17; both are built, and so is
the third item §9 had left open, the history fence's contention, model first (§9).

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
   Branch B is cut from the second; another caller may cut C from B or from main, tag any retained version, and
   register a clone of any retained version (the catalog's tag `tc` on it, ADR 0198 §1) and end it. Each action is one
   effect, in the code's order: the job's rewrite of each head; the verification's head judgement, the filing of the tags
   it drops, the drops, reclaim (its reading of the refs, then its deletes), verify (`erasure.verify`), the tag
   announcements, the read of the release that stands and the filing of the release's notices, the release's record and
   the notices (`_schedule_release`), and `erasure_obligations.settle`; the release tick's plan and claim, or its report
   of the clones that hold it, deletes, "recreating", recreates, reclaim and finish (`erasure_release._release`); the
   finish of what an ended verification filed; maintenance's cleanup; a recoverable drop and its undrop; and the
   obligation that follows this one on a derived table. Any verification and any tick may die between two effects. A
   notice is reduced to the deadline it names: each verification's own grace end, and the record's.

2. **`specs/catalog/Cascade.tla` models warehouse W, namespace P in it, C in P, table T in C, and table N that
   another caller creates (or renames in) in P.** The doors: P's cascade drop, recoverable (`_trash_subtree`) or
   destroying (`_destroy_subtree`); the create of N; the namespace undrop of P (`undrop_namespace`); the table door's
   undrop of T; the purge; and the warehouse delete with `?cascade=true` (`delete_warehouse`). Each may fail or die
   between two effects and is issued again, as each door documents. Each door is fair on its own, strongly, since a
   fence's refusal makes one door's next step wait on another's.

3. **What the gate proves** (`make tla-check`, four workers):
   - `Erasure.cfg` (one verification, branch B, a derived table, a branch cut from B, a drop and undrop, two
     failures): 61,977 states.
   - `ErasureConcurrent.cfg` (two verifications at once, no failure): 116,457 states.
   - `ErasureRefsInTheGrace.cfg` (a tag and a branch another caller makes on any retained version, its door in steps
     and able to die between them, one failure): 142,501 states.
   - `ErasureClone.cfg` (a clone another caller registers on any retained version, branch B, one failure): 5,193 states.
   - `ErasureNoticeRace.cfg` (two verifications at once, one failure, the invariants): 244,980 states.
   - `ErasureNoticeNoReverify.cfg` (no verification comes again, one failure): 1,014 states.
   - `ErasureWide.cfg` and `ErasureNoticeRaceWide.cfg`, ON DEMAND (two verifications with one failure and a branch cut
     from B; two verifications with two failures and the liveness).
   - `Fence.cfg` (two adds, two drops, a holder sharing with nobody, any hold lapsing): 4,096 states.
   - `FenceHistory.cfg` (a table's history: two ref writes, a reclaim, two branch creates and a release, any hold
     lapsing): 22,528 states.
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
   - `NoticeOnlyForARecordedRelease`: nobody is told of a release the catalog did not record, nor of a deadline no
     recorded release names, a ref's creator included.
   - `ACreatorIsToldBeforeTheRelease`: whoever put a ref the release takes while its record stood was told, with that
     record's deadline, before the release ran (§9).
   - `EveryReleasedBranchComesBack`: the release recreates every branch it deleted.
   - `NoCloneIsBroken`: a registered clone's version is never deleted while the clone stands.
   - `ErasedOnceReleased` (liveness): once released, the release and cleanup leave the subject in no version and no
     branch head, whatever ref another caller made, once every clone holding it has ended (A6).
   - `EveryObligationIsReleased`, `EveryReleaseFinishes`, `EveryDroppedTagIsAnnounced` (liveness), whatever request dies,
     and `EveryoneIsTold`: every recorded release's deadline is told, and every creator owed a notice is told while
     the ref stands.
   - `AHeldReleaseIsReported` (liveness): a release a clone holds says so, unless the clone ends first.

   `specs/catalog/Fence.tla` models one fence: adds, drops, branch creates (sharing with adds alone) and a kind that
   shares with nobody, each holder writing an object of its own and listing the others, any hold lapsing while its
   holder works. A namespace's fence has adds and drops; a table's history has ref writes (adds), reclaims and
   schedulings (drops), branch creates and releases. `Exclusive`: two holders of kinds
   that exclude each other never hold it at once; `NoLastStepBesideAnExcluder`: no last step is taken beside one admitted
   since.

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
   - Erasure A6, `ErasureCloneOutlives.cfg`: a registered clone told that it holds a release is ended by its owner.
     Without it the release waits for good and the subject stays readable through the clone's version
     (`ErasedOnceReleased`): the cost the owner accepted (§9).
   - Erasure, not an assumption but a mode: `Reverify` says whether a verification comes again while the obligation is
     open. Every gate has it; `ErasureNoticeNoReverify.cfg` and `ErasureNoticeLost.cfg` do not, which is what the
     notices' filing exists for.
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
      object per holder under each namespace name's directory on the control root (§8.4); holders of one kind share it,
      an add and a drop refuse each other
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
      `services/catalog/tests/test_an_erasure_keeps_what_it_answered.py::test_a_released_erasure_whose_release_ran_leaves_the_subject_on_no_ref`,
      a tag made in the grace, a branch cut in the grace, and a tag written while the release runs, each judged by the
      residual version gone from storage with no later verification (§8.6).
   5. **A verification cut short lost the release's notices. FIXED, guard G2 of the erasure model.** The verification
      files the notices before it writes the release's record (`endpoints/erasure._schedule_release`, through
      `Announcer.announcing` on `erasure_release.notice_target`), each landed while the record stands
      (`RecordHeld`) and sent under an `event_id` fixed by the release and the person (`erasure_release.notice`). A
      verification that meets the record tells its people again under those ids; the catalog's finish sends what a
      dead one filed; and the release tick leaves a release alone while its notices are filed
      (`erasure_release.release_due`). Proved needed by `ErasureNoticeLost.cfg` (`EveryoneIsTold`, with no verification
      coming again); tested by `test_an_erasure_keeps_what_it_answered.py::test_everyone_a_release_costs_is_told_before_it_runs`,
      where no second verification comes: the tick waits while the filing stands and the finish sends it (§8.6).
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
   warehouse is being deleted. Adds beneath one namespace never refuse each other (§8.4). A tag or branch write on a
   table answers 409 while an erasure's release runs on it. The release deletes, and recreates, refs it was not named,
   and reclaims the residual versions itself; `report.release.bound` says so. It never deletes a registered clone's tag:
   while a clone stands on a residual the release waits, the obligation lists the clone's tag `held`, and the release's
   people receive `erasure_release_scheduled` again with `extra.held_by_clones` (§8.1). A notice may arrive again under
   the same `event_id`, always naming the deadline of the release that stands (§8.3). A warehouse delete whose hold of
   its fence lapsed answers partial 409 code 14 and keeps the warehouse for its re-issue (§8.5). Each costs a few
   control-root round trips per add or drop. Ref writes on one table no longer refuse each other, and a tag or branch
   write answers 409 code 14 for the moment a verification records a release on the table; whoever puts a ref a
   scheduled release will take receives `erasure_release_scheduled` naming it in `extra.ref`, and a verification that
   ref writes keep from recording its release answers the `release` surface failed with the obligation open (§9). A
   maintain-tier vend lists its table's history-fence directory and writes only a reclaim's holds there (§9).

8. **Found by review of the fixes (commit `7dcc463`), each FIXED, model first.** Each guard is removed by a config that
   must fail, and its test fails without the guard (each removal run on 2026-11-17, the result named).
   1. **The release deleted a registered clone's tag, then the version the clone reads. FIXED, guard G4 of the erasure
      model.** Reproduced first: a clone of v2 registered in the grace (the catalog's `rask.clone.<digest>` tag on v2);
      the release's plan took every tag on a residual, the clone's included, deleted it and reclaimed v2, and the
      obligation read released. A verification never drops a clone's tag and holds the obligation open while one keeps
      a residual (`held_by_clones`, ADR 0198 §1); the release now does the same. `erasure_release._plan` lists no
      clone's tag to delete and returns those on a residual or on a branch it would delete; while any stands,
      `erasure_release._release` changes nothing (`_held_by_clones`): it records them on the release
      (`ReleaseRecord.held_by_clones`), names each on the obligation as a verification does
      (`erasure_obligations.release_held`, `erasure.held_by_a_clone`, outcome `held`), and the tick tells the release's
      people again (`erasure_release.held_notice`, under an id fixed by the release, the person and the clones). The
      release runs at the first tick after the clone ends, and clears what it named. Proved needed by
      `ErasureReleaseTakesCloneTag.cfg` (`NoCloneIsBroken`); held by `ErasureClone.cfg`; tested by
      `test_an_erasure_keeps_what_it_answered.py::test_a_release_never_takes_a_registered_clones_tag`, red with clone
      tags put back into the plan. The owner's decision and its cost are §9.
   2. **The history fence's stored field was renamed, and records written before became unreadable. FIXED.**
      Reproduced first: a record `{"uuid": …, "holders": []}`, as every release left one, refused `take`, `renew` and
      `release` with an untyped validation error. Each fence kind names its own stored field over the shared base
      (`Fence.stored_field`, `RecordFence.record_model`, `HolderFence.holder_model`): the history fence's is `uuid`
      again (`history_fences.HistoryFence`), the namespace fence's `name`. No dual read. A stored fence that does not
      parse, or names another object, raises `fences.FenceUnreadableError` (the spec's InternalError) naming the key and
      why. A history record whose holders are empty stays: the store offers no conditional delete (as
      `lakehouse.location_claims` states), an unconditional one could remove a take that landed after the release's
      read, and the record ends with its table's other records (`lakehouse.object_records`). No model treats a fence's
      record as present, so none changed. Tested by
      `packages/lakehouse/tests/test_a_tables_history_is_held_by_one_kind_of_work_at_a_time.py` (a case for the record
      a release leaves, and `test_a_record_that_does_not_parse_is_refused_by_name`), red at `7dcc463`.
   3. **A notice could go out with a later deadline than the release's, under the release's fixed id. FIXED, guard
      G5 of the erasure model.** Reproduced first: the first verification recorded the release and failed before its
      notices; a table owner was added; a second verification an hour later filed notices built from its own record (a
      `due_at` an hour later), met the record, and failed before its filing was removed; the finish sent that filing,
      and the new owner's only notice named the later deadline. `endpoints/erasure._schedule_release` now reads the
      release that stands before it files anything, and files and sends the notices OF THAT RECORD, its deadline, to its
      people and to whoever must be told now; and each notice lands only while the record it describes stands
      (`erasure_release.notice`, `RecordHeld` with the record's `due_at`, judged by `Announcer.landed`), so a
      verification that read no record and then lost the create files nothing that lands, and tells the people of the
      record that won. Proved needed by `ErasureNoticeDeadline.cfg` (`NoticeOnlyForARecordedRelease`); held by
      `ErasureNoticeRace.cfg` and, on demand, `ErasureNoticeRaceWide.cfg`, with the deadline in `EveryoneIsTold`;
      tested by `test_every_notice_names_the_deadline_of_the_release_that_stands`, its case `it-loses-the-create` red
      with the record's `due_at` dropped from the evidence. The read before filing alone is held by the evidence and
      the re-read after a lost create (its removal leaves both cases green): it spares a filing that cannot land.
   4. **Adds beneath one namespace refused each other. FIXED, guard F1 of the fence model.** Measured on the pinned
      RustFS 1.0.3, through the catalog's own create door with one shared fence record per namespace: of 16 concurrent
      creates of child namespaces into one namespace, 8 answered 409 code 14 ("8 attempts … each lost to another
      writer"); of 64, 39; and of 16 and 64 bare takes of the fence, 6 and 44 were refused. No drop ran. The namespace
      fence now keeps one object per holder (`fences.HolderFence`): a take lists the namespace's directory, refuses at
      once beside a live holder of the excluding kind, writes its own object under a key no other holder writes, and
      lists again, yielding (its object removed, 409) when an excluding holder shows. Holders that share the fence
      never write the same key, so contention cannot refuse them; a holder lapsed for a whole hold is swept, marked under
      its ETag before it is deleted so a renewal that landed meanwhile keeps it. Measured again: 16 of 16 and 64 of 64
      creates answered 200 (1.9 s and 8.0 s), and 16 of 16 and 64 of 64 takes held. Proved needed by
      `FenceNoRecheck.cfg` (`Exclusive`), held by `Fence.cfg`; tested by
      `packages/lakehouse/tests/test_a_namespaces_adds_share_its_fence_and_its_drop_excludes_them.py`, red at `7dcc463`
      (1, 3 and 6 of 32 adds refused on a local root in three runs). The history fence kept its one record then, its
      ref writes contending as measured; it moved to holder objects since (§9).
   5. **A holder whose hold lapsed went on to its last step. FIXED, guard F2 of the fence model.** Read from the code:
      `Fence.held` renews in a thread and logs a lost hold, while the block runs on; the warehouse delete then removed
      the warehouse's record although a trash record could have been filed under it after its hold lapsed. The delete
      now renews its hold once more before that record goes (`fences.Hold.confirm`, through
      `fenced_namespace.holding_to_confirm`): a renewal succeeds only while the hold stands, so one that succeeds shows
      no add was admitted since the take, and a lapsed hold refuses 409, the record kept for the re-issue. Proved needed
      by `FenceNoConfirm.cfg` (`NoLastStepBesideAnExcluder`); tested by
      `tests/integration/test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py::test_a_warehouse_deleted_beside_a_recoverable_drop_leaves_none_of_its_trash`,
      its case `its-fence-lapsed-meanwhile`, red with the confirm removed.
   6. **Guards no test held.** Each now has the test named, red with exactly that guard removed:
      - the release's own reclaim (`erasure_release._reclaim`): every case of
        `test_a_released_erasure_whose_release_ran_leaves_the_subject_on_no_ref`, which judges the residual version gone
        from storage after the release with no later verification (the next verification's reclaim had hidden it);
      - the release's hold excluding ref writes (`history_fences._excludes`, `RELEASE`): its case
        `a-tag-written-while-the-release-runs`; held as a reclaim instead (`RECLAIM`, which also excludes ref writes),
        `test_erasure_releases_a_pinning_branch_after_its_notice.py::test_a_release_waits_while_a_reclaim_holds_the_tables_history`
        fails, since two reclaims share the fence;
      - the filing of the release's notices: `test_everyone_a_release_costs_is_told_before_it_runs`, re-pointed at the case
        no second verification comes (with nothing filed the release ran first and nobody was told); its comment had
        credited the second verification's re-send;
      - `trash.put` under its warehouse's fence: `test_a_warehouse_deleted_beside_a_recoverable_drop_leaves_none_of_its_trash`
        fails with exactly that take removed (`if True:` in its place), at `7dcc463` and now, leaving both records: the
        claim that no test fails without it did not hold, and no case was added;
      - the branch variant of a ref made in the grace: its case `a-branch-cut-in-the-grace`.

9. **The owner's decisions, and what is still open.**
   - **A registered clone standing past the deadline: DECIDED, "Wait and report"** (owner, 2026-11-17, through the
     question tool). The erasure's contract says the subject must not stay readable past the deadline; ADR 0198 says a
     clone is never broken by anything but its own end. The release keeps the clone, stays unfinished, and says so on the
     obligation and to the release's people, finishing on the tick after the clone ends (§8.1). The accepted cost: the
     subject stays readable through the clone past the deadline until the clone's owner ends it
     (`ErasureCloneOutlives.cfg`). The obligation's state stays `released` (A4: a released obligation is never written
     open again); its `open_surfaces` name the clone `held`. The clone's own owner is named by no event, since the tag
     names the clone's location only.
   - **A branch or tag made on a residual during the grace: DECIDED, "Tell the creator"** (owner, 2026-11-17, through the
     question tool): whoever creates it gets the release's `erasure_release_scheduled`, with the due time, at creation,
     filed before the ref as the other notices are. **BUILT, guards G6 and G7 of the erasure model** (below).
   - **The history fence's ref writes contended** (§8.4): concurrent tag or branch writes on one table were refused 409
     although nothing excluded them. **FIXED** (below).

   1. **Tell the creator. FIXED, guards G6 and G7 of the erasure model.** Every door that puts a ref on a version, and
      what each does now:
      - `tags/create`, `tags/update` (`dataplane.create_tag`, `update_tag`) and `branches/create`
        (`dataplane.create_branch`): each holds the table's history (as a ref write, or a branch create) and asks its
        `before_ref` inside the hold, before the ref (`dataplane.BeforeRef`, `api/ref_notices.putting_ref`). The hold's
        take lists the fence's directory, which carries a mark per release scheduled on the table
        (`erasure-release.<id>.json`, `fences.MARK_NAME`, `erasure_release.mark_key`); for each mark the door reads the
        release's record once (`erasure_release.RefNotices`) and judges whether the release takes the ref as its next
        tick reads the table (`erasure_release.takes`, which shares `_taken_branches` with `_plan`): a tag on a residual
        or on a branch the release takes, a branch cut from either, never a clone's tag. When it does, the door files the
        caller's `erasure_release_scheduled` on the release's own target (`notice_target`) before the ref
        (`erasure_release.creator_notice`): `extra.subject` the caller (`fga.user_object` of the verified token's
        subject, never a role literal), `extra.ref` (`tag:<name>`, `branch:<name>`), `extra.on` (`<branch or
        main>@<version>`), the release's `extra.due_at`, `refs` and `release_id`, `object_id` the table's `table:<uuid>`,
        `actor` the erasure's requester, under an `event_id` fixed by the release, the person and the ref. It lands once
        the ref stands where it was put and the record with its `due_at` stands (`announcement_intents.RefOnARelease`,
        judged by `Announcer.landed`). The door sends it after the ref (`Announcer.send_filed`); a write that failed sends
        only what landed (`Announcer.send_landed`); a door that died leaves it to the catalog's finish; and the release
        tick leaves the release alone while any filing on its target stands (`release_due`, as for every notice). A
        caller no token names is told nothing. The control lane's event carries no `author.sub` or `lance.project`
        (those are the lineage lane's): its targeting fields are `extra.subject` and `object_id`.
      - The verification records the release while it holds the table's history as a scheduling
        (`HistoryWork.SCHEDULE`, which excludes ref writes and branch creates as a reclaim does): it writes the mark,
        confirms its hold, and creates the record (`erasure_release.schedule`). A ref door either ended before the record
        stood, or its take listed the mark. A scheduling that ref writes keep out at five takes answers the `release`
        surface failed, and the obligation stays open for the next verification. The release's end deletes its record,
        then its mark (`_finish`); a ref door that finds a mark whose record is gone removes it, since no scheduling can
        hold the table's history beside its own hold.
      - A registered clone (`clone_sources.take`, through the register door): its tag is never taken (G4), so nothing is
        filed for it; while it holds the release, the release's own people hear `extra.held_by_clones` each tick, and the
        clone's owner hears nothing of its own, as before (wait and report).
      - `version/create` puts no ref: it commits onto a branch the table already lists and answers 404 code 22 for any
        other ("A branch is created through CreateTableBranch"). It was not changed. `restore` commits a version; the
        release's own recreates are the release's.
      - Straight to the store, on a vended credential: none can write a ref. No tier is granted anything under the
        table's `_refs/` (`lakehouse.table_layout.REFS_GLOB`): `read` gets only; `write` puts only `WRITER_PUT_GLOBS`
        under the ref it writes (and, for the table's own credential, `tree/*`), with an explicit Deny on every final
        manifest (`vending.build_session_policy`); `maintain` gets and deletes, with an explicit Deny of delete on
        `_refs/*`. Lance keeps every tag and branch pointer at the dataset root's `_refs/`
        (`lance_docs/lance/format/table/branch_tag.md` § Branch Metadata Path, § Tag Storage), which no put glob reaches,
        and a session policy only narrows the catalog's canned policy. No narrowing was needed.
      Round trips, measured on the pinned RustFS 1.0.3 through the tag door (botocore requests to the control root): a
      tag on a table with no release scheduled makes 7 (the fence's two listings, its holder's create, read and delete,
      and two reads the door made before), none of them the notice path's; one on a table whose release does not take
      the tag adds one read (the release's record); one the release takes adds that read, the filing's put and its
      delete. The judgement also lists the table's branches through pylance, once, only where a mark stands. Proved
      needed by `ErasureCreatorUntold.cfg` (G6: sent after the ref with nothing filed, the door dying between the two)
      and `ErasureCreatorRace.cfg` (G7: the record written without the hold beside a door that read no release), each
      failing `ACreatorIsToldBeforeTheRelease`; held by `Erasure.cfg`, `ErasureRefsInTheGrace.cfg` and, on demand,
      `ErasureWide.cfg`, with the creators in `EveryoneIsTold`. Tested by
      `services/catalog/tests/test_an_erasure_keeps_what_it_answered.py::test_whoever_puts_a_ref_the_release_takes_is_told_before_it_runs`:
      another principal tags version 2, or cuts a branch from it, in the grace, and its notice is on the bus with the
      record's `due_at` before the release runs; red at `4c76afd` (told nothing). Its case
      `its-door-fails-after-the-tag` is red with the filing removed (sent after the ref only) and red with the tick's
      wait removed; its case `a-tag-written-while-the-release-is-recorded` is red with the scheduling's hold removed (the
      door read the mark, found no record yet, wrote its tag and was told nothing).
   2. **Ref writes on one table refused each other. FIXED, the history fence on holder objects.** Reproduced first
      through the catalog's tag door on the pinned RustFS 1.0.3
      (`tests/integration/test_a_tables_ref_writes_share_its_history.py`, the `s3` case of `race_ns_client`): of 16
      concurrent tag writes on one table 7, 8 and 9 answered 409 code 14 in three runs ("8 attempts to hold this
      table's history for a refs each lost to another writer"), and of 64, 39 and 36, with no reclaim running. The
      history fence now keeps one object per holder beneath the table's directory (`history_fences.TableHistory` on
      `fences.HolderFence`, `fence_directory`), as the namespace fence does (§8.4); the record fence is gone. Which kinds
      exclude which is kept exactly (`history_fences._excludes`): ref writes share with ref writes, reclaims with
      reclaims, a branch create with ref writes alone, a release with nobody; the scheduling added above shares with
      reclaims and itself. Measured again: 16 of 16 and 64 of 64 answered 200. `Fence.tla` takes the history fence's
      kinds (adds, drops, branch creates, solos) and `FenceHistory.cfg` holds `Exclusive` and
      `NoLastStepBesideAnExcluder` for them; without the second listing it fails `Exclusive`, as `FenceNoRecheck.cfg`
      shows for the namespace fence. The test is red at `4c76afd` on RustFS; on a local root contention never refused a
      take (0 of 64 in three runs), so its `dir` case passes either way.
      **The maintain-tier vend's grant** (`vending.TableRecords.maintain_statements`), which a reclaim signs its hold's
      writes with. Before: get and put on the one key `_history_fences/table-<digest>.json`. After: `s3:ListBucket` on
      the control bucket with `s3:prefix` exactly `<root>/_history_fences/table-<digest>/` (`StringLike`, no wildcard),
      get on `<that directory>/*`, and put and delete only on `<that directory>/reclaim-*`, the holder objects of the one
      kind maintenance takes. It lists and reads its own table's fence and writes only its own kind's holds: it cannot
      remove or rewrite a ref writer's or a release's hold, nor a release's mark (before, a put on the one record could
      rewrite every holder in it), and a lapsed holder of another kind it meets is left for the catalog's next take to
      sweep (`HolderFence._sweep` never raises). Still a session policy under the catalog's own identity, 900 s; the
      catalog's canned policy and every other identity's are unchanged, and no chart value changed. Measured on RustFS
      1.0.3 with STS AssumeRole by a non-root user holding the catalog's canned policy and the vend's own session
      policy: the vend takes, renews and releases a reclaim's hold; it lists its fence directory, and is refused
      `AccessDenied` listing the same prefix without its slash, `_history_fences/`, another table's directory and the
      control root; it reads a ref writer's hold and the mark, and is refused putting, overwriting or deleting either,
      putting a reclaim hold in another table's directory, reading another table's hold, putting beside the directory,
      and reading or writing the trash. The grant costs 484 characters of the 2,048 a session policy takes: at a
      63-character bucket a maintain vend now fits a table prefix of 182 characters, against 333 before, and a write vend
      fits 89, so no table a writer can be vended loses its maintain vend. An uncontended ref write makes one more
      control-root request than with the record (7 against 6, measured as above).
      **The old single records** (`_history_fences/table-<digest>.json`) are inert: no code reads that key, a listing of
      the directory `table-<digest>/` does not show it, and nothing removes it; one small object per table that ever
      had a ref written or reclaimed, on a pre-production estate. A table's end now deletes its fence directory, holders
      and marks (`history_fences.end`, through `object_records.end_object_records`).
   - **Left open, for the owner:** a commit onto a branch the release will recreate (`version/create`, or `insert`
     and `merge_insert` naming a branch) is lost with it, and its committer, who may be neither the branch's creator
     nor an owner, is told nothing; telling them would put a release read on every commit to a table with a release
     scheduled. A ref put before the release is recorded, while the obligation is open, is named by the verification's
     pins, and its creator is told only as the requester or an owner; the ruling named the grace period.

## The four facts

- **GitOps:** nothing here is declared in git; the obligations, releases, trash and fences are records on the control
  root, the maintain vend's scope is rendered by the catalog's code from the table's uuid and never granted by hand, and
  each fix leaves no record a reconcile of git would have to end (a release's mark goes with its record): a fence lapses on the store's clock, the
  trash no longer outlives its warehouse, and a namespace fence's holder objects go with their holders, a lapsed one
  swept by the next take (§8.4).
- **Cloud native:** each loop stays resumable by re-issue, the cloud-native answer to a dying pod: G2 and G3 of the
  cascade order its writes so a re-issue finishes, and G2, G3 and G5 of the erasure file what a dying request owes before
  it acts, finished by whoever comes next, each filing naming the record it describes. A held release (G4) is
  level-triggered: every tick reads the table again. Every exclusion is a conditional write and a listing on the object
  store, which every replica sees; no lock lives in a process, and F2 makes a holder that lost its fence stop.
- **Lance-namespace/table-idiomatic:** the erasure rests on Lance's own refs and cleanup, a clone on Lance's own tag
  (G4 never takes it), and the namespace fence holds apart two `__manifest` commits the `dir` catalog does not order, as
  measured, leaving each to Lance's own check. Which refs a release takes is read off Lance's own ref listing, as the
  release's tick reads it (§9), and no vended credential is granted anything under `_refs/`, per the layout Lance fixes.
- **Zero trust:** G1 of the erasure means no caller holding a tag or branch write can keep an erased subject, which the
  release exists to prevent ("an erasure that can be blocked is not an erasure", `erasure_release.py`); a registered
  clone holds it only while its owner keeps it, and says so (G4, §9). The fences are written under the catalog's own
  credential, except a reclaim's hold, which maintenance writes under its table's maintain-tier vend: that vend now
  lists its own table's fence directory and writes only reclaim holds in it (§9), narrower in what it may write than the
  one record it could rewrite before, and refused everywhere else, as measured. A ref's creator is told under the
  identity the catalog verified, and only of a release that takes that ref (G6).

## Not modelled

The predicate and its digest; index segments (one more residual kind a head must clear); external bases and MemWAL
shards; the retention window (the model reclaims at retention 0); a clone on a branch version (it holds the release as
one on main does); which person a notice reaches (a notice is its deadline); late derived tables (a ruling: listed and
paged); grants and their revoke; protection and `force` (the cascade's table step, protection included, is
`TableLifecycle.tla`'s); claims and fences beyond `fence_listed`'s skip and the table undrop's fence (G5)
(TableLifecycle.tla); the announcements of a cascade; projects; `?purge_bucket`; a fence a dead holder keeps until its
hold lapses (Erasure and Cascade lapse it at once; Fence.tla models the lapse and the sweep's effect, not the clock);
the release's own ref announcements.

## Consequences

- The catalog's port follows these models' steps, the guards included.
- `make tla-check` runs the five models. Measured on four cores shared with other work, the gate configs added here take
  about 85 s, and with the configs that must fail about two minutes; the whole `make tla-check` about four. The configs
  §8 adds: `ErasureClone.cfg` (5,193 states), `ErasureNoticeRace.cfg` (244,980 states, invariants, about 20 s),
  `ErasureNoticeNoReverify.cfg` (1,014), `Fence.cfg` (4,096), and the five that must fail, each in seconds;
  `ErasureNoticeRaceWide.cfg` is on demand (1,222,016 states, about fourteen minutes on two cores). §9 grows
  `Erasure.cfg` to 61,977 states and `ErasureRefsInTheGrace.cfg` to 142,501 (about two and a half minutes with its
  liveness), and adds `FenceHistory.cfg` (22,528) and the two configs that must fail, each in seconds; the whole
  `make tla-check` took 17 minutes on four cores shared with other work, 2026-11-17.
