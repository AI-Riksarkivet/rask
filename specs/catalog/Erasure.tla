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
(* Main's versions: 1 holds the subject in rows a read answers ("visible"); *)
(* 2 is a writer's plain delete, the subject's rows behind a deletion      *)
(* vector ("hidden"), tagged t1 (the shape of                              *)
(* test_erasure_releases_a_pinning_branch_after_its_notice.py), and 1 is   *)
(* tagged t0, a tag the verification drops; 3 is the job's rewrite; 4 a    *)
(* restore's commit, holding what it restored. Branch B is cut from main   *)
(* 2, so it pins it; C is cut by another caller, from a main version or    *)
(* from B. A branch pins the version it is cut from while                  *)
(* it stands (erasure.py, "THE RECLAIM ORDER IS FORCED BY THE FORMAT").    *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. The release reads the table's branches again when it runs, so a   *)
(*       branch cut from a pinning branch in the grace goes with it        *)
(*       (erasure_release._plan, lines 244-252). ErasureNoReread removes   *)
(*       it: Lance refuses to delete a branch another is cut from, so the  *)
(*       release fails on every tick (EveryReleaseFinishes).               *)
(*   A2. The release records where each branch comes back before it        *)
(*       deletes one (erasure_release._claim, 278-279, before _delete).    *)
(*       ErasureNoPlanFirst removes it; a tick that dies after a delete    *)
(*       loses the branch (EveryReleasedBranchComesBack).                  *)
(*   A3. A reclaim and a write of a ref never overlap on one table: the    *)
(*       history fence (lakehouse/history_fences.py, held by erasure.py    *)
(*       _reclaim 584 and dataplane.create_tag 3978, create_branch 4111).  *)
(*       ErasureNoFence removes it, and a tag names a deleted version      *)
(*       (PinsNameRetainedVersions).                                       *)
(*   A4. An obligation's record changes only under the ETag it was read    *)
(*       by, and a released one is never written open again                *)
(*       (erasure_obligations._settling 298-299, records.mutate_json).     *)
(*       ErasureNoCAS lets a slower verification's open write land over a  *)
(*       release (ReleasedStaysReleased).                                  *)
(*   A5. Cleanup runs (maintenance's hourly audit and pass) on a table     *)
(*       nothing holds: it is fair here. A table left in the trash is not  *)
(*       cleaned, so its undrop is fair too.                               *)
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
(*       while a filing stands. ErasureNoticeLost removes it.              *)
(*   G3. The verification files each tag's announcement before it drops    *)
(*       the tag, and the finish sends the filing of a verification that   *)
(*       ended (the door's own start, or maintenance's hourly finish).     *)
(*       ErasureTagEventLost removes it.                                   *)
(*   ErasureRestore is a limit, not a guard: a restore of a residual made  *)
(*   once the obligation is released brings the subject back to main's     *)
(*   head (the release's `bound` names it in words).                       *)
(*                                                                         *)
(* NOT MODELLED: the predicate and its digest; index segments (one more    *)
(* kind of residual a head must clear); external bases and MemWAL shards   *)
(* (held surfaces, which keep the obligation open); the retention window   *)
(* (retain_days 0); a registered clone's tag; late derived tables (listed  *)
(* and paged, a ruling: erasure_derived.watch_released); lineage's feed.   *)
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
          Reread,        \* A1
          PlanFirst,     \* A2
          Fenced,        \* A3
          CAS,           \* A4
          Sweep,         \* G1
          NoticeFiled,   \* G2
          TagsFiled      \* G3

Versions == 1..4
Tags     == {"t0", "t1", "t2"}
Branches == {"B", "C"}
NoRel    == [on |-> FALSE, refs |-> {}, res |-> {}, phase |-> "pending", tags |-> {}, plan |-> {}, at |-> 0]

VARIABLES retained,   \* main's versions with their files on storage
          subj,       \* what each version holds of the subject: "none", "hidden" (bytes, behind a deletion vector), "visible"
          head,       \* main's head
          tags,       \* each tag's version, 0 when it does not stand
          br,         \* each branch: whether it stands, the main version it is cut from (0: cut from B), whether its head holds the subject
          live,       \* the table is registered (not in the trash)
          ob,         \* the obligation: "open" or "released"
          followed,   \* an obligation on the derived table follows this one
          vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid,   \* each verification
          rel,        \* the release record on the control root
          due,        \* its grace period has ended
          rpc, rplan, \* the release tick, and the plan it keeps in memory when it does not record it first (A2 removed)
          dropped, announced,    \* tags verifications deleted, and those announced on the control lane
          notified, recorded,    \* the people were told; a release was ever recorded
          nfiled,                \* the standing filings of the release's notices: each running verification's, and "left" by one that ended (G2)
          tfiled,                \* the tags each running verification filed before it drops them, and those "left" by one that ended (G3)
          gone,                  \* versions deleted from storage
          deletedByRelease,      \* branches a release deleted
          cut, copied, restored, dropDone,
          badDrop, releasedOn, reopened,  \* history: a tag dropped wrongly; the evidence a release was made on; a released obligation written open
          faults

vars == <<retained, subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid,
          rel, due, rpc, rplan, dropped, announced, notified, recorded, nfiled, tfiled, gone, deletedByRelease, cut, copied, restored,
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
  /\ vres = [p \in Verifiers |-> {}]
  /\ vsettled = [p \in Verifiers |-> FALSE] /\ vdropped = [p \in Verifiers |-> {}] /\ vevid = [p \in Verifiers |-> FALSE]
  /\ rel = NoRel /\ due = FALSE /\ rpc = "idle" /\ rplan = {}
  /\ dropped = {} /\ announced = {} /\ notified = FALSE /\ recorded = FALSE
  /\ nfiled = {} /\ tfiled = [p \in Verifiers \cup {"left"} |-> {}]
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
  /\ UNCHANGED <<subj, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, due, rpc, rplan,
                 dropped, announced, notified, recorded, nfiled, tfiled, gone, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

JobBranch(b) ==
  /\ ob = "open" /\ live /\ br[b].on /\ br[b].dirty /\ NoFault
  /\ br' = [br EXCEPT ![b].dirty = FALSE]
  /\ UNCHANGED <<retained, subj, head, tags, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, due, rpc,
                 rplan, dropped, announced, notified, recorded, nfiled, tfiled, gone, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

---------------------------------------------------------------------------
\* A verification (verify_erasure). The door resolves the table, so it runs only while the table is registered.

VKeep == UNCHANGED <<subj, live, followed, due, rpc, rplan, notified, recorded, deletedByRelease, cut, copied, restored, dropDone>>
VUnchanged == VKeep /\ UNCHANGED <<badDrop, nfiled, tfiled>>
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

\* Step 2, _drop_subject_tags: only once every head is clean, a tag over a version a read answers the subject from.
\* A tag over a version holding it only behind a deletion vector is kept (it reads clean) and becomes a pin.
ToDrop(p) == IF vclean[p] THEN {t \in Tags : tags[t] # 0 /\ subj[tags[t]] = "visible"} ELSE {}

\* G3: what it drops is filed first (the door's `before_untag`), each tag's announcement landed once the tag is gone.
VTagFile(p) ==
  /\ vpc[p] = "tags" /\ TagsFiled /\ NoFault
  /\ tfiled' = [tfiled EXCEPT ![p] = ToDrop(p)]
  /\ Go(p, "drop")
  /\ VKeep /\ UNCHANGED <<badDrop, nfiled, retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

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
  /\ VKeep /\ UNCHANGED <<nfiled, tfiled, retained, head, br, ob, vclean, vdel, vpins, vres, vsettled, vevid, rel, announced, gone, releasedOn, reopened>>

\* Step 3, _reclaim: under the history fence (A3), deepest first; main only when its head is clean. Lance reads the
\* refs once, then deletes against that reading (history_fences.py, "LANCE READS A TABLE'S REFS ONCE PER RECLAIM").
VReclaimRead(p) ==
  /\ vpc[p] = "reclaim" /\ NoFault
  /\ Fenced => ~ReleaseRunning
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
  /\ VKeep /\ UNCHANGED <<badDrop, nfiled, retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vevid, rel, dropped, gone, releasedOn, reopened>>

Releasing(p) == vsettled[p] /\ vpins[p] # {}

\* G2: the release's notices are filed before its record is written.
VNoticeFile(p) ==
  /\ vpc[p] = "nfile" /\ NoFault
  /\ nfiled' = IF NoticeFiled /\ Releasing(p) THEN nfiled \cup {p} ELSE nfiled
  /\ Go(p, "schedule")
  /\ VKeep /\ UNCHANGED <<badDrop, tfiled, retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* _schedule_release: the record, under the obligation's id. One already there is taken while it names every pin
\* (_take_the_earlier_release); with G2 its people are told again, under the ids the first telling used.
VSchedule(p) ==
  /\ vpc[p] = "schedule" /\ NoFault
  /\ IF Releasing(p)
       THEN IF ~rel.on
              THEN /\ rel' = [NoRel EXCEPT !.on = TRUE, !.refs = vpins[p], !.res = vres[p]]
                   /\ UNCHANGED <<vsettled, nfiled>> /\ Go(p, "notify")
              ELSE /\ UNCHANGED rel
                   /\ vsettled' = [vsettled EXCEPT ![p] = vpins[p] \subseteq rel.refs]
                   /\ IF NoticeFiled /\ vpins[p] \subseteq rel.refs
                        THEN Go(p, "notify") /\ UNCHANGED nfiled
                        ELSE Go(p, "settle") /\ nfiled' = nfiled \ {p}
       ELSE UNCHANGED <<rel, vsettled, nfiled>> /\ Go(p, "settle")
  /\ UNCHANGED <<subj, live, followed, rpc, rplan, notified, deletedByRelease, cut, copied, restored, dropDone, badDrop, tfiled>>
  /\ due' = (due /\ rel.on) /\ recorded' = (recorded \/ rel'.on)
  /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vres, vdropped, vevid, dropped, announced, gone, releasedOn, reopened>>

\* Then the notices, to the requester and the table's owners, and the filing is removed.
VNotify(p) ==
  /\ vpc[p] = "notify" /\ NoFault
  /\ notified' = TRUE
  /\ nfiled' = nfiled \ {p}
  /\ Go(p, "settle")
  /\ UNCHANGED <<subj, live, followed, due, rpc, rplan, recorded, deletedByRelease, cut, copied, restored, dropDone, badDrop, tfiled>>
  /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

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
  /\ nfiled' = IF p \in nfiled THEN (nfiled \ {p}) \cup {"left"} ELSE nfiled
  /\ tfiled' = [tfiled EXCEPT !["left"] = @ \cup tfiled[p], ![p] = {}]
  /\ VKeep /\ UNCHANGED badDrop /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vdel, vpins, vres, vsettled, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

\* The finish of what an ended verification filed (the next verification's start, or maintenance's hourly finish): each
\* announcement whose change landed goes out, and the filing is removed.
FinishTags ==
  /\ tfiled["left"] # {} /\ NoFault
  /\ announced' = announced \cup {t \in tfiled["left"] : tags[t] = 0}
  /\ tfiled' = [tfiled EXCEPT !["left"] = {}]
  /\ UNCHANGED <<retained, subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, due, rpc, rplan,
                 dropped, notified, recorded, nfiled, gone, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

FinishNotice ==
  /\ "left" \in nfiled /\ NoFault
  /\ notified' = (notified \/ rel.on)
  /\ nfiled' = nfiled \ {"left"}
  /\ UNCHANGED <<retained, subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, due, rpc, rplan,
                 dropped, announced, recorded, tfiled, gone, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

---------------------------------------------------------------------------
\* The release tick (erasure_release._release), once the grace period has ended. It holds the history fence from its plan
\* to its last step (A3); with G1 as a release, which no ref write shares.

RUnchanged == UNCHANGED <<subj, head, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, dropped, announced,
                          notified, recorded, nfiled, tfiled, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

GraceEnds ==
  /\ rel.on /\ ~due /\ NoFault
  /\ due' = TRUE
  /\ UNCHANGED <<retained, tags, br, rel, rpc, rplan, gone, deletedByRelease>> /\ RUnchanged

\* G1: a branch stands on a residual: cut from one, or from a branch that does.
Tainted(b) == Sweep /\ br[b].on /\ IF br[b].fork = 0 THEN br["B"].on /\ br["B"].fork \in rel.res ELSE br[b].fork \in rel.res
TaintedSet == {b \in Branches : Tainted(b)}
ResidualTags == IF Sweep THEN {t \in Tags : tags[t] \in rel.res} ELSE {}

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
  /\ NoticeFiled => nfiled = {}
  /\ IF rel.phase = "recreating"
       THEN /\ rel' = [rel EXCEPT !.plan = @ \cup TaintedSet]
            /\ UNCHANGED rplan /\ rpc' = IF Sweep THEN "delete" ELSE "recreate"
       ELSE IF PlanFirst
              THEN /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.plan = @ \cup fresh,
                                         !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ UNCHANGED rplan /\ rpc' = "delete"
              ELSE /\ rel' = [rel EXCEPT !.phase = "deleting", !.tags = @ \cup ts, !.at = IF rel.phase = "pending" THEN head ELSE @]
                   /\ rplan' = fresh /\ rpc' = "delete"
  /\ UNCHANGED <<retained, tags, br, gone, deletedByRelease, due>> /\ RUnchanged

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
  /\ UNCHANGED <<retained, tags, br, rel, gone, deletedByRelease, due>> /\ RUnchanged

\* _delete: every tag, then every branch leaf-first; one already gone is skipped.
RDelete ==
  LET plan == DelBranches(IF PlanFirst THEN rel.plan ELSE rplan) IN
  /\ rpc = "delete" /\ NoFault
  /\ ~Refused(plan)
  /\ tags' = [t \in Tags |-> IF t \in DelTags THEN 0 ELSE tags[t]]
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
  /\ rpc' = IF Sweep THEN "reclaim" ELSE "finish"
  /\ UNCHANGED <<retained, tags, rel, rplan, gone, deletedByRelease, due>> /\ RUnchanged

\* G1: the release reclaims main itself, still under its hold: a version no ref pins and that is not the head goes.
RReclaim ==
  LET old == {v \in retained : v # head /\ ~Pinned(v)} IN
  /\ rpc = "reclaim" /\ NoFault
  /\ retained' = retained \ old /\ gone' = gone \cup old
  /\ rpc' = "finish"
  /\ UNCHANGED <<tags, br, rel, rplan, deletedByRelease, due>> /\ RUnchanged

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
  /\ UNCHANGED <<subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, due, rpc, rplan,
                 dropped, announced, notified, recorded, nfiled, tfiled, deletedByRelease, cut, copied, restored, dropDone, badDrop, releasedOn, reopened>>

---------------------------------------------------------------------------
\* Other callers.

Others == UNCHANGED <<vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, rel, due, rpc, rplan, dropped, announced, notified, recorded,
                      nfiled, tfiled, gone, deletedByRelease, badDrop, releasedOn, reopened, ob>>

\* create_tag, under the history fence as a ref write (A3): never while a reclaim runs, nor (G1) while a release does.
CopyTag(v) ==
  /\ CopyTags /\ ~copied /\ v \in retained /\ live /\ NoFault
  /\ Fenced => ~ReclaimRunning /\ (Sweep => ~ReleaseRunning)
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
VSteps(p) == VStart(p) \/ VHeads(p) \/ VTagFile(p) \/ VTagDrop(p) \/ VReclaimRead(p) \/ VReclaimDelete(p) \/ VVerify(p)
             \/ VEmit(p) \/ VNoticeFile(p) \/ VSchedule(p) \/ VNotify(p) \/ VSettle(p)
RSteps == RClaim \/ RDelete \/ RRefused \/ RMark \/ RRecreate \/ RReclaim \/ RFinish

Work ==
  \/ JobMain \/ \E b \in Branches : JobBranch(b)
  \/ \E p \in Verifiers : VSteps(p)
  \/ FinishTags \/ FinishNotice
  \/ GraceEnds \/ RSteps
  \/ Cleanup \/ Undrop \/ Follow

Next ==
  \/ Work
  \/ \E p \in Verifiers : VDies(p)
  \/ RDies
  \/ \E v \in Versions : CopyTag(v)
  \/ CutBranch \/ Restore \/ Drop

Fair ==
  /\ WF_vars(JobMain) /\ \A b \in Branches : WF_vars(JobBranch(b))
  /\ \A p \in Verifiers : WF_vars(VSteps(p))
  /\ WF_vars(FinishTags) /\ WF_vars(FinishNotice)
  \* Strong for the tick: it runs on every window, so a release a verification's brief filing holds off is claimed by a
  \* later window (G2).
  /\ WF_vars(GraceEnds) /\ SF_vars(RSteps)
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
