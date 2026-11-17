------------------------------- MODULE Grants -------------------------------
(***************************************************************************)
(* The catalog's authorization state beside its objects (ADR 0241): the    *)
(* tuples OpenFGA holds, a second store the object store's conditional     *)
(* writes do not arbitrate, written and revoked by the catalog's doors     *)
(* beside the registry records and tables they are about. Three parts,     *)
(* each checked by a config of its own (Parts); a part a config leaves out *)
(* stays at its initial state.                                             *)
(*                                                                         *)
(* PART "container": one project or warehouse id, named in OpenFGA by the  *)
(* id itself (`project:<id>`, `warehouse:<id>`), deleted and created again *)
(* by another tenant (incarnations 1..MaxInc). Each tuple carries, as a    *)
(* history variable the code never sees, the incarnation its writer was    *)
(* authorized on.                                                          *)
(*   Mint     the registry record minted with a fresh incarnation          *)
(*            (projects.establish_project, warehouses.establish_warehouse) *)
(*   Bear     its birth, ONE OpenFGA Write: the incarnation's `revision`   *)
(*            marker, its token and its seed (the creator's admin or owner *)
(*            and the tenancy edge; fga.bear, fga_deps.seed_project_admin, *)
(*            fga_deps.seed_warehouse). A re-POST of a record whose birth  *)
(*            never landed bears it again (Resume)                         *)
(*   Grant    a door grant on the container (members.grant_member,         *)
(*            access._access_mutate, a declared grant, the tenancy         *)
(*            rebuild): reads the token, asks the granter's authority, and *)
(*            writes (fga.write_fenced)                                    *)
(*   Delete   projects.delete_project / warehouses.delete_warehouse: marks *)
(*            its incarnation born, retires its token (fga.retire), reads  *)
(*            the tuples and revokes them one by one                       *)
(*            (fga.revoke_object_tuples), and removes the record last      *)
(*   Repair   the drift repair's ghost revoke (grant_reconcile.            *)
(*            revoke_ghost_grants): revokes what stands on an object no    *)
(*            record names                                                 *)
(*                                                                         *)
(* PART "table": one table name, its tables keyed by a minted uuid         *)
(* (fga.table_object, ADR 0173): a create seeds the uuid's owner, a purge  *)
(* revokes them, a recoverable drop keeps them for the undrop, the trash   *)
(* release revokes them past the deadline (trash_reclaim.release_trash),   *)
(* and a late grant decided on one table lands after it is gone. A reader  *)
(* decides on what OpenFGA answers (fga.check).                            *)
(*                                                                         *)
(* PART "converge": one tuple git may declare (declaration_converge.       *)
(* _Converger._grant / _revoke) beside the same tuple granted at a door.   *)
(*                                                                         *)
(* THE GUARDS, each removed by a config that must fail:                    *)
(*   G1 Fence: a grant on a container commits in one Write that moves the  *)
(*      token of the incarnation its writer read, so no grant decided on   *)
(*      one incarnation lands on another (GrantsUnfenced, NoLeakAcross-    *)
(*      Incarnations).                                                     *)
(*   G2 RetireFirst: a delete retires the token before it reads what to    *)
(*      revoke, so no grant lands between its read and its end             *)
(*      (GrantsRetireLast, NoLeakAcrossIncarnations).                      *)
(*   G3 BornKept: a delete marks its incarnation born before it retires,   *)
(*      a birth commits only unborn, and the marker is never revoked, so a *)
(*      birth stalled across the delete cannot bear the old incarnation    *)
(*      under the new one (GrantsRebirth, NoLeakAcrossIncarnations).       *)
(*   G4 ReadFirst: the ghost revoke reads what stands before it judges     *)
(*      the record absent, so it revokes nothing a create born after its   *)
(*      judgement wrote (GrantsRepairJudgesFirst, OwnedWhileLive).         *)
(*   G5 OwnOnly: the converge records a tuple as git's only when its own   *)
(*      write created it (GrantsConvergeAdopts, GitRevokesOnlyItsOwn).     *)
(* THE ASSUMPTIONS, each removed by a config that must fail:               *)
(*   A1 UuidKeyed: a table's tuples name its uuid, never its name          *)
(*      (GrantsTableByName, TableNoLeak).                                  *)
(*   A2 UndropSettles: an undrop admitted before the deadline registers   *)
(*      before the release may act (trash.RELEASE_SETTLE; GrantsUndrop-    *)
(*      Outlasts, TableOwnedWhileLive).                                    *)
(*   A3 FreshReads: every decision reads at HIGHER_CONSISTENCY             *)
(*      (fga._fresh_read_options; GrantsCachedCheck, NoAccessAfterRevoke). *)
(*                                                                         *)
(* NOT MODELLED: a token's counter (two grants of one incarnation meeting  *)
(* at its token: one is refused and reads again, a liveness matter); the   *)
(* last-admin guard (ADR 0213, its own CAS on the same token); roles,      *)
(* principals and labels; a grant's answer (an OpenFGA timeout leaves the  *)
(* write's outcome unknown, which here is a write that commits or not).    *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Parts,         \* which parts this config checks: a subset of {"container", "table", "converge"}
          MaxInc,        \* incarnations of the container id
          Grantees,      \* door grant requests on the container
          Deaths,        \* a door may stop between its steps and never take the next
          Fence,         \* G1
          RetireFirst,   \* G2
          BornKept,      \* G3
          ReadFirst,     \* G4
          OwnOnly,       \* G5
          UuidKeyed,     \* A1
          UndropSettles, \* A2
          FreshReads     \* A3

Incs == 1 .. MaxInc
On(part) == part \in Parts

\* ---------------------------------------------------------------- container
VARIABLES rec,     \* the registry record: its incarnation (0: none) and whether a delete started on it
          nextInc, \* the incarnation the next mint takes
          tup,     \* the tuples on the container: [k: "seed" or "grant", g, for: the incarnation decided on]
          tokens,  \* the incarnations whose token stands
          born,    \* the incarnations whose `revision` marker stands
          cpc,     \* each incarnation's create: "idle", "minted", "done", "dead"
          gpc,     \* each grant: "idle", "read", "checked", "done", "dead"
          gu,      \* the token incarnation a grant read
          gauth,   \* the incarnation whose seed authorized it
          dstep,   \* the delete: 0 idle, 1..5 the next of its steps, 6 dead
          du,      \* the incarnation it deletes
          dsnap,   \* what it read to revoke
          rpc,     \* the repair: "idle", "a", "b", "c", "done"
          rsnap,   \* what it read to revoke
          rok      \* whether its judgement found no record

\* -------------------------------------------------------------------- table
VARIABLES live,    \* the uuid the name holds (0: none)
          tnext,   \* the uuid the next create mints
          ttup,    \* the tuples: [obj: its key, g: "seed" or "grant", for: the uuid decided on]
          trashed, \* the uuids a recoverable drop keeps in the trash, grants and all
          tphase,  \* the deadline: "grace", then "settled"
          upc, uu, \* the undrop: "idle", "admitted", "done"; the uuid it restores
          relpc,   \* the release: "idle", "checked", "done"
          ru,      \* the uuid whose trash record it judged
          tgpc,    \* a late grant: "idle", "checked", "done"
          tgfor,   \* the uuid it was decided on
          everHeld,\* whether the grant ever stood (what a stale cache may still answer)
          revoked, \* whether a revoke of it answered
          bad      \* whether a reader was allowed by it after that

\* ----------------------------------------------------------------- converge
VARIABLES declared, \* git declares the tuple: "not yet", "yes", "dropped"
          held,     \* the tuple stands
          first,    \* who created it: "none", "git", "door"
          recorded, \* the declaration's ledger names it git's
          cvpc,     \* the converge: "idle", "read", "recorded"
          doorpc,   \* the door grant: "idle", "done"
          wrong     \* the converge revoked a tuple a door created

cvars == <<rec, nextInc, tup, tokens, born, cpc, gpc, gu, gauth, dstep, du, dsnap, rpc, rsnap, rok>>
tvars == <<live, tnext, ttup, trashed, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>
vvars == <<declared, held, first, recorded, cvpc, doorpc, wrong>>
vars == <<cvars, tvars, vvars>>

MaxUuid == 2

Init ==
  /\ rec = [inc |-> 0, deleting |-> FALSE] /\ nextInc = 1 /\ tup = {} /\ tokens = {} /\ born = {}
  /\ cpc = [i \in Incs |-> "idle"] /\ gpc = [g \in Grantees |-> "idle"] /\ gu = [g \in Grantees |-> 0]
  /\ gauth = [g \in Grantees |-> 0] /\ dstep = 0 /\ du = 0 /\ dsnap = {} /\ rpc = "idle" /\ rsnap = {} /\ rok = FALSE
  /\ live = 0 /\ tnext = 1 /\ ttup = {} /\ trashed = {} /\ tphase = "grace" /\ upc = "idle" /\ uu = 0 /\ relpc = "idle" /\ ru = 0
  /\ tgpc = "idle" /\ tgfor = 0 /\ everHeld = FALSE /\ revoked = FALSE /\ bad = FALSE
  /\ declared = "not yet" /\ held = FALSE /\ first = "none" /\ recorded = FALSE /\ cvpc = "idle" /\ doorpc = "idle" /\ wrong = FALSE

Seed(i) == [k |-> "seed", g |-> "none", for |-> i]
Live == rec.inc # 0 /\ ~rec.deleting

\* ---------------------------------------------------------------- container

\* A create mints the record when the id has none: a project or warehouse made again under a deleted id.
Mint(i) ==
  /\ On("container") /\ cpc[i] = "idle" /\ rec.inc = 0 /\ nextInc = i
  /\ rec' = [inc |-> i, deleting |-> FALSE] /\ nextInc' = i + 1 /\ cpc' = [cpc EXCEPT ![i] = "minted"]
  /\ UNCHANGED <<tup, tokens, born, gpc, gu, gauth, dstep, du, dsnap, rpc, rsnap, rok>>

\* The birth: one Write of the marker, the token and the seed, which the store refuses whole once the marker stands.
Bear(i) ==
  /\ On("container") /\ cpc[i] = "minted"
  /\ IF BornKept /\ i \in born
       THEN /\ cpc' = [cpc EXCEPT ![i] = "dead"] /\ UNCHANGED <<tup, tokens, born>>
       ELSE /\ born' = born \cup {i} /\ tokens' = tokens \cup {i} /\ tup' = tup \cup {Seed(i)}
            /\ cpc' = [cpc EXCEPT ![i] = "done"]
  /\ UNCHANGED <<rec, nextInc, gpc, gu, gauth, dstep, du, dsnap, rpc, rsnap, rok>>

\* A re-POST of a record whose birth never landed: it reads the record standing and bears it again.
Resume(i) ==
  /\ On("container") /\ cpc[i] = "dead" /\ rec.inc = i /\ ~rec.deleting /\ i \notin born
  /\ cpc' = [cpc EXCEPT ![i] = "minted"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, born, gpc, gu, gauth, dstep, du, dsnap, rpc, rsnap, rok>>

\* A grant reads the token it will write under (any standing; unfenced, it reads none).
GrantRead(g) ==
  /\ On("container") /\ gpc[g] = "idle"
  /\ IF Fence THEN \E u \in tokens : gu' = [gu EXCEPT ![g] = u] ELSE UNCHANGED gu
  /\ gpc' = [gpc EXCEPT ![g] = "read"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, born, cpc, gauth, dstep, du, dsnap, rpc, rsnap, rok>>

\* The granter's authority, asked of OpenFGA after the token is read: a seed standing on the container.
GrantCheck(g) ==
  /\ On("container") /\ gpc[g] = "read"
  /\ \E t \in tup : t.k = "seed" /\ gauth' = [gauth EXCEPT ![g] = t.for]
  /\ gpc' = [gpc EXCEPT ![g] = "checked"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, born, cpc, gu, dstep, du, dsnap, rpc, rsnap, rok>>

GrantWrite(g) ==
  /\ On("container") /\ gpc[g] = "checked"
  /\ IF Fence /\ gu[g] \notin tokens
       THEN UNCHANGED tup
       ELSE tup' = tup \cup {[k |-> "grant", g |-> g, for |-> gauth[g]]}
  /\ gpc' = [gpc EXCEPT ![g] = "done"]
  /\ UNCHANGED <<rec, nextInc, tokens, born, cpc, gu, gauth, dstep, du, dsnap, rpc, rsnap, rok>>

GrantDies(g) ==
  /\ On("container") /\ Deaths /\ gpc[g] \in {"read", "checked"}
  /\ gpc' = [gpc EXCEPT ![g] = "dead"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, born, cpc, gu, gauth, dstep, du, dsnap, rpc, rsnap, rok>>

CreateDies(i) ==
  /\ On("container") /\ Deaths /\ cpc[i] = "minted"
  /\ cpc' = [cpc EXCEPT ![i] = "dead"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, born, gpc, gu, gauth, dstep, du, dsnap, rpc, rsnap, rok>>

\* The delete's steps, in its order: marked born, token retired, tuples read, tuples revoked, record removed.
Steps == IF RetireFirst THEN <<"born", "retire", "read", "revoke", "record">> ELSE <<"born", "read", "revoke", "retire", "record">>

DeleteStart ==
  /\ On("container") /\ dstep \in {0, 6} /\ rec.inc # 0
  /\ rec' = [rec EXCEPT !.deleting = TRUE] /\ du' = rec.inc /\ dstep' = 1
  /\ UNCHANGED <<nextInc, tup, tokens, born, cpc, gpc, gu, gauth, dsnap, rpc, rsnap, rok>>

DeleteStep ==
  /\ On("container") /\ dstep \in 1 .. 5
  /\ LET s == Steps[dstep] IN
       /\ born' = IF s = "born" /\ BornKept THEN born \cup {du} ELSE born
       /\ tokens' = IF s = "retire" THEN tokens \ {du} ELSE tokens
       /\ dsnap' = IF s = "read" THEN tup ELSE dsnap
       /\ tup' = IF s = "revoke" THEN tup \ dsnap ELSE tup
       /\ rec' = IF s = "record" THEN [inc |-> 0, deleting |-> FALSE] ELSE rec
  /\ dstep' = IF dstep = 5 THEN 0 ELSE dstep + 1
  /\ UNCHANGED <<nextInc, cpc, gpc, gu, gauth, du, rpc, rsnap, rok>>

DeleteDies ==
  /\ On("container") /\ Deaths /\ dstep \in 1 .. 5
  /\ dstep' = 6
  /\ UNCHANGED <<rec, nextInc, tup, tokens, born, cpc, gpc, gu, gauth, du, dsnap, rpc, rsnap, rok>>

\* The ghost revoke: judge the record absent and read what stands, in its order, then revoke what it read (the
\* `revision` relation, marker and token, is never a ghost's: it confers nothing).
RepairStep ==
  /\ On("container") /\ rpc \in {"idle", "a", "b"}
  /\ LET judge == (ReadFirst /\ rpc = "a") \/ (~ReadFirst /\ rpc = "idle")
         read == (ReadFirst /\ rpc = "idle") \/ (~ReadFirst /\ rpc = "a")
     IN /\ rok' = IF judge THEN rec.inc = 0 ELSE rok
        /\ rsnap' = IF read THEN tup ELSE rsnap
        /\ rpc' = IF judge /\ rec.inc # 0 THEN "done" ELSE IF rpc = "idle" THEN "a" ELSE "b"
  /\ UNCHANGED <<rec, nextInc, tup, tokens, born, cpc, gpc, gu, gauth, dstep, du, dsnap>>

RepairRevoke ==
  /\ On("container") /\ rpc = "b" /\ rok
  /\ tup' = tup \ rsnap /\ rpc' = "done"
  /\ UNCHANGED <<rec, nextInc, tokens, born, cpc, gpc, gu, gauth, dstep, du, dsnap, rsnap, rok>>

ContainerNext ==
  \/ \E i \in Incs : Mint(i) \/ Bear(i) \/ Resume(i) \/ CreateDies(i)
  \/ \E g \in Grantees : GrantRead(g) \/ GrantCheck(g) \/ GrantWrite(g) \/ GrantDies(g)
  \/ DeleteStart \/ DeleteStep \/ DeleteDies \/ RepairStep \/ RepairRevoke

\* -------------------------------------------------------------------- table

Key(u) == IF UuidKeyed THEN u ELSE 0
TSeed(u) == [obj |-> Key(u), g |-> "seed", for |-> u]
Undropping == upc = "admitted"

TCreate ==
  /\ On("table") /\ live = 0 /\ tnext <= MaxUuid
  /\ live' = tnext /\ tnext' = tnext + 1 /\ ttup' = ttup \cup {TSeed(tnext)}
  /\ UNCHANGED <<trashed, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>

\* A grant decided on the table the name holds, its write landing whenever it lands.
TGrantCheck ==
  /\ On("table") /\ tgpc = "idle" /\ live # 0
  /\ tgfor' = live /\ tgpc' = "checked"
  /\ UNCHANGED <<live, tnext, ttup, trashed, tphase, upc, uu, relpc, ru, everHeld, revoked, bad>>

TGrantWrite ==
  /\ On("table") /\ tgpc = "checked"
  /\ ttup' = ttup \cup {[obj |-> Key(tgfor), g |-> "grant", for |-> tgfor]} /\ everHeld' = TRUE /\ tgpc' = "done"
  /\ UNCHANGED <<live, tnext, trashed, tphase, upc, uu, relpc, ru, tgfor, revoked, bad>>

\* The access door's revoke of that grant; it answers once the tuple is gone.
TRevoke ==
  /\ On("table") /\ tgpc = "done" /\ ~revoked
  /\ ttup' = {t \in ttup : t.g # "grant"} /\ revoked' = TRUE
  /\ UNCHANGED <<live, tnext, trashed, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, bad>>

\* A reader asks whether the grant opens the table; a cache may answer from before the revoke.
TDecide ==
  /\ On("table") /\ live # 0
  /\ LET stands == \E t \in ttup : t.g = "grant" /\ t.obj = Key(live)
         allowed == IF FreshReads THEN stands ELSE stands \/ everHeld
     IN bad' = (bad \/ (allowed /\ revoked))
  /\ UNCHANGED <<live, tnext, ttup, trashed, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked>>

TPurge ==
  /\ On("table") /\ live # 0
  /\ ttup' = {t \in ttup : t.obj # Key(live)} /\ live' = 0
  /\ UNCHANGED <<tnext, trashed, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>

TDropRecoverable ==
  /\ On("table") /\ live # 0 /\ tphase = "grace" /\ trashed = {}
  /\ trashed' = {live} /\ live' = 0
  /\ UNCHANGED <<tnext, ttup, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>

TUndropAdmit ==
  /\ On("table") /\ upc = "idle" /\ tphase = "grace" /\ trashed # {}
  /\ upc' = "admitted" /\ uu' = CHOOSE u \in trashed : TRUE
  /\ UNCHANGED <<live, tnext, ttup, trashed, tphase, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>

\* The undrop registers the table under its name and then clears its record; a name taken refuses it.
TUndropRegister ==
  /\ On("table") /\ upc = "admitted"
  /\ IF live = 0 THEN live' = uu /\ trashed' = trashed \ {uu} ELSE UNCHANGED <<live, trashed>>
  /\ upc' = "done"
  /\ UNCHANGED <<tnext, ttup, tphase, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>

TTick ==
  /\ On("table") /\ tphase = "grace" /\ (UndropSettles => ~Undropping)
  /\ tphase' = "settled"
  /\ UNCHANGED <<live, tnext, ttup, trashed, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>

\* The release: refused while a live table holds the name, then the trashed uuid's grants revoked.
TReleaseCheck ==
  /\ On("table") /\ relpc = "idle" /\ tphase = "settled" /\ trashed # {} /\ live = 0
  /\ relpc' = "checked" /\ ru' = CHOOSE u \in trashed : TRUE
  /\ UNCHANGED <<live, tnext, ttup, trashed, tphase, upc, uu, tgpc, tgfor, everHeld, revoked, bad>>

TRelease ==
  /\ On("table") /\ relpc = "checked"
  /\ ttup' = {t \in ttup : t.obj # Key(ru)} /\ relpc' = "done"
  /\ UNCHANGED <<live, tnext, trashed, tphase, upc, uu, ru, tgpc, tgfor, everHeld, revoked, bad>>

TableNext ==
  TCreate \/ TGrantCheck \/ TGrantWrite \/ TRevoke \/ TDecide \/ TPurge \/ TDropRecoverable
    \/ TUndropAdmit \/ TUndropRegister \/ TTick \/ TReleaseCheck \/ TRelease

\* ----------------------------------------------------------------- converge

GitDeclares == /\ On("converge") /\ declared = "not yet" /\ declared' = "yes" /\ UNCHANGED <<held, first, recorded, cvpc, doorpc, wrong>>
GitDrops == /\ On("converge") /\ declared = "yes" /\ declared' = "dropped" /\ UNCHANGED <<held, first, recorded, cvpc, doorpc, wrong>>

\* The tick's read of the store, again right before the record: a tuple it finds is somebody's, and is left so.
ConvRead ==
  /\ On("converge") /\ cvpc = "idle" /\ declared = "yes" /\ ~held
  /\ cvpc' = "read"
  /\ UNCHANGED <<declared, held, first, recorded, doorpc, wrong>>

\* Recorded before the tuple, so a tuple written and never recorded cannot outlive its declaration.
ConvRecord ==
  /\ On("converge") /\ cvpc = "read"
  /\ recorded' = TRUE /\ cvpc' = "recorded"
  /\ UNCHANGED <<declared, held, first, doorpc, wrong>>

\* The write: guarded, it commits only where the tuple is absent and forgets its record where one stands.
ConvWrite ==
  /\ On("converge") /\ cvpc = "recorded"
  /\ IF held
       THEN /\ recorded' = (IF OwnOnly THEN FALSE ELSE recorded) /\ UNCHANGED <<held, first>>
       ELSE /\ held' = TRUE /\ first' = "git" /\ UNCHANGED recorded
  /\ cvpc' = "idle"
  /\ UNCHANGED <<declared, doorpc, wrong>>

ConvRevoke ==
  /\ On("converge") /\ cvpc = "idle" /\ declared = "dropped" /\ recorded
  /\ wrong' = (wrong \/ (held /\ first = "door"))
  /\ held' = FALSE /\ first' = "none" /\ recorded' = FALSE
  /\ UNCHANGED <<declared, cvpc, doorpc>>

DoorGrants ==
  /\ On("converge") /\ doorpc = "idle"
  /\ IF held THEN UNCHANGED <<held, first>> ELSE held' = TRUE /\ first' = "door"
  /\ doorpc' = "done"
  /\ UNCHANGED <<declared, recorded, cvpc, wrong>>

ConvergeNext == GitDeclares \/ GitDrops \/ ConvRead \/ ConvRecord \/ ConvWrite \/ ConvRevoke \/ DoorGrants

Next ==
  \/ ContainerNext /\ UNCHANGED <<tvars, vvars>>
  \/ TableNext /\ UNCHANGED <<cvars, vvars>>
  \/ ConvergeNext /\ UNCHANGED <<cvars, tvars>>

Spec == Init /\ [][Next]_vars

\* ------------------------------------------------------------------ properties

\* No access leaks across objects: every tuple on a container whose record stands was decided on that incarnation, so
\* nothing a deleted project or warehouse held, or a door was granting on it, authorizes on one made again under its id.
NoLeakAcrossIncarnations == rec.inc # 0 => \A t \in tup : t.for = rec.inc

\* No missing owner: a container whose record stands and whose birth landed holds its seed (its creator's grant and its
\* tenancy edge), whatever else ran; one whose birth never landed is born by its create asked again (Resume).
OwnedWhileLive == (Live /\ rec.inc \in tokens) => \E t \in tup : t = Seed(rec.inc)

\* No access leaks across tables: a tuple on the table a name holds was decided on that table.
TableNoLeak == live # 0 => \A t \in ttup : t.obj = Key(live) => t.for = live

\* A table the name holds has its owner's grant, outside an undrop's own two steps.
TableOwnedWhileLive == (live # 0 /\ ~Undropping) => TSeed(live) \in ttup

\* No access outlives its grant: once a revoke answered, no reader is allowed by the revoked tuple.
NoAccessAfterRevoke == ~bad

\* Git's grants and door grants do not fight: the converge revokes only a tuple it created.
GitRevokesOnlyItsOwn == ~wrong

TypeOK ==
  /\ rec.inc \in 0 .. MaxInc /\ tokens \subseteq Incs /\ born \subseteq Incs
  /\ dstep \in 0 .. 6 /\ live \in 0 .. MaxUuid /\ held \in BOOLEAN
=============================================================================
