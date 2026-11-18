# 0243. What a maintenance pass deletes is modelled in TLA+, and a pass deletes nothing a door answered for (2026-11-17)

**Status:** accepted; the model of what ADR 0232 left out ("the pass's effects on the store"). Three findings, each
confirmed against the real pass and the real catalog on moto and on the pinned RustFS 1.0.3, each fixed model first
(owner's standing ruling: fix now).

## Context

ADR 0232 modelled who passes a table and when. What a pass does once it runs was one step: it deletes old versions
(Lance's `cleanup_old_versions`), the files in the table's own data directories no retained version names
(`base_reclaim`), the manifests a stock writer staged for a version taken by another, and the committed-file records no
retained version needs. A wrong delete is permanent, and it is the one class nothing here had modelled. What protects
each delete was argued in comments: the history fence (a ref write and a reclaim never overlap, ADR 0176, ADR 0233), the
unverified age (7 days), the trash read again under the fence, the policy record, a credential scoped to the table.

## Decision

1. **`specs/maintenance/TablePass.tla` models one table's history under its passes and the doors that write it.**
   Versions 1 and 2 stand, 2 the head, 1 past its retention; each version names a set of files, one of them in one of
   the table's own data directories. Each action is one effect, in the code's order:
   - the pass (`services/maintenance/src/maintenance/services`): `PStart`, `table_pass.run_table_pass`'s reads
     (`read_for`, the governing policy) and the maintain vend (`credentials.write_options_for`; the catalog's vend door
     refuses a table whose policy keeps its whole history, `credentials.require_reclaimable_history`); `PTake`,
     `expiry._history_held` (`TableHistory.held(RECLAIM)`, under the vend); `PRecheck`, `expiry._dropped_since` and
     `expiry._policy_changed`; `PList`, Lance's cleanup reading its refs, head and manifests once as it starts
     (`lakehouse.history_fences`' module note, lance `cleanup.rs:472-511`): the versions it removes, the files only they
     name, and the root's files no manifest names that are past the unverified age; `PDelData` and `PDelMan`, its
     deletes, every transaction and data file before any manifest, one at a time in any order (§3); `PBList` and
     `PBDel`, `base_reclaim.unreferenced` (read after the cleanup, from every retained version) and `base_reclaim.reclaim`;
     `PRelease`. A delete is signed by the vend, and once the vend has expired the store refuses every delete it signs.
   - the doors (`services/catalog/src/catalog`): a tag (`dataplane.create_tag`, and `update_tag`, `create_branch` and
     `lakehouse.clone_sources.take` alike); a restore (`api/v1/endpoints/tables._restore_holding_history`, whose commit,
     `dataplane.restore`, reads the restored version's manifest as it commits: measured on pylance 13.0.0, a `Restore`
     committed after a cleanup removed the version fails `Not found`); a table's policy door
     (`api/v1/endpoints/policies._set_holding_history`) writing a policy that keeps the whole history; a writer's commit
     through any door or `version/create`, which takes no fence.
   - faults, counted against `MaxFaults`: a pass that dies at any step, and a pass's credential expiring. A pass that
     dies, or whose take a ref write refused, is passed again (WorkQueue). Time passes at any moment: any superseded
     version may pass its retention, which over-approximates every clock.

2. **What it proves** (`make tla-check`; 2026-11-17, two TLC workers on four cores at a load of 14 to 15, the offline
   suite running beside it). Every gate config checks every invariant:
   - `TablePass.cfg` (one pass passed again and again; a tag, a restore, a policy keeping the whole history and a
     writer; two faults): 115,507 distinct states, 11 s.
   - `TablePassTwo.cfg` (two passes at once, as a claim that lapsed under a live pass admits, WorkQueue A1; a tag and a
     restore; one fault): 15,444 states, 8 s.
   - `TablePassLive.cfg` (one fault; a tag, a restore and a writer): 12,547 states, the invariants and the property, 5 s.
   - `TablePassWide.cfg`, ON DEMAND (two passes, every door, two faults): 1,849,702 states, 75 s.

   The invariants, each a claim the code makes:
   - `RefsWhole` (nothing live is deleted): the head and every pinned version (a tag, a branch, a registered clone's
     source tag) stand with every file they name.
   - `InFlightKept`: a commit in flight keeps the files it wrote until it lands.
   - `RetentionKept`: a policy keeping the whole history that answered 200 keeps every version that was whole then.
   - `HalfOnlyVictims`: a version standing over deleted files is one a cleanup chose to remove, never another.
   - `DeletesHeld`: every delete is the pass's while it holds the table's history.

   The property, once faults stop: `EventuallyWhole`, no version stands over deleted files for good; the next pass
   removes what a dead one left half removed.

   Two passes of one table at once (`TablePassTwo.cfg`) hold every invariant: they share the fence (RECLAIM shares with
   RECLAIM), each deletes only what no version it read keeps, and their deletes commute. So ADR 0232's A1 (a pass
   paused past its lease) costs no data, which ADR 0198 had only measured.

3. **Measured on pylance 13.0.0 (2026-11-17):** one `cleanup_old_versions` sends its transaction-file deletes first
   (one `DeleteObjects` each, read on the wire against moto), then its data files, then its manifests (on a local root, Lance's
   `file_audit` log names six data deletes, then six manifest deletes); a delete the store refuses is counted in
   `CleanupStats.failed_deletes` and the cleanup returns; and a catalog restore of version 1 beside a cleanup at age 0
   on a local root left a head naming deleted data files in 2 of 40 races.

4. **Assumptions, each a config that must fail** (`EXPECT VIOLATION`):
   - A1, `TablePassCredOutlivesHold.cfg` (`RefsWhole`): a pass's credential dies no later than its hold lapses. Held by
     the order (the vend before the take, `table_pass._expire`), by the catalog vending the maintain tier for at most
     `history_fences.HOLD` (900 s, `api/v1/endpoints/credentials.vend_credentials`, this ADR) whatever
     `vending.ttlSeconds` says, and by both being judged on the store's clock. Without it, a pass paused past its hold
     deletes on after a tag was admitted beside it. Before this ADR the cap was the chart's default value only.
   - A2, `TablePassWriterOutlivesAge.cfg` (`InFlightKept`): a commit in flight lands within the unverified age (7 days)
     of writing its files, so neither Lance's unverified rule nor `base_reclaim` takes them. A TIMING ARGUMENT with a
     7-day bound: Lance judges it on the pod's clock against the store's stamp, `base_reclaim` on the pass's `now` (the
     pod's) against the store's stamp, so a pod clock 7 days ahead breaks it. Nothing else protects an in-flight
     writer's staged files, a one-call door's fragments or a multipart upload: not the seal (ADR 0242, which refuses a
     put, not a delete) and not the committed-file record (written before the version lands, read only to report).
   - A3, `TablePassNoTagFence.cfg` (`RefsWhole`): a ref write holds the history as REFS, which RECLAIM excludes
     (`history_fences._excludes`). The measurement in `lakehouse.history_fences` (20 of 60 tags named a deleted version)
     is what it prevents.
   - A4: the fence's take is atomic here; `specs/catalog/Fence.tla` proves the listings and the put-if-not-exists that
     make it so.

5. **The model was shown to bite.** Six defects were planted one at a time in a copy and each was reported on
   `TablePass.cfg`: the cleanup deleting every file a removed version names, shared ones included (`RefsWhole`, 6
   states); the cleanup ignoring tags (`RefsWhole`, 9); `base_reclaim` reading only the head (`RefsWhole`, 12); a
   delete signed by a dead credential (`DeletesHeld`, 8); the cleanup's unverified rule ignoring the age (`InFlightKept`,
   7); the policy re-read comparing nothing (`RetentionKept`, 8). The four findings' configs below are the same check for
   the fixes' guards.

6. **Three findings, each CONFIRMED and FIXED.** Each is shown by
   `tests/integration/test_a_tables_pass_deletes_nothing_a_door_answered_for.py`: the real pass
   (`table_pass.run_table_pass`) and the real catalog app over one bucket, the pass reaching the store through a proxy
   that holds its cleanup's first delete, or answers its manifest deletes 403 `ExpiredToken`
   (`tests/integration/store_requests.py`: Lance's cleanup has no Python hook, so the trace's step is held on the wire),
   or held in Python before its hold; the other door run whole; the claim judged by what a reader opens. Every case
   failed on its harm assertion before its fix, on moto and on RustFS 1.0.3; each passes after it; with its guard removed
   it fails again, on both. A local root has no wire to hold a cleanup on, so no case runs there.
   - **RestoreBesideReclaim** (`RefsWhole`; `TablePassNoRestoreFence.cfg`). Trace: the pass takes its hold and its
     cleanup lists (`PStart PTake PRecheck PList`); a restore of version 1 (a version the cleanup removes) is judged and
     commits (`RTake RCheck RWrite`, `tables.restore_table` → `dataplane.restore`, which held no fence); the cleanup
     deletes version 1's files (`PDelData`), which the restored head names. **What is lost:** the restored rows, for
     good unless the store keeps object versions; the table's head reads `Not found` until another restore or write
     replaces it (the previous head, retained by the cleanup, can be restored). **Guard `RestoreFence`:** the restore
     holds the table's history as ref work (`tables._restore_holding_history`, `HistoryWork.REFS`) and confirms its
     hold before it commits, as a tag write does; beside a reclaim it answers 409 ConcurrentModification (14). Test
     `test_a_restore_answered_200_names_no_file_a_reclaim_beside_it_deletes`.
   - **PolicyReadBeforeHold** (`RetentionKept`; `TablePassNoPolicyReread.cfg`, `TablePassNoPolicyDoorFence.cfg`).
     Trace: the pass reads the table's policy (keep one version) as it starts; the owner sets a policy keeping the whole
     history (a legal hold) and the door answers 200; the pass takes its hold and reclaims under the policy it read
     (`PStart DTake DWrite PTake PRecheck PList PDelData`); or the door's write lands after the pass took its hold and
     before its cleanup deletes (`PStart PTake PRecheck PList DTake DWrite PDelData`). The vend door's own check of the
     hold (`require_reclaimable_history`) was read once, at the vend. **What is lost:** every version the cleanup removes
     after the hold answered 200, for good. **Two guards, store-arbitrated, one per interleaving:** `PolicyReread`, the
     pass reads the governing policy again once it holds the table's history and deletes nothing when it is another
     record or cannot be read (`expiry._policy_changed`, `PolicyRead.changed`; `skipped=policy_changed`, retried after
     `retrySeconds`); `PolicyDoorFence`, the table's policy door holds the table's history as ref work while it writes
     (`policies._set_holding_history`), so of a reclaim and the write one sees the other, and the door answers 409 (14)
     while a reclaim holds. Test `test_a_policy_keeping_the_whole_history_answered_200_keeps_every_version`, one case
     per guard (held before its hold, held at its cleanup's first delete).
     **The residual, decided:** a namespace's or a project's policy governs many tables, and its door holds none of
     their histories; taking each would cost a listing of every table beneath it per write. The pass's re-read under its
     hold sees such a policy written before the hold, so one written while a cleanup already runs binds the next pass,
     not that cleanup. An owner who needs a hold honoured at once sets the table's own policy.
   - **HalfRemoved** (`RefsWhole`; `TablePassNoWholeRef.cfg`). Trace: the cleanup lists and deletes version 1's data
     files (`PList PDelData`); the pass's credential expires (`Expire`), so the store refuses its manifest deletes, which
     Lance counts and returns, or the pass dies there; its hold lapses (`Lapse`); a tag on version 1, whose manifest
     stands, is written (`TTake TCheck TWrite`), or a restore of it commits. **What is lost:** nothing more than the
     cleanup was removing, but the tag answers 200 for a version no reader can read and pins it for good, and a restore
     makes the head unreadable (recoverable by restoring the previous head). **Guard `WholeRef`:** under its hold, every
     door that pins or brings back a version (`create_tag`, `update_tag`, `create_branch` from a version, the restore,
     a clone's source tag) refuses one not holding every file it names under its own ref (`dataplane.require_whole`,
     `lakehouse.whole_versions.missing_files`: one listing of each of the ref's `data/`, `_deletions/` and `_indices/`;
     409 InvalidTableState (19); `CloneSourceGoneError` for a clone). The hold makes the answer stand: no reclaim deletes
     while the door holds, and once the ref stands the version is pinned or the head. The next pass removes the version,
     which is still past its retention and pinned by nothing (`EventuallyWhole`). Test
     `test_a_ref_answered_200_names_a_version_a_reader_can_read_whole`, one case for the tag and one for the restore.

   **Verdicts on what looked like candidates and was not:**
   - The staged-manifest reclaim runs after the hold is released and is REFUTED as a hazard: a staged manifest is no
     version's (`version/create` finalizes a catalog-written copy, `managed_versions.stage`), and one for a version the
     table holds can never land.
   - A restore through `version/create` is refused there ("restore a version through the restore door",
     `managed_versions._refuse_rows_coming_back`), so the restore door is the one path.
   - A drop, an undrop and a purge are TableLifecycle.tla's: the purge deletes no registered table's bytes, and a pass
     reads the trash again under its hold, after which a drop costs at most one cleanup under the table's own policy
     (`expiry._dropped_since`'s note), which keeps the head.
   - UNDECIDED: a version's age is its manifest's timestamp, which its writer's clock stamps and `version/create` does
     not judge, so a stock writer whose clock runs behind writes a version a later cleanup removes as soon as it is
     superseded. Settled by a staged manifest whose timestamp is set back (pylance writes the local clock) committed
     through `version/create` and a pass after one more commit; the fix would be `version/create`'s
     (`managed_versions.stage`), which this change does not touch.

## What the model assumes beyond §4

- Lance's cleanup reads its refs, head and manifests once and deletes against that reading (`cleanup.rs` at v13.0.0);
  its own rules (tags, branches and the head are kept; a file only removed versions name, or one no manifest names past
  7 days, goes) are as `lance_docs/lance/guide/read_and_write.md` § cleanup states them.
- A paged or stale listing is a reading that misses what landed after it: the model's `PList` and `PBList` are
  snapshots, so a manifest committed while the cleanup lists is either read or later, and nothing is decided on it.

## Not modelled

A drop, undrop and purge (TableLifecycle.tla); an erasure release, whose RELEASE excludes every kind (Erasure.tla);
the staged manifests and the committed-file records a pass removes (above); the floor raise, a catalog commit like any
writer's; the orphan check, which deletes nothing; a branch's own cleanup (each branch is a ref of its own with the same
steps); two tables sharing a data directory, which the directory's claim excludes (`data_directories.claimed_by`); a
namespace's or project's policy (the residual above).

## What an operator observes

- `maintenance.reclaims.held_back{reason}` counts each dataset whose pass deleted nothing because a ref write held the
  table's history (`history_fenced`) or its policy changed since the pass read it (`policy_changed`); each is passed
  again after `retrySeconds`. The log line `maintenance_policy_changed_mid_pass` names the table.
- A restore beside a running reclaim answers 409 ConcurrentModification (14) where it answered 200; a table's
  `policy/set` beside one answers 409 (14).
- A tag, a branch from a version, a restore or a clone registration naming a version a cleanup half removed answers
  409 InvalidTableState (19), logged `ref_on_a_version_being_reclaimed`, until the next pass removes the version; a
  clone's registration answers 400 InvalidInput, as it answers a source version no longer kept
  (`table_bases._tag`).
- A maintain-tier credential expires within 900 s whatever `vending.ttlSeconds` is.

## The four facts

- **GitOps**: met. No new chart value: the hold (900 s) is the fence's, the retry is `retrySeconds`, and the cap ties the
  maintain credential to the hold rather than to a value an operator could set past it.
- **Cloud native**: met. Every exclusion is a record on the store any replica reads; no process holds a lock another
  would not see.
- **Lance-namespace/table-idiomatic**: met. Cleanup is Lance's own `cleanup_old_versions`, unchanged; the guards judge
  what Lance's manifest names (`get_fragments`, `describe_indices`) and the restore is Lance's own `Restore`.
- **Zero trust**: met, and widened. The maintain credential can no longer outlive the hold that licenses its deletes; a
  door's answer rests on the store's conditional writes (the fence) and a listing read under it, never on timing.

## Consequences

- A port of the pass keeps: the hold before the cleanup, the policy read again under the hold, and every door that
  pins or brings back a version holding the history and judging the version whole. A change to that order changes this
  model first.
- `make lifecycle-rustfs-check` runs the finding tests on RustFS; the offline suite runs them on moto.
- `make tla-check` runs this model's gate and expected failures in about 45 s at a load of 15.
