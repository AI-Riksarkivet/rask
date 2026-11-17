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
(* their number. Two fences are kept so: a namespace's (an add, a drop)    *)
(* and a table's history (lakehouse/history_fences.py: a ref write shares  *)
(* with ref writes; a reclaim, and a release's scheduling, with their own  *)
(* kind; a branch create with ref writes alone; a release with nobody).    *)
(* The store is the object store: a write is seen by every                 *)
(* listing that starts after it (S3's strong read-after-write and list     *)
(* consistency), each write and each listing one step here.                *)
(*                                                                         *)
(*   Look    Fence.take's first listing: a live excluding holder refuses   *)
(*           at once, before anything is written                           *)
(*   Write   its holder object, created under a key of its own            *)
(*   Check   the second listing: an excluding holder seen, it yields; the  *)
(*           marks it shows are handed to the holder (Hold.marks)          *)
(*   Ready   the holder's work is done and its LAST step comes (the        *)
(*           warehouse delete's record, for one)                           *)
(*   Release the last step, then its own object deleted                    *)
(*   Lapse   a hold whose renewals failed twice runs out while its holder *)
(*           still works                                                   *)
(*                                                                         *)
(* THE LOWER TIER (Planter). A table's history fence is also written by    *)
(* maintenance, under its table's maintain-tier vend, which may put and    *)
(* delete every key `<directory>/reclaim-*` (vending.TableRecords): an IAM *)
(* `*` matches `/`, so that is any key below a `reclaim-` name, with any   *)
(* body, at any time. It may plant a mark's name or another kind's holder *)
(* name in a subdirectory (`reclaim-x/erasure-release.<id>.json`), plant  *)
(* an object under a reclaim holder's own name with any body, and delete   *)
(* a reclaim's holder object, which every other holder sees as that hold's *)
(* lapse. The fence's own writes are the catalog's, under its own          *)
(* credential, and the catalog alone writes a mark.                        *)
(*                                                                         *)
(* THE GUARDS, each removed by a config that must fail:                   *)
(*   F1. The second listing (Recheck). Without it two excluding holders    *)
(*       that each listed before the other wrote both hold the fence      *)
(*       (FenceNoRecheck, Exclusive).                                      *)
(*   F2. A renewal before the last step (Confirm, Hold.confirm). Without   *)
(*       it a holder whose hold lapsed takes its last step beside a holder *)
(*       admitted since (FenceNoConfirm, NoLastStepBesideAnExcluder).      *)
(*   F3. A listing judges only the objects directly in the directory, and  *)
(*       of those only the names the fence defines; a `reclaim-` holder's  *)
(*       name is a reclaim's hold by its key, whatever its body, until it  *)
(*       lapses on the store's clock (DirectOnly; fences.HolderFence       *)
(*       ._listed). Without it a mark the lower tier planted in a          *)
(*       subdirectory is handed to a holder as the catalog's               *)
(*       (FenceSubdirectories, MarksAreTheCatalogs).                       *)
(*                                                                         *)
(* NOT MODELLED: the store's clock and the length of a hold (a lapse is   *)
(* one step); a holder of one or two writes, which a single hold outlasts *)
(* (it never lapses); the tombstone a taker writes over a lapsed holder's  *)
(* object before deleting it (fences.py), which only removes what has      *)
(* lapsed here; the sweep of what the lower tier planted, which removes    *)
(* only what has lapsed (Unplant).                                         *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Adds,     \* holders of a kind that shares the fence with its own (an add, a ref write)
          Drops,    \* holders of the other kind, sharing with each other (a drop; a reclaim, a release's scheduling)
          Branches, \* holders sharing with Adds alone (a branch create; none on a namespace's fence)
          Solos,    \* holders of a kind that shares with nobody, its own included (a release)
          Lapses,   \* a hold may run out while its holder works
          Planter,  \* the lower tier writes what its grant allows (a table's history fence only)
          Recheck,  \* F1
          Confirm,  \* F2
          DirectOnly \* F3

Holders == Adds \cup Drops \cup Branches \cup Solos
Kind(h) == CASE h \in Adds -> "add" [] h \in Drops -> "drop" [] h \in Branches -> "branch" [] OTHER -> "solo"
\* Which kinds exclude which: the namespace fence's add against drop, and history_fences._excludes.
KindsExclude(k, l) == "solo" \in {k, l} \/ {k, l} = {"add", "drop"} \/ {k, l} = {"branch", "drop"} \/ (k = "branch" /\ l = "branch")
Excludes(h, g) == h # g /\ KindsExclude(Kind(h), Kind(g))

\* What the lower tier may plant: a mark's name in a subdirectory, a release's holder name in a subdirectory, and an
\* object under a reclaim holder's own name in the directory (any body).
Plants == {"subMark", "subSolo", "reclaimKey"}

VARIABLES pc,       \* each holder: "idle", "write", "check", "work", "last"
          stored,   \* the holder objects on the store whose hold stands
          lapsed,   \* the holders whose hold ran out while they worked
          planted,  \* what the lower tier planted that stands
          forged    \* history: a holder was handed a mark the catalog never wrote

vars == <<pc, stored, lapsed, planted, forged>>

Init == pc = [h \in Holders |-> "idle"] /\ stored = {} /\ lapsed = {} /\ planted = {} /\ forged = FALSE

\* What a listing judges h's take against: the honest holders, and of what was planted, with F3, only the object under a
\* reclaim holder's name, a reclaim's hold by its key; without F3 also what lies in a subdirectory, by its last segment.
Excluded(h) ==
  \/ \E g \in stored : Excludes(h, g)
  \/ "reclaimKey" \in planted /\ KindsExclude(Kind(h), "drop")
  \/ ~DirectOnly /\ "subSolo" \in planted

Keep == UNCHANGED <<planted, forged>>

Look(h) ==
  /\ pc[h] = "idle"
  /\ ~Excluded(h)
  /\ pc' = [pc EXCEPT ![h] = "write"]
  /\ UNCHANGED <<stored, lapsed>> /\ Keep

Write(h) ==
  /\ pc[h] = "write"
  /\ stored' = stored \cup {h}
  /\ pc' = [pc EXCEPT ![h] = IF Recheck THEN "check" ELSE "work"]
  /\ UNCHANGED lapsed /\ Keep

\* The second listing; a holder admitted is handed the marks it shows.
Check(h) ==
  /\ pc[h] = "check"
  /\ IF Excluded(h)
       THEN stored' = stored \ {h} /\ pc' = [pc EXCEPT ![h] = "idle"] /\ lapsed' = lapsed \ {h} /\ UNCHANGED forged
       ELSE /\ UNCHANGED <<stored, lapsed>> /\ pc' = [pc EXCEPT ![h] = "work"]
            /\ forged' = (forged \/ (~DirectOnly /\ "subMark" \in planted))
  /\ UNCHANGED planted

\* The work done, the last step comes: with F2 only once a renewal shows the hold still stands.
Ready(h) ==
  /\ pc[h] = "work"
  /\ IF Confirm /\ h \in lapsed
       THEN pc' = [pc EXCEPT ![h] = "idle"] /\ lapsed' = lapsed \ {h}
       ELSE pc' = [pc EXCEPT ![h] = "last"] /\ UNCHANGED lapsed
  /\ UNCHANGED stored /\ Keep

Release(h) ==
  /\ pc[h] = "last"
  /\ stored' = stored \ {h} /\ pc' = [pc EXCEPT ![h] = "idle"] /\ lapsed' = lapsed \ {h}
  /\ Keep

\* Two renewals in a row failed, and the hold ran out: its object no longer counts for any listing.
Lapse(h) ==
  /\ Lapses /\ pc[h] = "work" /\ h \in stored
  /\ stored' = stored \ {h} /\ lapsed' = lapsed \cup {h}
  /\ UNCHANGED pc /\ Keep

\* The lower tier puts any object its grant allows, or one it put goes (it deletes it, or it lapses on the store's clock
\* and the next take sweeps it).
Plant(o)   == Planter /\ o \notin planted /\ planted' = planted \cup {o} /\ UNCHANGED <<pc, stored, lapsed, forged>>
Unplant(o) == o \in planted /\ planted' = planted \ {o} /\ UNCHANGED <<pc, stored, lapsed, forged>>
\* It deletes a reclaim's holder object, which every listing then reads as that hold's lapse.
Unhold(h) ==
  /\ Planter /\ h \in Drops /\ h \in stored /\ pc[h] \in {"check", "work"}
  /\ stored' = stored \ {h} /\ lapsed' = lapsed \cup {h}
  /\ UNCHANGED pc /\ Keep

Next ==
  \/ \E h \in Holders : Look(h) \/ Write(h) \/ Check(h) \/ Ready(h) \/ Release(h) \/ Lapse(h) \/ Unhold(h)
  \/ \E o \in Plants : Plant(o) \/ Unplant(o)

Spec == Init /\ [][Next]_vars

\* Two holders of kinds that exclude each other never both hold the fence, while neither hold has lapsed.
Exclusive == \A h, g \in Holders :
               (pc[h] \in {"work", "last"} /\ pc[g] \in {"work", "last"} /\ h \notin lapsed /\ g \notin lapsed) => ~Excludes(h, g)

\* A last step is never taken beside a holder of an excluding kind whose hold stands.
NoLastStepBesideAnExcluder == \A h, g \in Holders : (pc[h] = "last" /\ pc[g] \in {"work", "last"} /\ g \notin lapsed) => ~Excludes(h, g)

\* Every mark a holder is handed is one the catalog wrote (F3).
MarksAreTheCatalogs == ~forged
=============================================================================
