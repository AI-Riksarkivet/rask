---
name: rask-lance-catalog
description: The catalog's contract — the Lance Namespace spec (operations, error model, REST route grammar) and rask's own hierarchy layer (project > warehouse > namespace > table) on top of it. Use when adding/changing a catalog endpoint, raising an error from the catalog, touching the hierarchy guards or seed_ownership, designing create/delete/lifecycle semantics, or answering "is this spec-conformant".
---

# rask lance catalog — the spec and the estate's layer above it

Two contracts stack here, and confusing them is how bugs happen:

1. **The Lance Namespace spec** (lance.org/format/namespace/) — operations, error codes, REST
   grammar. Defines ONLY namespaces (recursive) + tables, and explicitly prescribes **no hierarchy
   enforcement** and nothing above a namespace.
2. **rask's hierarchy** — `project > warehouse > namespace > table`. Everything above a namespace is
   OURS: our objects, our guards, our lifecycle. The spec neither requires nor forbids it.

## The spec surface (verified 2026-08-04 against lance.org)

- **Operations: 47/47 implemented** in `services/catalog` (checked mechanically — snake_case diff of
  the spec's operation list over `catalog/api`). The spec's *minimum* is 8 metadata ops; we carry the
  whole list including versioning, tags, indices, transactions, `RefreshMaterializedView`.
- **Route grammar:** `POST /v1/<object>/{id}/<action>` — everything a reverse proxy needs (authN/Z,
  routing) is in the PATH, never only in the body. Path/body id conflict → 400. List ops are GET with
  query-param pagination; data ops (create/insert/query) are **Arrow IPC**, not JSON; count/explain
  return plain text.
- **Identifier:** segments joined by the configured delimiter (`$` default) — `["a","b","t"]` ↔
  `a$b$t`. The root namespace is the delimiter itself.
- **Identity headers:** `api_key` → `x-api-key`, `auth_token` → `Authorization: Bearer`; arbitrary
  context rides `x-lance-ctx-<key>`. Headers beat body fields.

## The error contract — NEVER invent a status

The spec defines **22 numeric error codes (0–21)**, identical across Python/Java/Rust/REST; clients
dispatch on the **code**, not the HTTP status. On the wire they are **RFC 9457 problem bodies**
(`application/problem+json`).

**The one rule:** an endpoint raises `lance_namespace` typed errors
(`InvalidInputError`, `NamespaceNotFoundError`, …) and lets
`service_kit/lakehouse/ns_errors.py::install_problem_handlers` translate — it maps all 22 codes
(not-founds → 404, already-exists/not-empty/concurrent → 409, `InvalidInput` → 400,
`PermissionDenied` → 403, `Unauthenticated` → 401, `Unsupported` → 501, `Throttling` → 429).
**Never `HTTPException` with a hand-picked status** for domain errors — a 422 was shipped once
(the hierarchy guards, same day they were written) and no generated client understood it; fixed to
`InvalidInputError` (95ae4cb). `NamespaceNotEmpty → 409` is the spec's own error for "container
refuses while full" — use it, don't mint one.

## rask's hierarchy layer

    project (tenant)  >  warehouse (ONE bucket; a project holds MANY)  >  namespace (self-nesting)  >  table

Three layers, each owned in ONE place:

| Layer | Owner |
| --- | --- |
| shape (what can exist) | the guards in `catalog/api/fga_deps.py` — `require_parent` (a table must have a namespace; rename destinations too), `require_warehouse_scoped` (a top-level namespace only via `POST /v1/warehouses/{id}/namespaces`; **no-op when `warehouses_enabled` is off** — single-bucket deployments have no warehouse to demand), `require_project_exists` (a warehouse's project must have a registry record — 404 naming `POST /v1/projects`), `require_not_protected` (deletion protection; `force=true` overrides the flag and **nothing else**) |
| who | the FGA model's `can_*` relations (`service_kit/governed/auth/model.fga`) — the app never invents policy |
| what is possible NOW | the registries, checked BEFORE the native write: **project records** (`catalog/services/projects.py`, `_projects/<id>.json`), warehouse records + `top_ns → warehouse_id → root_uri` bindings (`catalog/services/warehouses.py`) |

**A tenant EXISTS when its registry record does** — not when a warehouse implies it and not when FGA
holds tuples for it. `POST /v1/projects` writes the record AND the creator's `project#admin` tuple in
one operation (estate-admin gated on `can_observe_events` at the root), which is what stops existence
and permission drifting apart. There is **no bootstrap exception** in warehouse-create any more: one
door, and a warehouse-owner cannot ride a create into project admin.

Check order at every create door: **identity (401) → shape (`InvalidInput` 400) → parent exists
(404) → authz (403) → conflict (409) → native write → tuples (`seed_ownership`) → events.** Guards
run BEFORE `native.call` — rejecting after leaves a real Lance object with no authz parent.

`project` and `warehouse` are **control-plane objects, not spec objects** — their endpoints
(`/v1/projects`, `/v1/warehouses`) are ours. Keep their errors in the same problem-body format so
one client error path serves the whole API.

## Storage — what state lives where (and why there is NO app database)

| State | Store |
| --- | --- |
| table data + versions | Lance datasets on object storage (rustfs) |
| project registry (`_projects/<id>.json`), warehouse registry + namespace bindings | JSON records on the control root, CAS'd conditional writes (the `cas` e2e marker proves the primitive) |
| authz | OpenFGA on its chart-managed Postgres |
| lineage | AGE (Postgres), chart-managed |

A relational app-DB was removed at P7a and must not creep back for the catalog: registry writes are
admin-frequency, CAS handles their concurrency, and deletes are bottom-up single-object operations
by design — there is no multi-object transaction to need one. The moment that changes (atomic
cross-object invariants, high-frequency filtered listings), it is a design decision, not a default.

## Lifecycle (design: `open_hierarchy_lifecycle.md`; GC/maintenance: `open_table_maintenance.md`)

- Creates are top-down: parent must EXIST (registry), gated on the parent's `can_*`.
- Deletes are bottom-up: a container refuses **409, naming its contents**; `cascade` is explicit;
  warehouse delete gates on the PROJECT's `can_administer`; bucket purge is a **separate opt-in**;
  project delete has **no cascade at all**.
- **`force=true` overrides the `protected` flag and NOTHING else** — the FGA gate runs first and
  identically with or without it. Test both delete doors for force-without-authz.
- **NO EXISTENCE ORACLE on destructive doors (audit #4).** `delete_warehouse`, `delete_project` and
  `_set_warehouse_status` all collapse `PermissionDenied → TableNotFound`, so "not yours" and "does
  not exist" are byte-identical and the door cannot enumerate ids. CREATE doors deliberately do the
  opposite (`require_project_exists` 404s naming `POST /v1/projects`) because that 404 is a fix the
  caller needs. Class rule, not a per-endpoint judgement call.
- **A purge must prove sole ownership first.** One project may back two warehouses with one bucket
  (`projects_claiming_bucket` subtracts the caller's own project on purpose — the work+gold pair),
  so `?purge_bucket=true` checks same-project siblings AND the reserved platform buckets before the
  cascade. Without it, deleting the work warehouse wiped gold's data.
- Anything making a DESTRUCTIVE decision reads `read_bindings` (which returns unparseable paths) and
  refuses on a non-empty skip list; `list_bindings` is the tolerant half, for enumeration only. A
  binding you cannot read is a namespace you cannot see.
- `deactivate` = offboarding step one (quarantine; resolver 403s bound namespaces).
- Maintenance (compaction + `optimize_indices` + `cleanup_old_versions` with tags EXEMPT) lives in
  `services/compaction` — `catalog/api/maintenance.py` is read-only maintenance MODE, not this.
- The reconciler reports cross-store drift and deletes nothing until its report runs clean.

## Gotchas

- `deregister` keeps bytes ON PURPOSE (external data); `drop` removes them. Neither leaves Lance
  orphans — but partially-failed writes and unpurged buckets do, and nothing reclaims those yet.
- The FGA-only live seed (`fga_seed_demo.py`) writes projects no registry knows — the origin of
  "ghost projects". The replacement (`seed_estate.py`, planned) drives the real APIs in hierarchy
  order so seeded state is always constructible state.
- **The control-event vocabulary is a wire contract in three files.** `ControlAction` /
  `ControlObjectType` (`service_kit/control_events.py`) reach the frontend through
  `docs/catalog-openapi.json` → `frontend/packages/api/src/generated/catalog.ts`. Adding an action
  without `make openapi` + `bun --cwd=frontend run gen:types:catalog` leaves the TS client unable to
  name an event the backend publishes, and `test_openapi_contract` fails. Same for `TupleOrigin`
  (`service_kit/governed/fga.py`) — an origin string not in the Literal is a `ty` error, not a runtime one.
- `discover_dataset_uris` (maintenance sweep) walks ONE root — multi-warehouse sweeps are untested.
- **A namespace is a `__manifest` ROW, not a directory** (the `dir` impl the chart runs —
  `LANCE_REST_IMPL=dir`). Only a TABLE materialises a directory. Any scan that enumerates namespaces
  by listing directories silently returns `[]` on every real estate — which reads as "checked and
  clean". The reconciler's `unbound_namespaces` detector shipped with exactly that bug.
- **The `warehouse_binding_cache` eviction on delete is PER-PROCESS.** `_resolve_warehouse_root`
  caches bindings positively and forever on the premise that a binding is immutable; the warehouse
  delete is the first thing that breaks that premise. Other replicas keep routing a dropped
  namespace at the deleted warehouse's bucket. Latent only because `chart/values.yaml` pins the
  catalog to `replicas=1` — a second replica needs the control event wired to invalidation first.
- The `\Z`-anchored `CONTROL_ID_RE` (`catalog/core/identifiers.py`) is the ONE id-shape rule. It was
  three copies that had already drifted: Python's `$` also matches before a trailing newline, so
  `"acme\n"` was refused by one door and accepted by another.
- Credential-level tenant isolation (tenant B's creds refused on bucket A) is untested; only
  byte-placement isolation is proven (`test_warehouse_routing.py` locally,
  `test_warehouses_e2e.py` against real buckets, env-gated).
