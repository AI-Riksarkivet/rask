# 0245. Projects and warehouses are named in OpenFGA by their creation, and the fence machinery is deleted (2026-11-18)

**Status:** accepted. Supersedes ADR 0241's container part (§1 container, §5 F1 and F3's fixes, §7, §8 B1 to B5, §9's G21,
§10's G23); keeps its table part, its converge part (G5, G13) and its bucket half (G19, G22, G24, Q3, Q5, Q6). The
model is rewritten first (`specs/catalog/Grants.tla`), and the code follows it.

## Context

A table and a namespace are named in OpenFGA by the uuid their create mints (ADR 0173), so a tuple written for one table
never applies to another made under its name. A project and a warehouse were named by their id (`project:<id>`), which a
delete frees for another tenant. ADR 0241 kept the id and built, in four review rounds, a machinery to stop a grant, an
edge or a revoke decided on one creation from reaching the next: an incarnation per record and its marker and token
tuples, a ticket and an index tuple per write, a door that asks its gate, opens a fence and asks again, a delete that
retires and drains, a ticket sweep on a timing bound, a convergence that bears unborn incarnations and stamps them born,
a drift repair with a token of its own, and a `project_incarnation` on four record kinds. Every review round found
reproduced blocking defects in that machinery: 12 of the 29 findings landed in it.

§5 F1 rejected naming the objects by a uuid: it "would move every client, declaration and the Rust port's
`warehouse:<id>`". Measured on this tree (2026-11-18): no client names these objects (the repository hosts no client;
the console's one object string, the create dialog's extra admin `project:${project}` through the raw tuple door, is
replaced by the project's members door, review T5, so the console builds none); the Rust port reads lineage alone and names no project or warehouse object (`grep -rn 'project:\|warehouse:'
rust/` finds none); `catalog.declared` names ids, which the converge resolves. The estate is pre-production, so no
backward compatibility is owed. The rejection does not hold.

## Decision

1. **Identity.** Each create of a project or a warehouse mints a uuid (`lakehouse.governed.tenancy.new_uuid`, 32
   lowercase hex digits). Its registry record names it (`uuid`), and OpenFGA names the object `project:<uuid>` /
   `warehouse:<uuid>`. The id stays the address at every door, in the console, in `catalog.declared` and in events.
   The record's former `incarnation` becomes `uuid`, and every record belonging to a project (a warehouse's, a role's, a
   bucket's claim, a project's maintenance policy) names `project_uuid`, a required field of its Pydantic model, so a
   record naming none does not parse. A create refuses an id shaped like a uuid, so an id and a uuid are never one string.
2. **One resolver, one builder, and a Write that refuses an id.** `catalog.services.tenancy.resolve` / `resolved` turns
   an id into its LIVE record (one registry read; the estate's own warehouse resolves to its configured object), and
   `lakehouse.governed.tenancy.object_of` is the one builder of a `project:` or `warehouse:` object: a ruff banned-api
   rule admits it only at the resolver and at maintenance's rebuild of a tenancy edge from a record. Every OpenFGA Write
   (`fga.write_tuples`, `delete_tuples`, `commit_unless_changed`) asks `tenancy.require_named_by_creation` first: a tuple
   naming a project or a warehouse by anything a create could take as an id is refused (500, code 18) before it reaches
   the store, so a site that built an object from an id fails closed and never writes a tuple the next tenant would hold.
3. **Record states and the create's order.** A record is `creating`, `live` or `deleting` (`state`); a door resolves
   only `live`. A create writes its record first, `creating`, naming its uuid and the admin (a project) or owner (a
   warehouse) it seeds; a warehouse's create then resolves its project again and, finding another creation or none, ends
   its record and answers 404; then the seed on the uuid; then the record flips `live`, conditional on its ETag and
   uuid. Nothing is written in OpenFGA before the record stands. The converge tick finishes a `creating` project from
   what it names, and a `creating` warehouse whose create no longer runs (older than the request deadline plus five
   minutes) under its project's live creation and holding its claim, its catalog row ensured first; it ends one whose
   project's creation is not live.
4. **Doors.** A door resolves the id to the live uuid, asks its gate on that object, writes there in ONE Write, and
   resolves the id again (`tenancy.require_still`): an id no longer naming that uuid live answers the deleted object's
   404, and what was written stands on a dead object nothing resolves to. No ticket, no second ask, no fence; grants
   never refuse each other. An absent id is gated as an object nobody holds (`fga_deps.require_on_tenant`,
   `tenancy.nowhere`), so a refusal discloses nothing about which ids exist. A namespace created directly in a warehouse
   resolves the warehouse again once seeded (`fga_deps.seed_ownership`); a warehouse deleted meanwhile has the namespace
   taken back and answers 404.
5. **Delete.** Mark `deleting` (doors stop resolving it), read the children after the mark (a project's warehouses in
   every state: one still `creating` counts, and the delete refuses 409 while one stands; a warehouse's namespaces,
   cascaded), revoke every tuple on the uuid and every tuple naming it (`fga.revoke_object_tuples(naming=)`, the ONE way
   an object is revoked), end the record (`tenancy.end`): the record is stamped `ended` under the ETag of a read that
   found the delete's own mark and removed by that writer alone, which frees the id, and the creation is kept under
   `_ended/<kind>/<uuid>.json`. The checks, the mark, the point of no return and what each refusal leaves are set by the
   review's T3 (§ Found by review). The converge tick revokes what stands
   on each ended creation, and forgets it once a sweep past the bound found nothing: a write that raced the delete left
   tuples on a dead object, which authorize nothing; the sweep is hygiene.
6. **Registry layout.** Records stay keyed by id (`_projects/<id>.json`, `_warehouses/<id>.json`): "which uuid is live
   for this id" is one read of that key, and a create is one conditional write (iff absent). Every other change of a
   record is a write under the ETag of a read that found its creation (`control_records.change_creation`), and its
   removal the stamp `ended` written so, then the file removed by the writer whose stamp landed; a stamp whose writer
   died is removed by the converge tick past its bound (`remove_stale_ended`), restamped first so two ticks remove it once. An ended record is not
   kept under the id (it is removed, and its creation kept by uuid under `_ended/`), so it never blocks its id: G23
   (KeepsEndedKey) is unnecessary and deleted. The records-by-uuid-with-a-pointer layout the analysis proposed costs two
   reads for the same answer. G24 (PurgeByBucket) stays: a claim a dead creation took and no record names is reached by
   its bucket.
7. **What stays, unchanged in behaviour:** the bucket half (the claim, `written_by_others`, the tick's removal of what a
   create wrote, `require_claim_held`, the purge under its `purging` stamp, the two-row purge audit, the estate owner's
   purge by bucket): a bucket name is a physical resource every creation shares. ADR 0213's last-admin guard, now the
   token `revision:<n>` on the project's creation (`fga.next_token`; none standing, the removal writes `revision:0`,
   which a concurrent writer of the same tuple refuses). `GitRevokesOnlyItsOwn` (the declaration's ledger and evidence;
   the ledger names the uuid of the creation a grant was written on). Table and namespace authorization.
8. **One crossing by design.** A declared grant names an id. The converge writes it on the creation the id names now, so a
   project made again under a declared id is granted it again: git declared it. The ledger's record of it is about one
   creation: one naming a creation the grant is not held on is forgotten and the grant declared afresh (review T1). This is the one place a tuple crosses
   creations, and the model's `GrantsDeclared` checks it: the declarer writes on the creation it resolved, decided for
   that creation, so `NoLeakAcrossIncarnations` holds.

### Deleted

Tickets and their index (`type fence`, `fence#ticket`, `fence#open`, `fga.fenced`, `Fence`, `commit_fenced`,
`fences_stand`, `sweep_tickets`, `read_tickets`, `ticket_index`, `delete_absent_ignored`, `incarnations.ticket_bound`);
the marker, token-per-grant, `bear`, `is_born`, `read_incarnation`, `standing_incarnation`, `Incarnation`,
`retire_tokens`, `end_incarnation` and the drain; the repair token and the incarnated branch of `fga.revoke_ghost`;
`fga_deps.door_fence`, `parent_fence`, `door_fence_of`, `require_fence`, `DoorFence`, `_bear_or_assert`, the second asks;
`SettlingError` and its `Retry-After`; `catalog.services.incarnations` (`require_incarnation`,
`require_project_incarnation`, `establishing`, the born stamps, the convergence birth); `control_records.INCARNATION`,
`PROJECT_INCARNATION`, `BORN`, `new_incarnation`; `warehouse_records.PROJECT_INCARNATION`; the warehouse's ended record
(`ENDED_AT`, `ENDED_REASON`, `end_warehouse_record`, `mark_purging`, `require_key_free`, `EndedRecordHoldsItsClaim`) and
the estate owner's purge of a dead tenant's born warehouse by its record (a warehouse can no longer go live under a
creation of its project that ended, so there is none). Tests of deleted mechanisms went with them:
`test_a_fence_reads_nothing_that_grows_with_its_object.py`, `test_a_fenced_door_asks_again_on_the_incarnation_its_fence_read.py`,
the ticket cases of the refused-caller test, the drain and retire cases of the deleted-project test, the born-orphan and
ended-key cases of the dead-tenant test, the unborn-incarnation case of the converge test.

## Found by review, and fixed (2026-11-18)

An independent review of this decision as first shipped found five blocking defects (T1 to T5, four reproduced against
the catalog with a real OpenFGA, loopback Dex and moto S3) and four smaller ones (T6 to T9), each in this decision's own
code. All are fixed in one change, the model first where it can see the defect; each guard was removed in the code with
its test run and the file restored (measured 2026-11-18), and each
must-fail config fails without it.

| Problem | Guard | Where (file, function) | Must-fail config | Test | Guard removed |
| --- | --- | --- | --- | --- | --- |
| T1: a grant git declares was never written on a project or warehouse made again under its id: the ledger's settled record of creation A blocked the intent on B, and the tick reported `failed=[]` | U11 LedgerPerCreation: a settled record whose grant is not held on the live creation is forgotten (marked, then removed by the one forgetting whose mark landed) and the grant declared afresh in the same tick; a grant the tick neither holds nor writes raises and is named in `failed` as `<object>: <why>` | `services/catalog/src/catalog/api/declaration_converge.py` `_Converger._grant`; `services/catalog/src/catalog/services/declaration.py` `forget_settled`; `_attempt` | `GrantsDeclaredStale`, `GrantsConvergeStale` (temporal `DeclaredOnLive`, `DeclaredHeld`) | `tests/integration/test_a_delete_ends_only_the_creation_it_marked.py::test_a_declared_grant_is_held_on_the_object_made_again_under_its_id` (warehouse, project) | 2 failed |
| T2: a retried delete of creation A, held past A's end, destroyed B made again under the id (its record, its roles), and a held protection change landed on B | U12 EndsOwnCreation, ONE conditional way to change a tenancy record: `control_records.change_creation` (a write under the ETag of a read that found the uuid; the end is the stamp `ended` written that way, then the file removed by the one writer whose stamp landed, `end_record`); the delete's mark carries its run (`delete_run`) and every step past it reads the record again (`tenancy.require_own`); companions are selected by the creation (roles by `project_uuid`, label definitions by `project_uuid`, the trash by `warehouse_uuid`, which `trash.put` stamps) | `services/catalog/src/catalog/services/control_records.py` `change_creation`, `end_record`, `remove_stale_ended`; `services/catalog/src/catalog/services/tenancy.py` `DeleteRun`, `mark`, `require_own`, `end`; `roles.every_role`; `object_labels.end_definitions`; `warehouses.trash_owned_by`; `projects.set_project_protection`, `warehouses.set_warehouse_protection` | `GrantsDeleteTwiceById` (`DeleteEndsOnlyItsOwn`) | `test_a_projects_delete_held_across_its_end_ends_nothing_of_the_project_made_again`; `test_a_protection_change_held_across_its_objects_end_lands_on_no_object_made_again` (project, warehouse); `services/catalog/tests/test_a_warehouse_delete_ends_only_its_own_trash.py` | roles by id and no re-read: 1 failed; end unconditional and no re-read: 1 failed; protection by id: 2 failed; the re-read alone: none failed (masked by the selection by creation and the conditional end, which each hold it) |
| T3: a cascade refused by a protected namespace left the warehouse `deleting` for ever, answered absent at its doors while its names still took writes | U13 RevertsRefusal, the delete's point of no return: every check that can refuse with nothing changed runs before the mark (protection, emptiness, the purge's guards, the namespace fence, the subtree's protection, the clones outside); the mark is written under the ETag of the read those checks judged; a refusal after it, before the stamp `no_return`, puts the record back live (`tenancy.put_back`), and so does a guard of the same run refusing before any step landed; past the stamp the answer is `partial` (503) and the same delete asked again finishes it; a warehouse marked `deleting` refuses every door changing what lies beneath it (409, code 14) | `warehouses.delete_warehouse`, `projects.delete_project`; `tenancy.pass_no_return`, `put_back`; `services/catalog/src/catalog/api/dependencies.py` `require_active_warehouse`; `fga_deps._changes` | `GrantsRefusalStuck` (`RefusedDeleteLeavesLive`) | `test_a_cascade_refused_by_a_protected_namespace_leaves_its_warehouse_live`; `test_a_projects_delete_reads_its_warehouses_again_after_its_mark` (a refusal after the mark that puts back); `test_nothing_is_written_beneath_a_warehouse_its_delete_has_marked`; `test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py::test_a_cascade_refused_before_anything_landed_answers_its_refusal` | checks after the mark: 1 failed; no revert: 2 failed; writes admitted: 1 failed |
| T4: the members door and the warehouse access doors resolved before their gate, so a caller holding nothing was told 403 for an id that stands and 404 for one that does not | ONE gate for every door on a tenant's id: `fga_deps.on_tenant` resolves, asks the door's gate on the creation or on the object nobody holds, and only a caller the gate admits learns the id is missing; every such door goes through it (`require_on_tenant`, the members, access, roles, labels, project and warehouse create doors) | `services/catalog/src/catalog/api/fga_deps.py` `on_tenant`, `require_on_tenant`; `access._grant_gate`; `members.grant_member`, `revoke_member` | (U3 and G14 hold the gate's order; the oracle is a disclosure, not a state the model holds) | `services/catalog/tests/test_a_door_the_router_leaves_open_decides_for_itself.py::test_a_tenancy_door_answers_a_caller_it_grants_nothing_alike_for_an_id_no_object_holds` (every tenancy door the router serves) | the members door resolving first: 1 failed |
| T5: the estate admin's tuple doors answered 500 (code 18) for `project:acme`, and its read tools gave false answers on it; the console's create dialog wrote `project:<id>` through the raw tuple door | The tools speak ids: `project:<id>` / `warehouse:<id>` resolve to the creation (live for a write or a hypothesis, standing for a question), a creation's uuid is taken where one stands, anything else shaped like a tenancy object is 400 (code 13); answers name the id beside the uuid (`name`, `user_name`, `names`); the console grants the extra admin at the project's members door (`grantProjectAdmin`) | `services/catalog/src/catalog/api/v1/endpoints/access_admin.py` `_held_object`, `_creation_of`, `_held_user`; `object_names.tenancy_ids`; `frontend/console/src/lib/ProjectCreateDialog.svelte`, `remote/warehouses.api.ts` | — | `services/catalog/tests/test_the_estate_admins_access_doors_take_an_object_by_name.py` (project by id, tuples by id); `frontend/console/src/lib/remote/warehouses.api.test.ts` | ids taken as written: 5 errored (the fixture's own write refused 500) |
| T6: the role doors (assign, unassign, delete) answered 200 for a change on a project deleted while they ran | Every door writing a tuple on a tenancy creation resolves the id again after its Write through one helper, `tenancy.require_still` (the role doors, a role's create after its edge, the members, access and managed-access doors) | `roles._assign`, `_unassign`, `delete_role`, `_mint_role` | `GrantsAnswersUnread` | `test_a_door_the_router_leaves_open_decides_for_itself.py::test_a_tenancy_door_whose_creation_ends_under_its_write_answers_the_deleted_object` (every writing door; a roster test fails a door in neither roster) | the role assignment's: 1 failed; managed access's: 1 failed |
| T7: U7's after-mark child read was held by the model alone | — (the guard stands; a test now reaches it) | `projects.delete_project` | `GrantsCreatingUncounted` | `test_a_projects_delete_reads_its_warehouses_again_after_its_mark` | 1 failed (the delete answered 200) |
| T8: the sweep's coverage was a hand-kept list | Derived from the model's type restrictions | `packages/lakehouse/src/lakehouse/governed/fga.py` `types_naming`; `fga_deps.naming` | — | `test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py::test_a_projects_end_revokes_a_tuple_of_every_type_the_model_lets_name_it` (compares with the DSL) | a hand list without `role`: 1 failed |
| T9: a create that lost the race for its id seeded and announced on the uuid it minted, which named no record | A mint refused by the store answers 409 (code 2), seeding and announcing nothing; the announcement is filed once the record is written | `projects._write_project_record`, `establish_project`; `warehouses._write_record` | — | `test_a_project_create_that_lost_the_race_for_its_id_made_nothing`; `tests/integration/test_warehouses.py::test_a_create_that_lost_the_race_for_its_id_made_nothing` | converging on the winner: 1 failed |

**The delete's order** (decision 5 as it now stands). Project: authorization (404 for a caller who may not delete it) →
one read of the record → protection and emptiness judged on it (refusing with nothing changed) → the mark under that
read's ETag (`deleting`, `deleting_by`, `delete_run`; 409 where the record changed since) → the warehouses read again
(one found: put back live, 409) → the point of no return (`no_return`) → revoke its creation's tuples and those naming
it → its roles, its policy, its label definitions, each of this creation and each followed by `require_own` → the end
(`ended`, `_ended/`, the record removed). Warehouse: authorization → one read → protection, the namespaces listed, the
purge's guards, the estate store → the namespace fence (409 while held) → the subtree's protection, the trash of its
creation, the clones outside → the mark → the namespaces read again (a late one: put back and 409 without `cascade`, or
held to the same checks with it) → the point of no return → the purge's claim (given back where a guard refuses before
any step landed) → the trash, the cascade, the orphans, the revoke, each followed by `require_own` → the bytes → the
fence confirmed → the end → the claim released. "Nothing irreversible yet" ends at `no_return`; from there the delete
finishes, or answers `partial` (503) and the same delete asked again finishes it. A delete asked again over a mark takes
it over (`delete_run`), and the earlier run, reading its mark gone, stops (409), or, where the id names another
creation, answers 404.

## The guarantees, the guard holding each, and the configs

Each guard is removed in a config that must fail. The guards this change introduced (U1 to U10) were also removed
in the code, one at a time, with the named test run and the file restored (measured 2026-11-18); the guards it kept
unchanged were not re-run. File and line are this commit's.

| Property | Guard | Where | Must-fail config | Test | Guard removed |
| --- | --- | --- | --- | --- | --- |
| `NoLeakAcrossIncarnations`, `NoNarrowAcrossIncarnations` | U1 ResolveAtDoor: write on the creation resolved, never on an id | `services/catalog/src/catalog/services/tenancy.py:86` (resolve); `packages/lakehouse/src/lakehouse/governed/fga.py:2181,2316,2516` (`require_named_by_creation` at every Write) | `GrantsById`, `GrantsRevokeById` | `tests/unit/test_fga_resilience.py::test_a_write_naming_a_project_or_warehouse_by_its_id_reaches_no_store`; `test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py` (5 doors) | seam removed: 2 failed |
| `GrantAnswersTruthfully` | U3 LiveOnly: a door resolves only `live` | `services/catalog/src/catalog/services/tenancy.py:81` | `GrantsResolvesDeleting` | `test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py::test_a_project_being_deleted_is_changed_at_none_of_its_doors` | 3 failed |
| `GrantAnswersTruthfully` | U4 ResolveAgain: resolve again after the Write | `services/catalog/src/catalog/api/v1/endpoints/members.py:158,220`; `tenancy.require_still` | `GrantsAnswersUnread` | `test_a_change_held_across_its_projects_delete_reaches_no_project_made_again_under_the_id` | members grant's removed: 2 failed |
| `OwnedWhileLive` | U2 SeedFirst: the record flips `live` only after its seed | `services/catalog/src/catalog/api/v1/endpoints/projects.py:328,336` | `GrantsLiveBeforeSeed` | `tests/integration/test_the_catalog_converges_what_git_declares.py::test_a_project_whose_create_died_before_its_seed_is_finished_by_the_catalog` | 2 failed |
| `ChildUnderLiveParent` | U5 ChildRecordFirst: a child's record, then its parent resolved again | `services/catalog/src/catalog/api/v1/endpoints/warehouses.py:283`; roles' `_mint_role` | `GrantsChildReadFirst` | `test_the_catalog_converges_what_git_declares.py::test_a_warehouse_made_across_its_projects_delete_lands_in_no_tenant_made_again_under_the_id` | 1 failed |
| `ChildUnderLiveParent` | U6 MarksFirst: the delete marks before it reads children; a namespace's create resolves its warehouse again once seeded | `projects.py:610` then `:685`; `warehouses.py:1307` then `:1309`; `services/catalog/src/catalog/api/fga_deps.py:2207` | `GrantsChildrenBeforeMark` | `test_a_namespace_made_in_a_warehouse_across_its_delete_lands_in_no_warehouse_made_again_under_the_id` | namespace re-resolve removed: 1 failed |
| `ChildUnderLiveParent` | U7 CountsCreating: the delete counts children in every state | `projects.py:601,685` (`warehouses.every_warehouse`) | `GrantsCreatingUncounted` | `tests/integration/test_a_dead_tenants_records_reach_no_live_tenant.py::test_a_warehouse_made_across_its_projects_delete_keeps_the_project_and_its_bucket` | both reads live-only: 2 failed; the after-mark read alone: none failed (see below) |
| `CreatingSettles` | U8 TickFinishes | `services/catalog/src/catalog/services/tenancy_converge.py:179` | `GrantsCreatingUnsettled` (temporal) | `test_a_project_whose_create_died_before_its_seed_is_finished_by_the_catalog` | 2 failed |
| `ChildUnderLiveParent` | U9 TickChecksParent: a creating child under a creation not live is ended | `tenancy_converge.py:192` | `GrantsTickFinishesOrphan` | `test_a_dead_tenants_records_reach_no_live_tenant.py::test_the_tick_ends_a_warehouse_a_dead_creation_left_creating_and_sweeps_what_was_written_on_an_ended_one` | 1 failed |
| `DeadObjectsSwept` | U10 TickSweeps | `tenancy_converge.py:212` | `GrantsDeadUnswept` (temporal) | the same test | 1 failed |
| `OnlyAuthorizedWrites` | G14 AuthorizeFirst: the gate asked before any write | every door: `members.py:127-129` | `GrantsIntruderWrites` | `test_a_caller_the_project_authorizes_for_nothing_writes_nothing_there.py` | not re-run (unchanged in shape from ADR 0241) |
| `GitRevokesOnlyItsOwn` | G5 OwnOnly, G13 Intents | `services/catalog/src/catalog/api/declaration_converge.py` | `GrantsConvergeAdopts`, `GrantsConvergeDies` | `test_the_catalog_converges_what_git_declares.py::test_a_grant_removed_from_git_is_revoked_and_one_made_at_a_door_stands` | not re-run (unchanged) |
| `ClaimFreed`, `BornLeftKept`, `PurgeTakesNothingLive` | G19, G20 KeepsBorn, G22 PurgeStamp, G24 PurgeByBucket | `warehouses.py` (bucket half) | `GrantsClaimKept`, `GrantsBornReleased`, `GrantsPurgeUnstamped`, `GrantsClaimUnaddressed` | `test_a_dead_tenants_records_reach_no_live_tenant.py` (claim, purge and audit tests) | not re-run (unchanged) |
| `TableNoLeak`, `TableOwnedWhileLive`, `NoAccessAfterRevoke` | table part | unchanged | `GrantsTableByName`, `GrantsUndropOutlasts`, `GrantsCachedCheck` | unchanged | not re-run (unchanged) |

U7's after-mark read is masked in the test by the read before the mark, which counts the same creating record; a test
that lands the record between the two reads is not written, so that half of the guard is held by the model alone.

The one crossing by design (decision 8) is `GrantsDeclared`: the declarer resolves the creation the id names, decides
for it and writes on it, and `NoLeakAcrossIncarnations` holds there.

## The model

`specs/catalog/Grants.tla`, 896 lines before, 731 after; 41 configs before, 30 after. TLC, two workers, measured
2026-11-18 under the load of two other agents' runs (`make tla-check` result in the commit message):

| Config | Result |
| --- | --- |
| `Grants` | holds, 10,518 distinct states |
| `GrantsRevoke` | holds, 173,786 |
| `GrantsIntruder` | holds, 130,550 |
| `GrantsChild` (a warehouse under a project) | holds, 41,112 |
| `GrantsChildCascade` (a namespace under a warehouse) | holds, 22,804 |
| `GrantsDeclared` (MaxInc 2) | holds, 352,432 (1,397,283 once the delete is modelled as runs with their own marks, review T2) |
| `GrantsDeclaredLedger` (the declarer's ledger, the id made again, liveness `DeclaredOnLive`) | holds, 3,681 |
| `GrantsConvergeRecreate` (the converge part, its object made again between ticks, liveness `DeclaredHeld`) | holds, 58 |
| `GrantsDeleteTwice` (two deletes of one creation, either refusable, a cascaded child, the id made again) | holds, 653,120 |
| `GrantsTable` | holds, 440 |
| `GrantsConverge` | holds, 33 |
| `GrantsChildTwice` (two edges; on demand, `make tla-check-full`) | holds, 715,524, 694 s |
| 25 must-fail configs | each fails on the property it names (`GrantsDeclaredStale`, `GrantsConvergeStale`, `GrantsDeleteTwiceById` and `GrantsRefusalStuck` added by review) |

The gate dropped `DrainBounded`, `DeadTicketsSwept` and `ClaimNamedByItsRecord` (G23's), and their configs, with the
mechanisms. `GrantsDeclared` runs at MaxInc 2 in the gate (MaxInc 3: 8,935,664 states, 235 s, holds).

## Measurements

Requests to OpenFGA per door, counted at the SDK client, and wall time, on the catalog app over a loopback real
OpenFGA (`services/catalog/tests/conftest.py`'s `governed_catalog`), measured 2026-11-18 on a 4-core host shared with
two other agents' runs, so times are indicative and request counts exact. The same harness ran on the parent commit
(`8ecd9c5`) and on this one. Postgres: the pinned OpenFGA on a local Postgres 16 (`openfga migrate` then `run`).

| Measure | Datastore | Before: requests | Before: p50 | After: requests | After: p50 |
| --- | --- | --- | --- | --- | --- |
| Member grant, c=1 | memory | 9 (2 check, 3 write, 4 read) | 40.4 ms | 5 (1 check, 1 write, 3 read) | 30.4 ms |
| Member grant, c=16 (per grant) | memory | 9 | 345 ms (p95 373) | 5 | 276 ms (p95 300) |
| Member grant, c=1 | Postgres | 9 | 49.2 ms | 5 | 27.2 ms |
| Member grant, c=16 (per grant) | Postgres | 9 | 437 ms (p95 471) | 5 | 263 ms (p95 323) |
| Namespace create in a warehouse, 0 / 1,000 / 10,000 siblings | memory | 6 | 35.5 / 36.7 / 112.9 ms | 2 | 25.5 / 21.5 / 36.5 ms |
| Namespace create in a warehouse, 0 / 1,000 / 10,000 siblings | Postgres | 6 | 47.0 / 45.8 / 59.9 ms | 2 | 28.1 / 33.9 / 29.2 ms |
| Project delete (10 members) | memory | 25 | 163 ms | 16 | 42.6 ms |
| Project delete (10 members) | Postgres | 25 | 84.5 ms | 16 | 56.2 ms |
| Converge tick, 0 / 1,000 / 10,000 live warehouses, steady | memory | 1 request; 0.00 / 0.03 / 0.18 s | | 0 requests; 0.00 / 0.02 / 0.15 s | |
| Converge tick, 0 / 1,000 / 10,000 live warehouses, steady | Postgres | 1 request; 0.00 / 0.02 / 0.15 s | | 0 requests; 0.00 / 0.01 / 0.17 s | |

No grant refuses another at c=16 on either tree (every grant answered 200). The grant's one check is its gate, asked
once; before, the gate was asked twice, and the fence took two Writes beside the grant's own.

## What a client or an operator sees that differs

- OpenFGA tuples, the estate admin's tuple browser and the per-tuple audit rows name `project:<uuid>` / `warehouse:<uuid>`.
  Every door's own audit row naming such an object carries the id beside it (`project=<id>`, `warehouse=<id>`), as a
  grant event carries `object_name` (the id) and `object_uuid` (the creation) beside `object_id` (`docs/COMMIT-EVENTS.md`).
  `project_created`, `project_deleted`, `warehouse_created` and `warehouse_deleted` name the id in `object_id` and the
  creation in `extra.uuid` (`project_deleted`'s former `extra.incarnation`); `warehouse_deleted` carries no
  `dead_tenant` or `project_incarnation`.
- A grant, revoke or managed-access change on a project or warehouse deleted while it ran answers 404, where it answered
  409. A project or warehouse whose create died before its seed is answered as absent (404, or 403 to a caller its gate
  refuses) until the converge tick finishes it, where it answered 409 with `Retry-After`. No door answers `Retry-After`.
- A record naming no creation (`uuid`, `project_uuid`, `state`) does not parse: a tolerant listing skips and reports
  it, a strict read refuses (500, code 18). A create whose body names `project_uuid` is refused 422.
- A warehouse whose project was deleted while its create ran answers 404, its record ended, where it answered 409 and
  left an orphan record. A warehouse record still `creating` cannot be deleted at its door (404); the tick finishes or
  ends it. A namespace created directly in a warehouse deleted while it ran answers 404, where it answered 409.
- The project's and warehouse's access doors answer `name` (the id) beside `object` (the creation).
- `/management/v1/me`'s projects are mapped from the creations OpenFGA answers to the ids their live records hold.
- Maintenance's drift report judges the census by the uuids the records name; a ghost project or warehouse is named by
  its uuid. The converge tick's result names `finished`, `ended`, `swept` (tuples revoked from ended creations),
  `orphans`, `released` and `failed`; the metric is `catalog.tenancy.orphans` and the log `record_of_a_dead_creation`.
- A create of a project or warehouse whose id has the shape of a uuid (32 hex digits) is refused 400.
- A client that asks the catalog's decision door about a project (`/management/v1/decisions/*`, a notifier expanding
  `project:<…>#member`) names it by its creation, `project:<uuid>`, which the project's events carry in `extra.uuid`.
- A delete refused before its point of no return (a protected namespace or table in its cascade, a late namespace or
  warehouse, the namespace fence held, a clone outside, a guard of its cascade refusing before any step landed) leaves
  its object live and answers the refusal (409); one that failed past it answers `partial` (503, code 17) and leaves the
  object marked `deleting`: its names are still read, `GET /management/v1/warehouses/<id>` still answers it, its grant
  doors answer as for an absent object, every door changing what lies beneath a warehouse so marked answers 409 (code
  14), and the same delete asked again finishes it. A project's delete failing past its revoke answers 503 `partial`
  where it answered 500.
- Two deletes of one object: the later takes the mark over, the earlier stops (409); a delete held across its object's
  end and the id made again answers 404 and ends nothing of the object made again. A protection, status, manager or seal
  change decided on one creation answers 404 where the id names another by its write.
- A door on a project's or a warehouse's id answers a caller who holds nothing on it the same for an id that stands and
  one that does not (403 where it gates on a relation, 404 where it answers refusals as absent); only a caller its gate
  admits learns an id is missing. The warehouse create door answers 403 for a project nobody holds a grant on, where it
  answered 404; a project being deleted is refused 403 at its members and access doors, where it answered 404.
- A create that lost the race for its id answers 409 (code 2) and seeds nothing, where it converged on the winner.
- A role's assignment, unassignment, deletion and create answer 404 where the project was deleted while they ran.
- The estate admin's tools take `project:<id>` and `warehouse:<id>` (a 400, code 13, for one no creation holds), answer
  `name` (the id) beside a creation's uuid, `user_name` on a tuple whose user is a creation, and `names` on list-users.
- The declaration converge's `failed` entries read `<object>: <why>`; a grant git declares on an id made again is written
  on the new creation in the same tick.
- A label definition names the creation of its project that defined it (`project_uuid`); one another creation left is
  replaced by a define and is none of the project's.
- A trash record names the creation of the warehouse it was filed under (`warehouse_uuid`); a warehouse's delete ends
  only its own creation's.
- A name whose first segment names no warehouse (one deleted) is refused 403 at a gate, as any name holding no object
  is; it was asked on `warehouse:<id>` and answered by whatever that object held.
- A project policy set on a project with no live record answers 404, where it answered 400 ("no active warehouse").

## Line counts

Against the parent commit (`8ecd9c5`), measured with `diff --shortstat`:

| Part | Added | Removed | Net |
| --- | --- | --- | --- |
| Production (`packages/*/src`, `services/*/src`, the model and its changelog) | 1,892 | 2,482 | -590 |
| Tests | 860 | 1,207 | -347 |
| Model (`specs/catalog/Grants*`) | 1,217 | 2,156 | -939 |
| Collected tests (`pytest --collect-only`) | 4,524 before | 4,515 after | -9 |

By module: `fga.py` 3,070 to 2,664 lines; `fga_deps.py` 3,373 to 3,211; `catalog.services.warehouses` 1,048 to 995;
the warehouse doors 2,015 to 1,977; `catalog.services.incarnations` (503) is replaced by `tenancy_converge` (349),
`catalog.services.tenancy` (140) and `lakehouse.governed.tenancy` (129); `Grants.tla` 896 to 731.

## Consequences

- GitOps: `catalog.declared` names ids, and the converge writes each grant on the creation the id names; a creating
  record is finished, and an ended creation swept, by the converge tick with no hand-run step.
- Cloud native: nothing new runs; the resolver is one registry read the request already makes for routing.
- Lance-namespace/table-idiomatic: untouched.
- Zero trust: a tuple written for a dead creation can never apply to a live one, by construction; a caller is asked its
  gate before anything is written; a Write naming a project or warehouse by an id is refused before it reaches the store.
- The model change (`type fence` removed; `revision` keeps ADR 0213's token and the declared grant's evidence) is in
  `packages/lakehouse/src/lakehouse/governed/auth/CHANGELOG.md`.
