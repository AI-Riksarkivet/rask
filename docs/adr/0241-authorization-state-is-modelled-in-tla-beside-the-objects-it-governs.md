# 0241. Authorization state is modelled in TLA+ beside the objects it governs; three defects found and fixed (2026-11-17)

**Status:** accepted; the catalog's sixth model (ADR 0221, ADR 0227, ADR 0229, ADR 0233). It describes the Python
catalog as it runs, before it is ported (ADR 0217 §5). Three findings were confirmed against the running catalog and a
real OpenFGA, and each is fixed, model first (§5).

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
     and a dead delete is issued again.
   - **table**: one name, its tables keyed by a minted uuid (`fga.table_object`): a create seeds the owner
     (`fga_deps.seed_ownership`), a purge revokes, a recoverable drop keeps the grants for the undrop, the undrop
     admitted before the deadline registers later, the trash release judges the name free and revokes past
     `trash.RELEASE_SETTLE` (`trash_reclaim.release_trash`), a grant decided on one table lands late, the access door
     revokes it, and a reader decides on what OpenFGA answers (`fga.check`).
   - **converge**: one tuple git declares and later drops (`declaration_converge._Converger._grant` and `_revoke`),
     and the same tuple granted at a door at any point between.

2. **What the gate proves** (`make tla-check`; measured 2026-11-17 on two workers at load 4):
   - `Grants.cfg` (container: two incarnations, a door grant, a ghost revoke, any door dying): 12,038 states, 2.5 s.
   - `GrantsTable.cfg` (table: purge, recoverable drop, undrop, release, a late grant, a revoke and a reader): 440
     states, 1.2 s.
   - `GrantsConverge.cfg`: 19 states, 1.5 s.
   - `GrantsWide.cfg`, ON DEMAND (three incarnations and two grants, any door dying): 982,371 states, 36 s.

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

## What is not modelled

- A token's count: two grants meeting at one token, one refused and reading again (`write_fenced`'s attempts), is a
  liveness matter the model abstracts to "the token stands".
- The last-admin guard (ADR 0213), which moves the same token in its own CAS.
- A converge tick dying between its record and its write: the record stands without the tuple, and a door grant made
  before the next tick is then recorded git's (the next tick finds the tuple standing and its record already made, so it
  cannot tell). Record-first keeps this on the narrow side: a git-declared tuple is never left unrecorded.
- Roles: a role created at its door beside its project's delete (the record written after the delete listed the roles)
  survives the project, its `project` edge naming the id. UNDECIDED: a test holding `roles.create_role_record` while the
  project's delete runs would settle it; the role holds no assignee and its edge confers nothing a new tenant's admins
  do not hold, so no access leaks through it, and its record is listed under the new tenant.
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
  grants of an object made after it judged (G4). Every guard is a conditional OpenFGA Write, so it holds across replicas
  and needs no lock.
- The model change (`revision` on `warehouse`, `admin_revision` renamed `revision` on `project`) is recorded in
  `packages/lakehouse/src/lakehouse/governed/auth/CHANGELOG.md`; `model.fga.yaml` holds that a revision grants nothing.
