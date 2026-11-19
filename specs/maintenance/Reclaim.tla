---------------------------- MODULE Reclaim ----------------------------
(***************************************************************************)
(* What a reclaim may delete when it CONDEMNS FIRST and every door that    *)
(* references a version PINS FIRST (ADR 0246): the model of               *)
(* lakehouse.condemned_versions, the catalog's batch door                 *)
(* (catalog.services.reclaims), every ref door's pin and a pass's          *)
(* deletes (maintenance.services.expiry, base_reclaim).                   *)
(*                                                                         *)
(* THE RECORD. A table carries one condemned-versions record on the        *)
(* control root, written only by the catalog, by a conditional write       *)
(* (If-Match). An entry names a version by the IDENTITY of its ref (main,  *)
(* or one incarnation of a branch: Lance's branch_identifier, so a branch  *)
(* deleted and made again under its name is another ref), the version's   *)
(* number, the batch that wrote it, and its state: "c" (condemned) or "g" *)
(* (the batch passed its pin read and its policy read: deletion may have   *)
(* begun under it). A ref door refuses a version an entry names. An entry *)
(* is dropped only once its version's manifest is gone or its ref's        *)
(* identity no longer stands (GC), or by its own batch before it deleted  *)
(* anything (a pinned version, or a hold).                                 *)
(*                                                                         *)
(* A RECLAIM BATCH (a pass, the erasure's verification, the release's own  *)
(* reclaim: every reclaim calls the catalog once per batch of at most 16   *)
(* versions, then runs Lance's cleanup under its maintain vend):           *)
(*   RBegin    the reclaimer reads its ref's identity (main's is the       *)
(*             table's uuid, a branch's its incarnation), THEN opens the   *)
(*             handle it lists the versions past retention on: so the      *)
(*             listing is of that incarnation or a later one, and one      *)
(*             step stands for both (a later one is refused at RCondemn)   *)
(*   RCondemn  the catalog resolves the ref's name and refuses the batch   *)
(*             (409, code 14, nothing condemned) where it is another       *)
(*             identity than the one the reclaimer read (ListedOn); else   *)
(*             (CondemnFirst) the entries written, state "c": the numbers  *)
(*             asked that stand on the ref, and every standing version an  *)
(*             entry names on it (finish)                                  *)
(*   RPins     (ExplicitPins) the table's tags read: user tags and pins;   *)
(*             a version one names is excluded, unless an entry of another *)
(*             batch names it "g" (GoOnly): no door pinned it before that  *)
(*             batch's pin read, so every door that pinned it since reads  *)
(*             that entry and refuses; such pins are deleted with RPromote *)
(*   RLevel    (PolicyAfterCondemn) the governing policy read level by     *)
(*             level: the table's record, its namespace's, its project's;  *)
(*             a hold at any of them keeps every version                   *)
(*   RPromote  held: its own entries removed (nothing was deleted under   *)
(*             them), answer "held". Else its own entries of excluded      *)
(*             versions removed, the rest set "g", and (GoUnpins) the pins *)
(*             on another batch's "g" versions deleted: Lance keeps every  *)
(*             tagged version, so a dead door's pin would keep a half       *)
(*             removed version so for good                                 *)
(*             Each "g" entry carries its licence (l) until licensed_until; *)
(*             a batch of branch b then reads the branches (StandsAfterGo) *)
(*             and licenses nothing where its incarnation went             *)
(*   RSnap     lance cleanup_old_versions(versions=batch,                  *)
(*             error_if_tagged_old_versions=False): it reads the refs      *)
(*             (tags, pins, branches, heads) and the manifests, in an      *)
(*             order lance_docs does not state (LanceOrder; measured on    *)
(*             pylance 13.0.0: branches, tags, then manifests), and keeps  *)
(*             every version a tag it read names. It names NUMBERS, and    *)
(*             deletes the versions of those numbers under the PATH the    *)
(*             handle names: whichever incarnation branch b's name holds   *)
(*             when it lists (measured on pylance 13.0.0)                  *)
(*   RData     its transaction and data files, every one before any       *)
(*             manifest (measured on pylance 13.0.0, ADR 0243 §3)          *)
(*   RMan      its manifests; a version a standing branch is cut from      *)
(*             keeps its manifest, and in an erasure's fork cleanup        *)
(*             (ErasureFork) loses the files only it names (ADR 0243 R1)   *)
(*   RBase     base_reclaim.unreferenced: the table's own data directory   *)
(*             (Base, a plain base the root-scoped cleanup never touches,  *)
(*             LH-286) listed, less what every standing manifest names     *)
(*   RBDel     base_reclaim.reclaim's deletes, one at a time               *)
(*                                                                         *)
(* A REF DOOR (a tag create or update, a branch create, a restore, a      *)
(* clone's source tag):                                                    *)
(*   DPin      (PinFirst) a reserved Lance tag rask-pin-<hex> on the       *)
(*             version                                                     *)
(*   DCheck    the record read: a version an entry names is refused (409)  *)
(*   DAct      the ref, or the restore's commit (Lance reads the version's *)
(*             manifest: a version whose manifest is gone fails)           *)
(*   DUnpin    its pin deleted                                             *)
(* Each side writes, then reads the other's write: on a linearizable store *)
(* at least one sees the other. No lease, no renewal and no clock.         *)
(*                                                                         *)
(* A BRANCH CREATE reads the name free, then the record: a live licence   *)
(* under the name refuses it (NameGuard), as it holds off the release's    *)
(* recreate; a deleted incarnation's licensed entries are dropped only     *)
(* once their licence has passed (GCAfterLicence). A branch delete is      *)
(* never refused for a reclaim.                                            *)
(*                                                                         *)
(* A POLICY DOOR (the table's, a namespace's, a project's hold, and a     *)
(* rename into a namespace that holds) writes, then (HoldWritesFirst)      *)
(* reads the record, and answers 200 naming the versions already           *)
(* condemned: it promises every other standing version.                    *)
(* VERSION/DELETE condemns the versions it names, then reads the tags and *)
(* pins and takes back an entry one names (VDeleteCondemnsFirst); it      *)
(* deletes nothing.                                                        *)
(* THE ERASURE'S RELEASE still holds the history fence as RELEASE, which  *)
(* excludes every ref door (G1 of Erasure.tla) and no reclaim: it records *)
(* main's head, (ReleasePins) pins it and reads the record, deletes the    *)
(* branch, then makes it again at the recorded version, then unpins.       *)
(*                                                                         *)
(* FAULTS, counted against MaxFaults: a reclaim that dies at any step, or *)
(* whose deletes the store refuses (its credential expired): Lance counts  *)
(* the refusals and returns; a door that dies (its pin stays); the release *)
(* tick that dies (resumed from its record).                               *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   B1. Linearizable writes and listings on the control root and the     *)
(*       table's _refs/: a write is seen by every read that starts after   *)
(*       it. Each write and each read is one step here. Measured on        *)
(*       RustFS 1.0.3 (ADR 0246 §measurements: tag list after tag create,   *)
(*       record read after its conditional write).                         *)
(*   B2. Only the catalog writes or deletes a pin or the record: no vended *)
(*       tier is granted anything under _refs/ (put: write tier's globs    *)
(*       name none; delete: the maintain tier's explicit Deny), and the    *)
(*       control root is the catalog's. The tag doors refuse a user tag    *)
(*       named rask-pin-*. Measured on RustFS 1.0.3.                       *)
(*   A2 (kept from TablePass.tla). AgeFloor: a file a commit in flight     *)
(*       wrote is younger than the unverified age (7 days) until its       *)
(*       version lands. ReclaimWriterOutlivesAge removes it.                *)
(*   A3' (only where a pin sweep is adopted, PinSweep = "dead"): a pin is  *)
(*       swept only once its door can no longer act. ReclaimPinSweepLive    *)
(*       removes it. With no sweep, nothing is assumed: a dead door's pin   *)
(*       keeps its version whole. A release's pin is of a kind no sweep    *)
(*       takes (ReleasePinKind; ReclaimReleaseSwept removes it).            *)
(*   C (CredentialDies). A batch's credential is minted before the batch   *)
(*       is asked and lives at most CREDENTIAL_LIFETIME, and the store's    *)
(*       clock and the catalog's agree within CLOCK_MARGIN, so the store    *)
(*       refuses it once its licence's instant (licensed_until, the         *)
(*       catalog's clock) has passed (LicenceEnds kills it).               *)
(*       ReclaimCredentialOutlivesLicence removes it. LicenceEnds is taken  *)
(*       only where branch b's name can be made again; elsewhere a          *)
(*       credential dying mid-cleanup is RRefused, a fault, and the         *)
(*       liveness configs assume a cleanup ends within its licence.         *)
(*                                                                         *)
(* NOT MODELLED: the staged manifests a pass removes, which no version      *)
(* names, and the committed-file records it forgets, which govern no byte; *)
(* a version expiring by the catalog's commit-time stamp (Age changes it   *)
(* at any time, which over-approximates every clock, ADR 0243 §10); the    *)
(* floor raise, a catalog commit like any writer's; a drop, undrop and     *)
(* purge (TableLifecycle.tla: the purge deletes no registered table's      *)
(* bytes, and a pass reads the trash again before it deletes);            *)
(* the erasure's scheduling and notices (Erasure.tla, whose fence kinds    *)
(* SCHEDULE, RELEASE, REFS and BRANCH stay: CondemnedErasure.tla checks    *)
(* it with reclaims off the fence); two tags on one version; a batch of    *)
(* more than the versions here; a branch whose own versions name its       *)
(* fork's files (each branch version here names a file of its own, the     *)
(* erasure's shape); a hold lifted; a table dropped and made again at its  *)
(* location, another uuid and so another identity of main                  *)
(* (TableLifecycle.tla's): here main's identity never changes.             *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, Sequences, TLC

CONSTANTS Recl,        \* the reclaimers that may run at once (a pass, version/delete's finishing pass, an erasure's cleanup)
          Doors,       \* which of RefDoors \cup HoldDoors \cup {"vdelete", "bdelete", "writer", "bwriter", "release"} act
          InitBranch,  \* branch b stands at the start (incarnation b1, cut from main 1, one version of its own)
          ErasureFork, \* a reclaimer may run the erasure's fork cleanup of branch b (Reclaim.run(forks=True))
          LanceOrder,  \* "tags", "manifests" or "any": which Lance's cleanup reads first
          PinSweep,    \* "none", "dead" (a dead door's pin is swept: A3') or "any" (any pin is swept)
          MaxFaults,
          Off          \* the guards removed (Guards)

Guards == {"CondemnFirst", "PinFirst", "ExplicitPins", "PolicyAfterCondemn", "HoldWritesFirst", "GoOnly", "GCOnlyGone",
           "KeepAfterDelete", "IdentityKeys", "ReleasePins", "AgeFloor", "GoUnpins", "NameGuard", "GCAfterLicence",
           "StandsAfterGo", "CredentialDies", "ReleasePinKind", "VDeleteCondemnsFirst", "ListedOn"}
ASSUME Off \subseteq Guards
ASSUME LanceOrder \in {"tags", "manifests", "any"} /\ PinSweep \in {"none", "dead", "any"}
On(g) == g \notin Off

RefDoors  == {"tag", "restore", "branch"}
HoldDoors == {"holdT", "holdN", "holdP", "rename"}
Levels == <<"table", "ns", "proj">>
LevelOf(d) == CASE d = "holdT" -> "table" [] d \in {"holdN", "rename"} -> "ns" [] OTHER -> "proj"
Incs == {"b1", "b2"}
MaxMain == 4
MaxOwn == 5

\* A version is <<ref identity, number>>: main is "m"; a branch's incarnation is "b1" or "b2", numbered on from its fork.
MainV(n) == <<"m", n>>
Versions == {MainV(n) : n \in 1..MaxMain} \cup {<<i, n>> : i \in Incs, n \in 2..MaxOwn}
\* "b" lies in one of the table's own data directories (a plain base, LH-286): Lance's root-scoped cleanup never deletes it.
Base == {"b"}
OwnFile(i, n) == i \o "_" \o ToString(n)

VARIABLES manifests, names, files, mhead,
          binc,        \* the incarnation branch b's name resolves to, "none" when no branch b stands
          bfork, bhead, used, expired,
          tagged,      \* versions a user tag names
          pins,        \* <<who, version>>: each rask-pin tag standing
          tomb,        \* the record's entries [k, b, s]
          chosen,      \* the versions a reclaim chose past their retention, or version/delete named, or an erasure's fork cleanup took
          policy, answered,
          rpc, rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel,
          bdel,        \* the base files a reclaim's base_reclaim listed to delete
          rcred,       \* whether the store still honours the credential the reclaimer minted before its batch
          snapI,       \* the incarnation the reclaimer's path held when Lance listed its manifests ("m" for main)
          dpc, dv, dcond,
          vdv,         \* the version version/delete names
          wpc, relpc, relat,
          faults, badDelete, refusedWrong

vars == <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, pins, tomb, chosen, policy, answered,
          rpc, rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, bdel, rcred, snapI, dpc, dv, dcond, vdv,
          wpc, relpc, relat, faults, badDelete, refusedWrong>>
R == <<rpc, rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, bdel, rcred, snapI>>
D == <<dpc, dv, dcond, vdv>>

NoV == <<"none", 0>>
Proc == RefDoors \cup HoldDoors

Init ==
  /\ manifests = {MainV(1), MainV(2)} \cup (IF InitBranch THEN {<<"b1", 2>>} ELSE {})
  /\ names = [v \in Versions |-> CASE v = MainV(1) -> {"a", "s", "b"} [] v = MainV(2) -> {"s", "c"}
                                       [] v[1] \in Incs -> {OwnFile(v[1], v[2])} [] OTHER -> {}]
  /\ files = {"a", "s", "c", "b"} \cup (IF InitBranch THEN {OwnFile("b1", 2)} ELSE {})
  /\ mhead = 2
  /\ binc = IF InitBranch THEN "b1" ELSE "none"
  /\ bfork = [i \in Incs |-> 1]
  /\ bhead = [i \in Incs |-> IF InitBranch /\ i = "b1" THEN 2 ELSE 1]
  /\ used = IF InitBranch THEN {"b1"} ELSE {}
  /\ expired = {MainV(1)}
  /\ tagged = {}
  /\ pins = {}
  /\ tomb = {}
  /\ chosen = {}
  /\ policy = [l \in {"table", "ns", "proj"} |-> "none"]
  /\ answered = {}
  /\ rpc = [r \in Recl |-> "idle"] /\ rref = [r \in Recl |-> "m"] /\ rkind = [r \in Recl |-> "plain"]
  /\ rb = [r \in Recl |-> {}] /\ rfresh = [r \in Recl |-> {}] /\ rex = [r \in Recl |-> {}] /\ rgo = [r \in Recl |-> {}]
  /\ rheld = [r \in Recl |-> FALSE] /\ snapT = [r \in Recl |-> {}] /\ snapM = [r \in Recl |-> {}]
  /\ del = [r \in Recl |-> {}] /\ mdel = [r \in Recl |-> {}] /\ bdel = [r \in Recl |-> {}]
  /\ rcred = [r \in Recl |-> FALSE] /\ snapI = [r \in Recl |-> "none"]
  /\ dpc = [d \in Proc \cup {"vdelete", "bdelete"} |-> IF d \in Doors THEN "idle" ELSE "done"]
  /\ dv = [d \in RefDoors |-> NoV]
  /\ dcond = [d \in HoldDoors |-> {}]
  /\ vdv = NoV
  /\ wpc = [w \in {"writer", "bwriter"} |-> IF w \in Doors THEN "idle" ELSE "done"]
  /\ relpc = IF "release" \in Doors /\ InitBranch THEN "idle" ELSE "done"
  /\ relat = 0
  /\ faults = 0
  /\ badDelete = FALSE
  /\ refusedWrong = FALSE

\* ---------------------------------------------------------------- the table
StandsInc(i) == i = "m" \/ i = binc
Stands(v) == StandsInc(v[1])
Whole(v) == v \in manifests /\ names[v] \subseteq files
Named(S) == UNION {names[v] : v \in S}
HeadOf(i) == IF i = "m" THEN MainV(mhead) ELSE IF bhead[i] = bfork[i] THEN MainV(bfork[i]) ELSE <<i, bhead[i]>>
Heads == {HeadOf("m")} \cup (IF binc = "none" THEN {} ELSE {HeadOf(binc)})
Forks == IF binc = "none" THEN {} ELSE {MainV(bfork[binc])}
PinVs == {p[2] : p \in pins}
\* Every file written here is younger than the unverified age for as long as the model runs (A2): Lance judges a file's
\* age, not whether its version landed, so one its manifest listing missed is kept as young. No file here is an orphan
\* past the age; without AgeFloor the writer's staged file is taken as one.
Young == IF On("AgeFloor") THEN files ELSE files \ {"w"}

\* An entry's key: the version's ref identity (IdentityKeys), or the ref's NAME as a branch name resolves today.
NameOf(i) == IF i = "m" THEN "m" ELSE "b"
\* An entry's licence (field l) is live from its batch's promotion until its stated instant, licensed_until.
LiveUnder(n) == \E e \in tomb : NameOf(e.k[1]) = n /\ e.s = "g" /\ e.l
\* NameGuard: a branch created under the name b, by a door or the erasure's release, waits out every live licence under
\* that name: a licence names a path and numbers, and Lance's cleanup deletes by number under the path the name holds.
NameFree == ~On("NameGuard") \/ ~LiveUnder("b")
Key(v) == IF On("IdentityKeys") THEN v ELSE <<NameOf(v[1]), v[2]>>
Resolve(k) == IF k[1] = "b" THEN <<binc, k[2]>> ELSE k
TombKeys == {e.k : e \in tomb}
CondemnedVs == {Resolve(e.k) : e \in tomb}
GoVsOf(r) == {Resolve(e.k) : e \in {x \in tomb : x.s = "g" /\ x.b # r}}
OthersVsOf(r) == {Resolve(e.k) : e \in {x \in tomb : x.b # r}}
Own(r) == {e \in tomb : e.b = r}
Held == \E l \in {"table", "ns", "proj"} : policy[l] = "hold"
Running(r) == rpc[r] \notin {"idle", "dead"}
\* The release holds the history fence as RELEASE, which every ref door's REFS excludes (Erasure.tla G1).
ReleaseHolds == relpc \in {"check", "delete", "recreate", "finish"}
DoorInFlight == \E d \in RefDoors \cap Doors : dpc[d] \in {"check", "latepin", "act", "unpin"}

Fault == faults < MaxFaults /\ faults' = faults + 1

\* ---------------------------------------------------------------- a reclaim batch
RSeq == IF On("PolicyAfterCondemn") THEN <<"condemn", "pins", "lvl1", "lvl2", "lvl3", "promote">>
                                    ELSE <<"lvl1", "lvl2", "lvl3", "condemn", "pins", "promote">>
\* Where no policy door acts, the policy never changes and its three reads are left out: they would add interleavings
\* and decide nothing.
Steps == IF HoldDoors \cap Doors # {} THEN RSeq ELSE SelectSeq(RSeq, LAMBDA s : s \notin {"lvl1", "lvl2", "lvl3"})
After(s) == IF \E i \in 1..(Len(Steps) - 1) : Steps[i] = s THEN Steps[(CHOOSE i \in 1..(Len(Steps) - 1) : Steps[i] = s) + 1] ELSE "snap"
Go(r, s) == rpc' = [rpc EXCEPT ![r] = s]

OfRef(i) == {v \in manifests : v[1] = i}
Refs == {"m"} \cup (IF binc = "none" THEN {} ELSE {binc})

RKeep == UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, faults, refusedWrong>>
\* The path a batch's cleanup deletes under: main's, or branch b's name; the erasure's fork cleanup names a version of
\* main, the ref the fork is cut from.
PathOf(r) == IF rref[r] = "m" \/ rkind[r] = "fork" THEN "m" ELSE "b"

\* The reclaimer's own state set back to its rest, so states differing only in what a finished batch held are one.
Rest(r, s) ==
  /\ rpc' = [rpc EXCEPT ![r] = s]
  /\ rref' = [rref EXCEPT ![r] = "m"] /\ rkind' = [rkind EXCEPT ![r] = "plain"] /\ rb' = [rb EXCEPT ![r] = {}]
  /\ rfresh' = [rfresh EXCEPT ![r] = {}] /\ rex' = [rex EXCEPT ![r] = {}] /\ rgo' = [rgo EXCEPT ![r] = {}]
  /\ rheld' = [rheld EXCEPT ![r] = FALSE] /\ snapT' = [snapT EXCEPT ![r] = {}] /\ snapM' = [snapM EXCEPT ![r] = {}]
  /\ del' = [del EXCEPT ![r] = {}] /\ mdel' = [mdel EXCEPT ![r] = {}] /\ bdel' = [bdel EXCEPT ![r] = {}]
  /\ rcred' = [rcred EXCEPT ![r] = FALSE] /\ snapI' = [snapI EXCEPT ![r] = "none"]

\* The reclaimer reads the ref's identity i, then lists on a handle opened after: some of i's versions past retention that
\* no entry names (rfresh). It asks for their NUMBERS, naming i, and the catalog chooses the batch at the condemn. One step
\* stands for the read and the listing: a listing of a later incarnation names i all the same, and is refused there. The
\* erasure's fork cleanup instead names the version branch b is cut from (a batch of main), once b's own history no longer
\* names it.
RBegin(r) ==
  /\ rpc[r] = "idle"
  /\ \/ \E i \in Refs :
          LET eligible == {v \in OfRef(i) : v # HeadOf(i) /\ v \in expired /\ v \notin Forks /\ v \notin CondemnedVs}
              finish == CondemnedVs \cap OfRef(i)
          IN \E F \in SUBSET eligible :
               /\ F \cup finish # {}
               /\ rb' = [rb EXCEPT ![r] = {}] /\ rfresh' = [rfresh EXCEPT ![r] = F]
               /\ rref' = [rref EXCEPT ![r] = i] /\ rkind' = [rkind EXCEPT ![r] = "plain"]
     \/ /\ ErasureFork /\ binc # "none" /\ bhead[binc] > bfork[binc] /\ MainV(bfork[binc]) \in manifests
        /\ MainV(bfork[binc]) \notin CondemnedVs
        /\ rb' = [rb EXCEPT ![r] = {MainV(bfork[binc])}] /\ rfresh' = [rfresh EXCEPT ![r] = {MainV(bfork[binc])}]
        /\ rref' = [rref EXCEPT ![r] = binc] /\ rkind' = [rkind EXCEPT ![r] = "fork"]
  /\ Go(r, Steps[1])
  /\ rex' = [rex EXCEPT ![r] = {}] /\ rgo' = [rgo EXCEPT ![r] = {}] /\ rheld' = [rheld EXCEPT ![r] = FALSE]
  \* The credential the batch's cleanup deletes under is minted before the batch is asked, so it dies by the licence's
  \* instant, which the promotion states (CredentialDies, LicenceEnds).
  /\ rcred' = [rcred EXCEPT ![r] = TRUE]
  \* Each batch has an id of its own: the entries an earlier batch of this reclaimer left are another batch's ("old").
  /\ tomb' = {IF e.b = r THEN [e EXCEPT !.b = "old"] ELSE e : e \in tomb}
  /\ UNCHANGED <<snapT, snapM, del, mdel, bdel, snapI, pins, chosen, policy, answered, badDelete>> /\ RKeep

\* The catalog resolves the ref's name now (cur): a branch's name may hold another incarnation than the one the reclaimer
\* listed on. A name that holds none answers 404 (code 22), and one that holds another 409 (code 14, ListedOn), nothing
\* condemned. Else the batch is the numbers asked that stand on cur below its head, and every standing version of cur an
\* entry names. Without ListedOn the numbers listed on one incarnation are condemned on another.
\* CondemnFirst removed: the entries are written only once the cleanup has run (the order today's marks had before §7.4).
RCondemn(r) ==
  /\ rpc[r] = "condemn"
  /\ LET plain == rkind[r] = "plain"
         cur == IF plain /\ rref[r] # "m" THEN binc ELSE rref[r]
         asked == IF plain THEN {v \in manifests : v[1] = cur /\ v # HeadOf(cur) /\ v \notin Forks /\ \E u \in rfresh[r] : u[2] = v[2]}
                           ELSE rfresh[r]
         batch == asked \cup (IF plain THEN CondemnedVs \cap OfRef(cur) ELSE {})
     IN IF cur = "none" \/ (On("ListedOn") /\ cur # rref[r]) \/ batch = {}
          THEN Rest(r, "idle") /\ UNCHANGED <<tomb, chosen>>
          ELSE /\ rb' = [rb EXCEPT ![r] = batch] /\ rref' = [rref EXCEPT ![r] = cur]
               /\ tomb' = IF On("CondemnFirst") THEN tomb \cup {[k |-> Key(v), b |-> r, s |-> "c", l |-> FALSE] : v \in batch} ELSE tomb
               /\ chosen' = chosen \cup rfresh[r]
               /\ Go(r, After("condemn"))
               /\ UNCHANGED <<rkind, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, bdel, rcred, snapI>>
  /\ UNCHANGED <<pins, policy, answered, badDelete>> /\ RKeep

\* The table's tags read: a version a user tag or a pin names is excluded, unless (GoOnly) another batch's entry names it
\* "g". Without GoOnly any other batch's entry licenses ignoring the pin.
RPins(r) ==
  /\ rpc[r] = "pins"
  /\ LET licensed == IF On("GoOnly") THEN GoVsOf(r) ELSE OthersVsOf(r)
         named == tagged \cup PinVs
     IN /\ rex' = [rex EXCEPT ![r] = IF On("ExplicitPins") THEN (rb[r] \cap named) \ licensed ELSE {}]
        /\ rgo' = [rgo EXCEPT ![r] = IF On("ExplicitPins") THEN rb[r] \cap licensed ELSE {}]
  /\ Go(r, After("pins"))
  /\ UNCHANGED <<rref, rkind, rb, rfresh, rheld, snapT, snapM, del, mdel, bdel, rcred, snapI, pins, tomb, chosen, policy, answered, badDelete>> /\ RKeep

\* The governing policy, one level at a time: a hold at any level keeps everything (ADR 0243 §8, unchanged).
RLevel(r, n) ==
  /\ rpc[r] = "lvl" \o ToString(n)
  /\ rheld' = [rheld EXCEPT ![r] = rheld[r] \/ policy[Levels[n]] = "hold"]
  /\ Go(r, After("lvl" \o ToString(n)))
  /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, snapT, snapM, del, mdel, bdel, rcred, snapI, pins, tomb, chosen, policy, answered, badDelete>> /\ RKeep

\* Promoted, each own entry carries its licence (l) until its instant. A batch of branch b then reads the branches
\* (StandsAfterGo): one whose incarnation no longer stands under the name licenses nothing, so a name made free after its
\* promotion is free only of a licence no reclaimer acts on, and a name made free before it is one a create's record read
\* finds licensed.
RPromote(r) ==
  /\ rpc[r] = "promote"
  /\ IF rheld[r]
       THEN /\ tomb' = tomb \ Own(r) /\ Rest(r, "idle") /\ UNCHANGED pins
       ELSE /\ tomb' = {IF e.b = r THEN [e EXCEPT !.s = "g", !.l = TRUE] ELSE e : e \in {x \in tomb : ~(x.b = r /\ Resolve(x.k) \in rex[r])}}
            \* GoUnpins takes a door's pin, never the release's, which only the release removes (ReleasePinKind).
            /\ pins' = IF On("GoUnpins") THEN {p \in pins : p[1] = "rel" \/ p[2] \notin rgo[r]} ELSE pins
            /\ Go(r, IF PathOf(r) = "b" THEN "verify" ELSE "snap")
            /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, bdel, rcred, snapI>>
  /\ UNCHANGED <<chosen, policy, answered, badDelete>> /\ RKeep

RVerify(r) ==
  /\ rpc[r] = "verify"
  /\ IF ~On("StandsAfterGo") \/ rref[r] = binc
       THEN Go(r, "snap") /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, bdel, rcred, snapI>>
       ELSE Rest(r, "idle")
  /\ UNCHANGED <<pins, tomb, chosen, policy, answered, badDelete>> /\ RKeep

\* Lance's cleanup reads what it keeps (the tags and pins, the branches it keeps the forks of, the heads) and the
\* manifests, in either order (LanceOrder); then it decides. A tagged, pinned or head version is kept; a version a
\* standing branch is cut from keeps its manifest, and its files too unless named explicitly by the erasure's cleanup.
RefsRead == tagged \cup PinVs \cup Heads \cup Forks
FirstRead(r) == rpc[r] = "snap" /\ snapT[r] = {} /\ snapM[r] = {}
\* Every read and delete of the cleanup is signed by the batch's credential: once the store no longer honours it, the
\* cleanup's next request is refused (RCredDead).
RSnapT(r) ==
  /\ rpc[r] = "snap" /\ snapT[r] = {} /\ rcred[r]
  /\ LanceOrder # "manifests" \/ snapM[r] # {}
  /\ snapT' = [snapT EXCEPT ![r] = RefsRead \cup {NoV}]
  /\ UNCHANGED <<snapM, rpc, rref, rkind, rb, rfresh, rex, rgo, rheld, del, mdel, bdel, rcred, snapI, pins, tomb, chosen, policy, answered, badDelete>> /\ RKeep
\* Lance lists the manifests under the reclaimer's PATH as it stands now: main's, or whichever incarnation branch b's name
\* holds now, which need not be the one the batch condemned.
RSnapM(r) ==
  /\ rpc[r] = "snap" /\ snapM[r] = {} /\ rcred[r]
  /\ LanceOrder # "tags" \/ snapT[r] # {}
  /\ snapM' = [snapM EXCEPT ![r] = manifests \cup {NoV}]
  /\ snapI' = [snapI EXCEPT ![r] = IF PathOf(r) = "m" THEN "m" ELSE binc]
  /\ UNCHANGED <<snapT, rpc, rref, rkind, rb, rfresh, rex, rgo, rheld, del, mdel, bdel, rcred, pins, tomb, chosen, policy, answered, badDelete>> /\ RKeep

\* cleanup_old_versions(versions=...) names NUMBERS, and Lance deletes the versions of those numbers under the path the
\* handle names (its tree/<name>/_versions for a branch, measured on pylance 13.0.0): a version of the incarnation the
\* path holds now whose number the batch licensed for another goes with them.
Nums(S) == {<<NameOf(v[1]), v[2]>> : v \in S}
RDecide(r) ==
  /\ rpc[r] = "snap" /\ snapT[r] # {} /\ snapM[r] # {} /\ rcred[r]
  /\ LET sm == snapM[r] \ {NoV}
         st == snapT[r] \ {NoV}
         cand == {v \in sm : v[1] = snapI[r] /\ <<NameOf(v[1]), v[2]>> \in Nums(rb[r] \ rex[r])}
         dl == IF rkind[r] = "plain" THEN cand \ st ELSE {}
         fv == IF rkind[r] = "fork" THEN cand \ (st \ Forks) ELSE {}
         gone == dl \cup fv
         unverified == {f \in files : f \notin Named(sm) /\ f \notin Young}
     \* Lance's cleanup is root-scoped: the table's own data directory is base_reclaim's (RBase).
     IN /\ del' = [del EXCEPT ![r] = ((Named(gone) \ Named(sm \ gone)) \cup unverified) \ Base]
        /\ mdel' = [mdel EXCEPT ![r] = dl]
        \* Every standing version of a standing ref whose files or manifest go is one this table's reclaims chose (or a
        \* client named), condemned now. A deleted branch's versions no door can name, and a version whose manifest
        \* another batch removed since this one listed it loses nothing more.
        /\ badDelete' = (badDelete \/ \E v \in gone : v \in manifests /\ Stands(v) /\ (v \notin chosen \/ Key(v) \notin TombKeys))
  /\ Go(r, "data")
  /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, bdel, rcred, snapI, pins, tomb, chosen, policy, answered>> /\ RKeep

RData(r) ==
  /\ rpc[r] = "data" /\ rcred[r]
  /\ IF del[r] = {}
       THEN Go(r, "man") /\ UNCHANGED <<files, del>>
       ELSE \E f \in del[r] : files' = files \ {f} /\ del' = [del EXCEPT ![r] = @ \ {f}] /\ UNCHANGED rpc
  /\ UNCHANGED <<manifests, names, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, faults, refusedWrong>>
  /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, mdel, bdel, rcred, snapI, pins, tomb, chosen, policy, answered, badDelete>>

RMan(r) ==
  /\ rpc[r] = "man" /\ rcred[r]
  /\ IF mdel[r] = {}
       THEN Go(r, "base") /\ UNCHANGED <<manifests, mdel>>
       ELSE /\ \E v \in mdel[r] : manifests' = manifests \ {v} /\ mdel' = [mdel EXCEPT ![r] = @ \ {v}]
            /\ UNCHANGED rpc
  /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, bdel, rcred, snapI>>
  /\ UNCHANGED <<names, files, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, faults, refusedWrong>>
  /\ UNCHANGED <<pins, tomb, chosen, policy, answered, badDelete>>

\* base_reclaim, after the cleanup: the table's own data directory listed, less every file a standing manifest names
\* ("b" was written with version 1, past the unverified age; a commit in flight stages nothing here, A2). A version naming
\* a file it lists unnamed has its manifest gone, so no door can bring the file back: every ref door, and a restore,
\* reads the version's manifest.
RBase(r) ==
  /\ rpc[r] = "base" /\ rcred[r]
  /\ bdel' = [bdel EXCEPT ![r] = {f \in Base \cap files : f \notin Named(manifests)}]
  /\ Go(r, "bdel")
  /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, rcred, snapI, pins, tomb, chosen, policy, answered, badDelete>> /\ RKeep

RBDel(r) ==
  /\ rpc[r] = "bdel" /\ rcred[r]
  /\ IF bdel[r] = {}
       THEN Rest(r, "idle") /\ UNCHANGED files
       ELSE /\ \E f \in bdel[r] : files' = files \ {f} /\ bdel' = [bdel EXCEPT ![r] = @ \ {f}]
            /\ UNCHANGED <<rpc, rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, rcred, snapI>>
  /\ UNCHANGED <<manifests, names, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, faults, refusedWrong>>
  /\ UNCHANGED <<pins, tomb, chosen, policy, answered, badDelete>>

\* A reclaim that dies at any step: its entries stand.
RCrash(r) ==
  /\ Running(r) /\ Fault
  /\ Rest(r, "dead")
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, refusedWrong>>
  /\ UNCHANGED <<pins, tomb, chosen, policy, answered, badDelete>>

\* The store refuses its deletes (the vend expired): Lance counts them and returns, and the reclaim answers. With
\* KeepAfterDelete its entries stand for the next reclaim; without it the batch takes them back as though nothing went.
RRefused(r) ==
  /\ rpc[r] \in {"data", "man", "bdel"} /\ Fault
  /\ Rest(r, "idle")
  /\ tomb' = IF On("KeepAfterDelete") THEN tomb ELSE tomb \ Own(r)
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, refusedWrong>>
  /\ UNCHANGED <<pins, chosen, policy, answered, badDelete>>

\* The credential the batch minted is no longer honoured: the store refuses the cleanup's next request, which the
\* reclaim answers as RRefused does, its entries standing (no fault: it is the licence's clock, not a failure).
RCredDead(r) ==
  /\ rpc[r] \in {"snap", "data", "man", "base", "bdel"} /\ ~rcred[r]
  /\ Rest(r, "idle")
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, faults, refusedWrong>>
  /\ UNCHANGED <<pins, tomb, chosen, policy, answered, badDelete>>

\* The task delivered again (WorkQueue): the next reclaim of the table.
RRestart(r) ==
  /\ rpc[r] = "dead"
  /\ Go(r, "idle")
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, D, wpc, relpc, relat, faults, refusedWrong>>
  /\ UNCHANGED <<rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, bdel, rcred, snapI, pins, tomb, chosen, policy, answered, badDelete>>

\* A licence's instant passes (licensed_until, on the catalog's clock). Its batch's credential was minted before the
\* batch was asked and lives at most CREDENTIAL_LIFETIME, so by then the store refuses it (assumption C, CredentialDies:
\* the store's clock and the catalog's agree within the margin the instant carries). Without CredentialDies the
\* credential outlives the licence. Taken only where branch b's name can be made again (the only place a licence's end
\* decides anything): elsewhere a credential that dies mid-cleanup is RRefused, a fault, and the liveness configs keep
\* their assumption that a batch's cleanup ends within its licence.
Licences == InitBranch \/ "branch" \in Doors \/ "release" \in Doors
LicenceEnds ==
  /\ Licences
  /\ \E e \in tomb :
    /\ e.l
    /\ tomb' = {IF x.b = e.b /\ x.l THEN [x EXCEPT !.l = FALSE] ELSE x : x \in tomb}
    /\ rcred' = IF On("CredentialDies") /\ e.b \in Recl THEN [rcred EXCEPT ![e.b] = FALSE] ELSE rcred
    /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, pins, chosen, policy, answered>>
    /\ UNCHANGED <<rpc, rref, rkind, rb, rfresh, rex, rgo, rheld, snapT, snapM, del, mdel, bdel, snapI>>
    /\ UNCHANGED <<D, wpc, relpc, relat, faults, badDelete, refusedWrong>>

\* The catalog rewriting the record drops an entry whose version's manifest is gone or whose ref identity no longer
\* stands; a gone identity's licensed entry only once its licence has passed (GCAfterLicence), so a create under its name
\* finds it until then. Without GCOnlyGone it also drops one whose batch is not running, its manifest standing.
GC ==
  \E e \in tomb :
    /\ \/ Resolve(e.k) \notin manifests /\ Stands(Resolve(e.k))
       \/ ~Stands(Resolve(e.k)) /\ (e.l => ~On("GCAfterLicence"))
       \/ ~On("GCOnlyGone") /\ e.b # "vd" /\ (e.b \in Recl => ~Running(e.b))
    /\ tomb' = tomb \ {e}
    /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, pins, chosen, policy, answered>>
    /\ UNCHANGED <<R, D, wpc, relpc, relat, faults, badDelete, refusedWrong>>

\* Time: a superseded version passes its retention, on whatever clock.
Age ==
  \E v \in manifests \ (expired \cup Heads) :
    /\ expired' = expired \cup {v}
    /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, tagged, pins, tomb, chosen, policy, answered>>
    /\ UNCHANGED <<R, D, wpc, relpc, relat, faults, badDelete, refusedWrong>>

\* ---------------------------------------------------------------- the ref doors
DKeep == UNCHANGED <<R, wpc, relpc, relat, dcond, vdv, chosen, policy, answered, badDelete, expired>>
DGo(d, s) == dpc' = [dpc EXCEPT ![d] = s]

\* What a door may name: a tag, any standing version; a restore, a main version not the head; a branch create, a main
\* version while no branch b stands and an incarnation is left.
Targets(d) ==
  CASE d = "tag" -> {v \in manifests : Stands(v)}
    [] d = "restore" -> {v \in manifests : v[1] = "m" /\ v # HeadOf("m") /\ mhead < MaxMain}
    [] OTHER -> IF binc = "none" /\ Incs \ used # {} THEN {v \in manifests : v[1] = "m"} ELSE {}

DPin(d) ==
  /\ dpc[d] = "idle" /\ ~ReleaseHolds
  /\ \E v \in Targets(d) :
       /\ dv' = [dv EXCEPT ![d] = v]
       /\ pins' = IF On("PinFirst") THEN pins \cup {<<d, v>>} ELSE pins
  /\ DGo(d, "check")
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, tagged, tomb, faults, refusedWrong>> /\ DKeep

\* A branch create reads the name free (Targets), then the record: a live licence under the name refuses it too (409,
\* retryable once the licence's instant passes), which refuses only what no reclaim chose, so it is not refusedWrong.
DCheck(d) ==
  /\ dpc[d] = "check"
  /\ IF d = "branch" /\ ~NameFree
       THEN /\ DGo(d, "done") /\ pins' = pins \ {<<d, dv[d]>>} /\ UNCHANGED refusedWrong
     ELSE IF Key(dv[d]) \in TombKeys
       THEN /\ DGo(d, "done") /\ pins' = pins \ {<<d, dv[d]>>}
            /\ refusedWrong' = (refusedWrong \/ dv[d] \notin chosen)
       ELSE /\ DGo(d, IF On("PinFirst") THEN "act" ELSE "latepin") /\ UNCHANGED <<pins, refusedWrong>>
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, tagged, tomb, dv, faults>> /\ DKeep

DLatePin(d) ==
  /\ dpc[d] = "latepin"
  /\ pins' = pins \cup {<<d, dv[d]>>}
  /\ DGo(d, "act")
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, tagged, tomb, dv, faults, refusedWrong>> /\ DKeep

\* Lance's tag create, branch create and restore each read the version's manifest: one whose manifest is gone fails.
DAct(d) ==
  /\ dpc[d] = "act"
  /\ LET v == dv[d]
         lands == v \in manifests /\ (d # "restore" \/ mhead < MaxMain) /\ (d # "branch" \/ (binc = "none" /\ Incs \ used # {}))
         i == CHOOSE x \in Incs \ used : TRUE
     IN IF ~lands
          THEN UNCHANGED <<manifests, names, mhead, binc, bfork, bhead, used, tagged>>
          ELSE CASE d = "tag" -> tagged' = tagged \cup {v} /\ UNCHANGED <<manifests, names, mhead, binc, bfork, bhead, used>>
                 [] d = "restore" -> /\ manifests' = manifests \cup {MainV(mhead + 1)} /\ mhead' = mhead + 1
                                     /\ names' = [names EXCEPT ![MainV(mhead + 1)] = names[v]]
                                     /\ UNCHANGED <<binc, bfork, bhead, used, tagged>>
                 [] OTHER -> /\ binc' = i /\ used' = used \cup {i} /\ bfork' = [bfork EXCEPT ![i] = v[2]] /\ bhead' = [bhead EXCEPT ![i] = v[2]]
                             /\ UNCHANGED <<manifests, names, mhead, tagged>>
  /\ DGo(d, "unpin")
  /\ UNCHANGED <<files, pins, tomb, dv, faults, refusedWrong>> /\ DKeep

DUnpin(d) ==
  /\ dpc[d] = "unpin"
  /\ pins' = pins \ {<<d, dv[d]>>}
  /\ DGo(d, "done")
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, tagged, tomb, dv, faults, refusedWrong>> /\ DKeep

\* A door that dies leaves its pin.
DCrash(d) ==
  /\ dpc[d] \in {"check", "latepin", "act", "unpin"} /\ Fault
  /\ DGo(d, "dead")
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, tagged, pins, tomb, dv, refusedWrong>> /\ DKeep

\* A sweep of pins: of a dead door's only (A3'), or of any (which must fail). The release's pins are of a kind the sweep
\* never takes (ReleasePinKind): a release is no door, keeps no deadline and resumes its recorded plan whenever its tick
\* runs, so a stalled release is not a dead one. Without ReleasePinKind the sweep takes the pin of a release whose tick
\* has stalled, as it takes a dead door's.
Sweep ==
  /\ PinSweep # "none"
  /\ \E p \in pins :
       /\ \/ p[1] \in RefDoors /\ (PinSweep = "any" \/ dpc[p[1]] = "dead")
          \/ p[1] = "rel" /\ ~On("ReleasePinKind") /\ relpc \in {"dead-recreating", "dead-claimed"}
       /\ pins' = pins \ {p}
  /\ UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, tomb, chosen, policy, answered>>
  /\ UNCHANGED <<R, D, wpc, relpc, relat, faults, badDelete, refusedWrong>>

\* ---------------------------------------------------------------- the policy doors
\* The door writes its hold, then reads the record, and answers 200 naming what is condemned: it promises every other
\* standing version. Without HoldWritesFirst it reads the record first.
Promise(cond) == {v \in manifests : Stands(v) /\ Whole(v) /\ v \notin cond}
PKeep == UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, pins, tomb, chosen>>
          /\ UNCHANGED <<R, dv, vdv, wpc, relpc, relat, faults, badDelete, refusedWrong>>

PWrite(d) ==
  /\ dpc[d] = "idle" /\ On("HoldWritesFirst")
  /\ policy' = [policy EXCEPT ![LevelOf(d)] = "hold"]
  /\ answered' = answered \cup Promise(CondemnedVs)
  /\ DGo(d, "done") /\ UNCHANGED dcond /\ PKeep
PRead(d) ==
  /\ dpc[d] = "idle" /\ ~On("HoldWritesFirst")
  /\ dcond' = [dcond EXCEPT ![d] = CondemnedVs]
  /\ DGo(d, "write") /\ UNCHANGED <<policy, answered>> /\ PKeep
PLate(d) ==
  /\ dpc[d] = "write"
  /\ policy' = [policy EXCEPT ![LevelOf(d)] = "hold"]
  /\ answered' = answered \cup Promise(dcond[d])
  /\ DGo(d, "done") /\ UNCHANGED dcond /\ PKeep

\* version/delete: the version it names (any standing version but a head) condemned, then (VDeleteCondemnsFirst) the
\* tags and pins read and its entry dropped where one names the version (the door answers 409, naming it); nothing
\* deleted. A reclaim finishes what stands condemned unless a hold keeps it. Without VDeleteCondemnsFirst it reads the
\* tags first, and a tag door that pinned after that read and read the record before the condemn lands its tag on a
\* version left condemned for good.
VKeep == UNCHANGED <<manifests, names, files, mhead, binc, bfork, bhead, used, expired, tagged, pins, policy, answered>>
         /\ UNCHANGED <<R, dv, dcond, wpc, relpc, relat, faults, badDelete, refusedWrong>>
VTargets == {x \in manifests : Stands(x) /\ x \notin Heads /\ x \notin CondemnedVs}
VCondemn ==
  /\ dpc["vdelete"] = "idle" /\ On("VDeleteCondemnsFirst")
  /\ \E v \in VTargets :
       /\ tomb' = tomb \cup {[k |-> Key(v), b |-> "vd", s |-> "c", l |-> FALSE]}
       /\ chosen' = chosen \cup {v} /\ vdv' = v
  /\ DGo("vdelete", "read") /\ VKeep
VRead ==
  /\ dpc["vdelete"] = "read"
  /\ tomb' = IF vdv \in tagged \cup PinVs THEN {e \in tomb : ~(e.b = "vd" /\ e.k = Key(vdv))} ELSE tomb
  /\ DGo("vdelete", "done") /\ UNCHANGED <<chosen, vdv>> /\ VKeep
VReadFirst ==
  /\ dpc["vdelete"] = "idle" /\ ~On("VDeleteCondemnsFirst")
  /\ \E v \in VTargets : vdv' = v /\ DGo("vdelete", IF v \in tagged \cup PinVs THEN "done" ELSE "write")
  /\ UNCHANGED <<tomb, chosen>> /\ VKeep
VLate ==
  /\ dpc["vdelete"] = "write"
  /\ tomb' = tomb \cup {[k |-> Key(vdv), b |-> "vd", s |-> "c", l |-> FALSE]}
  /\ chosen' = chosen \cup {vdv}
  /\ DGo("vdelete", "done") /\ UNCHANGED vdv /\ VKeep
VDelete == VCondemn \/ VRead \/ VReadFirst \/ VLate

\* A branch delete: Lance refuses one a tag (a pin among them) names a version of.
BDelete ==
  /\ dpc["bdelete"] = "idle" /\ binc # "none" /\ ~ReleaseHolds
  /\ \A v \in tagged \cup PinVs : v[1] # binc
  /\ binc' = "none"
  /\ DGo("bdelete", "done")
  /\ UNCHANGED <<manifests, names, files, mhead, bfork, bhead, used, expired, tagged, pins, tomb, chosen, policy, answered>>
  /\ UNCHANGED <<R, dv, dcond, vdv, wpc, relpc, relat, faults, badDelete, refusedWrong>>

\* ---------------------------------------------------------------- the writers
WKeep == UNCHANGED <<binc, bfork, bhead, used, expired, tagged, pins, tomb, chosen, policy, answered>>
          /\ UNCHANGED <<R, D, relpc, relat, faults, badDelete, refusedWrong>>
WStage ==
  /\ wpc["writer"] = "idle" /\ mhead < MaxMain
  /\ files' = files \cup {"w"} /\ wpc' = [wpc EXCEPT !["writer"] = "staged"]
  /\ UNCHANGED <<manifests, names, mhead>> /\ WKeep
WCommit ==
  /\ wpc["writer"] = "staged" /\ mhead < MaxMain
  /\ manifests' = manifests \cup {MainV(mhead + 1)} /\ mhead' = mhead + 1
  /\ names' = [names EXCEPT ![MainV(mhead + 1)] = names[MainV(mhead)] \cup {"w"}]
  /\ wpc' = [wpc EXCEPT !["writer"] = "done"]
  /\ UNCHANGED files /\ WKeep
\* A commit on branch b: a version of its own, naming a file of its own (the erasure's rewrite).
BWrite ==
  /\ wpc["bwriter"] = "idle" /\ binc # "none" /\ bhead[binc] < bfork[binc] + 2 /\ bhead[binc] < MaxOwn
  /\ LET n == bhead[binc] + 1 IN
       /\ manifests' = manifests \cup {<<binc, n>>}
       /\ files' = files \cup {OwnFile(binc, n)}
       /\ bhead' = [bhead EXCEPT ![binc] = n]
  /\ UNCHANGED <<names, mhead, binc, bfork, used, expired, tagged, pins, tomb, chosen, policy, answered>>
  /\ UNCHANGED <<R, D, wpc, relpc, relat, faults, badDelete, refusedWrong>>

\* ---------------------------------------------------------------- the erasure's release
\* It holds RELEASE (no ref door beside it), records main's head as where branch b comes back, (ReleasePins) pins it and
\* reads the record, deletes b, makes b again at the recorded version, and unpins. A tick that dies resumes from its
\* record; its pin stands meanwhile.
LKeep == UNCHANGED <<manifests, names, files, mhead, expired, tagged, tomb, chosen, policy, answered>>
          /\ UNCHANGED <<R, D, wpc, badDelete, refusedWrong>>
RelClaim ==
  /\ relpc = "idle" /\ ~DoorInFlight /\ binc # "none"
  /\ relat' = mhead
  /\ pins' = IF On("ReleasePins") THEN pins \cup {<<"rel", MainV(mhead)>>} ELSE pins
  /\ relpc' = "check"
  /\ UNCHANGED <<binc, bfork, bhead, used, faults>> /\ LKeep
RelCheck ==
  /\ relpc = "check"
  /\ IF On("ReleasePins") /\ Key(MainV(relat)) \in TombKeys
       THEN relpc' = "idle" /\ pins' = {p \in pins : p[1] # "rel"}
       ELSE relpc' = "delete" /\ UNCHANGED pins
  /\ UNCHANGED <<relat, binc, bfork, bhead, used, faults>> /\ LKeep
RelDelete ==
  /\ relpc = "delete" /\ binc # "none"
  /\ \A v \in tagged : v[1] # binc
  /\ binc' = "none" /\ relpc' = "recreate"
  /\ UNCHANGED <<relat, pins, bfork, bhead, used, faults>> /\ LKeep
\* Lance's create_branch reads the recorded version's manifest; one gone fails, and the tick goes no further. A live
\* licence under the name holds the recreate off as it refuses a door's (NameGuard): the tick fails and the next resumes.
RelRecreate ==
  /\ relpc = "recreate" /\ binc = "none" /\ Incs \ used # {} /\ MainV(relat) \in manifests /\ NameFree
  /\ LET i == CHOOSE x \in Incs \ used : TRUE IN
       /\ binc' = i /\ used' = used \cup {i} /\ bfork' = [bfork EXCEPT ![i] = relat] /\ bhead' = [bhead EXCEPT ![i] = relat]
  /\ relpc' = "finish"
  /\ UNCHANGED <<relat, pins, faults>> /\ LKeep
RelFinish ==
  /\ relpc = "finish"
  /\ pins' = {p \in pins : p[1] # "rel"} /\ relpc' = "done"
  /\ UNCHANGED <<relat, binc, bfork, bhead, used, faults>> /\ LKeep
\* The tick dies: its hold lapses, and the next tick resumes where the record says ("recreating" once it deleted).
RelCrash ==
  /\ relpc \in {"check", "delete", "recreate", "finish"} /\ Fault
  /\ relpc' = IF relpc \in {"recreate", "finish"} THEN "dead-recreating" ELSE "dead-claimed"
  /\ UNCHANGED <<relat, pins, binc, bfork, bhead, used>> /\ LKeep
RelResume ==
  /\ relpc \in {"dead-recreating", "dead-claimed"} /\ ~DoorInFlight
  /\ relpc' = IF relpc = "dead-recreating" THEN "recreate" ELSE "check"
  /\ UNCHANGED <<relat, pins, binc, bfork, bhead, used, faults>> /\ LKeep

\* ---------------------------------------------------------------- the spec
RStep(r) == RBegin(r) \/ RCondemn(r) \/ RPins(r) \/ RLevel(r, 1) \/ RLevel(r, 2) \/ RLevel(r, 3) \/ RPromote(r) \/ RVerify(r)
            \/ RSnapT(r) \/ RSnapM(r) \/ RDecide(r) \/ RData(r) \/ RMan(r) \/ RBase(r) \/ RBDel(r) \/ RCredDead(r)
DStep(d) == DPin(d) \/ DCheck(d) \/ DLatePin(d) \/ DAct(d) \/ DUnpin(d)
PStep(d) == PWrite(d) \/ PRead(d) \/ PLate(d)
RelStep == RelClaim \/ RelCheck \/ RelDelete \/ RelRecreate \/ RelFinish \/ RelResume

Next ==
  \/ \E r \in Recl : RStep(r) \/ RCrash(r) \/ RRefused(r) \/ RRestart(r)
  \/ \E d \in RefDoors \cap Doors : DStep(d) \/ DCrash(d)
  \/ \E d \in HoldDoors \cap Doors : PStep(d)
  \/ VDelete \/ BDelete \/ WStage \/ WCommit \/ BWrite
  \/ RelStep \/ RelCrash
  \/ GC \/ Age \/ Sweep \/ LicenceEnds

Fairness ==
  /\ \A r \in Recl : WF_vars(RStep(r)) /\ WF_vars(RRestart(r))
  /\ \A d \in RefDoors : WF_vars(DStep(d))
  /\ \A d \in HoldDoors : WF_vars(PStep(d))
  /\ WF_vars(GC) /\ WF_vars(WStage \/ WCommit) /\ WF_vars(RelStep)

Spec == Init /\ [][Next]_vars /\ Fairness

ReclSymmetry == Permutations(Recl)

\* ---------------------------------------------------------------- properties
\* Nothing live is deleted: main's head, every tag and branch b's head stand with every file they name.
RefsWhole == /\ Whole(HeadOf("m"))
             /\ \A v \in tagged : Stands(v) => Whole(v)
             /\ binc # "none" => Whole(HeadOf(binc))
\* A commit in flight keeps the files it wrote until it lands (A2).
InFlightKept == wpc["writer"] = "staged" => "w" \in files
\* A hold answered 200 keeps every version it did not name as condemned.
RetentionKept == \A v \in answered : Stands(v) => Whole(v)
\* A version standing over deleted files is named by an entry, which every ref door reads.
HalfOnlyCondemned == \A v \in manifests : (Stands(v) /\ ~Whole(v)) => v \in CondemnedVs
\* Every delete is of a version a reclaim of its own ref chose, version/delete named or an erasure's fork cleanup took,
\* and an entry names it as it goes.
DeletesCondemned == ~badDelete
\* A door refuses only a version some reclaim, version/delete or fork cleanup chose.
RefusedOnlyChosen == ~refusedWrong
\* A version is condemned only by a batch whose reclaimer listed it on its own incarnation (or that version/delete named,
\* or a fork cleanup took): every entry names a version some reclaim chose, never another incarnation's of its number.
CondemnsAsListed == \A e \in tomb : e.k \in {Key(v) : v \in chosen}
\* Once version/delete has answered, none of its entries names a version a caller's tag names: a tag on a condemned
\* version would hide it from version/list and keep it condemned for as long as the tag stands.
VDeleteSparesTags == dpc["vdelete"] = "done" => \A e \in tomb : e.b = "vd" => Resolve(e.k) \notin tagged

\* Once faults stop, condemned versions are finished: no standing version stays over deleted files, unless a hold keeps it.
EventuallyWhole == <>[](Held \/ \A v \in manifests : Stands(v) => Whole(v))
\* Every policy door lands: none is ever refused.
PolicyLands == \A d \in HoldDoors \cap Doors : <>(dpc[d] = "done")
=============================================================================
