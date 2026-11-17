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
          ScheduleHeld   \* G7

Versions == 1..4
Tags     == {"t0", "t1", "t2", "tc"}
UserTags == Tags \ {"tc"}
Branches == {"B", "C"}
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
          cpc, ckind, cver, cdl, \* the ref door another caller runs: its step, the ref it makes and where, the deadline it read
          cfiled,                \* the creators' notices filed: by the door, or "left" once it ended (G6)
          ctold,                 \* the creators told, each with the deadline told
          cowed,                 \* the creators owed a notice: a ref the release takes landed while a record with that deadline stood
          untold                 \* history: the release ran while a creator it costs was untold

\* Everything but the ref door's and the faults.
Base == <<retained, subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl,
          rel, due, rpc, rplan, dropped, announced, toldDl, recDls, nfiled, tfiled, gone, deletedByRelease, clone, cloned, held,
          cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>
CVars == <<cpc, ckind, cver, cdl, cfiled, ctold, cowed, untold>>
CKeep == UNCHANGED CVars
vars == <<Base, faults, CVars>>

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
  /\ cpc = "idle" /\ ckind = "tag" /\ cver = 0 /\ cdl = "none"
  /\ cfiled = {} /\ ctold = {} /\ cowed = {} /\ untold = FALSE

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
ReleaseRunning == rpc # "idle"
\* The ref door holds the table's history as a ref write from its read of the release to its last step.
CreatorHolds == cpc # "idle"
\* A ref another caller makes stands: tag t2, or branch C.
Stands(r) == IF r = "t2" THEN tags["t2"] # 0 ELSE br["C"].on
RefOf(k) == IF k = "tag" THEN "t2" ELSE "C"

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
              THEN IF ScheduleHeld /\ CreatorHolds
                     THEN /\ vsettled' = [vsettled EXCEPT ![p] = FALSE] /\ nfiled' = nfiled \ Mine(p) /\ Go(p, "settle")
                          /\ UNCHANGED <<rel, recDls, vdl>>
                     ELSE /\ rel' = [NoRel EXCEPT !.on = TRUE, !.refs = vpins[p], !.res = vres[p], !.dl = p]
                          /\ recDls' = recDls \cup {p}
                          /\ UNCHANGED <<vsettled, nfiled, vdl>> /\ Go(p, "notify")
              ELSE IF NoticeOfTheStanding
                     THEN /\ nfiled' = nfiled \ Mine(p) /\ Go(p, "nfile")
                          /\ UNCHANGED <<rel, vsettled, vdl, recDls>>
                     ELSE /\ UNCHANGED <<rel, recDls>>
                          /\ vsettled' = [vsettled EXCEPT ![p] = vpins[p] \subseteq rel.refs]
                          /\ vdl' = [vdl EXCEPT ![p] = rel.dl]
                          /\ IF NoticeFiled /\ vpins[p] \subseteq rel.refs
                               THEN Go(p, "notify") /\ UNCHANGED nfiled
                               ELSE Go(p, "settle") /\ nfiled' = nfiled \ Mine(p)
       ELSE UNCHANGED <<rel, vsettled, nfiled, vdl, recDls>> /\ Go(p, "settle")
  /\ UNCHANGED <<subj, live, followed, rpc, rplan, toldDl, deletedByRelease, clone, cloned, held, cut, copied, restored, dropDone>>
  /\ due' = (due /\ rel.on)
  /\ NKeep

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

RUnchanged0 == UNCHANGED <<subj, head, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl, dropped, announced,
                          toldDl, recDls, nfiled, tfiled, clone, cloned, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>
                          /\ UNCHANGED <<cpc, ckind, cver, cdl, cfiled, ctold, cowed>>
RUnchanged == RUnchanged0 /\ UNCHANGED untold

GraceEnds ==
  /\ rel.on /\ ~due /\ NoFault
  /\ due' = TRUE
  /\ UNCHANGED <<retained, tags, br, rel, rpc, rplan, gone, deletedByRelease, held>> /\ RUnchanged

\* G1: a branch stands on a residual: cut from one, or from a branch that does.
Tainted(b) == Sweep /\ br[b].on /\ IF br[b].fork = 0 THEN br["B"].on /\ br["B"].fork \in rel.res ELSE br[b].fork \in rel.res
TaintedSet == {b \in Branches : Tainted(b)}
\* G1 takes every tag standing on a residual; with G4 never a registered clone's.
ResidualTags == IF Sweep THEN {t \in Tags : tags[t] \in rel.res /\ (CloneWaits => t # "tc")} ELSE {}
\* G4: a registered clone's tag on a residual holds the release.
CloneHolds == CloneWaits /\ CloneOn(rel.res)

\* The branches the release takes: those it names, those standing on a residual (G1), and with A1 every branch cut from
\* one since.
Subtree(named) == LET s == {b \in named : br[b].on} IN
                  IF Reread /\ "B" \in s /\ br["C"].on /\ br["C"].fork = 0 THEN s \cup {"C"} ELSE s
NamedBranches == {b \in Branches : <<"branch", b>> \in rel.refs}
NamedTags     == {t \in Tags : <<"tag", t>> \in rel.refs}

\* _plan and _claim: the plan read from the table now, recorded before any delete (A2). A resumed tick keeps what the
\* record holds and adds what is new. One resumed once its deletes are done adds only what stands on a residual (G1).
RClaim ==
  LET fresh == Subtree(NamedBranches \cup rel.plan \cup TaintedSet)
      ts == {t \in NamedTags \cup rel.tags : tags[t] # 0} \cup ResidualTags IN
  /\ rel.on /\ due /\ rpc = "idle" /\ ~ReclaimRunning /\ NoFault
  /\ Fenced => ~CreatorHolds
  /\ NoticeFiled => nfiled = {}
  \* G6: the tick waits while a creator's notice is filed.
  /\ CreatorTold => cfiled = {}
  /\ ~CloneHolds
  /\ untold' = (untold \/ \E x \in cowed : x \notin ctold /\ Stands(x.ref))
  /\ IF rel.phase = "recreating"
       THEN /\ rel' = [rel EXCEPT !.plan = @ \cup TaintedSet]
            /\ UNCHANGED rplan /\ rpc' = IF Sweep THEN "delete" ELSE "recreate"
       ELSE IF PlanFirst
              THEN /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.plan = @ \cup fresh,
                                         !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ UNCHANGED rplan /\ rpc' = "delete"
              ELSE /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ rplan' = fresh /\ rpc' = "delete"
  /\ UNCHANGED <<retained, tags, br, gone, deletedByRelease, due, held>> /\ RUnchanged0

\* G4: held by a clone, the tick changes nothing, records why on the obligation and tells the release's people
\* (_held_by_clones); a later tick reads again.
RHeld ==
  /\ rel.on /\ due /\ rpc = "idle" /\ CloneHolds /\ ~held /\ NoFault
  /\ held' = TRUE
  /\ UNCHANGED <<retained, tags, br, rel, rpc, rplan, gone, deletedByRelease, due>> /\ RUnchanged

\* What a delete takes: before the recreates everything planned; after them only what stands on a residual (G1), since a
\* branch already back stands where the release recreated it.
DelTags     == IF rel.phase = "recreating" THEN ResidualTags ELSE rel.tags
DelBranches(plan) == IF rel.phase = "recreating" THEN plan \cap TaintedSet ELSE plan

\* Lance refuses to delete a branch another branch is cut from (measured on pylance 12.0.0, erasure_release.py docstring):
\* a plan that leaves such a child out fails its delete, and the tick hands its lease back (release_due).
Refused(plan) == "B" \in plan /\ br["B"].on /\ br["C"].on /\ br["C"].fork = 0 /\ "C" \notin plan
RRefused ==
  LET plan == DelBranches(IF PlanFirst THEN rel.plan ELSE rplan) IN
  /\ rpc = "delete" /\ Refused(plan) /\ NoFault
  /\ rpc' = "idle" /\ rplan' = {}
  /\ UNCHANGED <<retained, tags, br, rel, gone, deletedByRelease, due, held>> /\ RUnchanged

\* _delete: every tag, then every branch leaf-first; one already gone is skipped.
RDelete ==
  LET plan == DelBranches(IF PlanFirst THEN rel.plan ELSE rplan) IN
  /\ rpc = "delete" /\ NoFault
  /\ ~Refused(plan)
  /\ tags' = [t \in Tags |-> IF t \in DelTags THEN 0 ELSE tags[t]]
  /\ br' = [b \in Branches |-> IF b \in plan THEN [br[b] EXCEPT !.on = FALSE] ELSE br[b]]
  /\ deletedByRelease' = deletedByRelease \cup {b \in plan : br[b].on}
  /\ rpc' = "mark"
  /\ UNCHANGED <<retained, rel, rplan, gone, held>> /\ UNCHANGED due /\ RUnchanged

\* The record moves to "recreating" (with A2 removed, the plan is written only now).
RMark ==
  /\ rpc = "mark" /\ NoFault
  /\ rel' = [rel EXCEPT !.phase = "recreating", !.plan = IF PlanFirst THEN @ ELSE rplan]
  /\ rpc' = "recreate"
  /\ UNCHANGED <<retained, tags, br, rplan, gone, deletedByRelease, due, held>> /\ RUnchanged

\* _recreate: each branch top-down at the recorded head; one cut from B comes back from B.
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

\* _finish deletes the record.
RFinish ==
  /\ rpc = "finish" /\ NoFault
  /\ rel' = NoRel /\ due' = FALSE /\ rpc' = "idle" /\ rplan' = {} /\ held' = FALSE
  /\ UNCHANGED <<retained, tags, br, gone, deletedByRelease>> /\ RUnchanged

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

Others == UNCHANGED <<vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl, rel, due, rpc, rplan, dropped, announced, toldDl, recDls,
                      nfiled, tfiled, gone, deletedByRelease, held, badDrop, releasedOn, reopened, ob>> /\ CKeep

\* create_tag and create_branch (C cut from main or from B), one door at a time, under the history fence as a ref write
\* from its read of the release to its last step (A3): never while a reclaim runs, nor (G1) while a release does. The door
\* reads the release that stands and judges whether it takes the ref; with G6 it files the creator's notice first, landed
\* once the ref and that record stand, and sends it after the ref; without G6 it sends it after the ref, filing nothing.
\* Whether the release that stands takes the ref k made on main version v (0: C cut from B), as its next tick reads the
\* table (erasure_release.takes, the same reading as _plan): a tag or a branch on a residual, or a branch cut from one the
\* release takes.
TaintedAt(v) == Sweep /\ IF v = 0 THEN br["B"].on /\ br["B"].fork \in rel.res ELSE v \in rel.res
Takes(k, v) ==
  /\ rel.on
  /\ IF k = "tag" THEN Sweep /\ v \in rel.res
     ELSE \/ TaintedAt(v)
          \/ v = 0 /\ rel.phase # "recreating" /\ Reread /\ br["B"].on /\ ("B" \in NamedBranches \/ "B" \in rel.plan \/ Tainted("B"))

CStill == UNCHANGED Base

CBegin(k, v) ==
  /\ cpc = "idle" /\ live /\ NoFault
  /\ IF k = "tag"
       THEN CopyTags /\ ~copied /\ v \in retained /\ (Fenced => ~ReclaimRunning /\ (Sweep => ~ReleaseRunning))
       ELSE /\ Cuts # "none" /\ ~cut /\ ~br["C"].on /\ (Fenced => ~ReclaimRunning /\ ~ReleaseRunning)
            /\ IF Cuts = "B" THEN v = 0 /\ br["B"].on ELSE v \in retained
  /\ ckind' = k /\ cver' = v
  /\ cdl' = IF Takes(k, v) THEN rel.dl ELSE "none"
  /\ cpc' = IF CreatorTold /\ Takes(k, v) THEN "file" ELSE "land"
  /\ UNCHANGED <<cfiled, ctold, cowed, untold>> /\ CStill

CFile ==
  /\ cpc = "file" /\ NoFault
  /\ cfiled' = cfiled \cup {[by |-> "door", ref |-> RefOf(ckind), dl |-> cdl]}
  /\ cpc' = "land"
  /\ UNCHANGED <<ckind, cver, cdl, ctold, cowed, untold>> /\ CStill

CLand ==
  /\ cpc = "land" /\ NoFault
  /\ IF ckind = "tag"
       THEN tags' = [tags EXCEPT !["t2"] = cver] /\ copied' = TRUE /\ UNCHANGED <<br, cut>>
       ELSE /\ br' = [br EXCEPT !["C"] = IF cver = 0 THEN [on |-> TRUE, fork |-> 0, dirty |-> br["B"].dirty]
                                                    ELSE [on |-> TRUE, fork |-> cver, dirty |-> Holds(cver)]]
            /\ cut' = TRUE /\ UNCHANGED <<tags, copied>>
  /\ cowed' = IF Takes(ckind, cver) THEN cowed \cup {[ref |-> RefOf(ckind), dl |-> rel.dl]} ELSE cowed
  /\ cpc' = IF cdl # "none" THEN "send" ELSE "idle"
  /\ UNCHANGED <<ckind, cver, cdl, cfiled, ctold, untold>>
  /\ UNCHANGED <<retained, subj, head, live, followed, clone, cloned, restored, dropDone, vpc, vclean, vdel, vpins, vres, vsettled,
                 vdropped, vevid, vdl, rel, due, rpc, rplan, dropped, announced, toldDl, recDls, nfiled, tfiled, gone,
                 deletedByRelease, held, badDrop, releasedOn, reopened, ob>>

CSend ==
  /\ cpc = "send" /\ NoFault
  /\ ctold' = ctold \cup {[ref |-> RefOf(ckind), dl |-> cdl]}
  /\ cfiled' = {f \in cfiled : f.by # "door"}
  /\ cpc' = "idle"
  /\ UNCHANGED <<ckind, cver, cdl, cowed, untold>> /\ CStill

\* The door ends between two steps; what it filed stands, and its hold lapses.
CDies ==
  /\ cpc # "idle" /\ Fault
  /\ cfiled' = {IF f.by = "door" THEN [f EXCEPT !.by = "left"] ELSE f : f \in cfiled}
  /\ cpc' = "idle"
  /\ UNCHANGED <<ckind, cver, cdl, ctold, cowed, untold>> /\ CStill

\* The finish sends what a dead door filed, where its ref and the record with its deadline stand, and removes the filing.
FinishCreator ==
  /\ \E f \in cfiled :
       /\ f.by = "left" /\ NoFault
       /\ ctold' = IF Stands(f.ref) /\ rel.on /\ rel.dl = f.dl THEN ctold \cup {[ref |-> f.ref, dl |-> f.dl]} ELSE ctold
       /\ cfiled' = cfiled \ {f}
  /\ UNCHANGED <<cpc, ckind, cver, cdl, cowed, untold>> /\ CStill

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
RSteps == RClaim \/ RHeld \/ RDelete \/ RRefused \/ RMark \/ RRecreate \/ RReclaim \/ RFinish

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
  \/ RDies \/ CDies
  \/ \E v \in Versions : RegisterClone(v)
  \/ \E k \in {"tag", "branch"}, v \in Versions \cup {0} : CBegin(k, v)
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

\* Whoever made a ref the release takes, once its record stood, was told of it, with that record's deadline, before the
\* release ran (G6, G7).
ACreatorIsToldBeforeTheRelease == ~untold

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
\* Everyone a release costs is told of it, with the deadline its record names, before it runs.
EveryoneIsTold ==
  /\ \A d \in Verifiers : (d \in recDls) ~> (d \in toldDl)
  /\ \A r \in {"t2", "C"}, d \in Verifiers : ([ref |-> r, dl |-> d] \in cowed) ~> ([ref |-> r, dl |-> d] \in ctold \/ ~Stands(r))
\* Every tag a verification deletes is announced.
EveryDroppedTagIsAnnounced == \A t \in Tags : (t \in dropped) ~> (t \in announced)
\* A release a clone holds says so, unless the clone ends or the release finishes first (G4).
AHeldReleaseIsReported == (rel.on /\ due /\ CloneHolds) ~> (held \/ ~CloneHolds \/ ~rel.on)
=============================================================================
