-------------------------- MODULE WriteGovernance --------------------------
(***************************************************************************)
(* Write governance: what a commit must keep, judged on one read and       *)
(* landed later (ADR 0244). Two seams, one module: each is a rule a door   *)
(* reads before its write lands, so each is the same question, whether    *)
(* the rule can change between the read and the landing; they share no    *)
(* state, and a config runs one of them (the other's actors are empty).   *)
(*                                                                         *)
(* THE DATA CONTRACT (ADR 0212). A table's owner sets a contract           *)
(* (endpoints/contracts.py set_table_contract, contracts.keep) and every   *)
(* commit is judged against it (contracts.require_kept): it may not        *)
(* introduce a violation of the contract, one the version it follows did   *)
(* not have. A contract is the set of columns it names, a schema the set   *)
(* of columns a version keeps as named (a retype or a loosening is a       *)
(* column the contract no longer finds as it states it), and {} is no      *)
(* contract. The record a set writes is IN FLIGHT until the set's barrier  *)
(* has landed on every ref and the set marks it IN FORCE                   *)
(* (contracts.confirm); an in-flight record carries the in-force record it *)
(* replaced, which a refusal puts back. Its actors:                        *)
(*   exact     a commit finalized exactly at the slot after the version it *)
(*             planned on, by the store's put-if-not-exists: every door's  *)
(*             stamped commit (dataplane.commit_stamped: plan, read the    *)
(*             contract, judge, finalize, plan again on a lost race) and a *)
(*             stock client's (versions.create_table_version:              *)
(*             managed_versions.stage on the version it follows, judge     *)
(*             with the contract read there, finalize; a lost race is the  *)
(*             client's 409 and its retry stages again, here a replan).    *)
(*             Each writer drops column a, or adds column b, or adds a     *)
(*             back, or restores the table's first version                 *)
(*             (dataplane.restore).                                        *)
(*   overwrite /create mode=Overwrite of a table (dataplane.create_table): *)
(*             judged against the head and the contract it read first;     *)
(*             landed by Lance's own commit, which lands an Overwrite over *)
(*             whatever committed since (lance_docs/lance/format/table/    *)
(*             transaction.md § Overwrite Compatibility; measured on       *)
(*             pylance 13.0.0: over an UpdateConfig of the table's config, *)
(*             its table metadata or its schema metadata alike), unless    *)
(*             OverwriteStamped makes it an exact commit.                  *)
(*   set       the contract door: a first read of every ref's head         *)
(*             (_require_described), the record kept in flight             *)
(*             (contracts.keep), then either a SECOND READ of every ref    *)
(*             (Barrier off) or a BARRIER on every ref listed after the    *)
(*             record stood: an exact commit, an UpdateConfig that changes *)
(*             nothing, whose plan judges the ref's head against the new   *)
(*             contract (Barrier); then the record marked in force         *)
(*             (contracts.confirm). A refusal after the record stood takes *)
(*             it back (contracts.take_back). Set s1 states column a, set  *)
(*             s2 column c only: a major version that no longer names a.   *)
(*   delete    the contract's delete door (contracts.forget).              *)
(*   branch    a branch created from main's head (dataplane.create_branch).*)
(*                                                                         *)
(* THE RUN RULE (ADR 0211). A namespace declares rask.requires_run at its  *)
(* create. Namespace x declares nothing at first; an actor makes it afresh *)
(* declaring the rule, in the code's steps: drop x (its Restrict drop,     *)
(* under x's DROP fence, refused while a table stands in x or an add holds *)
(* x's fence), forget its grants (namespaces._forget_namespace), create x  *)
(* again under its PARENT's add fence, which nothing on x excludes. Mix    *)
(* "rule": one request, the namespace Overwrite                            *)
(* (_create_or_replace_empty_namespace), which holds x's DROP fence from   *)
(* its emptiness check through its re-create when OverwriteHeld. Mix      *)
(* "redeclare": two requests, the drop door and then a create door, with   *)
(* nothing held between them. The actors placing a table that holds data   *)
(* no run produced into x:                                                 *)
(*   create    a /create of a new table naming no run: judged by the       *)
(*             write judge (write_judge.judge), then declared into x       *)
(*             (dataplane.declare_table's register, under the ADD fence),  *)
(*             then its data committed;                                    *)
(*   register  /register of bytes written elsewhere (refused under the     *)
(*             rule, UNSTAMPABLE_DATA_DOORS);                              *)
(*   rename    a rename into x from a namespace that does not require a    *)
(*             run (refused into one that does, write_judge.judge);        *)
(*   undrop    the undrop of a table soft-dropped from x, which lands it   *)
(*             in x only while x is the namespace it was dropped from      *)
(*             (UndropChecksParent).                                       *)
(*                                                                         *)
(* THE PROPERTY. No committed version of a governed table breaks the rule  *)
(* that stood when it committed:                                           *)
(*   ContractKept     a version a writer lands introduces no violation of  *)
(*                    the contract in force when it lands: the record once *)
(*                    marked, and while a set is in flight the in-force    *)
(*                    record it replaced, which its refusal puts back (the *)
(*                    in-flight contract is that set's to judge: its       *)
(*                    barrier, or its second read, sees the ref);          *)
(*   RollbackRestoresInForce  a refused set puts back the contract that    *)
(*                    was in force when it wrote (the last one answered    *)
(*                    200 by a set or the delete door, or none), never     *)
(*                    another: a set answered 200 is replaced only by a    *)
(*                    later set or delete, and a refused contract never    *)
(*                    stands in force;                                     *)
(*   RunRuleHeld      no table stands in a namespace that requires a run   *)
(*                    holding data no run produced;                        *)
(*   OverwriteUnseen  no placer holds x's ADD fence while the Overwrite is *)
(*                    between its drop and its re-create.                  *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. A version slot is filled put-if-not-exists (CommitPath A1).       *)
(*   A2. A record write is a put, a conditional write a replace under the  *)
(*       ETag of the read it judged (lakehouse.records.exchange_json).     *)
(*   A3. A namespace's rule changes only by its create (ADR 0211 §2: no    *)
(*       Lance operation alters a namespace's properties), and a drop      *)
(*       lands only once the namespace is empty.                           *)
(*   A4. The namespace fence: an add and a drop of one namespace never     *)
(*       hold it together (lakehouse.namespace_fences, Fence.tla).         *)
(*                                                                         *)
(* GUARDS (each a step of the design; Off names the ones a config removes, *)
(* and each such config must fail):                                        *)
(*   Judged          every commit reads the contract after its plan and    *)
(*                   refuses a violation it introduces                     *)
(*                   (contracts.require_kept in commit_stamped and         *)
(*                   managed_versions.judge).                              *)
(*   JudgedStrict    while a set is in flight a commit is judged against   *)
(*                   both its contract and the in-force one it replaced    *)
(*                   (ContractRecord.binding).                             *)
(*   Barrier         a set lands an exact commit on every ref listed after *)
(*                   its record stood, whose plan judges that ref's head   *)
(*                   against the contract (_verify_on_every_ref).          *)
(*   OverwriteStamped  a table's Overwrite is a stamped commit, judged     *)
(*                   against the version it lands on                       *)
(*                   (dataplane._overwrite_stamped).                       *)
(*   RollbackOwn     a refused set takes back only the record it wrote     *)
(*                   (contracts.take_back, under the ETag of a read that   *)
(*                   found its own record).                                *)
(*   ReplacedInKeep  the record a refused set puts back is the one its own *)
(*                   conditional write replaced (contracts.keep, through   *)
(*                   exchange_json), not one read before it.               *)
(*   RestoreInForce  what a set's record replaced is the record in force:  *)
(*                   over an in-flight record, the one that record         *)
(*                   carries (contracts.keep's _in_force).                 *)
(*   MarkOwn         a set answers 200 only once it marked its own record  *)
(*                   in force (contracts.confirm, under the ETag of a read *)
(*                   that found it); another's record refuses it 409.      *)
(*   PlacedUnderHold a door that places a table into a namespace reads the *)
(*                   namespace's rule while it holds the namespace's ADD   *)
(*                   fence, which it keeps until the table is added        *)
(*                   (write_judge.placing).                                *)
(*   PlacedStanding  inside that hold a placement into a namespace that    *)
(*                   does not stand is refused before the rule is read     *)
(*                   (write_judge.placing, requires_run(must_stand=True)). *)
(*   OverwriteHeld   the namespace Overwrite holds x's DROP fence from its *)
(*                   emptiness check through its re-create                 *)
(*                   (_create_or_replace_empty_namespace).                 *)
(*   RenameJudged    a rename into a namespace requiring a run from one    *)
(*                   that does not is refused (write_judge.judge).         *)
(*   RegisterJudged  a register under the rule is refused                  *)
(*                   (UNSTAMPABLE_DATA_DOORS).                             *)
(*   UndropChecksParent  an undrop lands only in the namespace its table   *)
(*                   was dropped from, by its uuid (tables.py undrop).     *)
(*                                                                         *)
(* NOT MODELLED, each argued in ADR 0244: the columns' types and           *)
(* nullability (a contracted column kept at another type or looser is a    *)
(* missing column here, as violations() reads it), a contract's own        *)
(* evolution rule (contracts.require_evolves, a check on the document),    *)
(* the record unreadable (fail closed: every commit refused), branches cut *)
(* from a version older than the contract (ADR 0212 §4: a violation the    *)
(* version followed had is not the next writer's), the write judge's rung  *)
(* (the router's), a run's inputs (judged per commit, on its own read), a  *)
(* write into a table already standing in a namespace (its namespace       *)
(* cannot be dropped while it stands there, A3), and the lifecycle doors'  *)
(* own fences (TableLifecycle.tla, Cascade.tla).                           *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Mix,       \* which actors: "gate", "branch", "sets", "rule", "redeclare"
          MaxTries,  \* lost races an exact commit, or a set's barrier, plans again after
          Off        \* the guards a config removes

Guards == {"Judged", "JudgedStrict", "Barrier", "OverwriteStamped", "RollbackOwn", "ReplacedInKeep", "RestoreInForce", "MarkOwn",
           "PlacedUnderHold", "PlacedStanding", "OverwriteHeld", "RenameJudged", "RegisterJudged", "UndropChecksParent"}
ASSUME Off \subseteq Guards
On(g) == g \notin Off

Cols == {"a", "b", "c"}
Refs == {"main", "br"}

\* The actors of each mix. "gate": a drop and an add, each an exact commit, and an Overwrite dropping the column, beside
\* one set. "branch": a branch made while a set runs, a drop committed on it, and a restore on main. "sets": two sets
\* and a delete beside a drop and a commit that adds the column back. "rule", "redeclare": one door of each kind
\* placing a table.
Writers == CASE Mix = "gate" -> {"w1", "w2", "w3"} [] Mix \in {"branch", "sets"} -> {"w1", "w2"} [] OTHER -> {}
Op == [w \in Writers |->
         CASE Mix = "gate" -> (IF w = "w2" THEN "add" ELSE "drop")
           [] Mix = "branch" -> (IF w = "w1" THEN "drop" ELSE "restore")
           [] OTHER -> (IF w = "w1" THEN "drop" ELSE "readd")]
Way == [w \in Writers |-> IF Mix = "gate" /\ w = "w3" THEN "overwrite" ELSE "exact"]
Ref == [w \in Writers |-> IF Mix = "branch" /\ w = "w1" THEN "br" ELSE "main"]
Sets == CASE Mix = "sets" -> {"s1", "s2"} [] Mix \in {"rule", "redeclare"} -> {} [] OTHER -> {"s1"}
\* The contract each set states.
K == [s \in Sets |-> IF s = "s2" THEN {"c"} ELSE {"a"}]
Deletes == IF Mix = "sets" THEN {"d1"} ELSE {}
Branching == Mix = "branch"
Placers == IF Mix \in {"rule", "redeclare"} THEN {"p1", "p2", "p3", "p4"} ELSE {}
Door == [p \in Placers |-> CASE p = "p1" -> "create" [] p = "p2" -> "register" [] p = "p3" -> "rename" [] OTHER -> "undrop"]
Doors == Sets \cup Deletes
\* Whether the actor remaking x keeps x's DROP fence across its steps: only the Overwrite, one request, can.
Holds == Mix = "rule" /\ On("OverwriteHeld")

Missing(k, s) == k \ s
\* What a version of schema `after`, following one of schema `before`, breaks of contract k that `before` kept.
Introduced(k, before, after) == Missing(k, after) \ Missing(k, before)

\* --------------------------------------------------------------------------
\* The contract seam.

\* A version: its schema, its maker, and the contract in force when it landed ({} when none).
Version(s, by, k) == [s |-> s, by |-> by, k |-> k]
None == <<{}, "init">>

VARIABLES hist,     \* each ref's versions, <<>> for a ref that does not exist
          rec,      \* the contract record: the columns it names, {} for none
          recBy,    \* the door whose write the record holds, "init" for none
          recLive,  \* whether the record is in force (a set marked it, or a delete or a rollback wrote it)
          recPrev,  \* an in-flight record's in-force record it replaced: <<columns, door>>
          truth,    \* (ghost) the record last put in force by an answer: <<columns, door>>
          answer,   \* each door's answer: "", "ok", "refused"
          lost,     \* (ghost) the in-force records a rollback did not put back
          wpc, wplan, wsch, wk, wtries,  \* each writer's step, the version it planned on, that version's schema, the
                                         \* contract it read
          spc, sreplaced, slist, splan, stries  \* each set's step, the record its refusal puts back, the refs it still
                                                \* verifies, the version its barrier planned on

VARIABLES nslive,   \* whether namespace x stands
          rule,     \* whether x requires a run
          gen,      \* x's incarnation: its uuid, minted at each create
          adds,     \* the placers holding x's ADD fence
          odrop,    \* whether the actor remaking x holds x's DROP fence
          placed,   \* the tables standing in x that hold data no run produced
          ppc, pjudged, pgen,  \* each placer's step, whether it read x's rule as requiring a run, the x it was dropped from
          opc       \* the step of the actor remaking x

cvars == <<hist, rec, recBy, recLive, recPrev, truth, answer, lost, wpc, wplan, wsch, wk, wtries, spc, sreplaced, slist, splan, stries>>
recvars == <<rec, recBy, recLive, recPrev, truth, lost>>
wvars == <<wpc, wplan, wsch, wk, wtries>>
svars == <<spc, sreplaced, slist, splan, stries>>
rvars == <<nslive, rule, gen, adds, odrop, placed, ppc, pjudged, pgen, opc>>
vars == <<cvars, rvars>>

Top(r) == hist[r][Len(hist[r])]
Exists(r) == hist[r] # <<>>
\* The contract in force: the record once marked; while a set is in flight, the in-force record it replaced.
InForce == IF recLive THEN rec ELSE recPrev[1]
\* What a commit is judged against: the record, and while it is in flight the in-force one too (the stricter of the two).
Binding == IF On("JudgedStrict") THEN rec \cup InForce ELSE rec
\* What a set's record replaces: the record in force (over an in-flight record, the one it carries).
Replaced == IF recLive \/ ~On("RestoreInForce") THEN <<rec, recBy>> ELSE recPrev

Init ==
  /\ hist = [r \in Refs |-> IF r = "main" THEN <<Version({"a", "c"}, "init", {})>> ELSE <<>>]
  /\ rec = {} /\ recBy = "init" /\ recLive = TRUE /\ recPrev = None /\ truth = None
  /\ answer = [d \in Doors |-> ""] /\ lost = {}
  /\ wpc = [w \in Writers |-> "plan"] /\ wplan = [w \in Writers |-> 0] /\ wsch = [w \in Writers |-> {}]
  /\ wk = [w \in Writers |-> {}] /\ wtries = [w \in Writers |-> 0]
  /\ spc = [s \in Sets |-> "first"] /\ sreplaced = [s \in Sets |-> None] /\ slist = [s \in Sets |-> {}]
  /\ splan = [s \in Sets |-> 0] /\ stries = [s \in Sets |-> 0]
  /\ nslive = TRUE /\ rule = FALSE /\ gen = 1 /\ adds = {} /\ odrop = FALSE /\ placed = {}
  /\ ppc = [p \in Placers |-> IF Door[p] = "undrop" THEN "dropped" ELSE "judge"]
  /\ pjudged = [p \in Placers |-> FALSE] /\ pgen = [p \in Placers |-> 1]
  /\ opc = IF Placers = {} THEN "done" ELSE "take"

Change(w, s) ==
  CASE Op[w] = "drop" -> s \ {"a"}
    [] Op[w] = "add" -> s \cup {"b"}
    [] Op[w] = "readd" -> s \cup {"a"}
    [] Op[w] = "restore" -> hist["main"][1].s

\* The writer reads the ref's head and plans its change on it.
WPlan(w) ==
  /\ wpc[w] = "plan" /\ Exists(Ref[w])
  /\ wplan' = [wplan EXCEPT ![w] = Len(hist[Ref[w]])]
  /\ wsch' = [wsch EXCEPT ![w] = Top(Ref[w]).s]
  /\ wpc' = [wpc EXCEPT ![w] = "judge"]
  /\ UNCHANGED <<hist, recvars, answer, wk, wtries, svars, rvars>>

\* commit_stamped reads the contract after its plan (table.binding()), and version/create after its stage.
WJudge(w) ==
  /\ wpc[w] = "judge"
  /\ wk' = [wk EXCEPT ![w] = Binding]
  /\ wpc' = [wpc EXCEPT ![w] =
       IF On("Judged") /\ Introduced(Binding, wsch[w], Change(w, wsch[w])) # {} THEN "refused" ELSE "land"]
  /\ UNCHANGED <<hist, recvars, answer, wplan, wsch, wtries, svars, rvars>>

Exact(w) == Way[w] = "exact" \/ On("OverwriteStamped")

WLand(w) ==
  /\ wpc[w] = "land"
  /\ LET r == Ref[w] IN
       IF ~Exact(w) \/ Len(hist[r]) = wplan[w]
       THEN \* Lance's own Overwrite lands over whatever committed since; an exact commit fills the slot after its plan.
            /\ hist' = [hist EXCEPT ![r] = Append(@, Version(Change(w, wsch[w]), w, InForce))]
            /\ wpc' = [wpc EXCEPT ![w] = "done"]
            /\ UNCHANGED wtries
       ELSE \* A lost race: plan again on the new head, or answer 409 once the attempts are spent.
            /\ wpc' = [wpc EXCEPT ![w] = IF wtries[w] < MaxTries THEN "plan" ELSE "conflict"]
            /\ wtries' = [wtries EXCEPT ![w] = @ + 1]
            /\ UNCHANGED hist
  /\ UNCHANGED <<recvars, answer, wplan, wsch, wk, svars, rvars>>

MeetsAll(k, refs) == \A r \in refs : Exists(r) => Missing(k, Top(r).s) = {}

\* The door's first read of every ref (_require_described): a contract the table does not meet now is refused 409.
\* Without ReplacedInKeep the door then reads the record it would put back, before it writes its own.
SFirst(s) ==
  /\ spc[s] = "first"
  /\ IF MeetsAll(K[s], Refs)
     THEN spc' = [spc EXCEPT ![s] = IF On("ReplacedInKeep") THEN "keep" ELSE "read"] /\ UNCHANGED answer
     ELSE spc' = [spc EXCEPT ![s] = "done"] /\ answer' = [answer EXCEPT ![s] = "refused"]
  /\ UNCHANGED <<hist, recvars, wvars, sreplaced, slist, splan, stries, rvars>>

\* The code before this ADR's review: the record read on its own, before contracts.keep (endpoints/contracts._standing).
SRead(s) ==
  /\ spc[s] = "read"
  /\ sreplaced' = [sreplaced EXCEPT ![s] = Replaced]
  /\ spc' = [spc EXCEPT ![s] = "keep"]
  /\ UNCHANGED <<hist, recvars, answer, wvars, slist, splan, stries, rvars>>

\* contracts.keep: the record written in flight, carrying what it replaced, which its conditional write read; the door
\* lists the refs it then verifies.
SKeep(s) ==
  /\ spc[s] = "keep"
  /\ sreplaced' = [sreplaced EXCEPT ![s] = IF On("ReplacedInKeep") THEN Replaced ELSE @]
  /\ recPrev' = Replaced
  /\ rec' = K[s] /\ recBy' = s /\ recLive' = FALSE
  /\ slist' = [slist EXCEPT ![s] = {r \in Refs : Exists(r)}]
  /\ spc' = [spc EXCEPT ![s] = IF On("Barrier") THEN "bplan" ELSE "second"]
  /\ UNCHANGED <<hist, truth, lost, answer, wvars, splan, stries, rvars>>

\* contracts.take_back: the record the set replaced put back, in force, over its own record only.
Rollback(s) ==
  /\ IF ~On("RollbackOwn") \/ recBy = s
     THEN /\ rec' = sreplaced[s][1] /\ recBy' = sreplaced[s][2] /\ recLive' = TRUE /\ recPrev' = None
          /\ lost' = IF sreplaced[s] # truth THEN lost \cup {truth[2]} ELSE lost
     ELSE UNCHANGED <<rec, recBy, recLive, recPrev, lost>>
  /\ answer' = [answer EXCEPT ![s] = "refused"]
  /\ spc' = [spc EXCEPT ![s] = "done"]
  /\ UNCHANGED truth

\* contracts.confirm: the set's own record marked in force, and the set answers 200; a record another door wrote since
\* refuses the set 409, and stays.
SMark(s) ==
  /\ spc[s] = "mark"
  /\ spc' = [spc EXCEPT ![s] = "done"]
  /\ IF ~On("MarkOwn") \/ recBy = s
     THEN /\ answer' = [answer EXCEPT ![s] = "ok"]
          /\ truth' = <<K[s], s>>
          /\ IF recBy = s THEN recLive' = TRUE /\ recPrev' = None ELSE UNCHANGED <<recLive, recPrev>>
     ELSE answer' = [answer EXCEPT ![s] = "refused"] /\ UNCHANGED <<truth, recLive, recPrev>>
  /\ UNCHANGED <<hist, rec, recBy, lost, wvars, sreplaced, slist, splan, stries, rvars>>

\* The second read (Barrier off): every ref meets the contract now, or the record is taken back.
SSecond(s) ==
  /\ spc[s] = "second"
  /\ IF MeetsAll(K[s], slist[s])
     THEN spc' = [spc EXCEPT ![s] = "mark"] /\ UNCHANGED <<recvars, answer>>
     ELSE Rollback(s)
  /\ UNCHANGED <<hist, wvars, sreplaced, slist, splan, stries, rvars>>

\* The barrier on one ref: plan on its head and judge it against the contract.
SBPlan(s) ==
  /\ spc[s] = "bplan"
  /\ IF slist[s] = {}
     THEN spc' = [spc EXCEPT ![s] = "mark"] /\ UNCHANGED <<recvars, answer, splan>>
     ELSE \E r \in slist[s] :
            IF Missing(K[s], Top(r).s) # {}
            THEN Rollback(s) /\ UNCHANGED splan
            ELSE /\ splan' = [splan EXCEPT ![s] = Len(hist[r])]
                 /\ spc' = [spc EXCEPT ![s] = "bland"]
                 /\ UNCHANGED <<recvars, answer>>
  /\ UNCHANGED <<hist, wvars, sreplaced, slist, stries, rvars>>

\* The barrier lands the slot after its plan, a version that changes nothing; a lost race plans again.
SBLand(s) ==
  /\ spc[s] = "bland"
  /\ \E r \in slist[s] :
       /\ Len(hist[r]) >= splan[s] /\ splan[s] > 0
       /\ IF Len(hist[r]) = splan[s]
          THEN /\ hist' = [hist EXCEPT ![r] = Append(@, Version(Top(r).s, s, InForce))]
               /\ slist' = [slist EXCEPT ![s] = @ \ {r}]
               /\ spc' = [spc EXCEPT ![s] = "bplan"]
               /\ UNCHANGED <<stries, recvars, answer>>
          ELSE IF stries[s] < MaxTries
               THEN /\ stries' = [stries EXCEPT ![s] = @ + 1] /\ spc' = [spc EXCEPT ![s] = "bplan"]
                    /\ UNCHANGED <<hist, slist, recvars, answer>>
               ELSE Rollback(s) /\ UNCHANGED <<hist, slist, stries>>
  /\ UNCHANGED <<wvars, sreplaced, splan, rvars>>

\* contracts.forget: idempotent, unconditional; its answer puts none in force.
Delete(d) ==
  /\ answer[d] = ""
  /\ rec' = {} /\ recBy' = d /\ recLive' = TRUE /\ recPrev' = None /\ truth' = <<{}, d>>
  /\ answer' = [answer EXCEPT ![d] = "ok"]
  /\ UNCHANGED <<hist, lost, wvars, svars, rvars>>

Branch ==
  /\ Branching /\ ~Exists("br")
  /\ hist' = [hist EXCEPT !["br"] = <<Version(Top("main").s, "branch", {})>>]
  /\ UNCHANGED <<recvars, answer, wvars, svars, rvars>>

\* --------------------------------------------------------------------------
\* The run rule seam: namespace x, which declares nothing at first.

\* A placer under PlacedUnderHold takes x's ADD fence first, refused (409) while the actor remaking x holds its DROP
\* fence, and reads the rule inside it: x missing is refused (PlacedStanding), else the rule it reads is the one it
\* lands under. Without the hold every add holds the fence only for the backend call (fenced_namespace), PAdd.
PJudge(p) ==
  /\ ppc[p] = "judge"
  /\ LET held == On("PlacedUnderHold") /\ Door[p] # "undrop" IN
       IF held /\ odrop
       THEN ppc' = [ppc EXCEPT ![p] = "refused"] /\ UNCHANGED <<adds, pjudged>>
       ELSE /\ adds' = IF held THEN adds \cup {p} ELSE adds
            /\ pjudged' = [pjudged EXCEPT ![p] = nslive /\ rule]
            /\ ppc' = [ppc EXCEPT ![p] =
                 IF held /\ ~nslive /\ On("PlacedStanding") THEN "refused"
                 ELSE IF nslive /\ rule /\ (Door[p] = "create" \/ (Door[p] = "register" /\ On("RegisterJudged"))
                                             \/ (Door[p] = "rename" /\ On("RenameJudged")))
                 THEN "refused" ELSE "add"]
  /\ UNCHANGED <<nslive, rule, gen, odrop, placed, pgen, opc, cvars>>

\* The add: the backend's register under x's ADD fence, refused while x is dropped or its drop holds the fence.
PAdd(p) ==
  /\ ppc[p] = "add"
  /\ \/ /\ nslive /\ (p \in adds \/ ~odrop)
        /\ IF Door[p] = "undrop" /\ On("UndropChecksParent") /\ pgen[p] # gen
           THEN ppc' = [ppc EXCEPT ![p] = "refused"] /\ UNCHANGED placed
           ELSE ppc' = [ppc EXCEPT ![p] = "done"] /\ placed' = placed \cup {p}
     \/ /\ ~nslive \/ (p \notin adds /\ odrop)
        /\ ppc' = [ppc EXCEPT ![p] = "refused"] /\ UNCHANGED placed
  /\ adds' = adds \ {p}
  /\ UNCHANGED <<nslive, rule, gen, odrop, pjudged, pgen, opc, cvars>>

\* An undrop's table was soft-dropped from x before the behaviour: the trash records x's uuid. Its undrop is judged by
\* the write judge with no rule refusal (it is no data door, and moves nothing in from outside).
PUndrop(p) ==
  /\ ppc[p] = "dropped"
  /\ ppc' = [ppc EXCEPT ![p] = "judge"]
  /\ UNCHANGED <<nslive, rule, gen, adds, odrop, placed, pjudged, pgen, opc, cvars>>

\* The Overwrite takes x's DROP fence for its whole run (OverwriteHeld), refused 409 while an add holds it; the drop
\* door of mix "redeclare", and the Overwrite without the guard, hold it only for the drop itself (ODrop).
OTake ==
  /\ opc = "take"
  /\ IF Holds
     THEN IF adds = {} THEN odrop' = TRUE /\ opc' = "drop" ELSE opc' = "done" /\ UNCHANGED odrop
     ELSE opc' = "drop" /\ UNCHANGED odrop
  /\ UNCHANGED <<nslive, rule, gen, adds, placed, ppc, pjudged, pgen, cvars>>

\* The Restrict drop under x's DROP fence: refused (409) while an add holds the fence or a table stands in x.
ODrop ==
  /\ opc = "drop"
  /\ IF adds = {} /\ placed = {}
     THEN nslive' = FALSE /\ opc' = "forget" /\ UNCHANGED odrop
     ELSE opc' = "done" /\ odrop' = FALSE /\ UNCHANGED nslive
  /\ UNCHANGED <<rule, gen, adds, placed, ppc, pjudged, pgen, cvars>>

\* _forget_namespace: the dropped namespace's grants and records end; x stays missing.
OForget ==
  /\ opc = "forget"
  /\ opc' = "create"
  /\ UNCHANGED <<nslive, rule, gen, adds, odrop, placed, ppc, pjudged, pgen, cvars>>

\* x created afresh, with a new uuid, declaring the rule: an add under x's PARENT's fence, which excludes nothing on x.
\* The Overwrite's own DROP hold on x ends with it.
OCreate ==
  /\ opc = "create"
  /\ nslive' = TRUE /\ rule' = TRUE /\ gen' = gen + 1 /\ odrop' = FALSE /\ opc' = "done"
  /\ UNCHANGED <<adds, placed, ppc, pjudged, pgen, cvars>>

Done ==
  /\ \A w \in Writers : wpc[w] \in {"done", "refused", "conflict"}
  /\ \A s \in Sets : spc[s] = "done"
  /\ \A d \in Deletes : answer[d] # ""
  /\ \A p \in Placers : ppc[p] \in {"done", "refused"}
  /\ opc = "done"
  /\ UNCHANGED vars

Next ==
  \/ \E w \in Writers : WPlan(w) \/ WJudge(w) \/ WLand(w)
  \/ \E s \in Sets : SFirst(s) \/ SRead(s) \/ SKeep(s) \/ SSecond(s) \/ SBPlan(s) \/ SBLand(s) \/ SMark(s)
  \/ \E d \in Deletes : Delete(d)
  \/ Branch
  \/ \E p \in Placers : PJudge(p) \/ PAdd(p) \/ PUndrop(p)
  \/ OTake \/ ODrop \/ OForget \/ OCreate
  \/ Done

Spec == Init /\ [][Next]_vars

\* --------------------------------------------------------------------------
\* Invariants.

TypeOK ==
  /\ rec \subseteq Cols /\ recPrev[1] \subseteq Cols /\ truth[1] \subseteq Cols
  /\ \A d \in Doors : answer[d] \in {"", "ok", "refused"}
  /\ \A r \in Refs : \A i \in 1..Len(hist[r]) : hist[r][i].s \subseteq Cols
  /\ adds \subseteq Placers /\ placed \subseteq Placers

\* No version a writer landed introduces a violation of the contract in force when it landed.
ContractKept ==
  \A r \in Refs : \A i \in 2..Len(hist[r]) :
    hist[r][i].by \in Writers => Introduced(hist[r][i].k, hist[r][i - 1].s, hist[r][i].s) = {}

\* A refused set puts back the contract in force when it wrote, never another.
RollbackRestoresInForce == lost = {}

\* No table stands in x while x requires a run, holding data no run produced.
RunRuleHeld == nslive /\ rule => placed = {}

\* While the Overwrite is between its drop and its re-create, no placer holds x's ADD fence.
OverwriteUnseen == Mix = "rule" /\ opc \in {"forget", "create"} => adds = {}
=============================================================================
