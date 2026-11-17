-------------------------------- MODULE Fence --------------------------------
(***************************************************************************)
(* One object's fence on the control root (lakehouse/fences.py): holders   *)
(* of kinds that exclude each other never hold it at once, and holders of  *)
(* kinds that share it never refuse each other.                            *)
(*                                                                         *)
(* Each holder writes an object of its own beneath the fenced object's     *)
(* directory, then lists the directory: it holds the fence when the       *)
(* listing shows no live holder of a kind its own excludes, and otherwise *)
(* deletes its object and answers refused. Holders that share the fence   *)
(* write distinct keys, so they never contend for one write and a take is *)
(* refused only by what Excluded names, never by contention: an add        *)
(* beneath a namespace and a ref write on a table go through whatever      *)
(* their number. The store is the object store: a write is seen by every   *)
(* listing that starts after it (S3's strong read-after-write and list     *)
(* consistency), each write and each listing one step here.                *)
(*                                                                         *)
(*   Look    Fence.take's first listing: a live excluding holder refuses   *)
(*           at once, before anything is written                           *)
(*   Write   its holder object, created under a key of its own            *)
(*   Check   the second listing: an excluding holder seen, it yields      *)
(*   Ready   the holder's work is done and its LAST step comes (the        *)
(*           warehouse delete's record, for one)                           *)
(*   Release the last step, then its own object deleted                    *)
(*   Lapse   a hold whose renewals failed twice runs out while its holder *)
(*           still works                                                   *)
(*                                                                         *)
(* THE GUARDS, each removed by a config that must fail:                   *)
(*   F1. The second listing (Recheck). Without it two excluding holders    *)
(*       that each listed before the other wrote both hold the fence      *)
(*       (FenceNoRecheck, Exclusive).                                      *)
(*   F2. A renewal before the last step (Confirm, Hold.confirm). Without   *)
(*       it a holder whose hold lapsed takes its last step beside a holder *)
(*       admitted since (FenceNoConfirm, NoLastStepBesideAnExcluder).      *)
(*                                                                         *)
(* NOT MODELLED: the store's clock and the length of a hold (a lapse is   *)
(* one step); a holder of one or two writes, which a single hold outlasts *)
(* (it never lapses); the tombstone a taker writes over a lapsed holder's  *)
(* object before deleting it (fences.py), which only removes what has      *)
(* lapsed here.                                                            *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Adds,     \* holders of a kind that shares the fence with its own (an add, a ref write)
          Drops,    \* holders of the other kind, sharing with each other (a drop, a reclaim)
          Solos,    \* holders of a kind that shares with nobody, its own included (a branch create, a release)
          Lapses,   \* a hold may run out while its holder works
          Recheck,  \* F1
          Confirm   \* F2

Holders == Adds \cup Drops \cup Solos
Kind(h) == IF h \in Adds THEN "add" ELSE IF h \in Drops THEN "drop" ELSE "solo"
Excludes(h, g) == h # g /\ (Kind(h) = "solo" \/ Kind(g) = "solo" \/ Kind(h) # Kind(g))

VARIABLES pc,       \* each holder: "idle", "write", "check", "work", "last"
          stored,   \* the holder objects on the store whose hold stands
          lapsed    \* the holders whose hold ran out while they worked

vars == <<pc, stored, lapsed>>

Init == pc = [h \in Holders |-> "idle"] /\ stored = {} /\ lapsed = {}

Excluded(h) == \E g \in stored : Excludes(h, g)

Look(h) ==
  /\ pc[h] = "idle"
  /\ ~Excluded(h)
  /\ pc' = [pc EXCEPT ![h] = "write"]
  /\ UNCHANGED <<stored, lapsed>>

Write(h) ==
  /\ pc[h] = "write"
  /\ stored' = stored \cup {h}
  /\ pc' = [pc EXCEPT ![h] = IF Recheck THEN "check" ELSE "work"]
  /\ UNCHANGED lapsed

Check(h) ==
  /\ pc[h] = "check"
  /\ IF Excluded(h)
       THEN stored' = stored \ {h} /\ pc' = [pc EXCEPT ![h] = "idle"]
       ELSE UNCHANGED stored /\ pc' = [pc EXCEPT ![h] = "work"]
  /\ UNCHANGED lapsed

\* The work done, the last step comes: with F2 only once a renewal shows the hold still stands.
Ready(h) ==
  /\ pc[h] = "work"
  /\ IF Confirm /\ h \in lapsed
       THEN pc' = [pc EXCEPT ![h] = "idle"] /\ lapsed' = lapsed \ {h}
       ELSE pc' = [pc EXCEPT ![h] = "last"] /\ UNCHANGED lapsed
  /\ UNCHANGED stored

Release(h) ==
  /\ pc[h] = "last"
  /\ stored' = stored \ {h} /\ pc' = [pc EXCEPT ![h] = "idle"] /\ lapsed' = lapsed \ {h}

\* Two renewals in a row failed, and the hold ran out: its object no longer counts for any listing.
Lapse(h) ==
  /\ Lapses /\ pc[h] = "work" /\ h \in stored
  /\ stored' = stored \ {h} /\ lapsed' = lapsed \cup {h}
  /\ UNCHANGED pc

Next == \E h \in Holders : Look(h) \/ Write(h) \/ Check(h) \/ Ready(h) \/ Release(h) \/ Lapse(h)

Spec == Init /\ [][Next]_vars

\* Two holders of kinds that exclude each other never both hold the fence, while neither hold has lapsed.
Exclusive == \A h, g \in Holders :
               (pc[h] \in {"work", "last"} /\ pc[g] \in {"work", "last"} /\ h \notin lapsed /\ g \notin lapsed) => ~Excludes(h, g)

\* A last step is never taken beside a holder of an excluding kind whose hold stands.
NoLastStepBesideAnExcluder == \A h, g \in Holders : (pc[h] = "last" /\ pc[g] \in {"work", "last"} /\ g \notin lapsed) => ~Excludes(h, g)
=============================================================================
