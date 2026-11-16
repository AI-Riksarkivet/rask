# 0213. A project's administrators are everyone its admin reaches through its roles (2026-11-16)

**Status:** accepted. Source: row LH-597 (#836), an audit finding reproduced on a real OpenFGA; the owner started the
row on 2026-11-16. Amends the last-admin rule of LH-434 and the role doors of LH-557.

## Context

The authorization model lets a role's assignees be people and other roles, and lets a project's `admin` be held by a
role. The catalog's guards read only direct tuples:

- **Assignment.** The role door asked `can_grant_admin` only when the role being filled held the project's `admin`
  itself. With `admins` holding `admin` and `leads` nested in it, a `security_admin` (who holds no admin power) assigned
  a person to `leads`, and that person administered the project.
- **Removal.** The unassign door, a role's ending and the offboarding door refused only the last assignee of a role
  that was the project's only direct admin. Emptying `leads` left `admins` holding `admin` and reaching nobody.
- **Counting.** The members door refused the last `admin` tuple. A project whose other `admin` tuple is a role nobody
  is in lost its last person and kept two tuples.

The references (read 2026-11-16): OpenFGA resolves a nested userset on `Check` and offers no constraint on the
resulting set, so a floor on it is the application's. Lakekeeper (main 3d5be6e,
`authz/openfga/v4.15/components/role.fga`) lets a role's `ownership` or its project's `security_admin` grant its
assignees (`can_grant_assignee`), whatever the role is nested beneath, and a search of its Rust for a
last-administrator guard finds none.

## Decision

1. **Who administers a project is every person its `admin` tuples reach**, directly or through roles nested to any
   depth. One analysis reads it (`catalog.services.project_admins.Administration`): the project's own tuples first,
   then each role its `admin` reaches, each role once, so roles assigned to each other in a ring end the walk.
2. **A removal that leaves a project's `admin` reaching no person is refused** 409 (code 14), whichever door asks: the
   members door, a role's unassign, a role's ending, the offboarding door, the declaration converge. A removal that
   takes nothing the `admin` passes through is not judged.
3. **While a project's `admin` reaches a role, filling, emptying or ending it asks `can_grant_admin`** on that project,
   as filling the role holding `admin` itself already did. A refusal answers the role's 404. Ending a role asked only
   the role's `can_delete` before, so a security admin could unmake administrators by deleting their role.
4. **Every change is one Write that moves the project's `admin_revision`**: an `admin` granted or revoked (at the
   members door and by the declaration converge), a role assignment written or removed. A project's delete revokes the
   revision its roles' endings moved, after its last role ended. The store refuses the Write whole once another such change committed after the read
   it was judged on, and the loser reads again. The assignment is arbitrated too, because whether a role is beneath an
   `admin` is itself changed by an assignment.
5. **A read that fails refuses the change** (503): the analysis never answers on a partial graph.

## Not taken, stated

- **Refusing a role nested beneath an admin role.** The issue's interim option. It removes a shape the model allows
  and projects may already hold.
- **Answering 409 to a security admin who empties a nested admin role.** The issue asked for it. The role holding
  `admin` already answers that caller 404 for want of `can_grant_admin`, and a nested role now answers the same; the
  409 is what a caller holding `can_grant_admin` reads.
- **A model change.** No relation expresses "at least one person"; the model is unchanged, so no store is rewritten.
- **The estate admin's raw tuple editor** writes past the arbitration, as it writes past every door.

## Consequences

- **A change touching more projects than one Write holds** (100 tuples, writes and deletes together) is removed in
  halves; one tuple that alone touches that many, which only the raw tuple editor can build, is refused 400.
- **Each role assignment and removal reads its project's administration** (the project's tuples and each role its
  `admin` reaches) and moves its revision, so two changes to one project's roles at once take turns. They are
  admin-frequency writes.
- **A project already left with an `admin` that reaches nobody** refuses further removals from that chain until an
  admin is granted, by the estate admin where nobody inside holds `can_grant_admin`.
- **Four facts:**
  - GitOps: the declaration converge revokes a declared admin through the same rule, and a refusal is counted on each
    tick until git declares another.
  - Cloud native: no new workload or store.
  - Lance-namespace and table idiomatic: nothing of Lance's is touched; refusals carry the spec's codes (14, 4).
  - Zero trust: no identity makes or unmakes an administrator on a rung that does not grant it, and a tenant cannot
    be left ungovernable from inside.
