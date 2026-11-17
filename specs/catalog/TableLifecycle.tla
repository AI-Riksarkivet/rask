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
(*   A4. A Lance create of a dataset is a conditional commit: of two       *)
(*       creates at one location one commits and the other fails          *)
(*       ("Dataset already exists", dataplane._write_blob). Atomic here.   *)
(*   A5. A fence whose request has ended lapses after LEASE (Lapse), and   *)
(*       no request outlives its LEASE. LEASE is otherwise not modelled:   *)
(*       a claim held under another name refuses every take and fence      *)
(*       here, where the code lets one whose holder is gone be taken past  *)
(*       its lease (location_claims._taken, holder_is_gone).              *)
(*                                                                         *)
(* FINDINGS, each a config whose first line reads FINDING (ADR 0227):     *)
(*   TableLifecycleCreateRace, TableLifecycleUndropRace,                   *)
(*   TableLifecycleUndropClearsRedrop, TableLifecycleUndropOverDestroyed,  *)
(*   TableLifecycleSoftDropWrongRow, TableLifecycleProtectRace. The gate   *)
(*   configs leave out the mixes each needs, and say so.                   *)
(*                                                                         *)
(* NOT MODELLED: namespaces and cascades; deregister and register as       *)
(* doors of their own; Overwrite and ExistOk creates; idempotency keys;    *)
(* the announcements; base records, data directories and clone endings;    *)
(* `force`, which turns protection off on purpose; a lease's clock; a      *)
(* purge refusal retried on a later tick (a noted refusal stays noted).    *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Ops1, Ops2, Ops3,   \* the operations each of the three callers may run, one per behaviour
          InitialTable,       \* whether name n1 starts as a written table at L0
          MaxFaults,          \* how many effects may fail or processes die in one behaviour
          NameCAS,            \* A1: RegisterTable is put-if-not-exists on the name
          Fences,             \* A2: a door's fence and a rename's hand-over are conditional and respected
          SettleOutlastsRequests \* A3: the release never lands while a request acting on the table still runs

Procs  == {"p1", "p2", "p3"}
Names  == {"n1", "n2"}
Locs   == {"L0", "L1", "L2", "L3"}
None   == "-"
\* The location (and uuid) a create by each caller mints.
Mint   == [p \in Procs |-> CASE p = "p1" -> "L1" [] p = "p2" -> "L2" [] p = "p3" -> "L3"]

\* An operation is "<door>_<name>" or "rename_<from>_<to>".
AllOps == {"none", "create_n1", "create_n2", "drop_n1", "softdrop_n1", "undrop_n1", "rename_n1_n2", "protect_n1"}
Door(o)   == CASE o = "none" -> "none" [] o \in {"create_n1", "create_n2"} -> "create"
                 [] o = "drop_n1" -> "drop" [] o = "softdrop_n1" -> "softdrop" [] o = "undrop_n1" -> "undrop"
                 [] o = "rename_n1_n2" -> "rename" [] o = "protect_n1" -> "protect"
NameOf(o) == IF o = "create_n2" THEN "n2" ELSE "n1"
Dest(o)   == "n2"

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
          purgeHitLive \* the purge deleted bytes a row named

store  == <<rows, bytes, claim, trash, prot, grants>>
locals == <<lloc, lver, lx, lpath>>
hist   == <<acked, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive>>
vars   == <<store, op, pc, locals, mpc, mloc, faults, hist>>

NoClaim  == [st |-> "none", name |-> None, fence |-> None, ver |-> 0]
NoRecord == [st |-> "none", name |-> None, residue |-> FALSE, refused |-> FALSE]

Init ==
  /\ op \in {f \in [Procs -> AllOps] : f["p1"] \in Ops1 /\ f["p2"] \in Ops2 /\ f["p3"] \in Ops3}
  /\ pc = [p \in Procs |-> "start"]
  /\ rows = IF InitialTable THEN {<<"n1", "L0">>} ELSE {}
  /\ bytes = [l \in Locs |-> IF InitialTable /\ l = "L0" THEN "data" ELSE "none"]
  /\ claim = [l \in Locs |-> IF InitialTable /\ l = "L0" THEN [st |-> "held", name |-> "n1", fence |-> None, ver |-> 1] ELSE NoClaim]
  /\ trash = [l \in Locs |-> NoRecord]
  /\ prot \in IF InitialTable THEN {{}, {"L0"}} ELSE {{}}
  /\ grants = IF InitialTable THEN {"L0"} ELSE {}
  /\ lloc = [p \in Procs |-> None] /\ lver = [p \in Procs |-> 0] /\ lx = [p \in Procs |-> None] /\ lpath = [p \in Procs |-> None]
  /\ mpc = "idle" /\ mloc = None
  /\ faults = 0
  /\ acked = IF InitialTable THEN {"L0"} ELSE {}
  /\ doorEnded = {} /\ softDropped = {} /\ undropOk = {} /\ purged = {} /\ protHit = FALSE /\ purgeHitLive = FALSE

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

\* A fence another request holds: the claim refuses this request's fence, hand-over and take.
FencedAgainst(l, p) == Fences /\ claim[l].st = "held" /\ claim[l].fence \notin {None, p}

\* Deleting what a location holds: data becomes deleted data, a marker nothing.
Deleted(b) == IF b = "data" \/ b = "deleted" THEN "deleted" ELSE "none"
DeleteBytes(l) ==
  /\ bytes' = [bytes EXCEPT ![l] = Deleted(@)]
  /\ protHit' = (protHit \/ (bytes[l] = "data" /\ l \in prot))

\* A door removed the row of `l` by its own act; what was answered of it no longer holds.
Retired(l) == undropOk' = {u \in undropOk : u[2] # l}

\* location_claims.release(location, name[, uuid]): only a claim this name holds is released.
Released(l, n) == claim' = [claim EXCEPT ![l] = IF @.st = "held" /\ @.name = n THEN [Bump(@) EXCEPT !.st = "released", !.fence = None] ELSE @]

\* A request acting on the table at `l` is in flight from its resolution (an undrop's: its admission) to its end.
RequestInFlight(l) == \E p \in Procs : lloc[p] = l /\ pc[p] \notin {"start", "done", "dead"}

Start(p) ==
  /\ pc[p] = "start" /\ NoFault
  /\ pc' = [pc EXCEPT ![p] = CASE Door(op[p]) = "none" -> "done" [] Door(op[p]) = "create" -> "c_probe"
                                [] Door(op[p]) \in {"drop", "softdrop"} -> "d_resolve" [] Door(op[p]) = "undrop" -> "u_find"
                                [] Door(op[p]) = "rename" -> "r_resolve" [] Door(op[p]) = "protect" -> "p_resolve"]
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

\* Declared path: table_claims.take(existing, holder) before _write_blob_into. A claim this name holds is the take's
\* retry; a released claim is written again without a uuid, which _claimed_uuid then refuses inside the write's try, so
\* the write's rollback runs; a fenced claim, or one another name holds, refuses before the try.
CTake(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "c_take" /\ NoFault
  /\ CASE FencedAgainst(l, p) -> Goto(p, "done") /\ UNCHANGED claim
       [] claim[l].st = "held" /\ claim[l].name = n -> Goto(p, "c_write") /\ UNCHANGED claim
       [] claim[l].st # "held" -> Goto(p, "c_rb_drop") /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> n, fence |-> None, ver |-> @.ver + 1]]
       [] OTHER -> Goto(p, "done") /\ UNCHANGED claim
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* Fresh path, dataplane.declare_table: claim_new_table on the minted location (nobody else names it), then
\* before_register (fga_deps.GrantsBeforeWrite), then _reserve, then RegisterTable.
CClaim(p) ==
  /\ pc[p] = "c_claim" /\ NoFault
  /\ claim' = [claim EXCEPT ![lloc[p]] = [st |-> "held", name |-> NameOf(op[p]), fence |-> None, ver |-> @.ver + 1]]
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

\* _write_blob(mode="create"): Lance's create commits only where no dataset is (A4).
CWrite(p) ==
  /\ pc[p] = "c_write" /\ NoFault
  /\ IF bytes[lloc[p]] = "data"
       THEN Goto(p, "c_rb_drop") /\ UNCHANGED bytes
       ELSE Goto(p, "c_answer") /\ bytes' = [bytes EXCEPT ![lloc[p]] = "data"]
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, hist>>

CWriteFails(p) ==
  /\ pc[p] = "c_write" /\ Fault
  /\ Goto(p, "c_rb_drop")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* _write_blob_into's rollback: `ns.drop_table(DropTableRequest(id=segments))`, the backend's DropTable BY NAME: it
\* removes whatever row the name has now, then deletes that row's directory
\* (lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md, DropTable, V2). TableNotFound is suppressed.
CRollbackDrop(p) ==
  LET n == NameOf(op[p]) IN
  /\ pc[p] = "c_rb_drop" /\ NoFault
  /\ IF Named(n) = {}
       THEN Goto(p, "c_rb_release") /\ UNCHANGED <<rows, lx>>
       ELSE /\ rows' = rows \ Named(n)
            /\ lx' = [lx EXCEPT ![p] = LocOf(n)]
            /\ Goto(p, "c_rb_delete")
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, lloc, lver, lpath, mpc, mloc, hist>>

CRollbackDelete(p) ==
  /\ pc[p] = "c_rb_delete" /\ NoFault
  /\ DeleteBytes(lx[p])
  /\ Goto(p, "c_rb_release")
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, purgeHitLive>>

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
  /\ UNCHANGED <<store, op, locals, mpc, mloc, doorEnded, softDropped, undropOk, purged, protHit, purgeHitLive>>

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

\* table_claims.fence -> location_claims.fence: one write under the ETag the resolution read (A2).
DFence(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "d_fence" /\ NoFault
  /\ IF Fences /\ (claim[l].ver # lver[p] \/ FencedAgainst(l, p) \/ (claim[l].st = "held" /\ claim[l].name # n))
       THEN Goto(p, "done") /\ UNCHANGED claim
       ELSE /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> n, fence |-> p, ver |-> @.ver + 1]]
            /\ Goto(p, IF Door(op[p]) = "drop" THEN "d_judge" ELSE "s_put")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

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
  /\ UNCHANGED <<bytes, claim, prot, grants, op, locals, mpc, mloc, acked, softDropped, purged, protHit, purgeHitLive>>

\* _delete_everything_under; a delete that fails is TableBytesLeftError: the residue record and the claim stay.
DDelete(p) ==
  /\ pc[p] = "d_delete" /\ NoFault
  /\ DeleteBytes(lloc[p])
  /\ Goto(p, "d_clear")
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, softDropped, undropOk, purged, purgeHitLive>>

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
  /\ claim' = [claim EXCEPT ![lloc[p]] = IF @.fence = p THEN [Bump(@) EXCEPT !.fence = None] ELSE @]
  /\ Goto(p, IF pc[p] = "s_lift" THEN "s_unprot" ELSE "done")
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
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, acked, purged, protHit, purgeHitLive>>

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
              /\ Goto(p, IF Cardinality(kept) = 1 /\ trash[l].st = "kept" /\ ~trash[l].residue THEN "u_stand" ELSE "done")
  /\ UNCHANGED <<store, op, lver, lx, lpath, mpc, mloc, hist>>

\* `standing = await resolver.table(segments)`: another table holding the name answers 409.
UStand(p) ==
  LET s == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "u_stand" /\ NoFault
  /\ Goto(p, IF s # None /\ s # lloc[p] THEN "done" ELSE "u_take")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, hist>>

\* table_claims.take(location, name): the drop kept the claim for this table, so a claim this name holds is the take's
\* retry; a fenced claim or one another name holds refuses; a released one is written again with no uuid, and the door
\* then answers InternalError.
UTake(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "u_take" /\ NoFault
  /\ CASE FencedAgainst(l, p) -> Goto(p, "done") /\ UNCHANGED claim
       [] claim[l].st = "held" /\ claim[l].name = n -> Goto(p, "u_register") /\ UNCHANGED claim
       [] claim[l].st # "held" -> Goto(p, "done") /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> n, fence |-> None, ver |-> @.ver + 1]]
       [] OTHER -> Goto(p, "done") /\ UNCHANGED claim
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* RegisterTable(name, relative location); `except TableAlreadyExistsError:` is read as "already recovered" and the
\* door goes on to clear the record.
URegister(p) ==
  LET n == NameOf(op[p]) l == lloc[p] IN
  /\ pc[p] = "u_register" /\ NoFault
  /\ IF NameCAS /\ Named(n) # {}
       THEN UNCHANGED <<rows, softDropped>>
       ELSE rows' = rows \cup {<<n, l>>} /\ softDropped' = softDropped \ {l}
  /\ Goto(p, "u_clear")
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, acked, doorEnded, undropOk, purged, protHit, purgeHitLive>>

\* trash.clear(uuid): the record goes once the table is registered.
UClear(p) ==
  /\ pc[p] = "u_clear" /\ NoFault
  /\ trash' = [trash EXCEPT ![lloc[p]] = NoRecord]
  /\ Goto(p, "u_answer")
  /\ UNCHANGED <<rows, bytes, claim, prot, grants, op, locals, mpc, mloc, hist>>

UAnswer(p) ==
  /\ pc[p] = "u_answer" /\ NoFault
  /\ undropOk' = undropOk \cup {<<NameOf(op[p]), lloc[p]>>}
  /\ Goto(p, "done")
  /\ UNCHANGED <<store, op, locals, mpc, mloc, acked, doorEnded, softDropped, purged, protHit, purgeHitLive>>

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
       ELSE Goto(p, "r_dereg") /\ claim' = [claim EXCEPT ![l] = [st |-> "held", name |-> Dest(op[p]), fence |-> p, ver |-> @.ver + 1]]
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

\* ns.deregister_table(source) by name; a failure hands the claim back.
RDereg(p) ==
  LET n == NameOf(op[p]) IN
  /\ pc[p] = "r_dereg" /\ NoFault
  /\ IF Named(n) = {}
       THEN Goto(p, "r_handback") /\ UNCHANGED <<rows, doorEnded, undropOk>>
       ELSE rows' = rows \ Named(n) /\ doorEnded' = doorEnded \cup {lloc[p]} /\ Retired(LocOf(n)) /\ Goto(p, "r_register")
  /\ UNCHANGED <<bytes, claim, trash, prot, grants, op, locals, mpc, mloc, acked, softDropped, purged, protHit, purgeHitLive>>

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
  /\ claim' = [claim EXCEPT ![l] = IF @.st = "held" /\ @.name = Dest(op[p]) THEN [st |-> "held", name |-> NameOf(op[p]), fence |-> None, ver |-> @.ver + 1] ELSE @]
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

RLift(p) ==
  LET l == lloc[p] IN
  /\ pc[p] = "r_lift" /\ NoFault
  /\ claim' = [claim EXCEPT ![l] = IF @.fence = p THEN [Bump(@) EXCEPT !.fence = None] ELSE @]
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, locals, mpc, mloc, hist>>

---------------------------------------------------------------------------
\* PROTECT (set_table_protection): resolve the name, write the record. No fence, no re-read.

PResolve(p) ==
  LET l == LocOf(NameOf(op[p])) IN
  /\ pc[p] = "p_resolve" /\ NoFault
  /\ IF l = None THEN Goto(p, "done") /\ UNCHANGED lloc ELSE Goto(p, "p_set") /\ lloc' = [lloc EXCEPT ![p] = l]
  /\ UNCHANGED <<store, op, lver, lx, lpath, mpc, mloc, hist>>

PSet(p) ==
  /\ pc[p] = "p_set" /\ NoFault
  /\ prot' = prot \cup {lloc[p]}
  /\ Goto(p, "done")
  /\ UNCHANGED <<rows, bytes, claim, trash, grants, op, locals, mpc, mloc, hist>>

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

\* A fence whose request has ended lapses after LEASE.
Lapse(l) ==
  /\ claim[l].fence # None /\ pc[claim[l].fence] \in {"done", "dead"} /\ NoFault
  /\ claim' = [claim EXCEPT ![l] = [Bump(@) EXCEPT !.fence = None]]
  /\ UNCHANGED <<rows, bytes, trash, prot, grants, op, pc, locals, mpc, mloc, hist>>

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
  /\ UNCHANGED <<rows, claim, trash, prot, grants, op, pc, locals, mloc, acked, doorEnded, softDropped, undropOk, purged>>

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
            /\ claim' = [claim EXCEPT ![l] = IF @.st = "held" /\ @.name = trash[l].name THEN [Bump(@) EXCEPT !.st = "released", !.fence = None] ELSE @]
            /\ trash' = [trash EXCEPT ![l] = NoRecord]
            /\ purged' = purged \cup {l}
            /\ mpc' = "idle" /\ mloc' = None
  /\ UNCHANGED <<rows, bytes, op, pc, locals, acked, doorEnded, softDropped, undropOk, protHit, purgeHitLive>>

---------------------------------------------------------------------------
Step(p) ==
  \/ Start(p)
  \/ CProbe(p) \/ CTake(p) \/ CClaim(p) \/ CGrant(p) \/ CReserve(p) \/ CRegister(p) \/ CUnreserve(p) \/ CTakeBack(p)
  \/ CWrite(p) \/ CRollbackDrop(p) \/ CRollbackDelete(p) \/ CRollbackRelease(p) \/ CAnswer(p)
  \/ DResolve(p) \/ DProtect(p) \/ DFence(p) \/ DJudge(p) \/ DDereg(p) \/ DDelete(p) \/ DClear(p) \/ DRelease(p)
  \/ DRecords(p) \/ DRevoke(p) \/ DLift(p) \/ SPut(p) \/ SDereg(p) \/ SUnprot(p)
  \/ UFind(p) \/ UStand(p) \/ UTake(p) \/ URegister(p) \/ UClear(p) \/ UAnswer(p)
  \/ RResolve(p) \/ RProtect(p) \/ RCheck(p) \/ RHand(p) \/ RDereg(p) \/ RRegister(p) \/ RComp(p) \/ RHandBack(p) \/ RLift(p)
  \/ PResolve(p) \/ PSet(p)

Maintain ==
  \/ \E l \in Locs : Deadline(l) \/ Release(l) \/ Lapse(l) \/ PPick(l)
  \/ PCheck \/ PVend \/ PDelete \/ PEnd

Fails ==
  \E p \in Procs : CRegisterFails(p) \/ CWriteFails(p) \/ DDeleteFails(p) \/ RRegisterFails(p) \/ Dies(p)
  \/ PDeleteFails

Next == (\E p \in Procs : Step(p)) \/ Maintain \/ Fails

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

\* A protected table is never destroyed (require_table_unprotected; `force` not modelled).
ProtectedNeverDestroyed == ~protHit

\* The purge never deletes bytes a live row names (purge._recovered_since_the_snapshot).
PurgeNeverRemovesLive == ~purgeHitLive

\* A create that answered 200 made a table that stands, with its data, until a door the owner called removes it
\* (a drop or rename door that resolved that very table).
AnsweredCreateStands == \A l \in acked : l \in doorEnded \/ (Live(l) /\ bytes[l] = "data")

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
