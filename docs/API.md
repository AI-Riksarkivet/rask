# API surface

The canonical, machine-readable contract lives in two generated OpenAPI files, refreshed from the live
FastAPI apps by `make openapi` and drift-guarded in CI by `dagger call openapi` (`.dagger/openapi.go`,
`.github/workflows/ci.yml`) — reachable locally as `make openapi-check`:

- [`catalog-openapi.json`](./catalog-openapi.json) — the catalog service
- [`lineage-openapi.json`](./lineage-openapi.json) — the lineage service

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
| ★ **Client-direct write** | spec: `describe`/`declare` (they vend the tier FGA grants and answer `managed_versioning=true`), `POST /v1/table/{id}/version/create`; rask extension: `POST /management/v1/table/{id}/credentials` | the client writes data, transaction and deletion files and a staged manifest with its vended credential; the catalog judges the staged version and finalizes it (ADR 0157). No data byte transits the catalog |
| **Schema evolution** | `POST /v1/table/{id}/{add_columns,alter_columns,drop_columns,backfill_column,update_field_metadata,schema_metadata/update}` | |
| **Versions / branches / tags** | `POST /v1/table/{id}/version/{create,delete,describe,list}`, `.../branches/{create,delete,list}`, `.../tags/{create,update,delete,list,version}` | tags are the ref plane the model `blessed` tag reuses; `version/delete` never removes the current version, nor a tagged or forked-from one that exists when it runs (a ref created during the reclaim is not seen); `version/create` answers 406 (`managed_versioning` is never advertised) |
| **Indexes** | `POST /v1/table/{id}/{create_index,create_scalar_index,index/list,index/{name}/drop,index/{name}/stats}` | |
| **Blobs** | `GET /management/v1/table/{id}/blobs` | credential-less blob read (data-reader gated) |
| **Batch** | `POST /v1/table/{batch-commit,version/batch-create}` | 406: the catalog judges and finalizes one table version at a time |
| ★ **Publication & gates** | `POST /management/v1/table/{id}/publish`, `GET /management/v1/promotions`, `POST /management/v1/table/{id}/promotion/{approve,reject}`, `POST /management/v1/project/{id}/gate/{set,describe,delete}` | the project's quality gate, run at publish; a pass moves the table's `published` tag, and with review declared a breach is held for a `can_promote` holder to approve or reject ([TIER-GOVERNANCE.md](TIER-GOVERNANCE.md)) |
| ★ **Model registry & promotion (#17/#42)** | `GET /management/v1/model` (list), `GET /management/v1/model/{model}`, `POST /management/v1/model/{model}/promote` | list = governed registry enumeration (reader-rung `list_objects` filter; candidate + blessed versions); describe (candidate vs blessed + metrics); promote = validator-gated metrics-gated move of the `blessed` tag on `models$<model>` |
| ★ **Project registry / tenants** | `GET,POST /management/v1/projects`, `GET,DELETE /management/v1/projects/{id}` | A tenant EXISTS when its registry record does — `POST` writes the record AND the `project#admin` tuple in ONE operation (the creator's; a machine identity creating a project names the person in `admin` and is never admin itself, LH-433), so existence and permission cannot drift (the list is the caller's projects — every tenant for an estate observer, else those the caller is a `member` of; create and get are estate-observer gated; a fresh project legitimately has zero warehouses). `DELETE` is gated on the project's OWN `can_administer`, refuses **409 naming its warehouses** while any remain, and has **no `cascade` parameter at all** — retiring a tenant must never destroy buckets transitively. `?force=true` overrides the `protected` flag ONLY, never the authz gate |
| ★ **Warehouse admin / physical multi-tenancy (#3-A)** | `GET,POST /management/v1/warehouses`, `GET,DELETE /management/v1/warehouses/{id}`, `POST /management/v1/warehouses/{id}/{activate,deactivate,namespaces}` | project-admin (`can_create_warehouse`) provisions a bucket per warehouse + binds namespaces to it; the project must already exist (404 naming `POST /management/v1/projects` — there is no bootstrap exception). `DELETE` gates on the **project's** `can_administer` (destroying storage is a tenant-level act), refuses **409 naming bound namespaces**, and takes three SEPARATE opt-ins that never share a default: `?cascade=true` drops those namespaces, `?purge_bucket=true` is the only thing that removes bytes (and refuses if a same-project sibling or platform reserve claims the bucket), `?force=true` overrides `protected` alone |
| ★ **Maintenance policies (#50)** | `POST /v1/table/{id}/policy/{set,describe,delete}`, `POST /v1/namespace/{id}/policy/{set,describe,delete}` | per-target compaction cadence/opt-out + old-version retention (`retention_days`/`retain_versions`), enforced by the compaction sweep; set/delete are owner-tier (`can_drop`/`can_delete`, audited), describe is reader-tier; tag-pinned versions (e.g. `blessed`) are exempt from cleanup by Lance itself |
| ★ **Access review (#51)** | `POST /v1/table/{id}/access/list`, `POST /v1/namespace/{id}/access/list` | who holds which `can_*` action, expanded through the FGA model (roles, teams, parent cascade) via ListUsers; relation set enumerated from the compiled model; owner-tier (`can_drop`/`can_delete`) with the disclosure itself audited as a distinct `access_review` event; 501 on an auth-off stack. Expansion caveats: a leaf owner sees parent-scope grantees (within-tenant, by design), and results past OpenFGA's ListUsers cap (default 1000/3s) are truncated server-side (logged) |
| **Materialized views** | `POST /v1/materialized_view/{id}/{create,refresh}` | spec-defined + FGA-typed, but the `dir` backend does not implement MVs yet (returns 501); dormant until Lance adds native MV support |
| **Transactions** | `POST /v1/transaction/{id}/{alter,describe}` | |
| **Health** | `GET /livez`, `GET /readyz` | |

## Lineage service

Read gates on `can_get_metadata` for the addressed dataset; the ingest accepts either an OIDC human or the
service-door principal (app token + allowlisted subject — how a service's self-emitted events land).

| Capability | Endpoints | Notes |
|---|---|---|
| **Ingest** | `POST /api/v1/lineage`, `GET /dapr/subscribe` | HTTP + Dapr-subscription transports into the AGE graph |
| **Dataset graph** | `GET /datasets`, `GET /datasets/{name}/{upstream,downstream,graph,creator,producers,schema,reconcile}` | dataset-level DERIVED_FROM lineage |
| **Column lineage** | `GET /datasets/{name}/columns`, `.../columns/{field}/{upstream,downstream}` | field-to-field `columnLineage` |
| **Runs & events** | `GET /runs`, `GET /runs/{run_id}/inputs`, `GET /events`, `GET /events/projection`, `GET /jobs`, `GET /namespaces` | a run's pinned input dataset versions; `GET /events` is the durable commit feed a pipeline polls ([LINEAGE.md](LINEAGE.md#the-commit-feed-get-events-is-a-pipelines-contract); forward cursor: LH-472 #598) |
| ★ **Governance metadata (#49)** | `GET /datasets/{name}/governance`, `PUT,DELETE /datasets/{name}/tags/{tag}`, `PUT /datasets/{name}/description` | human-curated tags + description; reads on the reader rung, writes on `can_write_data` (fail-closed), every change attributable (who/when on the node); producer facet tags UNION with curated ones |
| **Search** | `GET /search` | |
| **Health** | `GET /livez`, `GET /readyz` | |

The maintenance sweep is Dapr/queue-driven (no public REST surface), so it is not in these specs; its contract is
the OpenLineage events it publishes, consumed here. A user's pipeline that moves data between tiers is not a rask
service: it writes through the catalog's doors above
([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)).
