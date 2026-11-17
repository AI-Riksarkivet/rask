------------------------------- MODULE Erasure -------------------------------
(***************************************************************************)
(* An erasure obligation on one table, from the obligation the catalog      *)
(* records to the release of what pins its residual and the cleanup after.  *)
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
(*              the tag announcements, _schedule_release (record, then the *)
(*              notices), erasure_obligations.settle                       *)
(*   release    services/erasure_release._release (plan and claim, the     *)
(*              deletes, "recreating", the recreates, _finish)             *)
(*   cleanup    maintenance's cleanup_old_versions: a version no ref pins  *)
(*              and that is not a head goes                                *)
(*   others     a tag, a branch or a restore made by another caller, a     *)
(*              recoverable drop and its undrop, the obligation that       *)
(*              follows this one on a derived table                        *)
(*                                                                         *)
(* Main's versions: 1 holds the subject in rows a read answers ("visible");*)
(* 2 is a writer's plain delete, the subject's rows behind a deletion      *)
(* vector ("hidden"), tagged t1 (the shape of                              *)
(* test_erasure_releases_a_pinning_branch_after_its_notice.py), and 1 is   *)
(* tagged t0, a tag the verification drops; 3 is the job's rewrite; 4 a   *)
(* restore's commit, holding what it restored. Branch B is cut from main   *)
(* 2, so it pins it; C is cut by another caller, from a main version or   *)
(* from B. A branch pins the version it is cut from while                  *)
(* it stands (erasure.py, "THE RECLAIM ORDER IS FORCED BY THE FORMAT").    *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. The release reads the table's branches again when it runs, so a  *)
(*       branch cut from a pinning branch in the grace goes with it        *)
(*       (erasure_release._plan, lines 244-252). ErasureNoReread removes   *)
(*       it: Lance refuses to delete a branch another is cut from, so the  *)
(*       release fails on every tick (EveryReleaseFinishes).               *)
(*   A2. The release records where each branch comes back before it       *)
(*       deletes one (erasure_release._claim, 278-279, before _delete).    *)
(*       ErasureNoPlanFirst removes it; a tick that dies after a delete    *)
(*       loses the branch (EveryReleasedBranchComesBack).                  *)
(*   A3. A reclaim and a write of a ref never overlap on one table: the    *)
(*       history fence (lakehouse/history_fences.py, held by erasure.py    *)
(*       _reclaim 584 and dataplane.create_tag 3978, create_branch 4111).  *)
(*       ErasureNoFence removes it, and a tag names a deleted version      *)
(*       (PinsNameRetainedVersions).                                       *)
(*   A4. An obligation's record changes only under the ETag it was read    *)
(*       by, and a released one is never written open again               *)
(*       (erasure_obligations._settling 298-299, records.mutate_json).     *)
(*       ErasureNoCAS lets a slower verification's open write land over a  *)
(*       release (ReleasedStaysReleased).                                  *)
(*   A5. Cleanup runs (maintenance's hourly audit and pass) on a table     *)
(*       nothing holds: it is fair here. A table left in the trash is not  *)
(*       cleaned, so its undrop is fair too.                               *)
(*                                                                         *)
(* FINDINGS, each a config whose first line reads FINDING:                 *)
(*   ErasureCopyTag, ErasureCutBranch: a tag or a branch made in the grace *)
(*   from a residual is not among what the release deletes.                *)
(*   ErasureRestore: the same for a restore of a residual (a limit the     *)
(*   release's `bound` names in words).                                    *)
(*   ErasureTagEventLost: a verification that ends between its tag drops  *)
(*   and their announcements never announces them.                         *)
(*   ErasureNoticeLost: a verification that ends between the release's     *)
(*   record and its notices leaves the people it costs untold.             *)
(*                                                                         *)
(* NOT MODELLED: the predicate and its digest; index segments (one more    *)
(* kind of residual a head must clear); external bases and MemWAL shards  *)
(* (held surfaces, which keep the obligation open); the retention window  *)
(* (retain_days 0); a registered clone's tag; late derived tables (listed  *)
(* and paged, a ruling: erasure_derived.watch_released); lineage's feed.   *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Verifiers,     \* the verification requests that may run at once
          MaxFaults,     \* how many steps may fail or processes die in one behaviour
          HasBranch,     \* whether branch B (cut from main 2) stands at the start
          HasDerived,    \* whether a table derived from this one is on the edge list
          CopyTags,      \* another caller may tag a retained version in the grace
          Cuts,          \* another caller may cut branch C: "none", "B" (from B), "main" (from a main version)
          Restores,      \* another caller may restore a retained version once the obligation is released
          DropUndrop,    \* a recoverable drop of the table and its undrop
          Reread,        \* A1
          PlanFirst,     \* A2
          Fenced,        \* A3
          CAS            \* A4

Versions == 1..4
Tags     == {"t0", "t1", "t2"}
Branches == {"B", "C"}
NoRel    == [on |-> FALSE, refs |-> {}, phase |-> "pending", tags |-> {}, plan |-> {}, at |-> 0]

VARIABLES retained,   \* main's versions with their files on storage
          subj,       \* what each version holds of the subject: "none", "hidden" (bytes, behind a deletion vector), "visible"
          head,       \* main's head
          tags,       \* each tag's version, 0 when it does not stand
          br,         \* each branch: whether it stands, the main version it is cut from (0: cut from B), whether its head holds the subject
          live,       \* the table is registered (not in the trash)
          ob,         \* the obligation: "open" or "released"
          followed,   \* an obligation on the derived table follows this one
          vpc, vclean, vdel, vpins, vsettled, vdropped, vevid,   \* each verification
          rel,        \* the release record on the control root
          due,        \* its grace period has ended
          rpc, rplan, \* the release tick, and the plan it keeps in memory when it does not record it first (A2 removed)
          dropped, announced,    \* tags verifications deleted, and those announced on the control lane
          notified, recorded,    \* the people were told; a release was ever recorded
          gone,                  \* versions deleted from storage
          deletedByRelease,      \* branches a release deleted
          cut, copied, restored, dropDone,
          badDrop, releasedOn, reopened,  \* history: a tag dropped wrongly;: the evidence a release was made on; a released obligation written open
          faults

vars == <<retained, subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vsettled, vdropped, vevid,
          rel, due, rpc, rplan, dropped, announced, notified, recorded, gone, deletedByRelease, cut, copied, restored,
          dropDone, badDrop, releasedOn, reopened, faults>>

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
  /\ vsettled = [p \in Verifiers |-> FALSE] /\ vdropped = [p \in Verifiers |-> {}] /\ vevid = [p \in Verifiers |-> FALSE]
  /\ rel = NoRel /\ due = FALSE /\ rpc = "idle" /\ rplan = {}
  /\ dropped = {} /\ announced = {} /\ notified = FALSE /\ recorded = FALSE
  /\ gone = {} /\ deletedByRelease = {}
  /\ cut = FALSE /\ copied = FALSE /\ restored = FALSE /\ dropDone = FALSE
  /\ badDrop = FALSE /\ releasedOn = TRUE /\ reopened = FALSE
  /\ faults = 0

Fault   == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults

Holds(v) == subj[v] # "none"
\* The version a branch's head stands on, for its pin: its fork, or B's when cut from B.
Pinned(v) == (\E t \in Tags : tags[t] = v) \/ (\E b \in Branches : br[b].on /\ br[b].fork = v)
HeadsClean == ~Holds(head) /\ \A b \in Branches : br[b].on => ~br[b].dirty
Residual == {v \in retained : Holds(v)}
\* What pins each residual, as `pinned_by` names it.
PinsOf(res) == {<<"tag", t>> : t \in {t \in Tags : tags[t] \in res}} \cup {<<"branch", b>> : b \in {b \in Branches : br[b].on /\ br[b].fork \in res}}
\* settled: every head clean, every residual held only by refs (never by a head), every derived table followed.
Evidence == HeadsClean /\ (\A v \in Residual : v # head /\ Pinned(v)) /\ (HasDerived => followed)
\* The subject's bytes nowhere a read or a ref reaches.
Erased == (\A v \in retained : ~Holds(v)) /\ (\A b \in Branches : br[b].on => ~br[b].dirty)

ReclaimRunning == \E p \in Verifiers : vpc[p] = "delete"
ReleaseRunning == rpc # "idle"

---------------------------------------------------------------------------
\* The bring-your-own job: on every ref whose head holds the subject, delete by row id and compact (one commit here).

JobMain ==
  /\ ob = "open" /\ live /\ Holds(head) /\ 3 \notin retained \cup gone /\ NoFault
  /\ retained' = retained \cup {3} /\ head' = 3
  /\ UNCHANGED <<subj, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, due, rpc, rplan,
                 dropped, announced, notified, recorded, gone, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

JobBranch(b) ==
  /\ ob = "open" /\ live /\ br[b].on /\ br[b].dirty /\ NoFault
  /\ br' = [br EXCEPT ![b].dirty = FALSE]
  /\ UNCHANGED <<retained, subj, head, tags, live, ob, followed, vpc, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, due, rpc,
                 rplan, dropped, announced, notified, recorded, gone, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

---------------------------------------------------------------------------
\* A verification (verify_erasure). The door resolves the table, so it runs only while the table is registered.

VKeep == UNCHANGED <<subj, live, followed, due, rpc, rplan, notified, recorded, deletedByRelease, cut, copied, restored, dropDone>>
VUnchanged == VKeep /\ UNCHANGED badDrop
Go(p, s) == vpc' = [vpc EXCEPT ![p] = s]

VStart(p) ==
  /\ vpc[p] = "idle" /\ ob = "open" /\ live /\ NoFault
  /\ Go(p, "heads")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* Step 1, _judge_heads.
VHeads(p) ==
  /\ vpc[p] = "heads" /\ NoFault
  /\ vclean' = [vclean EXCEPT ![p] = HeadsClean]
  /\ Go(p, "tags")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vdel, vpins, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* Step 2, _drop_subject_tags: only once every head is clean, a tag over a version a read answers the subject from.
\* A tag over a version holding it only behind a deletion vector is kept (it reads clean) and becomes a pin.
VTags(p) ==
  LET drop == IF vclean[p] THEN {t \in Tags : tags[t] # 0 /\ subj[tags[t]] = "visible"} ELSE {} IN
  /\ vpc[p] = "tags" /\ NoFault
  /\ tags' = [t \in Tags |-> IF t \in drop THEN 0 ELSE tags[t]]
  /\ dropped' = dropped \cup drop
  /\ vdropped' = [vdropped EXCEPT ![p] = @ \cup drop]
  /\ badDrop' = (badDrop \/ (drop # {} /\ ~vclean[p]) \/ \E t \in drop : subj[tags[t]] # "visible")
  /\ Go(p, "reclaim")
  /\ VKeep /\ UNCHANGED <<retained, head, br, ob, vclean, vdel, vpins, vsettled, vevid, rel, announced, gone, releasedOn, reopened>>

\* Step 3, _reclaim: under the history fence (A3), deepest first; main only when its head is clean. Lance reads the
\* refs once, then deletes against that reading (history_fences.py, "LANCE READS A TABLE'S REFS ONCE PER RECLAIM").
VReclaimRead(p) ==
  /\ vpc[p] = "reclaim" /\ NoFault
  /\ Fenced => ~ReleaseRunning
  /\ IF vclean[p] /\ ~Holds(head)
       THEN vdel' = [vdel EXCEPT ![p] = {v \in retained : v # head /\ ~Pinned(v)}] /\ Go(p, "delete")
       ELSE vdel' = [vdel EXCEPT ![p] = {}] /\ Go(p, "verify")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vpins, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

VReclaimDelete(p) ==
  /\ vpc[p] = "delete" /\ NoFault
  /\ retained' = retained \ vdel[p]
  /\ gone' = gone \cup vdel[p]
  /\ Go(p, "verify")
  /\ VUnchanged /\ UNCHANGED <<head, tags, br, ob, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, dropped, announced, releasedOn, reopened>>

\* Step 4, _verify on a cold handle: the residuals, what pins them, and whether only pins hold them.
VVerify(p) ==
  /\ vpc[p] = "verify" /\ NoFault
  /\ vpins' = [vpins EXCEPT ![p] = PinsOf(Residual)]
  /\ vsettled' = [vsettled EXCEPT ![p] = vclean[p] /\ Evidence]
  /\ vevid' = [vevid EXCEPT ![p] = Evidence]
  /\ Go(p, "emit")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vdropped, rel, dropped, announced, gone, releasedOn, reopened>>

\* The door announces each tag the verification dropped, AFTER the drops (verify_erasure 232-242).
VEmit(p) ==
  /\ vpc[p] = "emit" /\ NoFault
  /\ announced' = announced \cup vdropped[p]
  /\ vdropped' = [vdropped EXCEPT ![p] = {}]
  /\ Go(p, "schedule")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vsettled, vevid, rel, dropped, gone, releasedOn, reopened>>

\* _schedule_release: the record FIRST, under the obligation's id. One already there is taken while it names every pin
\* (_take_the_earlier_release), and its people are not told again.
VSchedule(p) ==
  /\ vpc[p] = "schedule" /\ NoFault
  /\ IF vsettled[p] /\ vpins[p] # {}
       THEN IF ~rel.on
              THEN /\ rel' = [NoRel EXCEPT !.on = TRUE, !.refs = vpins[p]]
                   /\ UNCHANGED vsettled /\ Go(p, "notify")
              ELSE /\ UNCHANGED rel
                   /\ vsettled' = [vsettled EXCEPT ![p] = vpins[p] \subseteq rel.refs]
                   /\ Go(p, "settle")
       ELSE UNCHANGED <<rel, vsettled>> /\ Go(p, "settle")
  /\ UNCHANGED <<subj, live, followed, rpc, rplan, notified, deletedByRelease, cut, copied, restored, dropDone, badDrop>>
  /\ due' = (due /\ rel.on) /\ recorded' = (recorded \/ rel'.on)
  /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vdropped, vevid, dropped, announced, gone, releasedOn, reopened>>

\* Then the notices, to the requester and the table's owners.
VNotify(p) ==
  /\ vpc[p] = "notify" /\ NoFault
  /\ notified' = TRUE
  /\ Go(p, "settle")
  /\ UNCHANGED <<subj, live, followed, due, rpc, rplan, recorded, deletedByRelease, cut, copied, restored, dropDone, badDrop>>
  /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* erasure_obligations.settle: released when settled; a released one stays released (A4).
VSettle(p) ==
  /\ vpc[p] = "settle" /\ NoFault
  /\ ob' = CASE vsettled[p] -> "released" [] ob = "released" /\ CAS -> "released" [] OTHER -> "open"
  /\ reopened' = (reopened \/ (ob = "released" /\ ob' = "open"))
  /\ releasedOn' = IF vsettled[p] /\ ob = "open" THEN vevid[p] ELSE releasedOn
  /\ Go(p, "idle")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, dropped, announced, gone>>

\* The request ends between two steps: it dies, or a step fails and the door answers 500.
VDies(p) ==
  /\ vpc[p] \notin {"idle"} /\ Fault
  /\ Go(p, "idle")
  /\ vdropped' = [vdropped EXCEPT ![p] = {}]
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vsettled, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

---------------------------------------------------------------------------
\* The release tick (erasure_release._release), once the grace period has ended. It holds the history fence as a
\* branch write from its plan to its last recreate (A3).

RUnchanged == UNCHANGED <<subj, head, live, ob, followed, vpc, vclean, vdel, vpins, vsettled, vdropped, vevid, dropped, announced,
                          notified, recorded, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

GraceEnds ==
  /\ rel.on /\ ~due /\ NoFault
  /\ due' = TRUE
  /\ UNCHANGED <<retained, tags, br, rel, rpc, rplan, gone, deletedByRelease>> /\ RUnchanged

\* The branches the release takes: those it names, and with A1 every branch cut from one since.
Subtree(named) == LET s == {b \in named : br[b].on} IN
                  IF Reread /\ "B" \in s /\ br["C"].on /\ br["C"].fork = 0 THEN s \cup {"C"} ELSE s
NamedBranches == {b \in Branches : <<"branch", b>> \in rel.refs}
NamedTags     == {t \in Tags : <<"tag", t>> \in rel.refs}

\* _plan and _claim: the plan read from the table now, recorded before any delete (A2). A resumed tick keeps what the
\* record holds and adds what is new.
RClaim ==
  LET fresh == Subtree(NamedBranches \cup rel.plan)
      ts == {t \in NamedTags \cup rel.tags : tags[t] # 0} IN
  /\ rel.on /\ due /\ rpc = "idle" /\ ~ReclaimRunning /\ NoFault
  /\ IF rel.phase = "recreating"
       THEN UNCHANGED <<rel, rplan>> /\ rpc' = "recreate"
       ELSE IF PlanFirst
              THEN /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.plan = @ \cup fresh,
                                         !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ UNCHANGED rplan /\ rpc' = "delete"
              ELSE /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ rplan' = fresh /\ rpc' = "delete"
  /\ UNCHANGED <<retained, tags, br, gone, deletedByRelease, due>> /\ RUnchanged

\* Lance refuses to delete a branch another branch is cut from (measured on pylance 12.0.0, erasure_release.py docstring):
\* a plan that leaves such a child out fails its delete, and the tick hands its lease back (release_due 204-206).
Refused(plan) == "B" \in plan /\ br["B"].on /\ br["C"].on /\ br["C"].fork = 0 /\ "C" \notin plan
RRefused ==
  LET plan == IF PlanFirst THEN rel.plan ELSE rplan IN
  /\ rpc = "delete" /\ Refused(plan) /\ NoFault
  /\ rpc' = "idle" /\ rplan' = {}
  /\ UNCHANGED <<retained, tags, br, rel, gone, deletedByRelease, due>> /\ RUnchanged

\* _delete: every tag, then every branch leaf-first; one already gone is skipped.
RDelete ==
  LET plan == IF PlanFirst THEN rel.plan ELSE rplan IN
  /\ rpc = "delete" /\ NoFault
  /\ ~Refused(plan)
  /\ tags' = [t \in Tags |-> IF t \in rel.tags THEN 0 ELSE tags[t]]
  /\ br' = [b \in Branches |-> IF b \in plan THEN [br[b] EXCEPT !.on = FALSE] ELSE br[b]]
  /\ deletedByRelease' = deletedByRelease \cup {b \in plan : br[b].on}
  /\ rpc' = "mark"
  /\ UNCHANGED <<retained, rel, rplan, gone>> /\ UNCHANGED due /\ RUnchanged

\* The record moves to "recreating" (with A2 removed, the plan is written only now).
RMark ==
  /\ rpc = "mark" /\ NoFault
  /\ rel' = [rel EXCEPT !.phase = "recreating", !.plan = IF PlanFirst THEN @ ELSE rplan]
  /\ rpc' = "recreate"
  /\ UNCHANGED <<retained, tags, br, rplan, gone, deletedByRelease, due>> /\ RUnchanged

\* _recreate: each branch top-down at the recorded head; one cut from B comes back from B.
RRecreate ==
  /\ rpc = "recreate" /\ NoFault
  /\ br' = [b \in Branches |->
              IF b \in rel.plan /\ ~br[b].on
                THEN IF b = "C" /\ br["C"].fork = 0 /\ "B" \in rel.plan
                       THEN [on |-> TRUE, fork |-> 0, dirty |-> Holds(rel.at)]
                       ELSE [on |-> TRUE, fork |-> rel.at, dirty |-> Holds(rel.at)]
                ELSE br[b]]
  /\ rpc' = "finish"
  /\ UNCHANGED <<retained, tags, rel, rplan, gone, deletedByRelease, due>> /\ RUnchanged

\* _finish deletes the record.
RFinish ==
  /\ rpc = "finish" /\ NoFault
  /\ rel' = NoRel /\ due' = FALSE /\ rpc' = "idle" /\ rplan' = {}
  /\ UNCHANGED <<retained, tags, br, gone, deletedByRelease>> /\ RUnchanged

\* The tick dies or fails; its lease lapses (or it hands it back) and a later tick resumes from the record.
RDies ==
  /\ rpc # "idle" /\ Fault
  /\ rpc' = "idle" /\ rplan' = {}
  /\ UNCHANGED <<retained, tags, br, rel, gone, deletedByRelease, due>> /\ RUnchanged

---------------------------------------------------------------------------
\* Maintenance's cleanup on a registered table: a version no ref pins and that is not main's head goes (A5).

Cleanup ==
  LET old == {v \in retained : v # head /\ ~Pinned(v)} IN
  /\ live /\ old # {} /\ ~ReleaseRunning /\ ~ReclaimRunning /\ NoFault
  /\ retained' = retained \ old /\ gone' = gone \cup old
  /\ UNCHANGED <<subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, due, rpc, rplan,
                 dropped, announced, notified, recorded, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

---------------------------------------------------------------------------
\* Other callers.

Others == UNCHANGED <<vpc, vclean, vdel, vpins, vsettled, vdropped, vevid, rel, due, rpc, rplan, dropped, announced, notified, recorded,
                      gone, deletedByRelease, badDrop, releasedOn, reopened, ob>>

\* create_tag, under the history fence as a ref write (A3): never while a reclaim runs.
CopyTag(v) ==
  /\ CopyTags /\ ~copied /\ v \in retained /\ live /\ NoFault
  /\ Fenced => ~ReclaimRunning
  /\ tags' = [tags EXCEPT !["t2"] = v] /\ copied' = TRUE
  /\ UNCHANGED <<retained, subj, head, br, live, followed, cut, restored, dropDone>> /\ Others

\* create_branch, under the history fence as a branch write: never while a reclaim or a release runs.
CutBranch ==
  /\ Cuts # "none" /\ ~cut /\ ~br["C"].on /\ live /\ NoFault
  /\ Fenced => ~ReclaimRunning /\ ~ReleaseRunning
  /\ IF Cuts = "B"
       THEN br["B"].on /\ br' = [br EXCEPT !["C"] = [on |-> TRUE, fork |-> 0, dirty |-> br["B"].dirty]]
       ELSE \E v \in retained : br' = [br EXCEPT !["C"] = [on |-> TRUE, fork |-> v, dirty |-> Holds(v)]]
  /\ cut' = TRUE
  /\ UNCHANGED <<retained, subj, head, tags, live, followed, copied, restored, dropDone>> /\ Others

\* restore_table: main's head becomes a new version holding what the restored one holds.
Restore ==
  /\ Restores /\ ~restored /\ ob = "released" /\ live /\ NoFault
  /\ \E v \in retained \ {head} :
       /\ subj' = [subj EXCEPT ![4] = subj[v]]
  /\ retained' = retained \cup {4} /\ head' = 4 /\ restored' = TRUE
  /\ UNCHANGED <<tags, br, live, followed, cut, copied, dropDone>> /\ Others

\* A recoverable drop detaches the table and keeps its bytes; its undrop registers the same bytes again.
Drop ==
  /\ DropUndrop /\ ~dropDone /\ live /\ NoFault
  /\ live' = FALSE /\ dropDone' = TRUE
  /\ UNCHANGED <<retained, subj, head, tags, br, followed, cut, copied, restored>> /\ Others
Undrop ==
  /\ ~live /\ NoFault
  /\ live' = TRUE
  /\ UNCHANGED <<retained, subj, head, tags, br, followed, cut, copied, restored, dropDone>> /\ Others

\* Whoever may erase the derived table records an obligation following this one.
Follow ==
  /\ HasDerived /\ ~followed /\ NoFault
  /\ followed' = TRUE
  /\ UNCHANGED <<retained, subj, head, tags, br, live, cut, copied, restored, dropDone>> /\ Others

---------------------------------------------------------------------------
Work ==
  \/ JobMain \/ \E b \in Branches : JobBranch(b)
  \/ \E p \in Verifiers : VStart(p) \/ VHeads(p) \/ VTags(p) \/ VReclaimRead(p) \/ VReclaimDelete(p) \/ VVerify(p)
                          \/ VEmit(p) \/ VSchedule(p) \/ VNotify(p) \/ VSettle(p)
  \/ GraceEnds \/ RClaim \/ RDelete \/ RRefused \/ RMark \/ RRecreate \/ RFinish
  \/ Cleanup \/ Undrop \/ Follow

Next ==
  \/ Work
  \/ \E p \in Verifiers : VDies(p)
  \/ RDies
  \/ \E v \in Versions : CopyTag(v)
  \/ CutBranch \/ Restore \/ Drop

Fair ==
  /\ WF_vars(JobMain) /\ \A b \in Branches : WF_vars(JobBranch(b))
  /\ \A p \in Verifiers : WF_vars(VStart(p) \/ VHeads(p) \/ VTags(p) \/ VReclaimRead(p) \/ VReclaimDelete(p) \/ VVerify(p)
                                  \/ VEmit(p) \/ VSchedule(p) \/ VNotify(p) \/ VSettle(p))
  /\ WF_vars(GraceEnds) /\ WF_vars(RClaim \/ RDelete \/ RRefused \/ RMark \/ RRecreate \/ RFinish)
  /\ WF_vars(Cleanup) /\ WF_vars(Undrop) /\ WF_vars(Follow)

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

\* People are told only of a release the catalog recorded.
NoticeOnlyForARecordedRelease == notified => recorded

\* The release brings back every branch it deleted, once it has finished.
EveryReleasedBranchComesBack == (~rel.on /\ deletedByRelease # {}) => \A b \in deletedByRelease : br[b].on

\* What must eventually hold.

\* An obligation released leaves the subject's bytes in no version, branch or tag once the release and cleanup have run.
ErasedOnceReleased == (ob = "released") ~> Erased
\* Every obligation is released in the end (the job runs, and nothing pins a residual the release does not take).
EveryObligationIsReleased == <>(ob = "released")
\* A recorded release is finished, whatever tick dies.
EveryReleaseFinishes == rel.on ~> ~rel.on
\* Everyone a release costs is told before it runs.
EveryoneIsTold == recorded ~> notified
\* Every tag a verification deletes is announced.
EveryDroppedTagIsAnnounced == \A t \in Tags : (t \in dropped) ~> (t \in announced)
=============================================================================
