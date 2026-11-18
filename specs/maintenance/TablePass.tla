--------------------------- MODULE TablePass ---------------------------
(***************************************************************************)
(* What a maintenance pass DOES to one table: which versions and files it  *)
(* deletes, beside the doors that write the table's history or a policy   *)
(* that governs it and the writer that commits to it, with the pass dying *)
(* or losing its credential, and a door losing its hold, at any step.      *)
(* ADR 0232's WorkQueue.tla decides who passes a table and when; this      *)
(* module takes the pass's effects on the store (ADR 0243).                *)
(*                                                                         *)
(* THE TABLE. Versions 1 and 2 stand, 2 the head; version 1 is past its    *)
(* retention. Each version names a set of files (`names`): "a" only 1's,   *)
(* "s" both's, "c" only 2's, all under the table's root, and "b" only      *)
(* 1's, in one of the table's own data directories (a plain base,          *)
(* LH-286). A writer's commit stages "w" and lands a version naming the    *)
(* head's files and "w"; a restore lands one naming version 1's again.     *)
(* Whole(v): v's manifest stands and every file it names is stored. A file *)
(* here is any file a version names, a blob sidecar and every file of an  *)
(* index among them, whether or not a listing of the ref would notice it   *)
(* gone (lakehouse.whole_versions, the second line, is not modelled).      *)
(*                                                                         *)
(* THE POLICIES (lakehouse.maintenance_policies.governing_policy). Three   *)
(* levels may govern the table: its own record, its namespace's and its    *)
(* project's, each "none", "keep" (keep one version), "keepall" (keep      *)
(* every version, cleanup on) or "hold" (cleanup_enabled false). The      *)
(* nearest record set decides how much is kept (Nearest); a hold at ANY    *)
(* level keeps the whole history (guard HoldAnyLevel, ADR 0243 §7). The    *)
(* table's record keeps one, its namespace's every version where a door    *)
(* deletes the table's record (`pdelete`), else none.                      *)
(*                                                                         *)
(* THE FENCES. The table's history (lakehouse.history_fences: RECLAIM      *)
(* against REFS), and one policy fence per ancestor level that can govern  *)
(* it (lakehouse.policy_fences: a pass's RECLAIM against a policy door's   *)
(* SET). Holders of one kind share a fence.                                *)
(*                                                                         *)
(* THE PASS, each step one effect in the code's order                      *)
(* (services/maintenance/src/maintenance/services):                        *)
(*   PStart   table_pass.run_table_pass's reads (read_for: the governing   *)
(*            policy) and the maintain vend (credentials.write_options_for,*)
(*            whose door refuses a table a hold at any level keeps:        *)
(*            catalog credentials.require_reclaimable_history)             *)
(*   PTake    expiry._history_held: the table's history for a RECLAIM and  *)
(*            (guard AncestorFence) each ancestor level's policy fence for *)
(*            a RECLAIM, under the vend; one step, since nothing is        *)
(*            deleted before every take stands and a refused take releases *)
(*            the others                                                   *)
(*   PRecheck expiry._dropped_since, and (guard PolicyReread) the policy   *)
(*            read again under the holds (expiry._policy_changed)          *)
(*   PList    lakehouse.reclaim_marks.Reclaim, the one way every reclaim  *)
(*            deletes (a pass, the catalog's maintenance/run and           *)
(*            version/delete, an erasure's cleanups): (guard PolicyIntent) *)
(*            a policy door's intent beside a hold stops it before its     *)
(*            batch; each standing mark whose writer holds nothing (guard  *)
(*            MarkLive) and every file of whose versions stands is removed;*)
(*            then the victims, any part of what the policy reclaims, a    *)
(*            dead cleanup's still reclaimable among them, written as this *)
(*            reclaim's mark (guard VictimMark) before any delete; then    *)
(*            cleanup_old_versions restricted to them (its `versions`),    *)
(*            reading its refs, manifests and files once as it starts      *)
(*            (lance cleanup.rs). One step: the pass holds the history, so *)
(*            no ref moves between the dry run and the cleanup's reading,  *)
(*            and the cleanup removes no version its `versions` omits      *)
(*            (lance_docs/lance/guide/read_and_write.md, Cleanup). One     *)
(*            or two batches here (Batches): after a batch's manifests *)
(*            are gone the pass may list again, meeting an intent first;  *)
(*            the code's batches of 16 each repeat this step.             *)
(*   PDelData its deletes of transaction and data files, every one before  *)
(*            any manifest (measured on pylance 13.0.0, ADR 0243 §3), one  *)
(*            at a time in any order                                       *)
(*   PDelMan  its deletes of the removed versions' manifests; once every   *)
(*            one is gone, the marks naming no standing version removed    *)
(*   PBList   base_reclaim.unreferenced: the table's data directories      *)
(*            listed, less what every retained version names, older than   *)
(*            UNVERIFIED_AGE                                               *)
(*   PBDel    base_reclaim.reclaim's deletes                               *)
(*   PRelease the holds released (Fence.held's finally)                    *)
(* Every delete is signed by the vend. Once the vend has expired the store  *)
(* refuses every delete it signs: Lance counts its refused deletes         *)
(* (CleanupStats.failed_deletes) and goes on, base_reclaim raises, and     *)
(* either way the pass deletes nothing more, leaves its mark standing and  *)
(* releases its holds.                                                     *)
(*                                                                         *)
(* THE DOORS (services/catalog/src/catalog), each writing only after its   *)
(* hold is confirmed (guard DoorConfirm: lakehouse.fences.Fence.write_held)*)
(*   tag      dataplane.create_tag (and update_tag, create_branch,         *)
(*            lakehouse.clone_sources' source tag): the history held as    *)
(*            REFS, (guard VictimMark) the version refused when a standing *)
(*            mark names it (dataplane.require_whole), then the ref. The   *)
(*            mark alone: the doors list no file of the version (F7)       *)
(*   restore  api/v1/endpoints/tables._restore_holding_history: (guard     *)
(*            RestoreFence) the history held as REFS, (VictimMark), then   *)
(*            dataplane.restore's commit, which reads the restored         *)
(*            version's manifest as it commits (measured)                  *)
(*   policy   the table's policy set door: (guard PolicyDoorFence) the     *)
(*            history held as REFS while it writes a hold                  *)
(*   pdelete  the table's policy delete door: (guard PolicyDeleteFence)    *)
(*            the same hold while it deletes the table's record, which     *)
(*            hands the table to its namespace's (B1)                      *)
(*   nspolicy, projpolicy  the namespace's and the project's policy set    *)
(*            doors, writing a hold: the level's policy fence held as SET; *)
(*            a refused one is retried and leaves its holder standing as   *)
(*            an intent (guard PolicyIntent, F4)                           *)
(*   rename   a rename into a namespace that holds (dataplane.rename_table)*)
(*            (guard RenameFence, F3): the history held as REFS            *)
(*   writer   a commit through any door or version/create: no fence        *)
(*                                                                         *)
(* FAULTS, counted against MaxFaults: a pass that dies at any step         *)
(* (Crash), a pass's credential expiring (Expire), and a door's hold      *)
(* lapsing under it, its process frozen past HOLD (DoorLapse). A pass's   *)
(* holds lapse (Lapse) only once its credential is dead (A1).              *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. CredBeforeHold and DoorSession: a reclaimer's credential dies    *)
(*       no later than its holds lapse; a pass by its capped vend, a       *)
(*       catalog door or erasure by a session minted per batch before its *)
(*       confirm (R5). TablePassDoorSessionOff.cfg removes the second.     *)
(*       For a pass:                                                       *)
(*       lapse. The vend is asked before the take, the maintain tier is    *)
(*       vended for at most HOLD (catalog credentials.vend_credentials,    *)
(*       lakehouse.history_fences.HOLD, the policy fences' hold the same), *)
(*       and both are judged on the store's clock.                         *)
(*       TablePassCredOutlivesHold.cfg removes it.                         *)
(*   A2. AgeFloor: a file a commit in flight wrote is younger than         *)
(*       UNVERIFIED_AGE (7 days) until its version lands, so neither       *)
(*       Lance's cleanup (its unverified rule) nor base_reclaim deletes    *)
(*       it. A TIMING ARGUMENT, bounded by 7 days on the store's clock for *)
(*       base_reclaim (its `now` is the pod's: a pod 7 days ahead breaks   *)
(*       it) and on the pod's for Lance. TablePassWriterOutlivesAge.cfg.   *)
(*   A3. TagFence: a ref write holds the history as REFS, which RECLAIM    *)
(*       excludes (lakehouse.history_fences._excludes, Fence.tla).         *)
(*       TablePassNoTagFence.cfg.                                          *)
(*   A4. Each fence's take is atomic here; Fence.tla proves the two        *)
(*       listings and the put-if-not-exists that make it so.               *)
(*   A5. A door's write lands within HOLD of the confirm before it: the    *)
(*       confirm renews the hold, and the write is the next store request  *)
(*       (Fence.tla F2). Confirm and write are one step here.              *)
(*                                                                         *)
(* NOT MODELLED: a drop, undrop and purge (TableLifecycle.tla: the purge   *)
(* deletes no registered table's bytes, and a pass reads the trash again   *)
(* under its hold); an erasure release, whose RELEASE excludes every kind  *)
(* (Erasure.tla); the staged manifests a pass removes, which no version   *)
(* names; the committed-file records it forgets (ADR 0184), which govern  *)
(* no byte; the floor raise, a catalog commit like any writer's; a version *)
(* expiring by its writer's clock (ages here change at any time, which     *)
(* over-approximates every clock; version/create stamps the catalog's own  *)
(* commit time, ADR 0243 §10); the catalog's own reclaim doors,            *)
(* maintenance/run and version/delete, which are passes of this shape      *)
(* (the same holds, re-read, marks and per-batch confirm) signed by the    *)
(* catalog's own credential, which never dies: what holds them is a        *)
(* confirm of every hold before each batch's cleanup, which bounds a       *)
(* frozen door to one batch's deletes inside one HOLD (ADR 0243 §9); a     *)
(* version a branch was cut from, whose files an erasure's cleanup takes   *)
(* (its fork points, marked: ADR 0233 §10).                                *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Passes,       \* the passes of the table that may run; two only where the claim lapsed (WorkQueue A1)
          MaxFaults,    \* crashes, credential expiries and door lapses in one behaviour
          Doors,        \* which of "tag", "restore", "policy", "pdelete", "nspolicy", "projpolicy", "writer" act
          Off           \* the guards removed (Guards)

Guards == {"RestoreFence", "PolicyReread", "PolicyDoorFence", "PolicyDeleteFence", "AncestorFence", "HoldAnyLevel",
           "DoorConfirm", "VictimMark", "TagFence", "CredBeforeHold", "AgeFloor", "RenameFence", "PolicyIntent", "MarkLive",
           "DoorSession"}
ASSUME Off \subseteq Guards
On(g) == g \notin Off

Vers == 1..4
Files == {"a", "s", "c", "b", "w"}
Base == {"b"}                \* in the table's own data directory
Old == 1                     \* the version the tag and the restore name

Levels == {"table", "ns", "proj"}
PolicyDoors == {"policy", "pdelete", "nspolicy", "projpolicy", "rename"}
\* The policy doors their callers retry while a reclaim holds what they hold, each leaving an intent (guard PolicyIntent):
\* a namespace's and a project's (F4), and the table's own set and delete (R2), whose intent is a REFS holder of the
\* table's history.
RetriedDoors == {"policy", "pdelete", "nspolicy", "projpolicy"}
\* How many batches one reclaim may make (lakehouse.reclaim_marks.BATCH versions each): a door's intent is met between two.
Batches == 2
\* The fence a policy door holds, and the kind it holds it as.
FenceOf(d) == CASE d \in {"policy", "pdelete", "rename"} -> "hist" [] d = "nspolicy" -> "ns" [] OTHER -> "proj"
KindOf(d) == IF FenceOf(d) = "hist" THEN "refs" ELSE "set"
\* Whether a door's take is there at all: the table's two policy doors each had a guard removed by a config.
Fenced(d) == CASE d = "policy" -> On("PolicyDoorFence") [] d = "pdelete" -> On("PolicyDeleteFence") [] d = "rename" -> On("RenameFence") [] OTHER -> TRUE

VARIABLES manifests, head, names, files, pinned, expired, policy,
          pc, pol, cred, victims, doomed, bcand,
          marks,               \* [who, vs]: each standing mark of the versions a cleanup will remove, and its writer
                               \* while that writer still holds ("gone" once its holds were released or lapsed)
          holders,             \* [who, kind, fence] holds of the history fence and the ancestor policy fences
          tpc, rpc, dpc, wpc,  \* the tag and restore doors, the policy doors (a function) and the writer
          faults,
          answered,            \* the versions whole when a door answered 200 for keeping the whole history
          unheldDelete,        \* a pass deleted while it held no hold
          batched              \* the passes that finished a batch of their current reclaim

vars == <<manifests, head, names, files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, marks,
          holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete, batched>>

Whole(v) == v \in manifests /\ names[v] \subseteq files
Named(S) == UNION {names[v] : v \in S}
Young == IF On("AgeFloor") /\ wpc = "staged" THEN {"w"} ELSE {}
MarkedVs == UNION {m.vs : m \in marks}

\* The policy that governs: how much is kept is the nearest record's; a hold at any level keeps everything (HoldAnyLevel).
Nearest(P) == IF P["table"] # "none" THEN P["table"] ELSE IF P["ns"] # "none" THEN P["ns"] ELSE IF P["proj"] # "none" THEN P["proj"] ELSE "keep"
Eff(P) == IF On("HoldAnyLevel") /\ \E l \in Levels : P[l] = "hold" THEN "hold" ELSE Nearest(P)
KeepsAll(e) == e \in {"hold", "keepall"}

Init ==
  /\ manifests = {1, 2}
  /\ head = 2
  /\ names = [v \in Vers |-> CASE v = 1 -> {"a", "s", "b"} [] v = 2 -> {"s", "c"} [] OTHER -> {}]
  /\ files = {"a", "s", "c", "b"}
  /\ pinned = {}
  /\ expired = {1}
  /\ policy = [l \in Levels |-> CASE l = "table" -> "keep" [] l = "ns" -> (IF "pdelete" \in Doors THEN "keepall" ELSE "none") [] OTHER -> "none"]
  /\ pc = [p \in Passes |-> "idle"]
  /\ pol = [p \in Passes |-> "keep"]
  /\ cred = [p \in Passes |-> "none"]
  /\ victims = [p \in Passes |-> {}]
  /\ doomed = [p \in Passes |-> {}]
  /\ bcand = [p \in Passes |-> {}]
  /\ marks = {}
  /\ holders = {}
  /\ tpc = IF "tag" \in Doors THEN "idle" ELSE "done"
  /\ rpc = IF "restore" \in Doors THEN "idle" ELSE "done"
  /\ dpc = [d \in PolicyDoors |-> IF d \in Doors THEN "idle" ELSE "done"]
  /\ wpc = IF "writer" \in Doors THEN "idle" ELSE "done"
  /\ faults = 0
  /\ answered = {}
  /\ unheldDelete = FALSE
  /\ batched = {}

\* ---------------------------------------------------------------- the fences
Excludes(k1, k2, f) == IF f = "hist" THEN {k1, k2} = {"reclaim", "refs"} ELSE {k1, k2} = {"reclaim", "set"}
Admits(who, kind, f) == \A h \in holders : (h.who # who /\ h.fence = f) => ~Excludes(h.kind, kind, f)
Holds(who) == \E h \in holders : h.who = who
Drop(who) == {h \in holders : h.who # who}

\* ---------------------------------------------------------------- the pass
Active == {"take", "recheck", "list", "data", "man", "blist", "bdel", "release"}
PassFences == IF On("AncestorFence") THEN {"hist", "ns", "proj"} ELSE {"hist"}

PStart(p) ==
  /\ pc[p] = "idle"
  /\ pol' = [pol EXCEPT ![p] = Eff(policy)]
  \* The vend door refuses a table whose policy keeps its whole history.
  /\ IF Eff(policy) = "hold"
       THEN /\ pc' = [pc EXCEPT ![p] = "done"] /\ UNCHANGED cred
       ELSE /\ pc' = [pc EXCEPT ![p] = "take"] /\ cred' = [cred EXCEPT ![p] = "live"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, victims, doomed, bcand, marks, holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete, batched>>

PTake(p) ==
  /\ pc[p] = "take"
  /\ IF \A f \in PassFences : Admits(p, "reclaim", f)
       THEN /\ holders' = holders \cup {[who |-> p, kind |-> "reclaim", fence |-> f] : f \in PassFences}
            /\ pc' = [pc EXCEPT ![p] = "recheck"]
            /\ UNCHANGED cred
       ELSE /\ pc' = [pc EXCEPT ![p] = "idle"]                      \* HISTORY_FENCED or POLICY_FENCED: skipped, passed again
            /\ cred' = [cred EXCEPT ![p] = "none"]
            /\ UNCHANGED holders
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, victims, doomed, bcand, marks, tpc, rpc, dpc, wpc, faults, answered, unheldDelete, batched>>

PRecheck(p) ==
  /\ pc[p] = "recheck"
  /\ IF On("PolicyReread") /\ Eff(policy) # pol[p]
       THEN pc' = [pc EXCEPT ![p] = "release"]                        \* POLICY_CHANGED: nothing deleted, retried
       ELSE pc' = [pc EXCEPT ![p] = "list"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, victims, doomed, bcand, marks, holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete, batched>>

\* The victims: what the policy the pass planned under reclaims, written as this pass's mark before any delete. A version a
\* dead pass's mark names is among them while the policy still reclaims it (it was past its retention, and no door pins a
\* marked version), so the next pass finishes it; one a policy now keeps stays, marked, and no door may name it. Then
\* Lance's cleanup, restricted to them: the files only they name, and the root's files no manifest names that are past
\* the unverified age. Base files are never its.
\* A policy door's intent on a fence the pass holds (guard PolicyIntent): a SET holder a refused door left standing.
IntentOn(p) == \E h \in holders : h.who \in RetriedDoors /\ h.fence \in PassFences /\ Excludes(h.kind, "reclaim", h.fence)

\* A standing mark a reclaim may judge (lakehouse.reclaim_marks): one whose writer holds nothing any more (guard
\* MarkLive), so it deletes nothing more, and every file of every version it names still stands, so nothing was half
\* removed and there is nothing to protect. A dead cleanup's mark over deleted files stays.
Judged(m) == (On("MarkLive") => m.who = "gone") /\ \A v \in m.vs \cap manifests : Whole(v)

\* Each batch begins by asking for an intent (PolicyIntent): one found stops the pass before it marks or deletes anything
\* more (POLICY_FENCED). Otherwise the marks it may judge are removed, and the victims are written as this pass's mark
\* before any delete: SOME of what the policy the pass planned under reclaims, which over-approximates every reclaim (a
\* pass's dry run, the catalog's maintenance/run under its own retention, version/delete's explicit ranges, an erasure's
\* cleanup), a pass's including what a dead cleanup's standing mark names that the policy still reclaims (it was past
\* its retention, and no door pins a marked version), so the next pass finishes it; one a policy now keeps
\* stays, marked, and no door may name it. Then Lance's cleanup, restricted to them: the files only they name, and the
\* root's files no manifest names that are past the unverified age. Base files are never its.
PList(p) ==
  /\ pc[p] = "list"
  /\ IF On("PolicyIntent") /\ IntentOn(p)
       THEN /\ pc' = [pc EXCEPT ![p] = "release"]
            /\ UNCHANGED <<victims, doomed, marks>>
       ELSE \E v \in SUBSET (IF pol[p] = "keep" THEN {x \in manifests : x # head /\ x \notin pinned /\ x \in expired} ELSE {}) :
              LET eligible == IF pol[p] = "keep" THEN {x \in manifests : x # head /\ x \notin pinned /\ x \in expired} ELSE {}
                  kept == manifests \ v
                  verified == Named(v) \ Named(kept)
                  unverified == {f \in files : f \notin Named(manifests) /\ f \notin Young}
                  left == {m \in marks : ~Judged(m)}
              IN /\ \A m \in left : m.who = "gone" => (m.vs \cap eligible) \subseteq v
                 /\ victims' = [victims EXCEPT ![p] = v]
                 /\ doomed' = [doomed EXCEPT ![p] = (verified \cup unverified) \ Base]
                 /\ marks' = IF On("VictimMark") /\ v # {} THEN left \cup {[who |-> p, vs |-> v]} ELSE left
                 /\ pc' = [pc EXCEPT ![p] = "data"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, bcand, holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete, batched>>

Deleted(p) == unheldDelete' = (unheldDelete \/ ~Holds(p))

PDelData(p) ==
  /\ pc[p] = "data"
  /\ IF doomed[p] = {}
       THEN /\ pc' = [pc EXCEPT ![p] = "man"] /\ UNCHANGED <<files, doomed, unheldDelete>>
       ELSE IF cred[p] # "live"
         THEN /\ pc' = [pc EXCEPT ![p] = "release"] /\ UNCHANGED <<files, doomed, unheldDelete>>
         ELSE \E f \in doomed[p] :
                /\ files' = files \ {f}
                /\ doomed' = [doomed EXCEPT ![p] = @ \ {f}]
                /\ Deleted(p)
                /\ UNCHANGED pc
  /\ UNCHANGED <<manifests, head, names, pinned, expired, policy, pol, cred, victims, bcand, marks, holders, tpc, rpc, dpc, wpc, faults, answered, batched>>

\* Once every victim's manifest is gone the cleanup returned with no failed delete: each mark naming no standing version,
\* this pass's and those it finished, is removed.
PDelMan(p) ==
  /\ pc[p] = "man"
  /\ IF victims[p] = {}
       THEN /\ \/ pc' = [pc EXCEPT ![p] = "blist"] /\ UNCHANGED batched
               \/ p \notin batched /\ Batches > 1 /\ pc' = [pc EXCEPT ![p] = "list"] /\ batched' = batched \cup {p}
            /\ marks' = {m \in marks : m.vs \cap manifests # {}}
            /\ UNCHANGED <<manifests, victims, unheldDelete>>
       ELSE IF cred[p] # "live"
         THEN /\ pc' = [pc EXCEPT ![p] = "release"] /\ UNCHANGED <<manifests, victims, unheldDelete, marks>>
         ELSE \E v \in victims[p] :
                /\ manifests' = manifests \ {v}
                /\ victims' = [victims EXCEPT ![p] = @ \ {v}]
                /\ Deleted(p)
                /\ UNCHANGED <<pc, marks>>
  /\ victims[p] # {} => UNCHANGED batched
  /\ UNCHANGED <<head, names, files, pinned, expired, policy, pol, cred, doomed, bcand, holders, tpc, rpc, dpc, wpc, faults, answered>>

\* base_reclaim: what every retained version names is read now, after the cleanup.
PBList(p) ==
  /\ pc[p] = "blist"
  /\ bcand' = [bcand EXCEPT ![p] = {f \in Base \cap files : f \notin Named(manifests) /\ f \notin Young}]
  /\ pc' = [pc EXCEPT ![p] = "bdel"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, victims, doomed, marks, holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete, batched>>

PBDel(p) ==
  /\ pc[p] = "bdel"
  /\ IF bcand[p] = {}
       THEN /\ pc' = [pc EXCEPT ![p] = "release"] /\ UNCHANGED <<files, bcand, unheldDelete>>
       ELSE IF cred[p] # "live"
         THEN /\ pc' = [pc EXCEPT ![p] = "release"] /\ UNCHANGED <<files, bcand, unheldDelete>>
         ELSE \E f \in bcand[p] :
                /\ files' = files \ {f}
                /\ bcand' = [bcand EXCEPT ![p] = @ \ {f}]
                /\ Deleted(p)
                /\ UNCHANGED pc
  /\ UNCHANGED <<manifests, head, names, pinned, expired, policy, pol, cred, victims, doomed, marks, holders, tpc, rpc, dpc, wpc, faults, answered, batched>>

\* A pass's marks once it holds nothing: written by a writer that deletes nothing more.
Orphaned(p) == {IF m.who = p THEN [m EXCEPT !.who = "gone"] ELSE m : m \in marks}

PRelease(p) ==
  /\ pc[p] = "release"
  /\ holders' = Drop(p)
  /\ marks' = Orphaned(p)
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ cred' = [cred EXCEPT ![p] = "none"]
  /\ victims' = [victims EXCEPT ![p] = {}]
  /\ doomed' = [doomed EXCEPT ![p] = {}]
  /\ bcand' = [bcand EXCEPT ![p] = {}]
  /\ batched' = batched \ {p}
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, tpc, rpc, dpc, wpc, faults, answered, unheldDelete>>

\* ---------------------------------------------------------------- faults
Crash(p) ==
  /\ pc[p] \in Active /\ faults < MaxFaults
  /\ pc' = [pc EXCEPT ![p] = "dead"]
  /\ faults' = faults + 1
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, victims, doomed, bcand, marks, holders, tpc, rpc, dpc, wpc, answered, unheldDelete, batched>>

Expire(p) ==
  /\ cred[p] = "live" /\ (pc[p] = "dead" \/ faults < MaxFaults)
  /\ cred' = [cred EXCEPT ![p] = "dead"]
  /\ faults' = IF pc[p] = "dead" THEN faults ELSE faults + 1
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pc, pol, victims, doomed, bcand, marks, holders, tpc, rpc, dpc, wpc, answered, unheldDelete, batched>>

\* A pass's holds whose holder stopped renewing (dead, or paused past HOLD) are swept by the next take.
\* Passes stand for every reclaimer (a pass, the catalog's maintenance/run and version/delete, an erasure's cleanups):
\* a pass's credential is the maintain vend, capped at HOLD (CredBeforeHold); a catalog reclaimer's is the session each
\* batch mints before it confirms its holds (guard DoorSession, catalog.services.catalog_reclaims.sessions, ADR 0243
\* R5). Without either, a reclaimer's holds may lapse while its credential still signs.
CredDiesWithHold == On("CredBeforeHold") /\ On("DoorSession")
Lapse(p) ==
  /\ Holds(p) /\ pc[p] # "release"
  /\ IF CredDiesWithHold THEN cred[p] # "live" ELSE faults < MaxFaults
  /\ holders' = Drop(p)
  /\ marks' = Orphaned(p)
  /\ faults' = IF CredDiesWithHold THEN faults ELSE faults + 1
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, tpc, rpc, dpc, wpc, answered, unheldDelete, batched>>

\* A task delivered again once its pass died (WorkQueue): the next pass of the table.
Restart(p) ==
  /\ pc[p] = "dead" /\ ~Holds(p) /\ cred[p] # "live"
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ cred' = [cred EXCEPT ![p] = "none"]
  /\ victims' = [victims EXCEPT ![p] = {}]
  /\ doomed' = [doomed EXCEPT ![p] = {}]
  /\ bcand' = [bcand EXCEPT ![p] = {}]
  /\ batched' = batched \ {p}
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, marks, holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete>>

\* Time: a superseded version passes its retention, on whatever clock.
Age ==
  /\ \E v \in manifests \ (expired \cup {head}) : expired' = expired \cup {v}
  /\ UNCHANGED <<manifests, head, names, files, pinned, policy, pc, pol, cred, victims, doomed, bcand, marks, holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete, batched>>

\* A door's process frozen past its hold: the hold lapses under it, and a pass may be admitted.
DoorLapse(d) ==
  /\ Holds(d) /\ faults < MaxFaults
  /\ holders' = Drop(d)
  /\ faults' = faults + 1
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, marks, tpc, rpc, dpc, wpc, answered, unheldDelete, batched>>

\* ---------------------------------------------------------------- the doors
\* Every commit lands as the version after the head (Lance's commit is put-if-not-exists on the next slot).
NewVersion == head + 1

DoorVars == <<manifests, head, names, files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, marks, faults, unheldDelete, batched>>

\* A door's take of `fence` as `kind` where its guard holds; one an excluding holder refuses answers 409 and writes nothing.
TakeFor(fenced, who, kind, fence) ==
  IF ~fenced THEN [ok |-> TRUE, h |-> holders]
  ELSE IF Admits(who, kind, fence) THEN [ok |-> TRUE, h |-> holders \cup {[who |-> who, kind |-> kind, fence |-> fence]}]
  ELSE [ok |-> FALSE, h |-> holders]

\* A door that holds a fence writes only once its hold is confirmed (DoorConfirm); one whose hold lapsed answers 409.
Confirmed(fenced, who) == ~fenced \/ ~On("DoorConfirm") \/ Holds(who)

\* The version a ref door names may be pinned or restored: it stands and (VictimMark) no standing mark names it.
Admissible == Old \in manifests /\ (On("VictimMark") => Old \notin MarkedVs)

TTake ==
  /\ tpc = "idle"
  /\ LET t == TakeFor(On("TagFence"), "tag", "refs", "hist") IN (tpc' = (IF t.ok THEN "check" ELSE "done") /\ holders' = t.h)
  /\ UNCHANGED <<rpc, dpc, wpc, answered>>
  /\ UNCHANGED DoorVars
TCheck ==
  /\ tpc = "check"
  /\ tpc' = IF Admissible THEN "write" ELSE "done"
  /\ holders' = IF Admissible THEN holders ELSE Drop("tag")
  /\ UNCHANGED <<rpc, dpc, wpc, answered>>
  /\ UNCHANGED DoorVars
\* Lance's tag create reads the version's manifest.
TWrite ==
  /\ tpc = "write"
  /\ pinned' = IF Old \in manifests /\ Confirmed(On("TagFence"), "tag") THEN pinned \cup {Old} ELSE pinned
  /\ tpc' = "done"
  /\ holders' = Drop("tag")
  /\ UNCHANGED <<manifests, head, names, files, expired, policy, pc, pol, cred, victims, doomed, bcand, marks, faults, unheldDelete, rpc, dpc, wpc, answered, batched>>

RTake ==
  /\ rpc = "idle"
  /\ LET t == TakeFor(On("RestoreFence"), "restore", "refs", "hist") IN (rpc' = (IF t.ok THEN "check" ELSE "done") /\ holders' = t.h)
  /\ UNCHANGED <<tpc, dpc, wpc, answered>>
  /\ UNCHANGED DoorVars
RCheck ==
  /\ rpc = "check"
  /\ rpc' = IF Admissible THEN "write" ELSE "done"
  /\ holders' = IF Admissible THEN holders ELSE Drop("restore")
  /\ UNCHANGED <<tpc, dpc, wpc, answered>>
  /\ UNCHANGED DoorVars
\* Lance reads the restored version's manifest as it commits, so a restore of a version whose manifest is gone fails.
RWrite ==
  /\ rpc = "write"
  /\ LET lands == Old \in manifests /\ Confirmed(On("RestoreFence"), "restore")
     IN /\ names' = IF lands THEN [names EXCEPT ![NewVersion] = names[Old]] ELSE names
        /\ manifests' = IF lands THEN manifests \cup {NewVersion} ELSE manifests
        /\ head' = IF lands THEN NewVersion ELSE head
  /\ rpc' = "done"
  /\ holders' = Drop("restore")
  /\ UNCHANGED <<files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, marks, faults, unheldDelete, tpc, dpc, wpc, answered, batched>>

\* A refused door answers 409. A policy door (RetriedDoors) is retried by its caller, and (guard PolicyIntent) leaves its
\* holder standing as an intent, which every reclaim's take refuses and every reclaim deleting meets before its next
\* batch (lakehouse.fences.HolderFence.taking, policies._fenced, the table's door through its history's REFS); without
\* one, it leaves nothing. A tag, a restore and a rename leave none: their refusal stops no reclaim.
DTake(d) ==
  /\ dpc[d] = "idle"
  /\ LET t == TakeFor(Fenced(d), d, KindOf(d), FenceOf(d))
         intent == [who |-> d, kind |-> KindOf(d), fence |-> FenceOf(d)]
     IN IF t.ok
          THEN dpc' = [dpc EXCEPT ![d] = "check"] /\ holders' = t.h
          ELSE IF d \in RetriedDoors
            THEN dpc' = dpc /\ holders' = (IF On("PolicyIntent") THEN holders \cup {intent} ELSE holders)
            ELSE dpc' = [dpc EXCEPT ![d] = "done"] /\ holders' = holders
  /\ UNCHANGED <<tpc, rpc, wpc, answered>>
  /\ UNCHANGED DoorVars

\* What each door writes: the table's hold, the table's record deleted, a namespace's or a project's hold, and a rename
\* moving the table into a namespace that holds (whose hold then binds it: guard RenameFence).
Written(d) ==
  CASE d = "policy" -> [policy EXCEPT !["table"] = "hold"]
    [] d = "pdelete" -> [policy EXCEPT !["table"] = "none"]
    [] d \in {"nspolicy", "rename"} -> [policy EXCEPT !["ns"] = "hold"]
    [] OTHER -> [policy EXCEPT !["proj"] = "hold"]

\* The door answers 200 once its record stands. It answered for keeping the whole history where it wrote a hold, or where
\* what governs now keeps every version.
DWrite(d) ==
  /\ dpc[d] = "check"
  /\ IF Confirmed(Fenced(d), d)
       THEN /\ policy' = Written(d)
            /\ answered' = IF d # "pdelete" \/ KeepsAll(Eff(Written(d))) THEN answered \cup {v \in manifests : Whole(v)} ELSE answered
       ELSE UNCHANGED <<policy, answered>>
  /\ dpc' = [dpc EXCEPT ![d] = "done"]
  /\ holders' = Drop(d)
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, pc, pol, cred, victims, doomed, bcand, marks, faults, unheldDelete, tpc, rpc, wpc, batched>>

\* A writer's commit: its files written first, its version naming the head's files and them.
WStage ==
  /\ wpc = "idle"
  /\ files' = files \cup {"w"}
  /\ wpc' = "staged"
  /\ UNCHANGED <<manifests, head, names, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, marks, holders, tpc, rpc, dpc, faults, answered, unheldDelete, batched>>
WCommit ==
  /\ wpc = "staged"
  /\ names' = [names EXCEPT ![NewVersion] = names[head] \cup {"w"}]
  /\ manifests' = manifests \cup {NewVersion}
  /\ head' = NewVersion
  /\ wpc' = "done"
  /\ UNCHANGED <<files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, marks, holders, tpc, rpc, dpc, faults, answered, unheldDelete, batched>>

PassStep(p) == PStart(p) \/ PTake(p) \/ PRecheck(p) \/ PList(p) \/ PDelData(p) \/ PDelMan(p) \/ PBList(p) \/ PBDel(p) \/ PRelease(p)
DoorNames == {"tag", "restore"} \cup PolicyDoors

Next ==
  \/ \E p \in Passes : PassStep(p) \/ Crash(p) \/ Expire(p) \/ Lapse(p) \/ Restart(p)
  \/ Age
  \/ \E d \in DoorNames : DoorLapse(d)
  \/ TTake \/ TCheck \/ TWrite
  \/ RTake \/ RCheck \/ RWrite
  \/ \E d \in PolicyDoors : DTake(d) \/ DWrite(d)
  \/ WStage \/ WCommit

Fairness ==
  /\ \A p \in Passes : WF_vars(PassStep(p)) /\ WF_vars(Restart(p)) /\ WF_vars(Lapse(p) /\ pc[p] = "dead")
                       /\ WF_vars(Expire(p) /\ pc[p] = "dead")
  /\ WF_vars(TTake \/ TCheck \/ TWrite) /\ WF_vars(RTake \/ RCheck \/ RWrite) /\ WF_vars(WStage \/ WCommit)
  /\ \A d \in PolicyDoors : WF_vars(DTake(d) \/ DWrite(d))

Spec == Init /\ [][Next]_vars /\ Fairness

PassSymmetry == Permutations(Passes)

\* ---------------------------------------------------------------- properties
\* Nothing live is deleted: the head and every pinned version stay whole.
RefsWhole == Whole(head) /\ \A v \in pinned : Whole(v)
\* A commit in flight keeps the files it wrote until it lands.
InFlightKept == wpc = "staged" => "w" \in files
\* A door that answered 200 for keeping the whole history (a hold at any level, or a record whose removal hands the table
\* to one keeping every version) keeps every version that was whole then.
RetentionKept == \A v \in answered : Whole(v)
\* A version that stands over deleted files is named by a standing mark, which every ref door reads.
HalfOnlyVictims == \A v \in manifests : ~Whole(v) => v \in MarkedVs
\* Every delete is the pass's while it holds the table's history.
DeletesHeld == ~unheldDelete

\* Once faults stop, the next pass finishes what a dead one began: no version stands over deleted files.
EventuallyWhole == <>[](\A v \in manifests : Whole(v))
\* A policy door retried while reclaims keep coming beneath it lands (guard PolicyIntent), a level's and the table's
\* own alike (R2): each pass in flight stops at its next batch, and none is admitted beside the intent. The pass here holds on every delivery, which stands
\* for a backlog of reclaims beneath the level.
PolicyLands == \A d \in Doors \cap RetriedDoors : <>(dpc[d] = "done")
=============================================================================
