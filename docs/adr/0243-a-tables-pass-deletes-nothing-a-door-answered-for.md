# 0243. What a maintenance pass deletes is modelled in TLA+, and a pass deletes nothing a door answered for (2026-11-17)

**Status:** accepted; the model of what ADR 0232 left out ("the pass's effects on the store"). Three findings, each
confirmed against the real pass and the real catalog on moto and on the pinned RustFS 1.0.3, each fixed model first
(owner's standing ruling: fix now). Amended 2026-11-18: an independent review found nine more problems, each reproduced
first and FIXED model first (§7 to §11); one of them is a decision taken for the owner to overturn (§8, what a hold
means across levels). Amended again 2026-11-18: a third review found eight more and three smaller ones, each FIXED model
first (§12), and with them every reclaim in the estate became one function and the ref doors list no file.

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
     `expiry._history_held` (the history's RECLAIM and, guard `AncestorFence`, the policy fence of each namespace and the
     project above the table, §7.2, all under the vend); `PRecheck`, `expiry._dropped_since` and `expiry._policy_changed`;
     `PList`, the victims Lance's own dry run names written as the pass's mark (guard `VictimMark`, §7.4) and Lance's cleanup,
     restricted to them, reading its refs, head and manifests once as it starts (`lakehouse.history_fences`' module note,
     lance `cleanup.rs:472-511`): the versions it removes, the files only they name, and the root's files no manifest names
     that are past the unverified age; `PDelData` and `PDelMan` (the marks naming no standing version removed once every
     victim's manifest is gone), its
     deletes, every transaction and data file before any manifest, one at a time in any order (§3); `PBList` and
     `PBDel`, `base_reclaim.unreferenced` (read after the cleanup, from every retained version) and `base_reclaim.reclaim`;
     `PRelease`. A delete is signed by the vend, and once the vend has expired the store refuses every delete it signs.
   - the doors (`services/catalog/src/catalog`): a tag (`dataplane.create_tag`, and `update_tag`, `create_branch` and
     `lakehouse.clone_sources.take` alike); a restore (`api/v1/endpoints/tables._restore_holding_history`, whose commit,
     `dataplane.restore`, reads the restored version's manifest as it commits: measured on pylance 13.0.0, a `Restore`
     committed after a cleanup removed the version fails `Not found`); the policy doors (`api/v1/endpoints/policies._fenced`):
     the table's set (`policy`) and delete (`pdelete`, which hands the table to its namespace's policy keeping every
     version), a namespace's and a project's set (`nspolicy`, `projpolicy`), each writing only once its hold is confirmed
     (guard `DoorConfirm`, `lakehouse.fences.Fence.write_held`, §7.3); a writer's commit through any door or
     `version/create`, which takes no fence.
   - the policies: three levels (the table's record keeping one version, its namespace's, its project's), each none, keep
     one, keep every version, or hold; the nearest record set decides how much is kept and a hold at any level keeps
     everything (guard `HoldAnyLevel`, §8).
   - faults, counted against `MaxFaults`: a pass that dies at any step, a pass's credential expiring, and a door's hold
     lapsing under it (its process frozen past `HOLD`). A pass that dies, or whose take a ref write or a policy write
     refused, is passed again (WorkQueue). Time passes at any moment: any superseded version may pass its retention, which
     over-approximates every clock.

2. **What it proves** (`make tla-check`; 2026-11-18 after §12, two workers on four cores shared with other agents). Every
   gate config checks every invariant; the whole module's gate and expected failures run in about 30 s:
   - `TablePass.cfg` (one pass passed again and again; a tag, a restore, the table's hold and a writer; two faults):
     338,771 distinct states.
   - `TablePassPolicies.cfg` (every policy door: the table's set and delete, a namespace's and a project's hold, each
     retried and leaving an intent, a rename into a namespace that holds; one fault, a door's lapse among them): 82,978
     states.
   - `TablePassTwo.cfg` (two passes at once, as a claim that lapsed under a live pass admits, WorkQueue A1, each naming
     any part of its victims; a tag and a restore; one fault): 36,226 states.
   - `TablePassLive.cfg` (one fault; a tag, a restore and a writer): 25,632 states, the invariants and the property.
   - `TablePassIntentLive.cfg` (a namespace's and a project's hold retried beside passes delivered for ever): 178 states,
     the invariants and `PolicyLands` (§12 F4).
   - `TablePassWide.cfg`, ON DEMAND (two passes, a tag, a restore, the table's policy delete, a namespace's hold and a
     writer; one fault): 4,735,434 distinct states after §12, under three minutes on two workers. Every door with two faults was stopped by its time limit after 56,419,774 distinct
     states and no violation, unfinished.

   The invariants, each a claim the code makes:
   - `RefsWhole` (nothing live is deleted): the head and every pinned version (a tag, a branch, a registered clone's
     source tag) stand with every file they name.
   - `InFlightKept`: a commit in flight keeps the files it wrote until it lands.
   - `RetentionKept`: a policy door that answered 200 for keeping the whole history (a hold at any level, or a table
     record's delete handing the table to a level keeping every version) keeps every version that was whole then.
   - `HalfOnlyVictims`: a version standing over deleted files is named by a standing mark, which every ref door reads.
   - `DeletesHeld`: every delete is the pass's while it holds the table's history.

   The properties: `EventuallyWhole`, once faults stop no version stands over deleted files for good, the next pass
   removing what a dead one left half removed; and `PolicyLands`, a level's policy door retried while reclaims keep
   coming beneath it lands (§12 F4).

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
   - A4: each fence's take is atomic here; `specs/catalog/Fence.tla` proves the listings and the put-if-not-exists that
     make it so. A pass's takes of the history and of each ancestor's policy fence are one step: nothing is deleted
     before every take stands, and a refused take releases the others (`ExitStack` in `expiry._history_held`).
   - A5: a door's write lands within `HOLD` of the confirm before it. The confirm renews the hold, and the write is the
     next store request (`Fence.tla` F2); the model makes confirm and write one step. A door frozen for 15 minutes
     between its confirm and its write is not covered.

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
     The namespace's and the project's policy, left as a residual here on 2026-11-17, is closed by §7.2.
   - **HalfRemoved** (`RefsWhole`; `TablePassNoVictimMark.cfg`, which replaced `TablePassNoWholeRef.cfg`). Trace: the cleanup lists and deletes version 1's data
     files (`PList PDelData`); the pass's credential expires (`Expire`), so the store refuses its manifest deletes, which
     Lance counts and returns, or the pass dies there; its hold lapses (`Lapse`); a tag on version 1, whose manifest
     stands, is written (`TTake TCheck TWrite`), or a restore of it commits. **What is lost:** nothing more than the
     cleanup was removing, but the tag answers 200 for a version no reader can read and pins it for good, and a restore
     makes the head unreadable (recoverable by restoring the previous head). The guard first fixed here judged the files
     the manifest names (`WholeRef`); review found it misses a blob sidecar and a partly deleted index, and it is
     replaced by the cleanup's mark (`VictimMark`, §7.4), with the file check kept as the second line. The next pass
     removes the version, which is still past its retention and pinned by nothing (`EventuallyWhole`). Test
     `test_a_ref_answered_200_names_a_version_a_reader_can_read_whole`.

   **Verdicts on what looked like candidates and was not:**
   - The staged-manifest reclaim runs after the hold is released and is REFUTED as a hazard: a staged manifest is no
     version's (`version/create` finalizes a catalog-written copy, `managed_versions.stage`), and one for a version the
     table holds can never land.
   - A restore through `version/create` is refused there ("restore a version through the restore door",
     `managed_versions._refuse_rows_coming_back`), so the restore door is the one path.
   - A drop, an undrop and a purge are TableLifecycle.tla's: the purge deletes no registered table's bytes, and a pass
     reads the trash again under its hold, after which a drop costs at most one cleanup under the table's own policy
     (`expiry._dropped_since`'s note), which keeps the head.
   - A version's age is its manifest's timestamp, which its writer's clock stamped: left UNDECIDED on 2026-11-17,
     reproduced and fixed by §10.

## Found by review (2026-11-18), each FIXED

Each was reproduced first, on moto and on RustFS 1.0.3 (the reviewer's scratch tests and scripts, run unchanged against the
base of this change: 6 of 10 cases failed), then modelled, then fixed. Each guard's test is in
`tests/integration/test_a_tables_pass_deletes_nothing_a_door_answered_for.py` unless named; each test failed on its harm
assertion with its guard removed, on moto and on RustFS, and passes with it.

### 7.1 B1: the table's policy DELETE held no fence

`policies._delete` wrote with no hold where `_set` held the table's history. Deleting the table's own record (keep one
version) hands the table to its namespace's (keep every version): held at its cleanup's first delete, a pass that read
the table's record deleted versions 1 and 2 after the delete answered 200. **Guard `PolicyDeleteFence`:** the delete takes
the hold the set takes (`policies._fenced`, every policy write and delete at every level). Config
`TablePassNoPolicyDeleteFence.cfg` (`RetentionKept`). Test case `table-delete-at-its-cleanups-first-delete`.

### 7.2 A namespace's or a project's policy, written while a cleanup ran

Reproduced: a namespace's hold set while a cleanup of a table beneath it ran answered 200, and versions [1, 2, 3] became
[3]. **Guard `AncestorFence`:** a pass holds, for as long as it deletes, a SHARED holder (`PolicyWork.RECLAIM`) on the
policy fence of each namespace of its chain and of its project (`lakehouse.policy_fences.PolicyFence`, a `HolderFence`
subclass, `expiry._history_held`, `table_pass._policy_fences`), under its vend, beside the history's; a namespace's or a
project's set or delete holds that level's fence as `PolicyWork.SET`, which every reclaim excludes, and writes only once
it is confirmed (`policies._fenced`). Passes never exclude each other; a policy write answers 409 ConcurrentModification
(14), retryable, while any pass deletes beneath it; a pass that finds a door's hold deletes nothing
(`skipped=policy_fenced`) and is passed again after `retrySeconds`. The catalog's own reclaim doors take the same holds
(§9). Config `TablePassNoAncestorFence.cfg` (`RetentionKept`). Test case `namespace-hold-at-its-cleanups-first-delete`.

THE GRANT IS NOT EXACT, AND WHY. The brief asked the vend to reach exactly the holder objects of the table's own levels.
An STS session policy holds 2,048 characters (`vending.SESSION_POLICY_LIMIT`), and the maintain tier's, without any policy
fence and without `Sid`s, counts 1,389 under a 17-character bucket and 1,757 when the control and the data bucket names are
both 63 characters (`vending.counted_length`); one grant per level
costs about 200 characters, so a chain of three namespaces and a project did not fit (measured: the lane's first run
refused the vend 400 SessionPolicyTooLong at 2,054 with one level). So every namespace fence of a warehouse lies beneath
one directory (`policy_fences.warehouse_directory`, `_policy_fences/w<digest>/n<digest>`), the project's at
`_policy_fences/p<digest>`, and the vend holds two strings whatever the depth: put and delete of `/reclaim-*.json` beneath
its warehouse's directory and its project's, list and read of every policy fence (who holds one, never a policy). Measured
on RustFS 1.0.3 with the real session policy (`tests/integration/test_a_maintain_vend_plants_nothing_the_fence_trusts.py`):
a reclaim's holder at its namespace and at its project answered 200; a policy door's holder (`set-`) at its namespace, and
a reclaim's in another warehouse, AccessDenied. What a compromised maintain tier can plant is a reclaim's hold on a
namespace of its own warehouse, for one hold, which delays a policy write there and nothing else. The four fence
statements carry no `Sid`. Its size at the longest names is §12 F8's: 1,964 characters, and a warehouse that would not
fit is refused at registration.

THE COST, measured on moto through the counting proxy: each level a pass holds costs five requests (two listings, the
holder's put, the release's read and delete) and two more per renewal (every five minutes while it deletes); the policy is
now read at every level, not only up to the nearest record (`governing_policy`: the table's depth plus two reads, every
pass); the pre-vend probe lists the table's history fence for a mark (one listing a dataset); and the mark costs a put and
a delete. A pass of one table one level deep sent 123 requests in all, nine of them these.

### 7.3 B4: a door's write after its hold lapsed

The table policy door held with `held(REFS)` and wrote, where the tag and restore doors confirmed first; `clone_sources.take`
had the same shape. A catalog process frozen past the hold's 15 minutes resumed and wrote, answering 200, beside a cleanup
admitted once the hold lapsed. **Guard `DoorConfirm`:** `lakehouse.fences.Fence.write_held` holds, runs the door's
judgement under the hold, confirms the hold (a renewal that succeeds only while it stands), and only then writes; it is the
one way a door writes what its answer promises under a fence: `DoorTable.history_write` for the tag, branch and restore
doors, `policies._fenced` for every policy door, `clone_sources.take` for a clone's source tag. The model lets a door's hold
lapse (`DoorLapse`). Config `TablePassNoDoorConfirm.cfg` (`RetentionKept`). Test case
`namespace-hold-its-door-stalled-past-its-hold`: the door's hold shortened to 2 s, the door stalled before its confirm
until the hold lapsed and a pass was admitted and reached its first delete.

### 7.4 B2: the wholeness check missed what a cleanup deletes

Reproduced on RustFS 1.0.3 and pylance 13.0.0: one cleanup sends its deletes as `.txn`, `.lance`, the data file's blob
sidecar (`data/<stem>/<id>.blob`), the manifest; with the `.lance` and manifest deletes refused and the sidecar deleted,
`missing_files` returned nothing, a tag or restore answered 200, and reading the blobs failed. **Guard `VictimMark`:** the
guard rests on what the cleanup decided, not on file kinds (`lakehouse.reclaim_marks`). Under its RECLAIM hold, before any
delete, a reclaim names its victims from Lance's own dry run (`explain_cleanup_old_versions(..., include_files=True)`, the
manifests it names; every untagged version below the head when the dry run truncates, a superset), writes the mark, and
runs `cleanup_old_versions` restricted to them by its `versions`, which `lance_docs/lance/guide/read_and_write.md` § Cleanup
old versions says limits which versions it can remove (and an empty `versions` is refused, measured, so none is sent). The
mark is removed only once every cleanup returned with no failed delete, and with it each standing mark of the ref naming no
version that still stands. Every door that calls `require_whole` (tag create and update, branch create, restore, a clone's
source) refuses a version a standing mark names, whatever files still stand (409 InvalidTableState, 19). A later pass that
finds a mark finishes its versions while its policy still reclaims them (the probe lists the table's fence directory for
one, and a mark makes the pass reclaim below its threshold); a mark whose versions a policy now keeps stays, and no door
names them (`held_back{reason="marked"}`): when in doubt the pass keeps the file.

THE MARK: key `<history fence directory>/reclaim.<32 hex>.json` (`history_fences.VICTIMS_MARK`, one per cleanup call,
directly in the directory, never swept by a take), body `{"uuid": <table uuid>, "ref": <branch or null>, "versions":
[...], "holder": <its writer's hold token>}` (`reclaim_marks.VictimsMark`, extra fields refused, `main` read as null).
What a mark is trusted to say, who can make a door refuse what and for how long, is §12 F2.

The file listing this section first kept as a second line (`whole_versions.missing_files`) is deleted: §12 F7.

Config `TablePassNoVictimMark.cfg` (`RefsWhole`). Test cases `tag-5242880-data-files-and-manifests-pass` and
`restore-5242880-data-files-and-manifests-pass` (a 5 MiB payload per row, a dedicated sidecar); with the mark's check
removed every case of the test fails.

### 7.5 B3: no test pinned the maintain credential's cap

**Test** `test_a_maintain_vend_dies_within_the_history_hold_whatever_the_ttl_configured` (the real-store lane): with the
vendor's TTL configured at an hour, the maintain tier vended through the real door expires within the history hold on the
store's clock, and the store refuses a delete it signs once it has, the file standing. RustFS 1.0.3 issues no session
shorter than 900 s whatever is asked (measured: 5, 15, 60 and 900 s each expired at the same instant), so the case waits
out one hold: the file's two cases took 911 s on the lane. With the cap removed it fails at once, the vend lasting 3,600 s.

## 8. What a hold means across levels (decided 2026-11-18, for the owner to overturn)

Before this, the nearest record governed every setting: a table's own policy overrode a namespace's `cleanup_enabled`
false outright, and a namespace's hold never froze a table that had a policy of its own. **Decision:** `cleanup_enabled`
false at ANY level that can govern a table (its own record, any ancestor namespace's, its project's) keeps that table's
whole history; a narrower level cannot undo a hold. Every other setting (how many versions, how many days, the cadence)
stays nearest-wins. This is the conservative reading (when in doubt a pass keeps the file); the owner may overturn it for
nearest-wins on holds too, which `TablePassNearestHold.cfg` (`RetentionKept`) shows breaks the claim a hold's door
answered. **Where:** `maintenance_policies._held`, used by `governing_policy` (every reader: the pass's `read_for` and its
re-read under its holds, the vend door's `require_reclaimable_history`, the catalog's reclaim doors) and by
`resolve_policy`; the record returned is the nearest one with `cleanup_enabled` false and `held_by` naming the holding level
as `<kind>:<key>`. The policy doors' OpenAPI descriptions and `PolicyRequest.cleanup_enabled` say so, and the console's
namespace page no longer says a table policy overrides a namespace's. Test case `namespace-hold-before-the-pass`.

## 9. The catalog's own reclaim doors

`maintenance/run` and `POST /v1/table/{id}/version/delete` delete with the catalog's credential, which never dies, so A1
does not cover them, and `version/delete` deleted under a legal hold ("authorization alone governs it"). **Decision:** both
are held as a pass is (`catalog.services.catalog_reclaims.held`): the history and each level's policy fence as reclaims,
the governing policy read under them, a held table refused 409 InvalidTableState (19) (lifting the hold is a separate,
audited act at a policy door), the victims marked before the first delete, and every hold confirmed before each cleanup
call, one call per 16 versions (`reclaim_marks.BATCH`). Of the two the brief offered, a confirm before each delete
request is not open (Lance issues a cleanup's deletes with no hook between them) and a capped credential minted for the
door works only where the catalog vends (`LANCE_VENDING_MODE=web_identity`); the per-batch confirm is the leaner that
holds everywhere. What it leaves, restated after §12 F2 and F4: a door frozen inside one Lance call past `HOLD` deletes at
most that call's batch (16 versions), all named by its own mark; but a later reclaim that finds that mark with the door's
hold gone and every file of its versions still standing removes it (F2 (ii)), so a door frozen before its first delete of
the call and resuming after a whole pass judged it leaves that batch half removed and unmarked, which a ref door would
then admit. It needs the catalog's process frozen more than 15 minutes inside one cleanup call, and is bounded to 16
versions the door was asked to delete; it is the one residual of a credential that never dies.
`maintenance/run` refuses a held table too, since it reclaims under the same bar. Reproduced first: under a table's hold
both doors reclaimed (versions [1, 2, 3] became fewer); now each answers 409. Named in "Not modelled".

## 10. A version's age is the catalog's clock

`version/create` committed a stock client's manifest as staged, and Lance's cleanup ages a version by the manifest's
`timestamp`. Reproduced through stock pylance against the live catalog, each staged manifest restamped by the writer
before it asks for its commit: a version stamped ten years ahead survived the erasure release's own cleanup at a one-second
retention (`erasure.reclaim_refs`, what `erasure_release._reclaim` runs; at retention 0 every superseded version goes
whatever its stamp, measured, so the stamp bites at any retention above 0), and one stamped ten years behind was taken by
a cleanup keeping a day. **Decision:** the catalog does not trust the writer's clock; the bytes it finalizes carry its own
commit time (`managed_versions.stage`, `manifest_bytes.with_commit_time`, field 7 of `Manifest` rewritten, every other
field and the sections before the message kept byte for byte, as `with_manifest_version` keeps them), and the digest the
author record names is of those bytes. Measured on pylance 13.0.0: the stock client reads its commit back as its own (its
write answers the version it asked for) and `versions()` reads the catalog's stamp, since the client matches the
transaction, never the stamp. Every other commit path is stamped in the catalog's process already (the one-call doors,
the restore, the floor raise and the disarm commit through pylance in the catalog; maintenance commits nothing). Test
`tests/integration/test_a_versions_age_is_the_catalogs_commit_time.py`, a case each way; both failed before the fix.

## 11. Starvation

`maintenance.reclaims.held_back{reason}` had no alert: a table held back pass after pass meets neither its retention nor
an erasure's deadline. **`MaintenanceReclaimStarved`** (`deploy/alerting/rules.yml`, warning) fires when held-back passes
of one reason occur in every hour for 24 hours, across the estate (§12 F5: many tables each held back briefly fire it as
one starved table does): a starved table is retried every `retrySeconds` (15 minutes), four an hour,
and a day is one of the seven an erasure release's grace gives (`catalog.erasure.releaseGraceDays`), leaving six to find
the writer or the mark. The counter names no table (an open set would be unbounded cardinality); the reason's log line
does. Its case is in `rules_test.yml`; promtool is not installed where this was written, so `make alert-rules-check` was
not run.

## 12. Found by a third review (2026-11-18), each FIXED

Each was reproduced first against the base of this change on moto, on the RED cases of
`tests/integration/test_a_tables_pass_deletes_nothing_a_door_answered_for.py` (14 of 25 cases failed on the base, each on
its harm assertion), then modelled, then fixed. Each guard's test case is in that file unless named; with its guard
removed it fails on moto and on the lane's RustFS 1.0.3, and passes with it (the removal table at the end).

### F1: a mark written as of the ref `main` was main's for no door

`maintenance/run?branch=main` passed `ref="main"` to the mark, `version/delete` passed `recorded_branch(...)` (`None`), and
every reader compared against `None`. Reproduced: a 5 MiB payload table, `maintenance/run?branch=main` with its `.lance`
and `_versions/` deletes answered `ExpiredToken` left a mark naming `"main"`; `tags/create` on version 1 answered 200 and
reading the tag failed `Failed to read blob source`, and no later pass ever cleared the mark. **Guard:** a ref is
normalised in ONE place, the mark itself (`reclaim_marks.VictimsMark.ref` and `Reclaim.ref`, each through
`version_retention.recorded_branch` as it is read or built), so no caller can name main two ways; the doors pass the ref
as the request names it. The other records keyed by a ref were read for the same defect: a history fence is keyed by
the table's uuid alone (every ref shares it), a clone's source tag is matched by `(version, branch or None)` and its
reference written through `recorded_branch` (`clone_sources._reference`), and the committed-file records are keyed by
the table and file (not this change's files: another agent's). None carried it. Case
`tag-5242880-data-files-and-manifests-maintenance-run-on-main`.

### F2: a mark naming versions that still stand outlived every pass

A mark was removed only when none of its versions stood; an empty dry run returned before looking; and nothing bounded
what a mark named. Reproduced: a planted `reclaim-victims.<hex>.json` naming versions 1 to 49 stood through two passes
and two appends, and `tags/create` on the head answered 409. §7.4's claim that a planted mark does no more than a RECLAIM
hold was false: a hold lasts 900 s, that mark for ever. **Decision: a mark is trusted only for what it can truthfully
say** (`lakehouse.reclaim_marks`):

- (i) **Judged as it is read** (`trusted`, by every door and every reclaim): a mark names only versions below its ref's
  head that no tag names, at most `BATCH` (16) of them, since one cleanup call removes at most that many. One that names
  anything else is ignored whole, logged `reclaim_mark_ignored` and counted (`lance.reclaim_marks.ignored`), and removed
  by the next reclaim. Case `planted-naming-the-head`.
- (ii) **Judged by every reclaim that finds it** (`Reclaim._judged`), once its writer holds nothing: a mark names its
  writer's hold token (`VictimsMark.holder`), and a take hands its holder every other live holder's token
  (`lakehouse.fences.Taken.holders`), so a mark whose writer may still delete is left alone (`MarkLive`, a model guard:
  `TablePassJudgeLiveMark.cfg` fails `HalfOnlyVictims` without it, two reclaims of one table at once, the second naming
  other victims). One naming no standing version is removed; one every file of whose standing versions Lance's dry run
  restricted to them names still stands is removed (`explain_cleanup_old_versions(versions=..., include_files=True)`:
  measured on pylance 13.0.0, it names each version's transaction file, data files, every blob sidecar
  (`data/<stem>/<id>.blob`, kind `data`) and its manifest, and leaves out a version whose manifest is gone), each file
  asked by a HEAD (`stored_objects.identities`), at most the files of 16 versions. Case `planted-naming-whole-versions`.
- (iii) **A reclaim that deletes nothing writes no mark**: every batch confirms its holds (`before_each`) BEFORE it writes
  the batch's mark, so a door whose confirm fails before its first delete has written none. Case
  `a-door-whose-hold-lapsed-before-its-first-delete` (a `version/delete` stalled before its first confirm until its holds
  lapsed: 409, and no mark).
- (iv) **A mark the policy now keeps and that is half removed stays, and is visible**: F5.

WHO CAN MAKE A DOOR REFUSE WHAT, AND FOR HOW LONG. A ref door refuses only versions a trusted mark names: versions below
the ref's head, untagged, at most 16 to a mark. A compromised maintain-tier credential can plant such marks (any number),
and so make doors refuse versions it could have deleted anyway (its vend deletes anywhere in the table's prefix but its
refs), until the next reclaim of the table judges them (ii): the next pass of the table, which a mark makes vend and
reclaim whatever its threshold, removes a mark naming versions whose files all stand; one that a hold keeps the table
under stands for as long as the hold, counted and visible (F5). It cannot make a door refuse the head, a tagged version, or
another table's, and a mark of more than 16 versions is ignored whole. A restored backup's marks are judged the same way.

### F3: a table renamed into a held namespace while a cleanup ran lost its history

`PolicyRead.namespace_uuids` was the chain the pass read before its holds, and `dataplane.rename_table` took no fence when
it changed the namespace. Reproduced: a hold on `lance_catalog$held`, the pass held at its cleanup's first delete, a
rename into `held` answered 200, and versions [1, 2, 3] became [3]. **Guard `RenameFence`:** a rename that changes the
table's namespace writes through the table's history fence as `HistoryWork.REFS` (`Fence.write_held`, so confirmed
before it writes), so it answers 409 ConcurrentModification (14), retryable, beside a reclaim
(`dataplane.rename_table`, the pointer move itself `_repointed`); a table whose claim records no uuid has no pass
(maintenance refuses it `unidentified`) and takes none. And the pass reads its chain again under its holds
(`table_pass.chain_now`: the location's claim, the `__manifest` rows of its chain, its bucket's project) and treats a
changed or unresolvable chain as `policy_changed` (`PolicyRead.changed`, `expiry.Chain`): a rename that landed before the
pass held its history is read there. Model: the `rename` door (a move into a namespace that holds), and
`TablePassNoRenameFence.cfg` fails `RetentionKept` without the fence. Cases `rename-into-a-held-namespace-at-its-
cleanups-first-delete` (the fence) and `rename-into-a-held-namespace-before-its-hold` (the chain read again).

EVERY WAY A TABLE'S GOVERNING CHAIN CHANGES UNDER A PASS, and the guard over each:

| Change | Guard |
| --- | --- |
| a rename into another namespace | `RenameFence` (409 beside a reclaim) and the chain read again under the holds |
| a rename within its namespace | changes no chain; no fence |
| a namespace's own move | no door moves a namespace (no rename verb on `/v1/namespace`) |
| a project re-assignment | a bucket's project claim is written once, by the warehouse that took it (`warehouse_records.bucket_project`); no door moves a warehouse, and the chain read again would see it |
| a deregister | the location's claim ends, so the chain read again does not resolve, and the pass deletes nothing; a later pass finds the task's table gone |
| a register | a new table, a new uuid, a pass of its own; a registration at a held location is refused by its claim |
| an undrop | restores into its own parent only (`trash_doors.require_its_own_parent`), and a pass reads the trash again under its hold (`expiry._dropped_since`) |

### F4: a policy set on a namespace or a project had no progress bound

A level's SET was refused while any pass beneath it deleted, and the project's fence is held by every deleting pass in
the project; during a backlog a legal hold kept answering 409 while the deletes it was meant to stop went on. Reproduced:
a pass reclaiming 39 versions, a namespace hold asked at its cleanup's first delete answered 409, and all 39 were then
deleted. **Decision: the door has preference.** A level's write refused by reclaim holders leaves its own `set-` holder
standing, unrenewed, as an INTENT (`HolderFence.taking(intent=True)`, from `policies._fenced` through
`Fence.write_held(..., intent=True)`), and answers 409 naming the bound. Only the catalog's own identity can write a
`set-` holder (the maintain vend's grant names `reclaim-` keys alone: the lane's `a-policy-doors-hold-at-its-namespace`
AccessDenied). No reclaim beneath the level is admitted beside an intent (SET excludes RECLAIM); a reclaim already deleting
confirms every hold and asks for an intent before each batch (`Hold.confirm_unchallenged`, from the pass's and the
catalog doors' `before_each`) and stops deleting there (`PolicyFencedError`: `skipped=policy_fenced`, retried after
`retrySeconds`), finishing the Lance call in flight. A pass's cleanup is batched as the catalog's reclaim doors' are, 16
versions a batch, in the one function every reclaim goes through. "At each renewal" adds nothing a batch's check does
not: no step between two batches deletes, and the call in flight cannot be interrupted, so the check is made where it
can stop something. An intent never retried lapses one hold (900 s) after it was left, and until then no reclaim beneath
the level starts: a stream of SETs starves passes, which is the point of a hold, and is visible as
`held_back{reason="policy_fenced"}`.

THE BOUND: a hold asked while reclaims run beneath its level lands after at most one batch (16 versions) of each reclaim
in flight, plus the door's retry. Measured: the pass of 39 versions held at its first delete, the hold's first 409, then
16 versions (one batch) deleted before the retry landed, on moto and on RustFS 1.0.3 alike, and none after its
200. Model: the intent is a SET holder a refused level door leaves; the doors are retried; `PolicyLands` (each level
door's write eventually lands, under weak fairness, the pass delivered again and again) holds in
`TablePassIntentLive.cfg` and fails in `TablePassNoIntent.cfg` (`EXPECT VIOLATION temporal`); the safety invariants hold
in every gate config unchanged. Test `test_a_hold_beside_a_long_reclaim_lands_after_at_most_one_batch`.

### F5: starvation alerted for no one table, and never for `marked`

A table held back as `marked` set no `skipped`, so it was not retried after `retrySeconds` (it added about one count a
day against a rule needing one in every hour), a held table's marks were never counted, and `sum by (reason)` fires for
many tables each held back briefly as for one starved table. **Guard:** `DatasetResult.marks_left` (the trusted marks the
reclaim left, an erasure's fork marks excluded, F7) makes `table_pass._dataset_due_at` retry the table after
`retrySeconds`, and a held table's standing mark is counted too (`expiry._kept_whole`; the probe looks for one whatever
the policy). `MaintenanceReclaimStarved` says it is estate-wide. ONE gauge, the age of the oldest standing mark the worker
has seen (`maintenance.reclaims.mark_oldest`, one series, no table label, the store's clock off the probe's listing,
`reclaim_marks.oldest`; the table in `maintenance_victims_marked`), and `MaintenanceReclaimMarkStale` when it passes a
day, with its cases in `deploy/alerting/rules_test.yml` (M24 h3). promtool is not installed where this was written, so
`make alert-rules-check` was not run; the rules follow their neighbours' conventions (the unit appended to the series
name, as `maintenance_queue_oldest_seconds`). Case: every `pass` case of
`test_a_ref_answered_200_names_a_version_a_reader_can_read_whole` asserts the pass that left a mark is due again within
`retrySeconds`.

### F6: guards whose tests stayed green without them

- (a) the catalog's reclaim doors' per-batch confirm (`catalog_reclaims.marked`, `before_each=held.confirm`): case
  `held-between-its-batches` of `test_the_catalogs_own_reclaim_doors_delete_nothing_a_hold_keeps` (each door over 19
  versions, frozen after its first batch of 16 with its renewals until its holds lapsed; a namespace hold then answers
  200; the door resumes and must delete nothing more: 409 (14), versions 17 to 19 kept).
- (b) finishing a dead cleanup's marks below the threshold (`or target.marked` in `expiry._versions_due`): every case of
  `test_a_ref_answered_200_names_a_version_a_reader_can_read_whole` now ends with a pass under
  `MAINTENANCE_MIN_VERSIONS_TO_EXPIRE=100`, which must remove version 1 and leave no mark.
- (c) the mark check in `clone_sources.take`: case `clone-5242880-data-files-and-manifests-pass` (a shallow clone of the
  half-removed version 1 registered through the door: 400, and with the check removed the registration answers 200 and
  the clone's blobs fail to read). Its file check is gone with F7.

### F7: one way to reclaim, and a ref door's check that does not grow with the table

The erasure's verification and release cleanups wrote no mark, and the doors' file listing (`whole_versions`,
`stored_objects.present`) listed whole leading-prefix shards of the ref's `data/`: a tag create cost 1.6 s at 10,000
fragments whatever the version's size. **One way to reclaim:** every reclaim in the estate goes through
`lakehouse.reclaim_marks.Reclaim.run`: a pass (`expiry._reclaim_versions`), `maintenance/run` (`catalog.services.
maintenance.run_gc`) and `version/delete` (`delete_versions`), whose `marked` is now required, and the erasure's
verification and release (`erasure.reclaim_refs`, held as a reclaim or a release). A pass holding a vend must hold the
table's history (`ExpiryTarget._deletes_only_held`), so the unheld cleanup is gone. No other code in the catalog,
maintenance or the library calls Lance's cleanup on a governed table (grep, 2026-11-18): `manifest_reclaim` cleans the
catalog's own `__manifest` index, which no ref door names; a test's direct cleanup is a test's.

THE ERASURE'S FORK POINTS. Measured on pylance 13.0.0: a version a branch was cut from keeps its manifest through a
cleanup, but an unrestricted cleanup deletes the files only it names (the erasure's subject fragment, and its
transaction), leaving it standing over deleted files (the erasure reports it `dangling`); a cleanup restricted to the
dry run's victims keeps them, and one that names the fork version explicitly deletes them as the unrestricted one does.
So the erasure's reclaim names its ref's fork versions that no tag pins beside its victims (`Reclaim.run(forks=True)`):
it takes the subject's bytes from them as before, and marks them, so no door names a version standing over the files it
took, for as long as the branch keeps its manifest. Such a mark is not counted as left (no reclaim can finish it, and it
is the erasure's answer, not a reclaim starved). A cleanup whose delete the store refused is now a failed `history:`
surface of the erasure (it was reported `reclaimed`), so the obligation stays open for the next verification.

THE MODEL'S VERDICT: with every reclaim marking before it deletes (after its confirm) and unmarking only after a cleanup
call with no failed delete, `RefsWhole` and `HalfOnlyVictims` hold with the ref doors reading the mark alone: the model's
doors never listed a file (`Admissible` reads `MarkedVs`), `TablePass.cfg`, `TablePassTwo.cfg`, `TablePassLive.cfg`,
`TablePassPolicies.cfg` and `TablePassWide.cfg` hold, and `TablePassNoVictimMark.cfg` shows the mark is the guard. The
model's cleanup deletes only files of its victims, which is what the restriction by `versions` makes true; the erasure's
fork points, the one deletion outside that, are now among its marked victims. So the listing is DELETED
(`whole_versions.py` and `stored_objects.present` with it): `require_whole` and `clone_sources.take` read the version's
manifest (the checkout) and the trusted marks (`reclaim_marks.refused`: a read per mark, the ref's head and its tags),
whatever the version's or the ref's size. A store that cannot answer a mark's read is a 503
(`test_a_ref_door_answers_a_store_that_cannot_say_whether_a_version_is_marked_503`). What a store loses on its own, a
file no cleanup deleted, is no longer judged by a ref door: no cleanup's guard covers a store's loss, and the
committed-file records (`maintenance.services.committed_files`) report one.

THE COST, before and after: before, 0.16 s at 1,000 fragments and 1.6 s at 10,000 (16 listings, measured by the second
review on RustFS 1.0.3); measured again here on the lane's RustFS 1.0.3 at 10,000 fragments, the listing took 1.28 s and 1.43 s
and the check that replaced it (the door's fence take, the version's checkout and the trusted marks) 0.04 s twice: no request names a
data file, so the cost is the manifest's read and a read per mark whatever the ref holds (100,000 fragments not measured).

### F8: the maintain session policy did not fit at the longest bucket names

At two 63-character bucket names the maintain tier's session policy was 2,144 characters, past STS's 2,048, so such an
estate's passes reclaimed nothing. **Restructured:** a reclaim's holder (`reclaim-<32 hex>.json`) and a cleanup's mark,
now `reclaim.<32 hex>.json` (`history_fences.VICTIMS_MARK = "reclaim"`), are reached by ONE grant,
`<fence>/reclaim?<32 ?>.json`: one character between `reclaim` and the 32, so beside the two it reaches only names of
their length with another separator, which no take judges (a holder's name needs the `-`, a mark's the `.`) and the
catalog's next take sweeps once they have stood a hold; every longer name, `reclaim-x/erasure-release.<id>.json` among
them, stays refused, and nothing the policy denied is granted (the lane's grant table, below). Measured with
`vending.counted_length` (2026-11-18): 1,964 characters at the worst case, two 63-character bucket names (the control and
the data bucket) and a catalog-made table at `<bucket>/<uuid>`, the deepest location the catalog makes (ADR 0173 §5), a
margin of 84; 1,504 under a 17-character bucket. A KMS key's grant adds 230 (2,194 at 63 characters with a key ARN of 75)
and a control root may hold a key prefix, so **a warehouse whose tables' maintain policy would not fit is refused at
registration** (`vending.require_a_maintain_policy_that_fits`, from `warehouses.validated_ids`, 400 InvalidInput (13),
naming the limit and the bucket's, the control root's and the key's lengths), before any table its passes could not
reclaim is made. A table registered at a deeper location is refused its vend alone, 400 SessionPolicyTooLong, as a long
branch name is. Pinned by
`services/catalog/tests/test_the_catalog_signs_only_with_sessions_its_token_is_exchanged_for.py::test_a_warehouse_whose_tables_maintain_policy_sts_would_refuse_is_refused_at_registration`
(the two lengths) and by the lane's real vend at 63-character names
(`tests/integration/test_a_maintain_vend_plants_nothing_the_fence_trusts.py`: its bucket is 63 characters, the control
and the data bucket both, its table at the bucket's root): the store took the exchange, and the grant table answered as
before: the vend's own reclaim holder and its cleanup's mark 200, a policy door's `set-` holder at its namespace and a
reclaim in another warehouse's directory AccessDenied, a mark below a subdirectory and one where the catalog writes one
AccessDenied.

### The smaller ones

- **The truncated dry run's superset** dropped every version number any tag named on any branch, so a tag on `(b, 5)`
  left main's untagged version 5 out though main's cleanup deletes it. Fixed: a tag pins only its own branch's version
  (`reclaim_marks.pins`, `recorded_branch(tag.branch) == ref`), and a version a branch was cut from is left out as Lance
  leaves it. Test `packages/lakehouse/tests/test_a_truncated_dry_run_marks_every_version_the_cleanup_may_take.py` (the
  bound lowered to 1).
- **The project's policy fence** is found by one function, `policy_fences.levels`, from the pass (`table_pass._expire`)
  and the catalog's reclaim doors (`catalog_reclaims.held`) alike, each on the control root its settings name: the
  catalog's `registry_root` and maintenance's `resolved_control_root` both render from the chart's one
  `objectStore.bucket` (`LANCE_REST_ROOT`, `MAINTENANCE_S3_BUCKET`). The project door is driven through the real door and
  a real pass: case `project-hold-at-its-cleanups-first-delete` (the bucket a project's warehouse, its door answering 409
  while the pass deletes).
- **The catalog's reclaim doors' residual** after F4's batching: still one batch, now also after F2's judgement; §9 says
  what it leaves.

### The removal table

Each guard removed alone (`git checkout` restoring it after), its tests run on moto and on RustFS 1.0.3:

| Guard removed | Where | Cases that failed without it (moto and RustFS alike) |
| --- | --- | --- |
| F1, the ref normalised in the mark | `reclaim_marks.VictimsMark.ref`, `Reclaim.ref` | `tag-5242880-data-files-and-manifests-maintenance-run-on-main` |
| F2 (i), a mark judged as it is read | `reclaim_marks.trusted` | `planted-naming-the-head` |
| F2 (ii), a whole mark removed | `reclaim_marks.Reclaim._judged`, `_untouched` | `planted-naming-whole-versions` |
| F2 (iii), the confirm before the mark | `reclaim_marks.Reclaim.run` | `a-door-whose-hold-lapsed-before-its-first-delete` |
| F2, the writer's hold (`MarkLive`) | `Reclaim._judged` | none (model only: `TablePassJudgeLiveMark.cfg`) |
| F3, `RenameFence` | `dataplane.rename_table` | `rename-into-a-held-namespace-at-its-cleanups-first-delete` |
| F3, the chain read again | `table_pass.chain_now`, `PolicyRead.changed` | `rename-into-a-held-namespace-before-its-hold` |
| F4, the door's intent | `policies._fenced`, `HolderFence.taking` | `test_a_hold_beside_a_long_reclaim_lands_after_at_most_one_batch` |
| F4, a pass meeting the intent | `expiry._reclaim_versions` (`Hold.confirm_unchallenged`) | the same |
| F5, a marked table retried | `table_pass._dataset_due_at` | the five `pass` cases of `test_a_ref_answered_200_names_a_version_a_reader_can_read_whole` |
| F6 (a), the per-batch confirm | `catalog_reclaims.marked` | both `held-between-its-batches` cases |
| F6 (b), finishing a mark below the threshold | `expiry._versions_due` | all six cases of `test_a_ref_answered_200_names_a_version_a_reader_can_read_whole` |
| F6 (c), the clone source's mark | `clone_sources.take` | `clone-5242880-data-files-and-manifests-pass` |
| F7, a ref door's mark | `dataplane.require_whole` | the five tag and restore cases |
| the truncated superset's tags | `reclaim_marks.pins` | the truncation test (a local root; not a store case) |

## What the model assumes beyond §4

- Lance's cleanup reads its refs, head and manifests once and deletes against that reading (`cleanup.rs` at v13.0.0);
  its own rules (tags, branches and the head are kept; a file only removed versions name, or one no manifest names past
  7 days, goes) are as `lance_docs/lance/guide/read_and_write.md` § cleanup states them.
- A paged or stale listing is a reading that misses what landed after it: the model's `PList` and `PBList` are
  snapshots, so a manifest committed while the cleanup lists is either read or later, and nothing is decided on it.

## Not modelled

A drop, undrop and purge (TableLifecycle.tla); an erasure release, whose RELEASE excludes every kind (Erasure.tla), and
whose cleanup is the one marked reclaim (§12 F7), its fork points marked; the staged manifests and the committed-file records a pass removes (above); the floor
raise, a catalog commit like any writer's; the orphan check, which deletes nothing; a branch's own cleanup (each branch is
a ref of its own with the same steps, its mark naming the branch); two tables sharing a data directory, which the
directory's claim excludes (`data_directories.claimed_by`); a version expiring by its writer's clock (ages change at any
time here, which over-approximates every clock; `version/create` stamps the catalog's own, §10); the catalog's own reclaim
doors, `maintenance/run` and `version/delete` (§9), passes of the modelled shape signed by a credential that never dies,
held by a confirm of every hold before each batch of 16 versions (§9 states what a door frozen inside one batch leaves);
the batches themselves (the model's pass is one batch; each of the code's repeats its step); a mark a maintain-tier vend
plants, which F2 (i) ignores. The project level's door is modelled as the namespace's (`projpolicy`), the same code path
at its own fence, and an integration case drives it through the real door and a real pass (§12, the smaller items).

## What an operator observes

- `maintenance.reclaims.held_back{reason}` counts each dataset whose pass kept what it would reclaim because a ref write
  held the table's history (`history_fenced`), a policy write held a fence above it (`policy_fenced`), its policy changed
  since the pass read it (`policy_changed`), or a cleanup's victims stay marked (`marked`); each is passed again after
  `retrySeconds`. The log lines `maintenance_history_fenced`, `maintenance_policy_fenced`,
  `maintenance_policy_changed_mid_pass` and `maintenance_victims_marked` name the table. `MaintenanceReclaimStarved`
  (`deploy/alerting/rules.yml`) warns after a day of held-back passes in every hour (§11).
- A restore beside a running reclaim answers 409 ConcurrentModification (14) where it answered 200; a table's
  `policy/set` and `policy/delete` beside one answer 409 (14); a namespace's or a project's `policy/set` and
  `policy/delete` beside a reclaim of a table it governs answer 409 (14), retryable.
- A hold (`cleanup_enabled` false) on a namespace or a project keeps the whole history of every table beneath it, a table
  whose own record keeps less among them: the maintain vend is refused for it (409 InvalidTableState, 19), the
  catalog's `maintenance/run` and `version/delete` answer 409 (19) for it, and the effective record a pass reads names the
  holding level under `held_by`.
- A tag, a branch from a version, a restore or a clone registration naming a version a cleanup marked, or one missing a
  file it names, answers 409 InvalidTableState (19), logged `ref_on_a_version_being_reclaimed`, until the next pass
  removes the version; a clone's registration answers 400 InvalidInput, as it answers a source version no longer kept
  (`table_bases._tag`). A store that cannot say whether the version is whole answers 503.
- A maintain-tier credential expires within 900 s whatever `vending.ttlSeconds` is, and its session policy reaches the
  cleanup's mark and a reclaim's holder in the policy fences of its own warehouse's namespaces and its own project.
- §12's: a mark naming a version at or above its ref's head, a tagged one or more than 16 is ignored by every door and
  removed by the next reclaim (`reclaim_mark_ignored`, `lance.reclaim_marks.ignored`); a mark whose versions stand whole
  is removed by the next reclaim (`reclaim_mark_judged_whole`); a level's policy door refused beside a reclaim answers
  409 naming the bound and leaves its intent, and its retry lands once the batch in flight ends; a rename into another
  namespace answers 409 (14) beside a reclaim; a pass that left marks is passed again after `retrySeconds`, counted under
  a hold too, and the oldest mark's age is `maintenance.reclaims.mark_oldest` (`MaintenanceReclaimMarkStale` past a day);
  an erasure's cleanup that the store refused a delete of is a failed `history:` surface; a warehouse whose tables'
  maintain policy would not fit is refused at registration (400, 13).
- A version a stock client commits through `version/create` carries the catalog's commit time, whatever its writer's clock
  said (§10).

## The four facts

- **GitOps**: met. No new chart value: the holds (900 s) are the fences', the retry is `retrySeconds`, the cap ties the
  maintain credential to the hold rather than to a value an operator could set past it, the policy fences live on the
  control root as every lease does (`control_prefixes.POLICY_FENCES`, a lease a backup skips), and the starvation
  alert's day is derived from the chart's `catalog.erasure.releaseGraceDays` (§11).
- **Cloud native**: met. Every exclusion is a record on the store any replica reads; no process holds a lock another
  would not see; the policy fence is the existing per-holder fence (`HolderFence`), a subclass, not a third mechanism.
- **Lance-namespace/table-idiomatic**: met. Cleanup is Lance's own `cleanup_old_versions`, restricted by its own
  documented `versions` to the victims its own dry run (`explain_cleanup_old_versions`) names; the restore is Lance's own
  `Restore`; the stock client's path is unchanged (the version it commits carries the catalog's commit time, which it does
  not read back to decide its commit).
- **Zero trust**: met, with one stated widening. The maintain credential cannot outlive the holds that license its
  deletes; a door's answer rests on the store's conditional writes and a renewal just before its write, never on timing;
  the mark is trusted only at the key and shape the catalog defines, and a planted one only makes a door refuse. The
  widening: the vend may hold a reclaim in the policy fence of any namespace of its own warehouse, not only its own
  chain's, since one grant per level does not fit STS's 2,048 characters (§7.2); what it can plant there delays a policy
  write there by one hold and nothing else.

## Consequences

- A port of the pass keeps: the hold before the cleanup, the policy read again under the hold, and every door that
  pins or brings back a version holding the history and judging the version whole. A change to that order changes this
  model first.
- `make lifecycle-rustfs-check` runs the finding tests on RustFS; the offline suite runs them on moto.
- `make tla-check` runs this model's gate and expected failures in about 35 s.
- A port of the policy doors keeps their fences, `write_held` and a level's intent; a port of a ref door keeps
  `require_whole`'s one line, the trusted mark; a port of any reclaim calls the one marked reclaim
  (`reclaim_marks.Reclaim`), never Lance's cleanup directly.
