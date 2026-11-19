------------------------------- MODULE Grants -------------------------------
(***************************************************************************)
(* The catalog's authorization state beside its objects (ADR 0241, ADR     *)
(* 0245): the tuples OpenFGA holds, a second store the object store's      *)
(* conditional writes do not arbitrate, written and revoked by the         *)
(* catalog's doors beside the registry records and tables they are about.  *)
(* Three parts, each checked by configs of its own (Parts); a part a       *)
(* config leaves out stays at its initial state.                           *)
(*                                                                         *)
(* PART "container": one project id, deleted and created again by another  *)
(* tenant (creations 1..MaxInc). Each creation mints a uuid, and OpenFGA   *)
(* names the project by it (`project:<uuid>`), never by the id. A tuple    *)
(* records the uuid it was written on (obj) and, as a history variable the *)
(* code never sees, the uuid its writer was authorized on (for).           *)
(*   Create   its record first, `creating`, naming its uuid and creator;   *)
(*            then its seed on the uuid; then the record flips `live`      *)
(*            (projects.establish_project, warehouses.establish_warehouse).*)
(*            The converge tick finishes a `creating` record from what it  *)
(*            names (tenancy.converge).                                    *)
(*   Writer   a door: it resolves the id to the uuid of its live record    *)
(*            (tenancy.resolve, the ONE builder of a project or warehouse  *)
(*            object), asks its gate on that object, writes on it in ONE   *)
(*            Write, and resolves the id again: an id no longer naming    *)
(*            that uuid live is answered as the deleted object it is.      *)
(*              Granters: a member, admin, decider or rung grant.          *)
(*              Revokers: a revoke of the tuple RevTarget's grant made.    *)
(*              Intruders: a caller the project authorizes for nothing.    *)
(*              Declarers: the declaration's converge, which git           *)
(*              authorizes on whatever creation the id names: the one      *)
(*              writer whose tuple crosses creations, by design.           *)
(*   Child    with Child, a child created under the project: a warehouse   *)
(*            (its bucket claimed first, then its record `creating` naming *)
(*            the project's uuid, then the project resolved again, then    *)
(*            its seed and its tenancy edge, then `live`), or with Cascade *)
(*            a namespace under a warehouse (no bucket; the parent's       *)
(*            delete ends it rather than being refused by it). The tick    *)
(*            finishes a `creating` child whose parent is live and ends    *)
(*            one whose parent is not, releasing its bucket's claim where  *)
(*            the bucket holds only what a create writes. A dead claim no  *)
(*            record names is purged by the estate owner by its bucket.    *)
(*   Delete   runs the checks that can refuse with nothing changed, marks  *)
(*            the record `deleting` as its own (under the read it judged;  *)
(*            no door resolves it from then on), reads its children        *)
(*            (refused while one stands, the record put back live, or ends *)
(*            them with Cascade), stamps its point of no return, revokes   *)
(*            every tuple on the uuid and naming it, and ends the record.  *)
(*            Its stamp and its end are written only while its own mark   *)
(*            stands; its other steps act on its creation's own objects.   *)
(*            A failure before its stamp puts the record back, by the     *)
(*            delete whose run holds the mark; past the stamp nothing     *)
(*            puts it back, and every step that can refuse (the purge's   *)
(*            claim among them) runs before it. Dying, it is asked again, *)
(*            and the tick puts back or finishes a mark whose run no      *)
(*            longer runs: it lists the registry, then finishes only on a *)
(*            fresh read still carrying the listed run past its stamp;   *)
(*            two deletes of one creation may run (Deleters).             *)
(*   Sweep    the tick revokes what stands on an ended uuid: a write that  *)
(*            raced the delete landed on a dead object, which authorizes   *)
(*            nothing; the sweep is hygiene.                               *)
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
(* the tick dying between any two of its steps, and the object deleted and *)
(* made again between ticks (creations 1..MaxInc).                         *)
(*                                                                         *)
(* THE GUARDS, each removed by a config that must fail:                    *)
(*   U1 ResolveAtDoor: a door writes on the uuid it resolved and asked its *)
(*      gate on, never on what the id names at the write (GrantsById,      *)
(*      NoLeakAcrossIncarnations; GrantsRevokeById,                        *)
(*      NoNarrowAcrossIncarnations).                                       *)
(*   U2 SeedFirst: a record flips live only after its seed                 *)
(*      (GrantsLiveBeforeSeed, OwnedWhileLive).                            *)
(*   U3 LiveOnly: a door resolves only a live record, never one creating   *)
(*      or deleting (GrantsResolvesDeleting, GrantAnswersTruthfully).      *)
(*   U4 ResolveAgain: a door resolves the id again after its write and     *)
(*      answers a deleted object's refusal where it no longer names the    *)
(*      uuid live (GrantsAnswersUnread, GrantAnswersTruthfully).           *)
(*   U5 ChildRecordFirst: a child's create writes its record before it     *)
(*      resolves its parent again (GrantsChildReadFirst,                   *)
(*      ChildUnderLiveParent).                                             *)
(*   U6 MarksFirst: a delete marks its record before it reads its children *)
(*      (GrantsChildrenBeforeMark, ChildUnderLiveParent).                  *)
(*   U7 CountsCreating: a delete counts a child still creating             *)
(*      (GrantsCreatingUncounted, ChildUnderLiveParent).                   *)
(*   U8 TickFinishes: the tick finishes or ends every creating record      *)
(*      (GrantsCreatingUnsettled, temporal CreatingSettles).               *)
(*   U9 TickChecksParent: the tick finishes a creating child only under a  *)
(*      live parent (GrantsTickFinishesOrphan, ChildUnderLiveParent).      *)
(*   U10 TickSweeps: the tick revokes what stands on an ended uuid         *)
(*      (GrantsDeadUnswept, temporal DeadObjectsSwept).                    *)
(*   U11 LedgerPerCreation: the declaration's ledger record is about one   *)
(*      creation, so a record of a creation that ended is forgotten and   *)
(*      the grant declared afresh on the live one (GrantsDeclaredStale,    *)
(*      GrantsConvergeStale, temporal DeclaredOnLive, DeclaredHeld).       *)
(*   U12 EndsOwnCreation: a delete marks its record under the read it      *)
(*      judged, selects its children by its creation, and stamps and ends  *)
(*      only while its own mark stands (GrantsDeleteTwiceById,             *)
(*      DeleteEndsOnlyItsOwn). A read of the mark before each step         *)
(*      (tenancy.require_own) is no guard: removed in every config, no     *)
(*      property fails (measured 2026-11-18), and it is deleted.           *)
(*   U13 RevertsRefusal: every check that can refuse runs before the mark, *)
(*      and a refusal after it, before the no-return stamp, puts the       *)
(*      record back live (GrantsRefusalStuck, RefusedDeleteLeavesLive).    *)
(*   U14 PutsBackOnFailure: a delete failing (alive) between its mark and  *)
(*      its no-return stamp puts the record back live, whatever failed     *)
(*      (GrantsFailureStuck, RefusedDeleteLeavesLive).                     *)
(*   U15 TickSettlesMarks: the tick puts back a mark with no stamp whose   *)
(*      run no longer runs, and finishes a stamped one                     *)
(*      (GrantsMarkUnsettled, temporal DeletingSettles).                   *)
(*   U16 ChecksTakeOver: a delete taking over a mark with no stamp judges  *)
(*      every check again; only a stamped one is resumed without them      *)
(*      (GrantsTakeOverUnchecked, ProtectedDeletedOnlyByForce).            *)
(*   U17 ConditionalEnd: the end removes the record in ONE write          *)
(*      conditional on the read that found its own mark (DeleteObject      *)
(*      If-Match), never by its key alone (GrantsEndByKey,                 *)
(*      DeleteEndsOnlyItsOwn).                                             *)
(*   U18 FreshFinish: the tick finishes a delete only on a read of the     *)
(*      record made after its listing that still carries the listed run   *)
(*      past its stamp, conditional on that read; it never begins one.    *)
(*      Without it the finish acts on any record of the listed uuid, live *)
(*      or marked, as a new delete with no caller (the code before the     *)
(*      third review). Alone it is masked by U19, which keeps a stamped    *)
(*      record from going live; GrantsTickFinishLive removes both         *)
(*      (TickBeginsNoDelete).                                              *)
(*   U19 NoReturnFinal: past its stamp a record is never put back: every   *)
(*      step that can refuse (the purge's claim) runs before the stamp     *)
(*      (GrantsPutBackPastStamp, temporal StampedEnds).                    *)
(*   U20 PutsBackOwnMark: only the delete whose run holds the mark puts    *)
(*      it back (GrantsPutBackTakenOver, PutBackOnlyItsOwn).               *)
(*   G5 OwnOnly, G13 Intents: the converge part.                           *)
(*   G14 AuthorizeFirst: a door asks its gate before it writes             *)
(*      (GrantsIntruderWrites, OnlyAuthorizedWrites).                      *)
(*   G19 ReleasesUnborn: the tick releases a dead creation's bucket claim  *)
(*      (GrantsClaimKept, temporal ClaimFreed).                            *)
(*   G20 KeepsBorn: the tick ends a child only while it is creating, never *)
(*      one that went live (GrantsBornReleased, BornLeftKept).             *)
(*   G22 PurgeStamp: a purge stamps the claim its own before its first     *)
(*      delete and deletes only while the stamp stands                     *)
(*      (GrantsPurgeUnstamped, PurgeTakesNothingLive).                     *)
(*   G24 PurgeByBucket: the estate owner's purge reaches a dead claim no   *)
(*      record names by its bucket (GrantsClaimUnaddressed, temporal       *)
(*      ClaimFreed).                                                       *)
(* THE ASSUMPTIONS, each removed by a config that must fail:               *)
(*   A1 UuidKeyed: a table's tuples name its uuid, never its name          *)
(*      (GrantsTableByName, TableNoLeak).                                  *)
(*   A2 UndropSettles: an undrop admitted before the deadline registers   *)
(*      before the release may act (trash.RELEASE_SETTLE; GrantsUndrop-    *)
(*      Outlasts, TableOwnedWhileLive).                                    *)
(*   A3 FreshReads: every decision reads at HIGHER_CONSISTENCY             *)
(*      (fga._fresh_read_options; GrantsCachedCheck, NoAccessAfterRevoke). *)
(*                                                                         *)
(* NOT MODELLED: the last-admin guard (ADR 0213, its token on the project's *)
(* uuid, moved by each administration removal in its own Write);           *)
(* principals and labels; a Write's answer lost (a write that commits or   *)
(* not); time: "the create no longer runs" stands for the tick's bound     *)
(* (the request deadline plus a margin) and a create past it writes        *)
(* nothing more (warehouses.require_claim_held); the bytes are one flag,   *)
(* `foreign`, whether the bucket holds anything a create did not write.    *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, Sequences

CONSTANTS Parts,          \* which parts this config checks: a subset of {"container", "table", "converge"}
          MaxInc,         \* creations of the project id
          Granters,       \* door grants on the project
          Revokers,       \* door revokes of the tuple RevTarget's grant made
          RevTarget,      \* the granter whose tuple the revokers revoke
          Intruders,      \* callers the project authorizes for nothing, at a grant door
          Declarers,      \* the declaration's converge writing a grant git declares on the id
          Deleters,       \* deletes of the project id, each its own request
          Forcers,        \* the deletes asked with force=true: they pass a protected object's check
          Refusals,       \* a delete's check may refuse with nothing changed (the subtree, a clone, emptiness)
          Failures,       \* a delete's step between its mark and its stamp may fail, its request alive (a store or OpenFGA)
          Edges,          \* creates of the child id under the project, each minting its own uuid
          Child,          \* the children run
          Cascade,        \* the parent's delete ends a child (a namespace under a warehouse) rather than being refused
          Deaths,         \* a door may stop between its steps and never take the next
          ConvDeaths,     \* the converge tick may stop between its steps
          ResolveAtDoor,  \* U1
          SeedFirst,      \* U2
          LiveOnly,       \* U3
          ResolveAgain,   \* U4
          ChildRecordFirst, \* U5
          MarksFirst,     \* U6
          CountsCreating, \* U7
          TickFinishes,   \* U8
          TickChecksParent, \* U9
          TickSweeps,     \* U10
          LedgerPerCreation, \* U11
          EndsOwnCreation, \* U12
          RevertsRefusal, \* U13
          PutsBackOnFailure, \* U14
          TickSettlesMarks, \* U15
          ChecksTakeOver, \* U16
          ConditionalEnd, \* U17
          FreshFinish,    \* U18
          NoReturnFinal,  \* U19
          PutsBackOwnMark, \* U20
          OwnOnly,        \* G5
          Intents,        \* G13
          AuthorizeFirst, \* G14
          ReleasesUnborn, \* G19
          KeepsBorn,      \* G20
          PurgeStamp,     \* G22
          PurgeByBucket,  \* G24
          UuidKeyed,      \* A1
          UndropSettles,  \* A2
          FreshReads      \* A3

Incs == 1 .. MaxInc
Writers == Granters \cup Revokers \cup Intruders \cup Declarers
On(part) == part \in Parts
\* A warehouse's create claims a bucket; a namespace's does not.
Bucket == Child /\ ~Cascade

\* ---------------------------------------------------------------- container
VARIABLES prec,    \* the project's record: its state ("absent", "creating", "live", "deleting"), its uuid, the
                   \* delete whose mark it carries (by), and whether that delete passed its point of no return (nr)
          nextInc, \* the uuid the next create mints
          ended,   \* the project uuids a delete ended
          tup,     \* the tuples on and naming the project: [obj: the uuid written on, k: "seed", "grant" or "edge",
                   \* g: who, for: the uuid decided on]
          cpc,     \* each creation's create: "idle", "recorded", "seeded", "done", "dead"
          wpc,     \* each writer: "idle", "resolved", "pre", "gated", "written", "done", "dead"
          wu,      \* the uuid a writer resolved
          dlast,   \* the uuid each declarer last wrote on
          refused, \* the writers refused though no delete began while they ran
          dsince,  \* the writers a delete's mark ran beside
          narrowed,\* a revoke removed a tuple of a creation it was not decided on
          lied,    \* a door answered success though the id no longer named the uuid it wrote on live
          dstep,   \* each delete: 0 idle or answered, an index into DSteps, or DeadStep (it died)
          du,      \* the uuid each delete deletes
          dread,   \* the record each delete read when it began, which its mark is conditional on
          foreignEnd, \* a delete ended a record, or a child, of a creation it did not mark
          stuck,   \* a refused delete left its record `deleting` with nothing irreversible done
          unforced,\* a delete not asked with force passed its point of no return on a protected creation
          crec,    \* the child's record: its state, its uuid (an edge writer), the project uuid it names
          ctup,    \* the tuples on the child: [obj: its uuid, for: the project uuid its create was decided on]
          cended,  \* the child uuids ended
          estep,   \* each child create: 0 idle, 1 resolved, 2 .. 6 its next step in ESteps, 7 done, 8 dead
          eu,      \* the project uuid it resolved
          claim,   \* the bucket's claim: whether it stands, the child uuid it names (Other: another tenant's), stamped
          purge,   \* the estate owner's purge by bucket: its step and the child uuid the claim it took named
          purgedLive, \* a purge deleted the bytes of a bucket a live claim names
          foreign, \* the bucket holds bytes no create of the child writes (the tick then keeps its claim)
          wentLive,\* the child uuids that went live
          liveReleased, \* the tick released the claim of a child that went live
          tl,      \* the stamped record the tick's listing found, which its finish reads again (NoRecord: none)
          erased,  \* a delete put back a mark another delete's run held
          unasked  \* the tick ended a creation no delete had marked past its point of no return

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
          vinc,     \* the creation of its object the id names now
          held,     \* the tuple stands on that creation
          first,    \* who created it: "none", "git", "door"
          recorded, \* the creation the declaration's settled ledger record names (0: none)
          intent,   \* the ledger holds an intent to write it, not yet settled
          ev,       \* the evidence the tick's own Write leaves beside the tuple stands
          cvpc,     \* the converge: "idle", "read", "recorded" (record first); "read", "intent", "written" (intents)
          doorpc,   \* the door grant: "idle", "done"
          wrong     \* the converge revoked a tuple a door created

cvars == <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced,
           crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>
tvars == <<live, tnext, ttup, trashed, tphase, upc, uu, relpc, ru, tgpc, tgfor, everHeld, revoked, bad>>
vvars == <<declared, vinc, held, first, recorded, intent, ev, cvpc, doorpc, wrong>>
vars == <<cvars, tvars, vvars>>

MaxUuid == 2

NoRecord == [st |-> "absent", u |-> 0, by |-> "none", nr |-> FALSE, prot |-> FALSE]
NoChild == [st |-> "absent", u |-> "none", p |-> 0]
NoClaim == [held |-> FALSE, w |-> "none", purging |-> FALSE]
\* The child uuid another tenant's claim names: a child the model's project is not the parent of.
Other == "other"
NoPurge == [pc |-> "idle", w |-> "none"]
Seed(i) == [obj |-> i, k |-> "seed", g |-> "creator", for |-> i]
CSeed(e, p) == [obj |-> e, for |-> p]

Init ==
  /\ prec = NoRecord /\ nextInc = 1 /\ ended = {} /\ tup = {}
  /\ cpc = [i \in Incs |-> "idle"] /\ wpc = [w \in Writers |-> "idle"] /\ wu = [w \in Writers |-> 0]
  /\ dlast = [w \in Declarers |-> 0] /\ refused = {} /\ dsince = {} /\ narrowed = FALSE /\ lied = FALSE
  /\ dstep = [d \in Deleters |-> 0] /\ du = [d \in Deleters |-> 0] /\ dread = [d \in Deleters |-> NoRecord]
  /\ foreignEnd = FALSE /\ stuck = FALSE /\ unforced = FALSE
  /\ crec = NoChild /\ ctup = {} /\ cended = {} /\ estep = [e \in Edges |-> 0] /\ eu = [e \in Edges |-> 0]
  /\ claim = NoClaim /\ purge = NoPurge /\ purgedLive = FALSE /\ foreign \in (IF Bucket THEN BOOLEAN ELSE {FALSE})
  /\ wentLive = {} /\ liveReleased = FALSE /\ tl = NoRecord /\ erased = FALSE /\ unasked = FALSE
  /\ live = 0 /\ tnext = 1 /\ ttup = {} /\ trashed = {} /\ tphase = "grace" /\ upc = "idle" /\ uu = 0 /\ relpc = "idle" /\ ru = 0
  /\ tgpc = "idle" /\ tgfor = 0 /\ everHeld = FALSE /\ revoked = FALSE /\ bad = FALSE
  /\ declared = "not yet" /\ vinc = 1 /\ held = FALSE /\ first = "none" /\ recorded = 0 /\ intent = FALSE /\ ev = FALSE
  /\ cvpc = "idle" /\ doorpc = "idle" /\ wrong = FALSE

Live == prec.st = "live"
\* What a door resolves: only a live record, with U3.
Resolvable == IF LiveOnly THEN prec.st = "live" ELSE prec.st \in {"live", "deleting"}
\* The id names uuid u live: what the properties judge by.
NamesLive(u) == prec.st = "live" /\ prec.u = u
\* What the ONE resolver answers for uuid u, at a door's first and second resolve, a child's re-read and the tick's.
Resolves(u) == prec.u = u /\ Resolvable
\* A delete's steps, in its order. With U13 every check that can refuse with nothing changed (protection, the subtree,
\* a clone outside) runs before the mark; without it, after. With U6 the mark comes before the children are read. Past
\* "noreturn" the delete is irreversible: a refusal no longer puts the record back. Without U17 the end reads its mark
\* ("endread") and removes the record by its key in a later write ("end").
Ending == IF ConditionalEnd THEN <<"end">> ELSE <<"endread", "end">>
\* A step past the mark whose guard may refuse with nothing of it done (a purge's claim another purge holds,
\* warehouses.take_for_purge): with U19 before the stamp, without it after (the code before the third review).
Claim == IF Refusals THEN <<"claim">> ELSE <<>>
Stamp == IF NoReturnFinal THEN Claim \o <<"noreturn">> ELSE <<"noreturn">> \o Claim
DSteps ==
  (IF ~MarksFirst THEN <<"check", "children", "mark">>
   ELSE IF RevertsRefusal THEN <<"check", "mark", "children">>
   ELSE <<"mark", "check", "children">>) \o Stamp \o <<"cascade", "revoke">> \o Ending
NSteps == Len(DSteps)
DeadStep == NSteps + 1
\* Whether the step at index i comes after the delete's mark.
PastMark(i) == \E j \in 1 .. (i - 1) : DSteps[j] = "mark"
\* Whether the step at index i is past the mark and not past the no-return stamp: a failure there has done nothing
\* irreversible.
BeforeStamp(i) == PastMark(i) /\ \E j \in i .. NSteps : DSteps[j] = "noreturn"
\* A delete is running: a door beginning now is refused by it.
Deleting == (\E d \in Deleters : dstep[d] \in 1 .. NSteps) \/ prec.st = "deleting"

\* ---------------------------------------------------------------- the create

\* The record first, `creating`, naming the uuid the create minted; with U2 off it is written live.
CRecord(i) ==
  /\ On("container") /\ cpc[i] = "idle" /\ prec.st = "absent" /\ nextInc = i
  \* Protected or not, with force a delete may be asked with (a protection door's change is a write of the record, which
  \* a mark judged on the read before it refuses, so it is folded into the create).
  /\ \E p \in (IF Forcers = {} THEN {FALSE} ELSE BOOLEAN) :
       prec' = [NoRecord EXCEPT !.st = IF SeedFirst THEN "creating" ELSE "live", !.u = i, !.prot = p]
  /\ nextInc' = i + 1
  /\ cpc' = [cpc EXCEPT ![i] = "recorded"]
  /\ UNCHANGED <<ended, tup, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* Its seed, on the uuid it minted.
CSeedStep(i) ==
  /\ On("container") /\ cpc[i] = "recorded"
  /\ tup' = tup \cup {Seed(i)} /\ cpc' = [cpc EXCEPT ![i] = "seeded"]
  /\ UNCHANGED <<prec, nextInc, ended, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* The flip, conditional on the record still `creating` with its uuid.
CLive(i) ==
  /\ On("container") /\ cpc[i] = "seeded"
  /\ prec' = IF prec.st = "creating" /\ prec.u = i THEN [prec EXCEPT !.st = "live"] ELSE prec
  /\ cpc' = [cpc EXCEPT ![i] = "done"]
  /\ UNCHANGED <<nextInc, ended, tup, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

CDies(i) ==
  /\ On("container") /\ Deaths /\ cpc[i] \in {"recorded", "seeded"}
  /\ cpc' = [cpc EXCEPT ![i] = "dead"]
  /\ UNCHANGED <<prec, nextInc, ended, tup, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* U8: the tick finishes a creating record from what it names (its uuid and its creator).
TickFinish ==
  /\ On("container") /\ TickFinishes /\ prec.st = "creating"
  /\ tup' = tup \cup {Seed(prec.u)} /\ prec' = [prec EXCEPT !.st = "live"]
  /\ UNCHANGED <<nextInc, ended, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* ---------------------------------------------------------------- the doors

Running(w) == wpc[w] \in {"resolved", "pre", "gated", "written"}
\* The gate on the uuid resolved: its creator's seed authorizes every caller but an intruder; git is the declarers'.
Authorized(w) == w \in Declarers \/ (w \notin Intruders /\ Seed(wu[w]) \in tup)
\* The object a door writes on: with U1 the uuid it resolved, without it what the id names at the write.
WriteObj(w) == IF ResolveAtDoor THEN wu[w] ELSE prec.u

WResolve(w) ==
  /\ On("container") /\ (wpc[w] = "idle" \/ (w \in Declarers /\ wpc[w] = "dead"))
  \* A declarer: the tick, on a live creation its ledger's record is not about. With U11 a record about a creation that
  \* ended is forgotten and the grant declared afresh; without it, any record blocks the grant.
  /\ w \in Declarers => (Live /\ IF LedgerPerCreation THEN prec.u # dlast[w] ELSE dlast[w] = 0)
  /\ IF Resolvable
       THEN wu' = [wu EXCEPT ![w] = prec.u] /\ wpc' = [wpc EXCEPT ![w] = "resolved"]
       ELSE wpc' = [wpc EXCEPT ![w] = "done"] /\ UNCHANGED wu
  /\ dsince' = IF Deleting THEN dsince \cup {w} ELSE dsince
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, dlast, refused, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* G14: the gate before the write; with G14 off the write comes first and the gate after it.
WGate(w) ==
  /\ On("container") /\ wpc[w] = IF AuthorizeFirst THEN "resolved" ELSE "pre"
  /\ IF Authorized(w)
       THEN wpc' = [wpc EXCEPT ![w] = IF AuthorizeFirst THEN "gated" ELSE "written"] /\ UNCHANGED refused
       ELSE /\ wpc' = [wpc EXCEPT ![w] = "done"]
            /\ refused' = IF w \in dsince \/ w \in Intruders THEN refused ELSE refused \cup {w}
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wu, dlast, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* ONE Write on the object: a grant writes its tuple, a revoke removes RevTarget's grant there.
WWrite(w) ==
  /\ On("container") /\ wpc[w] = IF AuthorizeFirst THEN "gated" ELSE "resolved"
  /\ LET o == WriteObj(w) IN
       IF w \in Revokers
         THEN /\ tup' = {t \in tup : ~(t.obj = o /\ t.k = "grant" /\ t.g = RevTarget)}
              /\ narrowed' = (narrowed \/ \E t \in tup : t.obj = o /\ t.k = "grant" /\ t.g = RevTarget /\ t.for # wu[w])
         ELSE /\ tup' = IF \E t \in tup : t.obj = o /\ t.k = "grant" /\ t.g = w THEN tup
                        ELSE tup \cup {[obj |-> o, k |-> "grant", g |-> w, for |-> wu[w]]}
              /\ UNCHANGED narrowed
  /\ wpc' = [wpc EXCEPT ![w] = IF AuthorizeFirst THEN "written" ELSE "pre"]
  /\ UNCHANGED <<prec, nextInc, ended, cpc, wu, dlast, refused, dsince, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* U4: the id resolved again; success only where it still names the uuid written on live.
WAnswer(w) ==
  /\ On("container") /\ wpc[w] = "written"
  /\ LET ok == IF ResolveAgain THEN Resolves(wu[w]) ELSE TRUE
     IN /\ lied' = (lied \/ (ok /\ ~NamesLive(wu[w])))
        /\ refused' = IF ok \/ w \in dsince THEN refused ELSE refused \cup {w}
  /\ wpc' = [wpc EXCEPT ![w] = IF w \in Declarers THEN "idle" ELSE "done"]
  /\ dlast' = IF w \in Declarers THEN [dlast EXCEPT ![w] = wu[w]] ELSE dlast
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wu, dsince, narrowed, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

WDies(w) ==
  /\ On("container") /\ Deaths /\ Running(w)
  /\ wpc' = [wpc EXCEPT ![w] = "dead"]
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* ---------------------------------------------------------------- the child

\* The child create's steps after its gate, in its order: with U5 its record before it resolves its parent again.
ESteps == IF ChildRecordFirst THEN <<"claim", "record", "reread", "seed", "live">>
                              ELSE <<"claim", "reread", "record", "seed", "live">>
EDone == 7
EDead == 8
Creating(e) == estep[e] \in 1 .. 6
ChildStands(e) == crec.st # "absent" /\ crec.u = e
Edge(e, p) == [obj |-> p, k |-> "edge", g |-> e, for |-> eu[e]]

EResolve(e) ==
  /\ On("container") /\ Child /\ estep[e] = 0
  /\ IF Resolvable
       THEN eu' = [eu EXCEPT ![e] = prec.u] /\ estep' = [estep EXCEPT ![e] = 1]
       ELSE estep' = [estep EXCEPT ![e] = EDone] /\ UNCHANGED eu
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

EGate(e) ==
  /\ On("container") /\ Child /\ estep[e] = 1
  /\ estep' = [estep EXCEPT ![e] = IF Seed(eu[e]) \in tup THEN 2 ELSE EDone]
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* Abandoning: its record removed where it is still its own `creating` one; its claim stays for the tick.
Abandon(e) ==
  /\ crec' = IF crec.st = "creating" /\ crec.u = e THEN NoChild ELSE crec
  /\ cended' = IF crec.st = "creating" /\ crec.u = e THEN cended \cup {e} ELSE cended
  /\ estep' = [estep EXCEPT ![e] = EDone]

EStep(e) ==
  /\ On("container") /\ Child /\ estep[e] \in 2 .. 6
  /\ LET s == ESteps[estep[e] - 1]
         next == [estep EXCEPT ![e] = estep[e] + 1]
     IN CASE s = "claim" ->
               IF ~Bucket THEN estep' = next /\ UNCHANGED <<crec, cended, claim, tup, ctup, wentLive>>
               ELSE IF claim.held THEN estep' = [estep EXCEPT ![e] = EDone] /\ UNCHANGED <<crec, cended, claim, tup, ctup, wentLive>>
               ELSE claim' = [held |-> TRUE, w |-> e, purging |-> FALSE] /\ estep' = next /\ UNCHANGED <<crec, cended, tup, ctup, wentLive>>
          [] s = "record" ->
               IF crec.st # "absent" THEN estep' = [estep EXCEPT ![e] = EDone] /\ UNCHANGED <<crec, cended, claim, tup, ctup, wentLive>>
               ELSE crec' = [st |-> "creating", u |-> e, p |-> eu[e]] /\ estep' = next /\ UNCHANGED <<cended, claim, tup, ctup, wentLive>>
          [] s = "reread" ->
               IF Resolves(eu[e]) THEN estep' = next /\ UNCHANGED <<crec, cended, claim, tup, ctup, wentLive>>
               ELSE Abandon(e) /\ UNCHANGED <<claim, tup, ctup, wentLive>>
          [] s = "seed" ->
               /\ ctup' = ctup \cup {CSeed(e, eu[e])}
               /\ tup' = tup \cup {Edge(e, IF ResolveAtDoor THEN eu[e] ELSE prec.u)}
               /\ estep' = next /\ UNCHANGED <<crec, cended, claim, wentLive>>
          [] s = "live" ->
               /\ IF crec.st = "creating" /\ crec.u = e
                    THEN crec' = [crec EXCEPT !.st = "live"] /\ wentLive' = wentLive \cup {e}
                    ELSE UNCHANGED <<crec, wentLive>>
               /\ estep' = [estep EXCEPT ![e] = EDone] /\ UNCHANGED <<cended, claim, tup, ctup>>
  /\ UNCHANGED <<prec, nextInc, ended, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, eu, purge, purgedLive, foreign, liveReleased, tl, erased, unasked>>

EDies(e) ==
  /\ On("container") /\ Child /\ Deaths /\ Creating(e)
  /\ estep' = [estep EXCEPT ![e] = EDead]
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* The parent named by a child record is live: the record is the live tenant's.
ParentLive == Resolves(crec.p)
\* What the tick may release: a claim not stamped by a purge, on a bucket holding only what a create writes (G19).
Releasable == ReleasesUnborn /\ claim.held /\ ~claim.purging /\ ~foreign

\* U9: the tick finishes a creating child whose create no longer runs, under a live parent, holding its claim.
TickFinishChild ==
  /\ On("container") /\ Child /\ TickFinishes /\ crec.st = "creating" /\ ~Creating(crec.u)
  /\ TickChecksParent => ParentLive
  /\ Bucket => (claim.held /\ claim.w = crec.u)
  /\ ctup' = ctup \cup {[obj |-> crec.u, for |-> crec.p]}
  /\ tup' = tup \cup {[obj |-> crec.p, k |-> "edge", g |-> crec.u, for |-> crec.p]}
  /\ crec' = [crec EXCEPT !.st = "live"] /\ wentLive' = wentLive \cup {crec.u}
  /\ UNCHANGED <<prec, nextInc, ended, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, cended, estep, eu, claim, purge, purgedLive, foreign, liveReleased, tl, erased, unasked>>

\* The tick ends a creating child whose create no longer runs and whose parent is not live: its record removed, its
\* tuples swept with its uuid, its claim released where releasable. With G20 off it ends a live one too.
TickEndChild ==
  /\ On("container") /\ Child /\ TickFinishes
  /\ crec.st \in (IF KeepsBorn THEN {"creating"} ELSE {"creating", "live"})
  /\ ~Creating(crec.u) /\ ~ParentLive
  /\ LET rel == Bucket /\ Releasable /\ claim.w = crec.u
     IN /\ claim' = IF rel THEN NoClaim ELSE claim
        /\ liveReleased' = (liveReleased \/ (rel /\ crec.u \in wentLive))
  /\ crec' = NoChild /\ cended' = cended \cup {crec.u}
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, ctup, estep, eu, purge, purgedLive, foreign, wentLive, tl, erased, unasked>>

\* G19's other half: a claim naming a child uuid no record names, whose create no longer runs, is released.
ClaimEnd ==
  /\ On("container") /\ Bucket /\ Releasable /\ claim.w \in Edges /\ ~ChildStands(claim.w) /\ ~Creating(claim.w)
  /\ claim' = NoClaim
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* Another tenant claims the bucket wherever no claim stands (a released one included), and writes its own bytes there.
OtherClaim ==
  /\ On("container") /\ Bucket /\ ~claim.held
  /\ claim' = [held |-> TRUE, w |-> Other, purging |-> FALSE]
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* A claim of a child creation no record names and whose create no longer runs.
DeadClaim == claim.held /\ claim.w \in Edges /\ ~ChildStands(claim.w) /\ ~Creating(claim.w)

\* G24, G22: the estate owner's purge reaches a dead claim by its bucket and stamps it its own (under the read's ETag);
\* then deletes the bytes only while the stamp is its own, and releases the claim.
PurgeTake ==
  /\ On("container") /\ Bucket /\ PurgeByBucket /\ purge.pc = "idle" /\ DeadClaim
  /\ purge' = [pc |-> "taken", w |-> claim.w]
  /\ claim' = IF PurgeStamp THEN [claim EXCEPT !.purging = TRUE] ELSE claim
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

PurgeDelete ==
  /\ On("container") /\ Bucket /\ purge.pc = "taken"
  /\ LET del == IF PurgeStamp THEN claim.held /\ claim.purging /\ claim.w = purge.w ELSE TRUE
     IN /\ purgedLive' = (purgedLive \/ (del /\ claim.held /\ (claim.w # purge.w \/ ChildStands(claim.w))))
        /\ foreign' = (foreign /\ ~del)
        /\ claim' = IF del /\ claim.held /\ claim.w = purge.w THEN NoClaim ELSE claim
  /\ purge' = NoPurge
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, wentLive, liveReleased, tl, erased, unasked>>

\* ---------------------------------------------------------------- the delete

\* The states of a child the delete counts: with U7, one still creating.
Counted == IF CountsCreating THEN {"creating", "live"} ELSE {"live"}

\* The child a delete acts on: with U12, or where it refuses rather than ending (a warehouse under a project, counted by
\* the creation its record names), one made under the creation it deletes; without U12 a cascade selects by the id.
Selected(d) == IF EndsOwnCreation \/ ~Cascade THEN crec.p = du[d] ELSE TRUE

\* The delete's own mark stands: the record is `deleting`, of its creation, and marked by it. Without U12 any mark on
\* the creation is taken as its own (the stamp and the end keyed by the id).
Marked(d) == IF EndsOwnCreation THEN prec.st = "deleting" /\ prec.u = du[d] /\ prec.by = d
             ELSE prec.st = "deleting" /\ prec.u = du[d]

Advance(d) == dstep' = [dstep EXCEPT ![d] = dstep[d] + 1]

\* A step past the mark finds its mark gone (another delete took it over, or its creation ended): it answers, changing
\* nothing.
Stop(d) == dstep' = [dstep EXCEPT ![d] = 0] /\ UNCHANGED <<prec, stuck, unforced>>

\* Put back live (tenancy.put_back): a record still `deleting` for the delete's creation, with U20 only where the
\* delete's own run holds the mark (without it, whoever marked it), and with U19 only short of any point of no return
\* (without it, also past the delete's own stamp: a guard of its run refusing before any step of it landed).
Left(d) ==
  /\ prec.st = "deleting" /\ prec.u = du[d]
  /\ PutsBackOwnMark => prec.by = d
  /\ IF NoReturnFinal THEN ~prec.nr ELSE (~prec.nr \/ prec.by = d)
PutBack == [prec EXCEPT !.st = "live", !.by = "none", !.nr = FALSE]

\* A refusal: the delete answers. With U13 the record is put back; without, it is left marked, answered as absent by
\* every door.
Refuse(d) ==
  /\ dstep' = [dstep EXCEPT ![d] = 0]
  /\ prec' = IF Left(d) /\ RevertsRefusal THEN PutBack ELSE prec
  /\ stuck' = (stuck \/ (Left(d) /\ ~RevertsRefusal))
  /\ UNCHANGED unforced

\* A failure between the mark and the stamp, the request alive (a store or OpenFGA not answering). With U14 the record is
\* put back (tenancy.marked, around the span), or, where the put-back fails too, left marked and the caller told so, for
\* the tick (U15); without U14 it is left marked.
Fail(d) ==
  /\ dstep' = [dstep EXCEPT ![d] = 0]
  /\ IF PutsBackOnFailure
       THEN \/ prec' = (IF Left(d) THEN PutBack ELSE prec) /\ UNCHANGED stuck
            \/ UNCHANGED <<prec, stuck>>
       ELSE UNCHANGED prec /\ stuck' = (stuck \/ Left(d))
  /\ UNCHANGED unforced

\* Begun on a live record, or asked again on one a dead delete left; the record as read is what its mark is
\* conditional on (the ETag of that read).
DStart(d) ==
  /\ On("container") /\ dstep[d] \in {0, DeadStep} /\ prec.st \in {"live", "deleting"}
  /\ du' = [du EXCEPT ![d] = prec.u] /\ dstep' = [dstep EXCEPT ![d] = 1] /\ dread' = [dread EXCEPT ![d] = prec]
  /\ dsince' = dsince \cup {w \in Writers : Running(w)}
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, narrowed, lied, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* Whether the delete judges its checks on the record it read: with U16 unless that record is past a point of no return
\* (a resumed delete); without, only on a live one (a take-over of any mark skips them).
Judges(d) == IF ChecksTakeOver THEN ~dread[d].nr ELSE dread[d].st = "live"

DStep(d) ==
  /\ On("container") /\ dstep[d] \in 1 .. NSteps
  /\ LET s == DSteps[dstep[d]] IN
     \/ /\ Failures /\ BeforeStamp(dstep[d])
        /\ Fail(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
     \/ CASE s = "check" ->
                   \* Protection refuses a delete not asked with force; the other checks may refuse any delete.
                   IF Judges(d) /\ dread[d].prot /\ d \notin Forcers
                     THEN Refuse(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
                     ELSE \/ Advance(d) /\ UNCHANGED <<prec, stuck, unforced, dsince, ended, tup, crec, ctup, cended, foreignEnd>>
                          \/ Refusals /\ Refuse(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
              [] s = "mark" ->
                   /\ IF IF EndsOwnCreation THEN prec = dread[d] ELSE prec.u = du[d] /\ prec.st \in {"live", "deleting"}
                        THEN prec' = [prec EXCEPT !.st = "deleting", !.by = d] /\ Advance(d)
                        ELSE dstep' = [dstep EXCEPT ![d] = 0] /\ UNCHANGED prec
                   /\ dsince' = dsince \cup {w \in Writers : Running(w)}
                   /\ UNCHANGED <<stuck, unforced, ended, tup, crec, ctup, cended, foreignEnd>>
              [] s = "children" ->
                   IF Child /\ ~Cascade /\ Selected(d) /\ crec.st \in Counted
                     THEN Refuse(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
                     ELSE Advance(d) /\ UNCHANGED <<prec, stuck, unforced, dsince, ended, tup, crec, ctup, cended, foreignEnd>>
              [] s = "claim" ->
                   \* The purge's claim, which another purge may hold: refused with nothing of this step done.
                   \/ Advance(d) /\ UNCHANGED <<prec, stuck, unforced, dsince, ended, tup, crec, ctup, cended, foreignEnd>>
                   \/ Refuse(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
              [] s = "noreturn" ->
                   \* Written under the read that found its own mark (tenancy.pass_no_return).
                   IF Marked(d)
                     THEN /\ prec' = [prec EXCEPT !.nr = TRUE]
                          /\ unforced' = (unforced \/ (prec.prot /\ d \notin Forcers /\ ~prec.nr))
                          /\ Advance(d) /\ UNCHANGED <<stuck, dsince, ended, tup, crec, ctup, cended, foreignEnd>>
                     ELSE Stop(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
              [] s = "cascade" ->
                   /\ IF Child /\ Cascade /\ Selected(d) /\ crec.st \in Counted
                        THEN /\ crec' = NoChild /\ cended' = cended \cup {crec.u}
                             /\ foreignEnd' = (foreignEnd \/ crec.p # du[d])
                        ELSE UNCHANGED <<crec, cended, foreignEnd>>
                   /\ Advance(d) /\ UNCHANGED <<prec, stuck, unforced, dsince, ended, tup, ctup>>
              [] s = "revoke" ->
                   /\ tup' = {t \in tup : t.obj # du[d]}
                   /\ Advance(d) /\ UNCHANGED <<prec, stuck, unforced, dsince, ended, crec, ctup, cended, foreignEnd>>
              [] s = "endread" ->
                   \* Without U17: the read that finds its own mark, the removal by the key a later write.
                   IF Marked(d) THEN Advance(d) /\ UNCHANGED <<prec, stuck, unforced, dsince, ended, tup, crec, ctup, cended, foreignEnd>>
                   ELSE Stop(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
              [] s = "end" ->
                   \* With U17 one removal conditional on the read that found its own mark (DeleteObject If-Match).
                   IF ConditionalEnd /\ ~Marked(d)
                     THEN Stop(d) /\ UNCHANGED <<dsince, ended, tup, crec, ctup, cended, foreignEnd>>
                     ELSE /\ foreignEnd' = (foreignEnd \/ (prec.st # "absent" /\ prec.u # du[d]))
                          /\ prec' = NoRecord
                          /\ ended' = ended \cup {du[d]} /\ dstep' = [dstep EXCEPT ![d] = 0]
                          /\ UNCHANGED <<stuck, unforced, dsince, tup, crec, ctup, cended>>
  \* A put-back (a refusal's or a failure's) of a mark another delete's run holds.
  /\ erased' = (erased \/ (prec.st = "deleting" /\ prec'.st = "live" /\ prec.by # d))
  /\ UNCHANGED <<nextInc, cpc, wpc, wu, dlast, refused, narrowed, lied, du, dread, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, unasked>>

DDies(d) ==
  /\ On("container") /\ Deaths /\ dstep[d] \in 1 .. NSteps
  /\ dstep' = [dstep EXCEPT ![d] = DeadStep]
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* The run whose mark the record carries no longer runs: it answered, or died, or its request is older than the tick's
\* bound (the record's `marked_at`).
MarkerGone == ~(prec.by \in Deleters /\ dstep[prec.by] \in 1 .. NSteps)

\* U15: the tick puts back a mark with no stamp whose run no longer runs, conditional on the read that found it so;
\* nothing irreversible was done under it.
TickPutBack ==
  /\ On("container") /\ TickSettlesMarks /\ prec.st = "deleting" /\ ~prec.nr /\ MarkerGone
  /\ prec' = PutBack
  /\ UNCHANGED <<nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

\* U15: the tick's listing of the registry finds a stamped mark whose run no longer runs (tenancy_converge._deleting).
TickList ==
  /\ On("container") /\ TickSettlesMarks /\ tl = NoRecord /\ prec.st = "deleting" /\ prec.nr /\ MarkerGone
  /\ tl' = prec
  /\ UNCHANGED <<prec, nextInc, ended, tup, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, ctup, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, erased, unasked>>

\* What the finish acts on, read again after the listing (the delete door's own read, tenancy.judged). With U18 only a
\* record still carrying the listed run past its stamp, the mark taken over under that read's ETag; without it any record
\* of the listed uuid the delete door serves, live or marked, judged as a new delete with no caller, which only a
\* protection refuses (the code before the third review).
FinishActs ==
  IF FreshFinish THEN prec.st = "deleting" /\ prec.nr /\ prec.u = tl.u /\ prec.by = tl.by
  ELSE prec.u = tl.u /\ prec.st \in {"live", "deleting"} /\ ~(~prec.nr /\ prec.prot)

\* U15: the tick finishes what its listing found: it takes the mark over as a delete of its own and runs the steps past
\* the stamp from what the record names (one step here; each is a delete's step, which the deleters model): the
\* creation's cascaded child, its tuples, its record. Where the read finds anything else it does nothing.
TickFinishDelete ==
  /\ On("container") /\ TickSettlesMarks /\ tl # NoRecord
  /\ tl' = NoRecord
  /\ IF FinishActs
       THEN /\ IF Child /\ Cascade /\ crec.p = prec.u /\ crec.st \in Counted
                 THEN crec' = NoChild /\ cended' = cended \cup {crec.u}
                 ELSE UNCHANGED <<crec, cended>>
            /\ tup' = {t \in tup : t.obj # prec.u}
            /\ ended' = ended \cup {prec.u} /\ prec' = NoRecord
            /\ unasked' = (unasked \/ ~(prec.st = "deleting" /\ prec.nr))
       ELSE UNCHANGED <<crec, cended, tup, ended, prec, unasked>>
  /\ UNCHANGED <<nextInc, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, ctup, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, erased>>

\* U10: hygiene. What stands on an ended uuid, written by a door that raced its delete, is revoked.
Sweep ==
  /\ On("container") /\ TickSweeps
  /\ (\E t \in tup : t.obj \in ended) \/ (\E t \in ctup : t.obj \in cended)
  /\ tup' = {t \in tup : t.obj \notin ended} /\ ctup' = {t \in ctup : t.obj \notin cended}
  /\ UNCHANGED <<prec, nextInc, ended, cpc, wpc, wu, dlast, refused, dsince, narrowed, lied, dstep, du, dread, foreignEnd, stuck, unforced, crec, cended, estep, eu, claim, purge, purgedLive, foreign, wentLive, liveReleased, tl, erased, unasked>>

ContainerNext ==
  \/ \E i \in Incs : CRecord(i) \/ CSeedStep(i) \/ CLive(i) \/ CDies(i)
  \/ \E w \in Writers : WResolve(w) \/ WGate(w) \/ WWrite(w) \/ WAnswer(w) \/ WDies(w)
  \/ \E e \in Edges : EResolve(e) \/ EGate(e) \/ EStep(e) \/ EDies(e)
  \/ TickFinish \/ TickFinishChild \/ TickEndChild \/ ClaimEnd \/ OtherClaim \/ PurgeTake \/ PurgeDelete
  \/ \E d \in Deleters : DStart(d) \/ DStep(d) \/ DDies(d)
  \/ TickPutBack \/ TickList \/ TickFinishDelete
  \/ Sweep

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

GitDeclares == /\ On("converge") /\ declared = "not yet" /\ declared' = "yes" /\ UNCHANGED <<vinc, held, first, recorded, intent, ev, cvpc, doorpc, wrong>>
GitDrops == /\ On("converge") /\ declared = "yes" /\ declared' = "dropped" /\ UNCHANGED <<vinc, held, first, recorded, intent, ev, cvpc, doorpc, wrong>>

\* Between ticks the object is deleted and made again under its id: what stood on the old creation authorizes nothing
\* on the new one, and the ledger's record still names the old.
Recreate ==
  /\ On("converge") /\ vinc < MaxInc /\ cvpc = "idle" /\ ~intent
  /\ vinc' = vinc + 1 /\ held' = FALSE /\ first' = "none" /\ doorpc' = "idle"
  /\ UNCHANGED <<declared, recorded, intent, ev, cvpc, wrong>>

\* The tick's read of the store, again right before it records: a tuple it finds is somebody's, and is left so.
ConvRead ==
  /\ On("converge") /\ cvpc = "idle" /\ declared = "yes" /\ ~held /\ ~intent
  /\ cvpc' = "read"
  /\ UNCHANGED <<declared, vinc, held, first, recorded, intent, ev, doorpc, wrong>>

\* Record first (G13 off, the code before): the tuple recorded git's before it is written.
ConvRecord ==
  /\ On("converge") /\ ~Intents /\ cvpc = "read"
  /\ recorded' = vinc /\ cvpc' = "recorded"
  /\ UNCHANGED <<declared, vinc, held, first, intent, ev, doorpc, wrong>>

\* The record-first write: guarded, it commits only where the tuple is absent and forgets its record where one stands.
ConvWriteRecorded ==
  /\ On("converge") /\ ~Intents /\ cvpc = "recorded"
  /\ IF held
       THEN /\ recorded' = (IF OwnOnly THEN 0 ELSE recorded) /\ UNCHANGED <<held, first>>
       ELSE /\ held' = TRUE /\ first' = "git" /\ UNCHANGED recorded
  /\ cvpc' = "idle"
  /\ UNCHANGED <<declared, vinc, intent, ev, doorpc, wrong>>

\* G13: an intent recorded (created iff no record stands), then one Write of the tuple and the tick's evidence,
\* committing only where the tuple is absent. A settled record finds no tuple it names standing here: with U11 it is
\* forgotten and the intent recorded; without it the intent is refused and the tick moves on, silently.
ConvIntent ==
  /\ On("converge") /\ Intents /\ cvpc = "read"
  /\ IF recorded = 0 \/ LedgerPerCreation
       THEN recorded' = 0 /\ intent' = TRUE /\ cvpc' = "intent"
       ELSE cvpc' = "idle" /\ UNCHANGED <<recorded, intent>>
  /\ UNCHANGED <<declared, vinc, held, first, ev, doorpc, wrong>>

ConvWriteEvidenced ==
  /\ On("converge") /\ Intents /\ cvpc = "intent"
  /\ IF held THEN UNCHANGED <<held, first, ev>> ELSE held' = TRUE /\ first' = "git" /\ ev' = TRUE
  /\ cvpc' = "written"
  /\ UNCHANGED <<declared, vinc, recorded, intent, doorpc, wrong>>

\* The intent settled, by this tick or a later one: git's where its evidence stands, forgotten where it does not.
ConvSettle ==
  /\ On("converge") /\ Intents /\ intent /\ cvpc \in {"idle", "written"}
  /\ recorded' = (IF ev THEN vinc ELSE recorded) /\ ev' = FALSE /\ intent' = FALSE /\ cvpc' = "idle"
  /\ UNCHANGED <<declared, vinc, held, first, doorpc, wrong>>

ConvDies ==
  /\ On("converge") /\ ConvDeaths /\ cvpc \in {"read", "recorded", "intent", "written"}
  /\ cvpc' = "idle"
  /\ UNCHANGED <<declared, vinc, held, first, recorded, intent, ev, doorpc, wrong>>

\* The tick revokes a tuple git stopped declaring on the creation its record names; a record of a creation that ended is
\* forgotten, its tuple gone with that creation.
ConvRevoke ==
  /\ On("converge") /\ cvpc = "idle" /\ declared = "dropped" /\ recorded # 0 /\ ~intent
  /\ IF recorded = vinc
       THEN /\ wrong' = (wrong \/ (held /\ first = "door"))
            /\ held' = FALSE /\ first' = "none"
       ELSE UNCHANGED <<wrong, held, first>>
  /\ recorded' = 0
  /\ UNCHANGED <<declared, vinc, intent, ev, cvpc, doorpc>>

DoorGrants ==
  /\ On("converge") /\ doorpc = "idle"
  /\ IF held THEN UNCHANGED <<held, first>> ELSE held' = TRUE /\ first' = "door"
  /\ doorpc' = "done"
  /\ UNCHANGED <<declared, vinc, recorded, intent, ev, cvpc, wrong>>

ConvergeNext ==
  GitDeclares \/ GitDrops \/ Recreate \/ ConvRead \/ ConvRecord \/ ConvWriteRecorded \/ ConvIntent \/ ConvWriteEvidenced
    \/ ConvSettle \/ ConvDies \/ ConvRevoke \/ DoorGrants


Next ==
  \/ ContainerNext /\ UNCHANGED <<tvars, vvars>>
  \/ TableNext /\ UNCHANGED <<cvars, vvars>>
  \/ ConvergeNext /\ UNCHANGED <<cvars, tvars>>

\* A request ends, by its steps or its death (the tick's bound is longer than a request runs); the tick runs on every
\* period until nothing is left for it; the estate owner purges what the tick reports and keeps because its bucket holds
\* bytes no create wrote, and a purge begun is finished. A delete refused and put back may be asked again for ever, so
\* its parent is live and marked by turns: the tick, judging a creating child on each period, settles one it may settle
\* infinitely often (strong fairness).
Spec ==
  /\ Init /\ [][Next]_vars
  /\ \A i \in Incs : WF_vars((CSeedStep(i) \/ CLive(i)) /\ UNCHANGED <<tvars, vvars>>)
  /\ \A e \in Edges : WF_vars((EGate(e) \/ EStep(e)) /\ UNCHANGED <<tvars, vvars>>)
  /\ WF_vars(TickFinish /\ UNCHANGED <<tvars, vvars>>)
  /\ SF_vars(TickFinishChild /\ UNCHANGED <<tvars, vvars>>)
  /\ SF_vars(TickEndChild /\ UNCHANGED <<tvars, vvars>>)
  /\ WF_vars(Sweep /\ UNCHANGED <<tvars, vvars>>)
  \* A delete's request runs its steps to an answer; the tick, judging each mark on every period, settles one a
  \* delete asked again may hold for a while (strong fairness).
  /\ \A d \in Deleters : WF_vars(DStep(d) /\ UNCHANGED <<tvars, vvars>>)
  /\ SF_vars(TickPutBack /\ UNCHANGED <<tvars, vvars>>)
  /\ SF_vars(TickList /\ UNCHANGED <<tvars, vvars>>)
  /\ WF_vars(TickFinishDelete /\ UNCHANGED <<tvars, vvars>>)
  /\ WF_vars(ClaimEnd /\ UNCHANGED <<tvars, vvars>>)
  /\ SF_vars(PurgeTake /\ foreign /\ UNCHANGED <<tvars, vvars>>)
  /\ WF_vars(PurgeDelete /\ UNCHANGED <<tvars, vvars>>)
  \* The declaration's converge runs on every period: a declarer's steps, and the converge part's tick.
  /\ \A w \in Declarers : WF_vars((WResolve(w) \/ WGate(w) \/ WWrite(w) \/ WAnswer(w)) /\ UNCHANGED <<tvars, vvars>>)
  /\ WF_vars((ConvRead \/ ConvIntent \/ ConvWriteEvidenced \/ ConvSettle) /\ UNCHANGED <<cvars, tvars>>)

\* ------------------------------------------------------------------ properties

\* No access leaks across creations: every tuple on or naming the uuid a standing record names was decided on that uuid,
\* so nothing a deleted project held, or a door was writing on or under it, authorizes on one made again under its id.
NoLeakAcrossIncarnations == prec.st # "absent" => \A t \in tup : t.obj = prec.u => t.for = prec.u

\* Its narrowing twin: no tuple of one creation is removed by a revoke decided on another.
NoNarrowAcrossIncarnations == ~narrowed

\* Only a caller the project authorizes writes on it.
OnlyAuthorizedWrites == \A t \in tup : t.g \notin Intruders

\* No missing owner: a live record holds its seed, the project's and the child's.
OwnedWhileLive == (Live => Seed(prec.u) \in tup) /\ (crec.st = "live" => CSeed(crec.u, crec.p) \in ctup)

\* Grants do not exclude grants: a door is refused only by a delete running beside it.
GrantRefusedOnlyByDelete == refused = {}

\* A door answers success only where the id still names, live, the uuid it wrote on.
GrantAnswersTruthfully == ~lied

\* A live child names its parent's uuid, and that parent's record stands: no child is live under a creation that ended.
ChildUnderLiveParent == crec.st = "live" => (crec.p = prec.u /\ prec.st \in {"live", "deleting"})

\* A delete ends only the creation it marked: no record, and no child, of a creation made again under the id.
DeleteEndsOnlyItsOwn == ~foreignEnd

\* A delete that stopped short of its point of no return, refused or failing, leaves its object live: its mark is put
\* back (or, where the put-back itself fails, the caller is told and the tick puts it back).
RefusedDeleteLeavesLive == ~stuck

\* A protected creation is deleted only by a delete asked with force: a delete that takes over a mark short of its
\* point of no return judges the protection again.
ProtectedDeletedOnlyByForce == ~unforced

\* A delete's mark is put back only by the delete whose run holds it: a delete that took the mark over is not undone by
\* the one it took it from (which, its stamp and its end finding the mark gone, then answers for an object that stands).
PutBackOnlyItsOwn == ~erased

\* The tick never begins a delete: it ends only a creation a delete marked past its point of no return.
TickBeginsNoDelete == ~unasked

\* A point of no return is one: a creation stamped past it is ended in the end, never put back live.
StampedEnds == \A i \in Incs : (prec.u = i /\ prec.nr) ~> (prec.u # i \/ prec.st = "absent")

\* Nothing stays `deleting`: a mark is put back live, or its creation ended, with no hand-run step.
DeletingSettles == (prec.st = "deleting") ~> (prec.st \in {"live", "absent"})

\* A grant git declares is eventually held on every creation the id names, a project made again under it included.
DeclaredOnLive == \A i \in Incs : NamesLive(i) ~> (~NamesLive(i) \/ \A d \in Declarers : \E t \in tup : t.obj = i /\ t.g = d)

\* The converge part's twin: the declared tuple is eventually held on the creation of its object the id names now.
DeclaredHeld == \A i \in Incs : (declared = "yes" /\ vinc = i) ~> (held \/ declared # "yes" \/ vinc # i)

\* Every creating record goes live or is ended, with no hand-run step.
CreatingSettles == /\ (prec.st = "creating") ~> (prec.st # "creating")
                   /\ (crec.st = "creating") ~> (crec.st # "creating")

\* What stands on an ended uuid is revoked in the end.
DeadObjectsSwept == \A i \in Incs : (i \in ended /\ \E t \in tup : t.obj = i) ~> ~(\E t \in tup : t.obj = i)

\* No tenant takes what a child that went live holds: the tick never releases the claim of one.
BornLeftKept == ~liveReleased

\* A claim of a child creation no record names is released in the end, with no hand-run step but the estate owner's
\* purge of a bucket holding bytes no create wrote.
ClaimFreed == \A e \in Edges : (claim.held /\ claim.w = e /\ ~ChildStands(e)) ~> ~(claim.held /\ claim.w = e /\ ~ChildStands(e))

\* Bytes are deleted only under a claim the purge itself holds.
PurgeTakesNothingLive == ~purgedLive

\* No access leaks across tables: a tuple on the table a name holds was decided on that table.
TableNoLeak == live # 0 => \A t \in ttup : t.obj = Key(live) => t.for = live

\* A table the name holds has its owner's grant, outside an undrop's own two steps.
TableOwnedWhileLive == (live # 0 /\ ~Undropping) => TSeed(live) \in ttup

\* No access outlives its grant: once a revoke answered, no reader is allowed by the revoked tuple.
NoAccessAfterRevoke == ~bad

\* Git's grants and door grants do not fight: the converge revokes only a tuple it created.
GitRevokesOnlyItsOwn == ~wrong

TypeOK ==
  /\ prec.st \in {"absent", "creating", "live", "deleting"} /\ prec.u \in 0 .. MaxInc /\ ended \subseteq Incs
  /\ \A d \in Deleters : dstep[d] \in 0 .. DeadStep
  /\ prec.by \in Deleters \cup {"none"} /\ prec.prot \in BOOLEAN /\ crec.st \in {"absent", "creating", "live"} /\ cended \subseteq Edges
  /\ live \in 0 .. MaxUuid /\ held \in BOOLEAN /\ recorded \in 0 .. MaxInc /\ vinc \in Incs
=============================================================================
