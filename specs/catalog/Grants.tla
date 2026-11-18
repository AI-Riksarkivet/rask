------------------------------- MODULE Grants -------------------------------
(***************************************************************************)
(* The catalog's authorization state beside its objects (ADR 0241): the    *)
(* tuples OpenFGA holds, a second store the object store's conditional     *)
(* writes do not arbitrate, written and revoked by the catalog's doors     *)
(* beside the registry records and tables they are about. Three parts,     *)
(* each checked by configs of its own (Parts); a part a config leaves out  *)
(* stays at its initial state.                                             *)
(*                                                                         *)
(* PART "container": one project or warehouse id, named in OpenFGA by the  *)
(* id itself (`project:<id>`, `warehouse:<id>`), deleted and created again *)
(* by another tenant (incarnations 1..MaxInc). OpenFGA holds a tuple once  *)
(* per (kind, subject); each tuple carries, as a history variable the code *)
(* never sees, the incarnation its writer was authorized on (0: a ghost,   *)
(* which nobody decided on and which the drift repair revokes).            *)
(*   Mint     the registry record minted with a fresh incarnation          *)
(*            (projects.establish_project, warehouses.establish_warehouse) *)
(*   Bear     its birth, ONE OpenFGA Write: the incarnation's marker, its  *)
(*            token and its seed, deleting any repair token standing       *)
(*            (fga.bear); born already, the seed is written under a fence  *)
(*            (fga_deps._bear_or_assert). Resume: a re-POST                *)
(*   Writer   a door's write on or naming the container, through ONE       *)
(*            context manager (fga_deps.door_fence, fga.fenced): it asks   *)
(*            its caller's authority with no write (Gate), writes a ticket *)
(*            of its own, reads the token, asks the authority again on the *)
(*            incarnation it read (Check), and commits in one Write that   *)
(*            deletes its ticket (fga.commit_fenced); the ticket is taken  *)
(*            back on every exit.                                          *)
(*              Granters: a member, admin, decider or rung grant on it     *)
(*              (members.grant_member, access._access_mutate, the          *)
(*              declaration's grant, the tenancy rebuild).                 *)
(*              Edges: a tuple naming it as SUBJECT: a warehouse's         *)
(*              tenancy edge written by its birth (warehouses.             *)
(*              create_warehouse, fga_deps.seed_warehouse), a role's       *)
(*              project edge and its assignments (roles.create_role,       *)
(*              roles.assign_role). Only the delete's read by subject      *)
(*              finds them.                                                *)
(*              Revokers: a revoke of a granter's tuple (the access        *)
(*              door's revoke, members.revoke_member,                      *)
(*              project_admins.withdraw).                                  *)
(*              Intruders: a caller the object authorizes for nothing.     *)
(*   Child    with Child, the edge writer is a warehouse's create: between *)
(*            its last fence check and its birth it writes its registry    *)
(*            record, naming the project and the incarnation its fence     *)
(*            read (warehouses.establish_warehouse). The boot backfill     *)
(*            (fga_deps.converge_warehouse_grants) writes its tenancy edge *)
(*            from the record under a fence of the incarnation standing,   *)
(*            and the converge tick ends a record whose project            *)
(*            incarnation no longer stands (incarnations.converge).        *)
(*   Delete   projects.delete_project / warehouses.delete_warehouse: marks *)
(*            its incarnation born, retires its token, then drains         *)
(*            (fga.end_incarnation): reads what stands on it and what      *)
(*            names it, deletes the tickets it read, revokes what it read, *)
(*            and reads again until a read finds nothing; refused while a  *)
(*            child record of its incarnation stands; the record last      *)
(*   Repair   the drift repair's ghost revoke (grant_reconcile.            *)
(*            revoke_ghost_grants, fga.revoke_ghost): writes a repair      *)
(*            token, reads, judges the record absent, and revokes what it  *)
(*            read in one Write that deletes its token                     *)
(*   Boot     the catalog's convergence at boot and on each converge tick  *)
(*            (incarnations.converge): a record whose birth never landed   *)
(*            is born, with no seed of its own, and the ticket of a        *)
(*            request that died is swept once older than the bound        *)
(*                                                                         *)
(* PART "table": one table name, its tables keyed by a minted uuid         *)
(* (fga.table_object, ADR 0173): a create seeds the uuid's owner, a purge  *)
(* revokes them, a recoverable drop keeps them for the undrop, the trash   *)
(* release revokes them past the deadline (trash_reclaim.release_trash),   *)
(* and a late grant decided on one table lands after it is gone. A reader  *)
(* decides on what OpenFGA answers (fga.check).                            *)
(*                                                                         *)
(* PART "converge": one tuple git may declare (declaration_converge.       *)
(* _Converger._grant / _revoke) beside the same tuple granted at a door,   *)
(* the tick dying between any two of its steps.                            *)
(*                                                                         *)
(* THE GUARDS, each removed by a config that must fail:                    *)
(*   G1 Fence: a grant commits only while the incarnation it read before   *)
(*      its authority stands (GrantsUnfenced, NoLeakAcrossIncarnations).   *)
(*   G2 RetireFirst: a delete retires the token before it reads what to    *)
(*      revoke (GrantsRetireLast, NoLeakAcrossIncarnations).               *)
(*   G3 BornKept: a delete marks its incarnation born before it retires,   *)
(*      a birth commits only unborn, and no revoke takes a marker          *)
(*      (GrantsRebirth, NoLeakAcrossIncarnations).                         *)
(*   G4 ReadFirst: the ghost revoke reads what stands before it judges     *)
(*      (GrantsRepairJudgesFirst, OwnedWhileLive).                         *)
(*   G5 OwnOnly: the record-first converge forgets its record where its    *)
(*      write found the tuple standing (GrantsConvergeAdopts,              *)
(*      GitRevokesOnlyItsOwn).                                             *)
(*   G6 RepairToken: the ghost revoke commits only while no birth ran      *)
(*      since it read, so it never takes a seed equal to the ghost it read *)
(*      (GrantsRepairUntokened, OwnedWhileLive).                           *)
(*   G7 Tickets: a writer's fence is a ticket of its own, not the one      *)
(*      token every writer moves, so a grant is refused only by the        *)
(*      object's delete (GrantsSharedToken, GrantRefusedOnlyByDelete).     *)
(*   G8 Drain: the delete reads again after deleting the tickets it read,  *)
(*      and revokes what a writer committed in between                     *)
(*      (GrantsDrainOnce, NoLeakAcrossIncarnations).                       *)
(*   G9 FenceEdges: a tuple naming the container as subject is fenced too  *)
(*      (GrantsEdgeUnfenced, NoLeakAcrossIncarnations).                    *)
(*   G10 ReadsSubjects: the delete reads what names the container, not     *)
(*      only what stands on it (GrantsDeleteMissesEdges,                   *)
(*      NoLeakAcrossIncarnations).                                         *)
(*   G11 FenceRevokes: a revoke is fenced as a grant is                    *)
(*      (GrantsRevokeUnfenced, NoNarrowAcrossIncarnations).                *)
(*   G12 BootConverge: a record whose birth never landed is born by the    *)
(*      catalog (GrantsUnbornUnconverged, EveryRecordBorn).                *)
(*   G13 Intents: the converge records an intent, writes the tuple with    *)
(*      evidence of its own in one Write, and settles the intent by the    *)
(*      evidence, so a tick dying between two steps never records a door's *)
(*      tuple git's (GrantsConvergeDies, GitRevokesOnlyItsOwn).            *)
(*   G14 AuthorizeFirst: a writer asks its caller's authority before it    *)
(*      writes its ticket, so a caller the object authorizes for nothing   *)
(*      writes nothing on it (GrantsIntruderFenced, OnlyAuthorizedWrites). *)
(*   G15 Recheck: the authority is asked again once the fence is open, of  *)
(*      the incarnation the fence read (GrantsNoRecheck,                   *)
(*      NoLeakAcrossIncarnations).                                         *)
(*   G16 Sweeps: the converge tick deletes the ticket of a request that    *)
(*      died (GrantsTicketsLeft, temporal DeadTicketsSwept).               *)
(*   G17 NamesIncarnation: a child record names the incarnation of the     *)
(*      project it was made under, and nothing writes an edge from a       *)
(*      record whose project incarnation does not stand (GrantsChild-      *)
(*      Unnamed, NoLeakAcrossIncarnations).                                *)
(* THE ASSUMPTIONS, each removed by a config that must fail:               *)
(*   A1 UuidKeyed: a table's tuples name its uuid, never its name          *)
(*      (GrantsTableByName, TableNoLeak).                                  *)
(*   A2 UndropSettles: an undrop admitted before the deadline registers   *)
(*      before the release may act (trash.RELEASE_SETTLE; GrantsUndrop-    *)
(*      Outlasts, TableOwnedWhileLive).                                    *)
(*   A3 FreshReads: every decision reads at HIGHER_CONSISTENCY             *)
(*      (fga._fresh_read_options; GrantsCachedCheck, NoAccessAfterRevoke). *)
(*                                                                         *)
(* NOT MODELLED: the last-admin guard (ADR 0213, the token every           *)
(* administration change moves in its own Write); principals and labels;  *)
(* a grant's answer (an OpenFGA timeout leaves the write's outcome         *)
(* unknown, which here is a write that commits or not); time (the sweep's *)
(* bound exceeds a request's run, so it takes only a dead request's        *)
(* ticket and an orphan whose create no longer runs).                      *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Parts,          \* which parts this config checks: a subset of {"container", "table", "converge"}
          MaxInc,         \* incarnations of the container id
          Granters,       \* door grants on the container
          Edges,          \* writers of a tuple naming the container as subject
          Revokers,       \* door revokes of the tuple RevTarget's grant made
          RevTarget,      \* the granter whose tuple the revokers revoke
          Intruders,      \* callers the container authorizes for nothing, at a grant door
          Child,          \* the edge writer is a warehouse's create, which writes a record under the project
          Ghost,          \* the id starts with a ghost tuple equal to a new incarnation's seed
          Repairing,      \* the drift repair runs beside the doors
          Deaths,         \* a door may stop between its steps and never take the next
          ConvDeaths,     \* the converge tick may stop between its steps
          Fence,          \* G1
          RetireFirst,    \* G2
          BornKept,       \* G3
          ReadFirst,      \* G4
          OwnOnly,        \* G5
          RepairToken,    \* G6
          Tickets,        \* G7
          Drain,          \* G8
          FenceEdges,     \* G9
          ReadsSubjects,  \* G10
          FenceRevokes,   \* G11
          BootConverge,   \* G12
          Intents,        \* G13
          AuthorizeFirst, \* G14
          Recheck,        \* G15
          Sweeps,         \* G16
          NamesIncarnation, \* G17
          UuidKeyed,      \* A1
          UndropSettles,  \* A2
          FreshReads      \* A3

Incs == 1 .. MaxInc
Writers == Granters \cup Edges \cup Revokers \cup Intruders
On(part) == part \in Parts

\* ---------------------------------------------------------------- container
VARIABLES rec,     \* the registry record: whether it stands, its incarnation, whether a delete started on it
          nextInc, \* the incarnation the next mint takes
          tup,     \* the tuples on and naming the container: [k: "seed", "grant" or "edge", g, for: the incarnation decided on]
          tokens,  \* the incarnations whose token stands
          tokver,  \* the token's count, moved by every write when Tickets is off
          born,    \* the incarnations whose `revision` marker stands
          tickets, \* the writers whose ticket stands
          cpc,     \* each incarnation's create: "idle", "minted", "done", "dead"
          wpc,     \* each writer: "idle", "gated", "open", "read", "checked", "fenced", "recorded", "done", "dead"
          wu,      \* the incarnation whose token a writer read
          wv,      \* the token's count it read
          wauth,   \* the incarnation whose seed authorized it
          refused, \* the writers refused though no delete ran while they did
          dsince,  \* the writers a delete's retire or ticket deletion ran beside
          narrowed,\* a revoke removed a tuple of an incarnation it was not decided on
          dstep,   \* the delete: 0 idle, an index into Steps, or DeadStep
          du,      \* the incarnation it deletes
          dsnap,   \* what its last read found
          dtick,   \* the tickets its last read found
          drounds, \* how many times it read again and found something
          rpc,     \* the repair: "idle", "a", "b", "c", "done"
          rsnap,   \* what it read to revoke
          rok,     \* whether its judgement found no record
          rtok,    \* whether its token stands
          crec     \* the child's record: whether it stands, the project incarnation it names (0: none), the one its create was authorized on

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
          intent,   \* the ledger holds an intent to write it, not yet settled
          ev,       \* the evidence the tick's own Write leaves beside the tuple stands
          cvpc,     \* the converge: "idle", "read", "recorded" (record first); "read", "intent", "written" (intents)
          doorpc,   \* the door grant: "idle", "done"
          wrong     \* the converge revoked a tuple a door created

cvars == <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed,
           dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>
tvars == <<live, tnext, ttup, trashed, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>
vvars == <<declared, held, first, recorded, intent, ev, cvpc, doorpc, wrong>>
vars == <<cvars, tvars, vvars>>

MaxUuid == 2

Seed(i) == [k |-> "seed", g |-> "none", for |-> i]
Id(t) == <<t.k, t.g>>
Ids(S) == {Id(t) : t \in S}
NoRecord == [exists |-> FALSE, inc |-> 0, deleting |-> FALSE]
NoChild == [exists |-> FALSE, pinc |-> 0, made |-> 0]

Init ==
  /\ rec = NoRecord
  /\ nextInc = 1
  /\ tup = IF Ghost THEN {Seed(0)} ELSE {}
  /\ tokens = {} /\ tokver = 0 /\ born = {} /\ tickets = {}
  /\ cpc = [i \in Incs |-> "idle"] /\ wpc = [w \in Writers |-> "idle"] /\ wu = [w \in Writers |-> 0]
  /\ wv = [w \in Writers |-> 0] /\ wauth = [w \in Writers |-> 0] /\ refused = {} /\ dsince = {} /\ narrowed = FALSE
  /\ dstep = 0 /\ du = 0 /\ dsnap = {} /\ dtick = {} /\ drounds = 0
  /\ rpc = "idle" /\ rsnap = {} /\ rok = FALSE /\ rtok = FALSE /\ crec = NoChild
  /\ live = 0 /\ tnext = 1 /\ ttup = {} /\ trashed = {} /\ tphase = "grace" /\ upc = "idle" /\ uu = 0 /\ relpc = "idle" /\ ru = 0
  /\ tgpc = "idle" /\ tgfor = 0 /\ everHeld = FALSE /\ revoked = FALSE /\ bad = FALSE
  /\ declared = "not yet" /\ held = FALSE /\ first = "none" /\ recorded = FALSE /\ intent = FALSE /\ ev = FALSE
  /\ cvpc = "idle" /\ doorpc = "idle" /\ wrong = FALSE

Live == rec.exists /\ ~rec.deleting

\* ---------------------------------------------------------------- container

\* The seed of incarnation i written where its (kind, subject) is absent; a tuple equal to it already standing is the
\* same tuple in OpenFGA, and from then on it is i's seed.
WithSeed(i) == {t \in tup : Id(t) # Id(Seed(i))} \cup {Seed(i)}

\* A create mints the record when the id has none: a project or warehouse made again under a deleted id.
Mint(i) ==
  /\ On("container") /\ cpc[i] = "idle" /\ ~rec.exists /\ nextInc = i
  /\ rec' = [exists |-> TRUE, inc |-> i, deleting |-> FALSE] /\ nextInc' = i + 1 /\ cpc' = [cpc EXCEPT ![i] = "minted"]
  /\ UNCHANGED <<tup, tokens, tokver, born, tickets, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* The birth: one Write of the marker, the token and the seed, deleting the repair's token where it read one, which the
\* store refuses whole once the marker stands. Born already (by the boot convergence), the seed is written under the
\* token while it stands, and nothing is written once it was retired.
Bear(i) ==
  /\ On("container") /\ cpc[i] = "minted"
  /\ IF BornKept /\ i \in born
       THEN /\ IF i \in tokens THEN tup' = WithSeed(i) ELSE UNCHANGED tup
            /\ cpc' = [cpc EXCEPT ![i] = IF i \in tokens THEN "done" ELSE "dead"]
            /\ UNCHANGED <<tokens, born, rtok>>
       ELSE /\ born' = born \cup {i} /\ tokens' = tokens \cup {i} /\ tup' = WithSeed(i)
            /\ rtok' = (rtok /\ ~RepairToken)
            /\ cpc' = [cpc EXCEPT ![i] = "done"]
  /\ UNCHANGED <<rec, nextInc, tokver, tickets, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, crec>>

\* A re-POST of a record whose create died: it reads the record standing and bears it again.
Resume(i) ==
  /\ On("container") /\ cpc[i] = "dead" /\ rec.exists /\ rec.inc = i /\ ~rec.deleting
  /\ cpc' = [cpc EXCEPT ![i] = "minted"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

CreateDies(i) ==
  /\ On("container") /\ Deaths /\ cpc[i] = "minted"
  /\ cpc' = [cpc EXCEPT ![i] = "dead"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* Whether a writer's write is fenced at all: a grant by G1, an edge by G9, a revoke by G11.
Fenced(w) == IF w \in Revokers THEN FenceRevokes ELSE IF w \in Edges THEN FenceEdges ELSE Fence
WriterTuple(w) == [k |-> IF w \in Edges THEN "edge" ELSE "grant", g |-> w, for |-> wauth[w]]
Target == <<"grant", RevTarget>>
\* A warehouse's create: the edge writer when Child is on.
IsChild(w) == Child /\ w \in Edges
\* The authority, asked of OpenFGA: a seed standing on the container authorizes every caller but an intruder.
Authorized(w) == w \notin Intruders /\ \E t \in tup : t.k = "seed"
\* The incarnation a seed standing authorizes as: a ghost equal to the seed (a person's admin an earlier incarnation
\* left) authorizes as the incarnation whose token the writer read.
AuthorizedOn(w, t) == IF t.for = 0 THEN wu[w] ELSE t.for
\* The state a writer opens its fence from.
Opening == IF AuthorizeFirst THEN "gated" ELSE "idle"

\* G14: the authority asked first, with no write: a caller refused writes nothing on the container. A ghost authorizes
\* as the incarnation the writer will read, so here only a seed of an incarnation names one.
WGate(w) ==
  /\ On("container") /\ AuthorizeFirst /\ wpc[w] = "idle"
  /\ IF Authorized(w)
       THEN /\ \E t \in tup : t.k = "seed" /\ wauth' = [wauth EXCEPT ![w] = t.for]
            /\ wpc' = [wpc EXCEPT ![w] = "gated"]
       ELSE /\ wpc' = [wpc EXCEPT ![w] = "done"] /\ UNCHANGED wauth
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wu, wv, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* The ticket next, written before the token is read; an unfenced writer writes none.
WOpen(w) ==
  /\ On("container") /\ wpc[w] = Opening
  /\ tickets' = IF Fenced(w) /\ Tickets THEN tickets \cup {w} ELSE tickets
  /\ wpc' = [wpc EXCEPT ![w] = "open"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, cpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* The token read before the authority is asked again; none standing, the door refuses (being established, or deleted)
\* and deletes its ticket.
WRead(w) ==
  /\ On("container") /\ wpc[w] = "open"
  /\ IF ~Fenced(w)
       THEN /\ wpc' = [wpc EXCEPT ![w] = "read"] /\ UNCHANGED <<wu, wv, tickets>>
       ELSE IF tokens = {}
         THEN /\ wpc' = [wpc EXCEPT ![w] = "done"] /\ tickets' = tickets \ {w} /\ UNCHANGED <<wu, wv>>
         ELSE /\ \E u \in tokens : wu' = [wu EXCEPT ![w] = u]
              /\ wv' = [wv EXCEPT ![w] = tokver]
              /\ wpc' = [wpc EXCEPT ![w] = "read"] /\ UNCHANGED tickets
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, cpc, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* The authority asked (again, with G14) on what stands once the fence is open; refused, the writer deletes its ticket.
\* With G14 and without G15 it is not asked again, and the gate's answer stands.
WCheck(w) ==
  /\ On("container") /\ wpc[w] = "read"
  /\ IF AuthorizeFirst /\ ~Recheck
       THEN /\ wpc' = [wpc EXCEPT ![w] = "checked"] /\ UNCHANGED <<tickets, wauth>>
       ELSE IF Authorized(w)
         THEN /\ \E t \in tup : t.k = "seed" /\ wauth' = [wauth EXCEPT ![w] = AuthorizedOn(w, t)]
              /\ wpc' = [wpc EXCEPT ![w] = "checked"] /\ UNCHANGED tickets
         ELSE /\ wpc' = [wpc EXCEPT ![w] = "done"] /\ tickets' = tickets \ {w} /\ UNCHANGED wauth
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, cpc, wu, wv, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* A warehouse's create checks its fence still stands, then writes its record: two steps, so a delete and a re-creation
\* of its project can run between them (the window ADR 0241 §8 B3 holds open in its test).
WFenceCheck(w) ==
  /\ On("container") /\ IsChild(w) /\ wpc[w] = "checked"
  /\ wpc' = [wpc EXCEPT ![w] = IF ~Fenced(w) \/ ~Tickets \/ w \in tickets THEN "fenced" ELSE "done"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* Its record names the project's id and, with G17, the incarnation its fence read; a record already standing under
\* the id refuses it.
WRecord(w) ==
  /\ On("container") /\ IsChild(w) /\ wpc[w] = "fenced"
  /\ IF crec.exists
       THEN /\ wpc' = [wpc EXCEPT ![w] = "done"] /\ tickets' = tickets \ {w} /\ UNCHANGED crec
       ELSE /\ crec' = [exists |-> TRUE, pinc |-> IF NamesIncarnation THEN wu[w] ELSE 0, made |-> wauth[w]]
            /\ wpc' = [wpc EXCEPT ![w] = "recorded"] /\ UNCHANGED tickets
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, cpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok>>

\* The write: with tickets, one Write deleting the writer's own ticket; without, one moving the token it read.
Commits(w) ==
  IF ~Fenced(w) THEN TRUE
  ELSE IF Tickets THEN w \in tickets
  ELSE wu[w] \in tokens /\ tokver = wv[w]

WWrite(w) ==
  /\ On("container") /\ wpc[w] = IF IsChild(w) THEN "recorded" ELSE "checked"
  /\ IF Commits(w)
       THEN /\ IF w \in Revokers
                 THEN /\ tup' = {t \in tup : Id(t) # Target}
                      /\ narrowed' = (narrowed \/ \E t \in tup : Id(t) = Target /\ t.for \notin {0, wauth[w]})
                 ELSE /\ tup' = IF Id(WriterTuple(w)) \in Ids(tup) THEN tup ELSE tup \cup {WriterTuple(w)}
                      /\ UNCHANGED narrowed
            /\ tickets' = tickets \ {w}
            /\ tokver' = IF Fenced(w) /\ ~Tickets THEN tokver + 1 ELSE tokver
            /\ UNCHANGED refused
       ELSE /\ refused' = IF w \in dsince THEN refused ELSE refused \cup {w}
            /\ UNCHANGED <<tup, narrowed, tickets, tokver>>
  /\ wpc' = [wpc EXCEPT ![w] = "done"]
  /\ UNCHANGED <<rec, nextInc, tokens, born, cpc, wu, wv, wauth, dsince, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* A death between two steps: whatever the writer wrote stays, its ticket among it.
WDies(w) ==
  /\ On("container") /\ Deaths /\ wpc[w] \in {"gated", "open", "read", "checked", "fenced", "recorded"}
  /\ wpc' = [wpc EXCEPT ![w] = "dead"]
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* G16: the converge tick deletes a ticket older than the bound, which only a request that died leaves, on an id a
\* record names (incarnations.converge, fga.sweep_tickets).
SweepTicket(w) ==
  /\ On("container") /\ Sweeps /\ rec.exists /\ w \in tickets /\ wpc[w] = "dead"
  /\ tickets' = tickets \ {w}
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* The child record's project incarnation is the one standing: a record of the live tenant.
ChildOfLive == crec.exists /\ rec.exists /\ crec.pinc = rec.inc

\* The boot backfill of a child record's tenancy edge (fga_deps.converge_warehouse_grants, and the convergence bearing an
\* unborn warehouse): under a fence of the project incarnation standing, the edge the record justifies. It was decided
\* on the incarnation the create was authorized on. With G17 it writes only from a record of the live tenant.
Backfill ==
  /\ On("container") /\ Child /\ crec.exists /\ tokens # {} /\ (NamesIncarnation => ChildOfLive)
  /\ LET e == CHOOSE x \in Edges : TRUE
         edge == [k |-> "edge", g |-> e, for |-> crec.made]
     IN tup' = IF Id(edge) \in Ids(tup) THEN tup ELSE tup \cup {edge}
  /\ UNCHANGED <<rec, nextInc, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* G17's sweep: the converge tick ends a child record whose project incarnation no longer stands, once its create no
\* longer runs, releasing its name; nothing stands under it.
ChildEnd ==
  /\ On("container") /\ Child /\ Sweeps /\ NamesIncarnation /\ crec.exists /\ ~ChildOfLive
  /\ \A e \in Edges : wpc[e] \notin {"gated", "open", "read", "checked", "fenced", "recorded"}
  /\ crec' = NoChild
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok>>

\* The delete's steps, in its order. "settle" reads again: finding something, it goes back to delete the tickets it
\* found and revoke what it found; finding nothing, or with Drain off, it goes on.
Steps == IF RetireFirst THEN <<"born", "retire", "read", "untick", "revoke", "settle", "record">>
                        ELSE <<"born", "read", "untick", "revoke", "settle", "retire", "record">>
NSteps == 7
DeadStep == NSteps + 1
StepAt(s) == CHOOSE n \in 1 .. NSteps : Steps[n] = s
\* What a read of the container finds: what stands on it, and, by subject, what names it.
Seen(S) == IF ReadsSubjects THEN S ELSE {t \in S : t.k # "edge"}
\* A child record the project's delete counts as its own: by the incarnation it names with G17, by the id without.
ChildOf(i) == crec.exists /\ (NamesIncarnation => crec.pinc = i)

DeleteStart ==
  /\ On("container") /\ dstep \in {0, DeadStep} /\ rec.exists
  /\ rec' = [rec EXCEPT !.deleting = TRUE] /\ du' = rec.inc /\ dstep' = 1 /\ drounds' = 0
  /\ UNCHANGED <<nextInc, tup, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dsnap, dtick, rpc, rsnap, rok, rtok, crec>>

\* The record goes last, and not while a child record of its incarnation stands (the delete's late re-read, refused
\* 409 and asked again).
DeleteStep ==
  /\ On("container") /\ dstep \in 1 .. NSteps
  /\ LET s == Steps[dstep]
         again == s = "settle" /\ Drain /\ Seen(tup) # {}
         blocked == s = "record" /\ ChildOf(du)
     IN /\ born' = IF s = "born" /\ BornKept /\ du # 0 THEN born \cup {du} ELSE born
        /\ tokens' = IF s = "retire" THEN {} ELSE tokens
        /\ dsnap' = IF s = "read" \/ again THEN Seen(tup) ELSE dsnap
        /\ dtick' = IF s = "read" \/ again THEN tickets ELSE dtick
        /\ tickets' = IF s = "untick" THEN tickets \ dtick ELSE tickets
        /\ tup' = IF s = "revoke" THEN {t \in tup : Id(t) \notin Ids(dsnap)} ELSE tup
        /\ rec' = IF s = "record" /\ ~blocked THEN NoRecord ELSE rec
        /\ drounds' = IF again THEN drounds + 1 ELSE drounds
        /\ dsince' = IF s \in {"retire", "untick"} THEN dsince \cup {w \in Writers : wpc[w] \in {"gated", "open", "read", "checked", "fenced", "recorded"}} ELSE dsince
        /\ dstep' = IF again THEN StepAt("untick") ELSE IF blocked THEN DeadStep ELSE IF s = "record" THEN 0 ELSE dstep + 1
  /\ UNCHANGED <<nextInc, tokver, cpc, wpc, wu, wv, wauth, refused, narrowed, du, rpc, rsnap, rok, rtok, crec>>

DeleteDies ==
  /\ On("container") /\ Deaths /\ dstep \in 1 .. NSteps
  /\ dstep' = DeadStep
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, du, dsnap, dtick, drounds, rpc, rsnap, rok, rtok, crec>>

\* The ghost revoke: its token, then its judgement and its read in the code's order, then the revoke of what it read,
\* which with G6 commits only while its token stands (no birth since).
RepairStart ==
  /\ On("container") /\ Repairing /\ rpc = "idle"
  /\ rtok' = RepairToken /\ rpc' = "a"
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rsnap, rok, crec>>

RepairStep ==
  /\ On("container") /\ rpc \in {"a", "b"}
  /\ LET judge == (ReadFirst /\ rpc = "b") \/ (~ReadFirst /\ rpc = "a")
         read == (ReadFirst /\ rpc = "a") \/ (~ReadFirst /\ rpc = "b")
     IN /\ rok' = IF judge THEN ~rec.exists ELSE rok
        /\ rsnap' = IF read THEN tup ELSE rsnap
        /\ rpc' = IF judge /\ rec.exists THEN "done" ELSE IF rpc = "a" THEN "b" ELSE "c"
  /\ UNCHANGED <<rec, nextInc, tup, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rtok, crec>>

RepairRevoke ==
  /\ On("container") /\ rpc = "c" /\ rok
  /\ IF ~RepairToken \/ rtok THEN tup' = {t \in tup : Id(t) \notin Ids(rsnap)} ELSE UNCHANGED tup
  /\ rtok' = FALSE /\ rpc' = "done"
  /\ UNCHANGED <<rec, nextInc, tokens, tokver, born, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rsnap, rok, crec>>

\* The catalog's convergence: a record whose birth never landed (its create died between its record and its birth) is
\* born with no seed of its own; its birth Write is refused once the marker stands. Every record names an incarnation:
\* a create mints it with the record (B5, no record without one is converged).
BootBear ==
  /\ On("container") /\ BootConverge /\ rec.exists /\ ~rec.deleting /\ rec.inc \notin born
  /\ born' = born \cup {rec.inc} /\ tokens' = tokens \cup {rec.inc} /\ rtok' = (rtok /\ ~RepairToken)
  /\ UNCHANGED <<rec, nextInc, tup, tokver, tickets, cpc, wpc, wu, wv, wauth, refused, dsince, narrowed, dstep, du, dsnap, dtick, drounds, rpc, rsnap, rok, crec>>

ContainerNext ==
  \/ \E i \in Incs : Mint(i) \/ Bear(i) \/ Resume(i) \/ CreateDies(i)
  \/ \E w \in Writers : WGate(w) \/ WOpen(w) \/ WRead(w) \/ WCheck(w) \/ WFenceCheck(w) \/ WRecord(w) \/ WWrite(w) \/ WDies(w) \/ SweepTicket(w)
  \/ DeleteStart \/ DeleteStep \/ DeleteDies \/ RepairStart \/ RepairStep \/ RepairRevoke
  \/ BootBear \/ Backfill \/ ChildEnd

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

GitDeclares == /\ On("converge") /\ declared = "not yet" /\ declared' = "yes" /\ UNCHANGED <<held, first, recorded, intent, ev, cvpc, doorpc, wrong>>
GitDrops == /\ On("converge") /\ declared = "yes" /\ declared' = "dropped" /\ UNCHANGED <<held, first, recorded, intent, ev, cvpc, doorpc, wrong>>

\* The tick's read of the store, again right before it records: a tuple it finds is somebody's, and is left so.
ConvRead ==
  /\ On("converge") /\ cvpc = "idle" /\ declared = "yes" /\ ~held /\ ~intent
  /\ cvpc' = "read"
  /\ UNCHANGED <<declared, held, first, recorded, intent, ev, doorpc, wrong>>

\* Record first (G13 off, the code before): the tuple recorded git's before it is written.
ConvRecord ==
  /\ On("converge") /\ ~Intents /\ cvpc = "read"
  /\ recorded' = TRUE /\ cvpc' = "recorded"
  /\ UNCHANGED <<declared, held, first, intent, ev, doorpc, wrong>>

\* The record-first write: guarded, it commits only where the tuple is absent and forgets its record where one stands.
ConvWriteRecorded ==
  /\ On("converge") /\ ~Intents /\ cvpc = "recorded"
  /\ IF held
       THEN /\ recorded' = (IF OwnOnly THEN FALSE ELSE recorded) /\ UNCHANGED <<held, first>>
       ELSE /\ held' = TRUE /\ first' = "git" /\ UNCHANGED recorded
  /\ cvpc' = "idle"
  /\ UNCHANGED <<declared, intent, ev, doorpc, wrong>>

\* G13: an intent recorded, then one Write of the tuple and the tick's evidence, committing only where the tuple is absent.
ConvIntent ==
  /\ On("converge") /\ Intents /\ cvpc = "read"
  /\ intent' = TRUE /\ cvpc' = "intent"
  /\ UNCHANGED <<declared, held, first, recorded, ev, doorpc, wrong>>

ConvWriteEvidenced ==
  /\ On("converge") /\ Intents /\ cvpc = "intent"
  /\ IF held THEN UNCHANGED <<held, first, ev>> ELSE held' = TRUE /\ first' = "git" /\ ev' = TRUE
  /\ cvpc' = "written"
  /\ UNCHANGED <<declared, recorded, intent, doorpc, wrong>>

\* The intent settled, by this tick or a later one: git's where its evidence stands, forgotten where it does not.
ConvSettle ==
  /\ On("converge") /\ Intents /\ intent /\ cvpc \in {"idle", "written"}
  /\ recorded' = (recorded \/ ev) /\ ev' = FALSE /\ intent' = FALSE /\ cvpc' = "idle"
  /\ UNCHANGED <<declared, held, first, doorpc, wrong>>

ConvDies ==
  /\ On("converge") /\ ConvDeaths /\ cvpc \in {"read", "recorded", "intent", "written"}
  /\ cvpc' = "idle"
  /\ UNCHANGED <<declared, held, first, recorded, intent, ev, doorpc, wrong>>

ConvRevoke ==
  /\ On("converge") /\ cvpc = "idle" /\ declared = "dropped" /\ recorded /\ ~intent
  /\ wrong' = (wrong \/ (held /\ first = "door"))
  /\ held' = FALSE /\ first' = "none" /\ recorded' = FALSE
  /\ UNCHANGED <<declared, intent, ev, cvpc, doorpc>>

DoorGrants ==
  /\ On("converge") /\ doorpc = "idle"
  /\ IF held THEN UNCHANGED <<held, first>> ELSE held' = TRUE /\ first' = "door"
  /\ doorpc' = "done"
  /\ UNCHANGED <<declared, recorded, intent, ev, cvpc, wrong>>

ConvergeNext ==
  GitDeclares \/ GitDrops \/ ConvRead \/ ConvRecord \/ ConvWriteRecorded \/ ConvIntent \/ ConvWriteEvidenced
    \/ ConvSettle \/ ConvDies \/ ConvRevoke \/ DoorGrants


Next ==
  \/ ContainerNext /\ UNCHANGED <<tvars, vvars>>
  \/ TableNext /\ UNCHANGED <<cvars, vvars>>
  \/ ConvergeNext /\ UNCHANGED <<cvars, tvars>>

\* The convergence runs on every tick until nothing is left for it to do: it bears what is unborn and sweeps what a dead
\* request left.
Spec ==
  /\ Init /\ [][Next]_vars
  /\ WF_vars(BootBear /\ UNCHANGED <<tvars, vvars>>)
  /\ \A w \in Writers : WF_vars(SweepTicket(w) /\ UNCHANGED <<tvars, vvars>>)

\* ------------------------------------------------------------------ properties

\* No access leaks across objects: every tuple on or naming a container whose record stands was decided on that
\* incarnation (or is a ghost nobody decided on, the drift repair's), so nothing a deleted project or warehouse held,
\* or a door was writing on or under it, authorizes on one made again under its id: no grant, no warehouse planted in
\* it, no role of it, and no tenancy edge a converge writes from a record a dead tenant's create left.
NoLeakAcrossIncarnations == rec.exists => \A t \in tup : t.for \in {0, rec.inc}

\* Its narrowing twin: no tuple of one incarnation is removed by a revoke decided on another.
NoNarrowAcrossIncarnations == ~narrowed

\* Only a caller the object's incarnation authorizes writes on it: a caller refused at the gate leaves no ticket and no
\* tuple, so an unprivileged caller cannot write on, slow or wedge another tenant's object.
OnlyAuthorizedWrites == \A w \in Intruders : w \notin tickets /\ w \notin {t.g : t \in tup}

\* No missing owner: a container whose record stands and whose own create bore it holds its seed whatever else ran. One
\* whose create died before its birth is born by the boot convergence with no seed, and holds it once its create is
\* asked again (Resume).
OwnedWhileLive == (Live /\ rec.inc \in tokens /\ cpc[rec.inc] = "done") => Seed(rec.inc) \in tup

\* Grants do not exclude grants: a writer is refused only where a delete retired or drained while it ran.
GrantRefusedOnlyByDelete == refused = {}

\* The delete's drain ends: after deleting the tickets its first read found it reads again, and at most that once more
\* finds something, since no fenced writer commits once its ticket is gone.
DrainBounded == drounds <= 1

\* Every record is born: one whose birth never landed is born with no hand-run step, unless its delete has begun.
EveryRecordBorn == Live ~> (~Live \/ rec.inc \in born)

\* No ticket of a dead request stands for ever on an id a record names: the converge tick sweeps it.
DeadTicketsSwept == \A w \in Writers : (w \in tickets /\ wpc[w] = "dead") ~> (w \notin tickets \/ ~rec.exists)

\* No access leaks across tables: a tuple on the table a name holds was decided on that table.
TableNoLeak == live # 0 => \A t \in ttup : t.obj = Key(live) => t.for = live

\* A table the name holds has its owner's grant, outside an undrop's own two steps.
TableOwnedWhileLive == (live # 0 /\ ~Undropping) => TSeed(live) \in ttup

\* No access outlives its grant: once a revoke answered, no reader is allowed by the revoked tuple.
NoAccessAfterRevoke == ~bad

\* Git's grants and door grants do not fight: the converge revokes only a tuple it created.
GitRevokesOnlyItsOwn == ~wrong

TypeOK ==
  /\ rec.inc \in 0 .. MaxInc /\ tokens \subseteq Incs /\ born \subseteq Incs /\ tickets \subseteq Writers
  /\ dstep \in 0 .. DeadStep /\ live \in 0 .. MaxUuid /\ held \in BOOLEAN /\ crec.pinc \in 0 .. MaxInc
=============================================================================
