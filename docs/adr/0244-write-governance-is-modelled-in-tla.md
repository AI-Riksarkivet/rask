# 0244. Write governance is modelled in TLA+, and a rule a write was judged under is the rule it lands under (2026-11-18)

**Status:** accepted; the model of the last part of the write path no model described. Four defects were found, each
reproduced against the real catalog over HTTP and fixed model first (owner's standing ruling: fix now). An independent
review then found three more (W1, W2, W3: §7 to §9), each reproduced over HTTP with held requests, two of them places
where the code did less than the model assumed; each is now in the model as the code's own steps and FIXED. A second
review found two more (U1, U2: §11, §12), each reproduced over HTTP with held requests: the undrops judged their
namespace outside its add fence, which the model's undrop did in one step with its add, and the insert door's Overwrite
landed through Lance's own commit, which C2 had closed for the create door's only. It amends
[ADR 0212](0212-publication-goes-and-a-tables-data-contract-is-checked-on-every-commit.md) §3 ("the door reads the table
before it writes the record and again after") and withdraws its "Not taken: serializing a set with every commit", and
it amends [ADR 0211](0211-labels-a-project-defines-replace-tiers-and-the-run-rule-stands-alone.md) §2 with where a door
placing a table reads the run rule.

## Context

Write governance is two rules a commit is held to in the catalog's write path, so that no writer can go around them:

- **the data contract** (ADR 0212): a table's owner sets an ODCS v3 contract, and every commit keeps the columns it
  names (`contracts.require_kept`, which refuses a violation the commit introduces over the version it follows);
- **the run rule** (ADR 0211): a namespace declares `rask.requires_run` at its create, every namespace beneath it is
  bound, and under it a write that adds data names its run and inputs, while `update`, `add_columns`, `register` and a
  rename in from outside are refused (`catalog.core.write_judge`).

Both are judged on one read and land later. The property both must keep: **no committed version of a governed table
breaks the rule that stood when it committed, by any door.** Nothing had checked whether a rule can change between its
read and the landing.

## Decision

1. **`specs/catalog/WriteGovernance.tla` models both seams in one module.** They share no state (a contract is a
   table's, keyed by its uuid; the rule is a namespace's), but they are one question, whether the rule can change between
   a door's read of it and its landing, with one property and one guard table, so one module; a config runs one seam,
   the other's actors empty.
   - **Variables.** The contract seam: each ref's versions (`hist`: a version's schema, as the set of columns it keeps as
     a contract names them, its maker, and the contract in force when it landed), the contract record (`rec`, `recBy`,
     whether it is in force, `recLive`, and the in-force record an in-flight one carries, `recPrev`), the record last put
     in force by an answer (`truth`, a ghost), each door's answer, the in-force records a rollback did not put back
     (`lost`), each writer's step, plan, planned schema, read contract and lost races, each set's step, the record its
     refusal puts back, refs still to verify and its barrier's plan. Set `s1` states column `a`, set `s2` column `c`
     only (a major version that no longer names `a`). The run rule seam: whether namespace `x` stands, whether it
     requires a run, its incarnation (its uuid), the placers holding its add fence, whether the actor remaking `x` holds
     its drop fence, the tables standing in it (or beneath it) that hold data no run produced, each placer's step and the
     incarnation an undrop's table, or a namespace undrop's namespace, was dropped from, and the step of the actor
     remaking `x`.
   - **Actions.** `WPlan` (a commit reads the ref's head), `WJudge` (reads the contract after its plan, as
     `commit_stamped` and `version/create` do, and refuses what it introduces), `WLand` (an exact commit fills the slot
     after its plan or loses and plans again; Lance's own Overwrite lands over whatever committed since); `SFirst` (the
     set door's read of every ref, `_require_described`), `SRead` (the record read on its own before the write, the code
     before W2), `SKeep` (`contracts.keep`, the record written in flight), `SSecond` (the second read the door made
     before this ADR) or `SBPlan`/`SBLand` (the barrier, §2), `SMark` (`contracts.confirm`), each refusal taking the
     record back (`Rollback`, `contracts.take_back`); `Delete` (`contracts.forget`); `Branch` (a branch from main's
     head). `PJudge` (the write judge reads `x`'s rule; under guard PlacedUnderHold while holding `x`'s add fence),
     `PAdd` (the backend's add under the fence), `PUndrop`. The placers are a create, a register, a rename in, a table
     undrop and a namespace undrop (`y`, dropped from `x` by a cascade of `y` with a table in it, made again under `x`
     with the table beneath it); an undrop reads `x`'s uuid in `PJudge`, under `x`'s add fence with guard UndropHeld,
     and adds in `PAdd` with no second read, as the code does. `OTake` (the Overwrite takes `x`'s drop fence for its run),
     `ODrop` (the Restrict drop under that fence, once `x` is empty and no add holds it), `OForget`
     (`_forget_namespace`) and `OCreate` (`x` made afresh with a new uuid, declaring the rule, an add under the parent's
     fence). Mix `rule` runs these as the namespace Overwrite, one request; mix `redeclare` as the drop door and then a
     create door, two requests with nothing held between them.
   - **Invariants.** `ContractKept`: no version a writer landed introduces a violation of the contract in force when
     it landed (a record is in force once the set that wrote it marked it, right before it answers 200; while a set is
     in flight the contract in force is the one its record replaced, which its refusal puts back; the in-flight contract
     is that set's to judge). `RollbackRestoresInForce`: a refused set puts back the contract in force when it wrote (the
     last one a set or the delete door answered 200 for, or none), never another: so a set answered 200 is replaced only
     by a later set or delete, and a refused contract never stands in force. `RunRuleHeld`: no table holding data no run
     produced stands in a namespace that requires a run. `OverwriteUnseen`: no placer holds `x`'s add fence while the
     Overwrite is between its drop and its re-create. `EveryActorMoves`: every unfinished actor of the run rule seam has
     a step, so none waits on another's hold (§ Lock order). `TypeOK`.
   - **What it proves** (`scripts/tla_check.sh WriteGovernance`, two TLC workers on four cores shared with other
     agents, 2026-11-18), every gate config checking every invariant:

     | Config | Actors | Distinct states | Time |
     |---|---|---|---|
     | `WriteGovernance.cfg` | a drop and an add (exact), an Overwrite dropping the column, one set; one replan | 11,791 | 2.4 s |
     | `WriteGovernanceBranch.cfg` | a branch made while a set runs, a drop on it, a restore of main | 1,448 | 1.6 s |
     | `WriteGovernanceSets.cfg` | two sets (the second no longer naming the column) and a delete, a drop and a commit adding the column back | 650,613 | 14.5 s |
     | `WriteGovernanceWide.cfg` | the same with two lost races each | 887,175 | 18 s |
     | `WriteGovernanceRule.cfg` | a create, a register, a rename in, a table undrop and a namespace undrop beside the Overwrite declaring the rule, in its steps | 2,676 | 1.3 s |
     | `WriteGovernanceRedeclare.cfg` | the same beside the drop door and then a create door declaring the rule | 1,868 | 1.5 s |

2. **Guards**, each with the config that removes it and must fail, and the test that fails without it:

   | Guard | Where | Config (invariant) | Test |
   |---|---|---|---|
   | Judged: every commit reads the contract after its plan and refuses what it introduces | `dataplane.commit_stamped`, `managed_versions.judge` (`contracts.require_kept`) | `WriteGovernanceUnjudged.cfg` (ContractKept) | `test_a_tables_contract_is_held_on_every_commit_a_stock_client_or_a_door_makes` |
   | JudgedStrict (new, W2): while a set is in flight a commit is held to its contract and to the one in force it replaced | `contracts.ContractRecord.binding`, read by `DoorTable.binding` | `WriteGovernanceJudgedLoose.cfg` (ContractKept) | model only (Could not confirm, §8) |
   | Barrier (new, C1): a set commits on every ref listed once its record stands, a stamped commit whose plan judges the ref's head against the contract | `endpoints/contracts.py` `_verify_on_every_ref` | `WriteGovernanceSecondRead.cfg` (ContractKept) | `test_a_commit_judged_before_its_contract_was_set_does_not_land_after_the_set_answered[door]`, `[stock]` |
   | OverwriteStamped (new, C2; U2 for the insert door): a table's Overwrite is a stamped commit, at the create door and at the insert door alike | `dataplane._overwrite_stamped` | `WriteGovernanceOverwriteRebases.cfg` (ContractKept) | `…[overwrite]`; `test_an_insert_overwrite_keeps_a_column_added_after_it_read_the_schema[under-a-contract-naming-it]` |
   | KeepsSchema (new, U2; not modelled, the door's own promise): the insert door's Overwrite refuses a ref whose schema changed since its rows were aligned to it | `dataplane._overwrite_stamped(keeps_schema=True)` | — | `…[under-no-contract]`, and `[under-a-contract-naming-it]` answers code 19 instead of 14 without it |
   | RollbackOwn (new, C3): a refused set takes back only its own record | `contracts.take_back` (`_replace_own`) | `WriteGovernanceRollbackAny.cfg` (RollbackRestoresInForce) | `test_a_refused_set_takes_back_only_the_contract_it_wrote[after-its-record]` |
   | ReplacedInKeep (new, W2): what a refused set puts back is the record its own conditional write replaced | `contracts.keep` (`exchange_json`, `ContractRecord.replaced`) | `WriteGovernanceReplacedRead.cfg` (RollbackRestoresInForce) | `test_a_refused_set_takes_back_only_the_contract_it_wrote[before-its-record]` |
   | RestoreInForce (new, W2): a record written over an in-flight one carries the in-force record that one carried | `contracts.keep` (`ContractRecord.in_force_record`) | `WriteGovernanceRestoreRefused.cfg` (RollbackRestoresInForce) | model only (Could not confirm, §8) |
   | MarkOwn (new, W2): a set answers 200 only once it marked its own record in force | `contracts.confirm`, `set_table_contract` | `WriteGovernanceMarkAny.cfg` (RollbackRestoresInForce) | model only (§8) |
   | PlacedUnderHold (new, R1): a door placing a table reads the namespace's rule under its add fence, held through the add | `write_judge.placing`, at `table_create.create_governed_table`, `tables.register_table`, `tables.rename_table` | `WriteGovernanceUnheld.cfg` (RunRuleHeld) | `test_a_table_placed_beside_the_overwrite_that_declares_the_rule_does_not_stand_under_it`: `[create]` (held inside the fence) fails without the hold; `[create-before-its-fence]`, `[register]`, `[rename]` (held before it) fail without the second read |
   | PlacedStanding (new, W1): inside that hold a namespace that does not stand is refused before its rule is read | `write_judge.placing` (`requires_run(must_stand=True)`) | `WriteGovernanceUnstanding.cfg` (RunRuleHeld) | `…[create-after-a-drop-and-a-create]` |
   | OverwriteHeld (new, W1): the namespace Overwrite holds the namespace's drop fence from its emptiness check through its re-create | `endpoints/namespaces.py` `_create_or_replace_empty_namespace` | `WriteGovernanceOverwriteUnheld.cfg` (OverwriteUnseen); with PlacedStanding also off, `WriteGovernanceOverwriteGap.cfg` (RunRuleHeld) | `…[create-between-drop-and-recreate]`, `[register-…]`, `[rename-…]`: answered 404 instead of 409 without it, and a table no run produced stands under the rule without both |
   | RenameJudged: a rename into a namespace requiring a run from one that does not is refused | `write_judge.judge`, `placing` | `WriteGovernanceRenameUnjudged.cfg` (RunRuleHeld) | `test_a_write_names_its_run_where_a_namespace_above_it_requires_one` and the rename case above |
   | RegisterJudged: a register under the rule is refused | `UNSTAMPABLE_DATA_DOORS` | `WriteGovernanceRegisterUnjudged.cfg` (RunRuleHeld) | the register case above |
   | UndropChecksParent: an undrop, of a table or of a namespace, lands only in the namespace it was dropped from, by its uuid | `trash_doors.require_its_own_parent` | `WriteGovernanceUndropUnchecked.cfg` (RunRuleHeld) | measured once; under UndropHeld the Overwrite meets the hold first, so no test removes it alone |
   | UndropHeld (new, U1): an undrop reads that uuid, and the namespace's rule, under the namespace's add fence, held through its register (a namespace undrop: through each create and register beneath it) | `trash_doors.placing_back` (`write_judge.placing`), at `trash_doors.restore_kept_table` and `namespaces.undrop_namespace` | `WriteGovernanceUndropUnheld.cfg` (RunRuleHeld) | `test_a_table_placed_beside_the_overwrite_that_declares_the_rule_does_not_stand_under_it[undrop]`, `[namespace-undrop]` |

3. **C1: a set beside a commit judged before it.** Trace: a commit plans on version N and reads the table's contract
   (none); the set's first read finds every ref meeting the contract, it writes the record, its second read finds every
   ref meeting it again, and it answers 200; the commit lands at N+1, dropping a contracted column. The table then holds,
   under a contract answered 200, a version that breaks it, and `require_kept` exempts that violation from every later
   commit, so the column stays unguarded until its owner mends it. Real in `endpoints/contracts.py` (the second read)
   and ADR 0212's own "Not taken". Reproduced: a commit held right after its contract read (`DoorTable.contract`),
   the set run whole (200), the commit released: on the base the door's `drop_columns`, a stock client's
   `drop_columns` through `version/create`, and an Overwrite each dropped `v`. **Fix: the set commits on every ref.**
   Once the record stands the door commits on main and on each branch it then lists a version that changes nothing (an
   `UpdateConfig` that updates nothing, §9), as a stamped commit whose plan judges the ref's head against the
   contract. A commit lands only at the slot after the version it planned on (A1), and reads the contract
   after that plan, so one that read before the record stood either landed first, and the set's plan judges it, or
   loses its slot to the set's version and reads the contract when it plans again. A branch made after the listing was
   made after the record stood, so every commit on it reads the contract. A caller now sees: the set lands one version
   per ref, each announced on lineage as a schema metadata update by the setter; the held commit answers 409 (code 19)
   for the contract instead of 200; and a set whose ref a commit made stop meeting the contract meanwhile answers 409
   (code 19) with the contract in force it replaced put back. With the barrier replaced by a second read the `[door]` and
   `[stock]` cases fail again; `[overwrite]` still holds, by C2's guard, its stamped commit reading the contract again.
4. **C2: an Overwrite lands over the set's version.** Found by the model once C1 was fixed: Lance's own commit of an
   Overwrite does not conflict with anything committed since its read but another Overwrite or an UpdateConfig of a
   config key it upserts, which pylance's `LanceOperation.Overwrite` cannot name (measured on pylance 13.0.0: an
   Overwrite landed over an UpdateConfig of the config, the table metadata and the schema metadata alike). Its contract
   was judged on its first read. **Fix: a table's Overwrite is a stamped commit** (`dataplane._overwrite_stamped`), judged
   against the version it lands on and the contract read after its plan, and finalized at the slot after it, as every
   door's commit is; each attempt writes its rows again, since a lost race deletes what the attempt wrote. The early
   judgement in `create_table` stays, so an Overwrite the standing contract forbids is refused before its rows are
   written. A caller now sees: an Overwrite racing another commit plans again on the new head (up to the stamped commit's
   attempts) instead of landing over it, its commit judged for the primary key, the encoding knobs and the contract
   against what it replaces. `CommitPath.tla`'s mix `overwrite` is now a stamped commit (`Path`), and its configs hold
   and fail as before. With `_overwrite_stamped` landing through Lance's own commit the `[overwrite]` case fails again.
5. **C3: a refused set takes back another's contract.** Trace: set A writes its record; set B, a major version that
   no longer names `v`, writes over it, finds every ref meeting it and answers 200; `v` is dropped under B; A's
   verification finds `v` gone and puts back what it replaced, nothing, deleting B's contract. Real in
   `contracts.restore` (an unconditional put or delete). Reproduced: A held after `contracts.keep`, B set (200), `v`
   dropped (200), A released: B's contract was gone. **Fix: `contracts.take_back` puts back the replaced record only
   under the ETag of a read that found A's own record; none is put back as a record holding no contract** (the store
   deletes unconditionally), which `held` reads as none and a set replaces. A caller now sees B's contract standing;
   with the own-record check removed the test fails again.
   The corner this left (B too refused afterwards put back A's refused contract) is closed by W2 (§8).
6. **R1: a placement judged before the namespace was made afresh under the rule.** Trace: a create, a register or a
   rename into `x` (which declares nothing) passes the write judge; an admin's Overwrite drops `x`, empty, and makes it
   afresh declaring the rule; the door's add lands in the new `x`. Real: the write judge reads the rule in the router,
   the namespace fence is held only around the backend's add (`fenced_namespace`). Reproduced: each door held at the
   backend call after its judgement and before its add, the Overwrite run whole (200), the door released (200): a table
   holding data no run produced stood under the rule, for all three doors (and for a create sent as ExistOk). **Fix: `write_judge.placing`**: the door reads
   the rule again while it holds the namespace's add fence, and keeps it through the add; the Overwrite's drop takes the
   drop fence, which no add shares, and the backend refuses it once a table stands in the namespace (A3, A4), so the
   rule read is the one the table lands under. A caller now sees: the Overwrite answers 409 (code 14, the namespace is
   held) while a placement into it runs, or the placement is refused as the rule refuses it. A create holds the fence for
   its whole write, so a drop of its namespace answers 409 for that long. This fix left the gap W1 (§7) open.
7. **W1 (found by review, FIXED): a placement holding the add fence of a namespace that does not stand.** The model's
   `PJudge` under PlacedUnderHold ran only while `x` stood; the code had no such check. The fence is the NAME's, taken
   whether or not a namespace stands under it, `requires_run` read a missing namespace as declaring nothing, and the
   Overwrite (`_create_or_replace_empty_namespace`) dropped `x` under `x`'s drop fence, released it, ran
   `_forget_namespace`, and created `x` again declaring the rule as an add under the PARENT's fence, which nothing
   excludes from a placer holding `x`'s add fence. Reproduced over HTTP on the parent commit, each door held right before
   it takes its add fence and again inside it (create at its write's look, `describe_table`; register at
   `register_table`; rename at the source's `deregister_table`), the Overwrite held between its drop and its re-create:
   the door took the fence, read no rule, the Overwrite answered 200 with `rask.requires_run: true`, and the door
   answered 200: a table no run produced stood under the rule, for create, register and rename alike; and the same with
   the drop door and then a create door declaring the rule, nothing held between them. The model now splits the
   Overwrite as the code does (`OTake`, `ODrop`, `OForget`, `OCreate`; mix `redeclare` runs the two-door version).
   **Fix, both halves:** (a) PlacedStanding: inside its hold a placement refuses a namespace that does not stand, before
   it reads the rule (`write_judge.placing`, `requires_run(must_stand=True)`, every namespace on the path read); (b)
   OverwriteHeld: the Overwrite holds `x`'s drop fence from its emptiness check through its re-create
   (`_held_by_the_door(..., NamespaceWork.DROP)`), so "missing and about to be remade under a rule" is never visible to a
   placer holding `x`'s add fence. The hold is the name's and the re-create's take is the parent's, and every take
   refuses rather than waits, so the two cannot deadlock. A caller now sees: a placement sent while the Overwrite is
   between its drop and its re-create answers 409 (code 14); one whose namespace was dropped and not made again answers
   404 (code 1, `NamespaceNotFoundError`); an Overwrite answers 409 while a placement holds the namespace's add fence (as
   its drop did before). Configs: `WriteGovernanceUnstanding.cfg` (PlacedStanding off, mix `redeclare`) and
   `WriteGovernanceOverwriteGap.cfg` (both off, the review's trace) fail RunRuleHeld; `WriteGovernanceOverwriteUnheld.cfg`
   (OverwriteHeld off) fails OverwriteUnseen, and with PlacedStanding on fails no other invariant: each half alone keeps
   the Overwrite's race closed, and only (a) closes the two-door one. Tests, cases of the R1 test:
   `[create-between-drop-and-recreate]`, `[register-…]`, `[rename-…]` answer 404 instead of 409 with OverwriteHeld
   removed, and a table no run produced stands under the rule with both removed; `[create-after-a-drop-and-a-create]`
   stands under the rule with PlacedStanding removed.
8. **W2 (found by review, FIXED): a refused set deleted a contract another set got 200 for.** `set_table_contract`
   read the record it would put back (`_standing`) before `contracts.keep`, which then did its own read and replace; the
   model's `SKeep` took both in one step. Reproduced on the parent commit: set A (2.0.0, requiring `v`) held after that
   read and before `keep`; set B (1.0.0, without `v`) answered 200; `drop_columns v` 200; A released: its `keep` evolved
   over B's record, its barrier found `v` gone, 409 (code 19), and `take_back` found A's own record and put back none:
   B's contract, answered 200, was gone. **Fix: a record is IN FLIGHT until its set answers.** `contracts.keep` writes it
   with `in_force: false` and, in `replaced`, the in-force record it replaced, read inside its own conditional write
   (`exchange_json`, ReplacedInKeep; over another in-flight record, the in-force one that record carries,
   RestoreInForce); the set marks its own record in force once its barrier landed on every ref (`contracts.confirm`,
   under the ETag of a read that found it, MarkOwn) and answers 200, or answers 409 (code 14) when another set or the
   delete door wrote over it meanwhile; a refused set puts back, over its own record only, what its record carries
   (`contracts.take_back`). What a refusal restores was itself in force, or is none: the corner §5 left (two concurrent
   sets both refused, the second putting back the first's refused contract) is closed, and
   `WriteGovernanceRestoreRefused.cfg` fails without RestoreInForce.
   **What a writer is judged against while a set is in flight:** both contracts, the stricter reading
   (`ContractRecord.binding`, JudgedStrict): the in-flight one, which the barrier would otherwise refuse the set for, and
   the one in force, which the set's refusal puts back. Without the second, a commit dropping a column the in-force
   contract names lands while a set that no longer names it is in flight, and the set's refusal puts that contract back
   over the violation (`WriteGovernanceJudgedLoose.cfg`, ContractKept). The model's `ContractKept` now reads the contract
   in force while a set is in flight as the one its record carries, and `RollbackRestoresInForce` replaces
   `AnsweredSetStands`, which it implies (and it also refuses a refused contract restored into force). A caller now sees:
   `describe` answers `in_force` (false while a set is in flight or died there, null for no contract); a commit made
   while a set is in flight is held to both contracts; a set another overtook before it took effect answers 409 (code
   14). Configs: `WriteGovernanceReplacedRead.cfg`, `WriteGovernanceRestoreRefused.cfg`, `WriteGovernanceMarkAny.cfg`
   (RollbackRestoresInForce), `WriteGovernanceJudgedLoose.cfg` (ContractKept). Test: the review's interleaving is
   `test_a_refused_set_takes_back_only_the_contract_it_wrote[before-its-record]`, whose second contract is gone again
   with the record put back read before `keep`; `[after-its-record]` also reads the in-flight record as not in force.
   **A set that dies** after `keep` and before it answers leaves its record in flight with no barrier: it keeps judging
   every commit against both contracts (fail closed) and promises nothing (`describe` shows `in_force: false`); a commit
   judged before the record that lands in that window can leave a violation of the dead contract, which `require_kept`
   then exempts as the table's own, and the same set retried answers 409 "does not meet". What removes it is its owner:
   the delete door, or a set over it (which replaces it and carries the in-force record it carried). That is enough: the
   state is visible on `describe`, it refuses only more than the contract in force does, and nothing reads it as a
   promise.
9. **W3 (found by review, FIXED): a stock client's restore to a version before the latest set was refused.** The
   barrier stamped the schema metadata key `rask.contract`, and `managed_versions._refuse_changed_schema_governance`
   refuses any change to reserved schema metadata; a restored manifest is the target version's own, which lacks the
   stamp or carries an older one, so the restore was refused 400 "reserved schema metadata (lineage.*, rask.*) is changed
   only through schema_metadata/update", a door that refuses `rask.*` from its callers. Reproduced on the parent commit:
   create, `alter_columns`, the set (v3 carrying `rask.contract: orders@1.0.0`), a stock restore of version 2 refused at
   `version/create` with that message. Lance's own rules name the cause: a Restore conflicts with an UpdateConfig "only if
   it updates schema or field metadata, which a restore rewinds" (`lance_docs/lance/format/table/transaction.md`
   § Restore Compatibility). **What the barrier needs is the slot, not a key.** Every commit that can change what a
   contract names is an exact commit, finalized at the slot after the version it planned on (A1): the doors' stamped
   commits, the Overwrite (C2) and a stock client's `version/create`; what lands through Lance's own commit is an append,
   which keeps the schema of the version it lands on. So the barrier is the smallest commit that takes a slot, an
   `UpdateConfig` that updates nothing (`_verify_on_every_ref`). Measured on pylance 13.0.0: committed through Lance's
   own commit and detached alike, it lands a version whose schema metadata and config are its parent's; under Lance's own
   commit a `Project` and an `Append` planned before it rebase over it, so it orders nothing by Lance's conflict rules
   and needs not to, the catalog's put-if-not-exists of the slot does; a restore to the version before it lands. The
   stamp and `contracts.CONTRACT_KEY` are deleted, and `_refuse_changed_schema_governance` is unchanged, since nothing
   writes a reserved key a later commit must carry. The barrier version is announced as before, as a schema metadata
   update by the setter, the operation the commit reconciler names every `UpdateConfig` with
   (`managed_versions.LINEAGE_OPERATION`). A caller now sees: the set's version changes nothing; a restore, stock or at
   the catalog's door, to a version older than the set lands when it keeps what the contract names, and is refused 409
   (code 19) with the contract's own message when it does not. Test: `test_a_tables_contract_is_held_on_every_commit_…`
   asserts the set's version carries its parent's schema metadata and config, restores version 2 (before the set) from a
   stock client and at the door, and later restores a version lacking the contracted column `n`, refused by the contract
   ("column 'n' is gone") both ways; with the stamp put back it fails on the first.
10. **Smaller, same change.**
    - **The stamped Overwrite's bound** (C2). Its body is capped at `LANCE_MAX_BODY_BYTES` (256 MiB), and it makes at
      most `_STAMPED_COMMIT_ATTEMPTS` (10) attempts, each writing its rows again (a lost race deletes what the attempt
      wrote): at most ten times the body written, then 409 (code 14) "lost the race for the next version". Its commit
      turn is taken after its first lost race, as every stamped door's is, held for `commit_turns.HOLD` (30 s) with no
      renewal, and released when the commit ends (`commit_stamped`'s `ExitStack`); an attempt that outlasts the hold
      lets the ref's other writers past it, and then races as it would with no turn. No stamped door renews its turn:
      the turn is an aid to progress, and correctness is the slot's put-if-not-exists.
    - **The register door renews its Idempotency-Key after its fence is held** (ADR 0227 §8: renew before the
      irreversible effect, with nothing slow between): `renew_held` moved inside `write_judge.placing`, right before
      `register_table`, so the fence take and the rule reads no longer sit between the renewal and the put.
11. **U1 (found by the second review, FIXED): an undrop judged its namespace outside the namespace's add fence.** The
    table undrop read the parent's uuid once, at the door (`require_its_own_parent` on the resolver's read), and
    registered the table later in `trash_doors.restore_kept_table`, holding only the table's claim fence; the namespace
    undrop held the add fence of the namespace it restored, not its parent's, and read the parent's uuid before
    `create_or_keep_namespace` and the tables' registers. The model's undrop did the check in one step with the add
    (`PAdd`, `pgen[p] # gen`), which the code did not, and the model had no namespace undrop. The namespace Overwrite's
    emptiness check lists live and declared tables, never trashed ones, so it ran in that gap. Reproduced over HTTP on
    the parent commit: a table dropped from `plain` (kept in the trash), its undrop held after its parent check and before
    its claim fence, `plain` overwritten declaring the rule (200), the undrop released (200): the table stood under the
    rule, attached under a namespace of another uuid; and a namespace `plain$sub` cascade-dropped with a table in it, its
    undrop held before it made `sub` again, the same Overwrite (200), the undrop released (200): `plain$sub$t` stood
    under the rule. **Model:** the undrop's uuid read moved into `PJudge`, its add in `PAdd` with no second read, the
    namespace undrop added as a fifth placer, and guard UndropHeld; `WriteGovernanceUndropUnheld.cfg` (the check outside
    the hold, as the code did) fails RunRuleHeld, and the gate configs hold with it, checking `EveryActorMoves` too.
    **Fix: an undrop is a placement like any other.** `trash_doors.placing_back` takes the parent's add fence through
    `write_judge.placing` (the parent must stand, its rule read; no rule refuses an undrop, which restores what stood
    under the rule that namespace holds all its life, A3), and inside it `trash_doors.require_its_own_parent` reads the
    parent's uuid from the backend (not from the request's resolver, which caches the router's earlier read) and refuses
    another. `restore_kept_table`, the one step both undrops register a table by, runs inside that hold, so every table
    either undrop restores is placed under its own parent's; the namespace undrop holds its parent's for its whole run
    and places each namespace it makes again under that namespace's parent's. A caller now sees: an undrop answers 409
    (code 14) while its namespace is being dropped or made afresh, 404 (code 1) when it does not stand, and 409 (code 19)
    when another namespace holds its name; a namespace Overwrite answers 409 (code 14) while an undrop into it runs.
12. **U2 (found by the second review, FIXED): `/insert?mode=overwrite` landed through Lance's own commit.**
    `dataplane._land_rows` wrote its fragments with `mode="overwrite"` and committed them with
    `lance.LanceDataset.commit`, unstamped and with no contract read where it landed; its rows had been aligned to the
    schema the ref had when the request arrived (`coerce_insert_arrow`). Lance lands an Overwrite over anything since its
    read but another Overwrite or an UpdateConfig of a key it writes (§ Overwrite Compatibility), so the set's barrier
    ordered nothing against it. Reproduced on the parent commit: the overwrite held after it wrote its fragments, a
    column `n` added (200), a contract naming `n` set (200, in force), the overwrite released (200): `n` was gone. The
    door table's sentence that an insert "adds fragments and keeps the schema of the version it lands on" held for an
    append only. **Model:** the `overwrite` writer is the one path both doors take, so `WriteGovernanceOverwriteRebases.cfg`
    is this defect's config as it is C2's. **Fix: one path for an Overwrite of a table.** The insert door's overwrite
    lands through `dataplane._overwrite_stamped`, as the create door's does: a stamped commit on the ref the request
    names (its branch included, the fragments written onto that ref's handle), judged against the version it lands on
    and the contract in force there, finalized at the slot after its plan. Its run rule is judged as the create door's
    Overwrite's is, by the write judge (`insert` is a stamping door) and on the table standing in its namespace (A3). It
    also keeps the ref's schema, as the spec's InsertIntoTable `mode` states ("remove all data in the table and then
    insert data to it", `lance_docs/ns_catalog/spec.yaml`), which is why the door asks only the writer rung: a plan on a
    version whose schema is not the one its rows were aligned to (a column added, dropped or retyped, a field's or the
    schema's metadata changed) is refused 409 (code 14) before it writes (KeepsSchema), and a retry aligns afresh. The
    stamped Overwrite also writes its files at the table's own file version rather than the create pin: measured on
    pylance 13.0.0, `write_fragments(dataset, mode="overwrite", data_storage_version="2.2")` over a 2.0 table writes 2.2
    files under a manifest still naming 2.0, a mixed table. `_land_rows` now lands appends only. A caller now sees: an
    insert overwrite racing another commit plans again on the new head; one racing a schema change or a contract its
    rows break answers 409 (code 14, code 19 for the contract).
    **The other landings through Lance's own commit, swept:** `_land_rows` (an append, which keeps the schema of the
    version it lands on), `land_keyed` (the same append under an Idempotency-Key, and a fresh create's first version) and
    `_write_blob` (a create's first version; its `mode="overwrite"` is reached only by a caller passing no `overwriting`,
    which `table_create` always passes for a table it overwrites, an Overwrite of a name it found no table under being
    downgraded to a Create, so no door reaches it). Every other door commits through `commit_stamped` or a stock
    client's `version/create`. **The other placements, swept:** create, register and rename in judge under
    `write_judge.placing`; a declare places a table holding no data, whose first version `version/create` judges with the
    table standing (A3); a clone is a register; a namespace create places no data.

## Lock order

Every take of a namespace fence or a table's claim fence refuses (409, code 14 or 19) rather than waits
(`lakehouse.fences.HolderFence`), so no request waits on another's hold and no cycle of waits can form; the model's
`EveryActorMoves` holds in every reachable state of `WriteGovernanceRule.cfg` and `WriteGovernanceRedeclare.cfg`. The
order still decides which of two requests is refused, and every request holding several takes them in one order:
namespace fences from the shallowest down, then a table's claim fence, then, for a trash record it files, its
warehouse's add fence (`lakehouse.trash.put`, which only a warehouse delete excludes). An undrop takes its parent's add
fence before its own namespace's and before the table's claim, so it keeps that order.

| Request | Takes, in order |
|---|---|
| table undrop | the parent's add, then the table's claim (`fence_kept`), then the parent's add again for the backend's register (adds share) |
| namespace undrop of `y` under `x` | `x`'s add (`placing_back`), `y`'s add (its own, ADR 0233), each namespace's parent's add as it is made again, then each table's parent's add and its claim (`restore_kept_table`) |
| create, register, rename in | the destination's add (`placing`), then the claim (a create's declaration fence) |
| namespace Overwrite | `x`'s drop, then `x`'s parent's add for its re-create |
| cascade drop of `x` | `x`'s drop, then each table's claim in turn with its warehouse's add for the record it files, then each child's drop, deepest first |

A namespace undrop and a cascade of its parent meet at the parent's fence first, an add against a drop, and one of them
is refused before it holds anything else; a namespace undrop and the Overwrite of its parent likewise.

## What the model assumes

- **A1.** A version slot is filled put-if-not-exists (`CommitPath.tla` A1, held by the backend's `create_table_version`).
- **A2.** A record's conditional write replaces it only under the ETag of the read it judged (`lakehouse.records`).
- **A3.** A namespace's rule changes only at its create, and a drop lands only once the namespace is empty (ADR 0211
  §2; the backend's Restrict refuses a drop of a namespace a table stands in). The Overwrite, and the drop door followed
  by a create door, are modelled in their own steps.
- **A4.** An add and a drop of one namespace never hold its fence together (`lakehouse.namespace_fences`, `Fence.tla`).

## The doors

Every way a version lands or a table's schema, name or location changes, where it is judged, and against what:

| Door | Contract: where judged, against what | Run rule: where judged |
|---|---|---|
| `/create` mode=Create of a new name | none: a new table holds no contract (one is set on a table that exists); a name made again after a drop is a new uuid | the write judge, then `write_judge.placing` under the parent's add fence, refusing a parent that does not stand (run named in the Context) |
| `/create` mode=ExistOk | writes nothing | as Create; nothing placed when the table stands |
| `/create` mode=Overwrite | `create_table` on its first read, then `commit_stamped` (`_overwrite_stamped`) against the version it lands on | the write judge; the table stands in its namespace (A3) |
| `/insert` mode=overwrite | `_overwrite_stamped`, as the create door's Overwrite: `commit_stamped` against the version it lands on, on the ref it names; refused 409 (code 14) when that version's schema is not the one its rows were aligned to, since it keeps the ref's schema (U2) | the write judge: run named; the table stands in its namespace (A3) |
| `/insert` (append), `/merge_insert` | `commit_stamped`; an append through Lance's own commit (`land_keyed`, `_land_rows`) adds fragments and keeps the schema of the version it lands on | the write judge: run named |
| `/update`, `/delete`, `add_columns`, `alter_columns`, `drop_columns`, `update_field_metadata`, `schema_metadata/update`, `drop_index` | `commit_stamped`, against the version it lands on | `update`, `add_columns` refused under the rule; the others add no data |
| `backfill_column` | not judged: the backend answers 406 (unserved) | not judged, likewise |
| `/restore` (to any version, one older than the contract included) | `commit_stamped` (a Restore): refused when the restored schema breaks a contracted column the head keeps, and only then (the set's version carries no key a restore rewinds, §9); a stock client's restore likewise at `version/create` | adds no data the table did not hold under the same rule (A3) |
| `version/create` (a stock client, a compaction) | `managed_versions.judge` against the version it follows, finalized exactly at the next slot (ADR 0157) | `judge_staged_run`: `rask.run_id` and `rask.inputs` read off the staged manifest |
| `declare` + a stock commit | `version/create`; a table's first version holds no contract | `version/create` reads the rule then; the declared table already stands in its namespace |
| `register` (and so a shallow or deep clone, which is registered) | none: a registration is a new table, a new uuid; a source's contract does not follow its clone | refused under the rule at the write judge and again in `placing`, which refuses a namespace that does not stand |
| `rename` | commits nothing; the contract follows the uuid | the write judge and `placing` under the destination's add fence (`moved_from`), refusing a destination that does not stand |
| branch create | not judged: a branch cut from a version older than the contract carries that version's violation (ADR 0212 §4); commits on it are judged as main's | the table's namespace |
| a branch's commits | as main's, against the branch's head | as main's |
| tag create, update, delete | no version, no schema | no data |
| soft drop, undrop (of a table, or of a namespace and the tables beneath it) | the record is kept through the trash (`OBJECT_RECORD_PREFIXES` end at purge), so an undrop keeps its contract | `trash_doors.placing_back` under the parent's add fence, refusing a parent that does not stand, and inside it `require_its_own_parent`: the undrop lands only in the namespace it was dropped from, by uuid (U1) |
| purge, deregister | the record ends with the table | — |
| the contract's set | its first read (`_require_described`), its record kept in flight, its barrier on every ref, then its own record marked in force (`contracts.confirm`) | — |
| any commit while a set is in flight | held to the in-flight contract and to the one in force it replaced (`ContractRecord.binding`) | — |
| the namespace Overwrite | — | holds the namespace's drop fence from its emptiness check through its re-create |
| the contract's delete | unconditional, idempotent; removes a record in flight too, whose set then answers 409 | — |

The contract follows the table's **uuid**: a rename keeps it, an undrop keeps it, a drop and a create of the name, a
register of the same bytes or a clone hold none.

## Not modelled

- **Column types, nullability, nested columns, case and the primary key.** A contracted column held at another type,
  nullable where the contract says required, renamed or dropped is one missing column here, as `violations` reads it;
  a nested column is named by its path and judged child by child (`contracts._field_at`), case included, since Lance
  field names are case-sensitive. A violation is a set difference whatever its kind, so no kind can hide one.
- **The contract's own evolution** (`require_evolves`): a check on the document at the door, not on a commit.
- **An unreadable record**: fail closed, every commit refused (`contracts.held` raises), which only refuses more.
- **A branch cut from a version older than the contract**: ADR 0212 §4 makes its violation not the next writer's;
  `require_kept` refuses anything a commit on it adds. The set door verifies every branch standing when it runs.
- **The write judge's rung, a run's inputs and a stock client's `rask.*` properties.** A stock client names its own
  `rask.run_id` (the catalog checks none) and `rask.inputs` (each judged `can_read_data` and recorded by uuid); its
  `rask.author` must be its caller (`run_context.require_caller_authored`). The run rule asks only that a run and inputs
  be named.
- **A write into a table standing in a namespace**: its namespace cannot change its rule while the table stands there
  (A3), so its judgement at the router is the judgement it lands under.
- **The lifecycle doors' own fences** (`TableLifecycle.tla`, `Cascade.tla`) and the commit path's keys and turns
  (`CommitPath.tla`).

## Consequences

- A contract set costs one commit per ref, each announced; a table with many branches pays one each. A set of the
  document already standing lands them too, so a reconciler applying contracts from git compares the one `describe`
  answers (as it was set) and sets only a changed one. Skipping the commits for an identical document was not taken: the
  standing record may be a set still in flight, which a later refusal takes back, and the skip would then answer 200 for
  a table its commits never ordered.
- A create holds its namespace's add fence for its whole write; a drop or Overwrite of that namespace answers 409
  meanwhile and is retried.
- A table's Overwrite under contention plans again rather than landing over another commit.
- A contract set costs one more read and conditional write of its record (the mark), and a commit made while a set
  runs is held to two contracts.
- A namespace Overwrite holds its namespace's drop fence while it forgets the dropped namespace's grants; a placement
  into the namespace meanwhile answers 409 and is retried.
- An undrop holds its parent namespace's add fence through its register, and a namespace undrop for its whole run; a
  drop or Overwrite of that parent answers 409 meanwhile and is retried. Each namespace and table an undrop places back
  costs a fence take and the describes of the namespaces above it.
- An insert overwrite under contention plans again rather than landing over another commit, and one racing a schema
  change answers 409 for the caller to align its rows again.
- **Four facts.** GitOps: nothing new is live-only; a contract applied from git converges as before. Cloud native: no
  new workload. Lance-namespace and table idiomatic: the set is ordered by Lance's own put-if-not-exists of the next
  version, its own Lance operation (an `UpdateConfig` that updates nothing), which leaves nothing a restore or a stock client must carry. Zero trust: no writer is trusted to have read the latest
  contract or rule; the store's conditional writes order every one.

## Alternatives not taken

- **A second read after the record, or a wait for in-flight commits.** A commit lands after any read; nothing bounds
  how long a commit runs between its contract read and its put.
- **A fence every commit takes beside the contract set** (as the history fence orders ref writes with a reclaim). It
  closes the race too and holds the Overwrite without changing its commit, but costs every commit two store writes for an
  event (a contract set) that happens at owner frequency; the barrier costs only the set.
- **Keeping the contract in the table's own schema metadata, so the version a commit plans on carries it.** Ordered by
  the same slot, and branches and clones would carry it; but a restore to a version older than the contract would
  restore its absence, and a stock client could rewrite it, so every door would need a rule for it. The record stays
  authoritative.
- **A visible stamp on the barrier's version** (`rask.contract` in the schema metadata, the first form of C1's fix).
  Every later restore past it, stock or at the door, rewound it and was refused (W3), and every door would have had to
  exempt a reserved key nothing may trust. The barrier needs the slot, not a key.
- **Closing W1 by the placement's refusal alone.** It closes both the Overwrite's race and the two-door one, and the
  model holds with it alone; the Overwrite's hold is kept beside it so that a missing namespace about to be remade under
  a rule is never visible to a placer at all, and a placement into it answers the retryable 409 rather than a 404 for
  a namespace that stands again a moment later.
- **A commit held to the in-flight contract alone while a set runs** (the reading before W2).
  `WriteGovernanceJudgedLoose.cfg` fails: a set loosening the contract lets a commit through that its refusal then makes
  a violation of the contract it puts back.
- **The insert door's Overwrite re-aligning its rows to the version it plans on.** A column added since would be
  missing from the rows and refused 400 by the alignment, and one dropped would be extra; the caller's rows were
  written for the schema it read, so the door answers the race 409 and the caller's retry aligns afresh.
- **Holding only each placement's own parent in a namespace undrop.** It is enough for the run rule (once `y` stands in
  `x`, `x` is not empty and no Overwrite drops it), but it takes `y`'s fence before `x`'s, the opposite of a cascade of
  `x`; the parent's hold over the whole run keeps one order.
- **Refusing a namespace Overwrite that declares the rule.** It is how the rule is dropped and declared again (ADR 0211
  §2); the race is the placement's, which now reads the rule under the fence it adds under.
