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
 catalog ── authorizes (OpenFGA), commits the Lance write, announces it ──pub(lineage.events.v1)──▶ lineage ─▶ commit feed + edge list
                                                                                                      └─▶ forwarded to a graph backend
 the user's pipeline (on its own compute) reads bronze, writes silver through the catalog, emits its own
   │  OpenLineage run; then silver → gold the same way
   ▼
 maintenance ── expires versions, purges trash, scans orphans, reconciles stores ── emits maintenance lineage
```

## 1. A client writes, through the catalog

Data enters only through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`,
`MergeInsertIntoTable`, `DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`). The
writer is a client: the deployer's ingest job on its own compute (a sealed runner, CP-061, not yet built), or a user's
pipeline moving data between tiers, as the live lane `tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py`
does with stock pylance. Each door authorizes the caller on OpenFGA before it commits.

→ [`TIER-GOVERNANCE.md`](TIER-GOVERNANCE.md) · code: `services/catalog`

## 2. The catalog announces the commit

The catalog announces every committed write as an OpenLineage event on `lineage.events.v1`: a data write (`insert`,
`merge_insert`, an overwrite, a schema evolution) as a `COMPLETE` `RunEvent`, a definition change (create, drop,
register, declare) as a `DatasetEvent`. The event names the table, the Lance version the write produced, the branch it
landed on, the project and the verified author, and it is signed with the catalog's key.

→ [`LINEAGE.md`](LINEAGE.md#signed-events-the-rask_signature-facet) · code: `services/catalog/src/catalog/core/lineage_emit.py`

## 3. Lineage records it — feed and edge list

The lineage service consumes the topic and records each event once, as a row of the commit feed
(`provenance$events`) and, for a successful run, one edge per (input, output) pair at their versions
(`provenance$edges`). Both are Lance tables in the catalog's provenance namespace, written through the catalog's
`merge_insert` door as lineage's own principal; an insert-only merge on each row's key makes a redelivery land
nothing. The relay tick forwards every recorded event, as standard OpenLineage, to the graph backend the operator
configured (Marquez in development), which answers upstream, downstream and run-history questions.

→ [`LINEAGE.md`](LINEAGE.md) · code: `services/lineage/{services/consumer.py,services/provenance.py,services/forward.py}`

## 4. The user's pipeline moves the data on

The user's pipeline reads the tier, transforms on its own compute, writes the next tier through the catalog's doors
(step 1 again) and emits its own OpenLineage run for the transform (`POST /api/lineage/api/v1/lineage`), naming its
inputs so the edge list records what the output derives from. A pipeline outside the estate learns of a committed
write by following lineage's durable `GET /events` feed forward with its own token; the NATS topics are internal. ADR
0156 moves the run and inputs into the write itself (the spec `Context` headers, stamped by the catalog: LH-225, #180).

→ [`COMMIT-EVENTS.md`](COMMIT-EVENTS.md)

## 5. Maintenance — data lifecycle

The maintenance service (`services/maintenance`) decides and rewrites nothing (ADR 0162). It expires a table's old
versions right after a write that leaves 20 reclaimable, runs trash purge, the orphan scan, cross-store reconcile and
protection, and every 30 minutes lists the tables due for compaction or an index optimize
(`GET /management/v1/maintenance/due`). Each expiry that reclaims versions gets a versionless `expire_versions`
maintenance run, so the work shows up in the feed and the graph backend. Compaction and index builds are a bring-your-own job's, on its
own compute, committed through `version/create`, which judges them.

→ [`DURABILITY.md`](DURABILITY.md) · code: `services/maintenance`

## 6. Recovery & drift

- **Recovery:** `restore_table` rolls a dataset to any retained Lance version; the feed records the version each
  write produced.
- **Drift:** a crash between the Lance commit and the catalog's outbox stage loses that write's announcement, and
  nothing detects it yet (LH-497, #641).

→ [`LINEAGE.md`](LINEAGE.md), [`DURABILITY.md`](DURABILITY.md)
