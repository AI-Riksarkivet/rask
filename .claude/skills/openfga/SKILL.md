---
name: openfga
description: OpenFGA authorization modeling best practices for defining types and relations, writing relationship tuples, deriving can_* permissions, applying type restrictions and usersets, and authoring .fga.yaml check/list_objects/list_users tests. Use when authoring, reviewing, or refactoring OpenFGA models, tuples, permissions, .fga files, .fga.yaml test files, or OpenFGA SDK integrations in Python.
license: Apache-2.0
metadata:
  author: openfga
  version: "1.2.1"
---

# OpenFGA Best Practices

Use this skill to design and review OpenFGA models end to end: define types and relations, write tuples, derive `can_*` permissions, choose usersets and inheritance patterns, and validate behavior with `.fga.yaml` tests and CLI checks.

## Quick Start Example

Minimal model:

```fga
model
  schema 1.1

type user

type organization
  relations
    define admin: [user]
    define member: [user] or admin

type document
  relations
    define organization: [organization]
    define owner: [user]
    define can_edit: owner or admin from organization
    define can_view: can_edit or member from organization
```

Example tuples:

```text
organization:acme#admin@user:alice
document:roadmap#organization@organization:acme
document:roadmap#owner@user:bob
```

Example test targets:
- `check` that `user:alice` can view and edit `document:roadmap`
- `check` that `user:bob` can edit their own document
- `list_users` for `document:roadmap#can_view`
- `list_objects` for documents `user:alice` can edit

## How to Use

When a workflow step points to a rule ID, open the matching file in `references/` for detailed guidance and examples.

**Note:** SDK references (`sdk-*.md`) are only needed for integration tasks — skip them during pure model authoring and testing.

## Rule Index

### Core
| File | Description |
|------|-------------|
| `references/core-types.md` | Define types for entity classes |
| `references/core-relations.md` | Relations belong on object types |
| `references/core-tuples.md` | Relationship tuples as facts |
| `references/core-separation.md` | Model vs data separation |
| `references/core-schema-version.md` | Schema version |

### Relations
| File | Description |
|------|-------------|
| `references/relation-direct.md` | Direct relationships |
| `references/relation-indirect.md` | Indirect relationships with X from Y |
| `references/relation-concentric.md` | Concentric relationships |
| `references/relation-usersets.md` | Usersets for group-based access |
| `references/relation-conditions.md` | Conditional relationships |
| `references/relation-wildcards.md` | Wildcards for public access |
| `references/relation-wildcards-as-booleans.md` | Wildcards for boolean attributes |

### Design
| File | Description |
|------|-------------|
| `references/design-permissions.md` | Define permissions with can_ relations |
| `references/design-hierarchy.md` | Hierarchical structures |
| `references/design-organization.md` | Organization-level access |
| `references/design-create-on-parent.md` | Check create permissions on parent objects |
| `references/design-naming.md` | Naming conventions |
| `references/design-modules.md` | Modularize models (only when asked) |

### Roles
| File | Description |
|------|-------------|
| `references/roles-simple.md` | Simple static roles |
| `references/roles-static-combo.md` | Combining static and custom roles |
| `references/roles-assignments.md` | Role assignments for resource-specific roles |
| `references/roles-when-to-use.md` | When to use each role pattern |

### Optimization
| File | Description |
|------|-------------|
| `references/optimize-simplify.md` | Simplify models |
| `references/optimize-tuples.md` | Minimize tuple count |
| `references/optimize-type-restrictions.md` | Type restrictions |

### rask-specific
| File | Description |
|------|-------------|
| `references/grant-provenance.md` | Who GAVE a grant — the audit-join review procedure, and why a tuple cannot answer it |

**Every subject is a principal, `<idp-id>~<subject>`, and becomes a `user` id in one place,
`lakehouse.governed.fga.user_object`** (ADR 0159 D5, ADR 0156 C5, [[LH-063]], [[LH-474]];
`lakehouse.governed.principal`). A service account is `kubernetes~system:serviceaccount:<ns>:<sa>`; a client signed in
for itself is `<idp-id>-client~<client id>` under its issuer's idp id; a person is the idp id the deployment names their
issuer (`RASK_OIDC_IDP_ID` or a `RASK_OIDC_PROVIDERS` entry, chart `auth.oidc.idpId`/`auth.oidc.providers`) and the
token's `sub` or the first claim the issuer's subject claims name (ADR 0208), or, under a Dex mapped connector by connector (`RASK_OIDC_CONNECTORS`,
chart `auth.oidc.connectors`; the dev Dex's `local` is `dex`), the connector's idp id and the user id Dex reports in
`federated_claims`, never Dex's `sub`, which encodes the connector.
`user_object` refuses a value that names no identity provider (`InvalidInputError`, 400), so no tuple, seed or check
names a bare name, and writes `user:` and the key percent-encoded as Lakekeeper encodes a `UserId`
(`urllib.parse.quote(key, safe="")`): OpenFGA refuses a user id with a second `:` ("the 'user' field is malformed",
HTTP 400, measured on the live store 2026-10-14) and reads a `#` as a userset, so `user:kubernetes~system%3Aserviceaccount
%3A<ns>%3A<sa>` and `user:corp~a%23b` are each one user. `subject_of` decodes an id read back (`list_users` does). A grant
API's grantee goes through `grantee_object`: `user:*` and an object of another model type pass as written, a `user:<id>`
is the stored id (decoded and encoded again, so an id a reader echoes back names its tuple), and a bare value is a
principal key. A tuple, a seed or a test that spells a user by hand is refused by the store or names the wrong one;
spell it through `user_object`.

**Every data rung is a stored relation and an `_effective` twin** ([[LH-222]], `auth/CHANGELOG.md`). On `warehouse`,
`namespace`, `table` and `materialized_view` the bare name (`reader`) holds only what was granted on the object: grant
doors write it and list it. `reader_effective` adds the rungs that imply it and what flows from the parent, and no
`can_*` reads a bare data rung. A check that means "holds it here" asks a `can_*` or a twin, never the bare rung; a new rung ships
as both relations. `visible_below` (upward visibility) reads the bare `owner`/`writer`/`reader` and recurses downward
through `child` only: never define it over a twin, which sends every descendant back up to the project on each refusal
(measured 2026-11-13 on a local OpenFGA, 234 descendants: 167 ms refused before the split, 17 ms after). A project
`member` holds no data rung. A change to `model.fga` adds an entry to `auth/CHANGELOG.md` saying whether a store's
tuples are rewritten or added to. **Each estate-admin door asks a verb of its own on the estate root** ([[LH-076]]):
a new estate-wide door adds a computed `can_<purpose>: owner` and its `model.fga.yaml` rows, never a second use of an
existing verb.

**Every table and namespace object is its uuid, `table:<uuid>` / `namespace:<uuid>`** (ADR 0173 §8, [[LH-524]];
`fga.table_object`, `fga.namespace_object`), the uuid the catalog minted (a table's in its location claim, a namespace's
its `rask.uuid`; DescribeTable and DescribeNamespace answer it). No name reaches OpenFGA: a rename writes no tuple, a
table created again under a dropped name inherits nothing, and a name OpenFGA could not hold as an id (a space) is
fine. A catalog door resolves the name once per request (`catalog.api.dependencies.Resolver`); a test or a seed spells
an object by resolving the name first, never by joining it into `table:<name>`. A
warehouse, a project, a classification label and a transaction keep their ids.

**A project's and a warehouse's id is reused once it is deleted, so a write on or naming it is fenced by its incarnation**
(ADR 0241, `specs/catalog/Grants.tla`): `revision` on `project`, `warehouse` and `estate` is bookkeeping holding the marker
`revision:<incarnation>` (never revoked), the token `revision:<incarnation>.<n>`, the drift repair's token
`revision:repair-<hex>` and a declared grant's evidence `revision:git-<hex>`; a door's ticket is `<object> ticket
fence:<hex>` on `type fence` (§8 B1, B2), off the object, so no read of the object reads it, written and deleted in the
same Writes as its index `fence:open open fence:<hex>` (§9 P5, `fga.Fence.tuples`): OpenFGA 1.18.3's Read takes a
type-only object only beside a user (`{object: "fence:"}` alone is refused 400 `validation_error`, measured), so the one
party every index names makes the estate's open tickets one paged Read. A test planting a ticket a dead request left
plants its index too (`fga.ticket_index`). The audit trail leaves the three relations out, and a delete writes the
retirement as its own row. A door asks its gate with NO write first, then opens a
fence (`fga_deps.door_fence(gate=)`, the context manager `fga.fenced` underneath, the ONE way a fence is opened, which takes
its ticket back on every exit), then asks its gate again on the incarnation the fence read, and writes or revokes through
`fga.commit_fenced` (or `project_admins.confer` / `withdraw(fences=)`), never `fga.write_tuples` / `delete_tuples`; a
namespace created in a warehouse gets this from the router's `parent_fence`, listed after `authorize`. This covers a tuple
naming the object as SUBJECT too (a warehouse's tenancy edge, a role's `project` edge). A Write cannot require a tuple
without deleting it (a Write that deletes and writes one tuple is refused `cannot_allow_duplicate_tuples_in_one_request`,
measured on 1.18.3), so each writer deletes its own ticket and grants never refuse each other. **Read what you need and
no more:** an incarnation through `fga.read_incarnation` (the `revision` relation, filtered on the server), one tuple
through `fga.holds` (its key), one relation through `fga.read_relation`; `fga.read_object_tuples` grows with the object's
use and is for a listing or a delete. Every read pages to the end with no ceiling (`fga.read_all`); OpenFGA 1.18.3 takes
at most 100 tuples in one Write (`fga.TUPLES_PER_WRITE`, refused 400 `exceeded_entity_limit` past it). A delete ends the
incarnation with `fga.end_incarnation` (mark born, retire, drain until a read finds nothing, tickets deleted in Writes of
100 with `on_missing_deletes: IGNORE`); the converge tick sweeps every ticket older than the request deadline plus five
minutes, on any id, from one read of the indexes (`fga.sweep_tickets`), judged against the catalog's clock: a catalog
clock more than the five minutes ahead of the store's can sweep a live request's ticket, whose Write is then refused 409.
The tick asks OpenFGA about a record's birth once: it stamps the record `born` (`control_records.BORN`), and a marker is
never revoked. `estate#can_purge_dead_tenant_warehouse` (the estate owner) is the one door to a dead tenant's warehouse,
and, by its bucket in the warehouse's place, to a dead incarnation's bucket claim no record names (ADR 0241 §10 Q2); the
gate is asked before the door says whether either exists, so no other caller learns which ids or buckets a dead tenant left. A test seeding a project or warehouse straight to the store must seed its marker and token
too, or its doors answer 409 with `Retry-After` (being established); `tests/integration/openfga_grants.Grants` answers
every project and warehouse as born.

### Testing
| File | Description |
|------|-------------|
| `references/test-fga-yaml.md` | Structure tests in .fga.yaml |
| `references/test-check-assertions.md` | Check assertions |
| `references/test-list-objects.md` | List objects tests |
| `references/test-list-users.md` | List users tests |
| `references/test-conditions.md` | Testing conditions |
| `references/test-cli.md` | OpenFGA CLI usage |
| `references/workflow-validate.md` | Always validate models |

### SDK (rask's services are Python)
| File | Description |
|------|-------------|
| `references/sdk-python.md` | Python SDK |

## Recommended Workflow

1. Model the resource graph.
  Define types, direct relations, inheritance edges, and `can_*` permissions.
  Rules to check first: `core-*`, `relation-*`, `design-permissions`, `design-hierarchy`.

2. Add tuples and test intent.
  Add representative tuples and cover expected behavior with `check`, `list_objects`, and `list_users` tests.
  Rules to check next: `core-tuples`, `test-fga-yaml`, `test-check-assertions`, `test-list-objects`, `test-list-users`.

3. Review parent-child creation and deletion paths.
  Verify each parent -> child edge has create permissions on the parent and that no child permission is directly grantable unless intended.
  Rules to check: `design-create-on-parent`, `relation-direct`, `design-permissions`.

4. Simplify before removing schema.
  Before deleting any type or relation, confirm it is not referenced by permissions, tuples, or tests.
  If a simplification breaks a reference: restore the relation or update the model/tests, then re-run this step.
  Rules to check: `optimize-simplify`, `optimize-tuples`, `optimize-type-restrictions`.

5. Validate the model.
  Run:

```bash
fga model validate --file packages/lakehouse/src/lakehouse/governed/auth/model.fga
```

  If validation fails: read the reported relation or type errors, fix the model, and re-run validation before continuing.

6. Run the store tests.
  Run:

```bash
fga model test --tests packages/lakehouse/src/lakehouse/governed/auth/model.fga.yaml
```

  If tests fail: update tuples, assertions, or permission definitions, then re-run tests until all checks and list queries pass.
  In rask, `make fga-test` runs steps 5 and 6 and also fails when `model.json` drifted from `model.fga`.

7. Only then finalize delivery.
  For touched stores, finish only after validation and tests are green.
  Final rule to check: `workflow-validate`.
