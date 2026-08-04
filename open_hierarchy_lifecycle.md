# open_hierarchy_lifecycle — the estate's lifecycle contract

The rule the estate is built on, made enforceable end to end:

    project  >  warehouse  >  namespace (self-nesting)  >  table
    (tenant)    (physical:      (logical separation)       (data)
                 one bucket)

**Three layers, each owned in ONE place:**

| Layer | Question | Owner |
| --- | --- | --- |
| hierarchy | what shapes can exist | the guards in `catalog/api/fga_deps.py` (`require_parent`, `require_warehouse_scoped`, + the new ones below) |
| authz | who may do it | the FGA model's `can_*` relations — the app never invents policy |
| invariants | what is possible NOW | the catalog's registries (project/warehouse records, bindings), checked BEFORE any native write |

Everything below follows from four decisions.

## Decision 1 — a project is a REGISTRY RECORD, not just tuples

The root cause of "three projects in authz the catalog never heard of": a project today has no
existence of its own — it is inferred from warehouses and asserted by FGA tuples, and the two drift.

So: **`POST /v1/projects`** (new) writes BOTH in one operation:
- a registry record beside the warehouse registry (`_REGISTRY_PREFIX/projects/<id>.json`) — this is
  what "exists" means from now on;
- the FGA tuples (creator as `admin`, optional `team` edge).

Gate: estate admin (`can_observe_events` on the root — the same bar the events feed uses).
This **replaces** the warehouse-create bootstrap exception: instead of "estate admin may create a
warehouse for a project with no tuples", the estate admin creates the PROJECT explicitly, then any
project admin creates warehouses. The implicit minting path is removed — one way to make a tenant.

`GET /capi/v1/projects` then lists registry records (annotated with warehouses), so the gallery's
"merged" fallback becomes belt-and-braces rather than the only thing preventing an empty page.

## Decision 2 — creates are top-down, checked at the door

Every create validates, in this order, with these codes:

    401  no identity
    400  the identifier cannot satisfy the hierarchy   (shape — the spec's InvalidInput, code 13,
         RFC 9457 problem body; the spec has NO 422 and clients dispatch on the numeric code)
    404  the parent does not exist                     (existence — from the registry)
    403  the caller may not do this                    (authz — FGA can_*; spec code 15)
    409  the name is taken / not empty / would collide (spec codes 2/3/5/14)
    ───  only then the native write, then tuples, then events

| Create | Parent that must exist | FGA gate (on the parent) |
| --- | --- | --- |
| project | — (root) | estate admin |
| warehouse | project (registry record) | `project#can_create_warehouse` |
| namespace (top-level) | warehouse — via `POST /v1/warehouses/{id}/namespaces` ONLY (done: `require_warehouse_scoped`, InvalidInput otherwise) | `warehouse#can_create_namespace` |
| namespace (nested) | parent namespace | parent `namespace#can_create_namespace` |
| table | namespace (done: `require_parent`, InvalidInput; rename destination guarded too) | `namespace#can_create_table` |

New guard: **`require_project_exists(project_id)`** in the same module as the other two — reads the
registry, raises 404 with "create the project first: POST /v1/projects". Wired into
`create_warehouse`, deleting its bootstrap exception.

## Decision 3 — deletes are bottom-up, and a container refuses while full

**One rule: you cannot delete what still holds things.** Refusals are 409 and LIST what is inside —
a refusal that does not name the blocker just moves the search to the user.

| Delete | Gate | Refuses 409 while… | `cascade` | What is removed |
| --- | --- | --- | --- | --- |
| table `drop` | `table#can_drop` (owner) | — | — | dataset bytes + tuples |
| table `deregister` | `table#can_deregister` | — | — | catalog entry + tuples; bytes stay (that is what registering external data means) |
| namespace `drop` | `namespace#can_delete` | it holds tables/children, unless `behavior=cascade` | ✅ walks descendants, drops each | entries + tuples (+ bytes via the table drops) |
| **warehouse `DELETE`** (new) | **`project#can_administer`** — deleting storage is a tenant-level act | any namespace is bound to it (named in the 409) | ✅ `?cascade=true` drops them first | tuples, bindings, registry record. **Bucket only with `?purge_bucket=true`** — a catalog entry is recoverable, a customer's bucket is not; they never share a default |
| **project `DELETE`** (new) | `project#can_administer` | it holds any warehouse (named) | **NO cascade, deliberately** — deleting a project must never delete buckets transitively in one request | tuples (admins, members, team edges) + registry record |

`deactivate` stays as offboarding step one: quarantine → verify nothing breaks → delete.

Partial-failure honesty: if the native delete lands and the tuple revoke fails, the response says so
(the revoke is retried idempotently; a repeat call is the recovery path). Never "success" with
work silently left behind.

## Decision 4 — a reconciler REPORTS drift; it never deletes first

The cross-store consistency job (compaction service, beside the sweep), report-only:

- FGA projects/warehouses with no registry record (today's seeded ghosts);
- registry records with no tuples (undeletable by anyone);
- namespaces with no binding whose warehouse feature is on (pre-rule legacy);
- buckets whose warehouse record was deleted without purge;
- files in a managed bucket no live dataset version references (the orphan-file gap —
  `open_table_maintenance.md` §3).

Deleting is a later, separate decision per category, after the report has run clean on a real
estate. A reclaimer that deletes on its first run against an unvalidated rule eats live data.

## The seed becomes a user

`scripts/fga_seed_demo.py` (FGA-only) is replaced by `scripts/seed_estate.py`, which drives the
REAL APIs in order: create projects → warehouses → namespaces → tables → extra grants. The seeded
estate is then one a user could have built, every guard above has run against it, and a state that
cannot exist through the UI cannot be seeded either. (The FGA fixture file stays for CI model tests
— that is its job; populating a live estate is not.)

## Diffed against Lakekeeper (docs.lakekeeper.io, fetched 2026-08-04 — not from memory)

Their model: Server → Project → Warehouse → Namespace → Table, strict containment. Where we agree,
where we are ahead, and what is worth taking:

- **Agrees with Decision 3:** "Warehouses and Namespaces cannot be deleted via the /catalog API if
  child objects are present." Empty-or-refuse is their rule too.
- **We are ahead on cascade:** their cascade-drop is "planned for the management API"; ours is a
  designed, explicit `?cascade=true`.
- **Agrees on purge separation:** their table drop takes a PURGE flag; our bucket purge is a
  separate opt-in. Same principle — metadata removal and byte removal never share a default.

**ADOPTED from the diff (Decision 5): deletion protection.** Lakekeeper lets a warehouse, namespace
or table be marked *protected*; protected entities refuse deletion, and only an explicit
`force=true` bypasses it. This is cheap (one registry/properties field + one check in the delete
door) and is the guard rail that makes `cascade` survivable — a fat-fingered cascade cannot take a
protected object with it. Refusal: 409 with "protected; pass force=true to override" (and force
still requires the same FGA gate, it is not an authz bypass).

**DEFERRED from the diff (recorded, not adopted): table soft-delete/undrop.** Lakekeeper marks
dropped tables deleted and reclaims after an expiration delay, recoverable until then. Valuable,
but it needs a deferred-deletion queue (the compaction service's territory), and their own docs
show the footgun: engines that issue PURGE "immediately delete files", silently undermining it, and
the delay is frozen at drop time. Do it deliberately later or not at all — half a soft-delete is
worse than none. Until then, drop = drop, as today, and `deactivate` remains the recoverable step.

## Spec compliance (audited 2026-08-04 against lance.org)

- **Operations 47/47** of the spec's named list implemented, verified mechanically.
- **All 22 error codes** mapped in `service_kit/lakehouse/ns_errors.py` as RFC 9457 problem bodies;
  `NamespaceNotEmpty → 409` is the spec's own "container refuses while full" error — the deletion
  design uses it rather than minting one.
- The only off-spec surface found was the day-old guards' 422; fixed to `InvalidInput` (95ae4cb).
- Full contract captured in the project skill: `.claude/skills/rask-lance-catalog`.

## Storage decision — no new database

FGA (Postgres) answers WHO; the control-root registries (CAS'd JSON, same pattern as the warehouse
registry) answer WHAT EXISTS; Lance on object storage holds the data. Registry writes are
admin-frequency and single-object — deletes are bottom-up by design, so there is no multi-object
transaction that would justify a relational store. Revisit only if that changes.

## Order of work

1. Project registry + `POST /v1/projects` + `require_project_exists` in warehouse-create
   (removes the bootstrap exception).
2. `DELETE /v1/warehouses/{id}` — empty-check, cascade, separate bucket purge, protection flag
   (`protected` in the registry record; `force=true` to override, same FGA gate).
3. `DELETE /v1/projects/{id}` — empty-check, no cascade, same protection flag.
4. `seed_estate.py` through the real APIs; retire the FGA-only live seed.
5. The reconciler report.
6. Then the deferred hard problems, in this order: orphan-file reclamation (report → delete),
   credential-level tenant isolation tests, multi-warehouse maintenance sweep.

---

Delete this file when the work lands. GC/maintenance detail: `open_table_maintenance.md`.
