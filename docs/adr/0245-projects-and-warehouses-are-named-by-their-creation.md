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
   only `live`, and maintenance maintains only `live` (`lakehouse.warehouse_records`). No state names an ended creation:
   its end is its record's removal (decision 6). Who leaves each state, by what write, and what the converge tick does
   with it is the table under § Found by the second review. A create writes its record first, `creating`, naming its uuid and the admin (a project) or owner (a
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
   an object is revoked), end the record (`tenancy.end`): the creation is kept under `_ended/<kind>/<uuid>.json`, then
   the record is removed by ONE DeleteObject under the ETag of a read that found the delete's own mark
   (`control_records.end_record`, `lakehouse.records.remove_json`), which frees the id. The checks, the mark, the point of
   no return and what each refusal or failure leaves are set by the reviews' T3 and S1 (§ Found by review, § Found by the
   second review). The converge tick revokes what stands on each ended creation whose record no longer stands, and
   forgets it once a sweep past the bound found nothing: a write that raced the delete left tuples on a dead object, which
   authorize nothing; the sweep is hygiene.
6. **Registry layout, and the keyspace rule.** The two registries that map an id to its creation stay keyed by id
   (`_projects/<id>.json`, `_warehouses/<id>.json`): "which uuid is live for this id" is one read of that key, and a
   create is one conditional write (iff absent). Every other change of such a record is a write under the ETag of a read
   that found its creation (`control_records.change_creation`), and its removal one DeleteObject under the ETag of a read
   that found it (`control_records.end_record`; second review S3). Every record that is a project's own is FILED UNDER
   ITS CREATION'S UUID (third review): its maintenance policy (`_policies/project/<project_uuid>.json`), its label
   definitions (`_label_definitions/<project_uuid>/`) and its roles (`_roles/<project_uuid>/`), so a change decided on
   one creation lands in that creation's keyspace alone and cannot reach the next by construction; the creation's end
   removes its keyspace, and the converge tick ends what a change held across the end wrote there after it. Maintenance
   reads a project's policy under the creation its bucket's claim names. A declared grant's ledger record stays keyed by
   the grant (git names the id), and its removals are conditional on the read that judged them (`declaration.
   forget_record`, `forget_grant`). An ended record is not kept under the id (it is removed, and its creation kept by uuid under
   `_ended/`), so it never blocks its id: G23 (KeepsEndedKey) is unnecessary and deleted. The records-by-uuid-with-a-pointer layout the analysis proposed costs two
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
| T1: a grant git declares was never written on a project or warehouse made again under its id: the ledger's settled record of creation A blocked the intent on B, and the tick reported `failed=[]` | U11 LedgerPerCreation: a settled record whose grant is not held on the live creation is forgotten (one removal under the ETag of the read that found it unchanged, second review S3) and the grant declared afresh in the same tick; a grant the tick neither holds nor writes raises and is named in `failed` as `<object>: <why>` | `services/catalog/src/catalog/api/declaration_converge.py` `_Converger._grant`; `services/catalog/src/catalog/services/declaration.py` `forget_record`; `_attempt` | `GrantsDeclaredStale`, `GrantsConvergeStale` (temporal `DeclaredOnLive`, `DeclaredHeld`) | `tests/integration/test_a_delete_ends_only_the_creation_it_marked.py::test_a_declared_grant_is_held_on_the_object_made_again_under_its_id` (warehouse, project) | 2 failed |
| T2: a retried delete of creation A, held past A's end, destroyed B made again under the id (its record, its roles), and a held protection change landed on B | U12 EndsOwnCreation, ONE conditional way to change a tenancy record: `control_records.change_creation` (a write under the ETag of a read that found the uuid; the end is one removal under the ETag of a read that found the delete's own mark, `end_record`, second review S3); the delete's mark carries its run (`delete_run`), and its stamp and its end are written only while that mark stands (the per-step re-read `tenancy.require_own` this round added is deleted by the second review, S4); companions are selected by the creation (roles by `project_uuid`, label definitions by `project_uuid`, the trash by `warehouse_uuid`, which `trash.put` stamps) | `services/catalog/src/catalog/services/control_records.py` `change_creation`, `end_record`; `services/catalog/src/catalog/services/tenancy.py` `DeleteRun`, `mark`, `end`; `roles.every_role`; `object_labels.end_definitions`; `warehouses.trash_owned_by`; `projects.set_project_protection`, `warehouses.set_warehouse_protection` | `GrantsDeleteTwiceById` (`DeleteEndsOnlyItsOwn`) | `test_a_projects_delete_held_across_its_end_ends_nothing_of_the_project_made_again`; `test_a_protection_change_held_across_its_objects_end_lands_on_no_object_made_again` (project, warehouse); `services/catalog/tests/test_a_warehouse_delete_ends_only_its_own_trash.py` | roles by id and no re-read: 1 failed; end unconditional and no re-read: 1 failed; protection by id: 2 failed; the re-read alone: none failed (masked by the selection by creation and the conditional end, which each hold it) |
| T3: a cascade refused by a protected namespace left the warehouse `deleting` for ever, answered absent at its doors while its names still took writes | U13 RevertsRefusal, the delete's point of no return: every check that can refuse with nothing changed runs before the mark (protection, emptiness, the purge's guards, the namespace fence, the subtree's protection, the clones outside); the mark is written under the ETag of the read those checks judged; a refusal after it, before the stamp `no_return`, puts the record back live (`tenancy.put_back`), and so does a guard of the same run refusing before any step landed; past the stamp the answer is `partial` (503) and the same delete asked again finishes it; a warehouse marked `deleting` refuses every door changing what lies beneath it (409, code 14) | `warehouses.delete_warehouse`, `projects.delete_project`; `tenancy.pass_no_return`, `put_back`; `services/catalog/src/catalog/api/dependencies.py` `require_active_warehouse`; `fga_deps._changes` | `GrantsRefusalStuck` (`RefusedDeleteLeavesLive`) | `test_a_cascade_refused_by_a_protected_namespace_leaves_its_warehouse_live`; `test_a_projects_delete_reads_its_warehouses_again_after_its_mark` (a refusal after the mark that puts back); `test_nothing_is_written_beneath_a_warehouse_its_delete_has_marked`; `test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py::test_a_cascade_refused_before_anything_landed_answers_its_refusal` | checks after the mark: 1 failed; no revert: 2 failed; writes admitted: 1 failed |
| T4: the members door and the warehouse access doors resolved before their gate, so a caller holding nothing was told 403 for an id that stands and 404 for one that does not | ONE gate for every door on a tenant's id: `fga_deps.on_tenant` resolves, asks the door's gate on the creation or on the object nobody holds, and only a caller the gate admits learns the id is missing; every such door goes through it (`require_on_tenant`, the members, access, roles, labels, project and warehouse create doors) | `services/catalog/src/catalog/api/fga_deps.py` `on_tenant`, `require_on_tenant`; `access._grant_gate`; `members.grant_member`, `revoke_member` | (U3 and G14 hold the gate's order; the oracle is a disclosure, not a state the model holds) | `services/catalog/tests/test_a_door_the_router_leaves_open_decides_for_itself.py::test_a_tenancy_door_answers_a_caller_it_grants_nothing_alike_for_an_id_no_object_holds` (every tenancy door the router serves) | the members door resolving first: 1 failed |
| T5: the estate admin's tuple doors answered 500 (code 18) for `project:acme`, and its read tools gave false answers on it; the console's create dialog wrote `project:<id>` through the raw tuple door | The tools speak ids: `project:<id>` / `warehouse:<id>` resolve to the creation (live for a write or a hypothesis, standing for a question), a creation's uuid is taken where one stands, anything else shaped like a tenancy object is 400 (code 13); answers name the id beside the uuid (`name`, `user_name`, `names`); the console grants the extra admin at the project's members door (`grantProjectAdmin`) | `services/catalog/src/catalog/api/v1/endpoints/access_admin.py` `_held_object`, `_creation_of`, `_held_user`; `object_names.tenancy_ids`; `frontend/console/src/lib/ProjectCreateDialog.svelte`, `remote/warehouses.api.ts` | — | `services/catalog/tests/test_the_estate_admins_access_doors_take_an_object_by_name.py` (project by id, tuples by id); `frontend/console/src/lib/remote/warehouses.api.test.ts` | ids taken as written: 5 errored (the fixture's own write refused 500) |
| T6: the role doors (assign, unassign, delete) answered 200 for a change on a project deleted while they ran | Every door writing a tuple on a tenancy creation resolves the id again after its Write through one helper, `tenancy.require_still` (the role doors, a role's create after its edge, the members, access and managed-access doors) | `roles._assign`, `_unassign`, `delete_role`, `_mint_role` | `GrantsAnswersUnread` | `test_a_door_the_router_leaves_open_decides_for_itself.py::test_a_tenancy_door_whose_creation_ends_under_its_write_answers_the_deleted_object` (every writing door; a roster test fails a door in neither roster) | the role assignment's: 1 failed; managed access's: 1 failed |
| T7: U7's after-mark child read was held by the model alone | — (the guard stands; a test now reaches it) | `projects.delete_project` | `GrantsCreatingUncounted` | `test_a_projects_delete_reads_its_warehouses_again_after_its_mark` | 1 failed (the delete answered 200) |
| T8: the sweep's coverage was a hand-kept list | Derived from the model's type restrictions | `packages/lakehouse/src/lakehouse/governed/fga.py` `types_naming`; `fga_deps.naming` | — | `test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py::test_a_projects_end_revokes_a_tuple_of_every_type_the_model_lets_name_it` (compares with the DSL) | a hand list without `role`: 1 failed |
| T9: a create that lost the race for its id seeded and announced on the uuid it minted, which named no record | A mint refused by the store answers 409 (code 2), seeding and announcing nothing; the announcement is filed once the record is written | `projects._write_project_record`, `establish_project`; `warehouses._write_record` | — | `test_a_project_create_that_lost_the_race_for_its_id_made_nothing`; `tests/integration/test_warehouses.py::test_a_create_that_lost_the_race_for_its_id_made_nothing` | converging on the winner: 1 failed |

**The delete's order** (decision 5 as it now stands, after the second review). Project: authorization (404 for a
caller who may not delete it; the starter's own right and `can_finish_project_delete` only over a mark past its point of
no return) → one read of the record → protection and emptiness judged on it unless it is past a point of no return
(refusing with nothing changed) → the mark under that read's ETag (`deleting`, `deleting_by`, `delete_run`,
`marked_at`; 409 where the record changed since) → the warehouses read again (one found: 409) → the point of no return
(`no_return`, `delete_asked`), written under a read that found this run's mark → revoke its creation's tuples and those
naming it → its roles, its policy, its label definitions, each of this creation, each removed under a read that found it
so → the end (`_ended/`, the record removed under a read that found this run's mark). From the mark to the stamp is ONE
span for both doors (`tenancy.marked`): ANY failure in it puts the record back live. Warehouse: authorization → one read
→ protection (unless past a point of no return), the namespaces listed, the purge's guards, the estate store → the
namespace fence (409 while held) → the subtree's protection, the trash of its creation (refusing one under its name that
names no creation), the clones outside → the mark → the namespaces read again (a late one: 409 without `cascade`, or held
to the same checks with it) → the point of no return, recording `cascade` and `purge_bucket` → the purge's claim (given
back, and the record put back, where a guard refuses before any step landed) → the trash, the cascade, the orphans, the
revoke → the bytes → the fence confirmed → the end → the claim released. (The third review moves the claim, and a check
of the cascade's tables, before the stamp: § Found by the third review.) "Nothing irreversible yet" ends at `no_return`;
from there the delete finishes, or answers `partial` (503) and the same delete asked again, or the converge tick past its
bound, finishes it. A delete asked again over a mark takes it over (`delete_run`): over one short of its point of no
return it is a new delete, judged on every check; over one past it, it resumes. The earlier run's stamp or end then
finds its mark gone and stops (409), or, where the id names another creation, answers 404.

## Found by the second review, and fixed (2026-11-18)

A second independent review found two blocking defects (S1, S2), both reproduced against the catalog with a real
OpenFGA and moto S3, reasoned a third (S3), and left four things it could not settle (S4). All are closed in one
change, the model first; each guard was removed in the code with its test run and the file restored (measured
2026-11-18), and each must-fail config fails without it.

| Problem | Guard | Where (file, function) | Must-fail config | Test | Guard removed |
| --- | --- | --- | --- | --- | --- |
| S1a: a store or OpenFGA failing between a delete's mark and its point of no return (the after-mark read, the stamp's own write) left the record `deleting` for ever: its doors 404, writes beneath a warehouse 409, a re-create 409, every declaration tick failing a git-declared one, the tick reporting nothing | U14 PutsBackOnFailure: ONE span from the mark to the stamp for both doors, so no path out of it skips it; ANY failure in it puts the record back live; where the put-back fails too the answer (503, `MarkLeft`) says the object is left marked and how it is cleared (`marked_for_deletion`, `cleared_by`) | `services/catalog/src/catalog/services/tenancy.py` `marked`, `Marking.pass_no_return`, `MarkLeft`; used by `projects.delete_project`, `warehouses.delete_warehouse` | `GrantsFailureStuck` (`RefusedDeleteLeavesLive`) | `tests/integration/test_a_delete_ends_only_the_creation_it_marked.py::test_a_delete_failing_before_its_point_of_no_return_leaves_its_object_live` (project, warehouse) | put-back removed from the span: 2 failed |
| S1b: nothing ended a `deleting` record whose run died, before its stamp or after it | U15 TickSettlesMarks: the converge tick puts back, under a read that finds that same run's mark, a mark with no point of no return whose run is older than its bound (`marked_at`), and FINISHES a stamped one as old by running the delete door itself as no caller, as the stamp records it was asked (`delete_asked`: `cascade`, `purge_bucket`), so bytes are purged only where the delete that passed the stamp was asked to, under the claim the purge takes itself (ADR 0241 §10); the tick reports `put_back` and `deleted`; boot and the tick both run it | `services/catalog/src/catalog/services/tenancy_converge.py` `_deleting`, `_settle_mark`; `tenancy.put_back_stale`, `marked_before`; `services/catalog/src/catalog/api/declaration_converge.py` `finisher`; `control_records.DeleteAsked` | `GrantsMarkUnsettled` (temporal `DeletingSettles`) | `test_a_mark_its_delete_could_not_put_back_is_put_back_by_the_tick`; `test_the_tick_finishes_a_delete_that_failed_past_its_point_of_no_return` (project, warehouse) | the tick's settling removed: 3 failed |
| S1c: a delete asked again over a mark with no stamp skipped protection (and the starter's right skipped authorization), so a `force=true` delete that died in the window let a plain delete finish deleting a protected object | U16 ChecksTakeOver: taking over a mark short of its point of no return is a NEW delete, judged on every check and on the caller's own authorization; only a mark past one is resumed without them | `tenancy.judges`; `projects.delete_project`, `projects.project_to_delete`; `warehouses.delete_warehouse` | `GrantsTakeOverUnchecked` (`ProtectedDeletedOnlyByForce`) | `test_a_delete_taking_over_a_mark_short_of_its_point_of_no_return_is_judged_again` | checks only on a live record: 1 failed |
| S2: the one gate's refusal named the object it was asked on, so a caller holding nothing read `project:<uuid>` for an id that stands and `project:000…0` for one that does not, on the members door, both access grant doors, GET and POST of warehouses: the body told the ids apart and leaked the live creation's uuid | The refusal wording rule: a refusal at a tenancy gate is worded on what the CALLER sent (`<kind>:<id>`, the gate's label) and never on a resolved object; the uuid is the audit row's | `services/catalog/src/catalog/api/fga_deps.py` `on_tenant` (asks `gate(obj, label)`), `require_on_tenant`, `_require_any` (`labels`), `_require_rung` and `_authorize_grant` (`label`), `refuse_self_elevation`; every gate passed to `on_tenant`; `warehouses._require_project_admin` | — (a disclosure, not a state the model holds) | `services/catalog/tests/test_a_door_the_router_leaves_open_decides_for_itself.py::test_a_tenancy_door_answers_a_caller_it_grants_nothing_alike_for_an_id_no_object_holds` (now the whole problem body, minus `instance`, the ids read as `<id>`) and `::test_no_refusal_at_a_tenancy_door_names_a_creation` (no answer carries any creation's uuid, nor the object nobody holds) | the label set to the object: 2 failed |
| S3: a record was ended by a stamp then an UNCONDITIONAL delete: a stamper paused past the bound had its stamp removed by the tick, a create took the id, and the paused stamper deleted the NEW record; a project's policy record, a label definition and the ledger's forgotten record were removed by id alike | U17 ConditionalEnd: every id-keyed record is removed by ONE DeleteObject under the ETag of the read that judged it (`lakehouse.records.delete_json`, `remove_json`), so nothing removes a record another creation wrote since; the stamp `ended`, `remove_stale_ended`, the ledger's `forgotten` mark and its listing filter are deleted. Measured first (below): the stores rask names take a conditional delete | `packages/lakehouse/src/lakehouse/records.py` `delete_json`, `remove_json`; `control_records.end_record`; `maintenance_policies.end_project_policy` (the project's delete, its policy door, and the tick, which now ends a dead creation's policy once a read of the project's record made after its listing names that creation no more: the full suite caught the tick ending the policy of a project made after its listing, 1 run in 3 of `tests/integration/test_api.py::test_project_policies_list_returns_only_its_own_project_record`, 0 in 5 since, and the same read now guards its ending of a role, `tenancy_converge._creation_ended`); `object_labels.end_definitions`, `delete_definition`; `declaration.forget_record` | `GrantsEndByKey` (`DeleteEndsOnlyItsOwn`) | `test_a_delete_held_at_its_removal_removes_no_record_of_the_project_made_again`; `tests/integration/test_a_dead_tenants_records_reach_no_live_tenant.py::test_a_project_policy_governs_only_the_buckets_of_the_creation_that_set_it` (the tick ends the dead creation's policy and keeps the live one's) | the delete's `If-Match` dropped: 1 failed |
| S4a: a table create whose gate passed before its warehouse's cascade marked it was not shown to land nothing | — (no defect: held after its gate, before `create_governed_table`, across the cascade run to completion, the create is refused 404 by its parent check, since the namespace it named is gone, and its write is placed under the namespace's add fence, which the cascade's drop fence excludes) | `table_create.create_governed_table` (`require_parent_exists`, `write_judge.placing`) | — | `test_a_table_create_held_across_its_warehouses_cascade_lands_nothing_under_it` (no namespace, catalog row or byte of it under the warehouse made again) | — |
| S4b: `tenancy.require_own` after each long step guarded nothing the review could build | Deleted. Asked of the model: with the per-step re-read removed in every config, no property fails (measured 2026-11-18, 29 must-fail configs failing as before, every gate config holding); the stamp and the end are written only under a read that found the run's own mark (U12, U17), the children are selected by the creation, and the cascade writes only under the warehouse's namespace fence | — | — | the T2 tests above, unchanged | — |
| S4c: a trash record naming no `warehouse_uuid` was skipped silently by the warehouse's trash selection | Under a warehouse with a record, a trash record under its name naming no creation of it refuses the selection (500, code 18, logged `trash_record_unaccountable`), so the delete that cannot account for it refuses before its mark and the trash listing reports it, never skipping it | `services/catalog/src/catalog/services/warehouses.py` `trash_owned_by` | — | `services/catalog/tests/test_a_warehouse_delete_ends_only_its_own_trash.py::test_a_trash_record_the_selection_cannot_account_for_refuses_it` (unreadable, naming no creation) | the refusal removed: 1 failed |
| S4d: maintenance ignored a warehouse record's state, so a `creating` or `deleting` warehouse stayed maintainable | A warehouse is maintained only while `live`: `maintainable_buckets` (the sweep's and the purge's bucket set) and `warehouse_record` (a table's own warehouse) read the state, as the resolver does; one being deleted is emptied by its own delete | `packages/lakehouse/src/lakehouse/warehouse_records.py` | — | `packages/lakehouse/tests/test_maintenance_maintains_a_warehouse_only_while_it_is_live.py` (live, creating, deleting) | state unread: 2 failed |
| S4e: two replicas ticking `forget_record` and the new delete steps at once | No defect: every write either makes is conditional (a put-back under the read that found that run's mark; a finish takes the mark over, so of two finishers one's stamp or end finds its mark gone, which the tick answers as finished elsewhere, not as a failure; a forgetting removes the record only as it read it, so an intent another tick recorded since stays) | `tenancy_converge._settle_mark` | — | the tick's tests above run two ticks at once (`_settle`); `test_a_declared_grant_is_held_on_the_object_made_again_under_its_id` runs two declaration ticks at once (one writes the grant, the other finds its intent); each run three times | — |

**The record's states, who leaves each, and what the tick does.**

| State | Who may leave it | By what write | What the converge tick does |
| --- | --- | --- | --- |
| absent | a create | the record, `creating`, iff absent | nothing |
| `creating` | its create (to `live` once seeded; ended where its project's creation is gone) | `set_live` under the ETag of a read finding the uuid creating; `end_record` (one removal under that read's ETag) | past its bound: finishes it (seed, then `live`) under a live parent holding its claim, or ends it |
| `live` | a delete | the mark, under the ETag of the read its checks judged | nothing |
| `deleting`, no `no_return` | the delete whose run holds the mark (put back on any failure before its stamp, or stamped); a delete taking the mark over, as a new delete | `put_back` (only where `delete_run` is its own), `pass_no_return`, `mark`, each under the ETag of a read finding the creation | past its bound (`marked_at`): puts it back live under a read finding that same run's mark (`put_back`) |
| `deleting`, `no_return` | never put back: the delete holding the mark, or one taking it over, which resumes as `delete_asked` records | `end_record` under the ETag of a read finding the run's own mark | past its bound: finishes it through the delete door's steps, on a fresh read still carrying the listed run past its stamp, as `delete_asked` says (`deleted`) |

**The refusal wording rule.** A refusal is worded on what its caller sent: at a tenancy gate, `<relation> required on
<kind>:<id>`, whether or not the id stands. A creation's uuid is named in the audit row, the logs and the events of those
entitled to them (`project_created`'s `extra.uuid`, a grant event's `object_uuid`), never in a refusal. A refusal on a
table or a namespace names its name already (`_label`).

**The conditional delete, measured** (2026-11-18). On the pinned RustFS 1.0.3 (`.localbin/rustfs-1.0.3`, a local
volume): `DeleteObject` with a stale `If-Match` answers 412 `PreconditionFailed` and keeps the object; with the right
ETag, 204; 40 rounds of 8 conditional deletes racing 8 conditional puts under one ETag each had exactly one winner; on a
missing key, refused. Through `lakehouse.records.delete_json` (the ETag as read, unquoted): the same. On moto 5.2.2 a
stale `If-Match` answers `PreconditionFailed`. AWS S3 takes `If-Match` on `DeleteObject` in general purpose buckets since
2025-09. A local root does the compare and the unlink under the conditional replace's exclusive lock, and the replace now
refuses a file its delete unlinked (`records._still_at`). So S3 took the first design the review allowed: the layout is
kept, and every id-keyed removal is conditional. ADR 0241 §9 P2's "the store takes no conditional delete" is not true of
the stores rask names; RustFS's own s3-tests list excludes its conditional-delete tests
(`rustfs@1.0.3:scripts/s3-tests/excluded_tests.txt:138-156`), so the measurement above is rask's evidence, and a platform
store must take `If-Match` on `DeleteObject` as it takes it on `PutObject`.

**What remains a bound, not a guard.** "Its run no longer runs" is judged by the catalog's clock against `marked_at`
plus the tick's bound (the request deadline plus five minutes), as a `creating` record's is; a request running past that
is taken over by the tick, and the two act on one creation's own objects under the fence and the conditional writes.
Two deletes asked again for ever may hand a mark between them for ever (`GrantsTakeOver` checks safety alone);
`DeletingSettles` is checked with one delete asked again for ever (`Grants`, `GrantsMarkUnsettled`).

## Found by the third review, and fixed (2026-11-19)

A third independent review found five blocking defects (V1 to V5), each reproduced against the catalog with a real
OpenFGA and moto S3 (`tests/integration/test_review_tenancy_three.py` on the review branch, five failing on `cf74ba5`;
the tests are folded into the files of their seams). All are closed in one change, the model first; each guard was
removed in the code with its test run and the file restored (measured 2026-11-19), and each must-fail config fails
without it.

| Problem | Guard | Where (file, function) | Must-fail config | Test | Guard removed |
| --- | --- | --- | --- | --- | --- |
| V1: the tick deleted, and purged, a LIVE warehouse nobody authorized: a purge delete past its stamp, refused at the purge's claim, put its own record back live, and the tick's finisher, which had listed it stamped, read it live, judged it a new delete with no caller and the listing's `delete_asked`, and deleted it | U19 NoReturnFinal: a record past its point of no return is never put back, by anyone; every step that can still refuse runs before the stamp (the purge's claim, `warehouses.take_for_purge`; a listed table another request has fenced, `_require_tables_unfenced`), and past it a refusal answers `partial`. U18 FreshFinish: the tick finishes only on the delete door's own fresh read that still carries the run its listing found past its stamp (`tenancy.judged(finishing=)`, else `NothingToFinish`), takes the mark over under that read's ETag, takes what was asked from that record, and never begins a delete | `services/catalog/src/catalog/api/v1/endpoints/warehouses.py` `remove_warehouse`; `services/catalog/src/catalog/services/tenancy.py` `put_back`, `marked`, `judged`, `asked_of`; `services/catalog/src/catalog/api/declaration_converge.py` `finisher`; `projects.remove_project` | `GrantsPutBackPastStamp` (temporal `StampedEnds`), `GrantsTickFinishLive` (`TickBeginsNoDelete`, U18 and U19 removed together) | `tests/integration/test_a_delete_ends_only_the_creation_it_marked.py::test_a_purge_refused_at_its_claim_leaves_its_warehouse_live_and_the_tick_ends_nothing`; `::test_the_tick_finishes_only_the_delete_its_listing_found_and_as_its_stamp_asked` | the claim taken after the stamp: 1 failed; the fresh-read check removed: 1 failed |
| V2: a held project-policy set replaced the policy of the creation made again under the id: the compose took a record naming another creation for one an earlier creation left, and wrote over it | The keyspace rule (decision 6): the policy is filed under `_policies/project/<project_uuid>`; the set door writes in the keyspace of the creation it resolved and resolves again (`tenancy.require_still`); `_of_creation` and the cross-creation compare are deleted; maintenance reads the policy under the creation its bucket's claim names | `packages/lakehouse/src/lakehouse/maintenance_policies.py` `policy_key`, `end_project_policy`, `governing_policy`; `services/catalog/src/catalog/api/v1/endpoints/policies.py` `set_project_policy`, `describe_project_policy`, `delete_project_policy` | — (records the model does not hold) | `test_a_delete_ends_only_the_creation_it_marked.py::test_a_project_policy_set_held_across_its_projects_end_leaves_the_next_creations_policy`; `tests/integration/test_a_dead_tenants_records_reach_no_live_tenant.py::test_a_project_policy_governs_only_the_buckets_of_the_creation_that_set_it` | the resolve after the write removed: 1 failed (the review's reproduction failed on the id-keyed layout) |
| V3: the same for a label definition (`object_labels._defined_over`), and a dead creation's standing role held a role name from the next creation | Label definitions under `_label_definitions/<project_uuid>/`, roles under `_roles/<project_uuid>/`; `_defined_over`'s cross-creation branch deleted; the define and delete doors resolve again; the tick ends an ended project creation's definitions (`labels:<object>` in `ended`) | `services/catalog/src/catalog/services/object_labels.py`; `services/catalog/src/catalog/services/roles.py`; `labels.define_label`, `delete_label_definition`; `tenancy_converge._sweep_ended` | — | `::test_a_label_define_held_across_its_projects_end_leaves_the_next_creations_definition` | the resolve after the write removed: 1 failed |
| V4: a project re-POST whose creation ended under it answered 200 and announced `project_created` and `grant_added` (`set_live(...) or record`), on the door and on the declaration's path alike | A flip that finds its creation neither creating nor live is a lost creation: the create answers what stands (409 code 2 where another creation holds the id, 409 code 14 where none does or its own is being deleted), announces nothing (`project_created`'s evidence now names the creation's uuid live), and revokes what it wrote on the creation that ended | `services/catalog/src/catalog/api/v1/endpoints/projects.py` `establish_project`, `_lost_creation` | `GrantsAnswersUnread` (U4, unchanged) | `::test_a_project_re_post_whose_creation_ended_meanwhile_answers_what_stands_and_holds_nothing` | the old fallback restored: 1 failed |
| V5: a delete failing before its stamp put back a mark another delete had taken over, so the taker found its mark gone and answered 404 for a project that stands | U20 PutsBackOwnMark: only the delete whose run holds the mark (`delete_run`) puts it back; a write of a delete that lost its mark answers 409 (taken over, or settled by the tick) while the creation stands, 404 only where it is gone | `tenancy.put_back`, `tenancy._lost` | `GrantsPutBackTakenOver` (`PutBackOnlyItsOwn`) | `::test_a_delete_failing_before_its_stamp_puts_back_no_mark_another_delete_took_over` | the run check removed: 1 failed |

Also decided with them: a delete asked again over a stamped mark finishes it as the stamp records it was asked
(`delete_asked` is written once, at the first stamp; a resuming caller's `cascade` and `purge_bucket` never widen it, and
`bytes_kept` says why no byte was purged); `declaration.forget_grant` removes only the settled record of its grant, under
the ETag of the read that found it. The sweep for the same classes found two more: a warehouse create answered after
converging its grants without resolving again (it now does), and a project create's announcement evidence matched any
record at the key (it now names the creation, live).

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

`specs/catalog/Grants.tla`, 896 lines before the first change, 731 after it, 947 after the second review's, 1,013 after
the third's; 41 configs before, 30 after the first change, 42 after the second review's, 48 after the third's. The table
is regenerated from ONE run of `scripts/tla_check.sh Grants` (TLC two workers, 2026-11-19, the host shared with other
agents' runs, 960 s in all), after the third review; the delete is modelled with the purge's claim as a step that may
refuse (before the stamp with U19, after it without), its put-back by its own run alone (U20), and the tick's finish as
the code runs it, its listing first, then the fresh read it acts on (U18):

| Config | Result |
| --- | --- |
| `Grants` (one delete, refusable, failing between its mark and its stamp, its put-back failing too; liveness `DeletingSettles`, `StampedEnds`) | holds, 118,659 distinct states |
| `GrantsRevoke` | holds, 1,934,443 |
| `GrantsIntruder` | holds, 1,449,354 |
| `GrantsChildSafety` / `GrantsChildLiveness` (a warehouse under a project) | hold, 458,642 / 11,162 |
| `GrantsChildCascade` (a namespace under a warehouse) | holds, 247,176 |
| `GrantsDeclared` (MaxInc 2) | holds, 55,604 |
| `GrantsDeclaredLedger` (the declarer's ledger, the id made again, liveness `DeclaredOnLive`) | holds, 3,681 |
| `GrantsConvergeRecreate` (the converge part, its object made again between ticks, liveness `DeclaredHeld`) | holds, 58 |
| `GrantsDeleteTwice` (two deletes of one creation, either refusable, a cascaded child, the id made again) | holds, 6,135,573 |
| `GrantsTakeOver` (two deletes, one forced, a protected creation, failures between mark and stamp; safety alone) | holds, 696,241 |
| `GrantsTable` | holds, 440 |
| `GrantsConverge` | holds, 33 |
| `GrantsChild`, `GrantsChildTwice`, `GrantsDeclaredFull` (on demand, `make tla-check-full`) | not rerun after the third review |
| 32 must-fail configs | each fails on the property it names (`GrantsPutBackPastStamp`, `GrantsPutBackTakenOver` and `GrantsTickFinishLive` added by the third review) |

U18 alone removed is masked in the model by U19: no stamped record goes live for the finish to find, and a take-over
between the listing and the read is finished by either run harmlessly. That is reasoned, not checked by a config of its
own; `GrantsTickFinishLive` removes both, as the code before the third review had neither.

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
  its object live and answers the refusal (409); so does one failing there (a store or OpenFGA not answering), which
  answers the failure (503) with its object live; where its put-back fails too it answers 503 with
  `marked_for_deletion: true` and `cleared_by`, and the converge tick puts the object back live past its bound
  (`put_back` in its result). One that failed past it answers `partial` (503, code 17) and leaves the object marked
  `deleting`: its names are still read, `GET /management/v1/warehouses/<id>` still answers it, its grant doors answer as
  for an absent object, every door changing what lies beneath a warehouse so marked answers 409 (code 14), and the same
  delete asked again, or the converge tick past its bound (`deleted` in its result), finishes it as it was asked. A
  project's delete failing past its revoke answers 503 `partial` where it answered 500.
- A delete asked again over a mark short of its point of no return is a new delete: a protected object refuses it 409
  without `force=true`, and its starter's own right, and `can_finish_project_delete`, no longer authorize it; they
  authorize finishing a delete past its point of no return only.
- A warehouse delete refused at its purge's claim (another purge holds it), or at a table another request is acting
  on, answers 409 with the warehouse live; past its point of no return a refusal answers `partial` (503) and nothing puts
  the warehouse back. A delete asked again over a stamped mark does what the stamp records (`bytes_kept` says where a
  purge it asked for is not done).
- A project create or re-POST whose creation ended while it ran answers 409 and announces nothing, where it answered 200.
- A project's policy, label definitions and roles are stored under the creation's uuid; `policy_deleted` for a project
  still names `project:<id>`.
- A refusal at a door on a project's or a warehouse's id names `<kind>:<id>`, what the caller sent, where it named
  `<kind>:<uuid>` (or the object nobody holds for an id that does not stand); its body is the same for an id that
  stands and one that does not.
- A project's policy set by a creation of it that ended is ended by the converge tick (`policy:project:<id>` in `ended`
  beside `orphans`), where it was reported and kept.
- A warehouse's delete, and its trash listing, refuse (500, code 18) while a trash record under its name names no
  creation of it, where they skipped it.
- Maintenance maintains no warehouse being created or deleted: neither the sweep's and the purge's bucket set nor a
  table's own warehouse names one.
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

The second review's change, against its parent (`65fc577`), measured with `diff --shortstat`: production (`packages/*/src`,
`services/*/src`) +624 / -310, net +314 (the reconciler S1 asks for: the span, the tick's put-back and finish, the
stamp's `delete_asked`, and the conditional removal in `lakehouse.records`; removed: the stamp `ended` and
`remove_stale_ended`, the tick's `_removing_ended`, `require_own` and its six calls, the ledger's `forgotten` mark and
its filter); tests +341 / -24; the model (`specs/catalog/Grants*`) +683 / -64 (four must-fail configs and one gate
config added). Collected tests: 3,963 passed and 4 skipped before, 3,976 and 4 after (+13).

The third review's change, against its parent (`cf74ba5`), measured with `diff --shortstat`: production
(`packages/lakehouse/src`, `services/catalog/src`) +424 / -258; tests +216 / -41 (the review's five reproductions folded
into `test_a_delete_ends_only_the_creation_it_marked.py`, one more for the tick's fresh read); the model
(`specs/catalog`) +421 / -52 (three must-fail configs added, three constants in every config).

## Consequences

- GitOps: `catalog.declared` names ids, and the converge writes each grant on the creation the id names; a creating
  record is finished, and an ended creation swept, by the converge tick with no hand-run step.
- Cloud native: nothing new runs; the resolver is one registry read the request already makes for routing.
- Lance-namespace/table-idiomatic: untouched.
- Zero trust: a tuple written for a dead creation can never apply to a live one, by construction; a caller is asked its
  gate before anything is written; a Write naming a project or warehouse by an id is refused before it reaches the store.
- The model change (`type fence` removed; `revision` keeps ADR 0213's token and the declared grant's evidence) is in
  `packages/lakehouse/src/lakehouse/governed/auth/CHANGELOG.md`.
