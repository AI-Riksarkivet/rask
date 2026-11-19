# 0246. A reclaim condemns before it deletes, and a door pins before it reads (2026-11-18)

**Status:** accepted (2026-11-18), and implemented: it supersedes what [ADR 0243](0243-a-tables-pass-deletes-nothing-a-door-answered-for.md)
built to protect a reclaim's deletes, which is deleted (§13). `specs/maintenance/Reclaim.tla` is the model of the code;
`TablePass.tla` and its 22 configs are deleted. **It carries one product decision, taken for now and the owner's to
overturn (§9).** §1 to §12 are the step that checked the design before the code; §13 says what the code changed in it
and what was measured after; §14 says what an independent review of that code found and how each finding was fixed,
model first: a licence to delete ends at a stated instant, since Lance deletes by number under a branch's name, so the
clock §4 called gone is back for branches (assumption C). It carries a second open question for the owner (§14, V7, Q5). §15 fixes
the defect §14 found and left (a batch is asked for one incarnation of its ref, ListedOn), and records what the model gate
and the RustFS lane cost and how they were brought within what a person can wait for.

## Context

What a reclaim may delete is protected today by leases: the history fence's RECLAIM against REFS, renewed and confirmed;
a policy fence at each namespace level and the project, with a door's intent; victims marks that are trusted, judged and
aged; fork marks; a store session per catalog batch; a maintain credential capped to the hold (ADR 0243 §2 to §13). Four
review rounds each found reproduced blocking defects, 17 of the 23 later ones in marks, leases or policy fences. The
alternative this ADR checks replaces all of them with two write-then-read orders on records nobody but the catalog
writes, so that no lease, renewal or clock enters the argument for what a reclaim may delete.

## Decision

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
| A1, a reclaimer's credential dies no later than its hold | **gone for main, back for branches as C (§14, V2)**: on main a paused reclaimer resuming days later deletes only versions an entry names `g`, which no door has admitted since. On a branch it is false: Lance deletes by number under the name's path, and a branch deleted and made again repeats its numbers, so a licence ends at `licensed_until` and C holds the reclaimer's credential dead by then |
| A2, a commit in flight lands within the unverified age (7 days) | **stays** (`ReclaimWriterOutlivesAge`) |
| A3, a ref write holds REFS against RECLAIM | **gone**, replaced by PinFirst and CondemnFirst |
| A4, a fence take is atomic | **gone for reclaims**; stays for the erasure's kinds (`Fence.tla`) |
| A5, a door writes within HOLD of its confirm | **gone for every delete a reclaim makes**; stays for the erasure's notices and the release's take of a residual's pin |
| new B1 | linearizable writes and listings on the control root and the table's `_refs/` (measured, §5) |
| new B2 | only the catalog writes or deletes a pin or the record (measured, §5) |
| new A3', only if a pin sweep is adopted | a sweep takes only a pin whose door can no longer act (an erasure release's pin is of a kind no sweep takes, §14 V3) |
| new C (§14, V2) | a batch's credential is minted before the batch is asked and lives at most `CREDENTIAL_LIFETIME` (15 min), and the store's clock and the catalog's agree within `CLOCK_MARGIN` (1 min), so the store refuses it once the batch's `licensed_until` (promotion + 17 min, the catalog's clock) has passed. `ReclaimCredentialOutlivesLicence.cfg` removes it and fails; on a key pair (`mode_b`) it does not hold, the residual stated in §14 |

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

**Taken for now (2026-11-18), the owner's to overturn.** Every policy set and delete, at the table, a namespace and the
project, answers 200 at once with `condemned`, a list of `{table, branch, version}` read after its write: the versions
already condemned of the tables the level governs, which a batch in flight may still delete (at most one batch of 16 per
reclaim past its policy read, plus what `version/delete` or a dead batch condemned). A hold keeps every other version.
A describe and a listing carry no `condemned`. A rename answers the spec's `RenameTableResponse` and names nothing.

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
- Q5: which of an erasure and a retention hold wins (§14, V7): unchanged by this design, the erasure, for now.

### 12. Verdict of this step

**GO WITH CHANGES**: the model holds with the seven changes of §10, every ordering rule's removal fails, the measurements
support B1 and B2 on RustFS 1.0.3 and pylance 13.0.0, and the erasure's interplay is closed by keeping four fence kinds
and pinning the release's recreate points. Code follows only after the owner's decision of §9.

### 13. What the code changed in the design, and what it deleted (2026-11-18)

Implemented as modelled: one record per table (`lakehouse.condemned_versions`, `_condemned/table-<digest>.json`, a
conditional rewrite of at most 32 rounds), one batch function (`Batch.condemn`, `read_pins`, `promote`, and
`reclaim`), which every reclaim goes through (a pass through the catalog's `maintenance/condemn`, the catalog's
`maintenance/run`, an erasure's verification and its release in process, `catalog.services.reclaims`), one pin
protocol (`dataplane.require_whole`, `clone_sources.take`, the release's recreate points). Where the code forced a
change, the model was changed first or the change is one the model already covers:

1. **`base_reclaim` is in `Reclaim.tla`** (`RBase`, `RBDel`: the table's data directory listed after the cleanup, less
   what every standing manifest names, a file `b` only version 1 names), moved from `TablePass.tla`, which is deleted.
   Every gate config holds with it (`scripts/tla_check.sh Reclaim`: `Reclaim.cfg` 149,943 states, `ReclaimRelease.cfg`
   792,006, `ReclaimBranchesTag.cfg` 519,575, `ReclaimTwo.cfg` 519,447) and every must-fail config fails as it did. What
   `TablePass.tla` proved that `Reclaim.tla` did not: the writer in flight (A2) is `ReclaimWriter.cfg` and
   `ReclaimWriterOutlivesAge.cfg`; a hold at any level is `RLevel`; the catalog's commit-time stamp was modelled by
   neither (both let `Age` move any version at any time, which over-approximates every clock), and stays measured
   (ADR 0243 §10).
2. **`maintenance/run` reclaims in process, as before, through the one batch function**; it does not enqueue a pass. The
   catalog holds no publish right on `maintenance.*` (the chart's NATS users), and the chart is not this change's to
   edit. A reclaimer is a reclaimer in the model, whichever process it runs in. The catalog's in-process reclaims delete
   under its own identity: the per-batch store sessions are deleted, since no lease is left for a session to die with.
3. **`version/delete` only condemns** (§1), and its versions are finished by the table's next reclaim: its next pass
   (a commit's follow-up, or the recheck, `MAINTENANCE_RECHECK_HOURS`, 24) or `maintenance/run`. A pass asks the
   catalog for a batch when its victims meet the threshold, when the table's record stands (one listing of its key under
   maintenance's own session, which may list the control root and read none of it), or when a dead door's pin waits.
4. **A dead door's pin is swept (A3' adopted, `ReclaimSweep.cfg`)**: a batch deletes a pin older than `PIN_LIFETIME`
   (24 h) by the tag's `created_at`, and a door refuses to act once `DOOR_DEADLINE` (12 h) has passed since its pin by
   its own monotonic clock (503, retryable), so a swept pin's door can no longer act unless two catalog pods' clocks
   disagree by 12 hours. Counted (`lance.reclaim.pins_swept`). Without the sweep, a dead door's pin on a version no
   batch condemned would keep that version for good and need a hand to clear it.
5. **A fork point an erasure took stays condemned while its branch stands**, and is not re-condemned by every batch of
   its ref (`Batch.condemn` leaves it out of a batch's finish, `forks=True` alone names one): Lance keeps its manifest
   whatever a cleanup is asked. The model's `RBegin` finishes it too, to no effect (`RDecide` keeps a fork's manifest).
6. **The promise of a policy answer is about holds**: a set or delete that changes only retention binds the batches that
   condemn after it, and a batch already past its policy read finishes under the earlier retention; `RetentionKept` is
   about holds alone. A rename's answer is the spec's `RenameTableResponse`, which names no version: in the model a
   rename's answer is a hold door's, whose promise holds in every config, and the code answers less.
7. **An erasure's reclaims read the hold.** Before, an erasure's verification and release reclaimed whatever a
   maintenance hold said; under §1 every reclaim reads it, so a held table's `history:<ref>` surface fails, naming the
   hold, and its obligation stays open until the hold is lifted. (A change made without a decision: restored in §14, V7.)
8. **The condemned record is not backed up** by the control-root backup: its job's store policy names the prefixes it
   reads (the chart), which this change does not edit. A restored control root starts with no record; a version a dead
   batch left half removed is then unnamed until the next reclaim of its ref condemns it again. (Fixed in §14, V5.)
9. **The maintain vend's policy fits every catalog-made shape**: 912 characters at two 63-character buckets, 1,142 with
   a 75-character KMS key ARN (measured with `vending.counted_length`, 2026-11-18). The registration check stays for a
   control root key prefix past about 900 characters or a KMS key id past about 450 (deployment configuration, not a
   warehouse's); the register door's check stays, since a location of nine 60-character segments is 2,679 characters
   and two data directories with a KMS key 2,077. The `HOLD` cap on a maintain vend (900 s) stays as hygiene, a
   short-lived credential; nothing a reclaim deletes rests on it. (False for branches: §14, V2, where the cap is
   `CREDENTIAL_LIFETIME` and a licence's end rests on it.)

**Deleted**: `lakehouse.policy_fences`, `lakehouse.reclaim_marks` (the victims and fork marks, their trust, judging and
endings), `catalog.services.catalog_reclaims` (the holds, the per-batch sessions), the history fence's RECLAIM kind and
its two mark kinds, a fence take's intent and `Hold.confirm_unchallenged`, the policy doors' fences and the rename's,
the pass's mark probe, its re-read of the policy and the chain under holds (`PolicyRead`, `chain_now`,
`_policy_changed`, `_history_held`), the maintain vend's grants on the history fence and the policy fences,
`maintenance.reclaims.mark_oldest` and `MaintenanceReclaimMarkStale`, the held-back reasons `history_fenced`,
`policy_fenced`, `policy_changed` and `marked`, `specs/maintenance/TablePass.tla` and its 22 configs.

**Written**: `lakehouse.condemned_versions`, `catalog.services.reclaims`, the `maintenance/condemn` door
(`can_vend_maintain`), `DoorTable.pin_whole` and `DoorTable.condemned`, `dataplane.Pins`, the release's pins, the
policy answers' `condemned`, `version/list`'s filter, the tag doors' refusal of the pin prefix,
`maintenance.reclaims.condemned` and `MaintenanceCondemnedVersionsStanding`, `held_back{reason="condemned"}`.

**Each rule's test, and its reversal** (`tests/integration/test_a_reclaim_condemns_before_it_deletes.py` unless named;
each case on moto and on the pinned RustFS 1.0.3 of `make lifecycle-rustfs-check`, pylance 13.0.0; the reversal applied
to the code, the case run, the code restored):

| Rule | Test | Reversal | Result |
| --- | --- | --- | --- |
| CondemnFirst | `test_a_ref_answered_200_beside_a_reclaims_deletes_names_a_whole_version` (tag, restore, branch, clone) | the batch writes no entry before the cleanup | 8 of 8 fail: each door answers 200 on a version whose data file is gone |
| PinFirst | `test_a_door_that_read_the_record_before_a_batch_condemned_keeps_its_version` (tag, branch) | the door reads the record, then pins | 4 of 4 fail, as above |
| KeepAfterDelete, GCOnlyGone | `test_a_version_whose_deletes_the_store_refused_stays_condemned_until_the_next_reclaim_finishes_it` | a rewrite drops `g` entries | 2 of 2 fail: a tag lands on a half-removed version |
| GoUnpins | `test_a_dead_doors_pin_on_a_go_version_is_deleted_and_the_version_finished` | no pin on a `g` version deleted | 2 of 2 fail |
| A3' | `test_a_dead_doors_pin_past_its_lifetime_is_swept` | no sweep | 2 of 2 fail |
| PolicyAfterCondemn | `test_a_hold_answered_beside_a_reclaim_keeps_every_version_it_did_not_name` | the policy read before the condemn | the 6 cases held after the policy read fail; the 4 held before it pass, as they must (the batch reads the hold either way) |
| HoldWritesFirst | `test_a_hold_door_writes_before_it_reads` | the door reads, then writes | 2 of 2 fail |
| IdentityKeys | `test_entries_are_keyed_by_the_branchs_incarnation` | entries keyed by the branch's name | 2 of 2 fail |
| A fork point stays condemned | `test_an_erasures_fork_point_stays_condemned_while_a_branch_keeps_it` (tag, restore) | the batch drops its fork entries as it licenses them | 4 of 4 fail |
| ReleasePins | `services/catalog/tests/test_erasure_releases_a_pinning_branch_after_its_notice.py::test_a_release_pins_where_its_branches_come_back_before_its_first_delete` (local) | the release pins nothing | 1 of 1 fails |
| B2 (no caller pin) | `test_a_caller_neither_plants_nor_lifts_a_pin` | the tag door takes the pin prefix | 2 of 2 fail |
| B2 (no vend pin or entry) | `test_no_vended_credential_writes_a_pin_or_the_condemned_record.py` (RustFS only) | none: the store's answer | both tiers AccessDenied on every write |

GoOnly and ExplicitPins are not shown failing on Lance: pylance 13.0.0's cleanup reads the tags before it lists the
manifests (§5), so a pin that lands after the batch's read is honoured by Lance itself. They are the model's
(`ReclaimTagsFirst.cfg` and the must-fail configs), and the code keeps them for a store or a Lance that reads the other
way round.

**Requests and latency, measured after** (RustFS 1.0.3 as a process, through `tests/integration/store_requests.py`, the
real catalog app and the real pass, five rounds each, 2026-11-18; "before" is the parent commit's code, run by the same
scratch test):

| Operation | Before: requests, median | After: requests, median |
| --- | --- | --- |
| tag create | 27, 664 ms | 33 to 34, 772 ms (the pin's put, its listing and its delete, the record's read) |
| restore | 64 to 65, 1,452 ms | 70 to 71, 1,564 ms |
| `version/delete` of one version | 89 to 126 (it deleted), 2,727 ms | 28 to 40 (it condemns), 866 ms; with the `maintenance/run` that finishes it, 109 to 166, 3,106 ms |
| policy set (table) | 22, 646 ms | 17, 534 ms |
| a pass of the table, one version due | 159 to 237, 4,732 ms | 128 to 193, 3,511 ms |

A pass's requests grow with the versions it lists, round by round, in both columns.

**Counts.** Lines against the parent commit, this ADR's section included: about 3,430 added and 4,610 removed in all
(net about -1,180); production Python 1,707 and 2,007 (net -300); tests 1,076 and 1,574 (net -498); `specs/` 60 and 839
(net -779); the generated OpenAPI, operation table, client and port ledger 357 and 48. Collected offline tests: 3,965 before, 3,948 after (-17). Record kinds: the policy fences, the
victims mark and the fork mark go (3), the condemned record comes (1). Guards: a fence take's intent and challenger,
`confirm_unchallenged`, the policy doors' and the rename's fences, the pass's mark probe and its policy re-read go; the
batch, the pin before a ref door's read and the tag doors' pin-prefix refusal come. Model configs: `TablePass.tla`'s 22
go and no config is added. Alerts: `MaintenanceReclaimMarkStale` goes and `MaintenanceCondemnedVersionsStanding` comes.

### 14. What an independent review of the code found, and how each was fixed (2026-11-18)

A review of §13's code found seven problems; two were reproduced against the real catalog and the real reclaim, and two
were gaps the model hid. Each was fixed MODEL FIRST where the model was wrong, then with a test red before the fix and
red again with the rule reversed. "Found by review" names what the review reported; "Fixed" names the rule or guard, the
file and function, the config that fails without it, and its test.

**V1. A ref door acted on an implicit version** (found by review, reproduced: a branch create with no `from_version`
answered 200 and the branch read `Not found`). `require_whole` returned without pinning when the request named no version,
on the argument that a ref's latest version is its head, but `create_branch(name, None)` cuts at the version of the
HANDLE opened at the request's start (measured on pylance 13.0.0: a handle at version 2, main since at 4, cut at 2;
`tags.create(name, None)` the same; `checkout_version((None, None))` answered 4). Reproduced by the test below with the
old code: the door held after its record read, an insert on main, `maintenance/run` (`retain_versions: 1`) held at its
manifest delete, the door let go: 200, then a branch whose data file reads `Not found`. **Fixed**: no ref door hands Lance
an implicit version. Every door acts on a NUMBER: the request's, or the source's head read fresh
(`dataplane._source_head`, `checkout_version((ref, None))`), pinned, judged against the record, and passed to Lance
(`dataplane.create_branch`, `require_whole` takes `version: int`, `_tag_reference` answers a number). The routers' ref
doors are tag create and update, branch create, restore and a clone's registration (its source version is read off the
clone's manifest, `clone_sources.clone_reference`); of these only a branch create's request may name no version: the
spec requires `version` of a tag create, a tag update and a restore, and the door refuses one without it (400).
Test: `test_a_door_asked_with_no_version_acts_on_the_number_it_pinned` (a branch from main and from a branch with no
version; a tag create, a tag update and a restore with none, refused 400 with nothing changed). The model had it already:
a door's version is a number (`Targets`), so no config changes.

**V2. A licensed cleanup deleted the next incarnation of a branch** (found by review, reproduced twice). Entries are
keyed by the branch's identity, but `cleanup_old_versions(versions=...)` deletes by NUMBER under the path the handle
names (`tree/<name>/_versions`), and a branch deleted and made again under its name repeats its numbers (measured on
pylance 13.0.0: a cleanup through a handle of the deleted incarnation removed the made-again incarnation's versions).
`delete_branch` takes no fence and reclaims take none, so nothing ordered them. **The model hid it**: `RDecide`
intersected `<<identity, n>>` with the manifests, deleting by identity, which is not what Lance does, and §4's "a paused
reclaimer resuming days later deletes only versions an entry names g" was false for branches. Reproduced on moto by the
test below with NameGuard removed: (b) a tag answered 200 on version 4 of the made-again `b` once the paused cleanup read
its tags, then `b` held only version 5; (a) the table's hold answered 200 naming no version of the made-again `b`, then
versions 3 and 4 of it were gone.
- (i) **The model** (`Reclaim.tla`): `RSnapM` lists the manifests under the reclaimer's PATH as the name holds it then
  (`snapI`), and `RDecide` deletes by `<<name, n>>` under it (`Nums`). The interleaving is reachable: a batch of `b`
  promoted and paused, `b` deleted and made again, its cleanup deleting the new incarnation's versions.
  `ReclaimBranchReuse.cfg` (no NameGuard) fails `DeletesCondemned` (16 steps), `ReclaimBranchReuseTag.cfg` fails
  `RefsWhole` (the review's (b)), `ReclaimBranchReuseHold.cfg` fails `RetentionKept` (the review's (a)).
- (ii) **A licence ends at a stated instant.** A batch's promotion stamps its entries `licensed_until`, now plus
  `LICENCE` (`condemned_versions.Batch.promote`, `LICENCE` = `CREDENTIAL_LIFETIME` 15 min + 2 × `CLOCK_MARGIN` 1 min, so
  17 minutes). Every cleanup runs under a credential minted BEFORE its batch was asked
  (`condemned_versions.reclaim`, `RefReclaim.mint`) and acts on a batch only where the expiry the store stamped on that
  credential, plus `CLOCK_MARGIN`, is no later than `licensed_until` (`condemned_versions.covers`); a batch asked before
  the credential (a pass's first, asked before its vend) is asked again. A pass's credential is its maintain vend, capped
  at `CREDENTIAL_LIFETIME` by the vend door (`api/v1/endpoints/credentials.py`, the cap load-bearing again, as ADR 0243's
  A1 had it), whose `expires_at_millis` the pass now keeps (`credentials.Vended.expires_at`, `ExpiryTarget.write_expires`).
  The catalog's in-process reclaims (`maintenance/run`, an erasure's verification and its release) mint a maintain-tier
  session through the catalog's web identity for each batch, lasting at most `CREDENTIAL_LIFETIME`, and delete through
  the ref opened under it and nothing else (`reclaims.minter`, `reclaims.in_process`, `reclaims.erasure_reclaims`).
  **Residual**: where the catalog has no web identity (`LANCE_VENDING_MODE=mode_b`, the development fixtures on a key
  pair) it deletes under its own key, which no store expires, so a catalog frozen inside one cleanup call past its
  licence can still delete, by number, versions of a branch made again under the name since
  (`docs/architecture/deployment.md` says so).
- (iii) **A name a live licence names is not made again.** A branch create reads the name free, then the record, and is
  refused 409 code 14, retryable, naming the instant, while a `g` entry under the name for another incarnation is live
  (`dataplane.refuse_a_name_a_licence_holds`, `condemned_versions.licensed_under`); an erasure release's recreate waits
  the same way (`erasure_release._recreate`, the tick retries). The record's rewrite drops a gone incarnation's `g`
  entries only once their licence has passed (`Batch.condemn`, GCAfterLicence). A batch of a branch reads the branches
  after its promotion and licenses nothing where its incarnation went (`Batch.promote`, StandsAfterGo): the create reads
  the name free and then the record, the batch writes its licence and then reads the name, so of the two at least one
  sees the other. A branch delete is never refused for a reclaim. A reclaim of a branch deleted while it ran answers
  404 code 22 (`maintenance._a_store_refusal_retried`; Lance answers `Ref not found` through the stale handle, measured).
  The policy doors' `condemned` names each version's `branch_identifier`, so a version of the incarnation standing now is
  promised unless the answer names it.
- (iv) **The clock is an assumption again, for branches**: C, a batch's credential is minted before its batch is asked and
  lives at most `CREDENTIAL_LIFETIME`, and the store's clock and the catalog's agree within `CLOCK_MARGIN` (one minute),
  so the store refuses the credential once `licensed_until` has passed on the catalog's clock. Modelled as ADR 0243's A1
  was: `LicenceEnds` ends a licence at any time and the store then refuses its batch's credential (`RCredDead`);
  `ReclaimCredentialOutlivesLicence.cfg` removes it and fails `DeletesCondemned`. `ReclaimLicenceCollected.cfg`
  (GCAfterLicence removed) and `ReclaimPromotedOnAGoneBranch.cfg` (StandsAfterGo removed) fail `DeletesCondemned`.
  `LicenceEnds` is taken only where a branch's name can be made again; elsewhere a credential that dies mid-cleanup is
  `RRefused`, a fault, and the liveness configs assume a cleanup ends within its licence (with `LicenceEnds` everywhere,
  `ReclaimLiveDeadPin.cfg` found a behaviour in which every batch's licence ends before its cleanup, for ever).
Tests: `test_a_branch_made_again_under_its_name_loses_nothing_to_a_reclaim_of_the_one_deleted` (tagged and held, the review's (b) and (a); a rewrite between, GCAfterLicence; a promotion after the create, StandsAfterGo), `test_a_maintain_vend_dies_by_the_licence_of_a_batch_asked_after_it` (the vend door's cap against the licence),
`test_a_pass_deletes_only_under_a_vend_the_store_refuses_by_its_licences_end` (a vend that outlives its licence deletes
nothing; one the door caps finishes the batch), and on the lane alone
`test_the_store_refuses_a_maintain_session_once_the_licence_it_deletes_under_ends` (a maintain session as the vend door
mints it: `covers` holds for its stamped expiry, and once the licence's instant has passed the store refuses a delete
it signs; RustFS issues no session shorter than 900 s, measured, so the lane runs this one in the background of its
other tests, about 17 minutes).

**V3. The erasure release's pins matched the sweep** (found by review, reasoned). `_pin_name` was `rask-pin-<32 hex>`,
the pattern the sweep of dead doors' pins takes once 24 hours old (A3'); the release is no door, keeps no deadline,
resumes its recorded plan whenever its tick runs, and deleted its pins only on its success path, so a tick that stalled
for a day between its delete and its recreate lost its pins to a batch and its branches could not come back. **The model
hid it**: `Sweep` took only `RefDoors`' pins. **Fixed**: the release's pins are `rask-release-<32 hex>`
(`condemned_versions.release_pin_name`, derived from the release's record), a kind the sweep (`Batch.read_pins`) and
GoUnpins (`Batch.promote`) never take (`is_door_pin`), removed by the release at its finish, or when its recreate points
are refused, and by nothing else. The tag doors refuse and hide the whole `rask-` prefix (`RESERVED_PREFIX`,
`dataplane.refuse_a_pin_name`, `list_tags`), and the erasure's verification never drops one (`erasure._drop_subject_tags`).
Model: `Sweep` beside a stalled release; `ReclaimReleaseSwept.cfg` (the sweep takes a stalled release's pin) fails
`RefsWhole`, and `ReclaimRelease.cfg` now runs with the sweep on and holds. Tests:
`test_a_dead_doors_pin_past_its_lifetime_is_swept[release]` (a release's pin past the lifetime stays, its version kept)
and `test_a_caller_neither_plants_nor_lifts_a_pin[release]` (`rask-anything` and a release pin's name refused 400 code 13,
a release's pin hidden and unmovable).

**V4. A policy door read every condemned record in the estate** (found by review, reasoned): a record stayed until its
table ended, `every_record` read each, one malformed record made a landed policy write answer 503, and `condemned` was
unbounded. **Fixed**: a table has a record only while it has entries: the rewrite that removes the last deletes it under
the ETag read (`condemned_versions._rewrite`, `records.delete_json`; measured on moto 5 and RustFS 1.0.3, a DeleteObject
naming a stale If-Match answers 412 and keeps the object). A policy door lists `_condemned/` once and reads each record
standing, which is each table with a reclaim in flight or stuck, keeping those beneath its level by the name each
table's claim holds (`reclaims.condemned_versions_of`); a record's key names no level, so the door reads every record
standing, not only those beneath it, and that set is small by the rule above. A record that cannot be read is named in
`condemned_unread`, never a 503 (`condemned_versions.every_record`, `policies._condemned_under`); `condemned` names at
most 100 versions (`reclaims.CONDEMNED_SHOWN`) and `condemned_beyond` counts the rest. Test:
`test_a_policy_door_names_at_most_its_bound_and_what_it_could_not_read` (120 entries planted: 100 named, 20 beyond, a
malformed record named, 200; the record deleted once a reclaim finished its entries). Measured below.

**V5. `_condemned/` was missing from the control-root backup** (found by review): after a restore every fork point and
every half-removed version would be referenceable again. **Fixed**: `_condemned` is a record prefix
(`control_prefixes.RECORD_PREFIXES`, which `scripts/control_root_backup.py` copies) and the backup job's store policy
reads it (`chart/templates/_helpers.tpl`, `lance.objectStorePolicy.backup-control-root`): 2,891 characters before, 2,975
after (rendered with `backups.controlRoot.enabled=true`). No other list of control-root prefixes in the chart or
`deploy/` names it or needs to: the catalog's policy reaches every object, maintenance's lists the control root (the
pass's one listing of a table's record) and reads none of `_condemned/`, lineage's reaches its outbox and its timers,
the maintain vend reaches no control-root record but the table's committed-file records, and no network policy names a
prefix. No list names a prefix the redesign deleted (`_policy_fences`, the marks).

**V6. The `rask-lance-catalog` skill still described the deleted design** (found by review): rewritten for condemn and pin
(the maintenance section, `version/delete`, and the history fence's four kinds), as are `rask-rustfs`'s measured rows
(the maintain vend reaching a fence directory is gone; the B2 measurement, STS's 900 s floor and the conditional delete
measured), `docs/API.md` (`version/delete`) and `docs/architecture/deployment.md` (the residual).

**V7. Which wins between an erasure and a retention hold had been changed without a decision** (found by review).
§13.7 had an erasure's verification and release read the maintenance hold, failing a held table's history surface until
the hold lifts. Before ADR 0246 the erasure's reclaim did not read the maintenance hold. **Restored**: an erasure's
batches read no hold (`reclaims.batch(..., holds=False)`, an explicit parameter of the erasure's call,
`reclaims.erasure_reclaims`), so an erasure on a held table reclaims as it did before. Test:
`test_an_erasures_reclaim_reads_no_maintenance_hold`. **Which of the two wins is UNCHANGED by this redesign and an open
question for the owner (Q5).** An erasure's obligation is a legal duty with a deadline, a retention hold a promise that a
table's history is kept: as built, the erasure wins, and a hold's answer does not promise what an erasure on the table
takes (the hold's `condemned` names what is condemned when it lands, and an erasure's later batch condemns more). If the
hold wins, an erasure on a held table leaves its `history:` surface failed, naming the hold, and the obligation stays open
until the hold is lifted, which may miss its deadline; a hold door would then promise every version against the erasure
too.

**Smaller, same change.**
- **`version/delete` read the tags before it condemned** (found by review): a tag door that pinned after that read and
  read the record before the condemn left a user-tagged version condemned for good. **Fixed**: it condemns (a batch id
  of its own, `vd-<hex>`), then reads the tags, pins and branches cut from the ref, and takes its entries back and
  answers 409 where one names a version (`condemned_versions.condemn_named`, `maintenance.refuse_pinned_versions`;
  VDeleteCondemnsFirst). Model: `VDelete` in two steps and the invariant `VDeleteSparesTags`;
  `ReclaimVDeleteReadsFirst.cfg` fails it. Test: `test_version_delete_never_leaves_a_tagged_version_condemned`.
- **GoOnly on Lance** (the review: model-only was wrong): `test_only_another_batchs_go_entry_licenses_ignoring_a_pin` (a tag
  door that pinned and read before a first batch's condemn, held before it acts; that batch held after its condemn; a
  second batch (`maintenance/run`) held at its manifest delete; the door's tag then names a version that reads whole).
  ExplicitPins stays model-only: pylance 13.0.0's cleanup reads the tags before it lists the manifests (§5), so a pin
  landing after the batch's read is honoured by Lance itself, and no interleaving on this Lance shows the explicit read's
  removal.
- **A reclaim of a branch deleted while it ran answered 500** (found here): Lance's cleanup through the stale handle
  answers `Ref not found error`; it answers 404 code 22 now, nothing more deleted.
- **Found here, fixed in §15 (ListedOn)**: a reclaimer that lists a branch's victims on one incarnation and asks for its
  batch once the name holds another had the batch condemn and delete the new incarnation's versions of those numbers
  (each condemned first, so no door named them and no hold was broken, but versions younger than the retention went).

**The guarantees, each reversed** (`tests/integration/test_a_reclaim_condemns_before_it_deletes.py` unless named; moto on
every row, the pinned RustFS 1.0.3 of `make lifecycle-rustfs-check` on the store-arbitrated rows; the reversal applied to
the code, the cases run, the code restored from the commit; pylance 13.0.0, 2026-11-18):

| Rule | Test | Reversal | moto | RustFS |
| --- | --- | --- | --- | --- |
| CondemnFirst | `test_a_ref_answered_200_beside_a_reclaims_deletes_names_a_whole_version` (tag, restore, branch, clone) | the batch writes no entry; its promotion licenses what it chose | 4 of 4 fail | 4 of 4 fail |
| PinFirst | `test_a_door_that_read_the_record_before_a_batch_condemned_keeps_its_version` (tag, branch) | the door reads the record, then pins | 2 of 2 fail | 2 of 2 fail |
| KeepAfterDelete, GCOnlyGone | `test_a_version_whose_deletes_the_store_refused_stays_condemned_until_the_next_reclaim_finishes_it` | a rewrite drops `g` entries whose manifest stands | 1 of 1 fails | 1 of 1 fails |
| GoUnpins | `test_a_dead_doors_pin_on_a_go_version_is_deleted_and_the_version_finished` | no pin on another batch's `g` version deleted | 1 of 1 fails | (not store-arbitrated) |
| A3' | `test_a_dead_doors_pin_past_its_lifetime_is_swept[door]` | no dead door's pin swept | 1 fails (the `[release]` case passes, as it must) | (not store-arbitrated) |
| PolicyAfterCondemn | `test_a_hold_answered_beside_a_reclaim_keeps_every_version_it_did_not_name` | the policy read before the condemn | the 3 held after the policy read fail; the 2 held before it pass, as they must | the same |
| HoldWritesFirst | `test_a_hold_door_writes_before_it_reads` | the door reads, then writes | 1 of 1 fails | 1 of 1 fails |
| IdentityKeys | `test_entries_are_keyed_by_the_branchs_incarnation` | a door judges entries by the branch's name | 1 of 1 fails | (not store-arbitrated) |
| A fork point stays condemned | `test_an_erasures_fork_point_stays_condemned_while_a_branch_keeps_it` (tag, restore) | the batch drops its fork entries once it licensed them | 2 of 2 fail | (not store-arbitrated) |
| ReleasePins | `services/catalog/tests/test_erasure_releases_a_pinning_branch_after_its_notice.py::test_a_release_pins_where_its_branches_come_back_before_its_first_delete` (local) | the release pins nothing | 1 of 1 fails | (local root) |
| B2, the reserved prefix (V3) | `test_a_caller_neither_plants_nor_lifts_a_pin` (door, release) | the tag doors refuse only a door pin's name | 2 of 2 fail | (not store-arbitrated) |
| B2, no vend writes a pin or the record | `test_no_vended_credential_writes_a_pin_or_the_condemned_record.py` (RustFS only) | none: the store's answer | | both tiers AccessDenied on every write |
| No implicit version (V1) | `test_a_door_asked_with_no_version_acts_on_the_number_it_pinned` (branch from main, from a branch) | the door pins nothing and hands Lance no version where the request names none | the case from main fails (200, then `Not found`); from a branch passes, as Lance resolves `(branch, None)` fresh | (not store-arbitrated) |
| NameGuard (V2) | `test_a_branch_made_again_under_its_name_loses_nothing_to_a_reclaim_of_the_one_deleted` (tagged, held) | no licence read at a branch create | 2 of 2 fail (the review's (b) and (a)) | 2 of 2 fail |
| GCAfterLicence (V2) | the same, `tagged-after-a-rewrite` | a rewrite drops a gone incarnation's licensed entries at once | 1 of 1 fails | 1 of 1 fails |
| StandsAfterGo (V2) | the same, `tagged-before-its-promotion` | a branch's batch licenses without reading the branches after its promotion | 1 of 1 fails | 1 of 1 fails |
| C, `covers` (V2) | `test_a_pass_deletes_only_under_a_vend_the_store_refuses_by_its_licences_end` | every credential covers every licence | 1 of 1 fails | (not store-arbitrated) |
| C, the vend's cap (V2) | `test_a_maintain_vend_dies_by_the_licence_of_a_batch_asked_after_it` | the maintain vend uncapped | 1 of 1 fails | (not store-arbitrated) |
| C, the store's refusal (V2) | `test_no_vended_credential_writes_a_pin_or_the_condemned_record.py::test_the_store_refuses_a_maintain_session_once_the_licence_it_deletes_under_ends` (RustFS only) | none: the store's answer | | covered, then a delete signed after the licence's instant refused and the object kept (1 of 1 passes, 1,021 s) |
| ReleasePinKind (V3) | `test_a_dead_doors_pin_past_its_lifetime_is_swept[release]` | the sweep takes any of the catalog's pins | 1 of 1 fails | (not store-arbitrated) |
| V4, unread named | `test_a_policy_door_names_at_most_its_bound_and_what_it_could_not_read` | an unreadable record raises (the door answers 503) | 1 of 1 fails | (not store-arbitrated) |
| V4, a record goes with its last entry | the same | the empty record is written back, not deleted | 1 of 1 fails | 1 of 1 fails |
| V7, an erasure reads no hold | `test_an_erasures_reclaim_reads_no_maintenance_hold` | the erasure's batch reads the hold | 1 of 1 fails | (not store-arbitrated) |
| VDeleteCondemnsFirst | `test_version_delete_never_leaves_a_tagged_version_condemned` | `version/delete` reads the tags, then condemns | 1 of 1 fails | 1 of 1 fails |
| GoOnly | `test_only_another_batchs_go_entry_licenses_ignoring_a_pin` | any other batch's entry licenses ignoring a pin | 1 of 1 fails | 1 of 1 fails |
| ExplicitPins | model only (`ReclaimNoExplicitPins.cfg` fails; `ReclaimTagsFirst.cfg` holds) | | | |

**The model after these changes** (`scripts/tla_check.sh Reclaim`, two TLC workers on four cores shared with other
agents):

| Config | Verdict | Distinct states | Time |
| --- | --- | --- | --- |
| `Reclaim.cfg` | holds | 149943 | 6 s |
| `ReclaimBranchReuse.cfg` | fails DeletesCondemned as expected | 6920 | 2 s |
| `ReclaimBranchReuseHold.cfg` | fails RetentionKept as expected | 47252 | 5 s |
| `ReclaimBranchReuseTag.cfg` | fails RefsWhole as expected | 54480 | 4 s |
| `ReclaimBranches.cfg` | holds | 1660308 | 57 s |
| `ReclaimBranchesTag.cfg` | holds | 6316253 | 167 s |
| `ReclaimCredentialOutlivesLicence.cfg` | fails DeletesCondemned as expected | 8188 | 1 s |
| `ReclaimDoorReadsFirst.cfg` | fails RefsWhole as expected | 1667 | 1 s |
| `ReclaimEntryDropped.cfg` | fails HalfOnlyCondemned as expected | 345 | 1 s |
| `ReclaimFork.cfg` | holds | 1163873 | 27 s |
| `ReclaimForkEntryDropped.cfg` | fails HalfOnlyCondemned as expected | 175 | 1 s |
| `ReclaimGoAny.cfg` | fails RefsWhole as expected | 9073 | 2 s |
| `ReclaimHoldReadsFirst.cfg` | fails RetentionKept as expected | 122 | 1 s |
| `ReclaimLicenceCollected.cfg` | fails DeletesCondemned as expected | 10721 | 1 s |
| `ReclaimLive.cfg` | holds | 194202 | 37 s |
| `ReclaimLiveDeadPin.cfg` | holds | 3502 | 2 s |
| `ReclaimLiveNoUnpin.cfg` | fails temporal as expected | 3181 | 2 s |
| `ReclaimNamedEntries.cfg` | fails DeletesCondemned as expected | 25488 | 2 s |
| `ReclaimNoCondemn.cfg` | fails RefsWhole as expected | 2530 | 1 s |
| `ReclaimNoExplicitPins.cfg` | fails RefsWhole as expected | 1484 | 1 s |
| `ReclaimPinSweepLive.cfg` | fails RefsWhole as expected | 4694 | 2 s |
| `ReclaimPolicies.cfg` | holds | 168224 | 6 s |
| `ReclaimPolicyBeforeCondemn.cfg` | fails RetentionKept as expected | 65 | 1 s |
| `ReclaimPromotedOnAGoneBranch.cfg` | fails DeletesCondemned as expected | 8915 | 1 s |
| `ReclaimRelease.cfg` | holds | 4804048 | 117 s |
| `ReclaimReleaseSwept.cfg` | fails RefsWhole as expected | 36044 | 2 s |
| `ReclaimReleaseUnpinned.cfg` | fails RefsWhole as expected | 8525 | 2 s |
| `ReclaimRollbackAfterDelete.cfg` | fails HalfOnlyCondemned as expected | 289 | 1 s |
| `ReclaimSweep.cfg` | holds | 207462 | 5 s |
| `ReclaimTagsFirst.cfg` | holds | 52723 | 3 s |
| `ReclaimTwo.cfg` | holds | 519447 | 15 s |
| `ReclaimTwoRefs.cfg` | holds | 2006839 | 83 s |
| `ReclaimVDeleteReadsFirst.cfg` | fails VDeleteSparesTags as expected | 32 | 1 s |
| `ReclaimWriter.cfg` | holds | 137345 | 5 s |
| `ReclaimWriterOutlivesAge.cfg` | fails InFlightKept as expected | 86 | 1 s |
| `CondemnedErasure.cfg` | holds | 76065 | 66 s |
| `CondemnedErasureRefsInTheGrace.cfg` | holds | 195397 | 57 s |
| `CondemnedErasureRestore.cfg` | holds | 1600 | 1 s |
| `CondemnedErasureUnpinned.cfg` | fails PinsNameRetainedVersions as expected | 188 | 1 s |

The on-demand configs (`ReclaimWide`, `ReclaimTwoWide`, `ReclaimTwoFaults`, `ReclaimBranchesWide`, `ReclaimForkWide`) were not run for this change. With the licence's states (`l`, `LicenceEnds`, `rcred`), `ReclaimBranches.cfg` grew from 107,467 to 1,660,308 states and `ReclaimBranchesTag.cfg` from 519,575 to 6,316,253; `ReclaimRelease.cfg` now runs with the sweep on (4,804,048).

**V4 measured** (`reclaims.condemned_versions_of`, the door's read, five rounds each through a counting proxy):

| Store | Before: 10,000 records (9,990 empty), the door reads each | After: 10 records, one per table with entries |
| --- | --- | --- |
| moto 5 | 10,020 requests, 478 s (one round) | 21 requests, 1,012 ms (median of five) |
| RustFS 1.0.3 | 10,020 requests, 461 s (one round) | 21 requests, 928 ms (median of five) |

The 21 requests are the listing, the ten reads and the ten claim reads that name each table; every request passes the counting proxy, which costs about 45 ms a request on this host shared with other agents, so the times compare the two layouts and are no measure of the store. A table with no reclaim in flight costs the door nothing.

### 15. A batch is asked for one incarnation of its ref; the model gate and the lane cost what a person can wait for (2026-11-18)

**ListedOn: the defect §14 left, found and fixed.** A reclaimer listed a branch's victims on one incarnation and asked
for its batch by the branch's NAME; the catalog resolved the name at the condemn, so once the branch was deleted and made
again under it (Lance numbers each incarnation from its fork, so the numbers repeat) the batch condemned, licensed and
deleted the new incarnation's versions of the numbers listed: no door named them (condemned first) and no hold was broken,
but versions younger than the retention went. The same holds for main, whose identity is the table's: a registered
table deregistered and registered again at its location is another uuid under another policy, and a pass of the first
deleted the second's versions by number. Reproduced first, on moto, by the test below with the check removed (branch:
`b` held `[5]` of `[3, 4, 5]`; main: `[3]` of `[1, 2, 3]`).

- **The model first** (`Reclaim.tla`): `RBegin` reads the ref's identity and lists that incarnation's versions past
  retention (one step: a listing of a later incarnation names the earlier identity all the same and is refused); the
  batch asks for NUMBERS naming that identity, and `RCondemn` resolves the ref's name at the condemn and chooses the
  standing versions of those numbers there. The guard `ListedOn` refuses the batch where the name holds another identity,
  nothing condemned. A new invariant, `CondemnsAsListed` (every entry names a version some reclaim chose, `version/delete`
  named or a fork cleanup took), is checked by every holding gate config. `ReclaimListedElsewhere.cfg` (ListedOn removed)
  fails it in 11 steps: the reclaim lists `b1`'s version 2, `b` is deleted and made again (`b2`) and written twice, and
  the batch condemns `b2`'s version 2, which never aged; with `DeletesCondemned` in its place the same config fails that
  too (7,486 states), the retention's loss. Every other must-fail config still fails as before.
- **The guard**: a reclaimer reads its ref's identity (`condemned_versions.identity_of`: the table's uuid for main, the
  uuid Lance lists last in a branch's `branch_identifier` for a branch) BEFORE it opens the handle it lists its victims
  on, and names it with every batch; `condemned_versions.Batch` reads the ref's identity, the one its entries are keyed by,
  and raises `ListedElsewhereError` where it is another (`_require_listed_on`), as `condemn_named` (`version/delete`)
  does. Every caller: a pass (`table_pass._identity`, read before the pass's handle opens; the `listed_on` field of
  `maintenance/condemn`'s `CondemnRequest`), `maintenance/run` (`reclaims.in_process` reads it before the door opens the
  ref), an erasure's verification and its release (`reclaims.erasure_reclaims`, asked for in `erasure.reclaim_refs`
  before `_head` opens the ref), and `version/delete` (read before its handle opens).
- **What a caller sees**: 409 ConcurrentModification (code 14), nothing condemned, retryable after listing again. A pass
  is parked (`vend_denied`) and asked about again after `retry_seconds`; `maintenance/run` and `version/delete` answer
  the 409; an erasure's `history:<ref>` surface fails naming it, and its next tick tries again.
- **The test**: `tests/integration/test_a_reclaim_condemns_before_it_deletes.py::test_a_batch_names_the_incarnation_its_versions_were_listed_on`
  (branch, main): the real pass held as it asks the real catalog for its batch; branch `b` deleted, made again under
  its name and written to the same numbers (or the table deregistered and registered again at its location); let go.
  With the check: 409 code 14, nothing of the new incarnation deleted, and the next pass reclaims it by its own listing.
  With the check removed: 2 of 2 fail on moto and 2 of 2 on the pinned RustFS 1.0.3 (the pass deletes `b`'s 3 and 4, or
  the registered table's 1 and 2).

**The model gate's cost.** `make tla-check` took 2,275 s (38 min) on this machine (four cores shared with other agents,
two TLC workers, the gate run beside this change's own tests for part of it), 27 gate configs over 25 s, the largest
`ReclaimBranchesTag` at 249 s. The owner's rule: small constants, a gate a person can wait for, about 12 minutes in all
and no gate config over about 30 s. Every config over the bound was narrowed while its property is still exercised, and
its wide form kept `ON DEMAND`:

- a config with liveness properties, whose check over the behaviour graph cost ten to twenty times its safety check, was
  split in two: the safety config at the full constants (every invariant) and a `<Name>Liveness.cfg` with one failure
  fewer (or one caller fewer), the original kept as `<Name>Full.cfg`, on demand;
- a Reclaim config dropped a fault, a door or Lance's either-order read for the measured tags-first order where an
  existing `Wide` config, on demand, already covers the wide form, or kept a new `Wide` copy;
- the one must-fail config narrowed, `CommitPathFinishPassed.cfg` (38 s → 3 s, `Mix` lane → overwrite), still fails
  `EveryFileSealed` for its stated reason: its trace is the append's writer dying after its version landed and before it
  sealed, `ReconcileFails`, then `ReconcileFinish` moving the mark past it, then stuttering.

After: **810 s (13.5 min), no gate config over 20 s.** About 1.5 minutes over the bound: what is left is the cost of 233
small configs (the median 2 s, most of it TLC's own start), and the next cut would check liveness with no failure at all
(`ErasureLiveness`, `CondemnedErasureLiveness`, the `TableLifecycle*Liveness` configs, 6 to 18 s each), which this change
did not take. `scripts/tla_check.sh` now prints each config's states and seconds and takes `TLC_TIMEOUT`, a limit per
config past which it is reported not finished, never as holding. The table of every config is `specs/README.md`; the
configs this change touched:

| Config | Before: states, seconds | After: run, states, seconds |
| --- | --- | --- |
| `catalog/CommitPathFinishPassed.cfg` | 385,359, 38 s | gate, 12,041, 2 s |
| `catalog/CommitPathGap.cfg` | 440,234, 58 s | gate, 440,234, 10 s |
| `catalog/CommitPathGapFull.cfg` | 440,234, 58 s (as `catalog/CommitPathGap.cfg`) | on demand, 440,234, 78 s |
| `catalog/CommitPathGapLiveness.cfg` | (new) | gate, 108,153, 12 s |
| `catalog/CommitPathLane.cfg` | 392,280, 41 s | gate, 392,280, 7 s |
| `catalog/CommitPathLaneLiveness.cfg` | (new) | gate, 95,104, 9 s |
| `catalog/CondemnedErasure.cfg` | 76,065, 96 s | gate, 76,065, 4 s |
| `catalog/CondemnedErasureFull.cfg` | 76,065, 96 s (as `catalog/CondemnedErasure.cfg`) | on demand, 76,065, 79 s |
| `catalog/CondemnedErasureLiveness.cfg` | (new) | gate, 21,048, 18 s |
| `catalog/CondemnedErasureRefsInTheGrace.cfg` | 195,397, 80 s | gate, 195,397, 7 s |
| `catalog/CondemnedErasureRefsInTheGraceFull.cfg` | 195,397, 80 s (as `catalog/CondemnedErasureRefsInTheGrace.cfg`) | on demand, 195,397, 81 s |
| `catalog/CondemnedErasureRefsInTheGraceLiveness.cfg` | (new) | gate, 18,036, 7 s |
| `catalog/Erasure.cfg` | 65,124, 76 s | gate, 65,124, 4 s |
| `catalog/ErasureConcurrent.cfg` | 122,841, 63 s | gate, 122,841, 4 s |
| `catalog/ErasureConcurrentFull.cfg` | 122,841, 63 s (as `catalog/ErasureConcurrent.cfg`) | on demand, 122,841, 69 s |
| `catalog/ErasureConcurrentLiveness.cfg` | (new) | gate, 21,286, 9 s |
| `catalog/ErasureFull.cfg` | 65,124, 76 s (as `catalog/Erasure.cfg`) | on demand, 65,124, 101 s |
| `catalog/ErasureLiveness.cfg` | (new) | gate, 19,179, 17 s |
| `catalog/ErasureRefsInTheGrace.cfg` | 159,837, 69 s | gate, 159,837, 5 s |
| `catalog/ErasureRefsInTheGraceFull.cfg` | 159,837, 69 s (as `catalog/ErasureRefsInTheGrace.cfg`) | on demand, 159,837, 60 s |
| `catalog/ErasureRefsInTheGraceLiveness.cfg` | (new) | gate, 17,080, 6 s |
| `catalog/GrantsChild.cfg` | 143,976, 101 s | on demand, 143,976, 57 s |
| `catalog/GrantsChildLiveness.cfg` | (new) | gate, 10,004, 6 s |
| `catalog/GrantsChildSafety.cfg` | (new) | gate, 143,976, 4 s |
| `catalog/GrantsDeclared.cfg` | 1,397,283, 39 s | gate, 55,604, 3 s |
| `catalog/GrantsDeclaredFull.cfg` | 1,397,283, 39 s (as `catalog/GrantsDeclared.cfg`) | on demand, 1,397,283, 27 s |
| `catalog/TableLifecycle.cfg` | 243,416, 61 s | gate, 243,416, 7 s |
| `catalog/TableLifecycleCascade.cfg` | 234,291, 60 s | gate, 234,291, 7 s |
| `catalog/TableLifecycleCascadeFull.cfg` | 234,291, 60 s (as `catalog/TableLifecycleCascade.cfg`) | on demand, 234,291, 34 s |
| `catalog/TableLifecycleCascadeLiveness.cfg` | (new) | gate, 94,893, 15 s |
| `catalog/TableLifecycleCreate.cfg` | 167,698, 39 s | gate, 167,698, 5 s |
| `catalog/TableLifecycleCreateFull.cfg` | 167,698, 39 s (as `catalog/TableLifecycleCreate.cfg`) | on demand, 167,698, 23 s |
| `catalog/TableLifecycleCreateLiveness.cfg` | (new) | gate, 61,232, 10 s |
| `catalog/TableLifecycleFull.cfg` | 243,416, 61 s (as `catalog/TableLifecycle.cfg`) | on demand, 243,416, 41 s |
| `catalog/TableLifecycleLiveness.cfg` | (new) | gate, 101,648, 16 s |
| `catalog/TableLifecycleUndrop.cfg` | 234,695, 41 s | gate, 234,695, 8 s |
| `catalog/TableLifecycleUndropFull.cfg` | 234,695, 41 s (as `catalog/TableLifecycleUndrop.cfg`) | on demand, 234,695, 34 s |
| `catalog/TableLifecycleUndropLiveness.cfg` | (new) | gate, 103,269, 17 s |
| `maintenance/Reclaim.cfg` | 149,943, 9 s | gate, 149,943, 5 s |
| `maintenance/ReclaimBranches.cfg` | 1,660,468, 75 s | gate, 289,358, 8 s |
| `maintenance/ReclaimBranchesTag.cfg` | 5,943,373, 249 s | gate, 59,687, 3 s |
| `maintenance/ReclaimBranchesWide.cfg` | on demand, not run | on demand, not run |
| `maintenance/ReclaimFork.cfg` | 1,131,849, 40 s | gate, 232,014, 7 s |
| `maintenance/ReclaimForkWide.cfg` | on demand, not run | on demand, not run |
| `maintenance/ReclaimListedElsewhere.cfg` | 1,508, 1 s | gate, 938, 1 s |
| `maintenance/ReclaimLive.cfg` | 194,202, 40 s | gate, 3,899, 3 s |
| `maintenance/ReclaimLiveRestore.cfg` | (new) | gate, 58,101, 8 s |
| `maintenance/ReclaimLiveWide.cfg` | 194,202, 40 s (as `maintenance/ReclaimLive.cfg`) | on demand, not run |
| `maintenance/ReclaimPolicies.cfg` | 168,224, 6 s | gate, 168,224, 6 s |
| `maintenance/ReclaimRelease.cfg` | 4,859,836, 136 s | gate, 89,301, 3 s |
| `maintenance/ReclaimReleaseWide.cfg` | 4,859,836, 136 s (as `maintenance/ReclaimRelease.cfg`) | on demand, not run |
| `maintenance/ReclaimReleaseWriter.cfg` | (new) | gate, 368,101, 12 s |
| `maintenance/ReclaimSweep.cfg` | 207,462, 7 s | gate, 207,462, 6 s |
| `maintenance/ReclaimTwo.cfg` | 499,299, 16 s | gate, 426,270, 12 s |
| `maintenance/ReclaimTwoFaults.cfg` | on demand, not run | on demand, 3,550,974, 206 s |
| `maintenance/ReclaimTwoRefs.cfg` | 2,003,599, 80 s | gate, 429,952, 15 s |
| `maintenance/ReclaimTwoRefsWide.cfg` | 2,003,599, 80 s (as `maintenance/ReclaimTwoRefs.cfg`) | on demand, not run |
| `maintenance/ReclaimTwoWide.cfg` | on demand, not run | on demand, 11,699,903, 495 s |
| `maintenance/ReclaimWide.cfg` | on demand, not run | on demand, not run |
| `maintenance/ReclaimWriter.cfg` | 137,345, 5 s | gate, 137,345, 4 s |
| `maintenance/WorkQueueLive.cfg` | 81,845, 31 s | gate, 9,640, 6 s |

`make tla-check-full` (`TLC_TIMEOUT=30m`), once, beside the lane's slow case and this change's tests: stopped before it reached
every on-demand config. Every on-demand config it ran holds, among them each `Full` form this change made
(`CommitPathGapFull`, `CondemnedErasureFull`, `CondemnedErasureRefsInTheGraceFull`, `ErasureFull`,
`ErasureConcurrentFull`, `ErasureRefsInTheGraceFull`, `GrantsChild`, `GrantsDeclaredFull`, the four
`TableLifecycle*Full`), `ReclaimTwoFaults` and `ReclaimTwoWide`. Two did not finish within 30 minutes (`ErasureWide`,
`GrantsChildTwice`, neither touched here), and eight were not run: `ReclaimBranchesWide`, `ReclaimForkWide`,
`ReclaimLiveWide`, `ReclaimReleaseWide`, `ReclaimTwoRefsWide`, `ReclaimWide`, `WorkQueueLiveWide`, `WorkQueueWide`.
So the Reclaim model with ListedOn is shown to hold in its gate configs, not yet in its wide forms.

**The lane's wait.** `make lifecycle-rustfs-check` ran `test_the_store_refuses_a_maintain_session_once_the_licence_it_deletes_under_ends`
in its background, which waits a licence out (17 minutes: RustFS 1.0.3 issues no session shorter than 900 s), so the
lane took about 17 minutes where its other groups take about 6. The fact under test is the store's (it refuses an
expired session's delete); rask's part, that a credential's expiry is no later than the licence (`covers`), is pinned by
`test_a_pass_deletes_only_under_a_vend_the_store_refuses_by_its_licences_end` and
`test_a_maintain_vend_dies_by_the_licence_of_a_batch_asked_after_it`. The slow case now runs only when asked
(`RASK_LIFECYCLE_WAIT_LICENCE=1`, `make lifecycle-rustfs-check-full`; the test skips without it). Measured 2026-11-18 on
RustFS 1.0.3: the default lane 348 s, all passed; the full lane 1,082 s, the slow case 1 of 1 passed in 1,061 s.

**Net tests**: +2 collected (the ListedOn test's branch and main cases on moto; two more on the lane), none removed.

## The four facts

- **GitOps**: met; no new chart value.
- **Cloud native**: met; every exclusion is a record or a tag on the store any replica reads.
- **Lance-namespace/table-idiomatic**: improved. `version/delete` does what the spec says; a pin is a Lance tag; cleanup
  is restricted by its documented `versions`.
- **Zero trust**: improved. The maintain vend reaches nothing on the control root and its policy shrinks from 1,964 to 912
  characters at two 63-character buckets (§13); no vend can write under `_refs/` (measured). A clock enters what a reclaim
  may delete on a branch alone (§14, C): a licence ends at its instant, and every reclaimer deletes under a short-lived
  credential minted for it that the store refuses by then, the catalog's own reclaims included (a session its web
  identity mints per batch); a catalog on a key pair is the stated residual.

## Not modelled

Staged manifests and committed-file records; a drop,
undrop and purge; the erasure's scheduling and notices beyond `CondemnedErasure.tla`'s overrides; the release taking a
dead door's pin on a residual; two tags on one version; a branch whose own versions name its fork's files; a hold lifted;
batches larger than the model's versions; Q2; main's identity changing (a table registered again at its location is
another uuid, §15: checked by the code and its test, and here main's identity never changes).

## Appendix: the measurement scripts

Scratch, deleted after they ran: a driver starting the pinned RustFS as a process of its own with a root pair made for
the run, `measure.py` (sections 1 to 4 above: 1,000 pin trials through two `lance.dataset` handles, 1,000 record trials
through two boto3 clients, 16 threads condemning with If-Match retries, `cleanup_old_versions` on the wire through
`store_requests.counted` with `holding` at its first `_refs/` and `_versions/` listing), `measure_vend.py` (STS
AssumeRole by a non-root user holding an `s3:*` canned policy over the bucket, with `build_session_policy(..., "write")`
and `"maintain"`, then a put and a delete of `_refs/tags/rask-pin-<hex>.json`), and a pytest importing the estate fixture
of `test_a_tables_pass_deletes_nothing_a_door_answered_for.py` that counted the three operations of §6.
