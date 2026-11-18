--------------------------- MODULE CondemnedErasure ---------------------------
(***************************************************************************)
(* Erasure.tla with reclaims off the history fence (ADR 0246, proposed).   *)
(*                                                                         *)
(* Under ADR 0246 a reclaim condemns before it deletes and every ref door  *)
(* pins before it reads (specs/maintenance/Reclaim.tla), so a reclaim and  *)
(* a ref door no longer take the history fence against each other. What    *)
(* Erasure.tla's A3 says of them (a reclaim and a ref write never overlap) *)
(* is what Reclaim.tla proves of condemn-and-pin for every effect on the   *)
(* store: a door either is refused or pins a version the reclaim then      *)
(* keeps. So A3 stays here as the abstraction it is, and only what the     *)
(* fence ALSO did is removed: the release's RELEASE excluding reclaims.    *)
(* SCHEDULE, RELEASE against ref doors, REFS and BRANCH stay as they are    *)
(* (G1, G7, G10, G11 and branch nesting).                                  *)
(*                                                                         *)
(* Replaced by the configs (definition overrides, `X <- XC`):              *)
(*   VReclaimRead  the verification's reclaim no longer waits for a        *)
(*                 release that runs                                       *)
(*   Cleanup       maintenance's cleanup neither                          *)
(*   RClaim        the release tick no longer waits for a reclaim         *)
(*   Pinned        (ReleasePins) the version where the release makes its  *)
(*                 branches again is pinned from its claim until it        *)
(*                 finishes: a rask-pin tag the release writes and reads   *)
(*                 the record after, as any ref door does                  *)
(* CondemnedErasureUnpinned.cfg removes ReleasePins and must fail.          *)
(***************************************************************************)
EXTENDS Erasure

CONSTANT ReleasePins

\* The release's pin on where its branches come back, standing from its claim to its finish.
PinnedC(v) ==
  \/ (\E t \in Tags : tags[t] = v) \/ (\E b \in Branches : br[b].on /\ br[b].fork = v)
  \/ ReleasePins /\ rel.on /\ rel.phase \in {"deleting", "recreating"} /\ v = rel.at

VReclaimReadC(p) ==
  /\ vpc[p] = "reclaim" /\ NoFault
  /\ Fenced => ~CreatorHolds
  /\ IF vclean[p] /\ ~Holds(head)
       THEN vdel' = [vdel EXCEPT ![p] = {v \in retained : v # head /\ ~Pinned(v)}] /\ Go(p, "delete")
       ELSE vdel' = [vdel EXCEPT ![p] = {}] /\ Go(p, "verify")
  /\ VUnchanged /\ UNCHANGED <<retained, head, tags, br, ob, vclean, vpins, vres, vsettled, vdropped, vevid, rel, dropped, announced, gone, releasedOn, reopened>>

CleanupC ==
  LET old == {v \in retained : v # head /\ ~Pinned(v)} IN
  /\ live /\ old # {} /\ ~ReclaimRunning /\ ~(Fenced /\ CreatorHolds) /\ NoFault
  /\ retained' = retained \ old /\ gone' = gone \cup old
  /\ UNCHANGED <<subj, head, tags, br, live, ob, followed, vpc, vclean, vdel, vpins, vres, vsettled, vdropped, vevid, vdl, rel, due, rpc, rplan,
                 dropped, announced, toldDl, recDls, nfiled, tfiled, deletedByRelease, clone, cloned, held, cut, copied, restored, dropDone,
                 badDrop, releasedOn, reopened>> /\ CKeep

RClaimC ==
  LET fresh == TakenBranchesOf(br)
      ts == TakenTagsOf(tags) IN
  /\ rel.on /\ due /\ rpc = "idle" /\ NoFault
  /\ Fenced => ~CreatorHolds
  /\ NoticeFiled => nfiled = {}
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
=============================================================================
