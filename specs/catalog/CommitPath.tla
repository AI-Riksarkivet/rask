---------------------------- MODULE CommitPath ----------------------------
(***************************************************************************)
(* The catalog's commit path: which writes land on one table's history,    *)
(* in which order, and what each writer is answered.                       *)
(*                                                                         *)
(* A version of a table is a put-if-not-exists of the slot after the one   *)
(* its writer planned on. The writers, and the functions each action       *)
(* cites:                                                                  *)
(*   keyed    a door's write sent with an Idempotency-Key and sent again   *)
(*            by its caller (api/idempotency.py begin, keyed_door,         *)
(*            Converge; lakehouse/idempotency.py claim, renew, release,    *)
(*            record_outcome, HeldKey; dataplane.landed_under_key).        *)
(*            One actor stands for every keyed door that commits a        *)
(*            version, each mapped to it in ADR 0240 §8:                   *)
(*              /insert on a table declaring a primary key, a stamped      *)
(*              commit judged against the version it lands on (mixes      *)
(*              "retry", "rebody");                                        *)
(*              /insert on a table declaring none, Lance's own commit of   *)
(*              fragments written first, which rebases onto whatever      *)
(*              landed since and renews before each put                    *)
(*              (dataplane.land_keyed: mix "lane");                        *)
(*              /create mode=Overwrite of a table, the same Lance commit    *)
(*              of an Overwrite (dataplane._write_blob: mix "overwrite");  *)
(*              /restore, a stamped commit of Lance's Restore              *)
(*              (dataplane.restore: mix "restore").                        *)
(*            Each attempt is one actor; an attempt starts once the one    *)
(*            before it was answered, was told 503 at the request deadline *)
(*            (lakehouse/deadline.py: the door runs on), or died. A        *)
(*            stamped commit (dataplane.commit_stamped) plans on the       *)
(*            latest version, judges, finalizes exactly the next slot and  *)
(*            plans again on a lost race.                                  *)
(*   insert   an unkeyed /insert of one key: a stamped commit judged on a  *)
(*            table declaring a key (primary_keys.plan_new_keys), Lance's  *)
(*            own insert on one declaring none, which rebases an append    *)
(*            and is refused across an Overwrite or a Restore              *)
(*            (lance_docs/lance/format/table/transaction.md § Append).     *)
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
(*                                                                         *)
(* THE SEAL (ADR 0242): every file a version names is sealed and recorded  *)
(* (committed_files.seal_and_record). A stamped commit seals all of it     *)
(* before its finalize; a stock commit seals its data files at the         *)
(* judgement and the writer's transaction file once landed (versions.py);  *)
(* Lance's own commit (the "lance" path) writes its fragments first and    *)
(* seals them before the commit, and the transaction file Lance writes     *)
(* inside its commit once landed (dataplane._land_rows, _write_blob). A    *)
(* version whose writer died after it landed, its transaction file         *)
(* unsealed, is found and finished by the commit reconciler                *)
(* (commit_reconcile.finish_landed), which reads each ref's versions after *)
(* its mark, oldest first; a finish that fails holds the mark before its   *)
(* version, so the next pass tries it again (commit_reconcile.scan_ref).   *)
(* A version whose predecessor a cleanup reclaimed is finished from what   *)
(* its own transaction names (committed_files.added_by), so only a failure *)
(* that can heal holds the mark.                                           *)
(*                                                                         *)
(* A version other than the head may be REMOVED while a key lives: a       *)
(* cleanup (maintenance's pass, retain_versions with no age bound) or the  *)
(* owner tier's version/delete. A search whose range holds one it cannot   *)
(* read cannot prove the key's write did not land there.                   *)
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
(*   A4. The commit reconciler runs on its period and finishes every       *)
(*       landed version it reads (the catalog-commit-reconcile timer).     *)
(*       CommitPathNoReconciler.cfg.                                       *)
(*                                                                         *)
(* GUARDS (each a step of the design; Off names the ones a config removes, *)
(* and each such config must fail):                                        *)
(*   KeyBindsBody     a key binds the request it was sent with: its body   *)
(*                    and query (api/idempotency.py fingerprint), so the   *)
(*                    same key on another body is refused 400.             *)
(*   KeyRenews        a keyed attempt renews its claim right before each   *)
(*                    put that would land its version, after the turn      *)
(*                    wait; a claim another attempt took answers 409 with  *)
(*                    nothing committed (lakehouse/idempotency.renew,      *)
(*                    HeldKey.renew, every seam a door's version lands     *)
(*                    through: DoorTable.finalize_version, land_keyed).    *)
(*   RenewIsOwn       the renewal is a replace under the ETag of a read    *)
(*                    that found the claim this attempt's own              *)
(*                    (idempotency.renew's attempt check).                 *)
(*   RecordOwnClaim   an answer is recorded only over the attempt's own    *)
(*                    claim, so the answer a key serves is never replaced  *)
(*                    (idempotency.record_outcome).                        *)
(*   RefusalHoldsKey  a keyed attempt refused for its rows answers 400     *)
(*                    only while the key is still its own; a key another   *)
(*                    attempt took answers 409, its write maybe landed     *)
(*                    (idempotency.release, api/idempotency.keyed_door).   *)
(*   TakeoverCounts   a takeover answers the rows the found commit added,  *)
(*                    whatever operation it is (committed_under_key).      *)
(*   AppendJudged     an append to a table declaring a key is committed    *)
(*                    by commit_stamped and judged against the version it  *)
(*                    lands on, a nested key included.                     *)
(*   KeyKeepsFirst    a key's record keeps, across every takeover and      *)
(*                    release, the version the last put under it was      *)
(*                    planned on (_Claimed.written_from, set by the        *)
(*                    renewal before each put), and a takeover searches    *)
(*                    every version after it; without it a takeover        *)
(*                    searched from the lapsed claim's own claim, which a  *)
(*                    chain of takeovers moves past the first attempt's    *)
(*                    commit. The latest plan, not the lowest: an attempt  *)
(*                    renews only once its search found none of the key's  *)
(*                    writes and its own last put did not land, so no      *)
(*                    write under the key is at or before what it plans    *)
(*                    on (checked: with the latest plan every gate holds   *)
(*                    and every config that must fail fails, as with the   *)
(*                    lowest, ADR 0240 §12).                               *)
(*   ReleaseKeepsWritten  a released key whose record says an attempt may  *)
(*                    have written is searched by the attempt claiming it  *)
(*                    (idempotency.claim answers TakenOver); without it a  *)
(*                    takeover refused before its search released the key *)
(*                    as though nothing had been written.                  *)
(*   SealBeforeLanding    every path that writes its data files before its *)
(*                    commit seals and records them before the version     *)
(*                    lands; without it they are sealed once it landed.    *)
(*   FinishHoldsMark  a reconciler finish that fails holds the ref's mark  *)
(*                    before its version (commit_reconcile.scan_ref's      *)
(*                    contiguous), so the next pass finishes it; without   *)
(*                    it the mark moves past the version for good.         *)
(*   FinishFromOwn    a reconciler finish of a version whose predecessor   *)
(*                    a cleanup reclaimed reads what the version added off *)
(*                    its own transaction (commit_reconcile.finish_landed, *)
(*                    committed_files.added_by); without it the finish     *)
(*                    needs the version before, fails on every pass, and   *)
(*                    holds the mark for ever.                             *)
(*   SearchWhole      a takeover's search whose range (the versions after  *)
(*                    written_from) holds a version no longer readable,    *)
(*                    and no readable version carrying the key, refuses,   *)
(*                    409 InvalidTableState, the key's write perhaps       *)
(*                    landed in a version since removed                    *)
(*                    (dataplane.landed_under_key); without it the search  *)
(*                    reads past the gap, finds nothing, and writes again. *)
(*   StampBeforeGap   the search reads every version of its range that     *)
(*                    stands and answers the one carrying the key before   *)
(*                    it refuses for a removed one; without it a removed   *)
(*                    version anywhere in the range refuses a key whose    *)
(*                    write stands readable, on every later send.          *)
(*                                                                         *)
(* NOT MODELLED: branches (each ref is a history of its own, judged the    *)
(* same way), the announcement after a commit (CommitAnnounce.tla), the    *)
(* lifecycle doors and their fences (TableLifecycle.tla: every write door  *)
(* opens the table under the claim its resolution read, and a finalize     *)
(* naming the table asks again right before it, DoorTable.require_held and *)
(* require_named), erasure and the history fence (Erasure.tla, Fence.tla), *)
(* a merge-insert's and an update's own shapes (each is a stamped commit   *)
(* of one key here, as insert is), the keyed doors that commit no version  *)
(* (drop, deregister, register, rename, a create of a new name: a fence or *)
(* a name that no longer resolves refuses a superseded attempt, ADR 0240   *)
(* §8 and its race test), a store answering a put that landed as a lost    *)
(* race (land_keyed reads the versions since its plan for its own), and a  *)
(* stock client's own retry after a 503 (ADR 0240 Not modelled).          *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Mix,        \* which writers: "retry", "rebody", "lane", "keys", "nested", "overwrite", "restore", "gap"
          MaxFaults,  \* how many writers may die in one behaviour
          MaxTries,   \* how many times a stamped commit plans again after a lost race
          Off         \* the guards and assumptions a config removes

Guards == {"KeyBindsBody", "KeyRenews", "RenewIsOwn", "RecordOwnClaim", "RefusalHoldsKey", "TakeoverCounts", "AppendJudged",
           "KeyKeepsFirst", "ReleaseKeepsWritten", "SealBeforeLanding", "FinishHoldsMark", "FinishFromOwn", "SearchWhole",
           "StampBeforeGap"}
Assumptions == {"StoreCAS", "LeaseOutlastsCommit", "TurnLapses", "Reconciles"}
ASSUME Off \subseteq Guards \cup Assumptions
On(g) == g \notin Off

\* The writers of each mix: their kind, the key value each writes (a keyed attempt's body), and its path.
N == CASE Mix = "retry" -> 4 [] Mix = "rebody" -> 3 [] Mix = "lane" -> 4 [] Mix = "keys" -> 4 [] Mix = "nested" -> 2
       [] Mix \in {"overwrite", "restore"} -> 3 [] Mix = "gap" -> 4
Actors == 1..N
Kind(a) ==
  CASE Mix = "retry" -> (IF a <= 3 THEN "keyed" ELSE "insert")
    [] Mix = "rebody" -> "keyed"
    [] Mix = "lane" -> (IF a <= 3 THEN "keyed" ELSE "maint")
    [] Mix = "keys" -> (CASE a = 1 -> "insert" [] a = 2 -> "stock" [] a = 3 -> "delete" [] OTHER -> "maint")
    [] Mix = "nested" -> (IF a = 1 THEN "nested" ELSE "delete")
    [] Mix \in {"overwrite", "restore"} -> (IF a <= 2 THEN "keyed" ELSE "insert")
    \* A keyed append sent twice to a table declaring no key, beside a compaction and an unkeyed append: enough versions
    \* after the key's write for a removed one to stand between it and the head.
    [] Mix = "gap" -> (CASE a <= 2 -> "keyed" [] a = 3 -> "maint" [] OTHER -> "insert")
K(a) ==
  CASE Mix \in {"retry", "overwrite", "restore"} -> (IF Kind(a) = "keyed" THEN 1 ELSE 2)
    [] Mix = "rebody" -> (IF a = 2 THEN 2 ELSE 1)
    [] OTHER -> 1
Keyed(a) == Kind(a) = "keyed"
\* Whether the table declares a primary key, so that its rows are a set judged at every commit.
PKTable == Mix \notin {"lane", "overwrite", "restore", "gap"}
\* A keyed write that replaces the table's rows: an Overwrite (create mode=Overwrite) or a Restore.
Replaces(a) == Keyed(a) /\ Mix \in {"overwrite", "restore"}
\* How a writer's commit lands: "stamped" finalizes exactly the slot after its plan; "lance" is Lance's own commit, which
\* rebases onto whatever landed since (an append across an Overwrite or a Restore is refused).
Path(a) ==
  CASE Kind(a) = "nested" -> (IF On("AppendJudged") THEN "stamped" ELSE "lance")
    [] Kind(a) = "keyed" -> (IF Mix \in {"lane", "overwrite", "gap"} THEN "lance" ELSE "stamped")
    [] Kind(a) = "insert" -> (IF PKTable THEN "stamped" ELSE "lance")
    [] OTHER -> "stamped"
\* The table holds key 1 before any writer starts, where a writer's harm needs it held.
Seed == IF Mix = "nested" THEN <<[by |-> 0, op |-> "ins", k |-> 1, mark |-> FALSE, n |-> 1, path |-> "stamped", ds |-> TRUE, ts |-> TRUE, gone |-> FALSE]>> ELSE <<>>
\* How many versions a behaviour may remove, and how many reconciler finishes may fail: one of each is what a removal's
\* refusal and a held mark need to show, and a bound keeps the gate finite and its liveness checkable.
MaxRemovals == 1
MaxFinishFails == 1

\* The key's record. `wfrom` is the first version a write under the key can have landed at, one past the version the last
\* renewal's put was planned on, 0 while none reached a put (idempotency._Claimed.written_from); `at` is the version
\* the table was at when the attempt holding the claim claimed it, standing for the claim's own time, which a takeover
\* searched from without KeyKeepsFirst. `first` is a ghost, the version the first recorded answer named.
None == [st |-> "none", att |-> 0, body |-> 0, lapsed |-> FALSE, abody |-> 0, acnt |-> 0, aver |-> 0, first |-> 0, wfrom |-> 0, at |-> 0]

VARIABLES log,      \* the table's history: one record per version, in order
          rec,      \* the key's record on the control root
          pc,       \* each writer's next step
          plan,     \* the version each writer planned on; between a takeover and its search, the first version it searches
          renewed,  \* whether a keyed attempt renewed its claim since it last planned
          tries,    \* lost races so far
          told,     \* whether a keyed attempt's caller was told 503 at the request deadline
          ans,      \* what each writer was answered: "", "ok", "refused" (400, its key value held), "reused"
                    \* (400, the Idempotency-Key bound to another request), "conflict" (409), "gone" (409, the key's
                    \* write may be in a version its search could not read)
          abody, acnt, aver,  \* an "ok" answer's body, row count and version
          turn,     \* the writer holding the ref's commit turn, 0 for none
          faults,
          rmark,    \* the reconciler's mark on the ref: every version up to it was finished (commit_records.CommitMarks)
          rfails,   \* reconciler finishes that failed so far
          removals  \* versions removed so far

vars == <<log, rec, pc, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults, rmark, rfails, removals>>
\* What only the reconciler and a removal change.
others == <<rmark, rfails, removals>>

RECURSIVE RowsUpTo(_)
\* The key values the table holds once its first i versions have landed.
RowsUpTo(i) ==
  IF i = 0 THEN {}
  ELSE LET c == log[i] IN
       CASE c.op = "ins" -> RowsUpTo(i - 1) \cup {c.k}
         [] c.op = "del" -> RowsUpTo(i - 1) \ {c.k}
         [] c.op = "ow" -> {c.k}
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
  /\ rmark = Len(Seed) /\ rfails = 0 /\ removals = 0

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

\* Whether claiming the key now must search for a write an earlier attempt landed: once any attempt under the key reached
\* a put (KeyKeepsFirst, ReleaseKeepsWritten). Without them: every takeover of a lapsed claim, and no claim of a released one.
Searches ==
  IF rec.st = "in"
    THEN ~On("KeyKeepsFirst") \/ rec.wfrom # 0
    ELSE On("ReleaseKeepsWritten") /\ rec.wfrom # 0

\* idempotency.claim: a binding the record holds for another request is refused; a done key is replayed; a live claim
\* answers 409; a lapsed one is taken over (TakenOver) and its commit looked for; a free or released key is taken.
Claim(a) ==
  /\ pc[a] = "claim"
  /\ IF rec.st = "none"
       THEN /\ rec' = [None EXCEPT !.st = "in", !.att = a, !.body = K(a), !.at = Len(log)]
            /\ pc' = [pc EXCEPT ![a] = "plan"]
            /\ UNCHANGED <<ans, abody, acnt, aver, turn, plan>>
     ELSE IF On("KeyBindsBody") /\ rec.body # K(a)
       THEN /\ Finish(a, "reused") /\ UNCHANGED <<rec, abody, acnt, aver, plan>>
     ELSE IF rec.st = "done"
       THEN /\ Finish(a, "ok")
            /\ abody' = [abody EXCEPT ![a] = rec.abody]
            /\ acnt' = [acnt EXCEPT ![a] = rec.acnt]
            /\ aver' = [aver EXCEPT ![a] = rec.aver]
            /\ UNCHANGED <<rec, plan>>
     ELSE IF rec.st = "in" /\ ~rec.lapsed
       THEN /\ Finish(a, "conflict") /\ UNCHANGED <<rec, abody, acnt, aver, plan>>
     ELSE /\ rec' = [rec EXCEPT !.st = "in", !.att = a, !.lapsed = FALSE, !.at = Len(log)]
          /\ pc' = [pc EXCEPT ![a] = IF Searches THEN "scan" ELSE "plan"]
          /\ plan' = [plan EXCEPT ![a] = IF On("KeyKeepsFirst") THEN rec.wfrom ELSE rec.at + 1]
          /\ UNCHANGED <<ans, abody, acnt, aver, turn>>
  /\ UNCHANGED <<log, renewed, tries, told, faults>>

\* The versions a takeover's search reads: every one after written_from (plan[a] is the first), up to the head.
Range(a) == {i \in 1..Len(log) : i >= plan[a]}
\* Whether a version of that range is no longer readable: the search cannot prove the key's write did not land there.
Unwhole(a) == \E i \in Range(a) : log[i].gone

\* The versions of that range that stand and carry the key's stamp.
Hits(a) == {i \in Range(a) : log[i].mark /\ ~log[i].gone}
\* Whether the search refuses for a removed version: with StampBeforeGap only where no standing version carries the key.
RefusesGap(a) == On("SearchWhole") /\ Unwhole(a) /\ (Hits(a) = {} \/ ~On("StampBeforeGap"))

\* dataplane.landed_under_key, on every keyed door that commits a version: the commit carrying the key among the versions
\* after the one its search starts from, answered as its write was, its answer recorded. A removed version is not read.
Scan(a) ==
  /\ pc[a] = "scan"
  /\ ~RefusesGap(a)
  /\ LET hits == Hits(a) IN
     IF hits = {}
       THEN /\ pc' = [pc EXCEPT ![a] = "plan"]
            /\ UNCHANGED <<abody, acnt, aver>>
       ELSE LET i == CHOOSE j \in hits : TRUE IN
            /\ pc' = [pc EXCEPT ![a] = "record"]
            /\ abody' = [abody EXCEPT ![a] = log[i].k]
            /\ acnt' = [acnt EXCEPT ![a] = IF On("TakeoverCounts") \/ log[i].path = "lance" THEN log[i].n ELSE 0]
            /\ aver' = [aver EXCEPT ![a] = i]
  /\ UNCHANGED <<log, rec, plan, renewed, tries, told, ans, turn, faults>>

\* The door is refused between its takeover and its search (a turn wait refused, the table changed under its resolution):
\* a 4xx, which gives the claim back as the attempt's own (keyed_door, idempotency.release).
ScanRefused(a) ==
  /\ pc[a] = "scan"
  /\ Finish(a, "conflict") /\ ReleaseIfMine(a)
  /\ UNCHANGED <<log, plan, renewed, tries, told, abody, acnt, aver, faults>>

\* SearchWhole: a search whose range holds a removed version, and no standing one carrying the key (StampBeforeGap),
\* refuses, non-retryable (409 InvalidTableState), and never writes; the refusal is a 4xx after the claim, which gives
\* the claim back, its written_from kept, so every later send under the key searches the same range and is refused the
\* same way.
ScanUnwhole(a) ==
  /\ pc[a] = "scan" /\ RefusesGap(a)
  /\ Finish(a, "gone") /\ ReleaseIfMine(a)
  /\ UNCHANGED <<log, plan, renewed, tries, told, abody, acnt, aver, faults>>

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

\* idempotency.renew, once the ref's turn is free (DoorTable.finalize_version and land_keyed wait, then renew, then put):
\* the claim is still this attempt's, under the ETag read; a claim another attempt took answers 409 and nothing is
\* committed.
Renew(a) ==
  /\ pc[a] = "renew" /\ Free(a)
  /\ IF rec.st = "in" /\ (rec.att = a \/ ~On("RenewIsOwn"))
       THEN /\ rec' = [rec EXCEPT !.lapsed = FALSE, !.wfrom = plan[a] + 1]
            /\ renewed' = [renewed EXCEPT ![a] = TRUE]
            /\ pc' = [pc EXCEPT ![a] = "put"]
            /\ UNCHANGED <<ans, turn>>
       ELSE /\ Finish(a, "conflict") /\ UNCHANGED <<rec, renewed>>
  /\ UNCHANGED <<log, plan, tries, told, abody, acnt, aver, faults>>

\* Lance's own commit writes its fragments before the commit and its transaction file inside it; a stock writer stages
\* both, and its transaction file is sealed once landed, since it puts it again on each attempt of the commit.
Lance(a) == Path(a) = "lance"
Stock(a) == Kind(a) \in {"stock", "maint"}
\* A landed version: `ds` whether its data files are sealed and recorded, `ts` whether its transaction file is.
Commit(a) == [by |-> a, op |-> CASE Kind(a) = "delete" -> "del" [] Kind(a) = "maint" -> "rw" [] Replaces(a) -> "ow" [] OTHER -> "ins",
              k |-> K(a), mark |-> Keyed(a), n |-> IF Kind(a) \in {"delete", "maint"} THEN 0 ELSE 1, path |-> Path(a),
              ds |-> ~Lance(a) \/ On("SealBeforeLanding"), ts |-> ~Lance(a) /\ ~Stock(a), gone |-> FALSE]
\* A writer with something to seal once its version landed.
SealsAfter(a) == Lance(a) \/ Stock(a)

\* Lance refuses an append whose rebase crosses an Overwrite or a Restore (transaction.md § Append Compatibility).
CrossesAReplace(a) == Commit(a).op = "ins" /\ \E i \in (plan[a] + 1)..Len(log) : log[i].op = "ow"

\* The put-if-not-exists of the slot after the plan (A1). Lost: a stamped commit plans again under the ref's turn, up to
\* MaxTries, then answers 409; Lance's own commit rebases onto the slot now free and puts again, a keyed one renewing
\* its claim first (land_keyed: each attempt is one Lance commit with no retry of its own), an append refused across a
\* replace.
Put(a) ==
  /\ pc[a] = "put" /\ (Free(a) \/ renewed[a])
  /\ IF Len(log) = plan[a] \/ ~On("StoreCAS")
       THEN /\ log' = Append(log, Commit(a))
            /\ aver' = [aver EXCEPT ![a] = Len(log) + 1]
            /\ abody' = [abody EXCEPT ![a] = K(a)]
            /\ acnt' = [acnt EXCEPT ![a] = Commit(a).n]
            /\ IF SealsAfter(a)
                 THEN /\ pc' = [pc EXCEPT ![a] = "seal"] /\ UNCHANGED <<ans, turn>>
               ELSE IF Keyed(a)
                 THEN /\ pc' = [pc EXCEPT ![a] = "record"] /\ UNCHANGED <<ans, turn>>
                 ELSE Finish(a, "ok")
            /\ UNCHANGED <<plan, tries, rec, renewed>>
     ELSE IF Path(a) = "lance" /\ CrossesAReplace(a)
       THEN /\ Finish(a, "conflict") /\ ReleaseIfMine(a) /\ UNCHANGED <<log, plan, tries, abody, acnt, aver, renewed>>
     ELSE IF Path(a) = "lance"
       THEN /\ plan' = [plan EXCEPT ![a] = Len(log)]
            /\ IF Keyed(a) /\ On("KeyRenews")
                 THEN /\ pc' = [pc EXCEPT ![a] = "renew"] /\ renewed' = [renewed EXCEPT ![a] = FALSE]
                 ELSE UNCHANGED <<pc, renewed>>
            /\ UNCHANGED <<log, ans, abody, acnt, aver, turn, tries, rec>>
     ELSE IF tries[a] < MaxTries
       THEN /\ tries' = [tries EXCEPT ![a] = tries[a] + 1]
            /\ turn' = IF turn = 0 THEN a ELSE turn
            /\ pc' = [pc EXCEPT ![a] = "plan"]
            /\ UNCHANGED <<log, plan, ans, abody, acnt, aver, rec, renewed>>
     ELSE /\ Finish(a, "conflict") /\ ReleaseIfMine(a) /\ UNCHANGED <<log, plan, tries, abody, acnt, aver, renewed>>
  /\ UNCHANGED <<told, faults>>

\* committed_files.seal_and_record once landed: what the landed version names that only its landing let it seal, sealed
\* and recorded; then the key's answer, or the writer's.
SealLanded(a) ==
  /\ pc[a] = "seal"
  /\ log' = [log EXCEPT ![aver[a]].ds = TRUE, ![aver[a]].ts = TRUE]
  /\ IF Keyed(a)
       THEN /\ pc' = [pc EXCEPT ![a] = "record"] /\ UNCHANGED <<ans, turn>>
       ELSE Finish(a, "ok")
  /\ UNCHANGED <<rec, plan, renewed, tries, told, abody, acnt, aver, faults>>

\* idempotency.record_outcome, then the answer: recorded only over the attempt's own claim, onto its binding; an attempt
\* whose claim another took answers its own write and leaves the key's answer as it is.
Record(a) ==
  /\ pc[a] = "record"
  /\ rec' = IF (rec.st = "in" /\ rec.att = a) \/ ~On("RecordOwnClaim")
              THEN [rec EXCEPT !.st = "done", !.lapsed = FALSE, !.abody = abody[a], !.acnt = acnt[a], !.aver = aver[a],
                               !.first = IF rec.first = 0 THEN aver[a] ELSE rec.first]
              ELSE rec
  /\ Finish(a, "ok")
  /\ UNCHANGED <<log, plan, renewed, tries, told, abody, acnt, aver, faults>>

Step(a) == (Start(a) \/ Claim(a) \/ Scan(a) \/ ScanRefused(a) \/ ScanUnwhole(a) \/ Plan(a) \/ Renew(a) \/ Put(a) \/ SealLanded(a) \/ Record(a))
           /\ UNCHANGED others

---------------------------------------------------------------------------
\* The environment.

Die(a) ==
  /\ pc[a] \notin {"start", "done", "dead"} /\ faults < MaxFaults
  /\ pc' = [pc EXCEPT ![a] = "dead"]
  /\ faults' = faults + 1
  /\ UNCHANGED <<log, rec, plan, renewed, tries, told, ans, abody, acnt, aver, turn>> /\ UNCHANGED others

\* RequestDeadlineMiddleware answers 503 with no response begun; the door runs on.
Told(a) ==
  /\ Keyed(a) /\ pc[a] \notin {"start", "done", "dead"} /\ ~told[a]
  /\ told' = [told EXCEPT ![a] = TRUE]
  /\ UNCHANGED <<log, rec, pc, plan, renewed, tries, ans, abody, acnt, aver, turn, faults>> /\ UNCHANGED others

\* The key's lease passes on the store's clock; never between a renewal and the commit after it (A2).
KeyLapse ==
  /\ rec.st = "in" /\ ~rec.lapsed
  /\ ~(On("LeaseOutlastsCommit") /\ rec.att \in Actors /\ pc[rec.att] = "put" /\ renewed[rec.att])
  /\ rec' = [rec EXCEPT !.lapsed = TRUE]
  /\ UNCHANGED <<log, pc, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults>> /\ UNCHANGED others

\* A turn lapses at its hold, its holder alive or not (A3).
TurnLapse ==
  /\ turn # 0 /\ On("TurnLapses")
  /\ turn' = 0
  /\ UNCHANGED <<log, rec, pc, plan, renewed, tries, told, ans, abody, acnt, aver, faults>> /\ UNCHANGED others

\* The commit reconciler (A4, commit_reconcile.scan_ref and finish_landed): each pass reads the ref's versions after its
\* mark, oldest first, and finishes each, sealing and recording what a landed version names and no commit sealed, whoever
\* wrote it, its writer alive or not; a removed version is not in the ref's listing and is passed over. It changes only
\* what no writer reads (a version's seal, which a writer's own seal of it makes again, the same write), so it commutes
\* with every writer's step, and is let run once every writer has ended: the reconciler reads a version only once it is
\* older than its grace.
Reconciling == On("Reconciles") /\ (\A a \in Actors : Ended(a)) /\ rmark < Len(log)
AfterMark == log[rmark + 1]
\* The version after the mark stands and the one before it was reclaimed: the first version a cleanup retained.
PredecessorReclaimed == ~AfterMark.gone /\ rmark > 0 /\ log[rmark].gone

\* A finish of a version whose predecessor was reclaimed reads what it added off its own transaction (FinishFromOwn);
\* without, it needs the version before, which no pass can open again, so no finish of it succeeds.
ReconcileFinish ==
  /\ Reconciling
  /\ ~(PredecessorReclaimed /\ ~On("FinishFromOwn"))
  /\ log' = IF AfterMark.gone THEN log ELSE [log EXCEPT ![rmark + 1].ds = TRUE, ![rmark + 1].ts = TRUE]
  /\ rmark' = rmark + 1
  /\ UNCHANGED <<rec, pc, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults, rfails, removals>>

\* The finish fails (the store refused or did not answer the seal or the record: committed_files.finish_landed answers
\* False): with FinishHoldsMark the mark stays before the version and the next pass finishes it; without, the mark moves
\* past it as past a finished one.
ReconcileFails ==
  /\ Reconciling /\ rfails < MaxFinishFails
  /\ ~AfterMark.gone /\ ~(AfterMark.ds /\ AfterMark.ts)
  /\ rfails' = rfails + 1
  /\ rmark' = IF On("FinishHoldsMark") THEN rmark ELSE rmark + 1
  /\ UNCHANGED <<log, rec, pc, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults, removals>>

\* A cleanup or the owner tier's version/delete removes a version that is not the head (a cleanup never removes the
\* latest, and version/delete refuses it). The version stays in `log` as having landed, marked gone: no reader reads it,
\* and KeyAppliedOnce still counts it.
RemoveVersion ==
  /\ removals < MaxRemovals
  /\ \E i \in 1..(Len(log) - 1) :
       /\ ~log[i].gone
       /\ log' = [log EXCEPT ![i].gone = TRUE]
  /\ removals' = removals + 1
  /\ UNCHANGED <<rec, pc, plan, renewed, tries, told, ans, abody, acnt, aver, turn, faults, rmark, rfails>>

Next == (\E a \in Actors : Step(a) \/ Die(a) \/ Told(a)) \/ KeyLapse \/ TurnLapse \/ ReconcileFinish \/ ReconcileFails \/ RemoveVersion

Spec == Init /\ [][Next]_vars /\ \A a \in Actors : WF_vars(Step(a)) /\ WF_vars(TurnLapse) /\ WF_vars(ReconcileFinish)

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
RefusedWriteAbsent == \A a \in Actors : Heard(a) /\ ans[a] \in {"refused", "reused", "conflict", "gone"} => \A i \in 1..Len(log) : log[i].by # a

\* A keyed request is refused for a version its search could not read only while no standing version holds the write
\* its key names: a write the table still holds is answered, never refused for a gap elsewhere in the range.
GoneIsTrue == \A a \in Actors : Keyed(a) /\ ans[a] = "gone" => ~\E i \in 1..Len(log) : log[i].mark /\ ~log[i].gone

\* A keyed request is refused for a key value its table holds only while the write its Idempotency-Key names is not in
\* the history: a resend is never told that its own landed write is another row holding its key.
KeyedRefusalIsTrue == \A a \in Actors : Keyed(a) /\ Heard(a) /\ ans[a] = "refused" => Marked = {}

\* A keyed attempt answered success is answered for its own body, never for another request's.
SuccessIsForItsBody == \A a \in Actors : Keyed(a) /\ ans[a] = "ok" => abody[a] = K(a)

\* A success answers the rows the write it reports added.
SuccessCountsWhatLanded == \A a \in Actors : ans[a] = "ok" => acnt[a] = log[aver[a]].n

\* The answer a key serves once answered is the one it first recorded: every later send under the key is answered with
\* what the attempt holding the key answered.
AnswerStands == rec.first # 0 => rec.st = "done" /\ rec.aver = rec.first

\* Insert-if-absent: a version never adds a key value the version before it holds.
OneRowPerKey == PKTable => \A i \in 1..Len(log) : log[i].op = "ins" => log[i].k \notin RowsUpTo(i - 1)

\* Once failures stop, every writer is answered or gone, and no turn is held for ever.
EveryWriterEnds == <>[](\A a \in Actors : Ended(a))

\* No landed version names a data file that is not sealed and recorded: from the moment a version lands, no put replaces
\* what it reads (ADR 0242).
LandedDataSealed == \A i \in 1..Len(log) : log[i].ds

\* Every file a retained landed version names is sealed and recorded in the end, its writer alive or not and a finish that
\* failed included: the transaction file, which only the version's landing lets its writer seal, is finished by the
\* reconciler when the writer died first.
EveryFileSealed == <>[](\A i \in 1..Len(log) : log[i].gone \/ (log[i].ds /\ log[i].ts))

\* Once failures stop, the reconciler's mark reaches the head, a cleanup in the behaviour included: no finish that cannot
\* heal holds it.
MarkReachesHead == <>[](rmark = Len(log))

TypeOK ==
  /\ pc \in [Actors -> {"start", "claim", "scan", "plan", "renew", "put", "seal", "record", "done", "dead"}]
  /\ ans \in [Actors -> {"", "ok", "refused", "reused", "conflict", "gone"}]
  /\ turn \in Actors \cup {0}
=============================================================================
