------------------------------- MODULE Cascade -------------------------------
(***************************************************************************)
(* A namespace dropped with what is under it, and a warehouse deleted with *)
(* its namespaces, while other doors create, undrop and purge beneath them. *)
(*                                                                         *)
(* The `dir` backend drops a namespace Restrict whatever `behavior` says,  *)
(* so the catalog cascades itself, one effect at a time. The objects:      *)
(* warehouse W (its record on the control root and its row in __manifest), *)
(* namespace P in W, C in P, table T in C, and table N, which another      *)
(* caller creates (or renames in) in P. The doors, and the functions each  *)
(* action cites:                                                           *)
(*                                                                         *)
(*   drop     endpoints/namespaces.py drop_namespace with behavior=Cascade: *)
(*            _collect_descendants, then _trash_subtree (each table:       *)
(*            fence_listed, trash.put, deregister_table; each namespace,   *)
(*            deepest first: describe, trash.put, drop_namespace) or       *)
(*            _destroy_subtree (each table: destroy_table; each namespace: *)
(*            drop_namespace, an absent one tolerated)                     *)
(*   create   a create (or a rename in) of N in P: one __manifest add      *)
(*   undrop   namespaces.py undrop_namespace (each namespace shallowest    *)
(*            first: create_or_keep_namespace; each table through the     *)
(*            table undrop's own step, tables.restore_kept_table:          *)
(*            fence_kept, the record read again, register_table,           *)
(*            trash.clear, the fence lifted; then each namespace's         *)
(*            trash.clear, deepest first, its own last), and the table     *)
(*            door's undrop of T (endpoints/tables.py undrop_table, the    *)
(*            same step)                                                   *)
(*   purge    maintenance's purge of an expired trashed table, which       *)
(*            refuses one still registered (purge.check STILL_REGISTERED)  *)
(*            and one outside the maintained estate                        *)
(*   wh       endpoints/warehouses.py delete_warehouse with ?cascade=true: *)
(*            the warehouse's namespace fence held for a drop, then        *)
(*            trash_owned_by and each record ended, _namespaces_in, each   *)
(*            table detached (_detach_table, its bytes kept on purpose),   *)
(*            each namespace dropped, the warehouse's row, its record last *)
(*                                                                         *)
(* A request that fails or dies is issued again (every door documents its  *)
(* re-issue as the recovery). A refusal of a non-empty namespace, or one a *)
(* fence answers 409, is re-issued too.                                    *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. The `dir` backend checks a new row's parent, and a Restrict       *)
(*       drop's emptiness, each against the __manifest it read:            *)
(*       lance_docs/ns_catalog/catalog/dir/index.md "Manifest Table        *)
(*       Commits" promises only that two adds of one entry conflict, and   *)
(*       on pylance 13.0.0 an add sent beside its parent's Restrict drop   *)
(*       and the drop both land (50 of 50 races of a namespace, 30 of 30   *)
(*       of a declared table). Sent one after the other, from two          *)
(*       connections, each reads the other's commit and refuses (measured  *)
(*       on pylance 13.0.0). ParentAtomic = FALSE is the backend as        *)
(*       measured, and every gate runs on it.                              *)
(*   A2. drop_namespace refuses a namespace that holds a row (Restrict:    *)
(*       spec.yaml DropNamespaceRequest.behavior, "the namespace should    *)
(*       not contain any table or child namespace"). CascadeNoRestrict     *)
(*       removes it, and a child created after the listing outlives its    *)
(*       parent.                                                           *)
(*   A3. The purge refuses a table that is registered again (purge.check   *)
(*       STILL_REGISTERED, measured in the lifecycle model's               *)
(*       PurgeNeverRemovesLive). CascadeNoPurgeCheck removes it: a cascade *)
(*       that dies between a record and its detach leaves a record on a    *)
(*       live table, and the purge deletes its bytes (LiveHasData).        *)
(*                                                                         *)
(* THE GUARDS the fixes of ADR 0233 add, each removed by a config that     *)
(* must fail:                                                              *)
(*   G1. A row added under a namespace and that namespace's drop exclude   *)
(*       each other: every add holds its parent's namespace fence for an   *)
(*       add, every drop its own for a drop (lakehouse.namespace_fences,   *)
(*       taken by catalog.core.fenced_namespace at the backend's seam).    *)
(*       CascadeParentNotChecked removes it (NoOrphan).                    *)
(*   G2. The recoverable cascade files a namespace's record before its     *)
(*       drop, as it files a table's before its detach. CascadeNamespace-  *)
(*       RecordAfterDrop (NothingIsLost) and CascadeRootRecordAfterDrop    *)
(*       (TheCascadeFinishes) remove it.                                   *)
(*   G3. The namespace undrop clears the namespaces' records only once     *)
(*       every table is registered, deepest first and its own last, so a   *)
(*       re-issue finds them. CascadeUndropNotResumable removes it.        *)
(*   G4. A trash record is filed under a warehouse only while it holds     *)
(*       the warehouse's namespace fence for an add (trash.put), and the   *)
(*       warehouse delete holds it for a drop from before it reads the     *)
(*       trash until its record is gone. CascadeWarehouseLateTrash removes *)
(*       it (NoTrashOutlivesItsWarehouse).                                 *)
(*   G5. The table undrop's step holds the table's claim fenced from       *)
(*       before its register until its record is cleared (ADR 0227's       *)
(*       UndropFence and UndropHoldsFence), so a cascade's fence_listed    *)
(*       of the table answers 409 while it runs, and the cascade's         *)
(*       re-issue files its record only once the undrop's clear is done.   *)
(*       CascadeUndropClearsRedrop removes it (NothingIsLost).             *)
(* The undrop's own adds take the fence too; each is one step here.        *)
(*                                                                         *)
(* NOT MODELLED: grants and their revoke (the recoverable cascade keeps    *)
(* them, the destroying one ends them after its drops); protection and     *)
(* `force`; claims and fences (TableLifecycle.tla), beyond fence_listed's  *)
(* skip of a table that left; the announcements; projects (a project       *)
(* holds no rows, and its delete is recorded on it once its emptiness is   *)
(* judged, after which no door makes a warehouse under it);                *)
(* ?purge_bucket; a namespace undropped by hand under a new uuid; a fence  *)
(* a dead holder keeps until its hold lapses (here it lapses at once).     *)
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
          DropDiesAt,    \* where the cascade may end: a subset of {"table", "detach", "nsdrop", "nsfile"}
          ParentAtomic,  \* A1: TRUE would be a backend that checks the parent inside the add's commit
          Restrict,      \* A2
          PurgeChecks,   \* A3
          NsFence,       \* G1
          RecordFirst,   \* G2
          ClearsLast,    \* G3
          WhFence,       \* G4
          UndropFenced   \* G5

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
          addHeld,   \* the create of N holds P's namespace fence for an add (G1)
          upc, uStarted,           \* the undrop of P
          tpc,       \* the undrop of T
          wpc, wtrash, wtabs,      \* the warehouse delete, the trash it read, the tables it listed
          whHeld,                  \* the warehouse delete holds the warehouse's namespace fence for a drop (G4)
          whDetached,              \* tables the warehouse delete detached or ended on purpose, their bytes kept
          faults

vars == <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, expired, dpc, dtabs, dns, dcur, cpc, addHeld, upc, uStarted, tpc, wpc, wtrash,
          wtabs, whHeld, whDetached, faults>>

Init ==
  /\ whRec = TRUE /\ whRow = TRUE
  /\ nsRow = [n \in Nss |-> TRUE]
  /\ tRow = [t \in Tables |-> t = "T"]
  /\ bytes = [t \in Tables |-> IF t = "T" THEN "data" ELSE "none"]
  /\ trashT = [t \in Tables |-> "none"] /\ trashNs = {} /\ expired = FALSE
  /\ dpc = "start" /\ dtabs = {} /\ dns = {} /\ dcur = "C"
  /\ cpc = IF Creates THEN "start" ELSE "done"
  /\ addHeld = FALSE
  /\ upc = (IF UndropsNs THEN "wait" ELSE "done") /\ uStarted = FALSE
  /\ tpc = IF UndropsT THEN "start" ELSE "done"
  /\ wpc = (IF WhDeletes THEN "start" ELSE "done") /\ wtrash = {} /\ wtabs = {}
  /\ whHeld = FALSE /\ whDetached = {}
  /\ faults = 0

Fault   == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults

\* Rows directly in each container.
Children(n) == {t \in Tables : tRow[t] /\ Parent[t] = n} \cup (IF n = "P" /\ nsRow["C"] THEN {"C"} ELSE {})
Empty(n) == Children(n) = {}
WhEmpty == ~nsRow["P"]

\* A trash record filed under W is refused while the warehouse delete holds W's fence (G4): trash.put answers 409.
PutRefused == WhFence /\ whHeld
\* G5: the table door's undrop of T holds T's claim fenced from before its register until its record is cleared
\* (table_claims.fence_kept), so the cascade's fence_listed of T, and the namespace undrop's own fence of it, answer 409.
TFenced(t) == UndropFenced /\ t = "T" /\ tpc = "clear"
\* G1: the drop door holds P's fence for a drop from its identify to its answer, and the namespace undrop holds it for
\* an add while it runs: each refuses the other's take (409), as an add under P refuses a drop of P.
DropHeld   == NsFence /\ dpc \in {"table", "detach", "nsdrop", "nsfile"}
UndropHeld == NsFence /\ upc \in {"nsP", "nsC", "tT", "tN", "clrC", "clrP"}
DropFenced(n) == NsFence /\ n = "P" /\ (addHeld \/ UndropHeld)

---------------------------------------------------------------------------
\* THE CASCADE DROP OF P.

DOthers == UNCHANGED <<whRec, whRow, expired, cpc, addHeld, upc, uStarted, tpc, wpc, wtrash, wtabs, whHeld, whDetached>>

\* identify, then P's fence, then _collect_descendants: a namespace that is not there answers 404 (DropMode Fail).
DStart ==
  /\ dpc = "start" /\ NoFault
  /\ IF ~nsRow["P"] THEN UNCHANGED <<dtabs, dns>> /\ dpc' = "404"
     ELSE IF DropFenced("P") THEN UNCHANGED <<dtabs, dns>> /\ dpc' = "refused"
     ELSE /\ dtabs' = {t \in Tables : tRow[t]} /\ dns' = IF nsRow["C"] THEN {"C"} ELSE {}
          /\ dpc' = "table"
  /\ UNCHANGED <<nsRow, tRow, bytes, trashT, trashNs, dcur>> /\ DOthers

\* One listed table, under fence_listed: one that left since the listing is skipped. Recoverable: RECORD FIRST
\* (trash.put), DETACH SECOND (deregister_table), as _trash_listed_table does. Destroying: dataplane.destroy_table.
DFile(t) ==
  /\ dpc = "table" /\ t \in dtabs /\ NoFault
  /\ IF ~tRow[t] THEN UNCHANGED <<tRow, bytes, trashT>> /\ dpc' = "table" /\ dtabs' = dtabs \ {t}
     ELSE IF TFenced(t) THEN UNCHANGED <<tRow, bytes, trashT, dtabs>> /\ dpc' = "refused"
     ELSE IF Recoverable
            THEN IF PutRefused
                   THEN UNCHANGED <<tRow, bytes, trashT, dtabs>> /\ dpc' = "refused"
                   ELSE trashT' = [trashT EXCEPT ![t] = "cascade"] /\ dpc' = "detach" /\ UNCHANGED <<tRow, bytes>> /\ dtabs' = dtabs \ {t}
            ELSE tRow' = [tRow EXCEPT ![t] = FALSE] /\ bytes' = [bytes EXCEPT ![t] = "deleted"] /\ dpc' = "table" /\ UNCHANGED trashT
                 /\ dtabs' = dtabs \ {t}
  /\ dcur' = t
  /\ UNCHANGED <<nsRow, trashNs, dns>> /\ DOthers

DDetach ==
  /\ dpc = "detach" /\ NoFault
  /\ tRow' = [tRow EXCEPT ![dcur] = FALSE]
  /\ dpc' = "table"
  /\ UNCHANGED <<nsRow, bytes, trashT, trashNs, dtabs, dns, dcur>> /\ DOthers

\* The namespace whose turn it is: each deepest first, then P.
NsTurn == IF dns # {} THEN "C" ELSE "P"
RecordsFirst == Recoverable /\ RecordFirst

\* G2: the recoverable cascade files a namespace's record (describe, then trash.put) before its drop. An absent one
\* answers 404 at its describe and ends the request.
DNsRecord ==
  LET n == NsTurn IN
  /\ dpc = "table" /\ dtabs = {} /\ RecordsFirst /\ NoFault
  /\ IF ~nsRow[n] THEN dpc' = "fail" /\ UNCHANGED trashNs
     ELSE IF PutRefused THEN dpc' = "refused" /\ UNCHANGED trashNs
     ELSE trashNs' = trashNs \cup {n} /\ dpc' = "nsdrop"
  /\ dcur' = n
  /\ UNCHANGED <<nsRow, tRow, bytes, trashT, dtabs, dns>> /\ DOthers

\* drop_namespace, Restrict (A2), under the namespace's fence (G1); a refusal ends the request. Without G2 the record
\* is filed after it; destroying, an absent namespace is tolerated.
DNsDrop ==
  LET n == IF dpc = "nsdrop" THEN dcur ELSE NsTurn IN
  /\ \/ dpc = "nsdrop"
     \/ dpc = "table" /\ dtabs = {} /\ ~RecordsFirst
  /\ NoFault
  /\ IF ~nsRow[n]
       THEN /\ UNCHANGED nsRow
            /\ dpc' = (CASE Recoverable -> "fail" [] n = "P" -> "ok" [] OTHER -> "table")
            /\ IF ~Recoverable /\ n = "C" THEN dns' = dns \ {n} ELSE UNCHANGED dns
       ELSE IF DropFenced(n) \/ (Restrict /\ ~Empty(n))
              THEN dpc' = "refused" /\ UNCHANGED <<nsRow, dns>>
              ELSE /\ nsRow' = [nsRow EXCEPT ![n] = FALSE]
                   /\ dpc' = CASE Recoverable /\ ~RecordFirst -> "nsfile" [] n = "P" -> "ok" [] OTHER -> "table"
                   /\ dns' = IF Recoverable /\ ~RecordFirst THEN dns ELSE dns \ {n}
  /\ dcur' = n
  /\ UNCHANGED <<tRow, bytes, trashT, trashNs, dtabs>> /\ DOthers

\* Without G2: the record filed after the drop.
DNsFile ==
  /\ dpc = "nsfile" /\ NoFault
  /\ IF PutRefused
       THEN dpc' = "refused" /\ UNCHANGED <<trashNs, dns>>
       ELSE /\ trashNs' = trashNs \cup {dcur}
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
\* THE CREATE (or rename in) OF N in P. With A1 the __manifest add checks the parent's row in the same commit; as
\* measured it checks the row it read, so the check and the add are two steps, held apart from P's drop by G1.

COthers == UNCHANGED <<whRec, whRow, nsRow, trashT, trashNs, expired, dpc, dtabs, dns, dcur, upc, uStarted, tpc, wpc, wtrash, wtabs, whHeld,
                       whDetached>>

CCheck ==
  /\ cpc = "start" /\ NoFault
  /\ IF nsRow["P"] /\ whRec /\ ~tRow["N"] /\ ~DropHeld
       THEN IF ParentAtomic
              THEN tRow' = [tRow EXCEPT !["N"] = TRUE] /\ bytes' = [bytes EXCEPT !["N"] = "data"] /\ cpc' = "done" /\ UNCHANGED addHeld
              ELSE UNCHANGED <<tRow, bytes>> /\ cpc' = "add" /\ addHeld' = NsFence
       ELSE UNCHANGED <<tRow, bytes, addHeld>> /\ cpc' = "done"
  /\ COthers

CAdd ==
  /\ cpc = "add" /\ NoFault
  /\ tRow' = [tRow EXCEPT !["N"] = TRUE] /\ bytes' = [bytes EXCEPT !["N"] = "data"] /\ cpc' = "done"
  /\ addHeld' = FALSE
  /\ COthers

---------------------------------------------------------------------------
\* THE NAMESPACE UNDROP OF P (undrop_namespace), once P's record is filed.

UOthers == UNCHANGED <<whRec, whRow, bytes, expired, dpc, dtabs, dns, dcur, cpc, addHeld, tpc, wpc, wtrash, wtabs, whHeld, whDetached>>

\* admitted_trash: a record for P, with no namespace holding the name or the one it records (the same uuid: an earlier
\* attempt rebuilt it); 404 otherwise (a re-issue too).
UStart ==
  /\ upc \in {"wait", "start"} /\ ~DropHeld /\ NoFault
  /\ IF "P" \in trashNs /\ whRec
       THEN upc' = "nsP" /\ uStarted' = TRUE
       ELSE IF upc = "start" THEN upc' = "404" /\ UNCHANGED uStarted ELSE FALSE
  /\ UNCHANGED <<nsRow, tRow, trashT, trashNs>> /\ UOthers

\* Each namespace record shallowest first: create_or_keep_namespace. Without G3 its record is cleared right after.
UNs(n, here, next) ==
  /\ upc = here /\ NoFault
  /\ IF n \in trashNs
       THEN IF (n = "P" /\ whRow /\ ~(WhFence /\ whHeld)) \/ (n = "C" /\ nsRow["P"])
              THEN nsRow' = [nsRow EXCEPT ![n] = TRUE] /\ upc' = next
                   /\ trashNs' = IF ClearsLast THEN trashNs ELSE trashNs \ {n}
              ELSE upc' = "dead" /\ UNCHANGED <<nsRow, trashNs>>
       ELSE upc' = next /\ UNCHANGED <<nsRow, trashNs>>
  /\ UNCHANGED <<tRow, trashT, uStarted>> /\ UOthers

\* Each table the cascade filed, through the table undrop's own step (tables.restore_kept_table): the kept claim fenced,
\* the record read again, register_table (an already-registered one is its own only while the name holds it there),
\* trash.clear, the fence lifted; one step here, since no door acts on the table while the fence stands. A claim the
\* table door's undrop fences answers 409, and the namespace undrop is issued again.
UTable(t, here, next) ==
  /\ upc = here /\ NoFault
  /\ IF trashT[t] = "cascade"
       THEN IF TFenced(t) THEN upc' = "dead" /\ UNCHANGED <<tRow, trashT>>
            ELSE IF nsRow[Parent[t]]
              THEN tRow' = [tRow EXCEPT ![t] = TRUE] /\ trashT' = [trashT EXCEPT ![t] = "none"] /\ upc' = next
              ELSE upc' = "dead" /\ UNCHANGED <<tRow, trashT>>
       ELSE upc' = next /\ UNCHANGED <<tRow, trashT>>
  /\ UNCHANGED <<nsRow, trashNs, uStarted>> /\ UOthers

\* G3: each namespace's record cleared once every table is back, deepest first, P's last.
UClear(n, here, next) ==
  /\ upc = here /\ NoFault
  /\ trashNs' = trashNs \ {n}
  /\ upc' = next
  /\ UNCHANGED <<nsRow, tRow, trashT, uStarted>> /\ UOthers

UDies ==
  /\ "undrop" \in FaultsIn /\ upc \in {"nsP", "nsC", "tT", "tN", "clrC", "clrP"} /\ Fault
  /\ upc' = "dead"
  /\ UNCHANGED <<nsRow, tRow, trashT, trashNs, uStarted>> /\ UOthers

URetry ==
  /\ upc = "dead" /\ NoFault
  /\ upc' = "start"
  /\ UNCHANGED <<nsRow, tRow, trashT, trashNs, uStarted>> /\ UOthers

---------------------------------------------------------------------------
\* THE TABLE DOOR'S UNDROP OF T: its record, its own namespace standing (require_its_own_parent), register, clear.

TOthers == UNCHANGED <<whRec, whRow, nsRow, bytes, trashNs, expired, dpc, dtabs, dns, dcur, cpc, addHeld, upc, uStarted, wpc, wtrash, wtabs,
                       whHeld, whDetached>>

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

MOthers == UNCHANGED <<whRec, whRow, nsRow, tRow, trashNs, dpc, dtabs, dns, dcur, cpc, addHeld, upc, uStarted, tpc, wpc, wtrash, wtabs, whHeld,
                       whDetached>>

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

WOthers == UNCHANGED <<expired, dpc, dtabs, dns, dcur, cpc, addHeld, upc, uStarted, tpc>>

\* G4: the warehouse's namespace fence taken for a drop, then trash_owned_by: the warehouse's trash, read once.
WStart ==
  /\ wpc = "start" /\ NoFault
  /\ IF whRec
       THEN wtrash' = {t \in Tables : trashT[t] # "none"} \cup trashNs /\ wpc' = "end" /\ whHeld' = WhFence
       ELSE UNCHANGED <<wtrash, whHeld>> /\ wpc' = "404"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtabs, whDetached>> /\ WOthers

\* Each record it read is ended (_forget_trashed: trash.end; bytes kept without ?purge_bucket, for a register).
WEnd ==
  /\ wpc = "end" /\ NoFault
  /\ trashT' = [t \in Tables |-> IF t \in wtrash THEN "none" ELSE trashT[t]]
  /\ trashNs' = trashNs \ wtrash
  /\ whDetached' = whDetached \cup {t \in Tables : t \in wtrash /\ trashT[t] # "none"}
  /\ wpc' = "list"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, wtrash, wtabs, whHeld>> /\ WOthers

\* _namespaces_in and _cascade_subtrees: what stands under the warehouse now.
WList ==
  /\ wpc = "list" /\ NoFault
  /\ wtabs' = {t \in Tables : tRow[t]}
  /\ wpc' = "detach"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, whHeld, whDetached>> /\ WOthers

\* _detach_table: each listed table that still stands is deregistered, its bytes left where a register can attach them.
WDetach ==
  /\ wpc = "detach" /\ NoFault
  /\ tRow' = [t \in Tables |-> IF t \in wtabs THEN FALSE ELSE tRow[t]]
  /\ whDetached' = whDetached \cup {t \in wtabs : tRow[t]}
  /\ wpc' = "nsC"
  /\ UNCHANGED <<whRec, whRow, nsRow, bytes, trashT, trashNs, wtrash, wtabs, whHeld>> /\ WOthers

\* Each namespace deepest first, an absent one tolerated, a non-empty or fenced one refused (a partial 503), then the
\* warehouse's row.
WNs(n, here, next) ==
  /\ wpc = here /\ NoFault
  /\ IF nsRow[n] /\ (DropFenced(n) \/ (Restrict /\ ~Empty(n)))
       THEN wpc' = "dead" /\ UNCHANGED nsRow
       ELSE nsRow' = [nsRow EXCEPT ![n] = FALSE] /\ wpc' = next
  /\ whHeld' = (whHeld /\ wpc' # "dead")
  /\ UNCHANGED <<whRec, whRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

WRow ==
  /\ wpc = "row" /\ NoFault
  /\ IF Restrict /\ ~WhEmpty THEN wpc' = "dead" /\ UNCHANGED whRow ELSE whRow' = FALSE /\ wpc' = "record"
  /\ whHeld' = (whHeld /\ wpc' # "dead")
  /\ UNCHANGED <<whRec, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

\* The record goes last, and the fence with it.
WRecord ==
  /\ wpc = "record" /\ NoFault
  /\ whRec' = FALSE /\ wpc' = "ok" /\ whHeld' = FALSE
  /\ UNCHANGED <<whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

\* A delete that dies lets its hold lapse.
WDies ==
  /\ "wh" \in FaultsIn /\ wpc \in {"end", "list", "detach", "nsC", "nsP", "row", "record"} /\ Fault
  /\ wpc' = "dead" /\ whHeld' = FALSE
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whDetached>> /\ WOthers

WRetry ==
  /\ wpc = "dead" /\ NoFault
  /\ wpc' = "start"
  /\ UNCHANGED <<whRec, whRow, nsRow, tRow, bytes, trashT, trashNs, wtrash, wtabs, whHeld, whDetached>> /\ WOthers

---------------------------------------------------------------------------
DropSteps   == DStart \/ (\E t \in Tables : DFile(t)) \/ DDetach \/ DNsRecord \/ DNsDrop \/ DNsFile \/ DRetry
CreateSteps == CCheck \/ CAdd
UndropSteps == UStart \/ UNs("P", "nsP", "nsC") \/ UNs("C", "nsC", "tT") \/ UTable("T", "tT", "tN") \/ UTable("N", "tN", "clrC")
               \/ UClear("C", "clrC", "clrP") \/ UClear("P", "clrP", "ok") \/ URetry
TableSteps  == TUndrop \/ TClear
PurgeSteps  == Expire \/ \E t \in Tables : Purge(t)
WhSteps     == WStart \/ WEnd \/ WList \/ WDetach \/ WNs("C", "nsC", "nsP") \/ WNs("P", "nsP", "row") \/ WRow \/ WRecord \/ WRetry

Work == DropSteps \/ CreateSteps \/ UndropSteps \/ TableSteps \/ PurgeSteps \/ WhSteps

Next == Work \/ DDies \/ UDies \/ WDies

\* Each door is fair on its own; strongly, since a fence refusal makes a door's next step wait on another door's.
Spec == Init /\ [][Next]_vars /\ SF_vars(DropSteps) /\ SF_vars(CreateSteps) /\ SF_vars(UndropSteps) /\ WF_vars(TableSteps)
        /\ WF_vars(PurgeSteps) /\ SF_vars(WhSteps)

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
