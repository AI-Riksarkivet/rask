# 0241. Authorization state is modelled in TLA+ beside the objects it governs; three defects found and fixed, then nine a review found (2026-11-17)

**Status:** accepted; the catalog's sixth model (ADR 0221, ADR 0227, ADR 0229, ADR 0233). It describes the Python
catalog as it runs, before it is ported (ADR 0217 §5). Three findings were confirmed against the running catalog and a
real OpenFGA, and each is fixed, model first (§5). An independent review of that fix found it incomplete; its nine
findings are fixed here, model first (§7), and §7's fence replaces §5's token as what a grant is fenced by.

## Context

The models so far arbitrate the object store: a create, a drop, an undrop, a rename, a cascade, an erasure, a commit,
each judged on records whose conditional writes the store refuses whole. The tuples that say who may do what live in a
second store, OpenFGA, which those conditional writes do not reach. The catalog writes and revokes them beside the
registry records and tables they are about, at its doors, at boot and on a timer, and nothing had modelled the two
stores together.

A table and a namespace are named in OpenFGA by the uuid their create mints (ADR 0173, [[LH-524]]): a table dropped
and created again under its name is another object, and no tuple of the first reaches it. A project and a warehouse are
not. They are named by their id (`project:<id>`, `warehouse:<id>`), which every grant, every declaration and every
client names, and once a project or warehouse is deleted its id is free: a project made again under the id is another
tenant (`projects.py`, the maintenance policy's comment; [[LH-562]]).

## Decision

1. **`specs/catalog/Grants.tla` models the authorization state in three parts, each checked by configs of its own.**
   - **container**: one project or warehouse id, deleted and made again by another tenant, incarnations 1..`MaxInc`.
     Each tuple carries, as a history variable the code never sees, the incarnation its writer was authorized on. The
     actions, each one effect in the code's order: `Mint` the record (`projects.establish_project`,
     `warehouses.establish_warehouse`); `Bear` it, one OpenFGA Write of its marker, its token and its seed, the
     creator's grant and the tenancy edge (`fga.bear`, `fga_deps.seed_project_admin`, `fga_deps.seed_warehouse`);
     `Resume`, a re-POST of a record whose birth never landed; a door grant's `GrantRead` of the token, `GrantCheck` of
     the granter's authority and `GrantWrite` (`members.grant_member`, `access._access_mutate`, the declaration's
     `_grant`, the tenancy rebuild; `fga.write_fenced`); the delete's steps, marked born, token retired, tuples read,
     tuples revoked one by one, record removed (`projects.delete_project`, `warehouses.delete_warehouse`; `fga.retire`,
     `fga.revoke_object_tuples`); and the drift repair's ghost revoke, its judgement and its read in the code's order and
     then its revoke (`grant_reconcile.revoke_ghost_grants`). Any create, grant and delete may die between two steps,
     and a dead delete is issued again. Since §7: every writer on or naming the object (a grant, a revoke, a warehouse's
     tenancy edge, a role's edge) opens a ticket of its own, reads the token, is authorized and commits deleting its
     ticket (`fga.open_fence`, `fga.commit_fenced`); the delete retires and then drains until a read finds nothing
     (`fga.end_incarnation`); the repair writes a token a birth deletes (`fga.revoke_ghost`); a record stored before
     incarnations is given one and born by the catalog's convergence (`incarnations.converge`); and a token shared by
     every grant is a config of its own (`GrantsSharedToken`).
   - **table**: one name, its tables keyed by a minted uuid (`fga.table_object`): a create seeds the owner
     (`fga_deps.seed_ownership`), a purge revokes, a recoverable drop keeps the grants for the undrop, the undrop
     admitted before the deadline registers later, the trash release judges the name free and revokes past
     `trash.RELEASE_SETTLE` (`trash_reclaim.release_trash`), a grant decided on one table lands late, the access door
     revokes it, and a reader decides on what OpenFGA answers (`fga.check`).
   - **converge**: one tuple git declares and later drops (`declaration_converge._Converger._grant` and `_revoke`),
     and the same tuple granted at a door at any point between; since §7 the tick may die between any two of its steps
     and records an intent its evidence settles.

2. **What the gate proves** (`make tla-check`; measured 2026-11-17 on two workers at load 4, after §7):
   - `Grants.cfg` (container: two incarnations, a door grant, the drift repair, any door dying): 140,619 states, 4.0 s.
   - `GrantsContention.cfg` (two grants beside the delete): 40,154 states, 2.7 s.
   - `GrantsEdges.cfg` (a tuple naming the container as subject beside the delete, any door dying): 10,213 states, 2.7 s.
   - `GrantsRevoke.cfg` (a grant and its revoke beside the delete, any door dying): 377,819 states, 12.1 s.
   - `GrantsRepair.cfg` (a ghost equal to the next seed, the repair, any door dying): 6,259 states, 2.5 s.
   - `GrantsUpgrade.cfg` (a record stored before incarnations, a grant and a delete, any door dying; with the temporal
     property `EveryRecordBorn`): 7,762 states, 4.1 s.
   - `GrantsTable.cfg` (table: purge, recoverable drop, undrop, release, a late grant, a revoke and a reader): 440
     states, 1.7 s.
   - `GrantsConverge.cfg` (the tick dying between any two steps): 33 states, 1.3 s.
   - `GrantsWide.cfg`, ON DEMAND (three incarnations, a grant and an edge beside the delete, any door dying): 2,049,597
     states, 85 s. Wider configs (two grants, an edge, a revoke, a ghost and the repair at three incarnations) did not
     finish in 25 minutes and are not claimed.
   - Each guard removed (§3, §5, §7): its config fails in under 3 s.

   The properties, each a claim the code and its ADRs make:
   - `NoLeakAcrossIncarnations` (*no access leaks across objects*): every tuple on a project or warehouse whose record
     stands was decided on that incarnation, so nothing a deleted one held, or a door was granting on it, authorizes on
     one made again under its id.
   - `OwnedWhileLive` (*no missing owner*): a project or warehouse whose record stands and whose birth landed holds its
     seed whatever else ran; one whose birth never landed holds no token, so nothing is granted on it, until its create
     is asked again (`Resume`; `establish_project` and `establish_warehouse` bear a record that names an unborn
     incarnation).
   - `TableNoLeak`: a tuple on the table a name holds was decided on that table.
   - `TableOwnedWhileLive`: a table the name holds has its owner's grant, outside an undrop's own two steps.
   - `NoAccessAfterRevoke` (*no access outlives its grant*): once a revoke answered, no decision is allowed by the
     revoked tuple. The staleness the design allows is a request's own: one decided before the revoke answered may finish
     after it, and nothing decided after it is allowed (A3).
   - `GitRevokesOnlyItsOwn` (*git's grants and door grants do not fight*): the converge revokes only a tuple its own
     write created.
   - Since §7: `NoNarrowAcrossIncarnations` (no tuple of one incarnation removed by a revoke decided on another),
     `GrantRefusedOnlyByDelete` (a grant is refused only where a delete retired or drained while it ran),
     `DrainBounded` (the delete's re-read finds something at most once) and `EveryRecordBorn` (temporal: every record
     standing is born, with no hand-run step, under weak fairness of the catalog's convergence). `NoLeakAcrossIncarnations`
     now covers tuples naming the container as subject (a warehouse's tenancy edge, a role's edge), and exempts a ghost
     nobody decided on, which is the drift repair's.

3. **An assumption the design rests on is a config that must fail** (ADR 0221 §5):
   - A1, `GrantsTableByName.cfg` (`TableNoLeak`): tuples named by the table's name. Held by `fga.table_object`
     (`packages/lakehouse/src/lakehouse/governed/fga.py:345`) and `fga_deps.seed_ownership`
     (`services/catalog/src/catalog/api/fga_deps.py:2165`), every table door naming the uuid object its resolver read.
   - A2, `GrantsUndropOutlasts.cfg` (`TableOwnedWhileLive`): an undrop admitted before the deadline registers after the
     release judged the name free, and the release revokes the restored table's grants. Held by `trash.RELEASE_SETTLE`
     (`packages/lakehouse/src/lakehouse/trash.py:167`) through `trash_reclaim.release_refusal`
     (`services/catalog/src/catalog/services/trash_reclaim.py:142`), a timing argument as ADR 0227's A3 is.
   - A3, `GrantsCachedCheck.cfg` (`NoAccessAfterRevoke`): a decision answered from a cache older than the revoke. Held
     by `fga._fresh_read_options` (`fga.py:1136`): every check, batch check, list and read asks `HIGHER_CONSISTENCY`,
     which skips every OpenFGA cache ([[LH-488]]).
   - Each guard of §5 is one more.

4. **The model was shown to bite.** Six defects were planted one at a time in a copy, and the gate reported each: the
   delete never revoking what it read (`NoLeakAcrossIncarnations`, `Grants.cfg`); a birth writing its token without its
   seed (`OwnedWhileLive`); a fenced grant committing under any standing token rather than its own
   (`NoLeakAcrossIncarnations`); the delete removing the record before it revokes (`NoLeakAcrossIncarnations`); the
   trash release revoking the uuid the name holds rather than the one its record judged (`TableOwnedWhileLive`,
   `GrantsTable.cfg`); the converge revoking a dropped tuple whether or not its ledger names it
   (`GitRevokesOnlyItsOwn`, `GrantsConverge.cfg`).

5. **Three findings, each confirmed against the catalog app with a real OpenFGA on 2026-11-17 and each FIXED.** Each
   test holds one request at the step the trace names while the other doors run whole, and reads the harm over HTTP.
   Each failed on the code before this change and passes after it; each fails again with its guard removed.
   - **F1, a grant decided on a project or warehouse lands on the one made again under its id** (CONFIRMED;
     `NoLeakAcrossIncarnations`, `GrantsUnfenced.cfg`). The most serious class: a person the deleted tenant's admin was
     granting holds `admin`, `member` or `decider` on another tenant's project (or a rung on another tenant's warehouse,
     which reaches every namespace and table in it). Trace, on the code before: the members door asks
     `can_grant_admin` (`members.py:123`); the project's delete revokes every tuple it reads (`projects.py:653`) and
     removes its record (`projects.py:682`); another estate admin makes the project again (`projects.py:306-307`); the
     grant's write lands (`project_admins.confer` reads again and commits, `project_admins.py:223`; `fga.write_tuples`,
     `members.py:143`, `access.py:482`). `get_project`'s docstring said "no grant is made for it while its delete
     revokes": the grant doors never read the record. Test:
     `services/catalog/tests/test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py`,
     parametrized over the members door's `admin` and `member` and the access door's `decider`; all three answered 200
     and left the grant on the new tenant before.

     Fix, three guards. Renaming the objects to a uuid, as tables are, would move every client, declaration and the Rust
     port's `warehouse:<id>`; the incarnation is kept beside the id instead, in OpenFGA, where the tuples are, so the
     store that arbitrates the tuples arbitrates it. Each project and warehouse record carries an `incarnation`, minted
     with the record (`control_records.new_incarnation`) and kept by every merge; the model's `revision` relation on
     `project` and `warehouse` holds the incarnation's marker `revision:<incarnation>` and its token
     `revision:<incarnation>.<n>` (the project's former `admin_revision`, renamed, now moved by every grant).
     - G1 `Fence` (`GrantsUnfenced.cfg`): every write of a tuple on a project or warehouse is one Write that deletes the
       token it read and writes the next (`fga.write_fenced`), so it commits only while that incarnation stands. A door
       reads the token before it asks the caller's authority (`members.py:130`; `access.py`'s `project_incarnation` and
       `warehouse_incarnation`, listed before the gate), so the authority was asked of that incarnation. The members
       door's `admin` goes through `project_admins.confer(incarnation=…)`; the declaration's grants, the tenancy rebuild
       (`fga_deps.write_registry_edge`) and the boot backfill (`converge_warehouse_grants`) write under the record's.
     - G2 `RetireFirst` (`GrantsRetireLast.cfg`): a delete retires the token (`fga.retire`, each deletion conditional on
       the token as read) before it reads what to revoke (`projects.py:657`, `warehouses.py:1182`), so no grant lands
       between its read and its end.
     - G3 `BornKept` (`GrantsRebirth.cfg`): a delete marks its incarnation born before it retires, a birth commits only
       where the marker is absent (`fga.bear`), and no revoke takes a marker (`fga.revoke_read_tuples` leaves
       `BOOKKEEPING_RELATIONS`), so a birth stalled across the delete cannot bear the old incarnation's seed under the
       new one.

     Death leaves access narrower: a delete dying after the retire leaves a project or warehouse on which nothing can be
     granted until its delete is issued again; a create dying before its birth leaves a record no grant reaches until it
     is POSTed again; a grant dying anywhere wrote all or nothing.
   - **F2, the converge records a door's grant as git's and revokes it when git drops it** (CONFIRMED, narrowing;
     `GitRevokesOnlyItsOwn`, `GrantsConvergeAdopts.cfg`). Who loses what: a client's grant made at an access door is
     revoked by a tick, against [[LH-563]] and [[XC-213]]. Trace: the tick reads the store and finds no tuple
     (`declaration_converge.py:258`), records it git's (`:261`), a door grants the same tuple, the tick's write is a
     duplicate the store ignores (`:268`), git drops the grant, and the tick revokes it (`:279`). The comment named the
     window "one store write wide" and left it. Test: the parametrized
     `tests/integration/test_the_catalog_converges_what_git_declares.py::test_a_grant_removed_from_git_is_revoked_and_one_made_at_a_door_stands`
     (`as-the-tick-writes-it`). Fix, G5 `OwnOnly`: the tick's write commits only where the tuple is absent
     (`fga.commit_unless_changed` on the estate's objects, `fga.write_fenced` and `confer` on a project or warehouse, each
     answering whether it wrote), and a tick whose write found the tuple standing forgets the record it made
     (`declaration_converge._grant`, `declaration.record_grant` now answering whether it recorded): a record another tick
     made stays, since that tick wrote the tuple.
   - **F3, the ghost revoke takes the grants of a project made after its judgement** (CONFIRMED, narrowing;
     `OwnedWhileLive`, `GrantsRepairJudgesFirst.cfg`). Who loses what: the creator of a project made under an id whose
     ghosts maintenance's drift report found loses its `admin`, and the project stands with nobody who can administer,
     grant on or delete it. Trace: the repair judges the record absent (`grant_reconcile.py:71`), the project is made and
     born, and `revoke_object_tuples` reads what stands only then and revokes it (`grant_reconcile.py:75`,
     `fga.py:2571`); it answered `{"project:acme": 2}`. Test:
     `services/catalog/tests/test_a_ghost_revoke_takes_nothing_a_project_made_after_its_judgement_holds.py`. Fix, G4
     `ReadFirst`: the repair reads an object's tuples before it judges it (`fga_deps.read_ghost_object`) and revokes only
     what it read (`fga_deps.revoke_ghost_object`, `fga.revoke_read_tuples`): a record minted after the judgement was
     minted after the read, and so was its birth.

6. **What a client can now observe that differs.**
   - A grant at `PUT /management/v1/projects/{id}/members`, `POST /management/v1/projects/{id}/access/grant` or
     `POST /management/v1/warehouse/{id}/access/grant` on an object whose delete has begun, or that no birth bore, is
     answered 404 (code 4, the not-found the doors already answer); one whose project or warehouse was deleted while it
     ran is answered 409 (code 14) and nothing is granted. The estate's own warehouse (`warehouse:lance_catalog`) is
     never deleted and is granted on as before.
   - A project's or warehouse's registry record carries `incarnation`; OpenFGA holds `revision` tuples on both types,
     which confer nothing and which no door lists or revokes. A re-POST of a project or warehouse whose delete has
     retired its token is answered 409.
   - The tenancy rebuild answers a warehouse whose token is retired as unjustified rather than writing its edge.
   - Since §7: grants on one project or warehouse no longer refuse each other (a 409 answers only a delete that ran
     beside the grant). A revoke at the members door, the project's or warehouse's access door, or a role's unassign
     door decided on an object deleted meanwhile is answered 409 and takes nothing. A project or warehouse whose record
     stands and whose incarnation is not born yet (an upgrade not yet converged, a create still running or dead before its
     birth) is answered 409 with `Retry-After: 5` at every grant and revoke door and at its delete, never 404. A role's
     create, a warehouse's create and a namespace's create directly in a warehouse decided on a project or warehouse
     deleted meanwhile are answered 409 and make nothing. A project's delete answers 409 when a warehouse was made in it
     while it ran. The drift repair answers a project or warehouse made while it ran as refused. The audit trail no longer
     records the `revision` relation's tuples (marker, token, tickets, the repair's token, evidence), which grant nothing.
   - `estate#revision` exists (bookkeeping), and a declared grant's ledger record carries the incarnation it was written
     on and, while unsettled, the evidence it waits for.

7. **Nine more findings, by an independent review of the fix above, each FIXED here, model first** (2026-11-17). The
   container part gained the writers, the revokes, the edges, the repair's token, the drain, the contention case and the
   boot convergence, the converge part the tick's death; each new guard is removed by a config that must fail. Each test
   below failed on the code before this section (or fails with its guard removed, where the code before could not run it),
   and passes after it; the guard-removal runs are recorded with each.

   **One fence for every writer (R2, which replaces F1's token).** F1 fenced a grant by moving the one token every write
   on the object moved, three attempts, no backoff, so grants refused grants: measured on the pinned OpenFGA 1.18.3
   (memory datastore) on 2026-11-17, of 16 concurrent grants for distinct people on one live project 9, 10 and 12 were
   refused 409 in three trials, and of 64, 56 each time. The candidates for a condition grants do not contend on, each
   measured on that server (`scratchpad` probe, the same day): a Write that deletes and writes the same tuple (the marker,
   to require it without moving it) is refused 400 `cannot_allow_duplicate_tuples_in_one_request`; a conditional tuple's
   condition is evaluated at Check with the request's context and makes no Write depend on another tuple; contextual tuples
   exist only on Check, ListObjects and ListUsers; and the model's type restrictions are the only thing a Write is
   validated against, so no tuple can depend on another through the model at write time. What OpenFGA does take: a Write
   commits only while each tuple it deletes stands (a delete of an absent tuple is refused 400
   `write_failed_due_to_invalid_input`, the same Write again after it committed is refused so). So each writer brings a
   tuple of its own to delete: G7 **Tickets** (`GrantsSharedToken`, `GrantRefusedOnlyByDelete`). A door writes a ticket
   `revision:ticket-<hex>` on the object, reads the token, asks its caller's authority, and commits in ONE Write that
   deletes the ticket (`fga.open_fence`, `fga.commit_fenced`, `packages/lakehouse/src/lakehouse/governed/fga.py`); the
   delete retires the token and then drains (`fga.end_incarnation`): it reads what stands on the object and what names it,
   deletes every ticket it read, revokes what it read, and reads again until a read finds nothing (G8 **Drain**,
   `GrantsDrainOnce`, `NoLeakAcrossIncarnations`). A writer whose ticket the drain read committed, if at all, before its
   ticket was deleted, so the second read finds it; a ticket written after the retire reads no token, so a third read
   finds nothing: the model's `DrainBounded` holds that the drain finds something on at most one re-read. Measured after,
   same server: 16 of 16 and 64 of 64 concurrent grants succeed (three trials each), and 64 grants beside the delete leave
   nothing standing once the delete answered (three trials). The token stays: it is the incarnation standing, and an
   administration REMOVAL still moves it so two last-admin judgements cannot both commit ([[LH-434]], ADR 0213); a grant
   moves nothing. Test: `tests/integration/test_grants_on_one_project_never_refuse_each_other.py` (16, 64, 64 beside the
   delete); with the drain's re-read removed, the case beside the delete fails.

   - **R1, the drift repair could strip a new project's only admin** (REPRODUCED; `OwnedWhileLive`,
     `GrantsRepairUntokened`). F3's read-first left this: a ghost tuple equal to a new project's seed (its creator's
     admin, left by an earlier project under the id) is skipped by the birth, which writes only what is absent, and the
     repair then revokes the tuple it read, now the project's only admin. Fix, G6 **RepairToken**: the repair writes a
     token of its own (`revision:repair-<hex>`) before it reads, and revokes what it read in ONE Write that deletes it;
     a birth deletes every repair token it finds in its own Write (`fga.bear`, `fga.revoke_ghost`), so a birth after the
     read refuses the revoke and the object is answered refused. Test: the parametrized
     `services/catalog/tests/test_a_ghost_revoke_takes_nothing_a_project_made_after_its_judgement_holds.py`
     (`the-seed-itself`), which answered `{"revoked": {"project:acme": 1}}` and left the project with no admin on the code
     before; with the birth's deletion of the repair token removed, it fails again. G6 alone also holds F3's case: the
     config removing G4 removes G6 too (`GrantsRepairJudgesFirst`).
   - **R3, an upgrade left every existing project and warehouse ungrantable, and nothing converged it** (REPRODUCED by
     the review on the door side; `EveryRecordBorn`, `GrantsUpgradeUnconverged`, a temporal property under weak fairness of
     the catalog's convergence). Fix, G12 **BootConverge**: at boot and on every converge tick, before the declaration is
     read, the catalog gives every project and warehouse record naming no incarnation one, by a write conditional on the
     record that a delete's mark refuses, and bears every incarnation unborn with no seed of its own, every tuple standing
     on the id kept (`services/catalog/src/catalog/services/incarnations.py`, `converge`; `main.converge_at_boot`,
     `declaration_converge.tick`). A door that finds a record standing and no incarnation born answers 409 with
     `Retry-After: 5` (`lakehouse.ns_errors.SettlingError`, `fga_deps.require_fence`), never 404; a delete refuses such a
     record the same way while authorization is on, so no delete runs on an id whose incarnation is being given. The
     invariant is asserted where it is relied on (`incarnations.require_incarnation`: the deletes, the declaration's
     grants, `converge_warehouse_grants`). Test:
     `tests/integration/test_the_catalog_converges_what_git_declares.py::test_an_estate_stored_before_incarnations_converges_at_boot_with_nothing_posted_again`
     (a member grant, the last-admin guard and a declared grant, on records stripped of their incarnation and their
     `revision` tuples); with the boot's and the tick's convergence removed, it fails.
   - **R4, a warehouse could be planted in another tenant's re-created project** (REPRODUCED: on the code before, alice's
     create held in its store's probe answered 200 after acme was deleted and made again by erin, and the warehouse stood
     in erin's acme with alice its owner; `NoLeakAcrossIncarnations`, `GrantsEdgeUnfenced`). Fix, G9 **FenceEdges**:
     every tuple naming an incarnated object as SUBJECT is written under a fence of that object opened before the gate.
     The warehouse create's birth (`fga_deps.seed_warehouse`, `_bear_or_assert`) deletes the project's ticket in its Write,
     and the create checks the fence still stands before it writes its record (`warehouses.establish_warehouse`); a
     declared warehouse's converge opens its own (`converge_warehouse_grants`); the tenancy rebuild fences on the warehouse
     and its project (`fga_deps.write_registry_edge`); a namespace created directly in a warehouse is seeded under a fence
     of the warehouse its request opened before the router's gate (`fga_deps.parent_fence`, a router dependency listed
     before `authorize`; `fga.grant_on_create(fences=)`). A table's and a nested namespace's parent is a namespace's uuid,
     which no create takes again, and needs none. The project's delete also re-reads the registry after its drain and
     refuses (409, nothing deleted) while a warehouse it acquired meanwhile stands. Test: the parametrized
     `tests/integration/test_the_catalog_converges_what_git_declares.py::test_a_warehouse_made_across_a_delete_lands_in_no_tenant_made_again_under_the_id`
     (`its-project`: 409, no edge, no owner, no record); with the fence removed from the birth and the check before the
     record, it fails.
   - **R5, revokes were not fenced** (REPRODUCED: on the code before, alice's revoke of bob's `member` and `decider` held
     after its gate took bob's rungs from the project erin made again under the id; `NoNarrowAcrossIncarnations`, the
     narrowing twin of `NoLeakAcrossIncarnations`, `GrantsRevokeUnfenced`). Fix, G11 **FenceRevokes**: every revoke on an
     incarnated object goes under the fence its door opened before the gate (`members.revoke_member`,
     `access._access_mutate`, `project_admins.withdraw(fences=)`, the role unassign door, the declaration's revoke).
     Test: the parametrized
     `services/catalog/tests/test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py`
     (`members-revoke-member`, `access-revoke-decider`); with a revoke's ticket left out of its Write, both fail.
   - **R6, roles** (the model's UNDECIDED case, now decided: REPRODUCED: on the code before, a role whose create was held
     between its `project` edge and its record answered 201 after the project's delete, and was listed in the project
     erin made again, its edge naming the id). Fix: a role's `project` edge and every assignment are written under a fence
     of the project opened before the gate (`roles.create_role`, `_mint_role`; `roles.assign_role`, `confer(fences=)`); the
     project's delete drains what names it as subject (G10 **ReadsSubjects**, `GrantsDeleteMissesEdges`: a role's edge,
     a warehouse's tenancy edge), then ends every role its registry lists; a create whose record lands after that listing
     finds its edge gone and ends its record (409). Test: the parametrized
     `…test_a_role_made_across_its_projects_delete_is_made_in_no_project_made_again_under_the_id` (`before-its-edge`,
     `between-its-edge-and-its-record`); with the drain's read by subject removed, the second fails.
   - **R7, a converge tick dying between its record and its write** (`GitRevokesOnlyItsOwn`, `GrantsConvergeDies`; the
     model before left it out). Record-first left the record standing without the tuple; a door's grant of the same tuple
     was then recorded git's, and revoked when git dropped it. Fix, G13 **Intents**: the tick records an INTENT naming an
     evidence (`declaration.record_intent`), writes the tuple and the evidence `revision:git-<hex>` in ONE Write that
     commits only where the tuple is absent (`fga.write_evidenced`), and settles the intent by whether the evidence stands
     (`_Converger._settle`): git's where it does, forgotten where it does not, by this tick or the next. A ledger record
     also carries the incarnation it was written on, and a revoke under a later incarnation forgets it rather than
     revoking. `estate#revision` was added so the estate's own grants carry evidence too. Test: the parametrized
     `…test_a_grant_removed_from_git_is_revoked_and_one_made_at_a_door_stands`
     (`after-a-tick-died-between-its-intent-and-its-write`); with an intent settled git's whatever the evidence, it fails.
   - **R8, G2 and G3 had no test that failed without them.** G2: `…test_a_grant_run_whole_while_the_delete_is_held_before_its_retire_outlives_no_delete`
     holds the delete at its retire (`fga.retire_tokens`) while a grant runs whole; with the retire moved after the drain,
     the grant lands after the drain and holds on the project made again. G3: the `itself` case of the warehouse test
     holds alice's create between its record and its birth while the warehouse is deleted and made again by erin; with the
     delete's marking of the incarnation removed, the stalled birth bears it and alice owns erin's warehouse.
   - **R9, `admin_revision` in two test docstrings** (`test_a_project_always_keeps_an_administrator.py`,
     `test_a_project_admin_manages_a_role_whose_grant_reaches_its_assignees.py`): rewritten to the token.

   **Every tuple writer in `services/` and `packages/`, and whether it is fenced.** Fenced (`fga.commit_fenced`,
   `fga.bear(fences=)`, `project_admins.confer`/`withdraw(fences=)`, `fga.write_evidenced`): the members door's grant and
   revoke; the access door's grant and revoke on a project or warehouse; the role create's edge, the assign and unassign
   doors; the warehouse create's birth; a namespace's seed directly in a warehouse; the declaration's grants and revokes on
   a project or warehouse; the tenancy rebuild; `converge_warehouse_grants`; `_bear_or_assert`'s write of a seed under a
   born incarnation. Bookkeeping, arbitrated by OpenFGA itself: `fga.bear` (commits only while the marker is absent),
   `fga.end_incarnation` and `retire_tokens` (the delete), `fga.revoke_ghost` (the repair's token), `open_fence`'s ticket
   and `close_fences`. Unfenced, and why each is safe: every write or revoke on a table's or namespace's uuid object
   (`grant_on_create` with a namespace parent, the access door on a table or namespace, the rename's edge moves, the
   cascades and drops, the trash release, `take_back_seed`): no create takes a uuid again; the estate's own objects
   (`estate:<root>`, `warehouse:lance_catalog`, the provenance namespace): no door deletes them; the estate admin's raw
   tuple editor (`access_admin`): its gate is on the root, so nothing it writes was decided on an incarnation; the
   offboarding door (`subject_tuples`) and a role's ending (`role_grants.end_role`): they act on a principal or on a role's
   uuid, not on a decision about a project; `revoke_undeclared` of the release's own accounts on a warehouse
   (`converge_warehouse_grants`): the release's settings decide it, the same for every incarnation; the warehouse's
   managed-access flag (`access._set_managed_access`) is NOT fenced yet (see What is not modelled).

   **What an install and an upgrade from git do.** An install: the chart starts the catalog; every project and warehouse
   is made at a door or by the declaration's converge, each minted with an incarnation and born in one Write with its
   seed. An upgrade: the new catalog starts with records naming no incarnation and no `revision` tuples; at boot, in the
   background, before the warehouse grant backfill, `incarnations.converge` gives each record an incarnation (one
   conditional record write) and bears it (one Write: marker and token, every standing tuple kept), then the backfill
   writes the warehouses' cascade tuples under fences; until a record is reached its doors answer 409 with `Retry-After`.
   The converge tick (every `catalog.declarationConverge.periodSeconds`, 60 s by default) runs the same convergence first,
   so a record whose create died before its birth is born within one tick, with no seed of its own: its creator's admin is
   written when the create is asked again. Nothing is POSTed again and no hand-run step exists.

## What is not modelled

- The last-admin guard (ADR 0213), which moves the token in its own CAS; only administration removals move it now.
- A role's record left by a create that dies between its edge and its record after the project's delete revoked the
  edge: the record confers nothing (no edge, no assignee) and is listed under the next project made under the id until
  ended at its door. The record is the registry's and no fence reaches it.
- A warehouse's record written, and its birth refused, when its project is deleted in the one record write between the
  create's last fence check and its birth: the record names the project and stands with no tuple; the project's delete
  refuses while it stands, and once that delete has finished the record is listed under a project made again under the
  id. The same window and harm as the role's record above, narrower, and reported here rather than closed.
- Materialized views created directly in a warehouse (their parent edge names the warehouse) are not fenced as a
  namespace there is: no door creates one directly in a warehouse today.
- The warehouse's managed-access flag is fenced at its set door; its describe door reads only.
- Principals: the offboarding door revokes what it reads, and a grant landing after that read is a grant made after the
  offboarding, which a re-run takes (`access_admin.delete_principal`, by design).
- Labels, classifications, namespaces' Overwrite and the rename's parent edge (ADR 0227's rename and [[LH-568]]).

## Consequences

- GitOps: what git declares converges and nothing a door made is revoked by a reconcile (G5); the declaration's grants
  on a project or warehouse are fenced by the incarnation its record names, so a converge never writes onto another
  tenant's object made under a declared id.
- Cloud native: nothing new runs; the incarnation is a field of the record and a relation of the model, and the boot
  backfill writes under it.
- Lance-namespace/table-idiomatic: untouched; tables and namespaces keep their uuid objects.
- Zero trust: a grant authorizes nothing beyond the object it was decided on (G1–G3), and a repair never revokes the
  grants of an object made after it judged (G4, G6). Every guard is a conditional OpenFGA Write, so it holds across
  replicas and needs no lock. Since §7: nothing decided on a dead incarnation lands on a live one, widening or narrowing
  (G1, G9, G11), and a writer's death leaves only a ticket that grants nothing and that the object's delete removes; a
  ticket is written by an authenticated caller before the gate and taken back when the request ends, whatever it answered.
- GitOps, since §7: an upgrade from git converges every record's incarnation at boot and on each tick with no hand-run
  step (G12), and a reconcile revokes only a tuple its own Write created, whatever step a tick died at (G13).
- Cloud native, since §7: nothing new runs; the convergence is a step of the catalog's existing boot loop and converge
  timer.
- The model change (`revision` on `warehouse`, `admin_revision` renamed `revision` on `project`) is recorded in
  `packages/lakehouse/src/lakehouse/governed/auth/CHANGELOG.md`; `model.fga.yaml` holds that a revision grants nothing.
