# 0246. A reclaim condemns before it deletes, and a door pins before it reads (2026-11-18)

**Status:** proposed. The model-first step of a redesign of what protects a reclaim's deletes
([ADR 0243](0243-a-tables-pass-deletes-nothing-a-door-answered-for.md)). No production code changed: the model, its
configs and the measurements below are the evidence the code would be written to. **It carries one product decision for
the owner (§9).** `specs/maintenance/TablePass.tla` stays the model of the code until the code changes.

## Context

What a reclaim may delete is protected today by leases: the history fence's RECLAIM against REFS, renewed and confirmed;
a policy fence at each namespace level and the project, with a door's intent; victims marks that are trusted, judged and
aged; fork marks; a store session per catalog batch; a maintain credential capped to the hold (ADR 0243 §2 to §13). Four
review rounds each found reproduced blocking defects, 17 of the 23 later ones in marks, leases or policy fences. The
alternative this ADR checks replaces all of them with two write-then-read orders on records nobody but the catalog
writes, so that no lease, renewal or clock enters the argument for what a reclaim may delete.

## Decision (proposed)

### 1. The design as the model has it

**The condemned record.** A table carries one record on the control root (`_condemned/table-<digest>.json`, beside the
history fence's directory), written only by the catalog, each write conditional on the ETag it read (If-Match). An entry
names a version by the **identity** of its ref (main, or one incarnation of a branch: the uuid Lance lists last in the
branch's `branch_identifier`, as `VictimsMark.ref_id` does today), the version's number, the **batch** that wrote it (an
id minted per batch, not per reclaimer) and its state: `c` (condemned) or `g` (its batch passed its pin read and its
policy read; deletion may have begun under it). A ref door refuses a version an entry names. An entry is dropped only
(a) by the catalog's rewrite of the record once the version's manifest is gone or its ref's identity no longer stands,
or (b) by its own batch before that batch deleted anything (a pinned version, or a hold). **Condemned is final** while
the manifest stands: the next reclaim finishes it.

**A reclaim batch** (at most 16 versions; every reclaim in the estate: a pass, the erasure's verification and its
release's reclaim; `maintenance/run` enqueues a pass). The reclaimer calls the catalog once per batch; the catalog:

1. **condemns**: writes the batch's entries, state `c` (the versions past retention on one ref, plus every standing
   version of that ref an entry already names: a dead batch's, `version/delete`'s);
2. **reads the pins**: lists the table's tags (user tags and `rask-pin-*`); a version one names is excluded, unless an
   entry of **another** batch names it `g`;
3. **reads the policy** level by level (the table's record, each ancestor namespace's, the project's; a hold at any level
   keeps everything, ADR 0243 §8 unchanged);
4. **answers**: held, its own entries removed (nothing was deleted under them); else one conditional write that removes
   its own entries of excluded versions, sets the rest `g`, and deletes the pins standing on another batch's `g` versions
   (a door that pinned one of those after that batch's pin read reads its entry and refuses, so the pin protects
   nothing; Lance keeps every tagged version, so leaving it would keep a half-removed version so for good). It answers
   "go" with the versions.

The reclaimer then runs `cleanup_old_versions(versions=batch, error_if_tagged_old_versions=False)` under its maintain
vend, which reaches nothing on the control root.

**A ref door** (tag create and update, branch create, restore, a clone's source tag; the erasure's release for where its
branches come back): (1) writes a reserved Lance tag `rask-pin-<32 hex>` on the version, (2) reads the record and
refuses a version an entry names (409 InvalidTableState, 19), (3) acts, (4) deletes its pin. The tag doors refuse a
caller's tag named `rask-pin-*` and a delete of one.

**A policy door** (set and delete, at the table, a namespace, the project; a rename into another namespace) writes,
then reads the record, and answers 200 at once naming the versions an entry names. No fence, no intent, no 409.

**`version/delete`** is the spec's operation (`lance_docs/ns_catalog/spec.yaml`, BatchDeleteTableVersions: "deletes
version tracking records, NOT the actual table data"): it condemns the versions it names (never a head) and answers;
the next reclaim finishes them unless a hold keeps them.

Each side writes, then reads the other's write, so on a linearizable store at least one sees the other.

### 2. The guarantees

`specs/maintenance/Reclaim.tla`, two TLC workers on four cores shared with other agents, 2026-11-18. The gate configs:

| Config | Scope | Distinct states | Time |
| --- | --- | --- | --- |
| `Reclaim.cfg` | one reclaim delivered again and again, a tag, a restore, Lance's reads in either order, two faults | 105,239 | 3 s |
| `ReclaimWriter.cfg` | a restore and a writer's commit in flight, one fault | 84,220 | 4 s |
| `ReclaimPolicies.cfg` | the table's, a namespace's and a project's hold, a rename into a held namespace, `version/delete`, a tag, one fault | 69,782 | 4 s |
| `ReclaimTwo.cfg` | two reclaims of one table at once, a restore | 213,643 | 6 s |
| `ReclaimTwoRefs.cfg` | two reclaims at once, each of main or branch b, commits on b, a tag, one fault | 121,092 | 7 s |
| `ReclaimBranches.cfg` | branch b deleted and made again (another identity, repeating version numbers), commits on b, one fault | 107,467 | 4 s |
| `ReclaimBranchesTag.cfg` | the same with a tag on any version of main or b | 347,265 | 12 s |
| `ReclaimFork.cfg` | the erasure's fork cleanup of b over main 1, b deleted, main's reclaim finishing it, a tag, one fault | 88,918 | 5 s |
| `ReclaimRelease.cfg` | the release beside a writer, a tag and reclaims, two faults (§3) | 483,150 | 13 s |
| `ReclaimSweep.cfg` | a dead door's pin swept (A3'), two faults | 145,186 | 4 s |
| `ReclaimTagsFirst.cfg` | no explicit pin read, Lance reading tags first (measured order), one fault | 37,667 | 2 s |
| `ReclaimLive.cfg` | invariants and `EventuallyWhole`, `PolicyLands`; a tag, a restore, a hold, one fault | 151,160 | 30 s |
| `ReclaimLiveDeadPin.cfg` | `EventuallyWhole` with a door dying on its pin over a half-removed version, two faults | 2,368 | 2 s |
| `ReclaimWide.cfg`, ON DEMAND | a tag, a restore, a writer and `version/delete`, two faults | 41,614,322 | 19 min 32 s |
| `ReclaimTwoWide.cfg`, ON DEMAND | two reclaims, a tag and a restore | 4,739,236 | 2 min 10 s |
| `ReclaimTwoFaults.cfg`, ON DEMAND | two reclaims and a restore, one fault | 1,405,635 | 39 s |
| `ReclaimBranchesWide.cfg`, ON DEMAND | branches with a tag, one fault | 2,414,891 | 63 s |
| `ReclaimForkWide.cfg`, ON DEMAND | the fork cleanup with a tag and a restore, one fault | 2,534,307 | 59 s |

Every must-fail config below fails as expected, each in at most 3 s (53 to 30,192 states). Every gate config checks
`RefsWhole`, `InFlightKept`, `RetentionKept`, `HalfOnlyCondemned`, `DeletesCondemned` and `RefusedOnlyChosen`.

The invariants and properties:

| Property | In words | Ordering rule or guard | Must-fail config |
| --- | --- | --- | --- |
| `RefsWhole` | main's head, every tag and branch b's head stand with every file they name | PinFirst: a door pins, then reads the record | `ReclaimDoorReadsFirst` |
| | | CondemnFirst: a batch condemns before its pin read and before any delete | `ReclaimNoCondemn` |
| | | ExplicitPins: the catalog reads the tags itself, before calling Lance (Lance's own order is not in `lance_docs/`) | `ReclaimNoExplicitPins` (Lance reading manifests first) |
| | | GoOnly: only another batch's `g` entry licenses ignoring (and deleting) a pin | `ReclaimGoAny` |
| | | ReleasePins: the release pins where its branches come back before it deletes them | `ReclaimReleaseUnpinned` (two faults: ADR 0243 §13's residual) |
| | | A3' (only where a pin sweep is adopted): a sweep takes only a dead door's pin | `ReclaimPinSweepLive` |
| `RetentionKept` (new wording) | a hold answered 200 keeps every standing version it did not name as condemned | PolicyAfterCondemn: a batch reads the policy after it condemns | `ReclaimPolicyBeforeCondemn` |
| | | HoldWritesFirst: a policy door writes, then reads the record | `ReclaimHoldReadsFirst` |
| `HalfOnlyCondemned` | a version standing over deleted files is named by an entry | GCOnlyGone: an entry is dropped only once its manifest or its ref's identity is gone | `ReclaimEntryDropped`, `ReclaimForkEntryDropped` (the erasure's fork point) |
| | | KeepAfterDelete: a batch whose deletes the store refused keeps its entries | `ReclaimRollbackAfterDelete` |
| `DeletesCondemned` | every delete is of a version of its own ref that a reclaim chose, `version/delete` named or the erasure's fork cleanup took, and an entry names it as it goes | IdentityKeys: entries keyed by the ref's identity, not its name | `ReclaimNamedEntries` |
| `InFlightKept` | a commit in flight keeps its staged files | A2, AgeFloor (kept) | `ReclaimWriterOutlivesAge` |
| `RefusedOnlyChosen` | a door refuses only a version some reclaim, `version/delete` or fork cleanup chose | IdentityKeys | (held in every gate config) |
| `EventuallyWhole` (liveness) | once faults stop, every condemned version is finished, unless a hold keeps it | GoUnpins: a batch deletes the pins on another batch's `g` versions | `ReclaimLiveNoUnpin` (EXPECT VIOLATION temporal) |
| `PolicyLands` (liveness) | every policy door lands | none needed: no policy door is refused | (held in `ReclaimLive`) |

Two further configs hold and are evidence rather than gates: `ReclaimTagsFirst` (no explicit pin read, Lance reading its
tags before its manifests, as measured on pylance 13.0.0: it holds, so the explicit read is kept only because the docs do
not promise that order) and `ReclaimSweep` (a sweep of dead doors' pins under A3').

### 3. The erasure interplay

What the erasure needs from the history fence's kinds (`history_fences._excludes`), read from `Erasure.tla` and the code:

| Exclusion today | What it closes | Under this design |
| --- | --- | --- |
| RECLAIM vs REFS, BRANCH (A3) | a ref written between a reclaim's read of the refs and its deletes (`PinsNameRetainedVersions`) | condemn and pin (`Reclaim.tla`, `RefsWhole`); `Erasure.tla`'s A3 stays as the abstraction it refines |
| RELEASE vs REFS, BRANCH (G1) | a ref put on a residual while the release plans, deletes, recreates and reclaims (`ErasedOnceReleased`) | **stays**: the release must take every ref on a residual, and a door in flight would add one after its plan |
| SCHEDULE vs REFS, BRANCH (G7), the finish under SCHEDULE (G10), the door's confirm (G11) | a ref's creator told of the release before it runs (`ACreatorIsToldBeforeTheRelease`) | **stays** |
| BRANCH vs BRANCH | two branch creates judging the nesting of names (`a`, `a/b`) | **stays** |
| RELEASE vs RECLAIM | a reclaim between the release's delete of a branch and its recreate takes the version the branch comes back at | **replaced by the release's pin** on each recorded version, written before its first delete and removed at its finish |

`specs/catalog/CondemnedErasure.tla` extends `Erasure.tla` and replaces, by TLC definition overrides, the three places a
reclaim and the release wait for each other (`VReclaimRead`, `Cleanup`, `RClaim`) and `Pinned` (the release's pin):
`CondemnedErasure.cfg` (Erasure.cfg's gate, every invariant and liveness property) holds, 76,065 states, 76 s;
`CondemnedErasureRefsInTheGrace.cfg` holds, 195,397, 61 s; `CondemnedErasureRestore.cfg` (a restore moving main's head
off the recorded version, one failure) holds, 1,600; `CondemnedErasureUnpinned.cfg` (no release pin) fails
`PinsNameRetainedVersions`: with reclaims off RELEASE the residual of ADR 0243 §13 happens **in one tick**, with no crash
(trace: `RClaimC RDelete RMark Restore CleanupC RRecreate`), so the pin is required, not optional. The same scenario with
the fence (`Erasure.tla`, the same constants) holds. In `Reclaim.tla`, `ReclaimRelease` (the release beside a writer, a
tag and reclaims, two faults: the tick dying between its delete and its recreate, a reclaim cut short) holds, and
`ReclaimReleaseUnpinned` fails `RefsWhole`.

**So the history fence stays, with four kinds** (REFS, BRANCH, SCHEDULE, RELEASE), held by the ref doors, the
verification's scheduling, the release tick and the catalog's finish; **RECLAIM is deleted**, with every reclaim's take,
renewal and confirm, the maintain vend's grant on the fence directory, and the RELEASE-excludes-RECLAIM rule. Two rules
the erasure adds to the release: it pins each version it records for a branch's return before its first delete (closing
§13's residual), and it takes a `rask-pin-*` tag on a residual as one of the refs it takes (a door that died there left
it; under RELEASE no door is in flight, and one whose hold lapsed is refused by its confirm, G11, before it acts: that
rests on the fence's existing A5, which stays for the erasure's notices and this alone). The second is argued, not
modelled.

### 4. Assumptions: which of ADR 0243's go

| ADR 0243 | Here |
| --- | --- |
| A1, a reclaimer's credential dies no later than its hold | **gone**: no hold. A paused reclaimer resuming days later deletes only versions an entry names `g`, which no door has admitted since |
| A2, a commit in flight lands within the unverified age (7 days) | **stays** (`ReclaimWriterOutlivesAge`) |
| A3, a ref write holds REFS against RECLAIM | **gone**, replaced by PinFirst and CondemnFirst |
| A4, a fence take is atomic | **gone for reclaims**; stays for the erasure's kinds (`Fence.tla`) |
| A5, a door writes within HOLD of its confirm | **gone for every delete a reclaim makes**; stays for the erasure's notices and the release's take of a residual's pin |
| new B1 | linearizable writes and listings on the control root and the table's `_refs/` (measured, §5) |
| new B2 | only the catalog writes or deletes a pin or the record (measured, §5) |
| new A3', only if a pin sweep is adopted | a sweep takes only a pin whose door can no longer act |

### 5. Measurements (RustFS 1.0.3 `scripts/rustfs_server.sh`, pylance 13.0.0, 2026-11-18)

- **Pin read-after-write**: 1,000 trials, a `rask-pin-<32 hex>` tag created by one dataset handle and listed at once by
  another: missed 0; deleted and listed again: still listed 0. Lance accepts the name; a stock `tags.list()` shows it as
  any tag (`{'branch': None, 'version': 1, 'created_at': …, 'manifest_size': 409, 'metadata': {}}`); a pin on a branch's
  version is taken (`{'branch': 'b', 'version': 2, …}`).
- **Record read-after-conditional-write**: 1,000 trials, a put under If-Match then a read by another client: stale 0; a
  put under a stale ETag answered `PreconditionFailed`.
- **Latency and requests** (median, p99): tag create 7.1 ms, 18.8 ms, 3 requests (HEAD and HEAD of the version's manifest,
  PUT `_refs/tags/<name>.json`); tag delete 17.2 ms, 46.1 ms, 2 requests (HEAD, a bulk `DeleteObjects`); `tags.list` 3.2
  ms, 1 listing; the record's conditional write 7.2 ms, 16.1 ms; its read 1.9 ms, 4.7 ms.
- **Contention**: 16 concurrent condemns of different batches (16 entries each) on one table's record, read and
  If-Match write, retried: 976, 1,343 and 1,235 ms wall in three runs, 95, 98 and 96 retries in all, at most 13 for one
  batch, 256 of 256 entries every time.
- **Lance's cleanup order** (`cleanup_old_versions(versions=[1])` on the wire): list `_refs/branches/`, list
  `_refs/tags/`, list `_versions/`, read each manifest, then the deletes. `lance_docs/` states that tags, heads and branch
  dependencies are kept and that `versions` limits what is removed (`guide/read_and_write.md` § Cleanup old versions,
  `guide/tags_and_branches.md` "Tagged versions are exempted"), not the order. Held at its first `_refs/` listing, a tag
  written on version 1 then: the cleanup raised `Cleanup error: 1 tagged version(s) have been marked for cleanup`, version
  1 kept, the tag reads. Held at its first `_versions/` listing (after its tag read), a tag written then: version 1
  removed, the tag's read fails `Not found`. So a tag written after Lance's own tag read is not protected: the explicit pin
  read before the call is what the design rests on. With version 1 pinned and named in `versions`: the default raises;
  with `error_if_tagged_old_versions=False` it returns, removing nothing, version 1 kept, which the design passes.
- **Who can plant or remove a pin** (a non-root user's AssumeRole with the catalog's own session policy, a 63-character
  bucket): write tier: put a pin AccessDenied, delete the catalog's pin AccessDenied (read 200, a data file put 200);
  maintain tier: put AccessDenied, delete AccessDenied (the explicit Deny on `_refs/*`); the pin stood. The pin's trust
  rests on the store's policy (no vend reaches `_refs/` to write), the catalog's identity alone writing there, and the tag
  doors refusing the `rask-pin-` prefix.
- **The maintain session policy** with no control-root grant, `vending.counted_length`, two 63-character buckets: 701 of
  2,048 characters (3 statements), against 1,964 today (7 statements); 566 under an 18-character bucket. A warehouse's
  registration check (`require_a_maintain_policy_that_fits`) and the register door's fit (R6) keep their job for KMS keys
  and many bases, with a margin of 1,347 where today's is 84.

### 6. Requests and latency

Today's, counted on the wire through `tests/integration/store_requests.py` with the real catalog app on moto (a scratch
test beside `test_a_tables_pass_deletes_nothing_a_door_answered_for.py`'s estate, deleted after); "fence" is every
request under `_history_fences/` or `_policy_fences/`:

| Operation | Today: all (table, control root, of which fence) | Proposed |
| --- | --- | --- |
| tag create | 28 (5, 23, 7), 715 ms | +6 (pin 3, record read 1, unpin 2), about +26 ms on RustFS: 34 while ref doors keep the erasure's fence take; 27 if a later step drops it (§11 Q2) |
| restore | 64 (24, 40, 7), 1,535 ms | the same +6: 70, or 63 |
| one reclaim batch (`version/delete` of one version, its cleanup) | 90 (37, 53, 26), 2,626 ms | the 26 fence and mark requests become 4 to 5 (record read, condemn write, tag listing, promote write, a pin delete where one stands), plus retries under contention; no per-batch STS session (R5): about 68 |
| a policy set, at any level | a fence take, confirm and release (about 7), and 409 while a reclaim deletes beneath | its write and one record read; never 409 |

A tag and a restore cost more while the erasure keeps the fence; a reclaim, a pass and every policy door cost less.

### 7. What a client and an operator observe differently

- A hold, a policy set or delete and a rename answer 200 at once, never 409 beside a reclaim; a hold names the versions
  already condemned (at most one batch per reclaim in flight, plus what `version/delete` and dead batches condemned).
- `version/delete` answers at once and deletes nothing itself; `maintenance/run` enqueues a pass and is asynchronous.
- `list versions` omits condemned versions; a tag, branch, restore or clone source on one answers 409 (19), never because
  a lease lapsed.
- `rask-pin-*` tags appear in a stock client's `tags.list()`, for the length of a door's request. A dead door leaves one:
  on a version not condemned it keeps that version (a storage leak, counted by a gauge of pins older than a door's
  request deadline, the store's `created_at` against its clock); on a version a dead batch condemned and promoted, the
  next reclaim deletes it. A pin also makes Lance refuse a branch delete while it names that branch's version.
- The maintain vend reaches nothing on the control root; `MaintenanceReclaimMarkStale` becomes one gauge of the oldest
  standing `g` entry, and `held_back{reason}` loses `history_fenced` and `policy_fenced`.

### 8. What would be deleted and what written

**Deleted**: `policy_fences.py`, `catalog_reclaims.py`, most of `reclaim_marks.py` (marks, `trusted`, `_judged`,
`_untouched`, `ForkMark`, `settle_forks`, `ended`, `oldest`); RECLAIM in `history_fences` and every reclaim's take,
renewal and confirm (`expiry._history_held`, `Hold.confirm_unchallenged`, the intents in `HolderFence.taking`);
`policies._fenced` and `write_held`'s policy and rename uses; `dataplane.RenameFence`; `PolicyRead.changed`,
`_policy_changed` and `chain_now`; the pass's mark probe; `TableRecords`' control-root grants and the per-batch session
plumbing (the HOLD cap stays as hygiene); `TablePass.tla` and its 22 configs once the code follows.

**Written**: the record (`condemned.py`: read, conditional rewrite, GC by manifest and ref identity); the catalog's batch
door (condemn, pin read, policy read, promote, answer) that every reclaim calls; the pin protocol in
`dataplane.require_whole` and `clone_sources.take` and the release's pins; `version/delete` as a condemn; the tag doors'
refusal of `rask-pin-`; `list versions` filtering; the gauges. One test per ordering rule (PinFirst, CondemnFirst,
ExplicitPins, PolicyAfterCondemn, HoldWritesFirst, GoOnly, GoUnpins, GCOnlyGone, KeepAfterDelete, IdentityKeys,
ReleasePins), each red with its rule removed, on moto and on RustFS.

### 9. THE PRODUCT DECISION FOR THE OWNER

**Today a hold answers 409 while a reclaim deletes beneath it and, once it lands, promises every version whole at its
answer; under this design it answers 200 at once and promises every version not already condemned, naming those.** The
versions that die are the same either way (a batch in flight finishes, ADR 0243 §12 F4's bound); what changes is the
promise's wording and that a hold is never refused.

### 10. What the model changed in the proposal it started from

The analysis's scratch model (`Tomb`, condemn first, pin first) is refined where the model or a measurement required:

1. **An entry's second state, `g`, and the pins it licenses deleting** (GoOnly, GoUnpins). A pin is a Lance tag and Lance
   keeps every tagged version (measured), so a door that dies on a version a dead batch left half removed would keep it so
   for good (`ReclaimLiveNoUnpin` fails `EventuallyWhole`). A batch's promotion to `g` is one more conditional write; a
   pin on another batch's `g` version protects nothing (its door reads the entry and refuses) and is deleted. Licensing
   any condemned version instead breaks `RefsWhole` (`ReclaimGoAny`).
2. **`error_if_tagged_old_versions=False`**: with the default, a pin written between the batch's pin read and Lance's tag
   read makes the whole cleanup raise (measured).
3. **A policy door writes, then reads the record** (HoldWritesFirst): read first, it promises a version a batch whose
   policy read came before the hold then deletes (`ReclaimHoldReadsFirst`).
4. **A batch id per batch, not per reclaimer**: the first model keyed entries by reclaimer, and a reclaimer's next batch
   removed, as its own excluded version, an entry its dead batch had left over deleted files (found by TLC).
5. **Only a standing manifest's or standing ref's entry protects**: a batch that listed a version another batch has
   since removed loses nothing more; a deleted branch's versions no door can name.
6. **`version/delete` under a hold** condemns and deletes nothing: a reclaim reads the hold and leaves every entry.
7. **The erasure keeps its fence for four kinds** and its release pins where its branches come back (§3); taking reclaims
   off RELEASE without that pin breaks the release in one tick.

### 11. Open questions

- Q1: the product decision above.
- Q2: dropping the ref doors' fence take too, by re-expressing SCHEDULE (the verification writes its mark, then reads the
  pins) and RELEASE (it condemns the residuals, then reads the pins) as write-then-read orders. It would bring a tag back
  to 27 requests, and needs a dead door's pin swept under A3' for the release's liveness. Not modelled.
- Q3: whether to sweep dead doors' pins on whole versions at all (A3'), or count them and leave them.
- Q4: one record per table bounds a table's concurrent condemns to the store's conditional-write throughput (16 at once:
  about 1.2 s, measured).

### 12. Verdict of this step

**GO WITH CHANGES**: the model holds with the seven changes of §10, every ordering rule's removal fails, the measurements
support B1 and B2 on RustFS 1.0.3 and pylance 13.0.0, and the erasure's interplay is closed by keeping four fence kinds
and pinning the release's recreate points. Code follows only after the owner's decision of §9.

## The four facts

- **GitOps**: met; no new chart value.
- **Cloud native**: met; every exclusion is a record or a tag on the store any replica reads.
- **Lance-namespace/table-idiomatic**: improved. `version/delete` does what the spec says; a pin is a Lance tag; cleanup
  is restricted by its documented `versions`.
- **Zero trust**: improved. The maintain vend reaches nothing on the control root and its policy shrinks from 1,964 to 701
  characters; no vend can write under `_refs/` (measured); no clock enters what a reclaim may delete.

## Not modelled

`base_reclaim`, staged manifests and committed-file records (TablePass.tla checks `base_reclaim` under A2); a drop,
undrop and purge; the erasure's scheduling and notices beyond `CondemnedErasure.tla`'s overrides; the release taking a
dead door's pin on a residual; two tags on one version; a branch whose own versions name its fork's files; a hold lifted;
batches larger than the model's versions; Q2.

## Appendix: the measurement scripts

Scratch, deleted after they ran: a driver starting the pinned RustFS as a process of its own with a root pair made for
the run, `measure.py` (sections 1 to 4 above: 1,000 pin trials through two `lance.dataset` handles, 1,000 record trials
through two boto3 clients, 16 threads condemning with If-Match retries, `cleanup_old_versions` on the wire through
`store_requests.counted` with `holding` at its first `_refs/` and `_versions/` listing), `measure_vend.py` (STS
AssumeRole by a non-root user holding an `s3:*` canned policy over the bucket, with `build_session_policy(..., "write")`
and `"maintain"`, then a put and a delete of `_refs/tags/rask-pin-<hex>.json`), and a pytest importing the estate fixture
of `test_a_tables_pass_deletes_nothing_a_door_answered_for.py` that counted the three operations of §6.
