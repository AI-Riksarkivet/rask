------------------------------- MODULE Cascade -------------------------------
(***************************************************************************)
(* A namespace dropped with what is under it, and a warehouse deleted with  *)
(* its namespaces, while other doors create, undrop and purge beneath them.*)
(*                                                                         *)
(* The `dir` backend drops a namespace Restrict whatever `behavior` says,  *)
(* so the catalog cascades itself, one effect at a time, and nothing       *)
(* fences the subtree as a whole. The objects: warehouse W (its record on  *)
(* the control root and its row in __manifest), namespace P in W, C in P,  *)
(* table T in C, and table N, which another caller creates (or renames in) *)
(* in P. The doors, and the functions each action cites:                   *)
(*                                                                         *)
(*   drop     endpoints/namespaces.py drop_namespace with behavior=Cascade:*)
(*            _collect_descendants, then _trash_subtree (each table:       *)
(*            fence_listed, trash.put, deregister_table; each namespace,   *)
(*            deepest first: describe, drop_namespace, trash.put) or       *)
(*            _destroy_subtree (each table: destroy_table; each namespace: *)
(*            drop_namespace, an absent one tolerated)                     *)
(*   create   a create (or a rename in) of N in P: one __manifest add      *)
(*   undrop   namespaces.py undrop_namespace (each namespace shallowest    *)
(*            first: create_or_keep_namespace, trash.clear; each table:    *)
(*            table_claims.take, register_table, trash.clear), and the     *)
(*            table door's undrop of T (endpoints/tables.py undrop_table)  *)
(*   purge    maintenance's purge of an expired trashed table, which       *)
(*            refuses one still registered (purge.check STILL_REGISTERED)  *)
(*            and one outside the maintained estate                        *)
(*   wh       endpoints/warehouses.py delete_warehouse with ?cascade=true: *)
(*            trash_owned_by and each record ended, _namespaces_in, each   *)
(*            table detached (_detach_table, its bytes kept on purpose),   *)
(*            each namespace dropped, the warehouse's row, its record last *)
(*                                                                         *)
(* A request that fails or dies is issued again (every door documents its  *)
(* re-issue as the recovery). A refusal of a non-empty namespace is        *)
(* re-issued too.                                                          *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. A child's add to __manifest commits only while its parent's row   *)
(*       stands, checked in the same commit. THIS DOES NOT HOLD:           *)
(*       lance_docs/ns_catalog/catalog/dir/index.md "Manifest Table        *)
(*       Commits" promises only that two adds of one entry conflict, and   *)
(*       on the installed pylance an add sent beside its parent's Restrict *)
(*       drop and the drop both land (50 of 50 races of a namespace, 30 of *)
(*       30 of a declared table). ParentAtomic = FALSE is the backend as   *)
(*       measured; the gates hold the doors to the rest with it TRUE, the  *)
(*       behaviour a fix gives, and CascadeParentNotChecked is the finding.*)
(*   A2. drop_namespace refuses a namespace that holds a row (Restrict:    *)
(*       spec.yaml DropNamespaceRequest.behavior, "the namespace should    *)
(*       not contain any table or child namespace"; namespaces.py 393 and  *)
(*       615-620 measured the `dir` backend). CascadeNoRestrict removes    *)
(*       it, and a child created after the listing outlives its parent.    *)
(*   A3. The purge refuses a table that is registered again (purge.check   *)
(*       STILL_REGISTERED, measured in the lifecycle model's               *)
(*       PurgeNeverRemovesLive). CascadeNoPurgeCheck removes it: a cascade *)
(*       that dies between a record and its detach leaves a record on a    *)
(*       live table, and the purge deletes its bytes (LiveHasData).        *)
(*                                                                         *)
(* FINDINGS, each a config whose first line reads FINDING:                 *)
(*   CascadeParentNotChecked: A1 above.                                    *)
(*   CascadeNamespaceRecordAfterDrop, CascadeRootRecordAfterDrop: a        *)
(*   recoverable cascade drops each namespace before it files its record;  *)
(*   one that ends between the two leaves the tables beneath it in the     *)
(*   trash with no way back, and one that ends at P cannot be finished.    *)
(*   CascadeUndropNotResumable: the namespace undrop clears the            *)
(*   namespaces' records before it registers the tables, so a re-issue     *)
(*   after a failure answers 404.                                          *)
(*   CascadeWarehouseLateTrash: a recoverable drop landing while a         *)
(*   warehouse is deleted files trash the delete never ends, for a         *)
(*   warehouse that no longer exists.                                      *)
(*   CascadeUndropClearsRedrop: ADR 0227's UndropClearsRedrop, reached     *)
(*   through a cascade's re-issue.                                         *)
(*                                                                         *)
(* NOT MODELLED: grants and their revoke (the recoverable cascade keeps    *)
(* them, the destroying one ends them after its drops); protection and    *)
(* `force`; claims and fences (TableLifecycle.tla), beyond fence_listed's  *)
(* skip of a table that left; the announcements; projects (a project       *)
(* holds no rows, and its delete is recorded on it once its emptiness is  *)
(* judged, after which no door makes a warehouse under it);               *)
(* ?purge_bucket; a namespace undropped by hand under a new uuid.         *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Recoverable,   \* the cascade drop keeps its subtree in the trash (a grace period, no purge)
          Creates,       \* another caller creates (or renames in) table N in P
          UndropsNs,     \* the namespace undrop of P runs once the drop has filed P's record
          UndropsT,      \* the table door's undrop of T
          Purges,        \* maintenance's purge, once the trash has expired
          WhDeletes,     \* the warehouse is deleted with ?cascade=true
          MaxFaults,
          FaultsIn,      \* the doors that may fail or die: a subset of {"drop", "undrop", "wh"}
          DropDiesAt,    \* where the cascade may end: a subset of {"table", "detach", "nsfile"}
          ParentAtomic,  \* A1
          Restrict,      \* A2
          PurgeChecks    \* A3

Tables == {"T", "N"}
Nss    == {"P", "C"}
Parent == [t \in Tables |-> IF t = "T" THEN "C" ELSE "P"]
Anc    == [t \in Tables |-> IF t = "T" THEN {"C", "P"} ELSE {"P"}]

VARIABLES whRec,     \* the warehouse's record on the control root: routing reads it, and the purge's estate
          whRow,     \* the warehouse's own row in __manifest
          nsRow,     \* each namespace's row
          tRow,      \* each table's row
          bytes,     \* each table's data: "none", "data", "deleted"
          trashT,    \* each table's trash record: "none", "cascade" (filed by P's cascade), "own" (its own door's)
          trashNs,   \* namespaces with a trash record of P's cascade
          expired,   \* the trash's grace period has ended
          dpc, dtabs, dns, dcur,   \* the drop of P, what it listed, the namespace it is on
          cpc,       \* the create of N
          upc, uStarted,           \* the undrop of P
          tpc,       \* the undrop of T
          wpc, wtrash, wtabs,      \* the warehouse delete, the trash it read, the tables it listed
          whDetached,              \* tables the warehouse delete detached on purpose, their bytes kept
          faults

vars == <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, expired, dpc, dtabs, dns, dcur, cpc, upc, uStarted, tpc, wpc, wtrash, wtabs,
          whDetached, faults>>

Init ==
  /\ whRec = TRUE /\ whRow = TRUE
  /\ nsRow = [n \in Nss |-> TRUE]
  /\ tRow = [t \in Tables |-> t = "T"]
  /\ bytes = [t \in Tables |-> IF t = "T" THEN "data" ELSE "none"]
  /\ trashT = [t \in Tables |-> "none"] /\ trashNs = {} /\ expired = FALSE
  /\ dpc = "start" /\ dtabs = {} /\ dns = {} /\ dcur = "C"
  /\ cpc = IF Creates THEN "start" ELSE "done"
  /\ upc = (IF UndropsNs THEN "wait" ELSE "done") /\ uStarted = FALSE
  /\ tpc = IF UndropsT THEN "start" ELSE "done"
  /\ wpc = (IF WhDeletes THEN "start" ELSE "done") /\ wtrash = {} /\ wtabs = {}
  /\ whDetached = {}
  /\ faults = 0

Fault   == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults

\* Rows directly in each container.
Children(n) == {t \in Tables : tRow[t] /\ Parent[t] = n} \cup (IF n = "P" /\ nsRow["C"] THEN {"C"} ELSE {})
Empty(n) == Children(n) = {}
WhEmpty == ~nsRow["P"]

---------------------------------------------------------------------------
\* THE CASCADE DROP OF P.

DOthers == UNCHANGED <<whRec, whRow, expired, cpc, upc, uStarted, tpc, wpc, wtrash, wtabs, whDetached>>

\* identify, then _collect_descendants: a namespace that is not there answers 404 (DropMode Fail).
DStart ==
  /\ dpc = "start" /\ NoFault
  /\ IF nsRow["P"]
       THEN /\ dtabs' = {t \in Tables : tRow[t]} /\ dns' = IF nsRow["C"] THEN {"C"} ELSE {}
            /\ dpc' = "table"
       ELSE UNCHANGED <<dtabs, dns>> /\ dpc' = "404"
  /\ UNCHANGED <<nsRow, tRow, bytes, trashT, trashNs, dcur>> /\ DOthers

\* One listed table, under fence_listed: one that left since the listing is skipped. Recoverable: RECORD FIRST
\* (trash.put), DETACH SECOND (deregister_table), as _trash_listed_table does. Destroying: dataplane.destroy_table.
DFile(t) ==
  /\ dpc = "table" /\ t \in dtabs /\ NoFault
  /\ IF ~tRow[t] THEN UNCHANGED <<tRow, bytes, trashT>> /\ dpc' = "table"
     ELSE IF Recoverable
            THEN trashT' = [trashT EXCEPT ![t] = "cascade"] /\ dpc' = "detach" /\ UNCHANGED <<tRow, bytes>>
            ELSE tRow' = [tRow EXCEPT ![t] = FALSE] /\ bytes' = [bytes EXCEPT ![t] = "deleted"] /\ dpc' = "table" /\ UNCHANGED trashT
  /\ dcur' = t
  /\ dtabs' = dtabs \ {t}
  /\ UNCHANGED <<nsRow, trashNs, dns>> /\ DOthers

DDetach ==
  /\ dpc = "detach" /\ NoFault
  /\ tRow' = [tRow EXCEPT ![dcur] = FALSE]
  /\ dpc' = "table"
  /\ UNCHANGED <<nsRow, bytes, trashT, trashNs, dtabs, dns, dcur>> /\ DOthers

\* Each namespace deepest first, then P: drop_namespace, Restrict (A2); a NotEmpty refusal ends the request.
\* Recoverable: the record is filed AFTER the drop (_trash_subtree: describe, drop_namespace, trash.put).
\* Destroying: an absent namespace is tolerated.
DNsDrop ==
  LET n == IF dns # {} THEN "C" ELSE "P" IN
  /\ dpc = "table" /\ dtabs = {} /\ NoFault
  /\ IF ~nsRow[n]
       THEN UNCHANGED nsRow /\ dpc' = CASE Recoverable -> "fail" [] n = "P" -> "ok" [] OTHER -> "table"
       ELSE IF Restrict /\ ~Empty(n)
              THEN dpc' = "refused" /\ UNCHANGED nsRow
              ELSE nsRow' = [nsRow EXCEPT ![n] = FALSE] /\ dpc' = CASE Recoverable -> "nsfile" [] n = "P" -> "ok" [] OTHER -> "table"
  /\ dcur' = n
  /\ dns' = IF Recoverable THEN dns ELSE dns \ {n}
  /\ UNCHANGED <<tRow, bytes, trashT, trashNs, dtabs>> /\ DOthers

DNsFile ==
  /\ dpc = "nsfile" /\ NoFault
  /\ trashNs' = trashNs \cup {dcur}
  /\ dns' = dns \ {dcur}
  /\ dpc' = IF dcur = "P" THEN "ok" ELSE "table"
  /\ UNCHANGED <<nsRow, tRow, bytes, trashT, dtabs, dcur>> /\ DOthers

\* The request dies, or a step fails and it answers 5xx.
DDies ==
  /\ "drop" \in FaultsIn /\ dpc \in DropDiesAt /\ Fault
  /\ dpc' = "dead"
  /\ UNCHANGED <<nsRow, tRow, bytes, trashT, trashNs, dtabs, dns, dcur>> /\ DOthers

\* The caller issues it again.
DRetry ==
  /\ dpc \in {"dead", "refused", "fail"} /\ NoFault
  /\ dpc' = "start" /\ dtabs' = {} /\ dns' = {}
  /\ UNCHANGED <<nsRow, tRow, bytes, trashT, trashNs, dcur>> /\ DOthers

---------------------------------------------------------------------------
\* THE CREATE (or rename in) OF N in P: the __manifest add checks the parent's row in the same commit (A1).

COthers == UNCHANGED <<whRec, whRow, nsRow, trashT, trashNs, expired, dpc, dtabs, dns, dcur, upc, uStarted, tpc, wpc, wtrash, wtabs, whDetached>>

CCheck ==
  /\ cpc = "start" /\ NoFault
  /\ IF nsRow["P"] /\ whRec /\ ~tRow["N"]
       THEN IF ParentAtomic
              THEN tRow' = [tRow EXCEPT !["N"] = TRUE] /\ bytes' = [bytes EXCEPT !["N"] = "data"] /\ cpc' = "done"
              ELSE UNCHANGED <<tRow, bytes>> /\ cpc' = "add"
       ELSE UNCHANGED <<tRow, bytes>> /\ cpc' = "done"
  /\ COthers

CAdd ==
  /\ cpc = "add" /\ NoFault
  /\ tRow' = [tRow EXCEPT !["N"] = TRUE] /\ bytes' = [bytes EXCEPT !["N"] = "data"] /\ cpc' = "done"
  /\ COthers

---------------------------------------------------------------------------
\* THE NAMESPACE UNDROP OF P (undrop_namespace), once P's record is filed.

UOthers == UNCHANGED <<whRec, whRow, bytes, expired, dpc, dtabs, dns, dcur, cpc, tpc, wpc, wtrash, wtabs, whDetached>>

\* admitted_trash: a record for P and no namespace holding the name; 404 otherwise (a re-issue too).
UStart ==
  /\ upc \in {"wait", "start"} /\ NoFault
  /\ IF "P" \in trashNs /\ ~nsRow["P"] /\ whRec
       THEN upc' = "nsP" /\ uStarted' = TRUE
       ELSE IF upc = "start" THEN upc' = "404" /\ UNCHANGED uStarted ELSE FALSE
  /\ UNCHANGED <<nsRow, tRow, trashT, trashNs>> /\ UOthers

\* Each namespace record shallowest first: create_or_keep_namespace, then trash.clear right after it.
UNs(n, here, next) ==
  /\ upc = here /\ NoFault
  /\ IF n \in trashNs
       THEN IF (n = "P" /\ whRow) \/ (n = "C" /\ nsRow["P"])
              THEN nsRow' = [nsRow EXCEPT ![n] = TRUE] /\ trashNs' = trashNs \ {n} /\ upc' = next
              ELSE upc' = "dead" /\ UNCHANGED <<nsRow, trashNs>>
       ELSE upc' = next /\ UNCHANGED <<nsRow, trashNs>>
  /\ UNCHANGED <<tRow, trashT, uStarted>> /\ UOthers

\* Each table the cascade filed: register_table (an already-registered one is taken as recovered), then trash.clear.
UTable(t, here) ==
  /\ upc = here /\ NoFault
  /\ IF trashT[t] = "cascade"
       THEN IF nsRow[Parent[t]]
              THEN tRow' = [tRow EXCEPT ![t] = TRUE] /\ trashT' = [trashT EXCEPT ![t] = "none"]
                   /\ upc' = (IF t = "T" THEN "tN" ELSE "ok")
              ELSE upc' = "dead" /\ UNCHANGED <<tRow, trashT>>
       ELSE upc' = (IF t = "T" THEN "tN" ELSE "ok") /\ UNCHANGED <<tRow, trashT>>
  /\ UNCHANGED <<nsRow, trashNs, uStarted>> /\ UOthers

UDies ==
  /\ "undrop" \in FaultsIn /\ upc \in {"nsP", "nsC", "tT", "tN"} /\ Fault
  /\ upc' = "dead"
  /\ UNCHANGED <<nsRow, tRow, trashT, trashNs, uStarted>> /\ UOthers

URetry ==
  /\ upc = "dead" /\ NoFault
  /\ upc' = "start"
  /\ UNCHANGED <<nsRow, tRow, trashT, trashNs, uStarted>> /\ UOthers

---------------------------------------------------------------------------
\* THE TABLE DOOR'S UNDROP OF T: its record, its own namespace standing (require_its_own_parent), register, clear.

TOthers == UNCHANGED <<whRec, whRow, nsRow, bytes, trashNs, expired, dpc, dtabs, dns, dcur, cpc, upc, uStarted, wpc, wtrash, wtabs, whDetached>>

TUndrop ==
  /\ tpc = "start" /\ NoFault
  /\ IF trashT["T"] # "none" /\ ~tRow["T"] /\ nsRow["C"] /\ whRec
       THEN tRow' = [tRow EXCEPT !["T"] = TRUE] /\ tpc' = "clear"
       ELSE UNCHANGED tRow /\ tpc' = "done"
  /\ UNCHANGED trashT /\ TOthers

TClear ==
  /\ tpc = "clear" /\ NoFault
  /\ trashT' = [trashT EXCEPT !["T"] = "none"] /\ tpc' = "done"
  /\ UNCHANGED tRow /\ TOthers

---------------------------------------------------------------------------
\* MAINTENANCE: the trash's deadline passes; the purge deletes an expired table's bytes and ends its record, within the
\* maintained estate (a warehouse whose record stands) and never for a registered table (A3).

MOthers == UNCHANGED <<whRec, whRow, nsRow, tRow, trashNs, dpc, dtabs, dns, dcur, cpc, upc, uStarted, tpc, wpc, wtrash, wtabs, whDetached>>

Expire ==
  /\ Purges /\ ~expired /\ \E t \in Tables : trashT[t] # "none"
  /\ NoFault
  /\ expired' = TRUE
  /\ UNCHANGED <<bytes, trashT>> /\ MOthers

Purge(t) ==
  /\ expired /\ trashT[t] # "none" /\ whRec /\ NoFault
  /\ (PurgeChecks => ~tRow[t])
  /\ bytes' = [bytes EXCEPT ![t] = IF @ = "data" THEN "deleted" ELSE @]
  /\ trashT' = [trashT EXCEPT ![t] = "none"]
  /\ UNCHANGED expired /\ MOthers

---------------------------------------------------------------------------
\* THE WAREHOUSE DELETE with ?cascade=true and without ?purge_bucket.

WOthers == UNCHANGED <<expired, dpc, dtabs, dns, dcur, cpc, upc, uStarted, tpc>>

\* trash_owned_by: the warehouse's trash, read once.
WStart ==
  /\ wpc = "start" /\ NoFault
  /\ IF whRec
       THEN wtrash' = {t \in Tables : trashT[t] # "none"} \cup trashNs /\ wpc' = "end"
       ELSE UNCHANGED wtrash /\ wpc' = "404"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtabs, whDetached>> /\ WOthers

\* Each record it read is ended (_forget_trashed: trash.end; bytes kept without ?purge_bucket).
WEnd ==
  /\ wpc = "end" /\ NoFault
  /\ trashT' = [t \in Tables |-> IF t \in wtrash THEN "none" ELSE trashT[t]]
  /\ trashNs' = trashNs \ wtrash
  /\ wpc' = "list"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, wtrash, wtabs, whDetached>> /\ WOthers

\* _namespaces_in and _cascade_subtrees: what stands under the warehouse now.
WList ==
  /\ wpc = "list" /\ NoFault
  /\ wtabs' = {t \in Tables : tRow[t]}
  /\ wpc' = "detach"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, whDetached>> /\ WOthers

\* _detach_table: each listed table that still stands is deregistered, its bytes left where a register can attach them.
WDetach ==
  /\ wpc = "detach" /\ NoFault
  /\ tRow' = [t \in Tables |-> IF t \in wtabs THEN FALSE ELSE tRow[t]]
  /\ whDetached' = whDetached \cup {t \in wtabs : tRow[t]}
  /\ wpc' = "nsC"
  /\ UNCHANGED <<whRec, whRow, nsRow, bytes, trashT, trashNs, wtrash, wtabs>> /\ WOthers

\* Each namespace deepest first, an absent one tolerated, a non-empty one refused (a partial 503), then the warehouse's row.
WNs(n, here, next) ==
  /\ wpc = here /\ NoFault
  /\ IF nsRow[n] /\ Restrict /\ ~Empty(n)
       THEN wpc' = "dead" /\ UNCHANGED nsRow
       ELSE nsRow' = [nsRow EXCEPT ![n] = FALSE] /\ wpc' = next
  /\ UNCHANGED <<whRec, whRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

WRow ==
  /\ wpc = "row" /\ NoFault
  /\ IF Restrict /\ ~WhEmpty THEN wpc' = "dead" /\ UNCHANGED whRow ELSE whRow' = FALSE /\ wpc' = "record"
  /\ UNCHANGED <<whRec, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

\* The record goes last.
WRecord ==
  /\ wpc = "record" /\ NoFault
  /\ whRec' = FALSE /\ wpc' = "ok"
  /\ UNCHANGED <<whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

WDies ==
  /\ "wh" \in FaultsIn /\ wpc \in {"end", "list", "detach", "nsC", "nsP", "row", "record"} /\ Fault
  /\ wpc' = "dead"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

WRetry ==
  /\ wpc = "dead" /\ NoFault
  /\ wpc' = "start"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

---------------------------------------------------------------------------
Work ==
  \/ DStart \/ (\E t \in Tables : DFile(t)) \/ DDetach \/ DNsDrop \/ DNsFile \/ DRetry
  \/ CCheck \/ CAdd
  \/ UStart \/ UNs("P", "nsP", "nsC") \/ UNs("C", "nsC", "tT") \/ UTable("T", "tT") \/ UTable("N", "tN") \/ URetry
  \/ TUndrop \/ TClear
  \/ Expire \/ \E t \in Tables : Purge(t)
  \/ WStart \/ WEnd \/ WList \/ WDetach \/ WNs("C", "nsC", "nsP") \/ WNs("P", "nsP", "row") \/ WRow \/ WRecord \/ WRetry

Next == Work \/ DDies \/ UDies \/ WDies

Spec == Init /\ [][Next]_vars /\ WF_vars(Work)

---------------------------------------------------------------------------
\* What must always hold.

\* No row outlives its parent: a table's namespace, a namespace's parent, the warehouse's row and record.
NoOrphan ==
  /\ \A t \in Tables : tRow[t] => nsRow[Parent[t]]
  /\ nsRow["C"] => nsRow["P"]
  /\ nsRow["P"] => whRow /\ whRec

\* A registered table's data is there.
LiveHasData == \A t \in Tables : tRow[t] => bytes[t] = "data"

AtRest == /\ dpc \in {"ok", "404", "dead", "refused", "fail"} /\ cpc = "done" /\ upc \in {"wait", "ok", "404", "dead", "done"}
          /\ tpc = "done" /\ wpc \in {"ok", "404", "dead", "done"}

\* An undrop reaches a trashed table: its record stands, the warehouse is routed to, and each namespace above it stands
\* or has a record the namespace undrop rebuilds it from.
Restorable(t) == trashT[t] # "none" /\ whRec /\ \A a \in Anc[t] : nsRow[a] \/ a \in trashNs

\* Whenever no request is in flight, every table's data is registered, restorable, deleted by the purge or a destroy,
\* or left by the warehouse delete on purpose (its bytes kept for a register): never lost.
NothingIsLost == AtRest => \A t \in Tables : bytes[t] = "data" => (tRow[t] \/ Restorable(t) \/ t \in whDetached)

\* No trash record outlives its warehouse: the delete ends the warehouse's trash before its record goes.
NoTrashOutlivesItsWarehouse == (AtRest /\ ~whRec) => (trashNs = {} /\ \A t \in Tables : trashT[t] = "none")

\* What must eventually hold.

\* A cascade that died or was refused part-way is finished by its re-issue: it answers 200, or 404 once the warehouse
\* delete, the one other door that drops P, has dropped it.
TheCascadeFinishes == <>(dpc = "ok" \/ (dpc = "404" /\ wpc = "ok"))
\* An undrop once admitted is finished by its re-issue: every table the cascade filed is registered again.
TheUndropFinishes == uStarted ~> (upc = "ok")
=============================================================================
