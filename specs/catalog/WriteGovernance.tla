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
(* contract. Its actors:                                                   *)
(*   exact     a commit finalized exactly at the slot after the version it *)
(*             planned on, by the store's put-if-not-exists: every door's  *)
(*             stamped commit (dataplane.commit_stamped: plan, read the    *)
(*             contract, judge, finalize, plan again on a lost race) and a *)
(*             stock client's (versions.create_table_version:              *)
(*             managed_versions.stage on the version it follows, judge     *)
(*             with the contract read there, finalize; a lost race is the  *)
(*             client's 409 and its retry stages again, here a replan).    *)
(*             Each writer drops column a, or adds column b, or restores   *)
(*             the table's first version (dataplane.restore).              *)
(*   overwrite /create mode=Overwrite of a table (dataplane.create_table): *)
(*             judged against the head and the contract it read first;     *)
(*             landed by Lance's own commit, which lands an Overwrite over *)
(*             whatever committed since (lance_docs/lance/format/table/    *)
(*             transaction.md § Overwrite Compatibility; measured on       *)
(*             pylance 13.0.0: over an UpdateConfig of the table's config, *)
(*             its table metadata or its schema metadata alike), unless    *)
(*             OverwriteStamped makes it an exact commit.                  *)
(*   set       the contract door: a first read of every ref's head         *)
(*             (_require_described), the record kept (contracts.keep),     *)
(*             then either a SECOND READ of every ref (the code before     *)
(*             ADR 0244, Barrier off) or a BARRIER on every ref listed     *)
(*             after the record stood: an exact commit whose plan judges   *)
(*             the ref's head against the new contract (Barrier). A        *)
(*             refusal after the record stood takes it back                *)
(*             (contracts.restore).                                        *)
(*   delete    the contract's delete door (contracts.forget).              *)
(*   branch    a branch created from main's head (dataplane.create_branch).*)
(*                                                                         *)
(* THE RUN RULE (ADR 0211). A namespace declares rask.requires_run at its  *)
(* create, and a namespace Overwrite of an EMPTY namespace drops it and    *)
(* creates it afresh with or without the rule (namespaces.py               *)
(* _create_or_replace_empty_namespace); its drop is refused while any      *)
(* table stands in it (the backend's Restrict) and while an add holds its  *)
(* fence (fenced_namespace: every add holds NamespaceWork.ADD, every drop  *)
(* NamespaceWork.DROP, which exclude each other). Its actors, each placing *)
(* a table that holds data no run produced into namespace x:               *)
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
(*   ContractKept     a version a writer lands while a contract stands     *)
(*                    whose set was answered introduces no violation of    *)
(*                    it (a commit landing while a set is still in flight  *)
(*                    is that set's to judge: its barrier, or its second   *)
(*                    read, sees the ref it lands on);                     *)
(*   AnsweredSetStands  a set answered 200 is replaced only by a later     *)
(*                    set or delete, never by another set's rollback;      *)
(*   RunRuleHeld      no table stands in a namespace that requires a run   *)
(*                    holding data no run produced.                        *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. A version slot is filled put-if-not-exists (CommitPath A1).       *)
(*   A2. A record write is a put, a conditional write a replace under the  *)
(*       ETag of the read it judged (lakehouse.records.mutate_json).       *)
(*   A3. A namespace's rule changes only by its create, and its Overwrite  *)
(*       creates it afresh only once it is empty and dropped (ADR 0211     *)
(*       §2: no Lance operation alters a namespace's properties).          *)
(*   A4. The namespace fence: an add and a drop of one namespace never     *)
(*       hold it together (lakehouse.namespace_fences, Fence.tla).         *)
(*                                                                         *)
(* GUARDS (each a step of the design; Off names the ones a config removes, *)
(* and each such config must fail):                                        *)
(*   Judged          every commit reads the contract after its plan and    *)
(*                   refuses a violation it introduces                     *)
(*                   (contracts.require_kept in commit_stamped and         *)
(*                   managed_versions.judge).                              *)
(*   Barrier         a set lands an exact commit on every ref listed after *)
(*                   its record stood, whose plan judges that ref's head   *)
(*                   against the contract (contracts.verify_on_every_ref). *)
(*   OverwriteStamped  a table's Overwrite is a stamped commit, judged     *)
(*                   against the version it lands on                       *)
(*                   (dataplane.create_table, its overwrite branch).       *)
(*   RollbackOwn     a refused set takes back only the record it wrote     *)
(*                   (contracts.restore, under the ETag of a read that     *)
(*                   found its own record).                                *)
(*   PlacedUnderHold a door that places a table into a namespace reads the *)
(*                   namespace's rule while it holds the namespace's ADD   *)
(*                   fence, which it keeps until the table is added        *)
(*                   (write_judge.placed_under_rule).                      *)
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
(* write into a table already standing in a namespace (its namespace can   *)
(* change no rule while it stands there, A3), and the lifecycle doors'     *)
(* own fences (TableLifecycle.tla, Cascade.tla).                           *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Mix,       \* which actors: "gate", "branch", "sets", "rule"
          MaxTries,  \* lost races an exact commit, or a set's barrier, plans again after
          Off        \* the guards a config removes

Guards == {"Judged", "Barrier", "OverwriteStamped", "RollbackOwn", "PlacedUnderHold", "RenameJudged", "RegisterJudged",
           "UndropChecksParent"}
ASSUME Off \subseteq Guards
On(g) == g \notin Off

Cols == {"a", "b"}
\* The one contract a set states: column a, which every table here starts with.
K == {"a"}
Refs == {"main", "br"}

\* The actors of each mix. "gate": a drop and an add, each an exact commit, and an Overwrite dropping the column, beside
\* one set. "branch": a branch made while a set runs, a drop committed on it, and a restore on main. "sets": two sets
\* and a delete beside a drop and a commit that adds the column back. "rule": one door of each kind placing a table.
Writers == CASE Mix = "gate" -> {"w1", "w2", "w3"} [] Mix \in {"branch", "sets"} -> {"w1", "w2"} [] OTHER -> {}
Op == [w \in Writers |->
         CASE Mix = "gate" -> (IF w = "w2" THEN "add" ELSE "drop")
           [] Mix = "branch" -> (IF w = "w1" THEN "drop" ELSE "restore")
           [] OTHER -> (IF w = "w1" THEN "drop" ELSE "readd")]
Way == [w \in Writers |-> IF Mix = "gate" /\ w = "w3" THEN "overwrite" ELSE "exact"]
Ref == [w \in Writers |-> IF Mix = "branch" /\ w = "w1" THEN "br" ELSE "main"]
Sets == CASE Mix = "sets" -> {"s1", "s2"} [] Mix = "rule" -> {} [] OTHER -> {"s1"}
Deletes == IF Mix = "sets" THEN {"d1"} ELSE {}
Branching == Mix = "branch"
Placers == IF Mix = "rule" THEN {"p1", "p2", "p3", "p4"} ELSE {}
Door == [p \in Placers |-> CASE p = "p1" -> "create" [] p = "p2" -> "register" [] p = "p3" -> "rename" [] OTHER -> "undrop"]
Doors == Sets \cup Deletes

Missing(k, s) == k \ s
\* What a version of schema `after`, following one of schema `before`, breaks of contract k that `before` kept.
Introduced(k, before, after) == Missing(k, after) \ Missing(k, before)

\* --------------------------------------------------------------------------
\* The contract seam.

\* A version: its schema, its maker, and the contract that stood in force when it landed ({} when none, or when the
\* contract standing was a set still in flight).
Version(s, by, k) == [s |-> s, by |-> by, k |-> k]

VARIABLES hist,     \* each ref's versions, <<>> for a ref that does not exist
          rec,      \* the contract record: the columns it names, {} for none
          recBy,    \* the door whose write the record holds, "init" for none
          answer,   \* each door's answer: "", "ok", "refused"
          lost,     \* doors whose write another door's rollback replaced
          wpc, wplan, wsch, wk, wtries,  \* each writer's step, the version it planned on, that version's schema, the
                                         \* contract it read
          spc, sreplaced, slist, splan, stries  \* each set's step, the record it replaced, the refs it still verifies,
                                                \* the version its barrier planned on

VARIABLES nslive,   \* whether namespace x stands
          rule,     \* whether x requires a run
          gen,      \* x's incarnation: its uuid, minted at each create
          adds,     \* the placers holding x's ADD fence
          placed,   \* the tables standing in x that hold data no run produced
          ppc, pjudged, pgen,  \* each placer's step, whether it read x's rule as requiring a run, the x it was dropped from
          opc       \* the namespace Overwrite's step

cvars == <<hist, rec, recBy, answer, lost, wpc, wplan, wsch, wk, wtries, spc, sreplaced, slist, splan, stries>>
rvars == <<nslive, rule, gen, adds, placed, ppc, pjudged, pgen, opc>>
vars == <<cvars, rvars>>

Top(r) == hist[r][Len(hist[r])]
Exists(r) == hist[r] # <<>>
\* The contract in force: the record, once the set that wrote it has answered.
InForce == IF recBy \in Sets /\ answer[recBy] # "ok" THEN {} ELSE rec

Init ==
  /\ hist = [r \in Refs |-> IF r = "main" THEN <<Version({"a"}, "init", {})>> ELSE <<>>]
  /\ rec = {} /\ recBy = "init" /\ answer = [d \in Doors |-> ""] /\ lost = {}
  /\ wpc = [w \in Writers |-> "plan"] /\ wplan = [w \in Writers |-> 0] /\ wsch = [w \in Writers |-> {}]
  /\ wk = [w \in Writers |-> {}] /\ wtries = [w \in Writers |-> 0]
  /\ spc = [s \in Sets |-> "first"] /\ sreplaced = [s \in Sets |-> <<{}, "init">>] /\ slist = [s \in Sets |-> {}]
  /\ splan = [s \in Sets |-> 0] /\ stries = [s \in Sets |-> 0]
  /\ nslive = TRUE /\ rule = FALSE /\ gen = 1 /\ adds = {} /\ placed = {}
  /\ ppc = [p \in Placers |-> IF Door[p] = "undrop" THEN "dropped" ELSE "judge"]
  /\ pjudged = [p \in Placers |-> FALSE] /\ pgen = [p \in Placers |-> 1]
  /\ opc = IF Placers = {} THEN "done" ELSE "drop"

Change(w, s) ==
  CASE Op[w] = "drop" -> s \ {"a"}
    [] Op[w] = "add" -> s \cup {"b"}
    [] Op[w] = "readd" -> s \cup {"a"}
    [] Op[w] = "restore" -> hist["main"][1].s

\* The writer reads the ref's head and plans its change on it. An Overwrite reads the contract here too, and judges.
WPlan(w) ==
  /\ wpc[w] = "plan" /\ Exists(Ref[w])
  /\ wplan' = [wplan EXCEPT ![w] = Len(hist[Ref[w]])]
  /\ wsch' = [wsch EXCEPT ![w] = Top(Ref[w]).s]
  /\ wpc' = [wpc EXCEPT ![w] = "judge"]
  /\ UNCHANGED <<hist, rec, recBy, answer, lost, wk, wtries, spc, sreplaced, slist, splan, stries, rvars>>

\* commit_stamped reads the contract after its plan (table.contract()), and version/create after its stage.
WJudge(w) ==
  /\ wpc[w] = "judge"
  /\ wk' = [wk EXCEPT ![w] = rec]
  /\ wpc' = [wpc EXCEPT ![w] =
       IF On("Judged") /\ Introduced(rec, wsch[w], Change(w, wsch[w])) # {} THEN "refused" ELSE "land"]
  /\ UNCHANGED <<hist, rec, recBy, answer, lost, wplan, wsch, wtries, spc, sreplaced, slist, splan, stries, rvars>>

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
  /\ UNCHANGED <<rec, recBy, answer, lost, wplan, wsch, wk, spc, sreplaced, slist, splan, stries, rvars>>

MeetsAll(k, refs) == \A r \in refs : Exists(r) => Missing(k, Top(r).s) = {}

\* The door's first read of every ref (_require_described): a contract the table does not meet now is refused 409.
SFirst(s) ==
  /\ spc[s] = "first"
  /\ IF MeetsAll(K, Refs)
     THEN spc' = [spc EXCEPT ![s] = "keep"] /\ UNCHANGED answer
     ELSE spc' = [spc EXCEPT ![s] = "done"] /\ answer' = [answer EXCEPT ![s] = "refused"]
  /\ UNCHANGED <<hist, rec, recBy, lost, wpc, wplan, wsch, wk, wtries, sreplaced, slist, splan, stries, rvars>>

\* contracts.keep: the record written; the door keeps what it replaced, and lists the refs it then verifies.
SKeep(s) ==
  /\ spc[s] = "keep"
  /\ sreplaced' = [sreplaced EXCEPT ![s] = <<rec, recBy>>]
  /\ rec' = K /\ recBy' = s
  /\ slist' = [slist EXCEPT ![s] = {r \in Refs : Exists(r)}]
  /\ spc' = [spc EXCEPT ![s] = IF On("Barrier") THEN "bplan" ELSE "second"]
  /\ UNCHANGED <<hist, answer, lost, wpc, wplan, wsch, wk, wtries, splan, stries, rvars>>

Rollback(s) ==
  /\ IF ~On("RollbackOwn") \/ recBy = s
     THEN /\ rec' = sreplaced[s][1] /\ recBy' = sreplaced[s][2]
          /\ lost' = IF recBy # s THEN lost \cup {recBy} ELSE lost
     ELSE UNCHANGED <<rec, recBy, lost>>
  /\ answer' = [answer EXCEPT ![s] = "refused"]
  /\ spc' = [spc EXCEPT ![s] = "done"]

\* The second read (Barrier off): every ref meets the contract now, or the record is taken back.
SSecond(s) ==
  /\ spc[s] = "second"
  /\ IF MeetsAll(K, slist[s])
     THEN answer' = [answer EXCEPT ![s] = "ok"] /\ spc' = [spc EXCEPT ![s] = "done"] /\ UNCHANGED <<rec, recBy, lost>>
     ELSE Rollback(s)
  /\ UNCHANGED <<hist, wpc, wplan, wsch, wk, wtries, sreplaced, slist, splan, stries, rvars>>

\* The barrier on one ref: plan on its head and judge it against the contract.
SBPlan(s) ==
  /\ spc[s] = "bplan"
  /\ IF slist[s] = {}
     THEN answer' = [answer EXCEPT ![s] = "ok"] /\ spc' = [spc EXCEPT ![s] = "done"] /\ UNCHANGED <<rec, recBy, lost, splan>>
     ELSE \E r \in slist[s] :
            IF Missing(K, Top(r).s) # {}
            THEN Rollback(s) /\ UNCHANGED splan
            ELSE /\ splan' = [splan EXCEPT ![s] = Len(hist[r])]
                 /\ spc' = [spc EXCEPT ![s] = "bland"]
                 /\ UNCHANGED <<answer, rec, recBy, lost>>
  /\ UNCHANGED <<hist, wpc, wplan, wsch, wk, wtries, sreplaced, slist, stries, rvars>>

\* The barrier lands the slot after its plan, a version that changes no column; a lost race plans again.
SBLand(s) ==
  /\ spc[s] = "bland"
  /\ \E r \in slist[s] :
       /\ Len(hist[r]) >= splan[s] /\ splan[s] > 0
       /\ IF Len(hist[r]) = splan[s]
          THEN /\ hist' = [hist EXCEPT ![r] = Append(@, Version(Top(r).s, s, InForce))]
               /\ slist' = [slist EXCEPT ![s] = @ \ {r}]
               /\ spc' = [spc EXCEPT ![s] = "bplan"]
               /\ UNCHANGED <<stries, rec, recBy, lost, answer>>
          ELSE IF stries[s] < MaxTries
               THEN /\ stries' = [stries EXCEPT ![s] = @ + 1] /\ spc' = [spc EXCEPT ![s] = "bplan"]
                    /\ UNCHANGED <<hist, slist, rec, recBy, lost, answer>>
               ELSE Rollback(s) /\ UNCHANGED <<hist, slist, stries>>
  /\ UNCHANGED <<wpc, wplan, wsch, wk, wtries, sreplaced, splan, rvars>>

\* contracts.forget: idempotent, unconditional.
Delete(d) ==
  /\ answer[d] = ""
  /\ rec' = {} /\ recBy' = d /\ answer' = [answer EXCEPT ![d] = "ok"]
  /\ UNCHANGED <<hist, lost, wpc, wplan, wsch, wk, wtries, spc, sreplaced, slist, splan, stries, rvars>>

Branch ==
  /\ Branching /\ ~Exists("br")
  /\ hist' = [hist EXCEPT !["br"] = <<Version(Top("main").s, "branch", {})>>]
  /\ UNCHANGED <<rec, recBy, answer, lost, wpc, wplan, wsch, wk, wtries, spc, sreplaced, slist, splan, stries, rvars>>

\* --------------------------------------------------------------------------
\* The run rule seam: namespace x, which declares nothing at first.

\* A placer that holds x's ADD fence across its judgement takes it first (PlacedUnderHold); every add holds it for the
\* backend call (fenced_namespace), which this step and PAdd stand for.
PJudge(p) ==
  /\ ppc[p] = "judge"
  /\ IF On("PlacedUnderHold") /\ Door[p] # "undrop" THEN nslive /\ adds' = adds \cup {p} ELSE UNCHANGED adds
  /\ pjudged' = [pjudged EXCEPT ![p] = nslive /\ rule]
  /\ ppc' = [ppc EXCEPT ![p] =
       IF nslive /\ rule /\ (Door[p] = "create" \/ (Door[p] = "register" /\ On("RegisterJudged"))
                             \/ (Door[p] = "rename" /\ On("RenameJudged")))
       THEN "refused" ELSE "add"]
  /\ UNCHANGED <<nslive, rule, gen, placed, pgen, opc, cvars>>

\* The add: the backend's register under x's ADD fence, refused while x is being dropped or gone.
PAdd(p) ==
  /\ ppc[p] = "add"
  /\ \/ /\ nslive
        /\ IF Door[p] = "undrop" /\ On("UndropChecksParent") /\ pgen[p] # gen
           THEN ppc' = [ppc EXCEPT ![p] = "refused"] /\ UNCHANGED placed
           ELSE ppc' = [ppc EXCEPT ![p] = "done"] /\ placed' = placed \cup {p}
     \/ /\ ~nslive
        /\ ppc' = [ppc EXCEPT ![p] = "refused"] /\ UNCHANGED placed
  /\ adds' = adds \ {p}
  /\ UNCHANGED <<nslive, rule, gen, pjudged, pgen, opc, cvars>>

\* An undrop's table was soft-dropped from x before the behaviour: the trash records x's uuid. Its undrop is judged by
\* the write judge with no rule refusal (it is no data door, and moves nothing in from outside).
PUndrop(p) ==
  /\ ppc[p] = "dropped"
  /\ ppc' = [ppc EXCEPT ![p] = "judge"]
  /\ UNCHANGED <<nslive, rule, gen, adds, placed, pjudged, pgen, opc, cvars>>

\* The namespace Overwrite that declares the rule: drop x once it is empty and no add holds its fence, then create it
\* afresh, with a new uuid, declaring the rule.
ODrop ==
  /\ opc = "drop" /\ placed = {} /\ adds = {}
  /\ nslive' = FALSE /\ opc' = "create"
  /\ UNCHANGED <<rule, gen, adds, placed, ppc, pjudged, pgen, cvars>>

OCreate ==
  /\ opc = "create"
  /\ nslive' = TRUE /\ rule' = TRUE /\ gen' = gen + 1 /\ opc' = "done"
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
  \/ \E s \in Sets : SFirst(s) \/ SKeep(s) \/ SSecond(s) \/ SBPlan(s) \/ SBLand(s)
  \/ \E d \in Deletes : Delete(d)
  \/ Branch
  \/ \E p \in Placers : PJudge(p) \/ PAdd(p) \/ PUndrop(p)
  \/ ODrop \/ OCreate
  \/ Done

Spec == Init /\ [][Next]_vars

\* --------------------------------------------------------------------------
\* Invariants.

TypeOK ==
  /\ rec \subseteq Cols
  /\ \A d \in Doors : answer[d] \in {"", "ok", "refused"}
  /\ \A r \in Refs : \A i \in 1..Len(hist[r]) : hist[r][i].s \subseteq Cols
  /\ adds \subseteq Placers /\ placed \subseteq Placers

\* No version a writer landed introduces a violation of the contract in force when it landed.
ContractKept ==
  \A r \in Refs : \A i \in 2..Len(hist[r]) :
    hist[r][i].by \in Writers => Introduced(hist[r][i].k, hist[r][i - 1].s, hist[r][i].s) = {}

\* A set answered 200 is never replaced by another set's rollback.
AnsweredSetStands == \A s \in lost : answer[s] # "ok"

\* No table stands in x while x requires a run, holding data no run produced.
RunRuleHeld == nslive /\ rule => placed = {}

\* Every door ends (checked where a config asks).
EveryDoorEnds == <>[](\A s \in Sets : spc[s] = "done")
=============================================================================
