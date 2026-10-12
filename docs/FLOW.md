# End-to-end flow — what happens to a write, in order

The single read-this-first narrative of how data enters the lakehouse, moves between tiers and is traced. Each step
links to the deep doc for its subsystem. The lakehouse runs no ingest, no stage, no training and no engine
([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md),
[ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)): every write below is a client's, and the
lakehouse governs, records and announces it.

```
 a client (the user's pipeline, on its own compute)
   │  namespace doors: CreateTable / InsertIntoTable / MergeInsertIntoTable / DeclareTable / RegisterTable
   ▼
 catalog ── authorizes (OpenFGA), commits the Lance write, announces it ──pub(lineage.events.v1)──▶ lineage ─▶ Apache AGE graph
                                                                                                      └─▶ durable event log
 the user's pipeline (on its own compute) reads bronze, writes silver through the catalog, emits its own
   │  OpenLineage run; then silver → gold the same way
   ▼
 maintenance ── expires versions, purges trash, scans orphans, reconciles stores ── emits maintenance lineage
```

## 1. A client writes, through the catalog

Data enters only through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`,
`MergeInsertIntoTable`, `DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`). The
writer is a client: the deployer's ingest job on its own compute (a sealed runner, CP-061, not yet built), a user's
pipeline moving data between tiers, or in development and the e2e lanes `tests/e2e-py/bronze_seed.py`
(`seed_bronze_events`: create `<project>-bronze$events` if absent, then insert). Each door authorizes the caller on
OpenFGA before it commits.

→ [`MEDALLION.md`](MEDALLION.md) · code: `services/catalog`

## 2. The catalog announces the commit

The catalog announces every committed write as an OpenLineage event on `lineage.events.v1`: a data write (`insert`,
`merge_insert`, an overwrite, a schema evolution) as a `COMPLETE` `RunEvent`, a definition change (create, drop,
register, declare) as a `DatasetEvent`. The event names the table, the Lance version the write produced, the branch it
landed on, the project and the verified author, and it is signed with the catalog's key.

→ [`LINEAGE.md`](LINEAGE.md#signed-events-the-rask_signature-facet) · code: `services/catalog/src/catalog/core/lineage_emit.py`

## 3. Lineage records it — graph and feed

The lineage service consumes the topic and ingests each event idempotently into the AGE graph:

- `(:Run)-[:OF_JOB]->(:Job)`, `(:Run)-[:READ]->(:Dataset)` (inputs),
- `(:Run)-[:WROTE {version, schema, row_count, size_bytes, quality_passed, quality_assertions}]->(:Dataset)` (outputs),
- `(:Dataset)-[:DERIVED_FROM]->(:Dataset)`, `(:User)-[:CREATED]->(:Dataset)`.

In the same transaction it appends the full event to the durable feed (dedup'd on `(run_id, event_type, event_time)`).
Queries: `upstream`/`downstream`/`producers`/`graph`/`reconcile` + column-level lineage.

→ [`LINEAGE.md`](LINEAGE.md) · code: `services/lineage/{services/consumer.py,services/repository.py}`

## 4. The user's pipeline moves the data on

The user's pipeline reads the tier, transforms on its own compute, writes the next tier through the catalog's doors
(step 1 again) and emits its own OpenLineage run for the transform (`POST /api/lineage/api/v1/lineage`), naming its
inputs so the graph carries the `DERIVED_FROM` edge. How a pipeline outside the estate learns of a committed write is a
separate decision; the NATS topics are internal.

## 5. Maintenance — data lifecycle

The maintenance service (`services/maintenance`) runs version expiry, trash purge, the orphan scan, cross-store
reconcile and protection, and compacts tables by policy; each materially-changed dataset gets a versionless
`compaction` maintenance run so the work shows up in `producers()`. ADR 0155 decides that maintenance keeps the policy
of what needs compacting or reindexing while the rewrites leave for a bring-your-own job.

→ [`DURABILITY.md`](DURABILITY.md) · code: `services/maintenance`

## 6. Recovery & drift

- **Recovery:** `restore_table` rolls a dataset to any retained Lance version; the `WROTE` edge records the
  version each run produced.
- **Reconcile:** `GET /datasets/{name}/reconcile` cross-checks the graph's recorded version against the
  on-disk Lance version (`in_sync` / `storage_ahead` / `graph_ahead` / …) — drift detection neither Marquez
  nor Lakekeeper does.

→ [`LINEAGE.md`](LINEAGE.md), [`DURABILITY.md`](DURABILITY.md)
