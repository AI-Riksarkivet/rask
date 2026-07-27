# API surface

The canonical, machine-readable contract lives in two generated OpenAPI files, refreshed from the live
FastAPI apps by `make openapi` and drift-guarded in CI (`make openapi-check`):

- [`catalog-openapi.json`](./catalog-openapi.json) — the catalog service (75 paths)
- [`lineage-openapi.json`](./lineage-openapi.json) — the lineage service (24 paths)

This page is the human index: which capability each endpoint group serves, and which parts are **net-new to
this project** (★) versus the upstream Lance-namespace REST contract. Regenerate both specs and this page's
counts after adding or changing a route — CI fails if the committed JSON drifts from the code.

## Catalog service

Auth: OIDC bearer (Dex) at the edge, per-route OpenFGA relations (`can_*`). Reads gate on the reader rung,
writes on writer, destructive/admin ops on owner, and promotion on the separate validator rung. Fail-closed:
an OpenFGA outage is `503`, a denial is `403`, a missing token is `401`.

**Audit trail (#41, configurable via `LANCE_AUDIT_ENABLED`).** Every security-relevant *catalog* action —
authn success/failure, authz allow/deny (single, batch, and warehouse gates), credential vending — emits a
structured event (who / what / resource / outcome) on the dedicated `lance.audit` logger, exported over
OTLP to GreptimeDB and queryable by `audit.action` / `audit.outcome` / `audit.subject`. Scope: the catalog
service (the policy-decision point); lineage-read governance is enforced by its own FGA gates and logged
through the standard service logs, and durable data/model-mutation provenance (who created/wrote/promoted
what, when) additionally lives in the lineage graph. Retention: audit records currently share the
observability store's TTL (`observability.retention`, default `14d`) — raise it for compliance deploys;
routing `lance.audit` to an independently-retained table is a known open enhancement.

| Capability | Endpoints | Notes |
|---|---|---|
| **Namespaces** | `POST /v1/namespace/{id}/{create,describe,drop,exists}`, `GET /v1/namespace/{id}/{list,table/list}` | Lance-namespace core |
| **Tables — lifecycle** | `POST /v1/table/{id}/{create,declare,register,deregister,drop,rename,restore}`, `GET /v1/table` | create centralizes the 2.2 + stable-row-ids invariant |
| **Tables — read** | `POST /v1/table/{id}/{describe,exists,query,count_rows,stats,explain_plan,analyze_plan,schema}` | |
| **Tables — write** | `POST /v1/table/{id}/{insert,merge_insert,update,delete}` | server-side write path |
| ★ **Client-direct write (#2)** | `POST /v1/table/{id}/credentials`, `POST /v1/table/{id}/commit` | vend expiring creds → client writes fragments to the store → catalog folds only the metadata commit (zero data bytes transit the catalog) |
| **Schema evolution** | `POST /v1/table/{id}/{add_columns,alter_columns,drop_columns,backfill_column,update_field_metadata,schema_metadata/update}` | |
| **Versions / branches / tags** | `POST /v1/table/{id}/version/{create,delete,describe,list}`, `.../branches/{create,delete,list}`, `.../tags/{create,update,delete,list,version}` | tags are the ref plane the model `blessed` tag reuses |
| **Indexes** | `POST /v1/table/{id}/{create_index,create_scalar_index,index/list,index/{name}/drop,index/{name}/stats}` | |
| **Blobs** | `GET /v1/table/{id}/blobs` | credential-less blob read (data-reader gated) |
| **Batch** | `POST /v1/table/{batch-commit,version/batch-create}` | |
| ★ **Model registry & promotion (#17/#42)** | `GET /v1/model` (list), `GET /v1/model/{model}`, `POST /v1/model/{model}/promote` | list = governed registry enumeration (reader-rung `list_objects` filter; candidate + blessed versions); describe (candidate vs blessed + metrics); promote = validator-gated metrics-gated move of the `blessed` tag on `models$<model>` |
| ★ **Warehouse admin / physical multi-tenancy (#3-A)** | `GET,POST /v1/warehouses`, `GET /v1/warehouses/{id}`, `POST /v1/warehouses/{id}/{activate,deactivate,namespaces}` | project-admin (`can_create_warehouse`) provisions a bucket per warehouse + binds namespaces to it |
| ★ **Maintenance policies (#50)** | `POST /v1/table/{id}/policy/{set,describe,delete}`, `POST /v1/namespace/{id}/policy/{set,describe,delete}` | per-target compaction cadence/opt-out + old-version retention (`retention_days`/`retain_versions`), enforced by the compaction sweep; set/delete are owner-tier (`can_drop`/`can_delete`, audited), describe is reader-tier; tag-pinned versions (e.g. `blessed`) are exempt from cleanup by Lance itself |
| ★ **Access review (#51)** | `POST /v1/table/{id}/access/list`, `POST /v1/namespace/{id}/access/list` | who holds which `can_*` action, expanded through the FGA model (roles, teams, parent cascade) via ListUsers; relation set enumerated from the compiled model; owner-tier (`can_drop`/`can_delete`) with the disclosure itself audited as a distinct `access_review` event; 501 on an auth-off stack. Expansion caveats: a leaf owner sees parent-scope grantees (within-tenant, by design), and results past OpenFGA's ListUsers cap (default 1000/3s) are truncated server-side (logged) |
| **Materialized views** | `POST /v1/materialized_view/{id}/{create,refresh}` | spec-defined + FGA-typed, but the `dir` backend does not implement MVs yet (returns 501); dormant until Lance adds native MV support |
| **Transactions** | `POST /v1/transaction/{id}/{alter,describe}` | |
| **Health** | `GET /livez`, `GET /readyz` | |

## Lineage service

Read gates on `can_get_metadata` for the addressed dataset; the ingest accepts either an OIDC human or the
service-door principal (app token + allowlisted subject — how the Ray trainer's self-emitted events land).

| Capability | Endpoints | Notes |
|---|---|---|
| **Ingest** | `POST /api/v1/lineage`, `GET /dapr/subscribe` | HTTP + Dapr-subscription transports into the AGE graph |
| **Dataset graph** | `GET /datasets`, `GET /datasets/{name}/{upstream,downstream,graph,creator,producers,schema,reconcile}` | dataset-level DERIVED_FROM lineage |
| **Column lineage** | `GET /datasets/{name}/columns`, `.../columns/{field}/{upstream,downstream}` | field-to-field `columnLineage` (incl. `source_rowid ← _rowid` at the cascade head) |
| **Runs & events** | `GET /runs`, `GET /runs/{run_id}/inputs`, `GET /events`, `GET /jobs`, `GET /namespaces` | a run's pinned input dataset versions (which feature versions trained a model) |
| ★ **Governance metadata (#49)** | `GET /datasets/{name}/governance`, `PUT,DELETE /datasets/{name}/tags/{tag}`, `PUT /datasets/{name}/description` | human-curated tags + description; reads on the reader rung, writes on `can_write_data` (fail-closed), every change attributable (who/when on the node); producer facet tags UNION with curated ones |
| **Search** | `GET /search` | |
| **Health** | `GET /livez`, `GET /readyz` | |

Medallion movers, the compaction sweep, and the Ray train job are Dapr/queue-driven (no public REST surface),
so they are not in these specs; their contract is the OpenLineage events they publish, consumed here.
