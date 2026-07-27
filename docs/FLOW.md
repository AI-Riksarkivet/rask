# End-to-end flow — the implemented pipeline, in order

The single read-this-first narrative of the **information + batch-processing flow as it is actually built
and tested today**. Each stage links to the deep doc for that subsystem. For the *distributed* variants
(KubeRay, nats-py pull workers, Dapr-Workflow) see [§7 Future](#7-future--the-distributed-variants) — those
are aspirational and clearly marked; the docs that describe them
([`event-driven-pipeline.md`](event-driven-pipeline.md), [`image-pipeline-event-driven.md`](image-pipeline-event-driven.md))
are design sketches, **not** the current mechanism.

> **What "implemented" means here:** event-driven choreography (Dapr pub/sub over NATS JetStream), real
> versioned Lance data when compute is on, OpenLineage emitted+ingested to an Apache AGE graph, two opt-in
> promotion gates (authz + quality), and a compaction cron — all exercised by `tests/` and running in the
> `kind` cluster (`chart/`). The one piece that is *not* in-cluster is the **distributed** compute engine
> (a real Ray Data job); its in-process stand-in fills the identical contract so the loop is testable.

```
            POST /produce                 medallion.raw          medallion.bronze        medallion.silver
 (you/cron) ───────────▶ lance-ray ──pub──────────▶ raw→bronze ──pub──────▶ bronze→silver ──pub──▶ silver→gold
                         (producer)                  (mover)                 (mover)                (mover, terminal)
                            │ seed raw_events           │ transform             │ transform            │ transform
                            │ + emit lineage            │ + emit + GATE         │ + emit + GATE        │ + emit + GATE
                            ▼                           ▼                       ▼                      ▼
   every stage emits an OpenLineage RunEvent ─pub(lineage.events)─▶ lineage consumer ─▶ Apache AGE graph
                                                                                         (Run/Job/Dataset + edges)
   resulting lineage DAG:   raw_events ─▶ bronze$events ─▶ silver$features ─▶ gold$catalog
   compaction cron ──every N──▶ discover every dataset ─▶ compact + GC old versions ─▶ emit maintenance lineage
```

## 1. Ingest — the `lance-ray` producer registers the run and fires the cascade head

`lance-ray` is the **head** (a dummy Ray ingest job; `services/medallion/producer.py`, deployed as the
`lance-ns-lance-ray` pod running `medallion.producer:app`). On **`POST /produce`** it:

1. (compute on) seeds a real `raw_events` Lance dataset — the fake-Ray ingest;
2. emits an OpenLineage `RunEvent` for `raw_events` (no inputs — raw is the source).

It does **not** itself publish `medallion.raw`. `lance-ray` also *subscribes* to the lineage topic
(`/raw-arrival`) and — only for a raw-dataset write — publishes the first trigger `medallion.raw`. So the
cascade is driven by the raw-data **arrival event**, not the call (GOAL 4 B2); any raw writer (this dummy,
or the catalog) that emits a raw-write event drives it, and the head is a subscriber like every other stage.
Loop-guarded so the movers' own downstream events can't re-fire it.

A failed raw-write emit is the cascade head failing, so `/produce` returns **503** (the caller retries) —
not a silent 202. The raw-write event *is* the registered run; `/raw-arrival` turns it into `medallion.raw`,
and each mover downstream picks that up.

→ [`MEDALLION.md`](MEDALLION.md) · code: `services/medallion/{producer.py,services/produce.py}`

## 2. Transform — the movers (one Dapr subscriber per DAG edge)

The three movers run the **same** module (`medallion.mover:app`), differing only by `MEDALLION_*` env:

| Mover | subscribes | publishes | operation |
|-------|-----------|-----------|-----------|
| `raw→bronze` | `medallion.raw` | `medallion.bronze` | `ingest_events` |
| `bronze→silver` | `medallion.bronze` | `medallion.silver` | `embed_features` |
| `silver→gold` | `medallion.silver` | — (terminal) | `aggregate_gold` |

On each delivery a mover (`services/medallion/services/transform.py: handle_stage`):

1. (compute on) runs the **fake-Ray compute** — reads its upstream Lance dataset, applies a stage transform,
   writes the downstream dataset (`read → transform → write → version`), measuring exact rows + on-disk
   bytes. This is the **lance-ray seam** — the identical contract a distributed Ray Data job fills (§7).
2. runs the two **promotion gates** (§3);
3. emits the transform's OpenLineage `RunEvent` (`inputs=[upstream]`, `outputs=[downstream]` → the
   `DERIVED_FROM` edge), carrying the version + `outputStatistics` (+ `dataQualityAssertions` when the
   quality gate ran);
4. publishes the next stage's trigger (unless terminal, or blocked by a gate).

Idempotent + at-least-once safe: the run_id is `operation-token`, the graph MERGEs on it, and a transient
failure returns `RETRY` so the Dapr sidecar redelivers.

→ [`MEDALLION.md`](MEDALLION.md) · code: `services/medallion/{mover.py,services/transform.py,services/compute.py}`

## 3. Promotion gates — who *may* promote, and whether the data is *good enough*

A stage advances (fires the next trigger) only when it passes **two independent, opt-in gates** — the
distinction between a *registered validator that gates movement* and the *event-driven transform itself*:

| Gate | Flag | Question | Fail action |
|------|------|----------|-------------|
| **Authorization** (OpenFGA) | `MEDALLION_FGA_ENABLED` | May this identity promote? `silver→gold` needs `can_promote` (validator rung), the others `can_create_table` (writer) — checked as the mover's own service identity | `DROP` + `medallion.stage.denied` |
| **Data quality** | `MEDALLION_QUALITY_ENABLED` | Is the produced data good enough? assertions on the written dataset (`row_count_positive`, `not_null` on the key) | `DROP` + `medallion.stage.quality_blocked`; the failed run + its `dataQualityAssertions` are still emitted (auditable) |

Both gate the **same act** (promotion) and compose: a stage promotes only when *authorized* **and** the data
*passes quality*. A quality block is recorded on the `WROTE` edge as `quality_passed=false` *with* the real
version, so the blocked batch is auditable.

→ [`MEDALLION.md` § Promotion gates](MEDALLION.md#promotion-gates--who-may-promote-and-whether-the-data-is-good-enough-to)
· authz model: `services/common/auth/model.fga`

## 4. Lineage — emit → ingest → graph

Every stage's `RunEvent` is published to the `lineage.events` topic; the Dapr sidecar persists it to NATS
JetStream and delivers it to the **lineage service** (`services/lineage/`, a separate microservice owning
the Apache AGE graph). The consumer ingests it idempotently into:

- `(:Run)-[:OF_JOB]->(:Job)`, `(:Run)-[:READ]->(:Dataset)` (inputs),
- `(:Run)-[:WROTE {version, schema, row_count, size_bytes, quality_passed, quality_assertions}]->(:Dataset)` (outputs),
- `(:Dataset)-[:DERIVED_FROM]->(:Dataset)` (the medallion DAG), `(:User)-[:CREATED]->(:Dataset)`.

The full event JSON is also appended to a durable `/events` feed (dedup'd on `(run_id, event_type,
event_time)`). Queries: `upstream`/`downstream`/`producers`/`graph`/`reconcile` + column-level lineage.

→ [`LINEAGE.md`](LINEAGE.md) · code: `services/lineage/{services/consumer.py,services/repository.py}`

## 5. Batch processing & data lifecycle — the compaction cron

A **compaction** service (`services/compaction/`) runs on a Dapr cron binding: it discovers every dataset in
the bucket and, per dataset, compacts small fragments + `cleanup_old_versions(older_than=7d)`, then emits a
versionless `compaction` maintenance run so the GC shows up in `producers()`. Tiers differ in durability:
raw/bronze/silver are **transient** (re-derivable; their old versions are GC'd), gold/catalog are
**permanent** (the system of record). The current version is always kept (no data loss); only time-travel
*depth* is capped.

→ [`MEDALLION.md`](MEDALLION.md), [`DURABILITY.md`](DURABILITY.md) · code: `services/compaction/services/{sweep.py,optimize.py}`

## 6. Recovery & drift

- **Recovery:** `restore_table` rolls a dataset to any retained Lance version; the `WROTE` edge records the
  version each run produced.
- **Reconcile:** `GET /datasets/{name}/reconcile` cross-checks the graph's recorded version against the
  on-disk Lance version (`in_sync` / `storage_ahead` / `graph_ahead` / …) — drift detection neither Marquez
  nor Lakekeeper does.

→ [`LINEAGE.md`](LINEAGE.md), [`DURABILITY.md`](DURABILITY.md)

## 7. Future — the distributed variants

What lands when this merges into the sibling `rask` repo (see [`RASK-INTEGRATION.md`](RASK-INTEGRATION.md)):

- **Distributed compute:** the in-process fake-Ray compute (`compute.py`) is replaced by a real **lance-ray**
  Ray Data job on rask's **KubeRay** cluster — the *same* `read → transform → write → version` contract, just
  distributed. Nothing else in the flow changes.
- **Auto-instrumented lineage (GOAL 3):** instead of the mover hand-building the `RunEvent`, the lance-ray
  OpenLineage integration emits the `outputStatistics`/`dataQualityAssertions` facets **automatically** from
  the runtime — true Marquez-grade auto-lineage.
- **Other sketches:** [`event-driven-pipeline.md`](event-driven-pipeline.md) and
  [`image-pipeline-event-driven.md`](image-pipeline-event-driven.md) explore a `POST /jobs` + nats-py pull-worker
  + `ray.submit_job` + Dapr-Workflow QC-gate design. **These are aspirational** — the implemented system uses
  Dapr pub/sub subscriptions, the in-process fake-Ray transform, `POST /produce`, the `medallion.*` topics,
  and the FGA + row-count/not-null gates described above.
