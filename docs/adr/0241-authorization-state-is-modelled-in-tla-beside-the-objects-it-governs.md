# 0241. Authorization state is modelled in TLA+ beside the objects it governs; three defects found and fixed, then nine a review found, then five a second review found, then six a third review found, then six a fourth review found (2026-11-18)

**Status:** accepted; the catalog's sixth model (ADR 0221, ADR 0227, ADR 0229, ADR 0233). It describes the Python
catalog as it runs, before it is ported (ADR 0217 §5). Three findings were confirmed against the running catalog and a
real OpenFGA, and each is fixed, model first (§5). An independent review of that fix found it incomplete; its nine
findings are fixed here, model first (§7), and §7's fence replaces §5's token as what a grant is fenced by. A second
independent review of §7 found five more; each is fixed, model first (§8): a fence reads only what it needs, a caller is
asked its authority before it writes anything, no ticket outlives its request, a record naming a parent names the
parent's incarnation, and the convergence no longer repairs records stored before incarnations. A third independent
review of §8 found six more; each is fixed, model first where the model covers it (§9): a record naming a project and no
incarnation of it is an error wherever it is read, a project's warehouses and its policy are its standing incarnation's
alone, a dead tenant's bucket is released or purged rather than blocked for ever, the second ask of both fenced doors has
a test, and the converge tick's cost is what needs work rather than the estate's size. A fourth independent review of §9
found six more; each is fixed, model first where the model covers it (§10): bytes are deleted only under a claim their
purger holds, an ended record's id is taken only once its claim is released and a claim no record names is reached by
its bucket, one unreadable record stops no tick, a purge is audited before and after it, and a bucket is released holding
nothing, to no create that outlived its claim.

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
     ticket (`fga.fenced`, `fga.commit_fenced`); the delete retires and then drains until a read finds nothing
     (`fga.end_incarnation`); the repair writes a token a birth deletes (`fga.revoke_ghost`); a record whose birth never
     landed is born by the catalog's convergence (`incarnations.converge`); and a token shared by every grant is a config
     of its own (`GrantsSharedToken`). Since §8: a writer asks its gate before its ticket and again after its read
     (`Intruders`, a caller the container authorizes for nothing), the convergence sweeps a dead writer's ticket, and with
     `Child` the edge writer is a warehouse's create, which writes its registry record (naming the project incarnation its
     fence read) between its last fence check and its birth, beside the boot backfill and the end of an orphan record.
   - **table**: one name, its tables keyed by a minted uuid (`fga.table_object`): a create seeds the owner
     (`fga_deps.seed_ownership`), a purge revokes, a recoverable drop keeps the grants for the undrop, the undrop
     admitted before the deadline registers later, the trash release judges the name free and revokes past
     `trash.RELEASE_SETTLE` (`trash_reclaim.release_trash`), a grant decided on one table lands late, the access door
     revokes it, and a reader decides on what OpenFGA answers (`fga.check`).
   - **converge**: one tuple git declares and later drops (`declaration_converge._Converger._grant` and `_revoke`),
     and the same tuple granted at a door at any point between; since §7 the tick may die between any two of its steps
     and records an intent its evidence settles.

2. **What the gate proves** (`make tla-check`; measured 2026-11-18 on two workers at load 2, after §8):
   - `Grants.cfg` (container: two incarnations, a door grant, the drift repair, any door dying; with `DeadTicketsSwept`):
     102,275 states, 9.1 s.
   - `GrantsContention.cfg` (two grants beside the delete): 23,930 states, 3.7 s; `GrantsContentionDies.cfg` (two grants
     beside the delete over three incarnations, any door dying): 1,642,378 states, 33.2 s.
   - `GrantsEdges.cfg` (a tuple naming the container as subject beside the delete, any door dying): 6,565 states, 2.0 s.
   - `GrantsRevoke.cfg` (a grant and its revoke beside the delete, any door dying): 248,524 states, 5.5 s;
     `GrantsRevokeEdges.cfg` (a grant, its revoke and an edge beside the delete): 600,335 states, 10.9 s.
   - `GrantsRepair.cfg` (a ghost equal to the next seed, the repair, any door dying): 6,259 states, 1.9 s;
     `GrantsRepairGrant.cfg` (the same with a door grant): 318,650 states, 6.7 s.
   - `GrantsIntruder.cfg` (a caller authorized for nothing beside a granter and the delete, any door dying; with
     `DeadTicketsSwept`): 13,130 states, 3.2 s.
   - `GrantsChild.cfg` (a warehouse's create, its bucket claim and its record, the backfill, the convergence's fenced
     birth, the end of an orphan and the release of its claim, the estate admin's purge, beside the project's delete and
     re-creation, any door dying; with `BornLeftKept`, `DeadTicketsSwept` and `ClaimFreed`): 28,983 states, 9.2 s (§9).
     Since §10 it adds another tenant's claim on the bucket, the purge in two steps by record or by bucket, the child's id
     made again on another bucket and bytes no create wrote, with `PurgeTakesNothingLive` and `ClaimNamedByItsRecord`:
     102,242 states, 40 s. Its §10 guards removed: `GrantsPurgeUnstamped` fails in 6 s, `GrantsEndedKeyTaken` in 7 s,
     `GrantsClaimUnaddressed` (temporal) in 32 s.
   - `GrantsUnborn.cfg` (a create dying between its record and its birth, a grant and a delete; with `EveryRecordBorn`):
     6,565 states, 1.7 s.
   - `GrantsTable.cfg` (table: purge, recoverable drop, undrop, release, a late grant, a revoke and a reader): 440
     states, 0.9 s.
   - `GrantsConverge.cfg` (the tick dying between any two steps): 33 states, 1.1 s.
   - ON DEMAND: `GrantsWide.cfg` (three incarnations, a grant and an edge beside the delete, any door dying) and
     `GrantsRevokeEdgesDies.cfg` (a grant, its revoke and an edge, any door dying: 11,352,899 states, about 5 minutes).
   - Each guard removed (§3, §5, §7, §8, §9): its config fails in under 12 s.

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
   - Since §8: `OnlyAuthorizedWrites` (a caller the incarnation authorizes for nothing writes no ticket and no tuple on
     it) and `DeadTicketsSwept` (temporal: no ticket of a dead request stands for ever, on any id since §9, under weak
     fairness of the sweep); `NoLeakAcrossIncarnations` now covers the tenancy edge a backfill writes from a record.
   - Since §9: `BornLeftKept` (while a born record of a dead project incarnation stands, its bucket's claim names that
     incarnation, so no tenant reads or writes what it left) and `ClaimFreed` (temporal: a bucket git declares for the
     tenant standing is eventually claimable once its dead claimant is ended unborn, under weak fairness of every
     request's steps and of the convergence's birth, and strong fairness of the tick's end of an orphan and release of a
     claim, each enabled again whenever the tick's other steps finish).
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
   - Since §8: a caller with no right on a project or warehouse is answered at a fenced door exactly as before (403, or
     404 where the door hides the object) and has written nothing; a caller with the right is asked twice, before its
     fence and on the incarnation it read, so a fenced write costs two Checks. A project or warehouse whose incarnation is
     not born yet is refused 403/404 to whoever holds no right on it, and 409 with `Retry-After: 5` to one who does. A
     record naming no incarnation is answered 500 (code 18) at the deletes, the declaration's grants, the tenancy rebuild
     and the backfill, and is reported among a converge tick's failures, where it was 409 and repaired. A warehouse's
     record and its bucket's claim, and a role's record, carry `project_incarnation`; a record a dead tenant's create
     left is listed under no project made again under the id, its delete door answers it 404, a re-create of its id is
     refused 409 (code 2) until the converge tick ends it, and its bucket is refused (409, code 2) to every other project
     and to the project made again. `project_deleted` and `warehouse_deleted` carry the `incarnation` they ended, and the
     audit trail holds an `access_revoke` row with relation `revision` and `reason: incarnation_retired` per delete. A
     door's ticket is a `fence:<hex>` object (`type fence`) naming the project or warehouse, so the estate admin's tuple
     browser shows tickets under `fence:` and none on the project or warehouse.

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
     record that a delete's mark refuses (§8 B5 deletes this half: a record naming none is an internal error), and bears
     every incarnation unborn with no seed of its own, every tuple standing
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
   (`converge_warehouse_grants`): the release's settings decide it, the same for every incarnation. The warehouse's
   managed-access flag (`access._set_managed_access`) is fenced: its set door opens a fence on the warehouse between its
   gate's two askings and writes through `fga.commit_fenced`.

   **What an install and an upgrade from git do** (as §8 B5 leaves it). An install: the chart starts the catalog; every
   project and warehouse is made at a door or by the declaration's converge, each minted with an incarnation and born in
   one Write with its seed. An upgrade from a catalog that already minted incarnations changes nothing in the store. An
   estate whose records predate incarnations is re-created from git: the declared projects and warehouses converge from
   the chart, and door-made ones are made again; such a record is an internal error where its incarnation is relied on,
   and the convergence reports it and repairs nothing. The converge tick (every `catalog.declarationConverge.periodSeconds`,
   60 s by default) runs the convergence first, so a record whose create died before its birth is born within one tick,
   with no seed of its own: its creator's admin is written when the create is asked again; and it sweeps the tickets and
   ends the orphan records §8 names. No hand-run step exists.

8. **Five more findings, by a second independent review of §7, each FIXED here, model first** (2026-11-18). §7 holds
   for doors that finish (a delete is not blocked by new tickets and no grant outlives it, on the memory datastore and
   on Postgres); the review found what it costs, who could write it, what a request that does not finish leaves behind,
   and one window. The model gained intruders, the double ask, the sweep, a warehouse's record under its project as a
   second container level with its backfill and its end, and lost the record stored before incarnations (B5). Each test
   below failed on the code before this section, or, where the code before could not run it, fails with its guard
   removed; every guard-removal run is recorded with it.

   - **B1, a fence read every tuple on its object, and refused 503 once the object held more than 5,000** (REPRODUCED;
     a regression §7 introduced). `fga.open_fence` read the incarnation with `read_object_tuples`, 50 tuples a page and
     an outage at 100 pages, and every top-level namespace stores a `child` edge on its warehouse and every member a
     tuple on its project, so one namespace create directly in a warehouse cost what the warehouse's namespaces cost to
     read, and the same read stood in front of every fenced door's gate and of the delete's drain. Fix, G18 **NarrowReads**:
     a fence reads exactly what it needs. The incarnation is read from the `revision` relation alone, filtered on the
     server (`fga.read_incarnation` → `fga.read_relation`, the ONE read of an incarnation; it holds a marker per
     incarnation ever born under the id and one token); a fence's ticket is read back by its key (`fga.fences_stand` →
     `fga.holds`); `commit_fenced`, `bear`, the declaration's reads and `project_admins` read each tuple they change by
     its key, or the one relation they judge (`admin`, `revision`); and tickets live off the object (B2). The delete's
     drain, which must read everything, pages to the end with no ceiling (`fga.read_all`: a ceiling turned size into an
     outage; a server handing back a cursor it handed back before is refused instead), bounded by its request, and a
     delete cut off is asked again and finishes. Test:
     `tests/integration/test_a_fence_reads_nothing_that_grows_with_its_object.py::test_a_namespace_is_made_in_a_warehouse_holding_more_namespaces_than_a_read_once_took`
     (5,100 child edges); with the whole-object read and the page ceiling put back, it fails (and so does its sibling).

     The cost of one namespace create directly in a warehouse against its sibling count, p50 of 11, on the pinned
     OpenFGA 1.18.3 (measured 2026-11-18, the reviewer's own script for the code before, `scratchpad/measure_after.py`
     after; the fence's steps alone as the reviewer measured them, and after with the two Checks of B2 added; Postgres
     16 local):

     | siblings | memory before | memory after | memory after, both asks | Postgres before | Postgres after | Postgres after, both asks |
     | -------- | ------------- | ------------ | ----------------------- | --------------- | -------------- | ------------------------- |
     | 0        | 9.7 ms        | 11.6 ms      | 16.4 ms                 | 20.7 ms         | 14.6 ms        | 21.7 ms                   |
     | 1,000    | 109.8 ms      | 11.7 ms      | 23.9 ms                 | 121.4 ms        | 14.7 ms        | 21.0 ms                   |
     | 4,990    | 513.1 ms      |              |                         | 726.7 ms        |                |                           |
     | 5,100    | 503 for good  |              |                         | 503 for good    |                |                           |
     | 10,000   | (503)         | 25.3 ms      | 31.6 ms                 | (503)           | 19.7 ms        | 24.5 ms                   |

     The fence's reads no longer grow with siblings. What is left on the memory datastore at 10,000 is the datastore's
     own scan (its Read filters every tuple of the store in memory, whatever the filter), which Postgres's index does
     not pay.

   - **B2, a caller with no right on an object wrote on it before the gate, and a ticket a request left was never
     removed** (REPRODUCED against the catalog and OpenFGA). The doors opened the fence (a Write) before they asked the
     caller's authority, so a caller refused 403 on project acme had written its ticket there and paid a paginated read
     of another tenant's object; `close_fences` swallowed an outage, a process could die mid-request, and
     `_bear_or_assert` took its fences back only on one error, so `converge_warehouse_grants` left a project ticket at
     every boot backfill that met an OpenFGA hiccup; nothing swept tickets on a live object; and with B1 every ticket
     left slowed every door on its object, past 5,100 wedged it, and the delete removed them one Write each. That broke
     zero trust: an unprivileged caller could write on, slow and wedge another tenant's object. Fixes:
     - G14 **AuthorizeFirst** and G15 **Recheck** (`GrantsIntruderFenced` fails `OnlyAuthorizedWrites`, "only a caller
       the object's incarnation authorizes writes on it"; `GrantsNoRecheck` fails `NoLeakAcrossIncarnations`): a door
       asks its gate with no write, then opens its fence, then asks its gate again on the incarnation the fence read
       (`fga_deps.door_fence(gate=)`, the members door, the access doors' `*_grant_fence` / `*_revoke_fence`
       dependencies, the role doors, the warehouse create, the warehouse managed-access door; `fga_deps.parent_fence`,
       now listed AFTER `authorize` on the router and asking `_decide` again once its fence is open). The second ask is
       what the model's property needs: the authority must hold on the incarnation the write commits to. Test:
       `services/catalog/tests/test_a_caller_refused_at_a_fenced_door_writes_nothing_and_no_ticket_outlives_its_request.py::test_a_caller_the_project_authorizes_for_nothing_is_refused_having_written_nothing_there`
       (members grant and revoke, the access grant, the role create; the store's changelog holds no change by the
       refused request); with the first ask removed, all four fail.
     - G16 **Sweeps** (`GrantsTicketsLeft`, temporal `DeadTicketsSwept`: no ticket of a dead request stands for ever on
       an id a record names). A ticket is `<object> ticket fence:<hex>`, a tuple on an object of its own (`type fence`,
       `define ticket: [project, warehouse]`, bookkeeping no permission reads), so nothing that reads the project or
       warehouse reads it. The ONE way a fence is opened is the context manager `fga.fenced` (§7's `open_fence` and
       `close_fences` are gone), which takes the ticket back
       on every exit (`delete_absent_ignored`, one Write, an outage logged `fence_ticket_left`); every opener uses it
       (`door_fence`, `parent_fence`, `_bear_or_assert`, `write_registry_edge`, `converge_warehouse_grants`, the
       declaration's `_fenced`). What a dead process or an unanswered take-back leaves, the converge tick sweeps
       (`incarnations.converge` → `fga.sweep_tickets`): at boot and on every tick it deletes, in Writes of 100, each
       ticket naming a project or warehouse a record names that the store wrote more than the bound ago, the bound being
       the request deadline plus five minutes (`incarnations.ticket_bound`, 10 minutes at the default 300 s deadline),
       judged on the timestamp the store returns with each tuple against the catalog's clock. A door still running past
       the bound (a door runs on past its deadline, `lakehouse.deadline`) has its ticket deleted and its write refused
       409: narrower, never wider. Tests: `…::test_no_ticket_outlives_the_request_that_wrote_it`
       (`its-write-failed`: a door whose commit OpenFGA did not answer took its ticket back, with the take-back moved
       out of `finally` it fails; `its-process-died`: a ticket a dead process left stands within the bound and is swept
       past it, with the sweep removed it fails).
     - G8's batch: the drain deletes the tickets it read in Writes of `TUPLES_PER_WRITE` (100, OpenFGA 1.18.3's
       `maxTuplesPerWrite`: measured 2026-11-18, a Write of 100 commits and one of 101 is refused 400
       `exceeded_entity_limit`) with `on_missing_deletes: IGNORE` (measured: a Write deleting a standing and an absent
       ticket commits and removes the standing one), so a delete no longer pays a round trip per ticket. Test:
       `tests/integration/test_a_fence_reads_nothing_that_grows_with_its_object.py::test_tickets_requests_left_on_a_project_slow_no_grant_and_its_delete_drains_them`
       (5,100 tickets left on a project: a member grant commits, the delete revokes it and leaves no ticket); on the
       code before both answered 503.

     A project holding tickets requests left: its whole read (a members listing), a member grant, and its delete (p50
     of 5 for the read and the grant, one delete; the grant without the Checks, as the reviewer measured it):

     | tickets left | memory before: read / grant / delete | memory after | Postgres before | Postgres after |
     | ------------ | ------------------------------------ | ------------ | --------------- | -------------- |
     | 0            | 2 / 13 / 21 ms                       | 1.8 / 13.4 / 39 ms | 3 / 32 / 38 ms | 2.9 / 18.4 / 46 ms |
     | 1,000        | 103 / 215 / 2,239 ms                 | 1.9 / 16.6 / 225 ms | 119 / 214 / 3,207 ms | 2.2 / 16.9 / 331 ms |
     | 4,900        | 533 / 877 / 13,739 ms                |              | 750 / 1,462 / 16,620 ms |        |
     | 5,100        | 503 / 503 / 503                      |              | 503 / 503 / 503 |                |
     | 10,000       | (503)                                | 3.1 / 32.0 / 2,640 ms | (503) | 2.4 / 27.6 / 4,734 ms |

     A planted ticket no longer reaches a read or a grant of its object (the memory datastore's own scan is what grows
     at 10,000); the delete reads the tickets it must delete and removes them 100 a Write, and leaves none.

     A member grant for distinct people on one project, p50 (p95), none refused in any run:

     | concurrency | memory before | memory after | memory after, both asks | Postgres before | Postgres after | Postgres after, both asks |
     | ----------- | ------------- | ------------ | ----------------------- | --------------- | -------------- | ------------------------- |
     | 1           | 13.7 (16.9) ms | 13.7 (16.9) ms | 18.5 (20.0) ms      | 16.6 (19.0) ms  | 23.8 (30.2) ms | 22.5 (28.5) ms            |
     | 16          | 229.5 (260.4) ms | 173.0 (188.4) ms | 157.5 (230.7) ms  | 229.4 (306.0) ms | 157.1 (215.7) ms | 165.0 (212.7) ms        |

     The machine was shared with other work (load 4 to 10 on 4 cores) during every run; differences under a few
     milliseconds are noise.

   - **B3, a warehouse record left by a create that raced its project's delete was taken over by the next tenant under
     the project's id** (the takeover REPRODUCED against the catalog and OpenFGA with the window held). The create's last
     fence check was followed by its record write; a record landing after the project's delete re-read the registry
     survived, naming project acme with an unborn incarnation; the convergence bore it with an empty seed, and the boot
     backfill opened a fence on whichever incarnation of `project:acme` stood and wrote `project:acme project
     warehouse:wh-a`: the new tenant's admins owned a warehouse, its name, bucket and catalog row, another tenant's admin
     made. §7 called the leftover record harmless; it was the leak class the fence exists to close. Fix, G17
     **NamesIncarnation** (`GrantsChild` holds; `GrantsChildUnnamed`, the record naming the project's id and no
     incarnation, fails `NoLeakAcrossIncarnations`): a record that names a parent which can be deleted and made again
     names the incarnation of the parent it was made under (`control_records.PROJECT_INCARNATION`): a warehouse's
     record and its bucket's claim, taken from the fence the create held (`warehouses.establish_warehouse`,
     `claim_bucket`), and a role's record (`roles.create_role_record`, from the role create's fence). Every reader that
     would write an edge or act from such a record refuses one whose project incarnation is not the one standing:
     `converge_warehouse_grants` (its fence is opened on the record's project incarnation, `fga.fenced(incarnation=)`,
     and an orphan is reported and refused 409), `incarnations.converge` (bears no orphan), the tenancy rebuild
     (`write_registry_edge`), the warehouse's re-create and upsert (`_claim_the_bucket`, `upsert_warehouse`'s
     `WarehouseOfADeadTenant`: 409), the delete door's gate (`administered_warehouse`: 404 to the live tenant, which may
     neither change nor purge what a dead tenant made), the project's listing and its delete's emptiness checks (an orphan
     is no warehouse of the live incarnation), and the role listing and gates (`roles.list_roles` / `find_role`
     `incarnation=`). Each orphan is reported each time it is found (`catalog.incarnations.orphans`, the log
     `record_of_a_dead_tenant` naming the record). The converge tick ends it once it is older than the same bound as
     G16 and unborn: its registry record goes and its id is free; a role's record is stamped ended. **What it never
     touches:** a warehouse's bucket, its bytes and its catalog row stay, and so does its bucket's claim, which names the
     dead incarnation, so no tenant registers that bucket over what it may hold (logged
     `warehouse_of_a_dead_tenant_ended` naming the bucket); an orphan that is born, which may hold grants a door made, is
     reported and kept; with authorization off, where no birth tells whether a door made anything in it, an orphan is
     only reported; and nothing is ended while a project record cannot be read. Test (the window held: alice's
     create between its last fence check and its record write while acme is deleted and erin makes it again):
     `tests/integration/test_the_catalog_converges_what_git_declares.py::test_a_warehouse_record_a_create_held_across_its_projects_delete_wrote_reaches_no_tenant_made_again_and_is_ended`;
     with the convergence's check removed it fails (the orphan is born, its tuples stand), and with the backfill's fence
     opened on whichever incarnation stands it fails (the edge is written onto erin's acme).

     The role record §7 left (a create dying after the project's delete revoked its edge): harmless as §7 said (no
     edge, no assignee), but not "ended at its door": its door answers 404, since its gate needs the project edge. Its
     record names the incarnation it was made in, so the listing of the project made again does not list it and its name
     is the new tenant's to take once the converge tick ends it past the bound. Test: the parametrized
     `services/catalog/tests/test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py::test_a_role_made_across_its_projects_delete_is_made_in_no_project_made_again_under_the_id`
     (`and-dying-after-its-record`: the create held between its edge and its record across the delete and the
     re-creation, then dying once its record is written); with the listing's incarnation filter removed it fails.

   - **B5, the convergence's "give a record an incarnation" was a migration shim that ran for ever** (CLAUDE.md: no
     migration shims, no dual read paths; the estate is pre-production and current state is test data). Every create
     mints an incarnation with its record, so only a record stored before incarnations ever reached it. Deleted:
     `incarnations._with_incarnation` and `_give`, and the merges' "a record written before incarnations takes the
     request's" (`projects.upsert_project`, `warehouses.upsert_warehouse`). A record naming no incarnation (or a
     warehouse naming no project incarnation) is a typed internal error naming it (`incarnations.require_incarnation`,
     `require_project_incarnation`, `InternalError`, 500 code 18, logged `record_without_incarnation`) wherever its
     incarnation is relied on: the deletes, the declaration's grants, `require_fence`, the backfill, the tenancy
     rebuild, the convergence (which reports it among the tick's failures). It is never a 409 and never repaired. Kept,
     because it is a real invariant: a create can die between its record and its birth, so an UNBORN incarnation is born
     by the convergence at boot and on every tick with no seed of its own (G12, `GrantsUnborn` holds,
     `GrantsUnbornUnconverged` fails temporal `EveryRecordBorn`), and the create asked again writes its creator's admin.
     **An estate whose records predate incarnations is re-created from git:** the declared projects and warehouses
     converge from the chart (`catalog.declared`), and door-made ones are made again, as the repository's rule says.
     Test, rewritten from the one that pinned the upgrade:
     `…::test_an_incarnation_whose_birth_never_landed_is_born_by_the_catalog_and_a_record_naming_none_is_refused`
     (`boot`, `tick`); with the convergence's birth removed both fail.

   - **B4, two guards had no test that reliably failed without them.** G8, the drain's read again: with it removed the
     64-grants-beside-the-delete case failed in 2 of 4 runs. Now
     `services/catalog/tests/test_a_grant_on_a_deleted_project_reaches_no_project_made_again_under_its_id.py::test_a_grant_committing_between_the_drains_first_read_and_its_ticket_deletion_outlives_no_delete`
     holds bob's grant between its ticket and its commit and the delete between its drain's first read and its deletion
     of the tickets it read, releases the grant, then the delete: with the read again removed it fails, every run. G9b,
     a namespace seeded under its warehouse's fence: with `fences=()` in `seed_ownership` every test passed. Now
     `…/test_the_catalog_converges_what_git_declares.py::test_a_namespace_made_in_a_warehouse_across_its_delete_lands_in_no_warehouse_made_again_under_the_id`
     holds alice's namespace before its seed while `wh-a` is deleted with what it holds and erin makes `wh-a` again: with
     the fence removed from the seed it fails.

   - **The model's coverage.** `GrantsWide` was narrowed until it finished. Added, each in the gate: two grants beside
     the delete with deaths over three incarnations (`GrantsContentionDies`), a grant, its revoke and an edge beside the
     delete (`GrantsRevokeEdges`, no deaths), the drift repair with a ghost and a granter (`GrantsRepairGrant`), an
     intruder beside a granter with deaths (`GrantsIntruder`, with `DeadTicketsSwept`), and the child record
     (`GrantsChild`). On demand: the revoke with edges and deaths (`GrantsRevokeEdgesDies`, 11,352,899 states, about 5
     minutes) and `GrantsWide`. Unchecked, and why it cannot change a verdict: revokers with edges and deaths at three
     incarnations, and the repair with every writer: each writer's steps touch only its own ticket and its own tuple, and
     the delete's drain reads them all, so a third incarnation adds a state no two-incarnation trace lacks a twin of
     (every leak trace the gate's failing configs report needs two incarnations).

   - **The audit trail.** The trail leaves the `revision` relation's tuples out, so an incarnation's retirement was
     recorded nowhere. Now `fga_deps.end_incarnation` writes its own row (`access_revoke`, relation `revision`, the
     `incarnation`, `reason: incarnation_retired`, the count revoked) for both deletes, and `project_deleted` and
     `warehouse_deleted` carry the incarnation in their `extra`.

   Each fix meets the four facts: GitOps (B3's sweep and B2's ticket sweep run in the catalog's existing converge loop
   and end only what is the catalog's own bookkeeping, a dead request's ticket and a dead tenant's unborn record; B5
   re-creates from git rather than repairing state), cloud native (nothing new runs; the bound is derived from a setting
   the release already declares), Lance-namespace/table-idiomatic (untouched), zero trust (B2: an unauthorized caller
   writes nothing and a death leaves access narrower; B3: nothing a dead tenant made reaches a live one; B1: no tenant's
   size refuses another's door).

9. **Six more findings, by a third independent review of §8, each FIXED here** (2026-11-18). Each test below failed on the
   code before this section (P1, P2, P3 and P5 reproduced; P4 by mutation), or, where the code before could not run
   it, fails with its guard removed; every guard-removal run is recorded with it. The model gained the child level's
   bucket claim, its ended record, the convergence's two-step birth of a child record and the estate owner's purge, and
   its sweep no longer needs a record to name the ticket's id.

   - **P1, a project holding a warehouse record that names no project incarnation was deleted, the record left
     standing** (REPRODUCED: `registry/warehouses/wh-legacy.json` for `acme`, an `incarnation` and no
     `project_incarnation`: `GET /management/v1/projects/acme` answered 200 with `"warehouses": []`, and `DELETE` 200 with
     the record standing). The comparison `r.get(PROJECT_INCARNATION) == project.get(INCARNATION)` read a record naming
     none as another tenant's. Fix, the invariant asserted wherever a record's project incarnation is compared, through
     ONE comparison, `incarnations.made_under` (`services/catalog/src/catalog/services/incarnations.py`), which raises
     `InternalError` (500, code 18, logged `record_without_project_incarnation` naming the record), and ONE listing of a
     project's warehouses built on it, `incarnations.warehouses_of`. Every comparison of a record's project incarnation in
     the catalog goes through it: seven sites compared one before (the project's listing, its delete's emptiness and late
     re-read, the merge, the bucket claim, the re-create, the tenant's delete gate, the convergence's warehouse), and
     seventeen call sites ask it now, the seven and ten readers that compared none: the project's listing
     (`projects._load_tenants`), its delete's emptiness and its late re-read (`projects._warehouses_of`), the policy door's
     buckets and the policy listing (`policies.set_project_policy`, `list_project_policies`), a stored project policy
     (`policies._of_incarnation`, the project's delete), the re-create (`warehouses._claim_the_bucket`), the merge
     (`warehouses.upsert_warehouse`), the bucket claim and its release (`warehouses.claim_bucket`, `release_claim`), the
     tenant's delete gate (`warehouses._of_a_dead_tenant`), the purge's claim and named siblings
     (`warehouses._require_bucket_purgeable`), and the convergence (`incarnations._Convergence._warehouse`, `_claim`,
     `_policies`). A role's record names its project incarnation as a required field of its model (`roles.StoredRole`),
     so a record naming none is unreadable there already; maintenance reads a policy's incarnation against the claim's in
     `lakehouse.maintenance_policies.governing_policy`, which cannot import the catalog. No caller reaches the 500 with
     input alone: a create's record names the incarnation its fence read, and a body naming `project_incarnation` is
     refused 422 (code 13). Test:
     `tests/integration/test_a_dead_tenants_records_reach_no_live_tenant.py::test_a_warehouse_record_naming_its_project_and_no_incarnation_of_it_is_refused_where_it_is_read_and_never_hidden`;
     with `made_under` reading a record naming none as another tenant's, it fails.

   - **P2, a live tenant's project policy governed a dead tenant's bucket** (REPRODUCED: an orphan record naming acme at
     a dead incarnation alone satisfied "the project has an active warehouse", and `set` answered 200 with its bucket in
     `"buckets"`). Fix: the policy door's buckets and the policy listing are `incarnations.warehouses_of` the standing
     incarnation, and no `== project` filter over warehouse, role, policy or claim records remains in the catalog: the
     three left compare a project id with no incarnation to judge (`set_warehouse_protection` and `upsert_warehouse`
     refuse a record moved to another project, `projects_claiming_bucket` names rival projects) or are a label's
     (`object_labels`, outside this review). One level over: a project's policy record names the incarnation that set it
     (`project_incarnation`, `lakehouse.warehouse_records.PROJECT_INCARNATION`, the one definition of the field); a
     record an earlier incarnation of the id left is not the live tenant's to read (`describe` answers `set: false`),
     list or merge onto, it governs no bucket whose claim names another incarnation
     (`maintenance_policies.governing_policy`, the one read maintenance decides by), the project's delete ends only its
     own incarnation's record, and the converge tick reports a leftover one (`record_of_a_dead_tenant`,
     `policy:project:<id>`) and does not end it: a removal under the project's id could only be unconditional (the store
     takes no conditional delete), and would take a policy the tenant holding the id set meanwhile, a hold among them;
     the next set under the id replaces it. A project policy record naming no
     incarnation is malformed (`_record_is_well_formed`), which refuses what it may govern. A namespace's policy is keyed by
     the namespace's uuid, which no create takes again, so it is scoped already. Test:
     `…::test_a_project_policy_governs_only_the_buckets_of_the_incarnation_that_set_it`.

   - **P3, a dead tenant's bucket was blocked for ever and no door released it** (REPRODUCED with the held interleaving:
     alice's create of `wh-a` held between its last fence check and its record across acme's delete; git then declares
     acme and `wh-a` on the same bucket, and every tick failed `warehouse:wh-a`, since the tick deleted the record and
     kept the claim and the re-create's claim requires its own incarnation). Fixes, model first (`ClaimFreed`,
     `BornLeftKept`):
     - G19 **ReleasesUnborn** (`GrantsClaimKept` fails temporal `ClaimFreed`): an orphan's record is never deleted by the
       tick. Past the bound and unborn it is stamped `ended_at` and `ended_reason: made_under_a_dead_incarnation`
       (`warehouses.end_warehouse_record`, conditional on the record); an ended record is absent to every door
       (`warehouses.get_warehouse`) and listing (`read_warehouses`) but the estate owner's purge (`get_warehouse_record`),
       is reported until it is gone, and a create of its id takes its key under the read's ETag
       (`create_warehouse_record`). In the same pass the tick releases the bucket's claim (`incarnations._Convergence._claim`):
       a claim of a dead incarnation that no standing record names, older than the bound (`claimed_at`, which every claim
       now carries), is stamped `released_at` under the read's ETag (`warehouses.release_claim`, the one way a claim is
       released; the purge uses it too), and a later claim takes its key, provided what stands in the bucket is only what
       a create writes (`warehouses.written_by_others`); anything else keeps the claim, logged `bucket_claim_kept` with
       what it found.
     - **What a warehouse's create writes before its birth** (read from `establish_warehouse` and measured on moto by the
       test): into the catalog, the bucket's claim (before the provision), the registry record (after the probe); into the
       bucket, the probe's objects under `_validate/<hex>/` (`probe`, `cas`, `encrypted`, `data/seal-probe.lance`), each
       deleted by the probe's own cleanup and left only where that cleanup failed, and `__manifest/` (the directory
       catalog's table, `ensure_catalog_row`: measured on a local root, two manifests and the version hint under
       `_versions/`, one file under `_transactions/` and one under `data/`, holding one row whose `object_id` is the
       warehouse's id). Measured after the held create on moto: every key under `__manifest/`. Unborn, the warehouse holds
       no grant and no tenancy edge, and a namespace door on it is refused (its fence reads no token), so no door writes
       through it.
     - G20 **KeepsBorn** (`GrantsBornReleased` fails `BornLeftKept`): a born orphan (one whose marker stands) is reported
       and kept with its claim. The estate owner purges it: `DELETE /management/v1/warehouses/{id}?purge_bucket=true`
       (`cascade=true` where it holds namespaces), gated on `estate#can_purge_dead_tenant_warehouse`
       (`warehouses.warehouse_to_delete`; anyone else, its tenant's admin included, is answered 404; without
       `purge_bucket` it is refused 400): the delete door's own steps end its namespaces, its grants and its incarnation,
       purge its bytes, release its claim and remove its record, write the audit row `dead_tenant_purge` (subject,
       resource, project, project incarnation, bucket, objects purged, reason) and announce `warehouse_deleted` with
       `dead_tenant: true` and `project_incarnation`. A purge of any warehouse is refused (409) where the bucket's claim
       now names another project or incarnation.
     - G21 **FenceChildBirth** (`GrantsChildBirthUnfenced` fails temporal `ClaimFreed`): the convergence lists the
       projects before the warehouse records, so it may find a record naming an incarnation it read standing that a
       delete ended since; it bears a warehouse under a fence of the project incarnation the record names
       (`fga.fenced(incarnation=)`, `fga.bear(fences=)`), so such a record stays unborn and is ended, not born and kept.
     Tests: `…::test_a_bucket_git_declares_is_claimed_once_the_dead_tenant_whose_create_claimed_it_is_ended_unborn`
     (the held create, a convergence held across the delete, the boot backfill writing no edge, the tick converging git's
     `wh-a` after the end; it replaces §8's B3 test, whose "the record is deleted and its claim kept" it reverses);
     `…::test_a_born_dead_tenants_warehouse_keeps_its_bucket_until_the_estate_admin_purges_it`. Guard removals: the
     release removed, the first fails; the birth check removed, the second fails; the convergence's birth unfenced, the
     first fails.

   - **P4, the second ask of the namespace-create door had no test** (REPRODUCED by mutation: with `fga_deps.parent_fence`'s
     second `_decide` removed, 1,050 tests stayed green; `door_fence`'s second ask was pinned only by a call count on a
     double). Tests: `tests/integration/test_a_fenced_door_asks_again_on_the_incarnation_its_fence_read.py`, each holding
     the door between its two asks (`fga_deps._opened`) while the object is deleted and made again by another tenant:
     `…::test_a_namespace_decided_on_a_warehouse_deleted_and_made_again_before_its_fence_opened_is_made_nowhere`
     (`parent_fence`), `…::test_a_grant_decided_on_a_project_deleted_and_made_again_before_its_fence_opened_lands_nowhere`
     (`door_fence`, the members door), and
     `…::test_a_caller_the_warehouse_authorizes_for_nothing_creates_no_namespace_in_it_having_written_nothing`
     (`parent_fence` after `authorize`: the store's changelog holds nothing of the refused caller's). The call-count unit
     test (`tests/unit/test_warehouses.py::test_estate_admin_creates_a_project_and_the_project_then_governs_itself`) is
     deleted. Each refused caller is answered 403 (its gate's refusal on the incarnation it read).

   - **P5, the converge tick's cost grew with the estate** (REASONED, then measured). Each tick paid, per project and per
     warehouse, a `bear` Read and a `sweep_tickets` Read, one after another, and per project a role listing. Fix: the
     tick's OpenFGA requests are what needs work. Births: a record the tick found born is stamped `born` (the incarnation,
     `control_records.BORN`) on its registry record, so no later tick asks about it; a marker is never revoked, so a stamp
     the tick wrote is true, and a stamp written past every door on an unborn record fails closed: the tick never bears
     it, and every door reads the incarnation from OpenFGA and answers 409 `Retry-After` until its create is asked again.
     Tickets: each is written and deleted with its index `fence:open open fence:<hex>` (`fga.ticket_index`,
     `Fence.tuples`, `type fence` `define open: [fence]`), and the sweep reads every open ticket of the estate in ONE
     paged Read of the indexes and the ticket of each one past the bound by its object (`fga.sweep_tickets`): measured on
     OpenFGA 1.18.3 (memory datastore), a Read naming `fence:` and no user is refused 400 `validation_error`, one naming
     `fence:` and the user `fence:open` answers every index, and one naming `fence:<hex>` and `ticket` and no user answers
     its ticket. The fence check by key (`fences_stand`), the drain (it deletes each ticket it read with its index) and
     the model's tickets are unchanged, and the sweep now reaches a ticket on an id no record names (the model's
     `SweepTicket` no longer needs a record, `DeadTicketsSwept` drops "or no record names it"). Orphans: from one listing
     of each registry the tick already makes (projects, warehouses with the ended, every role in one recursive listing
     `roles.every_stored_role`, every claim `warehouses.every_claim`, the policies). The tick needs no timer of its own.

     One tick at N warehouses over 10 projects, every record born (`scratchpad/measure_tick.py`, the registry on local
     disk; OpenFGA 1.18.3; the machine shared with other work at load 4 to 8): requests (wall). After, the first tick asks
     about each record not yet stamped born, once; every later tick is the steady state.

     | N | memory before | memory after, first | memory after, steady | Postgres 16 before | Postgres after, first | Postgres after, steady |
     | ------ | ------------------- | ------------------- | ------------- | ------------------- | ------------------- | ------------- |
     | 0      | 20 Reads (0.03 s)   | 11 (0.03 s)         | 1 (0.00 s)    | 20 (0.04 s)         | 11 (0.04 s)         | 1 (0.00 s)    |
     | 1,000  | 2,020 (4.2 s)       | 1,011 (3.2 s)       | 1 (0.02 s)    | 2,020 (5.6 s)       | 1,011 (3.4 s)       | 1 (0.02 s)    |
     | 10,000 | 20,020 (47 to 50 s) | 10,011 (42 s)       | 1 (0.2 s)     | 20,020 (53 to 58 s) | 10,011 (41 s)       | 1 (0.2 s)     |

     What is left of the steady 0.2 s at 10,000 is the registry's own listing. `chart/values.yaml`'s
     `catalog.declarationConverge` text says this.

   - **P6, false text and history in §8's own files.** `fga.sweep_tickets` said the sweep was judged on the store's clock
     alone; the bound is judged against the catalog's clock (`incarnations.converge`'s `now`), and the docstring now says
     what skew it tolerates (a catalog clock up to the five-minute margin ahead of the store's) and what more does (a live
     ticket swept, its Write refused 409). The Consequences' "a ticket is written by an authenticated caller before the
     gate" and its description of the deleted upgrade convergence are rewritten. The history in
     `test_a_caller_refused_…py` and `fga.read_all` is rewritten as rationale.

   **Every guard removed, its test run** (2026-11-18, each removal alone, the test red every time): G8 (the drain's read
   again) — the drain test leaves bob's grant on erin's project; G9b (a namespace seeded under its warehouse's fence) —
   the namespace door answers 200 and erin's warehouse holds alice's namespace; G14 at `door_fence` — the four doors of
   the refused-caller test write 4 changes each; G14 at `parent_fence` (listed before `authorize`) — the refused
   namespace create writes 4 changes; G15 at `door_fence` — 200, bob a member of erin's acme; G15 at `parent_fence` —
   200, alice's namespace in erin's warehouse; G16 (the tick's sweep, a door's take-back) — the ticket stands; the
   ticket's index (P5) — a dead door's ticket is never swept; G17 (the convergence's check, the backfill's fence on the
   record's project incarnation) — the tick fails `wh-a` for ever, and the backfill writes `project:acme project
   warehouse:wh-a` onto erin's acme; G19 — every tick fails `wh-a`; G20 — the born orphan is ended; G21 (the fence
   removed from the convergence's birth) — the orphan is born and kept, every tick fails `wh-a`; P1 (`made_under`
   reading a record naming none as another's) — the listing answers 200 and the delete 200; P2 (the door's buckets by
   id, the policy's incarnation unread by maintenance) — the set answers 200 over the dead bucket, and the leftover
   record governs acme's bucket.

   **What a client can now observe that differs.** A record naming a project and no incarnation of it is answered 500
   (code 18) at the project's listing and detail, its delete, its policy doors and the warehouse doors that compare it,
   where the listing hid it and the delete passed. A project's policy door answers a project whose only warehouse is a
   dead tenant's 400 (no active warehouse), the policy listing names no such bucket, a project policy record carries
   `project_incarnation`, and one an earlier incarnation left is answered `set: false` and reported by the tick. The converge tick's result and
   log carry `released` (buckets whose dead claim it released); an orphan warehouse record is stamped `ended_at` and
   `ended_reason` instead of deleted, and a create of its id or a claim of its bucket then succeeds where it was refused
   409 for ever; a claim carries `claimed_at`, and a released one `released_at` and `released_reason`. The estate owner
   may delete a dead tenant's warehouse with `?purge_bucket=true` (400 without it), audited `dead_tenant_purge` and
   announced `warehouse_deleted` with `dead_tenant: true`; a purge whose bucket's claim names another project or
   incarnation is refused 409. A project's or warehouse's record carries `born` once the tick found it born. OpenFGA holds
   an index tuple `fence:open open fence:<hex>` beside every open ticket. A ticket on an id no record names is swept.

   Each fix meets the four facts: GitOps (a declared bucket converges with no hand-run step; the tick ends and releases
   only the catalog's own bookkeeping and an unborn record's claim, and keeps every byte but what a create wrote),
   cloud native (nothing new runs; the tick's cost no longer grows with the estate), Lance-namespace/table-idiomatic (the
   bucket's catalog is read as the directory namespace's `__manifest` table it is), zero trust (no live tenant reads,
   governs or claims what a dead one left, and only the estate owner, audited, purges it).

10. **Six more findings, by a fourth independent review of §9, each FIXED here, model first** (2026-11-18). Q1, Q2, Q3,
    Q6a and Q6b reproduced against the catalog with a real OpenFGA over moto before the fix (each test below failed on the
    code before it); Q4 by mutation; Q5 reasoned, then pinned. The model gained a second tenant's claim on the bucket
    (`OtherClaim`), the estate owner's purge as two steps (`PurgeTake`, `PurgeDelete`) addressing a record or a claim by
    its bucket, a create of the child's id again on another bucket (`Recreate`), and whether the bucket holds bytes no
    create wrote (`foreign`, chosen at the start: the tick then keeps the claim, and the estate owner purges what the tick
    reports, a fairness of the model's).

    - **Q1, the estate owner's purge could delete a live tenant's bucket** (REPRODUCED: the orphan of the held create,
      `converge(bound=0)` ended `wh-a` and released `wh-a-bucket`, the estate owner's `DELETE …/wh-a?purge_bucket=true&
      cascade=true` held at entry to `purge_bucket`, beta made `wh-e` and `wh-e$sales` there; released: 200,
      `objects_purged: 9`, `wh-e`'s catalog gone). A released claim read as no claim, and nothing held it between the
      check and the delete. Fix, G22 **PurgeStamp** (`GrantsPurgeUnstamped` fails `PurgeTakesNothingLive`, "no purge
      deletes the bytes of a bucket a live claim names, nor removes a live tenant's record"). **Who may delete a bucket's
      bytes, and under what:** only a purge holding the bucket's claim, through ONE function, `warehouses.purge_bucket`
      (`services/catalog/src/catalog/services/warehouses.py`), which takes the hold and reads the claim again before its
      first delete, between batches and before it deletes the bucket. Three purgers exist: a warehouse's delete with
      `?purge_bucket=true` (its tenant's admin for a live warehouse, the estate owner for a dead tenant's), the estate
      owner's purge of a dead claim by its bucket (Q2), and the converge tick's removal of what an unborn create wrote
      (Q6a). Each first takes the claim for itself, `warehouses.take_for_purge`: stamped `purging` (`<warehouse|bucket|
      catalog>:<name>@<incarnation>`) and `purging_at` under the read's ETag, only where the claim names the project at
      the incarnation addressed. `claim_bucket` refuses a stamped claim (a create on the bucket answers 409, code 14,
      until the purge ends), and the one way a claim is released is `warehouses.end_purge`, which only the stamp's
      holder does (§9's `release_claim` and `release_bucket_claim` are gone). A claim released, absent or naming anyone
      else is taken by nobody: the delete removes the record and NO byte, touches nothing in the bucket (a dead tenant's
      delete opens no connection to it), and answers `bucket_purged: false` with `bytes_kept` naming why. A dead tenant's
      record is stamped `purging` too (`warehouses.mark_purging`, conditional on its incarnation), so no create takes its
      key while its purge runs. The delete deletes the bytes, then its record, then releases the claim, so a released claim
      never names a bucket a record still routes to. **A purge that dies leaves its stamp**: an operator sees the claim
      with `purging` and `purging_at` naming the purge, and every create on that bucket is refused 409 meanwhile; only the
      same delete asked again resumes (its holder is derived from the warehouse and its incarnation, so it finds its
      stamp its own); a stamp whose warehouse record is gone (the delete died between its record and its release, so its
      bytes were gone) is released by the converge tick (`incarnations._Convergence._finish_purge`). Test:
      `tests/integration/test_a_dead_tenants_records_reach_no_live_tenant.py::test_a_purge_deletes_no_byte_of_a_bucket_whose_claim_it_does_not_hold`.
    - **Q2, recreating an ended record's id stranded its kept claim** (REPRODUCED: the bucket held bytes no create wrote,
      so the tick ended `wh-a` and kept its claim; a create of `wh-a` on another bucket replaced the ended record, after
      which no record named the claim, no tick reported it and no door reached it). Two fixes. G23 **KeepsEndedKey**
      (`GrantsEndedKeyTaken` fails `ClaimNamedByItsRecord`): a create takes an ended record's key only once its bucket's
      claim no longer names it and no purge stamped it (`warehouses.require_key_free`, asked by
      `create_warehouse_record` at the write and by `endpoints/warehouses._claim_the_bucket` before the create claims a
      bucket of its own): answered 409 (code 2) naming `DELETE /management/v1/warehouses/<id>?purge_bucket=true`. G24
      **PurgeByBucket** (`GrantsClaimUnaddressed` fails temporal `ClaimFreed`): a claim of a dead incarnation that no
      record, ended or not, names (a create that died between its claim and its record leaves one) is reported by the
      tick among `orphans` as `bucket:<name>` and in `catalog.incarnations.orphans`, and the estate owner's purge door
      reaches it by its bucket: the SAME route, `DELETE /management/v1/warehouses/{id}?purge_bucket=true`, with the
      bucket in the warehouse's place, consulted only where no record stands under the id and only once the caller holds
      the same relation, `estate#can_purge_dead_tenant_warehouse` (`warehouses.warehouse_to_delete`,
      `_unnamed_dead_claim`, `_purge_a_dead_claim`); anyone else is answered the missing warehouse's 404. Tests:
      `…::test_an_ended_records_id_is_taken_only_once_its_claim_is_released`,
      `…::test_a_dead_claim_no_record_names_is_reported_and_the_estate_owner_purges_it_by_its_bucket`.
    - **Q3, one malformed bucket claim stopped every tick** (REPRODUCED: `_warehouses/bucket-claims/junk.json` =
      `{"bucket":"junk","project":"x"}` raised `ServiceUnavailableError` out of the convergence, so the declaration's
      converge after it and the boot backfill never ran). Fix: every listing the tick makes is tolerant
      (`incarnations._Convergence._listing`): a record that cannot be read (a project, a warehouse, a claim
      `warehouses.read_claims`, a role `roles.every_stored_role`, a policy `maintenance_policies.read_policies`) is named
      in `failed` by its key and judged on nothing; a listing the store did not answer stops only what needs it; while any
      claim cannot be read, no warehouse record is ended and no claim released (`_may_end_warehouses`, `_may_release`).
      `incarnations.converge` never raises (what stopped it is named in `failed`), and `main.converge_at_boot` runs the
      backfill whatever the convergence found. Test: `…::test_one_unreadable_claim_stops_no_tick_and_no_claim_is_released_while_it_stands`
      (the junk claim beside an unborn orphan whose claim stays, and a declared `gamma` that is made).
    - **Q4, the "neither read nor merged onto" half of the policy's incarnation had no test** (REPRODUCED by mutation:
      `_set` given the stored record rather than `_of_incarnation(stored, …)` left every test green). The P2 test now
      plants the leftover with `retain_versions` and `retention_days`, sets a body naming only `compact_enabled`, and reads
      back no field of the leftover (`…::test_a_project_policy_governs_only_the_buckets_of_the_incarnation_that_set_it`).
    - **Q5, a purge that died after deleting bytes wrote no audit row** (REASONED). Fix: destruction is audited before it
      starts and when it ends, for every purge, under ONE action, `bucket_purge` (renamed from §9's `dead_tenant_purge`,
      which covered the dead tenant's alone; `packages/lakehouse/src/lakehouse/governed/audit.py` and the Rust trail's
      `rust/crates/lakehouse-core/src/telemetry/audit.rs`, same place in both, `rust/vectors/telemetry.json`
      regenerated), with a field `dead_tenant` and a field `stage` (`claim_taken`, `bytes_deleted`, `record_deleted`,
      `claim_released`): outcome `allow` once the purge holds the claim and before its first delete, so the trail holds the
      intent even if the process dies, then `success`, or `failure` naming the stage it reached whenever it held the claim
      and did not finish (`endpoints/warehouses._Purge`; the tick's removal writes the same rows as `system:catalog`). No
      action was added; the outcome vocabulary was enough. Test:
      `…::test_a_purge_is_audited_before_its_first_delete_and_one_that_fails_after_it_says_how_far_it_got` (a live
      warehouse's purge whose record removal fails once its bytes are gone, a create on the bucket refused meanwhile, the
      purge asked again resuming under its stamp).
    - **Q6a, what an unborn create leaves reached the next tenant** (REPRODUCED: after the release the bucket still held
      `__manifest/`). **What stands in a bucket after an unborn create** (measured on moto by the tests): `__manifest/`
      (two manifests and the version hint under `_versions/`, one transaction under `_transactions/`, one data file under
      `data/` holding one row whose `object_id` is the dead warehouse's id), and under `_validate/<hex>/` only what the
      probe's own cleanup failed to remove. **What reads `__manifest`**: the directory namespace itself (a warehouse's
      listing and resolution, every name beneath it beginning with its id, so the next tenant's names never resolve the dead
      row), `object_names` (a grant's uuid to its current name: the dead row's uuid holds no grant, so it is never asked),
      `table_bases` (table rows only; the dead create wrote a namespace row) and the reclaim of the index's history; none
      served the dead row for the new tenant, but it stood in its catalog and counted in its index. **Who removes it:** the
      converge tick. Once `written_by_others` finds only what the dead create wrote, the tick takes the claim with the same
      stamp (`catalog:<bucket>@<incarnation>`), deletes exactly the objects under `_validate/` and `__manifest/` through
      `purge_bucket(only=CREATE_PREFIXES, remove_bucket=False)`, lists again, and releases only if the bucket is empty
      (`warehouses.holds_anything`); anything else gives the claim back unstamped and keeps it. The process's connections
      rooted at the bucket are dropped (`NamespaceConnections.forget`), since a connection holding a version of an index
      that is gone answers `InternalError`. Test: the P3 test now asserts the bucket empty once released.
    - **Q6b, a create slower than the bound wrote into a bucket it no longer held** (REPRODUCED: alice's create held after
      its record and before its catalog row while the tick released the bucket and beta made `wh-e` there; released, it
      wrote `wh-a`'s row into `wh-e`'s catalog). Fix: `claim_bucket` answers when the claim was taken (a claim the
      project's incarnation already holds has `claimed_at` moved to now under its ETag, so the tick judges a claim's age
      by its latest create), and the create asks `warehouses.require_claim_held` before provisioning, before its probe,
      before its record and before its catalog row: the claim stands unreleased, unstamped, naming its project at its
      incarnation, and the create is younger than `incarnations.ticket_bound`, the bound the tick releases by; otherwise
      409 (code 14) and nothing more is written. Not the claim's ETag: a sibling create of the same incarnation moving
      `claimed_at` leaves it this create's own. **Residual:** a process frozen between that check and the write it guards
      (the probe's own writes count as one, under one check) still writes once, on a bucket the tick released only past the
      bound. Test: `…::test_a_create_that_outlived_its_claim_writes_nothing_more_into_the_bucket`.

    **Every guard removed, its test run** (2026-11-18, each removal alone, its test red every time): G22 (a released
    claim taken for the purge, the stamp unread, the dead tenant's bucket opened) — the purge answers `bucket_purged:
    true` and `wh-e`'s catalog is gone; G23 (`require_key_free` passing) — the create of `wh-a` answers 200; G24 (no
    claim reached by its bucket) — the estate owner's purge answers 404; the unnamed claim's report removed — the tick's
    `orphans` names no `bucket:lost-bucket`; Q3's no-release-while-unreadable removed — the orphan's claim is released
    beside the junk claim; Q3's per-claim tolerance removed alone the listing's own tolerance still names the junk claim
    and the test stays green, and with both removed the tick stops before the warehouses are judged and the test is red;
    Q5's intent row removed, and its failure row removed — the trail's rows differ; Q6a's removal skipped — the bucket
    holds `__manifest/` once released; Q6b's re-read removed — `wh-e`'s catalog holds `wh-a`'s row. And §9's again: G19,
    G20, G21, `made_under` reading a record naming none as another's, the policy door's buckets by id, the policy merged
    onto the leftover (Q4), and the policy's incarnation unread by maintenance (red in the dead-tenant policy test;
    `tests/integration/test_a_tables_pass_deletes_nothing_a_door_answered_for.py` stays green with it removed, its
    project-hold case seeding the policy and the claim under one incarnation).

    **What a client can now observe that differs.** A warehouse delete with `?purge_bucket=true` whose bucket's claim is
    released, absent or another's answers 200 with `bucket_purged: false` and `bytes_kept`, deleting no byte, where it
    deleted the bucket or was refused 409; the response carries `bytes_kept` (null otherwise). While a purge runs or after
    one died, a create on its bucket answers 409 (code 14) and a create of the purged record's id 409 (code 2). A create
    of an ended record's id answers 409 (code 2) naming the purge while its claim stands. The estate owner's
    `DELETE /management/v1/warehouses/<bucket>?purge_bucket=true` reaches a dead claim no record names. A create that ran
    past the bound, or whose claim another took, answers 409 (code 14). The audit action `dead_tenant_purge` is
    `bucket_purge`, written for a live warehouse's purge too, two rows per purge. The tick's `orphans` names `bucket:<name>`,
    its `failed` names each record it could not read, and its `released` a bucket it emptied of what an unborn create
    wrote. A claim carries `purging` and `purging_at` while a purge holds it, and `claimed_at` moves with each create.

    Each fix meets the four facts: GitOps (the tick still frees a declared bucket with no hand-run step, and one corrupt
    record no longer stops it), cloud native (nothing new runs), Lance-namespace/table-idiomatic (the directory catalog's
    `__manifest` is removed whole by whoever empties the bucket, and connections holding its versions are rebuilt), zero
    trust (bytes are deleted only under a claim their purger holds, every destruction is audited before and after it, and
    no create writes into a bucket it no longer holds).

## What is not modelled

- The last-admin guard (ADR 0213), which moves the token in its own CAS; only administration removals move it now.
- Time: the sweep's bound (the request deadline plus five minutes) is modelled as "only a dead request's ticket, and an
  orphan whose create no longer runs" (every request ends, by its steps or its death, under weak fairness); a door
  running past the bound is refused, narrower. The catalog's clock judges the bound against the store's timestamps
  (`fga.sweep_tickets`): a catalog clock up to the five-minute margin ahead of the store's sweeps nothing a request that
  ends by its deadline holds; one further ahead can sweep a live ticket, whose Write is refused 409, narrower.
- A bucket's bytes are one flag (§10): whether the bucket holds anything a create did not write. The tick's removal of
  what an unborn create wrote and its release are one step in the model: its stamp is taken under the claim's ETag where
  the claim names the dead incarnation, and no other writer lands on a stamped claim. A create past the bound is modelled
  as no longer running; the code makes it so by `warehouses.require_claim_held`, but for a process frozen between that
  check and its write (§10 Q6b).
- The estate owner acts on what the tick reports and keeps: the model's liveness of a kept claim rests on that fairness.
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
  (G1, G9, G11), and a writer's death leaves only a ticket that grants nothing and that the object's delete or the
  converge tick removes; a ticket is written only by a caller its door's gate admitted, and taken back when the request
  ends, whatever it answered (G14, G16).
- GitOps, since §7: a record whose create died before its birth is born at boot and on each tick with no hand-run step
  (G12), and a reconcile revokes only a tuple its own Write created, whatever step a tick died at (G13).
- Cloud native, since §7: nothing new runs; the convergence is a step of the catalog's existing boot loop and converge
  timer.
- Zero trust, since §8: a caller the object authorizes for nothing writes nothing on it (G14); no ticket outlives the
  request that wrote it past the bound (G16); no tenant's size refuses another's door (G18); nothing a dead tenant's
  create left reaches a live tenant, and the sweep ends only bookkeeping and unborn records, keeping every byte (G17).
- GitOps, since §8: a record without an incarnation is re-created from git, never repaired at runtime (B5).
- The model change of §8 (`type fence`, `ticket: [project, warehouse]`) is recorded in the same CHANGELOG, and
  `model.fga.yaml` holds that a ticket grants nothing.
- GitOps, since §9: a bucket git declares converges once the dead tenant that claimed it is ended unborn (G19), and the
  tick that converges it costs what needs work, not the estate's size (P5); zero trust, since §9: what a born dead tenant
  left is kept from every tenant until the estate owner purges it (G20, G21), and a project's policy governs only the
  buckets of the incarnation that set it (P2).
- The model change of §9 (`fence#open`, `estate#can_purge_dead_tenant_warehouse`) is recorded in the same CHANGELOG.
- Zero trust, since §10: a bucket's bytes are deleted only by a purger holding its claim (G22), a create writes nothing
  into a bucket it no longer holds, and every destruction is audited before it starts; GitOps, since §10: no record a
  tenant corrupts stops the tick, and every dead claim is reported and reachable (G23, G24). The authorization model is
  unchanged: the purge by bucket asks the relation §9 added.
- The model change (`revision` on `warehouse`, `admin_revision` renamed `revision` on `project`) is recorded in
  `packages/lakehouse/src/lakehouse/governed/auth/CHANGELOG.md`; `model.fga.yaml` holds that a revision grants nothing.
