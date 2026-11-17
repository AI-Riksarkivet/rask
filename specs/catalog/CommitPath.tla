---------------------------- MODULE CommitPath ----------------------------
(***************************************************************************)
(* The catalog's commit path: which writes land on one table's history,    *)
(* in which order, and what each writer is answered.                       *)
(*                                                                         *)
(* A version of a table is a put-if-not-exists of the slot after the one   *)
(* its writer planned on. The writers, and the functions each action       *)
(* cites:                                                                  *)
(*   keyed    /insert with an Idempotency-Key, sent again by its caller    *)
(*            (endpoints/data.py insert_into_table, api/idempotency.py     *)
(*            begin and Converge, lakehouse/idempotency.py claim, renew,   *)
(*            release, record_outcome; dataplane.committed_under_key).     *)
(*            Each attempt is one actor; an attempt starts once the one    *)
(*            before it was answered, was told 503 at the request deadline *)
(*            (lakehouse/deadline.py: the door runs on), or died. Every    *)
(*            keyed append is a stamped commit (dataplane.commit_stamped:  *)
(*            plan on the latest version, judge, finalize exactly the next *)
(*            slot, plan again on a lost race), on a table declaring a     *)
(*            primary key (mixes "retry", "rebody") or none ("lane").      *)
(*   insert   an unkeyed /insert of one key (commit_stamped,               *)
(*            primary_keys.plan_new_keys)                                  *)
(*   nested   an /insert to a table whose key is a field inside a struct,  *)
(*            which Lance's merge cannot join on (primary_keys.joinable):  *)
(*            a stamped commit of Lance's own insert (dataplane.captured), *)
(*            or, with AppendJudged off, Lance's own commit, which rebases *)
(*            an append onto whatever landed since ("lance" path)          *)
(*   delete   /delete of one key (dataplane.delete_from_table)             *)
(*   stock    a stock Lance client's commit through version/create         *)
(*            (endpoints/versions.py create_table_version,                 *)
(*            managed_versions.stage, judge, finalize)                     *)
(*   maint    maintenance's compaction, a stock commit that changes no row *)
(*            (managed_versions._refuse_a_rewrite_that_changes_rows)       *)
(* Every writer that lost its race takes the ref's commit turn, and every  *)
(* finalize waits while another writer holds it (lakehouse/commit_turns.py *)
(* TableTurns.take, wait, release; table_claims.DoorTable.finalize_version *)
(* and wait_for_turn). A turn lapses at its hold, held or not.             *)
(*                                                                         *)
(* A writer dies at any step; a keyed attempt's caller may be told 503 at  *)
(* any step while the attempt runs on, and sends the write again under the *)
(* same key, with the same body or another. The key's lease lapses at any  *)
(* time but one (A2).                                                      *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. A version slot is filled put-if-not-exists, so of two writers     *)
(*       planned on one version one lands there (the dir backend's         *)
(*       create_table_version; Lance's own commit on the table's V2        *)
(*       manifest names, dataplane._write_blob's enable_v2_manifest_paths, *)
(*       managed_versions.stage's naming scheme). CommitPathNoCAS.cfg.     *)
(*   A2. A keyed attempt's finalize ends within the key's LEASE (300 s) of *)
(*       its renewal: the renewal is the write right before the put       *)
(*       (DoorTable.finalize_version's before_write, after its turn       *)
(*       wait), so one write is left to it. CommitPathLeaseInCommit.cfg.   *)
(*   A3. A turn lapses at its hold whether its holder lives or not         *)
(*       (commit_turns._kept, Turn.expires_at). CommitPathNoTurnLapse.cfg. *)
(*                                                                         *)
(* GUARDS (each a step of the design; Off names the ones a config removes, *)
(* and each such config must fail):                                        *)
(*   KeyBindsBody     a key binds the request it was sent with: its body   *)
(*                    and query (api/idempotency.py fingerprint), so the   *)
(*                    same key on another body is refused 400.             *)
(*   KeyRenews        a keyed attempt renews its claim, conditional on it  *)
(*                    being its own, right before its commit; a claim      *)
(*                    another attempt took answers 409 with nothing        *)
(*                    committed (lakehouse/idempotency.renew).             *)
(*   RefusalHoldsKey  a keyed attempt refused for its rows answers 400     *)
(*                    only while the key is still its own; a key another   *)
(*                    attempt took answers 409, its write maybe landed     *)
(*                    (idempotency.release, api/idempotency.keyed_door).   *)
(*   TakeoverCounts   a takeover answers the rows the found commit added,  *)
(*                    whatever operation it is (committed_under_key).      *)
(*   AppendJudged     an append to a table declaring a key is committed    *)
(*                    by commit_stamped and judged against the version it  *)
(*                    lands on, a nested key included.                     *)
(*                                                                         *)
(* NOT MODELLED: branches (each ref is a history of its own, judged the    *)
(* same way), the announcement after a commit (CommitAnnounce.tla), the    *)
(* lifecycle doors and their fences (TableLifecycle.tla: every write door  *)
(* opens the table under the claim its resolution read, and a finalize     *)
(* naming the table asks again right before it, DoorTable.require_held and *)
(* require_named), erasure and the history fence (Erasure.tla, Fence.tla), *)
(* a merge-insert's and an update's own shapes (each is a stamped commit   *)
(* of one key here, as insert is), the create door's key, and a stock      *)
(* client's own retry after a 503 (pylance reads the version back).        *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Mix,        \* which writers: "retry", "rebody", "lane", "keys", "nested"
          MaxFaults,  \* how many writers may die in one behaviour
          MaxTries,   \* how many times a stamped commit plans again after a lost race
          Off         \* the guards and assumptions a config removes

Guards == {"KeyBindsBody", "KeyRenews", "RefusalHoldsKey", "TakeoverCounts", "AppendJudged"}
Assumptions == {"StoreCAS", "LeaseOutlastsCommit", "TurnLapses"}
ASSUME Off \subseteq Guards \cup Assumptions
On(g) == g \notin Off

\* The writers of each mix: their kind, the key value each writes (a keyed attempt's body), and its path.
N == CASE Mix = "retry" -> 4 [] Mix = "rebody" -> 3 [] Mix = "lane" -> 4 [] Mix = "keys" -> 4 [] Mix = "nested" -> 2
Actors == 1..N
Kind(a) ==
  CASE Mix = "retry" -> (IF a <= 3 THEN "keyed" ELSE "insert")
    [] Mix = "rebody" -> "keyed"
    [] Mix = "lane" -> (IF a <= 3 THEN "keyed" ELSE "maint")
    [] Mix = "keys" -> (CASE a = 1 -> "insert" [] a = 2 -> "stock" [] a = 3 -> "delete" [] OTHER -> "maint")
    [] Mix = "nested" -> (IF a = 1 THEN "nested" ELSE "delete")
K(a) ==
  CASE Mix = "retry" -> (IF a <= 3 THEN 1 ELSE 2)
    [] Mix = "rebody" -> (IF a = 2 THEN 2 ELSE 1)
    [] OTHER -> 1
\* Whether the table declares a primary key, so that its rows are a set judged at every commit.
Keyed(a) == Kind(a) = "keyed"
PKTable == Mix # "lane"
\* How a writer's commit lands: "stamped" finalizes exactly the slot after its plan; "lance" rebases an append.
Path(a) == IF Kind(a) = "nested" /\ ~On("AppendJudged") THEN "lance" ELSE "stamped"
\* The table holds key 1 before any writer starts, where a writer's harm needs it held.
Seed == IF Mix = "nested" THEN <<[by |-> 0, op |-> "ins", k |-> 1, mark |-> FALSE, n |-> 1, path |-> "stamped"]>> ELSE <<>>

None == [st |-> "none", att |-> 0, body |-> 0, lapsed |-> FALSE, abody |-> 0, acnt |-> 0, aver |-> 0]

VARIABLES log,      \* the table's history: one record per version, in order
          rec,      \* the key's record on the control root
          pc,       \* each writer's next step
          plan,     \* the version each writer planned on
          renewed,  \* whether a keyed attempt renewed its claim since it last planned
          tries,    \* lost races so far
          told,     \* whether a keyed attempt's caller was told 503 at the request deadline
          ans,      \* what each writer was answered: "", "ok", "refused" (400, its key value held), "reused"
                    \* (400, the Idempotency-Key bound to another request), "conflict" (409)
          abody, acnt, aver,  \* an "ok" answer's body, row count and version
          turn,     \* the writer holding the ref's commit turn, 0 for none
          faults

vars == <<log, rec, pc, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults>>

RECURSIVE RowsUpTo(_)
\* The key values the table holds once its first i versions have landed.
RowsUpTo(i) ==
  IF i = 0 THEN {}
  ELSE LET c == log[i] IN
       CASE c.op = "ins" -> RowsUpTo(i - 1) \cup {c.k}
         [] c.op = "del" -> RowsUpTo(i - 1) \ {c.k}
         [] OTHER -> RowsUpTo(i - 1)

Init ==
  /\ log = Seed
  /\ rec = None
  /\ pc = [a \in Actors |-> "start"]
  /\ plan = [a \in Actors |-> 0]
  /\ renewed = [a \in Actors |-> FALSE]
  /\ tries = [a \in Actors |-> 0]
  /\ told = [a \in Actors |-> FALSE]
  /\ ans = [a \in Actors |-> ""]
  /\ abody = [a \in Actors |-> 0] /\ acnt = [a \in Actors |-> 0] /\ aver = [a \in Actors |-> 0]
  /\ turn = 0
  /\ faults = 0

Ended(a) == pc[a] \in {"done", "dead"}
\* A keyed attempt's caller has its answer, its 503, or a dropped connection, and may send the write again.
Released(a) == Ended(a) \/ told[a]
Free(a) == turn \in {0, a}

\* Answer `what` and end; a turn the writer holds is released (TableTurns.release in held's finally).
Finish(a, what) ==
  /\ ans' = [ans EXCEPT ![a] = what]
  /\ pc' = [pc EXCEPT ![a] = "done"]
  /\ turn' = IF turn = a THEN 0 ELSE turn

\* A refusal the keyed door raises gives its claim back, only its own (Converge.release_if_refused, idempotency.release).
ReleaseIfMine(a) ==
  rec' = IF Keyed(a) /\ rec.st = "in" /\ rec.att = a THEN [rec EXCEPT !.st = "rel"] ELSE rec

---------------------------------------------------------------------------
Start(a) ==
  /\ pc[a] = "start"
  /\ \A b \in Actors : (Keyed(b) /\ b < a) => Released(b)
  /\ pc' = [pc EXCEPT ![a] = IF Keyed(a) THEN "claim" ELSE "plan"]
  /\ UNCHANGED <<log, rec, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults>>

\* idempotency.claim: a binding the record holds for another request is refused; a done key is replayed; a live claim
\* answers 409; a lapsed one is taken over (TakenOver) and its commit looked for; a free or released key is taken.
Claim(a) ==
  /\ pc[a] = "claim"
  /\ IF rec.st = "none"
       THEN /\ rec' = [None EXCEPT !.st = "in", !.att = a, !.body = K(a)]
            /\ pc' = [pc EXCEPT ![a] = "plan"]
            /\ UNCHANGED <<ans, abody, acnt, aver, turn>>
     ELSE IF On("KeyBindsBody") /\ rec.body # K(a)
       THEN /\ Finish(a, "reused") /\ UNCHANGED <<rec, abody, acnt, aver>>
     ELSE IF rec.st = "done"
       THEN /\ Finish(a, "ok")
            /\ abody' = [abody EXCEPT ![a] = rec.abody]
            /\ acnt' = [acnt EXCEPT ![a] = rec.acnt]
            /\ aver' = [aver EXCEPT ![a] = rec.aver]
            /\ UNCHANGED rec
     ELSE IF rec.st = "in" /\ ~rec.lapsed
       THEN /\ Finish(a, "conflict") /\ UNCHANGED <<rec, abody, acnt, aver>>
     ELSE /\ rec' = [rec EXCEPT !.st = "in", !.att = a, !.lapsed = FALSE]
          /\ pc' = [pc EXCEPT ![a] = IF rec.st = "in" THEN "scan" ELSE "plan"]
          /\ UNCHANGED <<ans, abody, acnt, aver, turn>>
  /\ UNCHANGED <<log, plan, renewed, tries, told, faults>>

\* dataplane.committed_under_key: the commit carrying the key, answered as its append was, its answer recorded.
Scan(a) ==
  /\ pc[a] = "scan"
  /\ LET hits == {i \in 1..Len(log) : log[i].mark} IN
     IF hits = {}
       THEN /\ pc' = [pc EXCEPT ![a] = "plan"]
            /\ UNCHANGED <<abody, acnt, aver>>
       ELSE LET i == CHOOSE j \in hits : TRUE IN
            /\ pc' = [pc EXCEPT ![a] = "record"]
            /\ abody' = [abody EXCEPT ![a] = log[i].k]
            /\ acnt' = [acnt EXCEPT ![a] = IF On("TakeoverCounts") \/ log[i].path = "lance" THEN log[i].n ELSE 0]
            /\ aver' = [aver EXCEPT ![a] = i]
  /\ UNCHANGED <<log, rec, plan, renewed, tries, told, ans, turn, faults>>

\* Plan on the latest version and judge the key against it (primary_keys.plan_new_keys, refuse_repeats_in_version,
\* managed_versions.judge); a nested key on Lance's own append is judged nowhere unless AppendJudged.
Plan(a) ==
  /\ pc[a] = "plan"
  /\ LET v == Len(log)
         judged == PKTable /\ Kind(a) \in {"keyed", "insert", "stock", "nested"} /\ Path(a) = "stamped"
     IN IF judged /\ K(a) \in RowsUpTo(v)
          THEN /\ Finish(a, IF Keyed(a) /\ On("RefusalHoldsKey") /\ ~(rec.st = "in" /\ rec.att = a) THEN "conflict" ELSE "refused")
               /\ ReleaseIfMine(a) /\ UNCHANGED <<plan, renewed>>
          ELSE /\ plan' = [plan EXCEPT ![a] = v]
               /\ renewed' = [renewed EXCEPT ![a] = FALSE]
               /\ pc' = [pc EXCEPT ![a] = IF Keyed(a) /\ On("KeyRenews") THEN "renew" ELSE "put"]
               /\ UNCHANGED <<ans, rec, turn>>
  /\ UNCHANGED <<log, tries, told, abody, acnt, aver, faults>>

\* idempotency.renew, once the ref's turn is free (DoorTable.finalize_version waits, then renews, then puts): the claim
\* is still this attempt's, under the ETag read; a claim another attempt took answers 409 and nothing is committed.
Renew(a) ==
  /\ pc[a] = "renew" /\ Free(a)
  /\ IF rec.st = "in" /\ rec.att = a
       THEN /\ rec' = [rec EXCEPT !.lapsed = FALSE]
            /\ renewed' = [renewed EXCEPT ![a] = TRUE]
            /\ pc' = [pc EXCEPT ![a] = "put"]
            /\ UNCHANGED <<ans, turn>>
       ELSE /\ Finish(a, "conflict") /\ UNCHANGED <<rec, renewed>>
  /\ UNCHANGED <<log, plan, tries, told, abody, acnt, aver, faults>>

Commit(a) == [by |-> a, op |-> CASE Kind(a) = "delete" -> "del" [] Kind(a) = "maint" -> "rw" [] OTHER -> "ins",
              k |-> K(a), mark |-> Keyed(a), n |-> IF Kind(a) \in {"delete", "maint"} THEN 0 ELSE 1, path |-> Path(a)]

\* The put-if-not-exists of the slot after the plan (A1). Lost: a stamped commit plans again under the ref's turn, up to
\* MaxTries, then answers 409; Lance's own commit of an append rebases onto the slot now free and puts again.
Put(a) ==
  /\ pc[a] = "put" /\ (Free(a) \/ renewed[a])
  /\ IF Len(log) = plan[a] \/ ~On("StoreCAS")
       THEN /\ log' = Append(log, Commit(a))
            /\ aver' = [aver EXCEPT ![a] = Len(log) + 1]
            /\ abody' = [abody EXCEPT ![a] = K(a)]
            /\ acnt' = [acnt EXCEPT ![a] = Commit(a).n]
            /\ IF Keyed(a)
                 THEN /\ pc' = [pc EXCEPT ![a] = "record"] /\ UNCHANGED <<ans, turn>>
                 ELSE Finish(a, "ok")
            /\ UNCHANGED <<plan, tries, rec>>
     ELSE IF Path(a) = "lance"
       THEN /\ plan' = [plan EXCEPT ![a] = Len(log)]
            /\ UNCHANGED <<log, pc, ans, abody, acnt, aver, turn, tries, rec>>
     ELSE IF tries[a] < MaxTries
       THEN /\ tries' = [tries EXCEPT ![a] = tries[a] + 1]
            /\ turn' = IF turn = 0 THEN a ELSE turn
            /\ pc' = [pc EXCEPT ![a] = "plan"]
            /\ UNCHANGED <<log, plan, ans, abody, acnt, aver, rec>>
     ELSE /\ Finish(a, "conflict") /\ ReleaseIfMine(a) /\ UNCHANGED <<log, plan, tries, abody, acnt, aver>>
  /\ UNCHANGED <<renewed, told, faults>>

\* idempotency.record_outcome, then the answer: written over whatever the record holds now, onto its binding.
Record(a) ==
  /\ pc[a] = "record"
  /\ rec' = [rec EXCEPT !.st = "done", !.lapsed = FALSE, !.abody = abody[a], !.acnt = acnt[a], !.aver = aver[a]]
  /\ Finish(a, "ok")
  /\ UNCHANGED <<log, plan, renewed, tries, told, abody, acnt, aver, faults>>

Step(a) == Start(a) \/ Claim(a) \/ Scan(a) \/ Plan(a) \/ Renew(a) \/ Put(a) \/ Record(a)

---------------------------------------------------------------------------
\* The environment.

Die(a) ==
  /\ pc[a] \notin {"start", "done", "dead"} /\ faults < MaxFaults
  /\ pc' = [pc EXCEPT ![a] = "dead"]
  /\ faults' = faults + 1
  /\ UNCHANGED <<log, rec, plan, renewed, tries, told, ans, abody, acnt, aver, turn>>

\* RequestDeadlineMiddleware answers 503 with no response begun; the door runs on.
Told(a) ==
  /\ Keyed(a) /\ pc[a] \notin {"start", "done", "dead"} /\ ~told[a]
  /\ told' = [told EXCEPT ![a] = TRUE]
  /\ UNCHANGED <<log, rec, pc, plan, renewed, tries, ans, abody, acnt, aver, turn, faults>>

\* The key's lease passes on the store's clock; never between a renewal and the commit after it (A2).
KeyLapse ==
  /\ rec.st = "in" /\ ~rec.lapsed
  /\ ~(On("LeaseOutlastsCommit") /\ rec.att \in Actors /\ pc[rec.att] = "put" /\ renewed[rec.att])
  /\ rec' = [rec EXCEPT !.lapsed = TRUE]
  /\ UNCHANGED <<log, pc, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults>>

\* A turn lapses at its hold, its holder alive or not (A3).
TurnLapse ==
  /\ turn # 0 /\ On("TurnLapses")
  /\ turn' = 0
  /\ UNCHANGED <<log, rec, pc, plan, renewed, tries, told, ans, abody, acnt, aver, faults>>

Next == (\E a \in Actors : Step(a) \/ Die(a) \/ Told(a)) \/ KeyLapse \/ TurnLapse

Spec == Init /\ [][Next]_vars /\ \A a \in Actors : WF_vars(Step(a)) /\ WF_vars(TurnLapse)

---------------------------------------------------------------------------
\* PROPERTIES

Marked == {i \in 1..Len(log) : log[i].mark}

\* A write the key names lands at most once, however often its caller sends it.
KeyAppliedOnce == Cardinality(Marked) <= 1

\* A write answered success is in the history, at the version answered: a keyed attempt's at the commit carrying its
\* key, any other's at its own commit.
AnsweredWriteLanded ==
  \A a \in Actors : ans[a] = "ok" =>
    /\ aver[a] \in 1..Len(log)
    /\ IF Keyed(a) THEN log[aver[a]].mark ELSE log[aver[a]].by = a

\* An answer its caller receives: one the deadline already answered 503 for is dropped (lakehouse/deadline.py).
Heard(a) == ans[a] # "" /\ ~told[a]

\* A write answered failure (400 or 409) is not in the history.
RefusedWriteAbsent == \A a \in Actors : Heard(a) /\ ans[a] \in {"refused", "reused", "conflict"} => \A i \in 1..Len(log) : log[i].by # a

\* A keyed request is refused for a key value its table holds only while the write its Idempotency-Key names is not in
\* the history: a resend is never told that its own landed write is another row holding its key.
KeyedRefusalIsTrue == \A a \in Actors : Keyed(a) /\ Heard(a) /\ ans[a] = "refused" => Marked = {}

\* A keyed attempt answered success is answered for its own body, never for another request's.
SuccessIsForItsBody == \A a \in Actors : Keyed(a) /\ ans[a] = "ok" => abody[a] = K(a)

\* A success answers the rows the write it reports added.
SuccessCountsWhatLanded == \A a \in Actors : ans[a] = "ok" => acnt[a] = log[aver[a]].n

\* Insert-if-absent: a version never adds a key value the version before it holds.
OneRowPerKey == PKTable => \A i \in 1..Len(log) : log[i].op = "ins" => log[i].k \notin RowsUpTo(i - 1)

\* Once failures stop, every writer is answered or gone, and no turn is held for ever.
EveryWriterEnds == <>[](\A a \in Actors : Ended(a))

TypeOK ==
  /\ pc \in [Actors -> {"start", "claim", "scan", "plan", "renew", "put", "record", "done", "dead"}]
  /\ ans \in [Actors -> {"", "ok", "refused", "reused", "conflict"}]
  /\ turn \in Actors \cup {0}
=============================================================================
