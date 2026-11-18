# 0244. Write governance is modelled in TLA+, and a rule a write was judged under is the rule it lands under (2026-11-18)

**Status:** accepted; the model of the last part of the write path no model described. Four defects were found, each
reproduced against the real catalog over HTTP and fixed model first (owner's standing ruling: fix now). It amends
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
     a contract names them, its maker, and the contract in force when it landed), the contract record (`rec`, `recBy`),
     each door's answer, the doors whose write a rollback replaced (`lost`), each writer's step, plan, planned schema,
     read contract and lost races, each set's step, replaced record, refs still to verify and its barrier's plan. The run
     rule seam: whether namespace `x` stands, whether it requires a run, its incarnation (its uuid), the placers holding
     its add fence, the tables standing in it that hold data no run produced, each placer's step and the incarnation an
     undrop's table was dropped from, and the namespace Overwrite's step.
   - **Actions.** `WPlan` (a commit reads the ref's head), `WJudge` (reads the contract after its plan, as
     `commit_stamped` and `version/create` do, and refuses what it introduces), `WLand` (an exact commit fills the slot
     after its plan or loses and plans again; Lance's own Overwrite lands over whatever committed since); `SFirst` (the
     set door's read of every ref, `_require_described`), `SKeep` (`contracts.keep`), `SSecond` (the second read the
     door made before this ADR) or `SBPlan`/`SBLand` (the barrier, §2), each refusal taking the record back (`Rollback`,
     `contracts.take_back`); `Delete` (`contracts.forget`); `Branch` (a branch from main's head). `PJudge` (the write judge
     reads `x`'s rule; under guard PlacedUnderHold while holding `x`'s add fence), `PAdd` (the backend's add under the
     fence), `PUndrop`; `ODrop` (the namespace Overwrite drops `x` once empty and no add holds its fence) and `OCreate`
     (makes it afresh with a new uuid, declaring the rule).
   - **Invariants.** `ContractKept`: no version a writer landed introduces a violation of the contract in force when
     it landed (a contract is in force once the set that wrote it answered 200; a commit landing while a set is in
     flight is that set's to judge). `AnsweredSetStands`: a set answered 200 is never replaced by another set's
     rollback. `RunRuleHeld`: no table holding data no run produced stands in a namespace that requires a run. `TypeOK`.
   - **What it proves** (`scripts/tla_check.sh WriteGovernance`, two TLC workers on four cores shared with other
     agents, 2026-11-18), every gate config checking every invariant:

     | Config | Actors | Distinct states | Time |
     |---|---|---|---|
     | `WriteGovernance.cfg` | a drop and an add (exact), an Overwrite dropping the column, one set; one replan | 9,766 | 1.6 s |
     | `WriteGovernanceBranch.cfg` | a branch made while a set runs, a drop on it, a restore of main | 1,187 | 1.3 s |
     | `WriteGovernanceSets.cfg` | two sets and a delete, a drop and a commit adding the column back | 167,765 | 4.2 s |
     | `WriteGovernanceWide.cfg` | the same with two lost races each | 213,305 | 4 s |
     | `WriteGovernanceRule.cfg` | a create, a register, a rename in and an undrop beside the Overwrite declaring the rule | 160 | 1.2 s |

2. **Guards**, each with the config that removes it and must fail, and the test that fails without it:

   | Guard | Where | Config (invariant) | Test |
   |---|---|---|---|
   | Judged: every commit reads the contract after its plan and refuses what it introduces | `dataplane.commit_stamped`, `managed_versions.judge` (`contracts.require_kept`) | `WriteGovernanceUnjudged.cfg` (ContractKept) | `test_a_tables_contract_is_held_on_every_commit_a_stock_client_or_a_door_makes` |
   | Barrier (new, C1): a set commits on every ref listed once its record stands, a stamped commit whose plan judges the ref's head against the contract | `endpoints/contracts.py` `_verify_on_every_ref` | `WriteGovernanceSecondRead.cfg` (ContractKept) | `test_a_commit_judged_before_its_contract_was_set_does_not_land_after_the_set_answered[door]`, `[stock]` |
   | OverwriteStamped (new, C2): a table's Overwrite is a stamped commit | `dataplane._overwrite_stamped` | `WriteGovernanceOverwriteRebases.cfg` (ContractKept) | `…[overwrite]` |
   | RollbackOwn (new, C3): a refused set takes back only its own record | `contracts.take_back` | `WriteGovernanceRollbackAny.cfg` (AnsweredSetStands) | `test_a_refused_set_takes_back_only_the_contract_it_wrote` |
   | PlacedUnderHold (new, R1): a door placing a table reads the namespace's rule under its add fence, held through the add | `write_judge.placing`, at `table_create.create_governed_table`, `tables.register_table`, `tables.rename_table` | `WriteGovernanceUnheld.cfg` (RunRuleHeld) | `test_a_table_placed_beside_the_overwrite_that_declares_the_rule_does_not_stand_under_it`: `[create]` (held inside the fence) fails without the hold; `[create-before-its-fence]`, `[register]`, `[rename]` (held before it) fail without the second read |
   | RenameJudged: a rename into a namespace requiring a run from one that does not is refused | `write_judge.judge`, `placing` | `WriteGovernanceRenameUnjudged.cfg` (RunRuleHeld) | `test_a_write_names_its_run_where_a_namespace_above_it_requires_one` and the rename case above |
   | RegisterJudged: a register under the rule is refused | `UNSTAMPABLE_DATA_DOORS` | `WriteGovernanceRegisterUnjudged.cfg` (RunRuleHeld) | the register case above |
   | UndropChecksParent: an undrop lands only in the namespace its table was dropped from, by its uuid | `endpoints/tables.py` undrop ("the namespace it was dropped from is gone") | `WriteGovernanceUndropUnchecked.cfg` (RunRuleHeld) | measured once, below (Candidates) |

3. **C1: a set beside a commit judged before it.** Trace: a commit plans on version N and reads the table's contract
   (none); the set's first read finds every ref meeting the contract, it writes the record, its second read finds every
   ref meeting it again, and it answers 200; the commit lands at N+1, dropping a contracted column. The table then holds,
   under a contract answered 200, a version that breaks it, and `require_kept` exempts that violation from every later
   commit, so the column stays unguarded until its owner mends it. Real in `endpoints/contracts.py` (the second read)
   and ADR 0212's own "Not taken". Reproduced: a commit held right after its contract read (`DoorTable.contract`),
   the set run whole (200), the commit released: on the base the door's `drop_columns`, a stock client's
   `drop_columns` through `version/create`, and an Overwrite each dropped `v`. **Fix: the set commits on every ref.**
   Once the record stands the door commits on main and on each branch it then lists a version that changes no column,
   stamping the schema metadata `rask.contract` = `<id>@<version>`, as a stamped commit whose plan judges the ref's head
   against the contract. A commit lands only at the slot after the version it planned on (A1), and reads the contract
   after that plan, so one that read before the record stood either landed first, and the set's plan judges it, or
   loses its slot to the set's version and reads the contract when it plans again. A branch made after the listing was
   made after the record stood, so every commit on it reads the contract. A caller now sees: the set lands one version
   per ref, each announced on lineage as a schema metadata update by the setter; the held commit answers 409 (code 19)
   for the contract instead of 200; and a set whose ref a commit made stop meeting the contract meanwhile answers 409
   (code 19) with the contract it replaced put back. With the barrier replaced by a second read the `[door]` and
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
   One corner stays: when B too is refused afterwards, it puts back what it replaced, A's refused contract, which then
   stands as a stricter contract than any answered 200 (every commit is judged against it, fail closed) until its owner
   sets or deletes it; the model's `InForce` reads it as none, and no invariant is weakened by it.
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
   its whole write, so a drop of its namespace answers 409 for that long.

## What the model assumes

- **A1.** A version slot is filled put-if-not-exists (`CommitPath.tla` A1, held by the backend's `create_table_version`).
- **A2.** A record's conditional write replaces it only under the ETag of the read it judged (`lakehouse.records`).
- **A3.** A namespace's rule changes only at its create, and its Overwrite makes it afresh only once it is empty and
  dropped (ADR 0211 §2; the backend's Restrict refuses a drop of a namespace a table stands in).
- **A4.** An add and a drop of one namespace never hold its fence together (`lakehouse.namespace_fences`, `Fence.tla`).

## The doors

Every way a version lands or a table's schema, name or location changes, where it is judged, and against what:

| Door | Contract: where judged, against what | Run rule: where judged |
|---|---|---|
| `/create` mode=Create of a new name | none: a new table holds no contract (one is set on a table that exists); a name made again after a drop is a new uuid | the write judge, then `write_judge.placing` under the parent's add fence (run named in the Context) |
| `/create` mode=ExistOk | writes nothing | as Create; nothing placed when the table stands |
| `/create` mode=Overwrite | `create_table` on its first read, then `commit_stamped` against the version it lands on | the write judge; the table stands in its namespace (A3) |
| `/insert`, `/merge_insert` | `commit_stamped`; an append through Lance's own commit (`land_keyed`, `_land_rows`) adds fragments and keeps the schema of the version it lands on | the write judge: run named |
| `/update`, `/delete`, `add_columns`, `alter_columns`, `drop_columns`, `update_field_metadata`, `schema_metadata/update`, `drop_index` | `commit_stamped`, against the version it lands on | `update`, `add_columns` refused under the rule; the others add no data |
| `backfill_column` | not judged: the backend answers 406 (unserved) | not judged, likewise |
| `/restore` (to any version, one older than the contract included) | `commit_stamped` (a Restore): refused when the restored schema drops a contracted column the head keeps | adds no data the table did not hold under the same rule (A3) |
| `version/create` (a stock client, a compaction) | `managed_versions.judge` against the version it follows, finalized exactly at the next slot (ADR 0157) | `judge_staged_run`: `rask.run_id` and `rask.inputs` read off the staged manifest |
| `declare` + a stock commit | `version/create`; a table's first version holds no contract | `version/create` reads the rule then; the declared table already stands in its namespace |
| `register` (and so a shallow or deep clone, which is registered) | none: a registration is a new table, a new uuid; a source's contract does not follow its clone | refused under the rule at the write judge and again in `placing` |
| `rename` | commits nothing; the contract follows the uuid | the write judge and `placing` under the destination's add fence (`moved_from`) |
| branch create | not judged: a branch cut from a version older than the contract carries that version's violation (ADR 0212 §4); commits on it are judged as main's | the table's namespace |
| a branch's commits | as main's, against the branch's head | as main's |
| tag create, update, delete | no version, no schema | no data |
| soft drop, undrop | the record is kept through the trash (`OBJECT_RECORD_PREFIXES` end at purge), so an undrop keeps its contract | the undrop lands only in the namespace its table was dropped from, by uuid |
| purge, deregister | the record ends with the table | — |
| the contract's set | its first read (`_require_described`), then its barrier on every ref | — |
| the contract's delete | unconditional, idempotent | — |

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
- **Four facts.** GitOps: nothing new is live-only; a contract applied from git converges as before. Cloud native: no
  new workload. Lance-namespace and table idiomatic: the set is ordered by Lance's own put-if-not-exists of the next
  version, and the stamp is the table's own schema metadata. Zero trust: no writer is trusted to have read the latest
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
  authoritative and the stamp is provenance.
- **Refusing a namespace Overwrite that declares the rule.** It is how the rule is dropped and declared again (ADR 0211
  §2); the race is the placement's, which now reads the rule under the fence it adds under.
