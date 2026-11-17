--------------------------- MODULE TableLifecycle ---------------------------
(***************************************************************************)
(* The catalog's table lifecycle: what create, drop (destructive or to the *)
(* trash), undrop, rename and protection do to a table name, step by step, *)
(* under concurrent callers, crashes and failed effects, with maintenance's*)
(* release and purge running at any time.                                  *)
(*                                                                         *)
(* Each door is a sequence of separate effects on                          *)
(*   (a) __manifest, the catalog's record of names (`rows`),               *)
(*   (b) the bytes at a table's location (`bytes`),                        *)
(*   (c) the location's claim on the control root (`claim`),               *)
(*   (d) the trash record (`trash`),                                       *)
(*   (e) the protection record (`prot`) and the OpenFGA tuples (`grants`), *)
(* in the order the code makes them. A location stands for the table's     *)
(* uuid: a create mints both at once (dataplane.declare_table puts the     *)
(* table at <root>/<uuid>), and a rename and an undrop keep both.          *)
(*                                                                         *)
(* The doors, and the functions each action cites:                         *)
(*   create   endpoints/data.py create_table, services/table_create.py     *)
(*            create_governed_table, services/dataplane.py create_table,   *)
(*            declare_table, _write_blob_into, _unreserve                  *)
(*   drop     endpoints/tables.py drop_table and _drop, dataplane          *)
(*            destroy_table (grace 0) or the trash branch (grace > 0)      *)
(*   undrop   endpoints/tables.py undrop_table                             *)
(*   rename   endpoints/tables.py rename_table, dataplane.rename_table     *)
(*   protect  endpoints/tables.py set_table_protection                     *)
(*   cascade  endpoints/namespaces.py drop_namespace's table step: the     *)
(*            listing and _require_descendants_unprotected, then           *)
(*            _destroy_listed_table or _trash_listed_table (resolve,       *)
(*            fence_listed, protection read again, the drop's effects)     *)
(*   nsundrop endpoints/namespaces.py undrop_namespace's table step, which *)
(*            is the table undrop's own (tables.restore_kept_table),       *)
(*            admitted by the namespace's record with no standing check    *)
(*   stock    a stock client's create: the declare door                    *)
(*            (dataplane.declare_table, its claim fenced for the declarer, *)
(*            DeclarationFence), its own Lance write on vended             *)
(*            credentials, and its first commit through version/create     *)
(*            (endpoints/versions.py: DoorTable.fence_first_version, then  *)
(*            the put-if-not-exists, then the fence's lift); stockinto is  *)
(*            another principal's stock client writing version 1 into the  *)
(*            declaration (DescribeTable, then the same write and commit)  *)
(*   owncreate the catalog's create door called by the declarer's own     *)
(*            principal on its declared name (dataplane.create_table)      *)
(*   release  endpoints/trash_reclaim.py release_trash                     *)
(*   purge    maintenance/services/purge.py _purge_one, with the catalog's *)
(*            vend_reclaim_credentials and end_trash doors                 *)
(* Reconcile (maintenance/services/reconcile.py) writes nothing: it is the *)
(* report the liveness property leans on (see Quiet).                      *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. RegisterTable adds a name to __manifest put-if-not-exists, so of  *)
(*       two adds of one name exactly one lands (the `dir` catalog's       *)
(*       manifest commit, lance_docs/ns_catalog/catalog/dir/index.md       *)
(*       "Manifest Table Commits"). Atomic here. TableLifecycleNoNameCAS   *)
(*       makes the add unconditional, and NameLiveOnce fails.              *)
(*   A2. A claim is written only under the ETag its writer read           *)
(*       (lakehouse/location_claims.py fence, hand_over, take, release;   *)
(*       lakehouse/records.py create_json and replace_json), and a fence   *)
(*       refuses every other request's fence, hand-over and take. Atomic   *)
(*       here. TableLifecycleNoFence lets a drop act without its fence     *)
(*       and a rename hand over a claim changed or fenced since it read    *)
(*       it, and AnsweredCreateStands fails: the rename deregisters by     *)
(*       name the table created under the name after the drop removed the  *)
(*       one the rename resolved.                                          *)
(*   A3. An undrop admitted before its record's deadline finishes before   *)
(*       the release, which waits trash.RELEASE_SETTLE (15 min) past the   *)
(*       deadline (lakehouse/trash.py), and the purge takes only released *)
(*       records (purge.due_from). TableLifecycleNoSettle lets the release *)
(*       land while such an undrop runs, and LiveHasData fails: the purge  *)
(*       deletes the bytes the undrop then registers.                      *)
(*   A4. A Lance create of a dataset refuses a location that already holds *)
(*       one ("Dataset already exists", dataplane._write_blob) before it   *)
(*       writes a row, and its commit is NOT conditional on that check: a  *)
(*       create whose check passed before another committed lands as the  *)
(*       next version, over the other's rows (measured on pylance 13       *)
(*       through the catalog, TableLifecycleCreateBothAnswered). Two steps *)
(*       here, CWrite and CCommit.                                         *)
(*   A5. A fence lapses LEASE after it was last written (Lapse): at once  *)
(*       here once its request has ended, and, with LiveLapse, while its   *)
(*       request still runs a step that can outlast LEASE (LongSteps: a    *)
(*       create's Lance write, a destroy's delete, a stock client's own    *)
(*       write; a declaration's fence wherever its declarer is). What is   *)
(*       assumed is narrower: a step that RENEWED its fence (a create's    *)
(*       commit and its rollback, CreateRenewsFence; a stock client's      *)
(*       finalize, StockFencesFirstVersion) ends within LEASE, one object  *)
(*       write after the renewal. A lapse here writes the claim (its ETag  *)
(*       moves), where the code's writes nothing: a declarer whose fence   *)
(*       lapsed unseen is refused here and admitted by the code, which    *)
(*       only spares it a 409. TableLifecycleCommitOutlastsLease lets      *)
(*       the fence lapse inside the commit, and OneCreateCommitsPerTable   *)
(*       fails. LEASE is otherwise not modelled: a claim held under        *)
(*       another name refuses every take and fence here, where the code    *)
(*       lets one whose holder is gone be taken past its lease             *)
(*       (location_claims._taken, holder_is_gone).                         *)
(*                                                                         *)
(* GUARDS, each a step of the design the seven findings of ADR 0227      *)
(* showed it needs; `Off` names those a config removes, and each such     *)
(* config must fail (EXPECT VIOLATION):                                   *)
(*   CreateFence  a create holds its table's claim fenced from its claim  *)
(*       until its data is committed; on a name another request declared  *)
(*       it fences the claim first, refusing one another request fences,  *)
(*       then answers 409 unless the name still describes that location   *)
(*       with no data there. TableLifecycleCreateRace,                    *)
(*       TableLifecycleCreateBothAnswered.                                *)
(*   CreateFenceHoldsDoors  every other door respects a create's fence:   *)
(*       no drop resolves and fences a table still being created.         *)
(*       TableLifecycleSoftDropWrongRow.                                  *)
(*   UndropFence  an undrop fences the claim it restores before it        *)
(*       registers, refusing one released, fenced or held by another      *)
(*       name, then answers 409 unless its trash record is still the one  *)
(*       it admitted. TableLifecycleUndropOverDestroyed.                  *)
(*   UndropHoldsFence  the undrop lifts that fence only once its trash    *)
(*       record is cleared. TableLifecycleUndropClearsRedrop.             *)
(*   UndropOwnConflict  a TableAlreadyExists is the undrop's own success  *)
(*       only while the name describes the restored location.             *)
(*       TableLifecycleUndropRace.                                        *)
(*   DropRereadsProtection  a drop reads protection again once it holds   *)
(*       its fence. TableLifecycleProtectRace.                            *)
(*   ProtectChecksFence  the protection door writes its record, then      *)
(*       answers 409 unless the name still describes the location under   *)
(*       the claim it resolved, unfenced. TableLifecycleProtectUnchecked. *)
(* and the four review found (ADR 0227 §8):                               *)
(*   CascadeRereadsProtection  a cascade reads each table's protection    *)
(*       again once it holds that table's fence.                          *)
(*       TableLifecycleCascadeProtectRace.                                *)
(*   CreateRenewsFence  a create renews its fence, conditional on its own *)
(*       token under the ETag it reads, right before its commit and its   *)
(*       rollback; a fence another request holds now answers 409 and      *)
(*       nothing is committed or rolled back. TableLifecycleCreateLapse.  *)
(* The namespace undrop's table step runs UndropFence, UndropHoldsFence   *)
(* and UndropOwnConflict, the table undrop's own steps:                   *)
(* TableLifecycleNsUndropRace removes the last.                           *)
(* and the three that close StockCreateRace (ADR 0227 §9, owner          *)
(* 2026-11-17: "Declaration is its declarer's"):                          *)
(*   DeclarationFence  DeclareTable takes its claim fenced for its        *)
(*       declarer (location_claims.ClaimFence.declarer), so every other   *)
(*       request's fence, take and hand-over is refused until the fence   *)
(*       lapses or the declarer's first version lifts it. A door that     *)
(*       removes the table (a drop, a cascade's table step) takes such a  *)
(*       fence over, so a declared, unwritten table is dropped at once.   *)
(*       TableLifecycleStockCreateRace.                                   *)
(*   DeclarerLifts  only a request of the declaring principal (its        *)
(*       authenticated subject) takes a declaration's fence over to write *)
(*       version 1: its version/create, or its own catalog create.        *)
(*       TableLifecycleStockIntruder.                                     *)
(*   StockFencesFirstVersion  version/create of a version 1 fences the   *)
(*       claim (one write under the ETag of the claim it reads, refused   *)
(*       while another request fences it) before the put-if-not-exists,  *)
(*       and lifts the fence after: a declarer whose own fence lapsed in  *)
(*       its write renews it so only while nobody acted on the claim.    *)
(*       TableLifecycleStockLapse.                                        *)
(* and the one review of §9 found (ADR 0227 §9, 2026-11-17):             *)
(*   RedeclareRenews  the declarer asking for its declaration again (its  *)
(*       DeclareTable sent again, its client's credential refresh)        *)
(*       renews the declaration's fence under the ETag it reads before a  *)
(*       credential is vended, which lasts no longer than the fence; a    *)
(*       claim another request acted on refuses the renewal and nothing  *)
(*       is vended. TableLifecycleStockRedeclare.                         *)
(*                                                                         *)
(* NOT MODELLED: namespaces' own rows (Cascade.tla); deregister and       *)
(* register as doors of their own; Overwrite and ExistOk creates;         *)
(* idempotency keys; the announcements; base records, data directories    *)
(* and clone endings; `force`, which turns protection off on purpose; a   *)
(* lease's clock beyond the lapse (A5); a purge refusal retried on a      *)
(* later tick (a noted refusal stays noted); a stock declare that fails  *)
(* after its claim and rolls back (its rollback renews its own fence     *)
(* first, as a create's does, so a declaration answered again since is    *)
(* left standing: pinned by a test, ADR 0227 §9).                         *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Ops1, Ops2, Ops3,   \* the operations each of the three callers may run, one per behaviour
          InitialTable,       \* whether name n1 starts as a written table at L0
          MaxFaults,          \* how many effects may fail or processes die in one behaviour
          NameCAS,            \* A1: RegisterTable is put-if-not-exists on the name
          Fences,             \* A2: a door's fence and a rename's hand-over are conditional and respected
          SettleOutlastsRequests, \* A3: the release never lands while a request acting on the table still runs
          LiveLapse,          \* A5: a fence lapses while its request still runs one of LongSteps
          CommitOutlastsLease, \* A5 removed: a fence also lapses inside the commit its create renewed it for
          Off                 \* the guards removed, each to show the design needs it (GUARDS above)

Guards == {"CreateFence", "CreateFenceHoldsDoors", "UndropFence", "UndropHoldsFence", "UndropOwnConflict",
           "DropRereadsProtection", "ProtectChecksFence", "CascadeRereadsProtection", "CreateRenewsFence",
           "DeclarationFence", "DeclarerLifts", "StockFencesFirstVersion", "RedeclareRenews"}
ASSUME Off \subseteq Guards
On(g) == g \notin Off

Procs  == {"p1", "p2", "p3"}
Names  == {"n1", "n2"}
Locs   == {"L0", "L1", "L2", "L3"}
None   == "-"
\* The location (and uuid) a create by each caller mints.
Mint   == [p \in Procs |-> CASE p = "p1" -> "L1" [] p = "p2" -> "L2" [] p = "p3" -> "L3"]

\* An operation is "<door>_<name>" or "rename_<from>_<to>".
AllOps == {"none", "create_n1", "create_n2", "drop_n1", "softdrop_n1", "undrop_n1", "rename_n1_n2", "protect_n1",
           "cascade_n1", "softcascade_n1", "nsundrop_n1", "stock_n1", "stockinto_n1", "owncreate_n1"}
Door(o)   == CASE o = "none" -> "none" [] o \in {"create_n1", "create_n2", "owncreate_n1"} -> "create"
                 [] o \in {"drop_n1", "cascade_n1"} -> "drop" [] o \in {"softdrop_n1", "softcascade_n1"} -> "softdrop"
                 [] o \in {"undrop_n1", "nsundrop_n1"} -> "undrop"
                 [] o = "rename_n1_n2" -> "rename" [] o = "protect_n1" -> "protect" [] o \in {"stock_n1", "stockinto_n1"} -> "stock"
NameOf(o) == IF o = "create_n2" THEN "n2" ELSE "n1"
Dest(o)   == "n2"
\* A namespace cascade's table step: the drop's effects, after a listing that read protection before the resolution.
Cascaded(o) == o \in {"cascade_n1", "softcascade_n1"}
\* The steps a live request can stay in past LEASE: a create's Lance write (c_commit is the write before the commit), a
\* destroy's delete; with CommitOutlastsLease also the commit after the create renewed its fence.
LongSteps == {"c_commit", "d_delete", "x_write"} \cup (IF CommitOutlastsLease THEN {"c_put"} ELSE {})
\* A create renews its fence before its commit and its rollback (CreateRenewsFence), where it holds one (CreateFence).
Renews == On("CreateFence") /\ On("CreateRenewsFence") /\ Fences

VARIABLES rows,      \* __manifest: the <<name, location>> rows
          bytes,     \* what each location holds: none, the reservation marker, data, or deleted data
          claim,     \* each location's claim: held or released, by which name, fenced by which request, its ETag
          trash,     \* each location's trash record: none, kept (inside the grace), expired, released
          prot,      \* locations whose table holds a protection record
          grants,    \* locations whose table holds OpenFGA tuples
          op,        \* what each caller runs
          pc,        \* where each caller is
          lloc,      \* the location a caller acts on
          lver,      \* the claim ETag a caller read
          lx,        \* a second location a caller holds: the row a by-name call removed
          lpath,     \* a create's path: a fresh table or one an earlier request declared
          mpc, mloc, \* the purge's step and the record it is on
          faults,
          \* History, read only by the invariants.
          acked,       \* locations whose create answered 200
          doorEnded,   \* locations a drop or rename door removed from __manifest, as the door that resolved them meant to
          softDropped, \* locations a recoverable drop removed and nothing has registered again
          undropOk,    \* <<name, location>> an undrop answered 200 for, until a door acts on the table again
          purged,      \* locations the purge ended
          protHit,     \* data was deleted while its table held protection
          purgeHitLive, \* the purge deleted bytes a row named
          overwritten, \* locations where a create's commit landed over rows another create committed
          protOk,      \* locations whose protection answered 200 (an initial record counts as one)
          owed,        \* locations declared, whose declaration has neither lapsed nor been ended or written by its declarer
          intruded,    \* locations another principal took to write version 1 into while their declaration was owed
          cred         \* the write credential a declaration's declarer holds for each location: none; bounded, vended
                       \* right after its declaration's fence was written or renewed, so it expires within the
                       \* request's latency of that fence's lapse; unbounded, vended under a fence written earlier,
                       \* so it can outlive the fence; stale, an unbounded one still live after its fence lapsed

store  == <<rows, bytes, claim, trash, prot, grants>>
locals == <<lloc, lver, lx, lpath>>
hist   == <<acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>
vars   == <<store, op, pc, locals, mpc, mloc, faults, hist>>

NoClaim  == [st |-> "none", name |-> None, fence |-> None, decl |-> FALSE, ver |-> 0]
NoRecord == [st |-> "none", name |-> None, residue |-> FALSE, refused |-> FALSE]

Init ==
  /\ op \in {f \in [Procs -> AllOps] : f["p1"] \in Ops1 /\ f["p2"] \in Ops2 /\ f["p3"] \in Ops3}
  /\ pc = [p \in Procs |-> "start"]
  /\ rows = IF InitialTable THEN {<<"n1", "L0">>} ELSE {}
  /\ bytes = [l \in Locs |-> IF InitialTable /\ l = "L0" THEN "data" ELSE "none"]
  /\ claim = [l \in Locs |-> IF InitialTable /\ l = "L0" THEN [st |-> "held", name |-> "n1", fence |-> None, decl |-> FALSE, ver |-> 1] ELSE NoClaim]
  /\ trash = [l \in Locs |-> NoRecord]
  /\ prot \in IF InitialTable THEN {{}, {"L0"}} ELSE {{}}
  /\ grants = IF InitialTable THEN {"L0"} ELSE {}
  /\ lloc = [p \in Procs |-> None] /\ lver = [p \in Procs |-> 0] /\ lx = [p \in Procs |-> None] /\ lpath = [p \in Procs |-> None]
  /\ mpc = "idle" /\ mloc = None
  /\ faults = 0
  /\ acked = IF InitialTable THEN {"L0"} ELSE {}
  /\ doorEnded = {} /\ softDropped = {} /\ undropOk = {} /\ purged = {} /\ protHit = FALSE /\ purgeHitLive = FALSE /\ overwritten = {}
  /\ protOk = prot
  /\ owed = {} /\ intruded = {}
  /\ cred = [l \in Locs |-> "none"]

---------------------------------------------------------------------------
\* Helpers.

Named(n)  == {r \in rows : r[1] = n}
\* What a describe of the name answers: the location of its row, or None.
LocOf(n)  == IF Named(n) = {} THEN None ELSE (CHOOSE r \in Named(n) : TRUE)[2]
Live(l)   == \E r \in rows : r[2] = l
Goto(p, s) == pc' = [pc EXCEPT ![p] = s]
Fault   == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults
Bump(c) == [c EXCEPT !.ver = @ + 1]
\* Who a request authenticates as: the declarer (stock_n1) and its own catalog create (owncreate_n1) are principal "A",
\* every other request a principal of its own. Only stock_n1 declares, so a declaration's declarer is always "A".
Principal(p) == IF op[p] \in {"stock_n1", "owncreate_n1"} THEN "A" ELSE p
\* The guard that reads protection again under the fence, for the door p runs.
Reprotects(p) == IF Cascaded(op[p]) THEN On("CascadeRereadsProtection") ELSE On("DropRereadsProtection")

\* A fence another request holds: the claim refuses this request's fence, hand-over and take. Without
\* CreateFenceHoldsDoors only a create respects a create's fence.
FencedAgainst(l, p) ==
  /\ Fences /\ claim[l].st = "held" /\ claim[l].fence \notin {None, p}
  /\ (On("CreateFenceHoldsDoors") \/ Door(op[p]) = "create" \/ Door(op[claim[l].fence]) # "create")

\* A declaration's fence (location_claims.ClaimFence.declarer) that request p takes over: a door removing the table takes
\* any (table_claims.fence), a request writing version 1 only its own principal's (DeclarerLifts).
TakesOver(l, p) ==
  /\ claim[l].decl
  /\ IF Door(op[p]) \in {"drop", "softdrop"} THEN TRUE ELSE (~On("DeclarerLifts") \/ Principal(p) = "A")

\* A fence that refuses request p: another request's, unless p takes a declaration's over.
Against(l, p) == IF FencedAgainst(l, p) THEN ~TakesOver(l, p) ELSE FALSE

\* Request p takes the claim of `l` to write version 1 or to remove the table: a declaration owed to its declarer is no
\* longer owed, and one another principal took to write into is an intrusion.
Owes(l, p) ==
  /\ owed' = owed \ {l}
  /\ intruded' = IF l \in owed /\ Door(op[p]) \notin {"drop", "softdrop"} /\ Principal(p) # "A" THEN intruded \cup {l} ELSE intruded
  /\ UNCHANGED cred

\* location_claims.lift_fence: request p's own fence goes; a claim it no longer fences is left.
Unfenced(l, p) == claim' = [claim EXCEPT ![l] = IF @.fence = p THEN [Bump(@) EXCEPT !.fence = None, !.decl = FALSE] ELSE @]

\* Deleting what a location holds: data becomes deleted data, a marker nothing.
Deleted(b) == IF b = "data" \/ b = "deleted" THEN "deleted" ELSE "none"
DeleteBytes(l) ==
  /\ bytes' = [bytes EXCEPT ![l] = Deleted(@)]
  /\ protHit' = (protHit \/ (bytes[l] = "data" /\ l \in protOk))

\* A door removed the row of `l` by its own act; what was answered of it no longer holds.
Retired(l) == undropOk' = {u \in undropOk : u[2] # l}

\* location_claims.release(location, name[, uuid]): only a claim this name holds is released.
Released(l, n) == claim' = [claim EXCEPT ![l] = IF @.st = "held" /\ @.name = n THEN [Bump(@) EXCEPT !.st = "released", !.fence = None, !.decl = FALSE] ELSE @]

\* A request acting on the table at `l` is in flight from its resolution (an undrop's: its admission) to its end.
RequestInFlight(l) == \E p \in Procs : lloc[p] = l /\ pc[p] \notin {"start", "done", "dead"}

Start(p) ==
  /\ pc[p] = "start" /\ NoFault
  /\ pc' = [pc EXCEPT ![p] = CASE Door(op[p]) = "none" -> "done" [] Door(op[p]) = "create" -> "c_probe"
                                [] Cascaded(op[p]) -> "k_list"
                                [] Door(op[p]) \in {"drop", "softdrop"} -> "d_resolve" [] Door(op[p]) = "undrop" -> "u_find"
                                [] Door(op[p]) = "rename" -> "r_resolve" [] Door(op[p]) = "protect" -> "p_resolve"
                                [] Door(op[p]) = "stock" -> "x_probe"]
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

---------------------------------------------------------------------------
\* CREATE (mode Create).

\* dataplane.create_table: `existing = _existing_location(ns, segments)`, `only_declared = not holds_a_version(existing)`.
CProbe(p) ==
  LET n == NameOf(op[p]) ex == LocOf(n) IN
  /\ pc[p] = "c_probe" /\ NoFault
  /\ CASE ex # None /\ bytes[ex] = "data" -> Goto(p, "done") /\ UNCHANGED <<lloc, lpath>>   \* TableAlreadyExists
       [] ex # None -> Goto(p, "c_take") /\ lloc' = [lloc EXCEPT ![p] = ex] /\ lpath' = [lpath EXCEPT ![p] = "declared"]
       [] OTHER -> Goto(p, "c_claim") /\ lloc' = [lloc EXCEPT ![p] = Mint[p]] /\ lpath' = [lpath EXCEPT ![p] = "fresh"]
  /\ UNCHANGED <<store, op, lver, lx, mpc, mloc, hist>>

\* Declared path, dataplane._fence_the_declaration: one write under the ETag of the claim it reads, fencing the claim
\* this name holds for this request; a claim another request fences (a create still writing, a declaration of another
\* principal's) answers 409 ConcurrentModification, a released one or one another name holds 409 as well; the
\* declarer's own create takes its declaration's fence over (TakesOver). Without CreateFence: the old
\* table_claims.take, which read a claim this name holds as its own retry and wrote nothing.
CTake(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "c_take" /\ NoFault
  /\ IF On("CreateFence")
       THEN IF Against(l, p) \/ claim[l].st # "held" \/ claim[l].name # n
              THEN Goto(p, "done") /\ UNCHANGED <<claim, owed, intruded, cred>>
              ELSE Goto(p, "c_recheck") /\ claim' = [claim EXCEPT ![l] = [Bump(@) EXCEPT !.fence = p, !.decl = FALSE]] /\ Owes(l, p)
       ELSE CASE Against(l, p) -> Goto(p, "done") /\ UNCHANGED <<claim, owed, intruded, cred>>
              [] claim[l].st = "held" /\ claim[l].name = n -> Goto(p, "c_write") /\ UNCHANGED claim /\ Owes(l, p)
              [] claim[l].st # "held" -> Goto(p, "c_rb_drop") /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> n, fence |-> None, decl |-> FALSE, ver |-> @.ver + 1]] /\ UNCHANGED <<owed, intruded, cred>>
              [] OTHER -> Goto(p, "done") /\ UNCHANGED <<claim, owed, intruded, cred>>
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk>>

\* The probe again, under the fence: the name still describes this location (a rollback that ran since removed the
\* declaration: 409 ConcurrentModification), and holds no version (a create that finished since wrote the table: 409
\* TableAlreadyExists), each with nothing to roll back. No other request writes either while the fence stands.
CRecheck(p) ==
  /\ pc[p] = "c_recheck" /\ NoFault
  /\ Goto(p, IF LocOf(NameOf(op[p])) # lloc[p] \/ bytes[lloc[p]] = "data" THEN "c_lift" ELSE "c_write")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* The create lifts its fence once its data is committed (then answers), or once it refused (then ends).
CLift(p) ==
  /\ pc[p] \in {"c_unfence", "c_lift"} /\ NoFault
  /\ Unfenced(lloc[p], p)
  /\ Goto(p, IF pc[p] = "c_unfence" THEN "c_answer" ELSE "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* Fresh path, dataplane.declare_table: claim_new_table on the minted location (nobody else names it), fenced for this
\* request (CreateFence), then before_register (fga_deps.GrantsBeforeWrite), then _reserve, then RegisterTable.
CClaim(p) ==
  /\ pc[p] = "c_claim" /\ NoFault
  /\ claim' = [claim EXCEPT ![lloc[p]] = [st |-> "held", name |-> NameOf(op[p]), fence |-> IF On("CreateFence") THEN p ELSE None, decl |-> FALSE, ver |-> @.ver + 1]]
  /\ Goto(p, "c_grant")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

CGrant(p) ==
  /\ pc[p] = "c_grant" /\ NoFault
  /\ grants' = grants \cup {lloc[p]}
  /\ Goto(p, "c_reserve")
  /\ UNCHANGED <<rows, bytes, claim, trash, prot, op, locals, mpc, mloc, hist>>

CReserve(p) ==
  /\ pc[p] = "c_reserve" /\ NoFault
  /\ bytes' = [bytes EXCEPT ![lloc[p]] = "marker"]
  /\ Goto(p, "c_register")
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* RegisterTable(id, location=<uuid>): put-if-not-exists on the name (A1); a refusal runs _unreserve.
CRegister(p) ==
  LET n == NameOf(op[p]) IN
  /\ pc[p] = "c_register" /\ NoFault
  /\ IF NameCAS /\ Named(n) # {}
       THEN Goto(p, "c_unreserve") /\ UNCHANGED rows
       ELSE Goto(p, "c_write") /\ rows' = rows \cup {<<n, lloc[p]>>}
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, hist>>

CRegisterFails(p) ==
  /\ pc[p] = "c_register" /\ Fault
  /\ Goto(p, "c_unreserve")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* dataplane._unreserve: the marker goes, then the claim; then table_create's take_back_unless_standing.
CUnreserve(p) ==
  /\ pc[p] = "c_unreserve" /\ NoFault
  /\ bytes' = [bytes EXCEPT ![lloc[p]] = Deleted(@)]
  /\ Released(lloc[p], NameOf(op[p]))
  /\ Goto(p, "c_takeback")
  /\ UNCHANGED <<rows, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* fga_deps.GrantsBeforeWrite.take_back_unless_standing: the grants go unless the name stands at the location.
CTakeBack(p) ==
  /\ pc[p] = "c_takeback" /\ NoFault
  /\ grants' = IF <<NameOf(op[p]), lloc[p]>> \in rows THEN grants ELSE grants \ {lloc[p]}
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, claim, trash, prot, op, locals, mpc, mloc, hist>>

\* _write_blob(mode="create"), Lance's write_dataset: a location already holding a dataset is refused ("Dataset
\* already exists") before a row is written (A4).
CWrite(p) ==
  /\ pc[p] = "c_write" /\ NoFault
  /\ Goto(p, IF bytes[lloc[p]] = "data" THEN "c_rb_drop" ELSE "c_commit")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* The rows written (lance.fragment.write_fragments), dataplane._commit_under_fence renews the fence before the commit
\* (CreateRenewsFence): one write under the ETag it reads, conditional on the fence being this request's. A fence another
\* request holds now (this one's lapsed in the write, and a door took the claim) answers 409 ConcurrentModification, and
\* nothing is committed or rolled back: the table is that request's now.
CCommit(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "c_commit" /\ NoFault
  /\ IF Renews
       THEN IF claim[l].fence # p
              THEN Goto(p, "done") /\ UNCHANGED claim
              ELSE Goto(p, "c_put") /\ claim' = [claim EXCEPT ![l] = Bump(@)]
       ELSE Goto(p, "c_put") /\ UNCHANGED claim
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* The commit (LanceDataset.commit of the create's transaction) is not conditional on Lance's check: a create committed at
\* the location since is overwritten, this one landing as the table's next version (A4).
CPut(p) ==
  /\ pc[p] = "c_put" /\ NoFault
  /\ bytes' = [bytes EXCEPT ![lloc[p]] = "data"]
  /\ overwritten' = IF bytes[lloc[p]] = "data" THEN overwritten \cup {lloc[p]} ELSE overwritten
  /\ Goto(p, "c_unfence")
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, protOk, owed, intruded, cred>>

CWriteFails(p) ==
  /\ pc[p] \in {"c_write", "c_commit", "c_put"} /\ Fault
  /\ Goto(p, "c_rb_drop")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* _write_blob_into's rollback: `ns.drop_table(DropTableRequest(id=segments))`, the backend's DropTable BY NAME: it
\* removes whatever row the name has now, then deletes that row's directory
\* (lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md, DropTable, V2). TableNotFound is suppressed.
\* With CreateRenewsFence the rollback first renews the fence as the commit does: one another request holds now rolls
\* nothing back.
CRollbackDrop(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "c_rb_drop" /\ NoFault
  /\ IF Renews /\ claim[l].fence # p
       THEN Goto(p, "done") /\ UNCHANGED <<rows, lx, claim>>
       ELSE /\ claim' = IF Renews THEN [claim EXCEPT ![l] = Bump(@)] ELSE claim
            /\ IF Named(n) = {}
                 THEN Goto(p, "c_rb_release") /\ UNCHANGED <<rows, lx>>
                 ELSE /\ rows' = rows \ Named(n)
                      /\ lx' = [lx EXCEPT ![p] = LocOf(n)]
                      /\ Goto(p, "c_rb_delete")
  /\ UNCHANGED <<bytes, trash, prot, grants, op, lloc, lver, lpath, mpc, mloc, hist>>

CRollbackDelete(p) ==
  /\ pc[p] = "c_rb_delete" /\ NoFault
  /\ DeleteBytes(lx[p])
  /\ Goto(p, "c_rb_release")
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

\* The rollback releases the claim under the name and the uuid the claim recorded, then (fresh path) the grants.
CRollbackRelease(p) ==
  /\ pc[p] = "c_rb_release" /\ NoFault
  /\ Released(lloc[p], NameOf(op[p]))
  /\ Goto(p, IF lpath[p] = "fresh" THEN "c_takeback" ELSE "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

CAnswer(p) ==
  /\ pc[p] = "c_answer" /\ NoFault
  /\ acked' = acked \cup {lloc[p]}
  /\ Goto(p, "done")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

---------------------------------------------------------------------------
\* DROP: the resolution, the protection guard and the fence are common to both branches of _drop.

\* get_resolved_table (table_claims.resolve): the name's location and that location's claim with its ETag.
DResolve(p) ==
  LET l == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "d_resolve" /\ NoFault
  /\ IF l = None THEN Goto(p, "done") /\ UNCHANGED <<lloc, lver>>
     ELSE Goto(p, "d_protect") /\ lloc' = [lloc EXCEPT ![p] = l] /\ lver' = [lver EXCEPT ![p] = claim[l].ver]
  /\ UNCHANGED <<store, op, lx, lpath, mpc, mloc, hist>>

\* fga_deps.require_table_unprotected, read before the fence.
DProtect(p) ==
  /\ pc[p] = "d_protect" /\ NoFault
  /\ Goto(p, IF lloc[p] \in prot THEN "done" ELSE "d_fence")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* table_claims.fence -> location_claims.fence: one write under the ETag the resolution read (A2). A declaration's fence
\* is taken over (TakesOver): a declared, unwritten table is dropped without waiting for the fence to lapse.
DFence(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "d_fence" /\ NoFault
  /\ IF Fences /\ (claim[l].ver # lver[p] \/ Against(l, p) \/ (claim[l].st = "held" /\ claim[l].name # n))
       THEN Goto(p, "done") /\ UNCHANGED <<claim, owed, intruded, cred>>
       ELSE /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> n, fence |-> p, decl |-> FALSE, ver |-> @.ver + 1]]
            /\ Owes(l, p)
            /\ Goto(p, CASE Reprotects(p) -> "d_reprot" [] Door(op[p]) = "drop" -> "d_judge" [] OTHER -> "s_put")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk>>

\* _drop's require_table_unprotected again, once the fence stands: a protection the door's first read missed was
\* written before the protection door read the claim, which then refused it, or before this read. Protected: 409,
\* the fence lifted.
DReprotect(p) ==
  /\ pc[p] = "d_reprot" /\ NoFault
  /\ Goto(p, CASE lloc[p] \in prot -> "d_lift" [] Door(op[p]) = "drop" -> "d_judge" [] OTHER -> "s_put")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* Destructive (destroy_table): judge the bytes; a location holding a dataset gets a residue record (grace 0) first.
DJudge(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "d_judge" /\ NoFault
  /\ trash' = IF bytes[l] = "data" THEN [trash EXCEPT ![l] = [st |-> "expired", name |-> NameOf(op[p]), residue |-> TRUE, refused |-> FALSE]] ELSE trash
  /\ Goto(p, "d_dereg")
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, locals, mpc, mloc, hist>>

\* DeregisterTable BY NAME, the commit point; TableNotFound clears the residue and _drop lifts the fence; a row naming
\* another location than the one judged is InternalError, answered as TableBytesLeftError with nothing deleted.
DDereg(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "d_dereg" /\ NoFault
  /\ IF Named(n) = {}
       THEN /\ trash' = [trash EXCEPT ![l] = IF @.residue THEN NoRecord ELSE @]
            /\ Goto(p, "d_lift") /\ UNCHANGED <<rows, doorEnded, undropOk>>
       ELSE /\ rows' = rows \ Named(n)
            /\ doorEnded' = doorEnded \cup {l}
            /\ Retired(LocOf(n))
            /\ Goto(p, IF LocOf(n) = l THEN "d_delete" ELSE "done")
            /\ UNCHANGED trash
  /\ UNCHANGED <<bytes, claim, prot, grants, op, locals, mpc, mloc, acked, softDropped, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

\* _delete_everything_under; a delete that fails is TableBytesLeftError: the residue record and the claim stay.
DDelete(p) ==
  /\ pc[p] = "d_delete" /\ NoFault
  /\ DeleteBytes(lloc[p])
  /\ Goto(p, "d_clear")
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

DDeleteFails(p) ==
  /\ pc[p] = "d_delete" /\ Fault
  /\ Goto(p, "done")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* _clear_residue, then (in _drop) the claim's release, end_object_records, then drop_table's revoke_ownership.
DClear(p) ==
  /\ pc[p] = "d_clear" /\ NoFault
  /\ trash' = [trash EXCEPT ![lloc[p]] = IF @.residue THEN NoRecord ELSE @]
  /\ Goto(p, "d_release")
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, locals, mpc, mloc, hist>>

DRelease(p) ==
  /\ pc[p] = "d_release" /\ NoFault
  /\ Released(lloc[p], NameOf(op[p]))
  /\ Goto(p, "d_records")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

DRecords(p) ==
  /\ pc[p] = "d_records" /\ NoFault
  /\ prot' = prot \ {lloc[p]}
  /\ Goto(p, "d_revoke")
  /\ UNCHANGED <<rows, bytes, claim, trash, grants, op, locals, mpc, mloc, hist>>

DRevoke(p) ==
  /\ pc[p] = "d_revoke" /\ NoFault
  /\ grants' = grants \ {lloc[p]}
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, claim, trash, prot, op, locals, mpc, mloc, hist>>

\* _drop's `except Exception: lift_fence`, and the trash branch's lift once the row is gone.
DLift(p) ==
  /\ pc[p] \in {"d_lift", "s_lift"} /\ NoFault
  /\ claim' = [claim EXCEPT ![lloc[p]] = IF @.fence = p THEN [Bump(@) EXCEPT !.fence = None, !.decl = FALSE] ELSE @]
  /\ Goto(p, IF pc[p] = "s_lift" /\ ~Cascaded(op[p]) THEN "s_unprot" ELSE "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* Recoverable (_drop, `if trashed`): RECORD FIRST, DETACH SECOND, then lift the fence and clear protection.
SPut(p) ==
  /\ pc[p] = "s_put" /\ NoFault
  /\ trash' = [trash EXCEPT ![lloc[p]] = [st |-> "kept", name |-> NameOf(op[p]), residue |-> FALSE, refused |-> FALSE]]
  /\ Goto(p, "s_dereg")
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, locals, mpc, mloc, hist>>

\* DeregisterTable by name; the trash branch compares no location. TableNotFound lifts the fence and raises.
SDereg(p) ==
  LET n == NameOf(op[p]) IN
  /\ pc[p] = "s_dereg" /\ NoFault
  /\ IF Named(n) = {}
       THEN Goto(p, "d_lift") /\ UNCHANGED <<rows, doorEnded, softDropped, undropOk>>
       ELSE /\ rows' = rows \ Named(n)
            /\ doorEnded' = doorEnded \cup {lloc[p]}
            /\ softDropped' = softDropped \cup {LocOf(n)}
            /\ Retired(LocOf(n))
            /\ Goto(p, "s_lift")
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, acked, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

SUnprot(p) ==
  /\ pc[p] = "s_unprot" /\ NoFault
  /\ prot' = prot \ {lloc[p]}
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, claim, trash, grants, op, locals, mpc, mloc, hist>>

---------------------------------------------------------------------------
\* UNDROP (endpoints/tables.py undrop_table).

\* resolver.admitted_trash + the_one_to_restore + require_restorable: one record dropped under the name, inside its
\* grace and no residue; none answers 404, a record past its deadline or a residue record 409.
UFind(p) ==
  LET n == NameOf(op[p])
      kept == {l \in Locs : trash[l].st # "none" /\ trash[l].name = n} IN
  /\ pc[p] = "u_find" /\ NoFault
  /\ IF kept = {}
       THEN Goto(p, "done") /\ UNCHANGED lloc
       ELSE \E l \in kept :
              /\ lloc' = [lloc EXCEPT ![p] = l]
              /\ Goto(p, CASE Cardinality(kept) # 1 \/ trash[l].st # "kept" \/ trash[l].residue -> "done"
                            [] op[p] = "nsundrop_n1" -> "u_take"
                            [] OTHER -> "u_stand")
  /\ UNCHANGED <<store, op, lver, lx, lpath, mpc, mloc, hist>>

\* `standing = await resolver.table(segments)`: another table holding the name answers 409.
UStand(p) ==
  LET s == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "u_stand" /\ NoFault
  /\ Goto(p, IF s # None /\ s # lloc[p] THEN "done" ELSE "u_take")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* table_claims.fence_kept (UndropFence): one write under the ETag of the claim it reads, fencing the claim the drop kept
\* for this table, held by the name with the record's uuid; a fenced claim, a released one or one another name holds
\* answers 409 and writes nothing. Without UndropFence: the old table_claims.take, which read a claim this name holds as
\* its own retry and wrote nothing, so it held nothing against a door acting on the table meanwhile.
UTake(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "u_take" /\ NoFault
  /\ IF On("UndropFence")
       THEN IF FencedAgainst(l, p) \/ claim[l].st # "held" \/ claim[l].name # n
              THEN Goto(p, "done") /\ UNCHANGED claim
              ELSE Goto(p, "u_recheck") /\ claim' = [claim EXCEPT ![l] = [Bump(@) EXCEPT !.fence = p, !.decl = FALSE]]
       ELSE CASE FencedAgainst(l, p) -> Goto(p, "done") /\ UNCHANGED claim
              [] claim[l].st = "held" /\ claim[l].name = n -> Goto(p, "u_register") /\ UNCHANGED claim
              [] claim[l].st # "held" -> Goto(p, "done") /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> n, fence |-> None, decl |-> FALSE, ver |-> @.ver + 1]]
              [] OTHER -> Goto(p, "done") /\ UNCHANGED claim
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* The record read again under the fence: still there and no destroying drop's residue (the code compares its
\* `dropped_at` with the one admitted). A destroying drop that fenced before this undrop and died left its residue and
\* maybe deleted the bytes; then 409, the fence lifted. No drop files a record while the fence stands.
URecheck(p) ==
  /\ pc[p] = "u_recheck" /\ NoFault
  /\ Goto(p, IF trash[lloc[p]].st = "none" \/ trash[lloc[p]].residue THEN "u_lift" ELSE "u_register")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* Where the undrop goes once its table is registered: it clears the record under its fence (UndropHoldsFence), or lifts
\* the fence first.
URegistered == IF On("UndropHoldsFence") THEN "u_clear" ELSE "u_unfence"

\* RegisterTable(name, relative location). A TableAlreadyExists is the undrop's own success (a drop whose deregister
\* failed after its record left the table registered) only while the name describes this location
\* (UndropOwnConflict); else another table holds the name: 409, the record kept and the fence lifted.
URegister(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "u_register" /\ NoFault
  /\ IF NameCAS /\ Named(n) # {}
       THEN /\ UNCHANGED <<rows, softDropped>>
            /\ Goto(p, IF ~On("UndropOwnConflict") \/ LocOf(n) = l THEN URegistered ELSE "u_lift")
       ELSE rows' = rows \cup {<<n, l>>} /\ softDropped' = softDropped \ {l} /\ Goto(p, URegistered)
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

\* What an undrop answers 200 for is fixed at its last effect: a door acting on the table after it acts on it again.
Answered(p) == undropOk' = undropOk \cup {<<NameOf(op[p]), lloc[p]>>}
UNotAnswered == <<acked, doorEnded, softDropped, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

\* trash.clear(uuid): the record goes once the table is registered. No other door writes it while the fence stands.
UClear(p) ==
  /\ pc[p] = "u_clear" /\ NoFault
  /\ trash' = [trash EXCEPT ![lloc[p]] = NoRecord]
  /\ IF On("UndropHoldsFence") THEN Goto(p, "u_unfence") /\ UNCHANGED undropOk ELSE Goto(p, "done") /\ Answered(p)
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, locals, mpc, mloc>> /\ UNCHANGED UNotAnswered

\* The undrop lifts its fence: once restored (then it answers, or clears first without UndropHoldsFence), or refused.
UUnfence(p) ==
  /\ pc[p] \in {"u_unfence", "u_lift"} /\ NoFault
  /\ Unfenced(lloc[p], p)
  /\ CASE pc[p] = "u_lift" -> Goto(p, "done") /\ UNCHANGED undropOk
       [] On("UndropHoldsFence") -> Goto(p, "done") /\ Answered(p)
       [] OTHER -> Goto(p, "u_clear") /\ UNCHANGED undropOk
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc>> /\ UNCHANGED UNotAnswered

---------------------------------------------------------------------------
\* RENAME n1 -> n2 (endpoints/tables.py rename_table, dataplane.rename_table).

RResolve(p) ==
  LET l == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "r_resolve" /\ NoFault
  /\ IF l = None THEN Goto(p, "done") /\ UNCHANGED <<lloc, lver>>
     ELSE Goto(p, "r_protect") /\ lloc' = [lloc EXCEPT ![p] = l] /\ lver' = [lver EXCEPT ![p] = claim[l].ver]
  /\ UNCHANGED <<store, op, lx, lpath, mpc, mloc, hist>>

\* require_table_unprotected on the source, first.
RProtect(p) ==
  /\ pc[p] = "r_protect" /\ NoFault
  /\ Goto(p, IF lloc[p] \in prot THEN "done" ELSE "r_check")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* holds_a_version(source) else 404; the destination free in every form else 409.
RCheck(p) ==
  /\ pc[p] = "r_check" /\ NoFault
  /\ Goto(p, IF bytes[lloc[p]] # "data" \/ LocOf(Dest(op[p])) # None THEN "done" ELSE "r_hand")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* table_claims.hand_over: one write under the ETag read at the resolution, fenced for this request (A2).
RHand(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "r_hand" /\ NoFault
  /\ IF Fences /\ (claim[l].ver # lver[p] \/ FencedAgainst(l, p) \/ claim[l].name # NameOf(op[p]))
       THEN Goto(p, "done") /\ UNCHANGED claim
       ELSE Goto(p, "r_dereg") /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> Dest(op[p]), fence |-> p, decl |-> FALSE, ver |-> @.ver + 1]]
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* ns.deregister_table(source) by name; a failure hands the claim back.
RDereg(p) ==
  LET n == NameOf(op[p]) IN
  /\ pc[p] = "r_dereg" /\ NoFault
  /\ IF Named(n) = {}
       THEN Goto(p, "r_handback") /\ UNCHANGED <<rows, doorEnded, undropOk>>
       ELSE rows' = rows \ Named(n) /\ doorEnded' = doorEnded \cup {lloc[p]} /\ Retired(LocOf(n)) /\ Goto(p, "r_register")
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, acked, softDropped, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

\* ns.register_table(destination, location): put-if-not-exists (A1); a failure compensates.
RRegister(p) ==
  LET m == Dest(op[p]) IN
  /\ pc[p] = "r_register" /\ NoFault
  /\ IF NameCAS /\ Named(m) # {}
       THEN Goto(p, "r_comp") /\ UNCHANGED rows
       ELSE Goto(p, "r_lift") /\ rows' = rows \cup {<<m, lloc[p]>>}
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, hist>>

RRegisterFails(p) ==
  /\ pc[p] = "r_register" /\ Fault
  /\ Goto(p, "r_comp")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* The compensation: register the source again at its location; failing that the pointer is lost (logged
\* rename_source_pointer_lost) and the claim stays with the destination, fenced until it lapses.
RComp(p) ==
  LET n == NameOf(op[p]) IN
  /\ pc[p] = "r_comp" /\ NoFault
  /\ IF NameCAS /\ Named(n) # {}
       THEN Goto(p, "done") /\ UNCHANGED rows
       ELSE Goto(p, "r_handback") /\ rows' = rows \cup {<<n, lloc[p]>>}
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* _hand_back: take(previous=destination, token) writes the claim back to the source, unfenced.
RHandBack(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "r_handback" /\ NoFault
  /\ claim' = [claim EXCEPT ![l] = IF @.st = "held" /\ @.name = Dest(op[p]) THEN [st |-> "held", name |-> NameOf(op[p]), fence |-> None, decl |-> FALSE, ver |-> @.ver + 1] ELSE @]
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

RLift(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "r_lift" /\ NoFault
  /\ claim' = [claim EXCEPT ![l] = IF @.fence = p THEN [Bump(@) EXCEPT !.fence = None, !.decl = FALSE] ELSE @]
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

---------------------------------------------------------------------------
\* CASCADE: a namespace drop's table step. The cascade lists the table with its uuid, and _require_descendants_unprotected
\* reads protection for every listed table before any is fenced; a protected one answers 409 and nothing is touched.
KList(p) ==
  LET l == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "k_list" /\ NoFault
  /\ IF l = None \/ l \in prot THEN Goto(p, "done") /\ UNCHANGED lx ELSE Goto(p, "k_resolve") /\ lx' = [lx EXCEPT ![p] = l]
  /\ UNCHANGED <<store, op, lloc, lver, lpath, mpc, mloc, hist>>

\* table_claims.resolve, then fence_listed's judgement of it: a table that left since the listing is skipped, another
\* table under the name answers 409; else the drop's fence (d_fence), then, with CascadeRereadsProtection, protection
\* read again under it (d_reprot).
KResolve(p) ==
  LET l == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "k_resolve" /\ NoFault
  /\ IF l # lx[p] THEN Goto(p, "done") /\ UNCHANGED <<lloc, lver>>
     ELSE Goto(p, "d_fence") /\ lloc' = [lloc EXCEPT ![p] = l] /\ lver' = [lver EXCEPT ![p] = claim[l].ver]
  /\ UNCHANGED <<store, op, lx, lpath, mpc, mloc, hist>>

---------------------------------------------------------------------------
\* STOCK: a stock client creates n1 itself. DeclareTable claims a fresh location fenced for its declarer
\* (DeclarationFence: dataplane.declare_table with declarer=, table_claims.claim_new_table) and registers the name; the
\* client writes its rows with its own Lance on vended credentials (Lance's check for a dataset first); its first commit
\* reaches version/create, which fences the claim (StockFencesFirstVersion), finalizes version 1 put-if-not-exists and
\* lifts the fence. stockinto_n1 is another principal's stock client writing into the declaration it finds (DescribeTable
\* answering the declared location and a credential its write grant earns), its commit through the same door.
XProbe(p) ==
  LET ex == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "x_probe" /\ NoFault
  /\ CASE op[p] = "stockinto_n1" ->
            IF ex # None /\ bytes[ex] # "data" THEN Goto(p, "x_write") /\ lloc' = [lloc EXCEPT ![p] = ex] ELSE Goto(p, "done") /\ UNCHANGED lloc
       [] ex # None -> Goto(p, "done") /\ UNCHANGED lloc
       [] OTHER -> Goto(p, "x_claim") /\ lloc' = [lloc EXCEPT ![p] = Mint[p]]
  /\ UNCHANGED <<store, op, lver, lx, lpath, mpc, mloc, hist>>

\* The claim, fenced for its declarer (DeclarationFence), the grants and the reservation marker. The declaration is owed
\* to its declarer from here until its fence lapses, a drop takes it over, or the declarer writes it.
XClaim(p) ==
  /\ pc[p] = "x_claim" /\ NoFault
  /\ claim' = [claim EXCEPT ![lloc[p]] = [st |-> "held", name |-> NameOf(op[p]), fence |-> IF On("DeclarationFence") THEN p ELSE None,
                                          decl |-> On("DeclarationFence"), ver |-> @.ver + 1]]
  /\ grants' = grants \cup {lloc[p]}
  /\ bytes' = [bytes EXCEPT ![lloc[p]] = "marker"]
  /\ owed' = owed \cup {lloc[p]}
  /\ Goto(p, "x_register")
  /\ UNCHANGED <<rows, trash, prot, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, intruded, cred>>

\* RegisterTable: a name taken since refuses it (A1), and the declare undoes what it made. Registered, the declare
\* answers with the write credential it vends (_vended_declaration), right after the fence its claim was written with, for
\* at most the fence's LEASE: bounded.
XRegister(p) ==
  LET n == NameOf(op[p]) IN
  /\ pc[p] = "x_register" /\ NoFault
  /\ IF NameCAS /\ Named(n) # {}
       THEN /\ Goto(p, "done") /\ UNCHANGED <<rows, cred>>
            /\ bytes' = [bytes EXCEPT ![lloc[p]] = Deleted(@)] /\ Released(lloc[p], n) /\ grants' = grants \ {lloc[p]}
            /\ owed' = owed \ {lloc[p]}
       ELSE /\ Goto(p, "x_write") /\ rows' = rows \cup {<<n, lloc[p]>>} /\ UNCHANGED <<bytes, claim, grants, owed>>
            /\ cred' = [cred EXCEPT ![lloc[p]] = "bounded"]
  /\ UNCHANGED <<trash, prot, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, intruded>>

\* The declarer asks for its declaration again while it writes, once per behaviour: its DeclareTable sent again (its
\* answer lost), or its client's credential refresh (DescribeTable). With RedeclareRenews the door renews its own
\* declaration's fence, one write under the ETag it read (table_claims.renew_declaration), and vends after it for at most
\* the fence's LEASE: bounded. A claim that no longer carries the declarer's live declaration refuses the renewal, and
\* nothing is vended (the declare answers as a first declare of the name would now, the describe without a credential).
\* Without the guard the door answers the declaration as it stands, with a credential of the full TTL vended under a fence
\* written earlier: unbounded, so it can outlive the fence.
XAsk(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "x_write" /\ op[p] = "stock_n1" /\ lpath[p] # "asked" /\ NoFault
  /\ lpath' = [lpath EXCEPT ![p] = "asked"]
  /\ IF claim[l].st = "held" /\ claim[l].decl /\ claim[l].fence = p
       THEN IF On("RedeclareRenews")
              THEN claim' = [claim EXCEPT ![l] = Bump(@)] /\ cred' = [cred EXCEPT ![l] = "bounded"]
              ELSE cred' = [cred EXCEPT ![l] = "unbounded"] /\ UNCHANGED claim
       ELSE UNCHANGED <<claim, cred>>
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, pc, lloc, lver, lx, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded>>

\* The client's Lance refuses a location that already holds a dataset, then writes and stages its manifest. Its own
\* process, outside the catalog: it can take longer than LEASE (LongSteps).
XWrite(p) ==
  /\ pc[p] = "x_write" /\ NoFault
  /\ Goto(p, IF bytes[lloc[p]] = "data" THEN "done" ELSE "x_resolve")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* version/create's door resolves the name and stages the manifest: a name that left the location answers 409
\* (TableChanged) or 404, a version already there 409 (VersionTaken).
XResolve(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "x_resolve" /\ NoFault
  /\ Goto(p, IF LocOf(NameOf(op[p])) # l \/ bytes[l] = "data" THEN "done" ELSE "x_fence")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* DoorTable.first_version_fenced (StockFencesFirstVersion): one write under the ETag of the claim it reads, refused
\* while another request fences the claim, a declaration's fence of another principal included (DeclarerLifts); the
\* declarer's own is taken over. Refused: 409 ConcurrentModification, nothing finalized. Without the guard the door writes
\* nothing before the finalize. The claim is judged as the fence reads it, not against the one the door's resolution
\* read: a check that it is still that one was measured redundant here (ADR 0227 §9), every request that acts on the
\* claim meanwhile leaving its fence, a version (XPut's put-if-not-exists) or a name that no longer describes the
\* location (XPut's DoorTable.require_named).
XFence(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "x_fence" /\ NoFault
  /\ IF claim[l].st # "held"
       THEN Goto(p, "done") /\ UNCHANGED <<claim, owed, intruded, cred>>
       ELSE IF On("StockFencesFirstVersion")
              THEN IF Against(l, p)
                     THEN Goto(p, "done") /\ UNCHANGED <<claim, owed, intruded, cred>>
                     ELSE Goto(p, "x_put") /\ claim' = [claim EXCEPT ![l] = [Bump(@) EXCEPT !.fence = p, !.decl = FALSE]] /\ Owes(l, p)
              ELSE Goto(p, "x_put") /\ UNCHANGED claim /\ Owes(l, p)
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk>>

\* The finalize: the name still describes the location (DoorTable.require_named), then version 1 put-if-not-exists; a
\* name that left the location, or a version finalized since, refuses it (409) and the client is told nothing landed.
XPut(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "x_put" /\ NoFault
  /\ IF bytes[l] = "data" \/ LocOf(NameOf(op[p])) # l
       THEN Goto(p, "x_lift") /\ UNCHANGED bytes
       ELSE Goto(p, "x_unfence") /\ bytes' = [bytes EXCEPT ![l] = "data"]
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* The fence lifted (version/create's `finally`), then the answer: 200 once version 1 landed.
XLift(p) ==
  /\ pc[p] \in {"x_unfence", "x_lift"} /\ NoFault
  /\ Unfenced(lloc[p], p)
  /\ acked' = IF pc[p] = "x_unfence" THEN acked \cup {lloc[p]} ELSE acked
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

---------------------------------------------------------------------------
\* PROTECT (set_table_protection): resolve the name and its claim, write the record, then judge the claim
\* (ProtectChecksFence). The record is written FIRST and the claim read AFTER, as a drop writes its fence first and
\* reads protection after (DropRereadsProtection): of the two, one reads what the other wrote.

PResolve(p) ==
  LET l == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "p_resolve" /\ NoFault
  /\ IF l = None THEN Goto(p, "done") /\ UNCHANGED <<lloc, lver>>
     ELSE Goto(p, "p_set") /\ lloc' = [lloc EXCEPT ![p] = l] /\ lver' = [lver EXCEPT ![p] = claim[l].ver]
  /\ UNCHANGED <<store, op, lx, lpath, mpc, mloc, hist>>

PSet(p) ==
  /\ pc[p] = "p_set" /\ NoFault
  /\ prot' = prot \cup {lloc[p]}
  /\ IF On("ProtectChecksFence") THEN Goto(p, "p_check") /\ UNCHANGED protOk ELSE Goto(p, "done") /\ protOk' = protOk \cup {lloc[p]}
  /\ UNCHANGED <<rows, bytes, claim, trash, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, owed, intruded, cred>>

\* DoorTable.require_named and the claim's fence: the name still describes the location under the claim the door
\* resolved (its ETag), and no other request fences that claim; else 409 ConcurrentModification, the record left (it
\* protects; the caller retries).
ProtCheck(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "p_check" /\ NoFault
  /\ IF LocOf(NameOf(op[p])) # l \/ claim[l].ver # lver[p] \/ claim[l].st # "held" \/ (FencedAgainst(l, p) /\ ~claim[l].decl)
       THEN UNCHANGED protOk
       ELSE protOk' = protOk \cup {l}
  /\ Goto(p, "done")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, owed, intruded, cred>>

---------------------------------------------------------------------------
\* A caller's process dies between two effects. What it wrote stays; its fence lapses (A5).
Dies(p) ==
  /\ pc[p] \notin {"start", "done", "dead"} /\ Fault
  /\ Goto(p, "dead")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

---------------------------------------------------------------------------
\* Time and maintenance.

\* The grace period ends.
Deadline(l) ==
  /\ trash[l].st = "kept" /\ NoFault
  /\ trash' = [trash EXCEPT ![l].st = "expired"]
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, pc, locals, mpc, mloc, hist>>

\* The claim's holder resolves to the place: the object stands again (trash_reclaim endpoint _standing_refusal).
Standing(l) == claim[l].st = "held" /\ <<claim[l].name, l>> \in rows

\* release_trash: RELEASE_SETTLE past the deadline (A3) and not standing; revoke the grants, stamp the record. A
\* refusal is noted on the record.
Release(l) ==
  /\ trash[l].st = "expired" /\ ~trash[l].refused /\ NoFault
  /\ SettleOutlastsRequests => ~RequestInFlight(l)
  /\ IF Standing(l)
       THEN trash' = [trash EXCEPT ![l].refused = TRUE] /\ UNCHANGED grants
       ELSE trash' = [trash EXCEPT ![l].st = "released"] /\ grants' = grants \ {l}
  /\ UNCHANGED <<rows, bytes, claim, prot, op, pc, locals, mpc, mloc, hist>>

\* A fence lapses LEASE after its last write: once its request has ended, or, with LiveLapse, while its request runs a
\* step that can outlast LEASE (A5). A declaration's fence is aged from the declare, and the declarer's own write can
\* outlast it, so with LiveLapse it lapses wherever its declarer is; its declaration is then owed to no one.
Lapse(l) ==
  /\ claim[l].fence # None /\ NoFault
  /\ pc[claim[l].fence] \in {"done", "dead"} \/ (LiveLapse /\ (pc[claim[l].fence] \in LongSteps \/ claim[l].decl))
  /\ claim' = [claim EXCEPT ![l] = [Bump(@) EXCEPT !.fence = None, !.decl = FALSE]]
  /\ owed' = IF claim[l].decl THEN owed \ {l} ELSE owed
  \* A credential vended right after the declaration's fence was written expires with it; one vended under a fence
  \* written earlier is still live.
  /\ cred' = IF claim[l].decl THEN [cred EXCEPT ![l] = CASE @ = "bounded" -> "none" [] @ = "unbounded" -> "stale" [] OTHER -> @] ELSE cred
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, pc, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, intruded>>

\* Without DeclarationFence there is no fence to lapse, and the declaration's LEASE runs out all the same: history only.
Expire(l) ==
  /\ ~On("DeclarationFence") /\ l \in owed /\ NoFault
  /\ \E q \in Procs : op[q] = "stock_n1" /\ lloc[q] = l /\ (pc[q] \in {"done", "dead"} \/ LiveLapse)
  /\ owed' = owed \ {l}
  /\ UNCHANGED <<store, op, pc, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, intruded, cred>>

\* A credential the store issued for longer than its fence expires at its own time, before or after the fence lapses.
CredExpires(l) ==
  /\ cred[l] \in {"unbounded", "stale"} /\ NoFault
  /\ cred' = [cred EXCEPT ![l] = "none"]
  /\ UNCHANGED <<store, op, pc, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive, overwritten, protOk, owed, intruded>>

\* The purge (purge._purge_one), one released record at a time. A refusal is noted and the purge goes back to idle.
Refuse(l) == trash' = [trash EXCEPT ![l].refused = TRUE] /\ mpc' = "idle" /\ mloc' = None

PPick(l) ==
  /\ mpc = "idle" /\ trash[l].st = "released" /\ ~trash[l].refused /\ NoFault
  /\ mpc' = "check" /\ mloc' = l
  /\ UNCHANGED <<store, op, pc, locals, hist>>

\* check() against the tick's snapshot, and _recovered_since_the_snapshot at the last instant: still_registered.
PCheck ==
  /\ mpc \in {"check", "recheck"} /\ NoFault
  /\ IF Live(mloc) THEN Refuse(mloc) ELSE (mpc' = IF mpc = "check" THEN "recheck" ELSE "vend") /\ UNCHANGED <<trash, mloc>>
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, pc, locals, hist>>

\* vend_reclaim_credentials: vend_refusal (a claim recording the record's uuid, released) and _standing_refusal.
PVend ==
  /\ mpc = "vend" /\ NoFault
  /\ IF claim[mloc].st = "none" \/ Standing(mloc) \/ trash[mloc].st # "released"
       THEN Refuse(mloc)
       ELSE (mpc' = "delete") /\ UNCHANGED <<trash, mloc>>
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, pc, locals, hist>>

\* _delete_bytes_or_refuse, under the vended session; a failed delete is retried on a later tick.
PDelete ==
  /\ mpc = "delete" /\ NoFault
  /\ DeleteBytes(mloc)
  /\ purgeHitLive' = (purgeHitLive \/ (Live(mloc) /\ bytes[mloc] = "data"))
  /\ mpc' = "end"
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, pc, locals, mloc, acked, doorEnded, softDropped, undropOk, purged, overwritten, protOk, owed, intruded, cred>>

PDeleteFails ==
  /\ mpc = "delete" /\ Fault
  /\ mpc' = "idle" /\ mloc' = None
  /\ UNCHANGED <<store, op, pc, locals, hist>>

\* end_trash: end_refusal (released, its place empty) and _standing_refusal; then trash.end: the records under the
\* uuid, the claim, the record last; revoke_removed_object takes what grants remain.
PEnd ==
  LET l == mloc IN
  /\ mpc = "end" /\ NoFault
  /\ IF bytes[l] = "data" \/ Standing(l) \/ trash[l].st # "released"
       THEN Refuse(l) /\ UNCHANGED <<grants, prot, claim, purged>>
       ELSE /\ grants' = grants \ {l} /\ prot' = prot \ {l}
            /\ claim' = [claim EXCEPT ![l] = IF @.st = "held" /\ @.name = trash[l].name THEN [Bump(@) EXCEPT !.st = "released", !.fence = None, !.decl = FALSE] ELSE @]
            /\ trash' = [trash EXCEPT ![l] = NoRecord]
            /\ purged' = purged \cup {l}
            /\ mpc' = "idle" /\ mloc' = None
  /\ UNCHANGED <<rows, bytes, op, pc, locals, acked, doorEnded, softDropped, undropOk, protHit, purgeHitLive, overwritten, protOk, owed, intruded, cred>>

---------------------------------------------------------------------------
Step(p) ==
  \/ Start(p)
  \/ CProbe(p) \/ CTake(p) \/ CClaim(p) \/ CGrant(p) \/ CReserve(p) \/ CRegister(p) \/ CUnreserve(p) \/ CTakeBack(p)
  \/ CWrite(p) \/ CCommit(p) \/ CRollbackDrop(p) \/ CRollbackDelete(p) \/ CRollbackRelease(p) \/ CAnswer(p)
  \/ CRecheck(p) \/ CLift(p) \/ CPut(p) \/ KList(p) \/ KResolve(p)
  \/ XProbe(p) \/ XClaim(p) \/ XRegister(p) \/ XAsk(p) \/ XWrite(p) \/ XResolve(p) \/ XFence(p) \/ XPut(p) \/ XLift(p)
  \/ DResolve(p) \/ DProtect(p) \/ DFence(p) \/ DReprotect(p) \/ DJudge(p) \/ DDereg(p) \/ DDelete(p) \/ DClear(p) \/ DRelease(p)
  \/ DRecords(p) \/ DRevoke(p) \/ DLift(p) \/ SPut(p) \/ SDereg(p) \/ SUnprot(p)
  \/ UFind(p) \/ UStand(p) \/ UTake(p) \/ URecheck(p) \/ URegister(p) \/ UClear(p) \/ UUnfence(p)
  \/ RResolve(p) \/ RProtect(p) \/ RCheck(p) \/ RHand(p) \/ RDereg(p) \/ RRegister(p) \/ RComp(p) \/ RHandBack(p) \/ RLift(p)
  \/ PResolve(p) \/ PSet(p) \/ ProtCheck(p)

Maintain ==
  \/ \E l \in Locs : Deadline(l) \/ Release(l) \/ Lapse(l) \/ PPick(l)
  \/ PCheck \/ PVend \/ PDelete \/ PEnd

Fails ==
  \E p \in Procs : CRegisterFails(p) \/ CWriteFails(p) \/ DDeleteFails(p) \/ RRegisterFails(p) \/ Dies(p)
  \/ PDeleteFails

Next == (\E p \in Procs : Step(p)) \/ Maintain \/ Fails \/ (\E l \in Locs : Expire(l) \/ CredExpires(l))

Spec == Init /\ [][Next]_vars /\ \A p \in Procs : WF_vars(Step(p)) /\ WF_vars(Maintain)

---------------------------------------------------------------------------
\* What must always hold.

\* A name that lists as a table has its bytes: no row names a location whose data was deleted
\* (purge.check STILL_REGISTERED, destroy_table's row-before-bytes order).
LiveHasData == \A r \in rows : bytes[r[2]] # "deleted"

\* A name is never live twice: of two creates of one name one wins the name (RegisterTable put-if-not-exists).
NameLiveOnce == \A n \in Names : Cardinality(Named(n)) <= 1

\* One location is never named by two tables (location_claims, [[LH-204]]).
OneNamePerLocation == \A l \in Locs : Cardinality({r \in rows : r[2] = l}) <= 1

\* A table whose protection answered 200 is never destroyed (require_table_unprotected; `force` not modelled).
ProtectedNeverDestroyed == ~protHit

\* The purge never deletes bytes a live row names (purge._recovered_since_the_snapshot).
PurgeNeverRemovesLive == ~purgeHitLive

\* A create that answered 200 made a table that stands, with its data, until a door the owner called removes it
\* (a drop or rename door that resolved that very table).
AnsweredCreateStands == \A l \in acked : l \in doorEnded \/ (Live(l) /\ bytes[l] = "data")

\* Of the creates that write one table, one commits: a Create never lands over rows another create committed, which
\* that create answers 200 for (CreateMode.CREATE: a written table is the create's conflict).
OneCreateCommitsPerTable == overwritten = {}

\* A declaration is its declarer's (owner, 2026-11-17): until its LEASE runs out, or a door removing the table ends it,
\* no request of another principal takes it to write version 1, by version/create or by the catalog's create.
DeclarationIsItsDeclarers == intruded = {}

\* No credential vended for a declaration is live after the declaration's fence lapsed while another principal's request
\* holds the claim: a write credential lasts no longer than the declaration it was vended for (zero trust), so a create of
\* the name by another principal after the lapse writes where no credential of the lapsed declarer still reaches.
DeclaredCredentialDiesWithItsFence == \A l \in Locs : cred[l] = "stale" => claim[l].fence = None \/ Principal(claim[l].fence) = "A"

\* A dropped table can be undropped until it is purged: a recoverable drop leaves its record, and the record
\* goes only with an undrop that registered the table again or with the purge.
RecoverableUntilPurged == \A l \in softDropped : trash[l].st # "none" \/ l \in purged

\* An undrop that answered 200 restored exactly what was dropped: the table of that uuid, at its location, under the
\* name, with its data and its grants, until a door acts on it again.
UndropRestoresExactly == \A u \in undropOk : u \in rows /\ bytes[u[2]] = "data" /\ u[2] \in grants

\* What must eventually hold once failures stop: every request has ended, every fence is lifted, and every trash
\* record is ended or carries a noted refusal, which the purge reports. Whatever else a run leaves (bytes no row
\* names, a row over no bytes, grants on no table) is a category of reconcile's report (unregistered_datasets,
\* absent_datasets, ghost_tables, ungoverned_tables), so nothing at rest is unseen.
Quiet ==
  /\ \A p \in Procs : pc[p] \in {"done", "dead"}
  /\ mpc = "idle"
  /\ \A l \in Locs : claim[l].fence = None
  /\ \A l \in Locs : trash[l].st = "none" \/ trash[l].refused
EverythingComesToRest == <>[]Quiet
=============================================================================
