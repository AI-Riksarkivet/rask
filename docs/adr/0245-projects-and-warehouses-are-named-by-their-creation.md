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
the console builds no object string, `grep -rn '"project:\|"warehouse:' frontend/console/src` finds only display text and
API paths); the Rust port reads lineage alone and names no project or warehouse object (`grep -rn 'project:\|warehouse:'
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
   an object is revoked), end the record (`projects.end_project`, `warehouses.end_warehouse`): the record is removed,
   which frees the id, and the creation is kept under `_ended/<kind>/<uuid>.json`. The converge tick revokes what stands
   on each ended creation, and forgets it once a sweep past the bound found nothing: a write that raced the delete left
   tuples on a dead object, which authorize nothing; the sweep is hygiene.
6. **Registry layout.** Records stay keyed by id (`_projects/<id>.json`, `_warehouses/<id>.json`): "which uuid is live
   for this id" is one read of that key, and a create is one conditional write (iff absent). An ended record is not
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
   project made again under a declared id is granted it again: git declared it. This is the one place a tuple crosses
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
| `GrantsDeclared` (MaxInc 2) | holds, 352,432 |
| `GrantsTable` | holds, 440 |
| `GrantsConverge` | holds, 33 |
| `GrantsChildTwice` (two edges; on demand, `make tla-check-full`) | holds, 715,524, 694 s |
| 21 must-fail configs | each fails on the property it names |

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
- A warehouse delete refused after its mark (a namespace made meanwhile and `cascade` not asked, a cascade that failed
  part-way) leaves the warehouse marked `deleting`: its names are still served, `GET /management/v1/warehouses/<id>`
  still answers it, its grant doors answer 404, and the same delete asked again finishes it. A purge refused because a
  sibling warehouse shares the bucket is refused before the mark, as before.
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
