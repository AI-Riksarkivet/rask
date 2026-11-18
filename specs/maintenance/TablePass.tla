--------------------------- MODULE TablePass ---------------------------
(***************************************************************************)
(* What a maintenance pass DOES to one table: which versions and files it  *)
(* deletes, beside the doors that write the table's history and the writer *)
(* that commits to it, with the pass dying or losing its credential at any *)
(* step. ADR 0232's WorkQueue.tla decides who passes a table and when;     *)
(* this module takes the pass's effects on the store (ADR 0243).           *)
(*                                                                         *)
(* THE TABLE. Versions 1 and 2 stand, 2 the head; version 1 is past its    *)
(* retention. Each version names a set of files (`names`): "a" only 1's,   *)
(* "s" both's, "c" only 2's, all under the table's root, and "b" only      *)
(* 1's, in one of the table's own data directories (a plain base,          *)
(* LH-286). A writer's commit stages "w" and lands a version naming the    *)
(* head's files and "w"; a restore lands one naming version 1's again.     *)
(* Whole(v): v's manifest stands and every file it names is stored.        *)
(*                                                                         *)
(* THE PASS, each step one effect in the code's order                      *)
(* (services/maintenance/src/maintenance/services):                        *)
(*   PStart   table_pass.run_table_pass's reads (read_for: the governing   *)
(*            policy) and the maintain vend (credentials.write_options_for,*)
(*            whose door refuses a table whose policy keeps its whole      *)
(*            history: catalog credentials.require_reclaimable_history)    *)
(*   PTake    expiry._history_held: the table's history fence for a        *)
(*            RECLAIM (lakehouse.history_fences.TableHistory.held), under  *)
(*            the vend                                                     *)
(*   PRecheck expiry._dropped_since, and (guard PolicyReread) the policy   *)
(*            read again under the hold (expiry._policy_changed)           *)
(*   PList    LanceDataset.cleanup_old_versions reading its refs, its      *)
(*            manifests and the files it will delete, once, as it starts   *)
(*            (lance cleanup.rs; lakehouse.history_fences module note)     *)
(*   PDelData its deletes of transaction and data files, every one before  *)
(*            any manifest (measured on pylance 13.0.0, ADR 0243 §3), one  *)
(*            at a time in any order                                       *)
(*   PDelMan  its deletes of the removed versions' manifests               *)
(*   PBList   base_reclaim.unreferenced: the table's data directories      *)
(*            listed, less what every retained version names, older than   *)
(*            UNVERIFIED_AGE                                               *)
(*   PBDel    base_reclaim.reclaim's deletes                               *)
(*   PRelease the hold released (Fence.held's finally)                     *)
(* Every delete is signed by the vend. Once the vend has expired the store  *)
(* refuses every delete it signs: Lance counts its refused deletes         *)
(* (CleanupStats.failed_deletes) and goes on, base_reclaim raises, and     *)
(* either way the pass deletes nothing more and releases its hold.         *)
(*                                                                         *)
(* THE DOORS (services/catalog/src/catalog):                               *)
(*   tag      dataplane.create_tag (and update_tag, create_branch,         *)
(*            lakehouse.clone_sources' source tag): the history held as    *)
(*            REFS, (guard WholeRef) the version judged whole under it     *)
(*            (dataplane.require_whole, lakehouse.whole_versions), then    *)
(*            the ref                                                      *)
(*   restore  api/v1/endpoints/tables._restore_holding_history: (guard     *)
(*            RestoreFence)                                                *)
(*            the history held as REFS, (WholeRef) the version judged      *)
(*            whole, then dataplane.restore's commit, which reads the      *)
(*            restored version's manifest as it commits (measured)         *)
(*   policy   api/v1/endpoints/policies._set_holding_history, for          *)
(*            set_table_policy: (guard PolicyDoorFence) the history held   *)
(*            as REFS while a policy keeping the whole history is written  *)
(*   writer   a commit through any door or version/create: no fence        *)
(*                                                                         *)
(* FAULTS, counted against MaxFaults: a pass that dies at any step         *)
(* (Crash), and a pass's credential expiring (Expire). A pass's hold       *)
(* lapses (Lapse) only once its credential is dead (A1).                   *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. CredBeforeHold: a pass's credential dies no later than its hold   *)
(*       lapses. The vend is asked before the take, the maintain tier is   *)
(*       vended for at most HOLD (catalog credentials.vend_credentials,    *)
(*       lakehouse.history_fences.HOLD), and both are judged on the        *)
(*       store's clock. TablePassCredOutlivesHold.cfg removes it.          *)
(*   A2. AgeFloor: a file a commit in flight wrote is younger than         *)
(*       UNVERIFIED_AGE (7 days) until its version lands, so neither       *)
(*       Lance's cleanup (its unverified rule) nor base_reclaim deletes    *)
(*       it. A TIMING ARGUMENT, bounded by 7 days on the store's clock for *)
(*       base_reclaim (its `now` is the pod's: a pod 7 days ahead breaks   *)
(*       it) and on the pod's for Lance. TablePassWriterOutlivesAge.cfg.   *)
(*   A3. TagFence: a ref write holds the history as REFS, which RECLAIM    *)
(*       excludes (lakehouse.history_fences._excludes, Fence.tla).         *)
(*       TablePassNoTagFence.cfg.                                          *)
(*   A4. The fence's take is atomic here; Fence.tla proves the two         *)
(*       listings and the put-if-not-exists that make it so.               *)
(*                                                                         *)
(* NOT MODELLED: a drop, undrop and purge (TableLifecycle.tla: the purge   *)
(* deletes no registered table's bytes, and a pass reads the trash again   *)
(* under its hold); an erasure release, whose RELEASE excludes every kind  *)
(* (Erasure.tla); the staged manifests a pass removes, which no version   *)
(* names; the committed-file records it forgets (ADR 0184), which govern  *)
(* no byte; the floor raise, a catalog commit like any writer's; a version *)
(* expiring by its writer's clock (ages here change at any time, which     *)
(* over-approximates every clock); a namespace's or a project's policy,    *)
(* whose door holds no table's history: the pass's read under its hold     *)
(* sees one written before it, and one written while a cleanup already     *)
(* runs is not honoured by that cleanup (ADR 0243, the residual).          *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Passes,       \* the passes of the table that may run; two only where the claim lapsed (WorkQueue A1)
          MaxFaults,    \* crashes and credential expiries in one behaviour
          Doors,        \* which of "tag", "restore", "policy", "writer" act
          Off           \* the guards removed (Guards)

Guards == {"RestoreFence", "PolicyReread", "PolicyDoorFence", "WholeRef", "TagFence", "CredBeforeHold", "AgeFloor"}
ASSUME Off \subseteq Guards
On(g) == g \notin Off

Vers == 1..4
Files == {"a", "s", "c", "b", "w"}
Base == {"b"}                \* in the table's own data directory
Old == 1                     \* the version the tag and the restore name

VARIABLES manifests, head, names, files, pinned, expired, policy,
          pc, pol, cred, victims, doomed, bcand,
          holders,             \* [who, kind] holds of the history fence
          tpc, rpc, dpc, wpc,  \* the tag, restore, policy doors and the writer
          faults,
          answered,            \* the versions whole when a policy keeping them all answered 200
          everVictim,          \* every version a cleanup ever chose
          unheldDelete         \* a pass deleted while it held no hold

vars == <<manifests, head, names, files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand,
          holders, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

Whole(v) == v \in manifests /\ names[v] \subseteq files
Named(S) == UNION {names[v] : v \in S}
Young == IF On("AgeFloor") /\ wpc = "staged" THEN {"w"} ELSE {}

Init ==
  /\ manifests = {1, 2}
  /\ head = 2
  /\ names = [v \in Vers |-> CASE v = 1 -> {"a", "s", "b"} [] v = 2 -> {"s", "c"} [] OTHER -> {}]
  /\ files = {"a", "s", "c", "b"}
  /\ pinned = {}
  /\ expired = {1}
  /\ policy = "keep"
  /\ pc = [p \in Passes |-> "idle"]
  /\ pol = [p \in Passes |-> "keep"]
  /\ cred = [p \in Passes |-> "none"]
  /\ victims = [p \in Passes |-> {}]
  /\ doomed = [p \in Passes |-> {}]
  /\ bcand = [p \in Passes |-> {}]
  /\ holders = {}
  /\ tpc = IF "tag" \in Doors THEN "idle" ELSE "done"
  /\ rpc = IF "restore" \in Doors THEN "idle" ELSE "done"
  /\ dpc = IF "policy" \in Doors THEN "idle" ELSE "done"
  /\ wpc = IF "writer" \in Doors THEN "idle" ELSE "done"
  /\ faults = 0
  /\ answered = {}
  /\ everVictim = {}
  /\ unheldDelete = FALSE

\* ---------------------------------------------------------------- the fence
Excludes(k1, k2) == {k1, k2} = {"reclaim", "refs"}
Admits(who, kind) == \A h \in holders : h.who # who => ~Excludes(h.kind, kind)
Holds(who) == \E h \in holders : h.who = who
Drop(who) == {h \in holders : h.who # who}

\* ---------------------------------------------------------------- the pass
Active == {"take", "recheck", "list", "data", "man", "blist", "bdel", "release"}

PStart(p) ==
  /\ pc[p] = "idle"
  /\ pol' = [pol EXCEPT ![p] = policy]
  \* The vend door refuses a table whose policy keeps its whole history.
  /\ IF policy = "hold"
       THEN /\ pc' = [pc EXCEPT ![p] = "done"] /\ UNCHANGED cred
       ELSE /\ pc' = [pc EXCEPT ![p] = "take"] /\ cred' = [cred EXCEPT ![p] = "live"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, victims, doomed, bcand, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

PTake(p) ==
  /\ pc[p] = "take"
  /\ IF Admits(p, "reclaim")
       THEN /\ holders' = holders \cup {[who |-> p, kind |-> "reclaim"]}
            /\ pc' = [pc EXCEPT ![p] = "recheck"]
            /\ UNCHANGED cred
       ELSE /\ pc' = [pc EXCEPT ![p] = "idle"]                      \* HISTORY_FENCED: skipped, passed again
            /\ cred' = [cred EXCEPT ![p] = "none"]
            /\ UNCHANGED holders
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, victims, doomed, bcand, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

PRecheck(p) ==
  /\ pc[p] = "recheck"
  /\ IF On("PolicyReread") /\ policy # pol[p]
       THEN pc' = [pc EXCEPT ![p] = "release"]                        \* POLICY_CHANGED: nothing deleted, retried
       ELSE pc' = [pc EXCEPT ![p] = "list"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, victims, doomed, bcand, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

\* Lance's cleanup as it starts: its refs, its head and its manifests read once; the versions it removes, the files only
\* they name, and the root's files no manifest names that are past the unverified age. Base files are never its.
PList(p) ==
  /\ pc[p] = "list"
  /\ LET v == IF pol[p] = "keep" THEN {x \in manifests : x # head /\ x \notin pinned /\ x \in expired} ELSE {}
         kept == manifests \ v
         verified == Named(v) \ Named(kept)
         unverified == {f \in files : f \notin Named(manifests) /\ f \notin Young}
     IN /\ victims' = [victims EXCEPT ![p] = v]
        /\ doomed' = [doomed EXCEPT ![p] = (verified \cup unverified) \ Base]
        /\ everVictim' = everVictim \cup v
  /\ pc' = [pc EXCEPT ![p] = "data"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, bcand, holders, tpc, rpc, dpc, wpc, faults, answered, unheldDelete>>

\* A delete the store refuses (an expired credential) fails the pass: Lance raises, and the hold is released.
Refused(p) ==
  /\ pc' = [pc EXCEPT ![p] = "release"]
  /\ UNCHANGED <<manifests, files, doomed, victims, bcand, unheldDelete>>

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
  /\ UNCHANGED <<manifests, head, names, pinned, expired, policy, pol, cred, victims, bcand, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim>>

PDelMan(p) ==
  /\ pc[p] = "man"
  /\ IF victims[p] = {}
       THEN /\ pc' = [pc EXCEPT ![p] = "blist"] /\ UNCHANGED <<manifests, victims, unheldDelete>>
       ELSE IF cred[p] # "live"
         THEN /\ pc' = [pc EXCEPT ![p] = "release"] /\ UNCHANGED <<manifests, victims, unheldDelete>>
         ELSE \E v \in victims[p] :
                /\ manifests' = manifests \ {v}
                /\ victims' = [victims EXCEPT ![p] = @ \ {v}]
                /\ Deleted(p)
                /\ UNCHANGED pc
  /\ UNCHANGED <<head, names, files, pinned, expired, policy, pol, cred, doomed, bcand, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim>>

\* base_reclaim: what every retained version names is read now, after the cleanup.
PBList(p) ==
  /\ pc[p] = "blist"
  /\ bcand' = [bcand EXCEPT ![p] = {f \in Base \cap files : f \notin Named(manifests) /\ f \notin Young}]
  /\ pc' = [pc EXCEPT ![p] = "bdel"]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, victims, doomed, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

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
  /\ UNCHANGED <<manifests, head, names, pinned, expired, policy, pol, cred, victims, doomed, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim>>

PRelease(p) ==
  /\ pc[p] = "release"
  /\ holders' = Drop(p)
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ cred' = [cred EXCEPT ![p] = "none"]
  /\ victims' = [victims EXCEPT ![p] = {}]
  /\ doomed' = [doomed EXCEPT ![p] = {}]
  /\ bcand' = [bcand EXCEPT ![p] = {}]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

\* ---------------------------------------------------------------- faults
Crash(p) ==
  /\ pc[p] \in Active /\ faults < MaxFaults
  /\ pc' = [pc EXCEPT ![p] = "dead"]
  /\ faults' = faults + 1
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, cred, victims, doomed, bcand, holders, tpc, rpc, dpc, wpc, answered, everVictim, unheldDelete>>

Expire(p) ==
  /\ cred[p] = "live" /\ (pc[p] = "dead" \/ faults < MaxFaults)
  /\ cred' = [cred EXCEPT ![p] = "dead"]
  /\ faults' = IF pc[p] = "dead" THEN faults ELSE faults + 1
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pc, pol, victims, doomed, bcand, holders, tpc, rpc, dpc, wpc, answered, everVictim, unheldDelete>>

\* A hold whose holder stopped renewing (dead, or paused past HOLD) is swept by the next take.
Lapse(p) ==
  /\ Holds(p) /\ pc[p] # "release"
  /\ IF On("CredBeforeHold") THEN cred[p] # "live" ELSE faults < MaxFaults
  /\ holders' = Drop(p)
  /\ faults' = IF On("CredBeforeHold") THEN faults ELSE faults + 1
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, tpc, rpc, dpc, wpc, answered, everVictim, unheldDelete>>

\* A task delivered again once its pass died (WorkQueue): the next pass of the table.
Restart(p) ==
  /\ pc[p] = "dead" /\ ~Holds(p) /\ cred[p] # "live"
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ cred' = [cred EXCEPT ![p] = "none"]
  /\ victims' = [victims EXCEPT ![p] = {}]
  /\ doomed' = [doomed EXCEPT ![p] = {}]
  /\ bcand' = [bcand EXCEPT ![p] = {}]
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, policy, pol, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

\* Time: a superseded version passes its retention, on whatever clock.
Age ==
  /\ \E v \in manifests \ (expired \cup {head}) : expired' = expired \cup {v}
  /\ UNCHANGED <<manifests, head, names, files, pinned, policy, pc, pol, cred, victims, doomed, bcand, holders, tpc, rpc, dpc, wpc, faults, answered, everVictim, unheldDelete>>

\* ---------------------------------------------------------------- the doors
\* Every commit lands as the version after the head (Lance's commit is put-if-not-exists on the next slot).
NewVersion == head + 1

DoorVars == <<manifests, head, names, files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, faults, everVictim, unheldDelete>>

\* A door's take of the history as REFS where its guard holds; one a RECLAIM refuses answers 409 and writes nothing.
TakeFor(fenced, who) ==
  IF ~fenced THEN [ok |-> TRUE, h |-> holders]
  ELSE IF Admits(who, "refs") THEN [ok |-> TRUE, h |-> holders \cup {[who |-> who, kind |-> "refs"]}]
  ELSE [ok |-> FALSE, h |-> holders]

\* The version a ref door names may be pinned or restored: it stands and (WholeRef) every file it names is stored.
Admissible == Old \in manifests /\ (On("WholeRef") => Whole(Old))

TTake ==
  /\ tpc = "idle"
  /\ LET t == TakeFor(On("TagFence"), "tag") IN (tpc' = (IF t.ok THEN "check" ELSE "done") /\ holders' = t.h)
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
  /\ pinned' = IF Old \in manifests THEN pinned \cup {Old} ELSE pinned
  /\ tpc' = "done"
  /\ holders' = Drop("tag")
  /\ UNCHANGED <<manifests, head, names, files, expired, policy, pc, pol, cred, victims, doomed, bcand, faults, everVictim, unheldDelete, rpc, dpc, wpc, answered>>

RTake ==
  /\ rpc = "idle"
  /\ LET t == TakeFor(On("RestoreFence"), "restore") IN (rpc' = (IF t.ok THEN "check" ELSE "done") /\ holders' = t.h)
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
  /\ names' = IF Old \in manifests THEN [names EXCEPT ![NewVersion] = names[Old]] ELSE names
  /\ manifests' = IF Old \in manifests THEN manifests \cup {NewVersion} ELSE manifests
  /\ head' = IF Old \in manifests THEN NewVersion ELSE head
  /\ rpc' = "done"
  /\ holders' = Drop("restore")
  /\ UNCHANGED <<files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, faults, everVictim, unheldDelete, tpc, dpc, wpc, answered>>

DTake ==
  /\ dpc = "idle"
  /\ LET t == TakeFor(On("PolicyDoorFence"), "policy") IN (dpc' = (IF t.ok THEN "check" ELSE "done") /\ holders' = t.h)
  /\ UNCHANGED <<tpc, rpc, wpc, answered>>
  /\ UNCHANGED DoorVars
\* The door answers 200 once the policy keeping the whole history stands.
DWrite ==
  /\ dpc = "check"
  /\ policy' = "hold"
  /\ answered' = {v \in manifests : Whole(v)}
  /\ dpc' = "done"
  /\ holders' = Drop("policy")
  /\ UNCHANGED <<manifests, head, names, files, pinned, expired, pc, pol, cred, victims, doomed, bcand, faults, everVictim, unheldDelete, tpc, rpc, wpc>>

\* A writer's commit: its files written first, its version naming the head's files and them.
WStage ==
  /\ wpc = "idle"
  /\ files' = files \cup {"w"}
  /\ wpc' = "staged"
  /\ UNCHANGED <<manifests, head, names, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, holders, tpc, rpc, dpc, faults, answered, everVictim, unheldDelete>>
WCommit ==
  /\ wpc = "staged"
  /\ names' = [names EXCEPT ![NewVersion] = names[head] \cup {"w"}]
  /\ manifests' = manifests \cup {NewVersion}
  /\ head' = NewVersion
  /\ wpc' = "done"
  /\ UNCHANGED <<files, pinned, expired, policy, pc, pol, cred, victims, doomed, bcand, holders, tpc, rpc, dpc, faults, answered, everVictim, unheldDelete>>

PassStep(p) == PStart(p) \/ PTake(p) \/ PRecheck(p) \/ PList(p) \/ PDelData(p) \/ PDelMan(p) \/ PBList(p) \/ PBDel(p) \/ PRelease(p)

Next ==
  \/ \E p \in Passes : PassStep(p) \/ Crash(p) \/ Expire(p) \/ Lapse(p) \/ Restart(p)
  \/ Age
  \/ TTake \/ TCheck \/ TWrite
  \/ RTake \/ RCheck \/ RWrite
  \/ DTake \/ DWrite
  \/ WStage \/ WCommit

Fairness ==
  /\ \A p \in Passes : WF_vars(PassStep(p)) /\ WF_vars(Restart(p)) /\ WF_vars(Lapse(p) /\ pc[p] = "dead")
                       /\ WF_vars(Expire(p) /\ pc[p] = "dead")
  /\ WF_vars(TTake \/ TCheck \/ TWrite) /\ WF_vars(RTake \/ RCheck \/ RWrite) /\ WF_vars(DTake \/ DWrite) /\ WF_vars(WStage \/ WCommit)

Spec == Init /\ [][Next]_vars /\ Fairness

PassSymmetry == Permutations(Passes)

\* ---------------------------------------------------------------- properties
\* Nothing live is deleted: the head and every pinned version stay whole.
RefsWhole == Whole(head) /\ \A v \in pinned : Whole(v)
\* A commit in flight keeps the files it wrote until it lands.
InFlightKept == wpc = "staged" => "w" \in files
\* A policy keeping the whole history that answered 200 keeps every version that was whole then.
RetentionKept == \A v \in answered : Whole(v)
\* A version that stands over deleted files is one a cleanup chose, never another.
HalfOnlyVictims == \A v \in manifests : ~Whole(v) => v \in everVictim
\* Every delete is the pass's while it holds the table's history.
DeletesHeld == ~unheldDelete

\* Once faults stop, the next pass finishes what a dead one began: no version stands over deleted files.
EventuallyWhole == <>[](\A v \in manifests : Whole(v))
=============================================================================
