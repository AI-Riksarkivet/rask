# API surface

The canonical, machine-readable contract lives in two generated OpenAPI files, refreshed from the live
FastAPI apps by `make openapi` and drift-guarded in CI by `dagger call openapi` (`.dagger/openapi.go`,
`.github/workflows/ci.yml`) — reachable locally as `make openapi-check`:

- [`catalog-openapi.json`](./catalog-openapi.json) — the catalog service
- [`lineage-openapi.json`](./lineage-openapi.json) — the lineage service

[`operations.json`](./operations.json) is generated beside them (`scripts/gen_operations.py`, the same two targets): one
row per operation of either service, with whether the Lance Namespace spec defines it and the gate in front of it (the
relation the catalog's router asks and on what, or that the request body or the handler decides), read from the
catalog's own rung maps.

!!! tip "Add a route, refresh the spec"

    `make openapi` regenerates both specs; `make openapi-check` regenerates and fails on any diff, which
    is the same contract CI enforces. Both need `uv sync --all-packages` — the root `pyproject.toml` has
    `dependencies = []`, so a plain `uv run` installs no workspace member and the import fails. The
    Makefile targets do this for you.

This page is the human index: which capability each endpoint group serves, and which parts are **net-new to
this project** (★) versus the upstream Lance-namespace REST contract. Per-service path counts are
deliberately not stated here — they went stale twice; count the `paths` keys in the JSON instead.

## Catalog service

Auth: OIDC bearer (Dex) at the edge, per-route OpenFGA relations (`can_*`). Reads gate on the reader rung,
writes on writer, and destructive/admin ops on owner. Fail-closed:
an OpenFGA outage is `503`, a denial is `403`, a missing token is `401`.

**Audit trail (#41, configurable via `LANCE_AUDIT_ENABLED`).** Every security-relevant *catalog* action —
authn success/failure, authz allow/deny (single, batch, and warehouse gates), credential vending — emits a
structured event (who / what / resource / outcome) on the dedicated `lance.audit` logger, exported over
OTLP under that instrumentation scope to the platform's gateway (in development into GreptimeDB) and queryable by `audit.action` / `audit.outcome` / `audit.subject`. Scope: the catalog
service (the policy-decision point); lineage-read governance is enforced by its own FGA gates and logged
through the standard service logs, and durable data/model-mutation provenance (who created/wrote/promoted
what, when) additionally lives in lineage's commit feed, for `services.lineage.eventsRetentionDays`, and in the
graph backend lineage forwards every event to. Retention: each service exports its audit records as itself to the
platform's OTLP gateway (`observability.export.endpoint`, ADR 0215), which routes the `lance.audit` scope to a store of
its own; the chart sets no retention, so a compliance deploy sets it on that store. In development the observability
fixture's gateway writes the scope to GreptimeDB's `lance_audit` table.

| Capability | Endpoints | Notes |
|---|---|---|
| **Namespaces** | `POST /v1/namespace/{id}/{create,describe,drop,exists}`, `GET /v1/namespace/{id}/{list,table/list}` | Lance-namespace core. An id's first segment is its warehouse (ADR 0206): `GET /v1/namespace/$/list` lists the warehouses the caller may see (`lance_catalog` is the estate's own), a one-segment id is the warehouse and answers only its reads, a namespace is `<warehouse>$<name>…` and a table `<warehouse>$<namespace>…$<table>` |
| **Tables — lifecycle** | `POST /v1/table/{id}/{create,declare,register,deregister,drop,rename,restore}`, `GET /v1/table` | create centralizes the 2.2 + stable-row-ids invariant |
| **Tables — read** | `POST /v1/table/{id}/{describe,exists,query,count_rows,stats,explain_plan,analyze_plan,schema}` | |
| **Tables — write** | `POST /v1/table/{id}/{insert,merge_insert,update,delete}` | server-side write path |
| ★ **Client-direct write** | spec: `describe`/`declare` (they vend the tier FGA grants and answer `managed_versioning=true`), `POST /v1/table/{id}/version/create`; rask extension: `POST /management/v1/table/{id}/credentials` | the client writes data, transaction and deletion files and a staged manifest with its vended credential; the catalog judges the staged version and finalizes it (ADR 0157). No data byte transits the catalog. A declaration is its declarer's: until the declarer's first `version/create` lands or 15 minutes pass since the declarer last asked for it, another principal's create of the name or version 1 into it answers 409 code 14; the declarer's `declare` sent again, its `describe` and its write-tier `credentials` renew the declaration and answer the same one, and a credential vended for a declaration expires with it (at most 15 minutes, whatever the configured TTL); a drop of the unwritten table is served at once (ADR 0227 §9) |
| **Schema evolution** | `POST /v1/table/{id}/{add_columns,alter_columns,drop_columns,backfill_column,update_field_metadata,schema_metadata/update}` | |
| **Versions / branches / tags** | `POST /v1/table/{id}/version/{create,delete,describe,list}`, `.../branches/{create,delete,list}`, `.../tags/{create,update,delete,list,version}` | `version/delete` condemns and deletes nothing (the next reclaim does), never the current version, nor one a tag, a pin or a branch cut from it names, which it reads after it condemns (ADR 0246); `version/create` judges and finalizes a stock client's staged version (`describe` and `declare` answer `managed_versioning=true`, ADR 0157) |
| **Indexes** | `POST /v1/table/{id}/{create_index,create_scalar_index,index/list,index/{name}/drop,index/{name}/stats}` | `create_index` and `create_scalar_index` answer 406: an index is built on the caller's compute and committed through `version/create` (ADR 0162) |
| **Blobs** | `GET /management/v1/table/{id}/blobs` | credential-less blob read (data-reader gated) |
| **Batch** | `POST /v1/table/{batch-commit,version/batch-create}` | 406: the catalog judges and finalizes one table version at a time |
| ★ **Data contract** | `POST /management/v1/table/{id}/contract/{set,describe,delete}` | a table's ODCS v3 contract, set by its owner (`can_set_contract`); a commit that drops, renames, retypes or loosens a contracted column is refused 409 code 19, and a quality or freshness rule is refused 406 ([DATA-CONTRACT.md](DATA-CONTRACT.md)) |
| ★ **Labels** | `GET /management/v1/projects/{id}/labels`, `POST /management/v1/projects/{id}/labels/define`, `POST /management/v1/projects/{id}/labels/{name}/delete`, `GET /management/v1/projects/{id}/labels/objects?label=`, `POST /management/v1/{table,namespace}/{id}/labels/{describe,set,remove}` | a project's admin defines its labels (a name, a scope of `namespace`/`table`, a kind of `marker`/`free_text`/`enumerated`; scope and allowed values only grow, the kind never changes, and a delete is refused 409 while any object carries the label); an object's owner sets and removes them at `can_label`, `describe` reads them at `can_get_metadata`; each change is announced untargeted. Labels authorize nothing ([ADR 0211](adr/0211-labels-a-project-defines-replace-tiers-and-the-run-rule-stands-alone.md)) |
| ★ **Project registry / tenants** | `GET,POST /management/v1/projects`, `GET,DELETE /management/v1/projects/{id}` | A tenant EXISTS when its registry record does — `POST` writes the record AND the `project#admin` tuple in ONE operation (the creator's; a machine identity creating a project names the person in `admin` and is never admin itself, LH-433), so existence and permission cannot drift (the list is the caller's projects — every tenant for an estate observer, else those the caller is a `member` of; create is estate-observer gated, and one project is read by the callers the list shows it to, anyone else answered the missing project's 404; a fresh project legitimately has zero warehouses). `DELETE` is gated on the project's OWN `can_administer`, refuses **409 naming its warehouses** while any remain, and has **no `cascade` parameter at all** — retiring a tenant must never destroy buckets transitively. `?force=true` overrides the `protected` flag ONLY, never the authz gate |
| ★ **Warehouse admin / physical multi-tenancy (#3-A)** | `GET,POST /management/v1/warehouses`, `GET,DELETE /management/v1/warehouses/{id}`, `POST /management/v1/warehouses/{id}/{activate,deactivate}` | project-admin (`can_create_warehouse`) provisions a bucket per warehouse; the warehouse's id is the first segment of every name in it (`POST /v1/namespace/<warehouse>$<name>/create` makes a namespace there, ADR 0206); the project must already exist (404 naming `POST /management/v1/projects` — there is no bootstrap exception). A warehouse or project git declares (`managed_by: "git"`) refuses a re-`POST`, `activate`, `deactivate`, protection and `DELETE` with 403 code 15: its declaration changes it ([OPERATORS.md § 2](OPERATORS.md)). `DELETE` gates on the **project's** `can_administer` (destroying storage is a tenant-level act), refuses **409 naming the namespaces it holds**, and takes three SEPARATE opt-ins that never share a default: `?cascade=true` drops those namespaces, `?purge_bucket=true` is the only thing that removes bytes (and refuses if a same-project sibling or platform reserve claims the bucket), `?force=true` overrides `protected` alone |
| ★ **Maintenance policies (#50)** | `POST /v1/table/{id}/policy/{set,describe,delete}`, `POST /v1/namespace/{id}/policy/{set,describe,delete}` | per-target compaction cadence/opt-out + old-version retention (`retention_days`/`retain_versions`), enforced by maintenance's per-table passes; set/delete are owner-tier (`can_drop`/`can_delete`, audited), describe is reader-tier; tag-pinned versions (e.g. a pipeline's `stable`) are exempt from cleanup by Lance itself |
| ★ **Access review and grants (#51)** | `POST /management/v1/{table,namespace,warehouse}/{id}/access/{list,grant,revoke}` | who holds which `can_*` action, expanded through the FGA model (roles, teams, parent cascade) via ListUsers, and the doors that hand a base rung out and take it back. A grant asks `can_grant_<rung>` and refuses one raising the caller's own access, a revoke asks `can_revoke_grant`; the listing asks `can_drop` on a table, `can_delete` on a namespace and `can_read_assignments` on a warehouse, which a project's admin holds (LH-555). Each disclosure is audited as `access_review`, each grant and revoke as `access_grant` / `access_revoke` with a `grant_added` / `grant_revoked` control event naming the grantee |
| **Materialized views** | `POST /v1/materialized_view/{id}/{create,refresh}` | spec-defined + FGA-typed, but the `dir` backend does not implement MVs yet (answers 406 Unsupported); dormant until Lance adds native MV support |
| **Transactions** | `POST /v1/transaction/{id}/{alter,describe}` | |
| **Health** | `GET /livez`, `GET /readyz` | |

## Lineage service

Lineage keeps provenance; the full lineage graph is the bring-your-own backend it forwards to (ADR 0158). Reads gate
on `can_get_metadata` for every dataset they name; the HTTP ingest takes the author from the verified bearer token.

| Capability | Endpoints | Notes |
|---|---|---|
| **Ingest** | `POST /api/v1/lineage` | the HTTP transport into the commit feed and the edge list; the bus transport is the durable consumer `lineage-ingest`, which lineage pulls on its own connection to the bus, on no public path |
| **Commit feed** | `GET /events`, `GET /events/projection` | the durable feed a pipeline polls ([COMMIT-EVENTS.md](COMMIT-EVENTS.md)); the projection is a notifier's, gated on `can_read_event_feed` (`event_reader`, granted at the estate admin's tuple door) |
| **Derived tables** | `GET /tables/{name}/derived?depth=` | every table derived from `name`, over the edge list; how an erasure finds them |
| **Dead letters** | `GET /admin/dlq`, `POST /admin/dlq/{run_id}/replay`, `GET /admin/dlq/parked`, `POST /admin/dlq/parked/{run_id}/replay` | the outbox's at-risk events, and the bus deliveries lineage parked in its refusals table (the estate admin's) |
| **Health** | `GET /livez`, `GET /readyz` | |

Maintenance has no public REST surface: its replicas share a NATS JetStream work queue (ADR 0198), so it is not in
these specs; its contract is the OpenLineage events it publishes, consumed here, and the due list the catalog serves
(`GET /management/v1/maintenance/due`). A user's pipeline that moves data between tables is not a rask
service: it writes through the catalog's doors above
([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)).
