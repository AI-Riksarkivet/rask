------------------------------- MODULE Erasure -------------------------------
(***************************************************************************)
(* An erasure obligation on one table, from the obligation the catalog     *)
(* records to the release of what pins its residual and the cleanup after. *)
(*                                                                         *)
(* The catalog rewrites nothing (ADR 0163). A bring-your-own job rewrites  *)
(* each ref's head; the verification door judges what it left and settles  *)
(* the obligation; the release tick deletes the refs that pinned a residual *)
(* at the end of the grace period; maintenance's cleanup then reclaims the *)
(* versions nothing pins. Each action is one effect, in the code's order:  *)
(*                                                                         *)
(*   job        conftest ErasureJob, a client's commits (not the catalog)  *)
(*   verify     api/v1/endpoints/erasure.py verify_erasure:                *)
(*              services/erasure.verify (heads, tags, reclaim, verify),    *)
(*              the tag announcements, _schedule_release (the standing     *)
(*              record read, the notices filed, the record, the notices),  *)
(*              erasure_obligations.settle                                 *)
(*   release    services/erasure_release._release (plan and claim, the     *)
(*              deletes, "recreating", the recreates, _finish)             *)
(*   cleanup    maintenance's cleanup_old_versions: a version no ref pins  *)
(*              and that is not a head goes                                *)
(*   others     a tag, a branch or a restore made by another caller, a     *)
(*              registered clone and its end, a recoverable drop and its   *)
(*              undrop, the obligation that follows this one on a derived  *)
(*              table                                                      *)
(*                                                                         *)
(* Main's versions: 1 holds the subject in rows a read answers ("visible"); *)
(* 2 is a writer's plain delete, the subject's rows behind a deletion      *)
(* vector ("hidden"), tagged t1 (the shape of                              *)
(* test_erasure_releases_a_pinning_branch_after_its_notice.py), and 1 is   *)
(* tagged t0, a tag the verification drops; 3 is the job's rewrite; 4 a    *)
(* restore's commit, holding what it restored. Branch B is cut from main   *)
(* 2, so it pins it; C is cut by another caller, from a main version or    *)
(* from B. A branch pins the version it is cut from while                  *)
(* it stands (erasure.py, "THE RECLAIM ORDER IS FORCED BY THE FORMAT").    *)
(* Tag tc is a registered clone's (lakehouse.clone_sources, ADR 0198 §1):  *)
(* the catalog writes it on the version the clone reads, under the history *)
(* fence as a ref write, and only the clone's end deletes it.              *)
(*                                                                         *)
(* A verification's notice is reduced to the deadline it names: verifier p *)
(* names its own grace end, also written p, and the record names the       *)
(* deadline of the verification that wrote it. A person's reader keeps the *)
(* first copy of an event id, so a notice with another deadline than the   *)
(* record's, under the release's id, may be the only one a person ever     *)
(* gets (the person added since an earlier verification read the owners).  *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. The release reads the table's branches again when it runs, so a   *)
(*       branch cut from a pinning branch in the grace goes with it        *)
(*       (erasure_release._plan). ErasureNoReread removes it: Lance        *)
(*       refuses to delete a branch another is cut from, so the release    *)
(*       fails on every tick (EveryReleaseFinishes).                       *)
(*   A2. The release records where each branch comes back before it        *)
(*       deletes one (erasure_release._claim, before _delete).             *)
(*       ErasureNoPlanFirst removes it; a tick that dies after a delete    *)
(*       loses the branch (EveryReleasedBranchComesBack).                  *)
(*   A3. A reclaim and a write of a ref never overlap on one table: the    *)
(*       history fence (lakehouse/history_fences.py, held by erasure.py    *)
(*       _reclaim, dataplane.create_tag and create_branch, and             *)
(*       clone_sources' tag take). ErasureNoFence removes it, and a tag    *)
(*       names a deleted version (PinsNameRetainedVersions).               *)
(*   A4. An obligation's record changes only under the ETag it was read    *)
(*       by, and a released one is never written open again                *)
(*       (erasure_obligations._settling, records.mutate_json).             *)
(*       ErasureNoCAS lets a slower verification's open write land over a  *)
(*       release (ReleasedStaysReleased).                                  *)
(*   A5. Cleanup runs (maintenance's hourly audit and pass) on a table     *)
(*       nothing holds: it is fair here. A table left in the trash is not  *)
(*       cleaned, so its undrop is fair too.                               *)
(*   A6. A registered clone told that it holds a release is ended by its   *)
(*       owner (EndClone is fair). ErasureCloneOutlives removes it: the    *)
(*       release waits for good and the subject stays in the version the   *)
(*       clone reads (ErasedOnceReleased): the cost the owner accepted     *)
(*       ("Wait and report", ADR 0233 §9).                                 *)
(*                                                                         *)
(* THE GUARDS the fixes of ADR 0233 add, each removed by a config that     *)
(* must fail:                                                              *)
(*   G1. The release takes every ref standing on a residual version, read  *)
(*       again when it runs (a tag on one, a branch cut from one, with     *)
(*       its subtree), and reclaims the residual itself before it ends,    *)
(*       all under a hold of the table's history no ref write shares       *)
(*       (erasure_release._release, HistoryWork.RELEASE). ErasureCopyTag   *)
(*       and ErasureCutBranch remove it: a tag or a branch made on a       *)
(*       residual in the grace, or after the release ran and before the    *)
(*       cleanup, keeps the subject for good (ErasedOnceReleased).         *)
(*   G2. The verification files the release's notices before it writes     *)
(*       the release's record (announcement_intents), a verification that  *)
(*       meets the record tells them again under the same ids, the         *)
(*       finish sends what a dead one filed, and the release tick waits    *)
(*       while a filing stands. ErasureNoticeLost removes it, with no      *)
(*       verification coming again (Reverify FALSE).                       *)
(*   G3. The verification files each tag's announcement before it drops    *)
(*       the tag, and the finish sends the filing of a verification that   *)
(*       ended (the door's own start, or maintenance's hourly finish).     *)
(*       ErasureTagEventLost removes it.                                   *)
(*   G4. The release never takes a registered clone's tag: while one       *)
(*       stands on a residual it does nothing, holds the release open and  *)
(*       reports it on the obligation and to the people the release costs *)
(*       (erasure_release._held_by_clones). ErasureReleaseTakesCloneTag    *)
(*       removes it: the release deletes the clone's tag and reclaims the  *)
(*       version the clone reads (NoCloneIsBroken).                        *)
(*   G5. A verification reads the release that stands before it files any *)
(*       notice, and files the notices of the record that stands, its      *)
(*       deadline included; each notice lands only while the record it     *)
(*       describes stands (RecordHeld with the record's due_at), so a      *)
(*       verification that lost the record's create files nothing that     *)
(*       lands (endpoints/erasure._schedule_release). ErasureNoticeDeadline *)
(*       removes it: a second verification files its own deadline, meets   *)
(*       the record, dies, and the finish sends its filing                 *)
(*       (NoticeOnlyForARecordedRelease).                                  *)
(*   G6. Whoever puts a ref the release takes, once its record stands,   *)
(*       is told before it runs (the owner's ruling, ADR 0233 §9): the ref *)
(*       door reads the release under its hold of the table's history, files *)
(*       the creator's notice before the ref (announcement_intents, landed  *)
(*       once the ref and the record with its deadline stand), sends it     *)
(*       after; the finish sends what a dead door filed, and the release   *)
(*       tick waits while a filing stands (erasure_release.release_due).   *)
(*       ErasureCreatorUntold removes it: the door sends after the ref with *)
(*       nothing filed, dies between the two, and the release takes the   *)
(*       ref of a creator nobody told (ACreatorIsToldBeforeTheRelease).    *)
(*   G7. The verification writes the release's mark and its record while  *)
(*       it holds the table's history as a scheduling, which no ref write  *)
(*       shares (HistoryWork.SCHEDULE): a ref door either finished before  *)
(*       the record stood or read it. ErasureCreatorRace removes it: a door *)
(*       reads no release, the record is written, its ref lands, and its   *)
(*       creator is never told (ACreatorIsToldBeforeTheRelease).           *)
(*   G8. ONE predicate decides which refs the release takes, shared by its *)
(*       claim, its deletes and every ref door: a ref stands on a residual *)
(*       version or in a taken subtree at the tick's own re-read, whatever *)
(*       its name (erasure_release._taken, under _plan and takes). A tag   *)
(*       its owner moved onto a clean version, or a branch deleted and cut *)
(*       again from clean history, pins nothing and is left alone.         *)
(*       ErasureNameTaking removes it: the release also takes the names it *)
(*       recorded, wherever they stand now, while the door judges by       *)
(*       placement, so the tag moved off the residual in the grace is       *)
(*       taken from a creator nobody told (ACreatorIsToldBeforeTheRelease). *)
(*   G9. The release ends inside its hold, deleting its record first and    *)
(*       its mark after (erasure_release._release, _finish): no state has a *)
(*       record without its mark. ErasureFinishOutsideHold moves the finish  *)
(*       out of the hold: a verification records the release again between  *)
(*       the two deletes and the second removes its new mark; and            *)
(*       ErasureFinishMarkFirst deletes the mark first and dies between       *)
(*       (ARecordHasItsMark).                                                *)
(*   G10. The catalog's finish judges a ref door's filing while it holds the *)
(*       table's history as a scheduling, which no ref write shares          *)
(*       (announcing.Announcer.landed, RefOnARelease): a door stalled past   *)
(*       the filings' lease while it holds the table's history keeps its      *)
(*       filing. ErasureCreatorStalled removes it: the finish removes the    *)
(*       filing of a door that has not yet written its ref, the door writes  *)
(*       it, and the tick takes it before the door sends (ACreatorIsTold...). *)
(*   G11. A ref door confirms its hold just before its ref (Hold.confirm):   *)
(*       one whose hold lapsed while it stalled writes nothing and removes  *)
(*       its filing. ErasureCreatorLapsed removes it: a door that read no    *)
(*       release lapses, the release is recorded beside it, the door writes  *)
(*       its ref under the record, and the release takes it from a creator   *)
(*       nobody told (ACreatorIsToldBeforeTheRelease).                       *)
(*   ErasureRestore is a limit, not a guard: a restore of a residual made  *)
(*   once the obligation is released brings the subject back to main's     *)
(*   head (the release's `bound` names it in words).                       *)
(*                                                                         *)
(* NOT MODELLED: the predicate and its digest; index segments (one more    *)
(* kind of residual a head must clear); external bases and MemWAL shards   *)
(* (held surfaces, which keep the obligation open); the retention window   *)
(* (retain_days 0); a clone on a branch version (it holds the release as   *)
(* one on main does); late derived tables (listed and paged, a ruling:     *)
(* erasure_derived.watch_released); lineage's feed; which person a notice  *)
(* reaches (a notice is its deadline here).                                *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Verifiers,     \* the verification requests that may run at once
          MaxFaults,     \* how many steps may fail or processes die in one behaviour
          HasBranch,     \* whether branch B (cut from main 2) stands at the start
          HasDerived,    \* whether a table derived from this one is on the edge list
          CopyTags,      \* another caller may tag a retained version
          Cuts,          \* another caller may cut branch C: "none", "B" (from B), "main" (from a main version)
          Restores,      \* another caller may restore a retained version once the obligation is released
          DropUndrop,    \* a recoverable drop of the table and its undrop
          Reverify,      \* a verification comes again while the obligation is open (the job's, or anyone's)
          Clones,        \* another caller may register a clone of a retained version
          ClonesEnd,     \* A6
          Reread,        \* A1
          PlanFirst,     \* A2
          Fenced,        \* A3
          CAS,           \* A4
          Sweep,         \* G1
          NoticeFiled,   \* G2
          TagsFiled,     \* G3
          CloneWaits,    \* G4
          NoticeOfTheStanding, \* G5
          CreatorTold,   \* G6
          ScheduleHeld,  \* G7
          Moves,         \* another caller may move tag t1, which the release names, or delete B and cut it again
          StalledDoors,  \* a ref door may stall past the filings' lease, its hold renewed or lapsed meanwhile
          PlacedOnly,    \* G8
          FinishHeld,    \* G9: the finish inside the release's hold
          MarkFirst,     \* G9 removed the other way: the finish deletes the mark before the record
          FinishFenced,  \* G10
          RefConfirmed   \* G11

Versions == 1..4
Tags     == {"t0", "t1", "t2", "tc"}
UserTags == Tags \ {"tc"}
Branches == {"B", "C"}
\* The refs a door may put: tag t2, branch C, and the release's own recorded names t1 (moved) and B (cut again).
CRefs    == {"t2", "C", "t1", "B"}
NoRel    == [on |-> FALSE, refs |-> {}, res |-> {}, phase |-> "pending", tags |-> {}, plan |-> {}, at |-> 0, dl |-> "none"]

VARIABLES retained,   \* main's versions with their files on storage
          subj,       \* what each version holds of the subject: "none", "hidden" (bytes, behind a deletion vector), "visible"
          head,       \* main's head
          tags,       \* each tag's version, 0 when it does not stand
          br,         \* each branch: whether it stands, the main version it is cut from (0: cut from B), whether its head holds the subject
          live,       \* the table is registered (not in the trash)
          ob,         \* the obligation: "open" or "released"
          followed,   \* an obligation on the derived table follows this one
          vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid,   \* each verification
          vdl,        \* the deadline each verification's notices name
          rel,        \* the release record on the control root
          due,        \* its grace period has ended
          rpc, rplan, \* the release tick, and the plan it keeps in memory when it does not record it first (A2 removed)
          dropped, announced,    \* tags verifications deleted, and those announced on the control lane
          toldDl, recDls,        \* the deadlines people were told; the deadlines of every release record written
          nfiled,                \* the standing filings of the release's notices: by whom ("left" once it ended) and the deadline filed (G2)
          tfiled,                \* the tags each running verification filed before it drops them, and those "left" by one that ended (G3)
          gone,                  \* versions deleted from storage
          deletedByRelease,      \* branches a release deleted
          clone, cloned,         \* the version a registered clone reads (0: none stands); one was registered
          held,                  \* the release found a clone holding it and said so (G4)
          cut, copied, restored, dropDone,
          badDrop, releasedOn, reopened,  \* history: a tag dropped wrongly; the evidence a release was made on; a released obligation written open
          faults,
          cpc, ckind, cver, cdl, \* the ref door another caller runs: its step, the ref it puts and where, the deadline it read
          chold,                 \* the door's hold of the table's history stands (it lapses only where a door stalls)
          cfiled,                \* the creators' notices filed: by the door, or "left" once it ended (G6), each with where its ref goes
          ctold,                 \* the creators told, each with the deadline told
          cput,                  \* each ref a door put: the deadline of the record that stood when it landed, "none" if none did
          moved, recut, bgone,   \* history: t1 moved by a door; B cut again by a door; B deleted by another caller
          untold,                \* history: the release deleted a ref a door put under its record, its creator untold
          rmark                  \* the release's mark in the table's history fence (erasure_release.mark_key)

\* Everything but the ref door's, the mark and the faults.
Base == <<retained, subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl,
          rel, due, rpc, rplan, dropped, announced, toldDl, recDls, nfiled, tfiled, gone, deletedByRelease, clone, cloned, held,
          cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>
CVars == <<cpc, ckind, cver, cdl, chold, cfiled, ctold, cput, moved, recut, bgone, untold>>
CKeep == UNCHANGED CVars /\ UNCHANGED rmark
vars == <<Base, faults, CVars, rmark>>

Init ==
  /\ retained = {1, 2}
  /\ subj = [v \in Versions |-> CASE v = 1 -> "visible" [] v = 2 -> "hidden" [] OTHER -> "none"]
  /\ head = 2
  /\ tags = [t \in Tags |-> CASE t = "t0" -> 1 [] t = "t1" -> 2 [] OTHER -> 0]
  /\ br = [b \in Branches |-> [on |-> b = "B" /\ HasBranch, fork |-> 2, dirty |-> b = "B" /\ HasBranch]]
  /\ live = TRUE
  /\ ob = "open"
  /\ followed = FALSE
  /\ vpc = [p \in Verifiers |-> "idle"]
  /\ vclean = [p \in Verifiers |-> FALSE] /\ vdel = [p \in Verifiers |-> {}] /\ vpins = [p \in Verifiers |-> {}]
  /\ vres = [p \in Verifiers |-> {}]
  /\ vsettled = [p \in Verifiers |-> FALSE] /\ vdropped = [p \in Verifiers |-> {}] /\ vevid = [p \in Verifiers |-> FALSE]
  /\ vdl = [p \in Verifiers |-> "none"]
  /\ rel = NoRel /\ due = FALSE /\ rpc = "idle" /\ rplan = {}
  /\ dropped = {} /\ announced = {} /\ toldDl = {} /\ recDls = {}
  /\ nfiled = {} /\ tfiled = [p \in Verifiers \cup {"left"} |-> {}]
  /\ gone = {} /\ deletedByRelease = {}
  /\ clone = 0 /\ cloned = FALSE /\ held = FALSE
  /\ cut = FALSE /\ copied = FALSE /\ restored = FALSE /\ dropDone = FALSE
  /\ badDrop = FALSE /\ releasedOn = TRUE /\ reopened = FALSE
  /\ faults = 0
  /\ cpc = "idle" /\ ckind = "tag" /\ cver = 0 /\ cdl = "none" /\ chold = FALSE
  /\ cfiled = {} /\ ctold = {} /\ cput = [r \in CRefs |-> "none"]
  /\ moved = FALSE /\ recut = FALSE /\ bgone = FALSE /\ untold = FALSE
  /\ rmark = FALSE

Fault   == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults

Holds(v) == subj[v] # "none"
\* The version a branch's head stands on, for its pin: its fork, or B's when cut from B. A clone's tag pins as any tag does.
Pinned(v) == (\E t \in Tags : tags[t] = v) \/ (\E b \in Branches : br[b].on /\ br[b].fork = v)
HeadsClean == ~Holds(head) /\ \A b \in Branches : br[b].on => ~br[b].dirty
Residual == {v \in retained : Holds(v)}
\* A registered clone's tag stands on one of `res` (held_by_clones).
CloneOn(res) == tags["tc"] \in res
\* What pins each residual, as `pinned_by` names it: never a clone's tag, which only the clone's end releases.
PinsOf(res) == {<<"tag", t>> : t \in {t \in UserTags : tags[t] \in res}} \cup {<<"branch", b>> : b \in {b \in Branches : br[b].on /\ br[b].fork \in res}}
\* settled: every head clean, every residual held only by refs (never by a head, nor by a clone), every derived table followed.
Evidence == HeadsClean /\ (\A v \in Residual : v # head /\ Pinned(v)) /\ ~CloneOn(Residual) /\ (HasDerived => followed)
\* The subject's bytes nowhere a read or a ref reaches.
Erased == (\A v \in retained : ~Holds(v)) /\ (\A b \in Branches : br[b].on => ~br[b].dirty)

ReclaimRunning == \E p \in Verifiers : vpc[p] = "delete"
\* The release's tick holds the table's history from its plan to its last step; with G9 its finish is inside the hold.
ReleaseRunning == rpc # "idle" /\ (FinishHeld \/ rpc \notin {"finish", "finish2"})
\* The ref door holds the table's history from its read of the release until its ref is written (dataplane.create_tag and
\* the rest); it sends after the hold has ended (ref_notices.putting_ref). A stalled door's hold may lapse meanwhile.
CreatorHolds == cpc \in {"file", "land"} /\ chold
RefOf(k) == CASE k = "tag" -> "t2" [] k = "branch" -> "C" [] k = "move" -> "t1" [] OTHER -> "B"
IsTag(r) == r \in {"t2", "t1"}
\* A ref stands where a door put it: a tag on main version `at`, a branch cut from main version `at` (0: from B).
StandsAt(r, at) == IF IsTag(r) THEN tags[r] = at ELSE br[r].on /\ br[r].fork = at

\* A filed notice lands, and a finish sends it, while the record stands, and with G5 only while it is the record the notice
\* describes (RecordHeld with the record's due_at).
Lands(f) == rel.on /\ (NoticeOfTheStanding => rel.dl = f.dl)
Mine(p) == {f \in nfiled : f.by = p}

---------------------------------------------------------------------------
\* The bring-your-own job: on every ref whose head holds the subject, delete by row id and compact (one commit here).

JobKeep == UNCHANGED <<vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl, rel, due, rpc, rplan,
                       dropped, announced, toldDl, recDls, nfiled, tfiled, gone, deletedByRelease, clone, cloned, held,
                       cut, copied, restored, dropDone, badDrop, releasedOn, reopened>> /\ CKeep

JobMain ==
  /\ ob = "open" /\ live /\ Holds(head) /\ 3 \notin retained \cup gone /\ NoFault
  /\ retained' = retained \cup {3} /\ head' = 3
  /\ UNCHANGED <<subj, tags, br, live, ob, followed>> /\ JobKeep

JobBranch(b) ==
  /\ ob = "open" /\ live /\ br[b].on /\ br[b].dirty /\ NoFault
  /\ br' = [br EXCEPT ![b].dirty = FALSE]
  /\ UNCHANGED <<retained, subj, head, tags, live, ob, followed>> /\ JobKeep

---------------------------------------------------------------------------
\* A verification (verify_erasure). The door resolves the table, so it runs only while the table is registered.

VKeep == UNCHANGED <<subj, live, followed, due, rpc, rplan, toldDl, recDls, deletedByRelease, clone, cloned, held, cut, copied, restored, dropDone>> /\ CKeep
VUnchanged == VKeep /\ UNCHANGED <<badDrop, nfiled, tfiled, vdl>>
Go(p, s) == vpc' = [vpc EXCEPT ![p] = s]

VStart(p) ==
  /\ vpc[p] = "idle" /\ ob = "open" /\ live /\ NoFault
  /\ Go(p, "heads")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* Step 1, _judge_heads.
VHeads(p) ==
  /\ vpc[p] = "heads" /\ NoFault
  /\ vclean' = [vclean EXCEPT ![p] = HeadsClean]
  /\ Go(p, "tags")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* Step 2, _drop_subject_tags: only once every head is clean, a tag over a version a read answers the subject from, and
\* never a registered clone's. A tag over a version holding it only behind a deletion vector is kept (it reads clean) and
\* becomes a pin.
ToDrop(p) == IF vclean[p] THEN {t \in UserTags : tags[t] # 0 /\ subj[tags[t]] = "visible"} ELSE {}

\* G3: what it drops is filed first (the door's `before_untag`), each tag's announcement landed once the tag is gone.
VTagFile(p) ==
  /\ vpc[p] = "tags" /\ TagsFiled /\ NoFault
  /\ tfiled' = [tfiled EXCEPT ![p] = ToDrop(p)]
  /\ Go(p, "drop")
  /\ VKeep /\ UNCHANGED <<badDrop, nfiled, vdl, retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

VTagDrop(p) ==
  LET drop == IF TagsFiled THEN {t \in ToDrop(p) : t \in tfiled[p]} ELSE ToDrop(p) IN
  /\ \/ vpc[p] = "drop" /\ TagsFiled
     \/ vpc[p] = "tags" /\ ~TagsFiled
  /\ NoFault
  /\ tags' = [t \in Tags |-> IF t \in drop THEN 0 ELSE tags[t]]
  /\ dropped' = dropped \cup drop
  /\ vdropped' = [vdropped EXCEPT ![p] = @ \cup drop]
  /\ badDrop' = (badDrop \/ (drop # {} /\ ~vclean[p]) \/ \E t \in drop : subj[tags[t]] # "visible")
  /\ Go(p, "reclaim")
  /\ VKeep /\ UNCHANGED <<nfiled, tfiled, vdl, retained, head, br, ob, vclean, vdel, vpins, vres, vsettled, vevid, rel, announced, gone, releasedOn, reopened>>

\* Step 3, _reclaim: under the history fence (A3), deepest first; main only when its head is clean. Lance reads the
\* refs once, then deletes against that reading (history_fences.py, "LANCE READS A TABLE'S REFS ONCE PER RECLAIM").
VReclaimRead(p) ==
  /\ vpc[p] = "reclaim" /\ NoFault
  /\ Fenced => ~ReleaseRunning /\ ~CreatorHolds
  /\ IF vclean[p] /\ ~Holds(head)
       THEN vdel' = [vdel EXCEPT ![p] = {v \in retained : v # head /\ ~Pinned(v)}] /\ Go(p, "delete")
       ELSE vdel' = [vdel EXCEPT ![p] = {}] /\ Go(p, "verify")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

VReclaimDelete(p) ==
  /\ vpc[p] = "delete" /\ NoFault
  /\ retained' = retained \ vdel[p]
  /\ gone' = gone \cup vdel[p]
  /\ Go(p, "verify")
  /\ VUnchanged /\ UNCHANGED <<head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, releasedOn, reopened>>

\* Step 4, _verify on a cold handle: the residuals, what pins them, and whether only pins hold them.
VVerify(p) ==
  /\ vpc[p] = "verify" /\ NoFault
  /\ vpins' = [vpins EXCEPT ![p] = PinsOf(Residual)]
  /\ vres' = [vres EXCEPT ![p] = Residual]
  /\ vsettled' = [vsettled EXCEPT ![p] = vclean[p] /\ Evidence]
  /\ vevid' = [vevid EXCEPT ![p] = Evidence]
  /\ Go(p, "emit")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vdropped, rel, dropped, announced, gone, releasedOn, reopened>>

\* The door announces each tag the verification dropped, AFTER the drops (verify_erasure), and removes its filing (G3).
VEmit(p) ==
  /\ vpc[p] = "emit" /\ NoFault
  /\ announced' = announced \cup (IF TagsFiled THEN {t \in tfiled[p] : tags[t] = 0} ELSE vdropped[p])
  /\ vdropped' = [vdropped EXCEPT ![p] = {}]
  /\ tfiled' = [tfiled EXCEPT ![p] = {}]
  /\ Go(p, "nfile")
  /\ VKeep /\ UNCHANGED <<badDrop, nfiled, vdl, retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vevid, rel, dropped, gone, releasedOn, reopened>>

Releasing(p) == vsettled[p] /\ vpins[p] # {}

NKeep == UNCHANGED <<badDrop, tfiled, retained, head, tags, br, ob, vclean, vdel, vpins, vres, vdropped, vevid, dropped, announced, gone, releasedOn, reopened>> /\ CKeep

\* G2: the release's notices are filed before its record is written. With G5 the release that stands is read first: one
\* that stands is taken (_take_the_earlier_release, while it names every pin) and its own notices filed, its deadline
\* named, and sent with no record written; none standing, the verification files the notices of the record it is about to
\* write, landed only while that record stands.
VNoticeFile(p) ==
  /\ vpc[p] = "nfile" /\ NoFault
  /\ IF NoticeOfTheStanding /\ Releasing(p) /\ rel.on
       THEN IF vpins[p] \subseteq rel.refs
              THEN /\ nfiled' = IF NoticeFiled THEN nfiled \cup {[by |-> p, dl |-> rel.dl]} ELSE nfiled
                   /\ vdl' = [vdl EXCEPT ![p] = rel.dl] /\ Go(p, "notify") /\ UNCHANGED vsettled
              ELSE /\ vsettled' = [vsettled EXCEPT ![p] = FALSE] /\ Go(p, "settle") /\ UNCHANGED <<nfiled, vdl>>
       ELSE /\ nfiled' = IF NoticeFiled /\ Releasing(p) THEN nfiled \cup {[by |-> p, dl |-> p]} ELSE nfiled
            /\ vdl' = [vdl EXCEPT ![p] = p] /\ Go(p, "schedule") /\ UNCHANGED vsettled
  /\ VKeep /\ NKeep /\ UNCHANGED rel

\* _schedule_release: the record, under the obligation's id, created only where none stands, and with G7 written with its
\* mark under a hold of the table's history no ref write shares: refused while a ref door holds it, the release is not
\* recorded, its filing removed, and the obligation stays open for the next verification. One there already was
\* written by another verification: without G5 it is taken while it names every pin, and its people are told again
\* under the ids the first telling used, while this verification's own filing, naming its own deadline, stands until its
\* notices go; with G5 its own filing is removed, never landing, and it reads again what stands.
VSchedule(p) ==
  /\ vpc[p] = "schedule" /\ NoFault
  /\ IF Releasing(p)
       THEN IF ~rel.on
              \* The scheduling's hold is refused beside a ref door's, and beside the release's own (which shares with nothing).
              THEN IF ScheduleHeld /\ (CreatorHolds \/ ReleaseRunning)
                     THEN /\ vsettled' = [vsettled EXCEPT ![p] = FALSE] /\ nfiled' = nfiled \ Mine(p) /\ Go(p, "settle")
                          /\ UNCHANGED <<rel, recDls, vdl, rmark>>
                     ELSE /\ rel' = [NoRel EXCEPT !.on = TRUE, !.refs = vpins[p], !.res = vres[p], !.dl = p]
                          /\ rmark' = TRUE
                          /\ recDls' = recDls \cup {p}
                          /\ UNCHANGED <<vsettled, nfiled, vdl>> /\ Go(p, "notify")
              ELSE IF NoticeOfTheStanding
                     THEN /\ nfiled' = nfiled \ Mine(p) /\ Go(p, "nfile")
                          /\ UNCHANGED <<rel, vsettled, vdl, recDls, rmark>>
                     ELSE /\ UNCHANGED <<rel, recDls, rmark>>
                          /\ vsettled' = [vsettled EXCEPT ![p] = vpins[p] \subseteq rel.refs]
                          /\ vdl' = [vdl EXCEPT ![p] = rel.dl]
                          /\ IF NoticeFiled /\ vpins[p] \subseteq rel.refs
                               THEN Go(p, "notify") /\ UNCHANGED nfiled
                               ELSE Go(p, "settle") /\ nfiled' = nfiled \ Mine(p)
       ELSE UNCHANGED <<rel, vsettled, nfiled, vdl, recDls, rmark>> /\ Go(p, "settle")
  /\ UNCHANGED <<subj, live, followed, rpc, rplan, toldDl, deletedByRelease, clone, cloned, held, cut, copied, restored, dropDone>>
  /\ due' = (due /\ rel.on)
  /\ UNCHANGED <<badDrop, tfiled, retained, head, tags, br, ob, vclean, vdel, vpins, vres, vdropped, vevid, dropped, announced, gone, releasedOn, reopened>>
  /\ UNCHANGED CVars

\* Then the notices, to the requester and the table's owners, naming the deadline it holds, and its filing is removed.
VNotify(p) ==
  /\ vpc[p] = "notify" /\ NoFault
  /\ toldDl' = toldDl \cup {vdl[p]}
  /\ nfiled' = nfiled \ Mine(p)
  /\ Go(p, "settle")
  /\ UNCHANGED <<subj, live, followed, due, rpc, rplan, recDls, deletedByRelease, clone, cloned, held, cut, copied, restored, dropDone, vdl, vsettled, rel>>
  /\ NKeep

\* erasure_obligations.settle: released when settled; a released one stays released (A4).
VSettle(p) ==
  /\ vpc[p] = "settle" /\ NoFault
  /\ ob' = CASE vsettled[p] -> "released" [] ob = "released" /\ CAS -> "released" [] OTHER -> "open"
  /\ reopened' = (reopened \/ (ob = "released" /\ ob' = "open"))
  /\ releasedOn' = IF vsettled[p] /\ ob = "open" THEN vevid[p] ELSE releasedOn
  /\ Go(p, "idle")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone>>

\* The request ends between two steps: it dies, or a step fails and the door answers 500. What it filed stands.
VDies(p) ==
  /\ vpc[p] \notin {"idle"} /\ Fault
  /\ Go(p, "idle")
  /\ vdropped' = [vdropped EXCEPT ![p] = {}]
  /\ nfiled' = {IF f.by = p THEN [f EXCEPT !.by = "left"] ELSE f : f \in nfiled}
  /\ tfiled' = [tfiled EXCEPT !["left"] = @ \cup tfiled[p], ![p] = {}]
  /\ VKeep /\ UNCHANGED <<badDrop, vdl>> /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* The finish of what an ended verification filed (the next verification's start, or maintenance's hourly finish): each
\* announcement whose change landed goes out, and the filing is removed.
FinishKeep == UNCHANGED <<retained, subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl,
                          rel, due, rpc, rplan, dropped, recDls, gone, deletedByRelease, clone, cloned, held, cut, copied, restored, dropDone,
                          badDrop, releasedOn, reopened>> /\ CKeep

FinishTags ==
  /\ tfiled["left"] # {} /\ NoFault
  /\ announced' = announced \cup {t \in tfiled["left"] : tags[t] = 0}
  /\ tfiled' = [tfiled EXCEPT !["left"] = {}]
  /\ UNCHANGED <<toldDl, nfiled>> /\ FinishKeep

FinishNotice ==
  /\ \E f \in nfiled :
       /\ f.by = "left" /\ NoFault
       /\ toldDl' = IF Lands(f) THEN toldDl \cup {f.dl} ELSE toldDl
       /\ nfiled' = nfiled \ {f}
  /\ UNCHANGED <<announced, tfiled>> /\ FinishKeep

---------------------------------------------------------------------------
\* The release tick (erasure_release._release), once the grace period has ended. It holds the history fence from its plan
\* to its last step (A3); with G1 as a release, which no ref write shares.

RFixed == UNCHANGED <<subj, head, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl, dropped, announced,
                     toldDl, recDls, nfiled, tfiled, clone, cloned, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>
          /\ UNCHANGED <<cpc, ckind, cver, cdl, chold, cfiled, ctold, moved, recut, bgone>>
RUnchanged == RFixed /\ UNCHANGED <<cput, untold, rmark>>

GraceEnds ==
  /\ rel.on /\ ~due /\ NoFault
  /\ due' = TRUE
  /\ UNCHANGED <<retained, tags, br, rel, rpc, rplan, gone, deletedByRelease, held>> /\ RUnchanged

\* G4: a registered clone's tag on a residual holds the release.
CloneHolds == CloneWaits /\ CloneOn(rel.res)

NamedBranches == {b \in Branches : <<"branch", b>> \in rel.refs}
NamedTags     == {t \in Tags : <<"tag", t>> \in rel.refs}

\* G8: THE ONE PREDICATE (erasure_release._taken), read off the table `tg` and `b` as they stand. A ref is taken while it
\* stands on a residual (G1: a tag on one, a branch cut from one or from a branch that is), or, with A1, is cut from a
\* branch the release takes. Without G1 only what the release recorded is taken, and still only while it stands so.
\* Without G8 the release also takes every name it recorded, wherever it stands now. With G4 never a clone's tag.
RecordedTag(t)    == rel.phase # "recreating" /\ t \in NamedTags \cup rel.tags
RecordedBranch(x) == rel.phase # "recreating" /\ (x \in NamedBranches \/ x \in rel.plan)
\* `byName`: the recorded names are taken wherever they stand, which only the release does, and only without G8.
TakenTagsBy(tg, byName) ==
  {t \in Tags : /\ tg[t] # 0 /\ (CloneWaits => t # "tc")
                /\ \/ Sweep /\ tg[t] \in rel.res
                   \/ RecordedTag(t) /\ (tg[t] \in rel.res \/ byName)}
OnRes(b, x) == b[x].on /\ IF b[x].fork = 0 THEN b["B"].on /\ b["B"].fork \in rel.res ELSE b[x].fork \in rel.res
Roots(b, byName) == {x \in Branches : b[x].on /\ ((Sweep /\ OnRes(b, x)) \/ (RecordedBranch(x) /\ (OnRes(b, x) \/ byName)))}
TakenBranchesBy(b, byName) == LET s == Roots(b, byName) IN IF Reread /\ "B" \in s /\ b["C"].on /\ b["C"].fork = 0 THEN s \cup {"C"} ELSE s
\* What the release takes; a door judges with byName FALSE, which is the same predicate whenever G8 holds.
TakenTagsOf(tg) == TakenTagsBy(tg, ~PlacedOnly)
TakenBranchesOf(b) == TakenBranchesBy(b, ~PlacedOnly)

\* _plan and _claim: the plan read from the table now, recorded before any delete (A2). A resumed tick keeps what the
\* record holds and adds what is taken now.
RClaim ==
  LET fresh == TakenBranchesOf(br)
      ts == TakenTagsOf(tags) IN
  /\ rel.on /\ due /\ rpc = "idle" /\ ~ReclaimRunning /\ NoFault
  /\ Fenced => ~CreatorHolds
  /\ NoticeFiled => nfiled = {}
  \* G6: the tick waits while a creator's notice is filed.
  /\ CreatorTold => cfiled = {}
  /\ ~CloneHolds
  /\ IF rel.phase = "recreating"
       THEN /\ rel' = [rel EXCEPT !.plan = @ \cup fresh]
            /\ UNCHANGED rplan /\ rpc' = IF Sweep THEN "delete" ELSE "recreate"
       ELSE IF PlanFirst
              THEN /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.plan = @ \cup fresh,
                                         !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ UNCHANGED rplan /\ rpc' = "delete"
              ELSE /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ rplan' = fresh /\ rpc' = "delete"
  /\ UNCHANGED <<retained, tags, br, gone, deletedByRelease, due, held>> /\ RUnchanged

\* G4: held by a clone, the tick changes nothing, records why on the obligation and tells the release's people
\* (_held_by_clones); a later tick reads again.
RHeld ==
  /\ rel.on /\ due /\ rpc = "idle" /\ CloneHolds /\ ~held /\ NoFault
  /\ held' = TRUE
  /\ UNCHANGED <<retained, tags, br, rel, rpc, rplan, gone, deletedByRelease, due>> /\ RUnchanged

\* What a delete takes: of what is planned, only what G8 takes now. A branch already back stands where the release
\* recreated it, and a ref its owner moved off a residual, or cut again from clean history, stands where nothing is taken.
DelBranches(plan) == plan \cap TakenBranchesOf(br)

\* Lance refuses to delete a branch another branch is cut from (measured on pylance 12.0.0, erasure_release.py docstring):
\* a plan that leaves such a child out fails its delete, and the tick hands its lease back (release_due).
Refused(plan) == "B" \in plan /\ br["B"].on /\ br["C"].on /\ br["C"].fork = 0 /\ "C" \notin plan
RRefused ==
  LET plan == DelBranches(IF PlanFirst THEN rel.plan ELSE rplan) IN
  /\ rpc = "delete" /\ Refused(plan) /\ NoFault
  /\ rpc' = "idle" /\ rplan' = {}
  /\ UNCHANGED <<retained, tags, br, rel, gone, deletedByRelease, due, held>> /\ RUnchanged

\* _delete: every tag, then every branch leaf-first; one already gone is skipped. What a creator is owed is read here,
\* from what the release actually deletes: a ref a door put while this record stood, whose creator was not told of it.
RDelete ==
  LET plan == DelBranches(IF PlanFirst THEN rel.plan ELSE rplan)
      dt == TakenTagsOf(tags)
      taken == {r \in CRefs : IF IsTag(r) THEN r \in dt ELSE r \in plan /\ br[r].on} IN
  /\ rpc = "delete" /\ NoFault
  /\ ~Refused(plan)
  /\ tags' = [t \in Tags |-> IF t \in dt THEN 0 ELSE tags[t]]
  /\ br' = [b \in Branches |-> IF b \in plan THEN [br[b] EXCEPT !.on = FALSE] ELSE br[b]]
  /\ deletedByRelease' = deletedByRelease \cup {b \in plan : br[b].on}
  /\ untold' = (untold \/ \E r \in taken : cput[r] = rel.dl /\ [ref |-> r, dl |-> rel.dl] \notin ctold)
  /\ cput' = [r \in CRefs |-> IF r \in taken THEN "none" ELSE cput[r]]
  /\ rpc' = "mark"
  /\ UNCHANGED <<retained, rel, rplan, gone, held, rmark, due>> /\ RFixed

\* The record moves to "recreating" (with A2 removed, the plan is written only now).
RMark ==
  /\ rpc = "mark" /\ NoFault
  /\ rel' = [rel EXCEPT !.phase = "recreating", !.plan = IF PlanFirst THEN @ ELSE rplan]
  /\ rpc' = "recreate"
  /\ UNCHANGED <<retained, tags, br, rplan, gone, deletedByRelease, due, held>> /\ RUnchanged

\* _recreate: each branch top-down at the recorded head; one cut from B comes back from B; one standing is skipped.
RRecreate ==
  /\ rpc = "recreate" /\ NoFault
  /\ br' = [b \in Branches |->
              IF b \in rel.plan /\ ~br[b].on
                THEN IF b = "C" /\ br["C"].fork = 0 /\ "B" \in rel.plan
                       THEN [on |-> TRUE, fork |-> 0, dirty |-> Holds(rel.at)]
                       ELSE [on |-> TRUE, fork |-> rel.at, dirty |-> Holds(rel.at)]
                ELSE br[b]]
  /\ rpc' = IF Sweep THEN "reclaim" ELSE "finish"
  /\ UNCHANGED <<retained, tags, rel, rplan, gone, deletedByRelease, due, held>> /\ RUnchanged

\* G1: the release reclaims main itself, still under its hold: a version no ref pins and that is not the head goes.
RReclaim ==
  LET old == {v \in retained : v # head /\ ~Pinned(v)} IN
  /\ rpc = "reclaim" /\ NoFault
  /\ retained' = retained \ old /\ gone' = gone \cup old
  /\ rpc' = "finish"
  /\ UNCHANGED <<tags, br, rel, rplan, deletedByRelease, due, held>> /\ RUnchanged

\* _finish, two deletes: with G9 the record and then the mark, inside the hold; without it after the hold has ended
\* (FinishHeld), or the mark first (MarkFirst). The tick may die between the two.
RecordGoes == rel' = NoRel /\ due' = FALSE /\ held' = FALSE /\ UNCHANGED rmark
MarkGoes   == rmark' = FALSE /\ UNCHANGED <<rel, due, held>>
RFinish ==
  /\ rpc = "finish" /\ NoFault
  /\ IF MarkFirst THEN MarkGoes ELSE RecordGoes
  /\ rpc' = "finish2"
  /\ UNCHANGED <<retained, tags, br, rplan, gone, deletedByRelease, cput, untold>> /\ RFixed
RFinish2 ==
  /\ rpc = "finish2" /\ NoFault
  /\ IF MarkFirst THEN RecordGoes ELSE MarkGoes
  /\ rpc' = "idle" /\ rplan' = {}
  /\ UNCHANGED <<retained, tags, br, gone, deletedByRelease, cput, untold>> /\ RFixed

\* The tick dies or fails; its lease lapses (or it hands it back) and a later tick resumes from the record.
RDies ==
  /\ rpc # "idle" /\ Fault
  /\ rpc' = "idle" /\ rplan' = {}
  /\ UNCHANGED <<retained, tags, br, rel, gone, deletedByRelease, due, held>> /\ RUnchanged

---------------------------------------------------------------------------
\* Maintenance's cleanup on a registered table: a version no ref pins and that is not main's head goes (A5).

Cleanup ==
  LET old == {v \in retained : v # head /\ ~Pinned(v)} IN
  /\ live /\ old # {} /\ ~ReleaseRunning /\ ~ReclaimRunning /\ ~(Fenced /\ CreatorHolds) /\ NoFault
  /\ retained' = retained \ old /\ gone' = gone \cup old
  /\ UNCHANGED <<subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl, rel, due, rpc, rplan,
                 dropped, announced, toldDl, recDls, nfiled, tfiled, deletedByRelease, clone, cloned, held, cut, copied, restored, dropDone,
                 badDrop, releasedOn, reopened>> /\ CKeep

---------------------------------------------------------------------------
\* Other callers.

Others0 == UNCHANGED <<vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl, rel, due, rpc, rplan, dropped, announced, toldDl, recDls,
                       nfiled, tfiled, gone, deletedByRelease, held, badDrop, releasedOn, reopened, ob>>
Others == Others0 /\ CKeep

\* The ref doors: create_tag of t2, create_branch of C (cut from main or from B), and, where Moves, update_tag of t1 and
\* create_branch of B once another caller deleted it. One door at a time, under the history fence as a ref write from its
\* read of the release until its ref is written (A3): never while a reclaim runs, nor (G1) while a release does. The door
\* lists the fence's directory: where the release's mark stands it reads the record, and judges whether the release takes
\* the ref by G8, the same predicate the release's delete runs, over the table as it will stand with the ref put
\* (erasure_release.takes). With G6 it files the creator's notice first, landed once the ref stands where it is put and
\* that record stands, and sends it after its hold has ended; without G6 it sends after the ref, filing nothing. A mark
\* whose record is gone is removed. With G11 the door confirms its hold before the ref: one whose hold lapsed while it
\* stalled writes nothing and removes its filing (a refusal, judged at once).
NewBranch(v) == IF v = 0 THEN [on |-> TRUE, fork |-> 0, dirty |-> br["B"].dirty] ELSE [on |-> TRUE, fork |-> v, dirty |-> Holds(v)]
Takes(k, v) ==
  /\ rmark /\ rel.on
  /\ IF IsTag(RefOf(k))
       THEN RefOf(k) \in TakenTagsBy([tags EXCEPT ![RefOf(k)] = v], FALSE)
       ELSE RefOf(k) \in TakenBranchesBy([br EXCEPT ![RefOf(k)] = NewBranch(v)], FALSE)

CStill == UNCHANGED Base

CBegin(k, v) ==
  /\ cpc = "idle" /\ live /\ NoFault
  /\ CASE k = "tag"    -> CopyTags /\ ~copied /\ v \in retained /\ (Fenced => ~ReclaimRunning /\ (Sweep => ~ReleaseRunning))
       [] k = "move"   -> Moves /\ ~moved /\ tags["t1"] # 0 /\ v \in retained /\ (Fenced => ~ReclaimRunning /\ ~ReleaseRunning)
       [] k = "recut"  -> Moves /\ ~recut /\ HasBranch /\ ~br["B"].on /\ v \in retained /\ (Fenced => ~ReclaimRunning /\ ~ReleaseRunning)
       [] OTHER        -> /\ Cuts # "none" /\ ~cut /\ ~br["C"].on /\ (Fenced => ~ReclaimRunning /\ ~ReleaseRunning)
                          /\ IF Cuts = "B" THEN v = 0 /\ br["B"].on ELSE v \in retained
  /\ ckind' = k /\ cver' = v /\ chold' = TRUE
  /\ cdl' = IF Takes(k, v) THEN rel.dl ELSE "none"
  /\ cpc' = IF CreatorTold /\ Takes(k, v) THEN "file" ELSE "land"
  /\ rmark' = (rmark /\ rel.on)
  /\ UNCHANGED <<cfiled, ctold, cput, moved, recut, bgone, untold>> /\ CStill

CFile ==
  /\ cpc = "file" /\ NoFault
  /\ cfiled' = cfiled \cup {[by |-> "door", ref |-> RefOf(ckind), at |-> cver, dl |-> cdl]}
  /\ cpc' = "land"
  /\ UNCHANGED <<ckind, cver, cdl, chold, ctold, cput, moved, recut, bgone, untold, rmark>> /\ CStill

CLand ==
  /\ cpc = "land" /\ NoFault
  /\ IF RefConfirmed /\ ~chold
       THEN /\ cfiled' = {f \in cfiled : f.by # "door"} /\ cpc' = "idle"
            /\ UNCHANGED <<tags, br, cut, copied, cput, moved, recut>>
       ELSE /\ CASE ckind = "tag"  -> tags' = [tags EXCEPT !["t2"] = cver] /\ copied' = TRUE /\ UNCHANGED <<br, cut, moved, recut>>
                 [] ckind = "move" -> tags' = [tags EXCEPT !["t1"] = cver] /\ moved' = TRUE /\ UNCHANGED <<br, cut, copied, recut>>
                 [] OTHER          -> /\ br' = [br EXCEPT ![RefOf(ckind)] = NewBranch(cver)] /\ UNCHANGED <<tags, copied, moved>>
                                      /\ IF ckind = "recut" THEN recut' = TRUE /\ UNCHANGED cut ELSE cut' = TRUE /\ UNCHANGED recut
            \* The ref is the door's, put while the record with this deadline stood (or none did).
            /\ cput' = [cput EXCEPT ![RefOf(ckind)] = IF rel.on THEN rel.dl ELSE "none"]
            /\ cpc' = IF cdl # "none" THEN "send" ELSE "idle"
            /\ UNCHANGED cfiled
  /\ UNCHANGED <<ckind, cver, cdl, chold, ctold, bgone, untold, rmark>>
  /\ UNCHANGED <<retained, subj, head, live, followed, clone, cloned, restored, dropDone, vpc, vclean, vdel, vpins, vres, vsettled,
                 vdropped, vevid, vdl, rel, due, rpc, rplan, dropped, announced, toldDl, recDls, nfiled, tfiled, gone,
                 deletedByRelease, held, badDrop, releasedOn, reopened, ob>>

CSend ==
  /\ cpc = "send" /\ NoFault
  /\ ctold' = ctold \cup {[ref |-> RefOf(ckind), dl |-> cdl]}
  /\ cfiled' = {f \in cfiled : f.by # "door"}
  /\ cpc' = "idle"
  /\ UNCHANGED <<ckind, cver, cdl, chold, cput, moved, recut, bgone, untold, rmark>> /\ CStill

\* The door ends between two steps; what it filed stands, and its hold lapses.
CDies ==
  /\ cpc # "idle" /\ Fault
  /\ cfiled' = {IF f.by = "door" THEN [f EXCEPT !.by = "left"] ELSE f : f \in cfiled}
  /\ cpc' = "idle"
  /\ UNCHANGED <<ckind, cver, cdl, chold, ctold, cput, moved, recut, bgone, untold, rmark>> /\ CStill

\* A door stalls with its hold standing: its renewals stop and the hold lapses while it is still at work. A stall counts
\* as a failure, so a behaviour has as many as MaxFaults allows.
CLapse ==
  /\ StalledDoors /\ cpc \in {"file", "land"} /\ chold /\ Fault
  /\ chold' = FALSE
  /\ UNCHANGED <<cpc, ckind, cver, cdl, cfiled, ctold, cput, moved, recut, bgone, untold, rmark>> /\ CStill

\* The finish sends what a door filed, where its ref stands where the door put it and the record with its deadline stands,
\* and removes the filing: a dead door's, or (StalledDoors) one whose door stalled past the filings' lease. With G10 it
\* judges only while it holds the table's history as a scheduling, which a ref door's hold and a release refuse.
FinishCreator ==
  /\ \E f \in cfiled :
       /\ f.by = "left" \/ StalledDoors
       /\ FinishFenced => ~CreatorHolds /\ ~ReleaseRunning
       /\ NoFault
       /\ ctold' = IF StandsAt(f.ref, f.at) /\ rel.on /\ rel.dl = f.dl THEN ctold \cup {[ref |-> f.ref, dl |-> f.dl]} ELSE ctold
       /\ cfiled' = cfiled \ {f}
  /\ UNCHANGED <<cpc, ckind, cver, cdl, chold, cput, moved, recut, bgone, untold, rmark>> /\ CStill

\* Another caller deletes B (branches/delete takes no hold): Lance refuses while a branch is cut from it or a tag names it
\* (lance_docs/lance/guide/tags_and_branches.md, "A branch cannot be deleted while other branches or tags reference it";
\* no tag here stands on a branch). Before the release takes anything: EveryReleasedBranchComesBack judges the release, not
\* an owner deleting a branch the release gave back.
DeleteB ==
  /\ Moves /\ ~bgone /\ live /\ br["B"].on /\ ~(br["C"].on /\ br["C"].fork = 0) /\ deletedByRelease = {} /\ rpc = "idle" /\ NoFault
  /\ br' = [br EXCEPT !["B"].on = FALSE] /\ bgone' = TRUE /\ cput' = [cput EXCEPT !["B"] = "none"]
  /\ UNCHANGED <<retained, subj, head, tags, live, followed, clone, cloned, cut, copied, restored, dropDone>> /\ Others0
  /\ UNCHANGED <<cpc, ckind, cver, cdl, chold, cfiled, ctold, moved, recut, untold, rmark>>

\* register_table of a shallow clone of a retained version: the catalog tags it under the history fence as a ref write
\* (clone_sources.take_tags), never while a reclaim or a release runs.
RegisterClone(v) ==
  /\ Clones /\ ~cloned /\ v \in retained /\ live /\ NoFault
  /\ Fenced => ~ReclaimRunning /\ ~ReleaseRunning
  /\ tags' = [tags EXCEPT !["tc"] = v] /\ clone' = v /\ cloned' = TRUE
  /\ UNCHANGED <<retained, subj, head, br, live, followed, cut, copied, restored, dropDone>> /\ Others

\* The clone's end (a drop or a deregister of it) deletes its tag (ADR 0198 §1).
EndClone ==
  /\ clone # 0 /\ NoFault
  /\ tags' = [tags EXCEPT !["tc"] = 0] /\ clone' = 0
  /\ UNCHANGED <<retained, subj, head, br, live, followed, cloned, cut, copied, restored, dropDone>> /\ Others

\* restore_table: main's head becomes a new version holding what the restored one holds.
Restore ==
  /\ Restores /\ ~restored /\ ob = "released" /\ live /\ NoFault
  /\ \E v \in retained \ {head} :
       /\ subj' = [subj EXCEPT ![4] = subj[v]]
  /\ retained' = retained \cup {4} /\ head' = 4 /\ restored' = TRUE
  /\ UNCHANGED <<tags, br, live, followed, clone, cloned, cut, copied, dropDone>> /\ Others

\* A recoverable drop detaches the table and keeps its bytes; its undrop registers the same bytes again.
Drop ==
  /\ DropUndrop /\ ~dropDone /\ live /\ NoFault
  /\ live' = FALSE /\ dropDone' = TRUE
  /\ UNCHANGED <<retained, subj, head, tags, br, followed, clone, cloned, cut, copied, restored>> /\ Others
Undrop ==
  /\ ~live /\ NoFault
  /\ live' = TRUE
  /\ UNCHANGED <<retained, subj, head, tags, br, followed, clone, cloned, cut, copied, restored, dropDone>> /\ Others

\* Whoever may erase the derived table records an obligation following this one.
Follow ==
  /\ HasDerived /\ ~followed /\ NoFault
  /\ followed' = TRUE
  /\ UNCHANGED <<retained, subj, head, tags, br, live, clone, cloned, cut, copied, restored, dropDone>> /\ Others

---------------------------------------------------------------------------
VRun(p) == VHeads(p) \/ VTagFile(p) \/ VTagDrop(p) \/ VReclaimRead(p) \/ VReclaimDelete(p) \/ VVerify(p)
           \/ VEmit(p) \/ VNoticeFile(p) \/ VSchedule(p) \/ VNotify(p) \/ VSettle(p)
VSteps(p) == VStart(p) \/ VRun(p)
RSteps == RClaim \/ RHeld \/ RDelete \/ RRefused \/ RMark \/ RRecreate \/ RReclaim \/ RFinish \/ RFinish2

Work ==
  \/ JobMain \/ \E b \in Branches : JobBranch(b)
  \/ \E p \in Verifiers : VSteps(p)
  \/ FinishTags \/ FinishNotice
  \/ GraceEnds \/ RSteps
  \/ Cleanup \/ Undrop \/ Follow \/ EndClone
  \/ CFile \/ CLand \/ CSend \/ FinishCreator

Next ==
  \/ Work
  \/ \E p \in Verifiers : VDies(p)
  \/ RDies \/ CDies \/ CLapse \/ DeleteB
  \/ \E v \in Versions : RegisterClone(v)
  \/ \E k \in {"tag", "branch", "move", "recut"}, v \in Versions \cup {0} : CBegin(k, v)
  \/ Restore \/ Drop

Fair ==
  /\ WF_vars(JobMain) /\ \A b \in Branches : WF_vars(JobBranch(b))
  \* A verification runs to its end once started; another starts only where someone verifies again.
  /\ \A p \in Verifiers : WF_vars(VRun(p))
  /\ IF Reverify THEN \A p \in Verifiers : WF_vars(VStart(p)) ELSE TRUE
  /\ WF_vars(FinishTags) /\ WF_vars(FinishNotice)
  \* Strong for the tick: it runs on every window, so a release a verification's brief filing holds off is claimed by a
  \* later window (G2).
  /\ WF_vars(GraceEnds) /\ SF_vars(RSteps)
  /\ WF_vars(Cleanup) /\ WF_vars(Undrop) /\ WF_vars(Follow)
  \* A ref door runs to its end once started, and the finish sends what a dead one filed (G6).
  /\ WF_vars(CFile \/ CLand \/ CSend) /\ WF_vars(FinishCreator)
  \* A6: a clone's owner, told it holds a release, ends it.
  /\ IF ClonesEnd THEN WF_vars(EndClone) ELSE TRUE

Spec == Init /\ [][Next]_vars /\ Fair

---------------------------------------------------------------------------
\* What must always hold.

\* A ref never names a version whose files are gone (the history fence, A3).
PinsNameRetainedVersions ==
  /\ \A t \in Tags : tags[t] # 0 => tags[t] \in retained
  /\ \A b \in Branches : br[b].on /\ br[b].fork # 0 => br[b].fork \in retained
  /\ head \in retained

\* A version deleted from storage never comes back, through an undrop or anything else.
NothingDeletedComesBack == retained \cap gone = {}

\* An obligation is released only on evidence: every head clean, every residual held by a ref the release names and by
\* nothing else, every derived table followed (erasure.verify's `settled`).
ReleasedOnEvidence == ob = "released" => releasedOn

\* A released obligation is never written open again (A4).
ReleasedStaysReleased == ~reopened

\* A derived table is never left behind: the obligation is released only once one of its own follows this one.
ADerivedTableIsFollowed == (ob = "released" /\ HasDerived) => followed

\* A tag is dropped only once every head is clean, and only over a version a read answers the subject from: a
\* reproducibility pointer over a version that reads clean stays, and becomes a pin the release takes.
OnlyTagsOverTheSubjectAreDropped == ~badDrop

\* People are told only of a release the catalog recorded, and only with the deadline a recorded release names (G5).
NoticeOnlyForARecordedRelease == toldDl \subseteq recDls /\ \A x \in ctold : x.dl \in recDls

\* Whoever put a ref the release deletes, while its record stood, was told of it, with that record's deadline, before the
\* delete (G6, G7, G8, G10, G11). What is owed is read off the release's own delete (RDelete), never off the door's
\* judgement.
ACreatorIsToldBeforeTheRelease == ~untold

\* No release's record stands without its mark, which is how every ref door finds it (G9).
ARecordHasItsMark == rel.on => rmark

\* The release brings back every branch it deleted, once it has finished.
EveryReleasedBranchComesBack == (~rel.on /\ deletedByRelease # {}) => \A b \in deletedByRelease : br[b].on

\* A registered clone reads a version whose files are there: nothing but the clone's own end lets go of it (G4).
NoCloneIsBroken == clone # 0 => clone \in retained

\* What must eventually hold.

\* An obligation released leaves the subject's bytes in no version, branch or tag once the release and cleanup have run.
ErasedOnceReleased == (ob = "released") ~> Erased
\* Every obligation is released in the end (the job runs, and nothing pins a residual the release does not take).
EveryObligationIsReleased == <>(ob = "released")
\* A recorded release is finished, whatever tick dies.
EveryReleaseFinishes == rel.on ~> ~rel.on
\* Everyone a recorded release names is told of it, with the deadline its record names. (A ref's creator is the safety
\* invariant ACreatorIsToldBeforeTheRelease, judged at the release's own delete; a filing that would hold the tick for
\* good fails EveryReleaseFinishes.)
EveryoneIsTold == \A d \in Verifiers : (d \in recDls) ~> (d \in toldDl)
\* Every tag a verification deletes is announced.
EveryDroppedTagIsAnnounced == \A t \in Tags : (t \in dropped) ~> (t \in announced)
\* A release a clone holds says so, unless the clone ends or the release finishes first (G4).
AHeldReleaseIsReported == (rel.on /\ due /\ CloneHolds) ~> (held \/ ~CloneHolds \/ ~rel.on)
=============================================================================
