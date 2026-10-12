# Event-driven medallion pipeline

The governed medallion — **bronze → silver → gold** (R23: raw is the EXTERNAL world — the IIIF Image
API, external object storage — never a catalog tier; bronze is the first Lance dataset we own) —
implemented as **event-driven microservices** on Dapr pub/sub (over NATS JetStream), not a script. One
bronze write cascades the whole chain, each hop emits OpenLineage (so the graph grows the DAG), and Dapr
propagates the trace context so the whole cascade is **one distributed trace**.

## What starts the pipeline? — the catalog's announcement of a bronze write

The lakehouse runs no ingest of its own ([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)).
Data enters only through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`,
`MergeInsertIntoTable`, `DeclareTable` + vended credentials, `RegisterTable`), written by a client: the deployer's
ingest job on its own compute (a sealed runner, CP-061, not yet built), or in development and the e2e lanes
`tests/e2e-py/bronze_seed.py`, which creates the project's bronze table if absent and inserts into it as the project
admin. A create alone is a DDL `DatasetEvent` and starts nothing; the insert is the arrival. The catalog
announces every committed write as an OpenLineage `COMPLETE` event on `lineage.events.v1`.

`medallion-producer` **subscribes** to that topic (`/bronze-arrival`) and — only for a write to
`<project>-bronze$events` (namespace `<project>-bronze`), or to a table a lane declares as its input — publishes
the first trigger `medallion.bronze`, naming the location the catalog vended for the table. So the cascade is
driven by the **arrival of data INTO bronze**, whoever wrote it. Loop-guarded: the stage runners' own
silver/gold events on that same topic are ignored, and so are the catalog's byte-free markers (register,
deregister, declare) and maintenance passes, so the head cannot self-trigger. `bronze→silver` subscribes to
`medallion.bronze` and derives silver:

```
   catalog insert into <project>-bronze$events ──catalog emits the write event──▶ lineage.events.v1 ─▶ (lineage svc → AGE)
   (a client, through the namespace doors)                                              │
                        medallion-producer  /bronze-arrival subscription ◀──────────────┘   (reacts ONLY to a BRONZE write)
                                    publishes medallion.bronze  (the FIRST trigger, carrying the vended location)
                                          │
                                          ▼
              ┌───────────────────┐   medallion.silver   ┌─────────────────┐
              │  bronze→silver    │ ───────────────────▶ │  silver→gold    │
              │  (embed_features) │                      │ (aggregate_gold)│
              └─────────┬─────────┘                      └────────┬────────┘
        emits           │ bronze$events → silver$features         │ silver$features → gold$catalog
        lineage ────────┴──▶ (lineage svc → AGE DERIVED_FROM edge) every hop                    ▼
                                                                              (terminal — no next trigger)

   resulting lineage DAG:   bronze$events ─▶ silver$features ─▶ gold$catalog
```

Bronze is the first governed tier (R23), a catalog table from its first row because it is written through the
catalog; `source_rowid` roots at bronze, minted by the first derive. Because the head is a subscriber like every
other stage, the pipeline is event-driven end to end — nothing polls or waits on a timer (GOAL 4 B2). The
producer's only write door is `POST /train`.

### Does the cascade produce real data, or just lineage?

Both modes, by a flag (`MEDALLION_COMPUTE_ENABLED`, chart toggle `medallion.compute`, **off by default**).
Off: the stage runners are pure **emitters** — they grow the lineage DAG but write no data (all the
event-driven *choreography* demo needs), so the graph asserts datasets that aren't on disk (`#23` reconcile
would flag them `missing_on_storage`). **On** (`--set medallion.compute=true`): each stage runs the
**fake-Ray compute** (`services/medallion/services/compute.py`) — a
REAL in-process Lance write: each stage runner reads its upstream Lance
dataset, stamps a `stage` provenance column, and writes the downstream dataset — so the whole loop produces
**actual versioned data** and the emitted OpenLineage carries the **real** Lance version (not a hardcoded
`1`). This is the **lance-ray seam**: the exact `read → transform → write → version` contract a
distributed Ray Data job (`lance-ray` on rask's KubeRay) swaps into in production; in-process here so the
loop is end-to-end testable without a Ray cluster (`tests/unit/test_medallion_cascade.py` runs the full
bronze→gold cascade and asserts both the data and the `DERIVED_FROM` chain).

> **The real Ray seam** (`docs/RAY.md`, `make ray-demo`): a genuine Ray cluster in kind + `ray job submit`
> runs a distributed `lance_ray` job proving Lance's distributed **write** (fragment-parallel + one commit),
> **indexing**, data **evolution** (`add_columns` + version pinning), and **compaction** against RustFS —
> the production shape this in-process `transform_stage` stands in for. Wiring the stage runners to submit Ray jobs
> (and the KubeRay operator) is the rask-merge step.

> **Compute + OpenBao:** compute-on writes to RustFS with the plaintext S3 secret, so it **requires OpenBao
> off** (`--set openbao.enabled=false medallion.compute=true`). The medallion is a dummy producer with no
> OpenBao secret-fetch (unlike the catalog), so with OpenBao on it **fails fast at boot** rather than 403'ing
> at first write. The event-driven choreography (below) runs identically with compute off.

## The services (all share the catalog image; different entrypoint)

| Service | App-id | Module | Subscribes | Publishes |
| ------- | ------ | ------ | ---------- | --------- |
| **medallion-producer** (cascade head) | `medallion-producer` | `medallion.producer:app` | `lineage.events.v1` (bronze filter, `/bronze-arrival`) + `catalog.control.v1` (`/publication-arrival`) | `medallion.bronze` on a bronze arrival; the lane's topic on a publication |
| **bronze→silver** | `bronze-to-silver` | `medallion.stage_runner:app` | `medallion.bronze` | `medallion.silver` + lineage |
| **silver→gold** | `silver-to-gold` | `medallion.stage_runner:app` | `medallion.silver` | — (terminal) + lineage |
| **media→silver** (media lane) | `media-to-silver` | `medallion.stage_runner:app` | `medallion.media` | — (terminal) + lineage |

**Every tier is governed from its first row.** Bronze is written through the catalog's namespace doors, so it
is a `table:` object before any stage reads it; the stage runners ask the catalog for their silver and gold
(`ensure_stage_output`) before they write. The maintenance policy, the protection record and every FGA grant key
off that object.

The 3 stage runners are the **same module**, differing only by `MEDALLION_*` env (from/to dataset, sub/pub
topic, operation, author) — see `chart/values.yaml` `medallion.stageRunners`. Triggers ride a dedicated
`MEDALLION` JetStream stream (`medallion.>`); the OpenLineage events ride the existing `LINEAGE` stream.

**The MEDIA lane (multimodal §9).** The media lane starts when its bronze table (`<project>-bronze-media$objects`)
is published through the catalog: `/publication-arrival` turns the catalog's `table_published` control event into
`medallion.media` (routed by `MEDALLION_TRANSFORM_ROUTES`). The
`media-to-silver` stage runner — the SAME generic stage runner binary, zero media config — carries the blob forward
and derives whatever the blob **content** supports (`medallion/services/derivers.py`: image →
inline `thumbnail` + `embedding`; unrecognised media carries through untouched; tabular datasets are a
no-op), writing `silver-media$features`. Undecodable-after-probe payloads are deterministic bad data:
the run FAILs in lineage and the trigger is DROPPED (the quality-gate contract), never retried.
Governed mode needs the media grants — `scripts/seed_medallion_fga.sh` seeds them. Ray note:
medallion-producer 0.4.2 reads blob BYTES correctly (its datasource reconstructs them via `take_blobs`), but
exposes blob-v2 columns as plain LargeBinary — so until the stage job re-attaches `blob_field` on
write and ships the deriver (+Pillow) in the ray image, blob upstreams take the in-process path.
Consumers with NO storage credentials (browser, notebook) fetch the blob bytes back through the
catalog: `GET /management/v1/table/{id}/blobs?column=payload&row=N[&version=]`, Range-capable (206/416) and
governed at reader-tier `can_read_data` — see `docs/adr/0019-feature-gap-1-serving-blob-serving-is-a-governed-proxy-not.md` "FEATURE-GAP §1 (serving)" for details.

**The cascade head's auth.** `/bronze-arrival` is sidecar-delivered: it requires the Dapr app-api-token and acts
only on an event whose `rask_signature` a signer its door allows has made ([[XC-078]]). Who may WRITE bronze is the
catalog's authorization on the namespace doors, not the producer's.

## Per-tenant zones (#84) — enabling a project's own cascade

With `medallion.projectsEnabled`, a write to `<p>-bronze$events` runs the whole cascade inside the **tenant's own
bucket** (the project's zone warehouse from the registry — multiple actives resolve deterministically to
the lowest warehouse id) with every lineage/FGA identity **project-qualified** (`<p>-bronze$events`,
`<p>-silver$features`, …): the catalog states the tenant on the write event, and `/bronze-arrival` carries it onto
the trigger. `/train` deliberately declares **no** project parameter and stays pinned to
`medallion.produceAdminProject`.

The qualified namespaces inherit **nothing** from the estate seed, so out of the box the stage runners are
denied on `namespace:<p>-bronze` and the trigger is dead-lettered — fail-closed by design (live-proven
2026-07-23: sidecar `DROP`, DLQ parking, no fallback into another tenant's roots). Enabling a tenant is
one idempotent command once its zone warehouse exists:

```bash
kubectl port-forward svc/lance-ns-openfga 8081:8080 &
OPENFGA_API_URL=http://localhost:8081 scripts/seed_medallion_fga.sh <project> <zone-warehouse-id>
```

which writes the three tuple groups: stage-namespace parents under the zone warehouse (project
admins/readers inherit visibility over their zone data), the stage runner service rungs on the qualified target
stages, and the table→namespace parent links (stage runners write Lance directly; nothing else seeds tables).
After the seed the runs appear in `/runs` for the project's members — before it they exist but are
governance-hidden, which is the same fail-closed lens, not data loss. Media lanes stay estate-only.

## Promotion gates — who *may* promote, and whether the data is *good enough* to

A stage moves data forward (fires the next trigger) only when it passes **two independent, opt-in gates** —
the distinction between a *registered validator that gates movement* and the *event-driven transform itself*:

| Gate | Flag | Question | Mechanism | Fail action |
| ---- | ---- | -------- | --------- | ----------- |
| **Authorization** | `RASK_FGA_ENABLED` | *May this identity promote?* | the stage runner CHECKs OpenFGA as its **own service identity** — silver→gold needs `can_promote` (validator rung), the others `can_create_table` (writer) | `DROP` (redelivery won't grant the role) + `medallion.stage.denied` |
| **Data quality** | `MEDALLION_QUALITY_ENABLED` (chart `medallion.quality`) | *Is the produced data good enough?* | after the compute writes the downstream dataset, the stage runner runs cheap, exact assertions on it (`row_count_positive`, `not_null` on the key column) via `services/medallion/services/quality.py` | `DROP` (deterministically bad) + `medallion.stage.quality_blocked`; the failed run + its `dataQualityAssertions` facet are **still emitted** so the bad batch is auditable in lineage |

Both gate the **same act** (promotion) from different angles, and both compose: a stage promotes only when
it is *authorized* **and** the data *passes quality*. The quality gate requires compute (there is no data to
check otherwise). A quality block is recorded on the `WROTE` edge as `quality_passed=false` *with* the real
version, so `producers()` shows exactly which batch was stopped and why. This is the automated **validator**
half of governance the [lineage doc](LINEAGE.md#runtime-measured-facets--declared--measured-lineage-marquez-goal-1--2)
describes; a future human-approval rung can layer on the same `quality_passed` signal without new plumbing.

## External edges — the ingest & egress seam

The cascade above is the *interior* (`bronze → silver → gold`). Its two edges to the outside world —
external raw in, gold egress out (R23: external raw spans at least two source families, the IIIF Image
API and external object storage via `packages/storage`) — are a **provider-agnostic seam** so no
provider SDK leaks into the pipeline:

| Edge | Contract | Adapters (concrete) | Provenance |
| ---- | -------- | ------------------- | ---------- |
| **Ingest** (source → bronze) | `SourceAdapter.iter_objects() -> SourceObject{uri, data}` (`packages/service-kit/src/service_kit/lakehouse/sources.py`) | `LocalDirSource`, `S3FileSystemSource` | each object's **source URI** is stamped as a `source_uri` column and emitted as the bronze `DERIVED_FROM` input |
| **Egress** (gold → sink) | `SinkAdapter.put(key, data) -> uri` (`packages/service-kit/src/service_kit/lakehouse/sinks.py`) | `LocalDirSink`, `S3FileSystemSink` | the returned sink URI is the **terminal** lineage output |

Real providers (IIIF / GCS / HuggingFace / HCP / any S3) are **plugins outside the lakehouse** that
implement the `SourceAdapter`/`SinkAdapter` Protocol — the `S3FileSystemSource`/`S3FileSystemSink` take a configured
`pyarrow.fs.S3FileSystem`, so MinIO, RustFS, or AWS is just a different filesystem, not different code.

The lakehouse itself runs no ingest head: a source adapter is used by the client that writes bronze through the
catalog (ADR 0154). Every cascade write sets `enable_stable_row_ids=True` — it is *create-time-only* (cannot be
turned on later), so it is set up front to keep a durable `_rowid` across compaction, which rewrites fragments and
invalidates row *addresses*. A blob flows forward through the cascade (`compute._blob_slices` reads it via one
row-aligned `blob_handling="all_binary"` scan in `MEDALLION_STAGE_BATCH_ROWS`-row slices, a null payload on its own
row — and re-wraps with `blob_array`, so a page whose harvest failed carries forward as a null instead of failing the
stage), and the silver stage derives the thumbnail + embedding — so an external image lands as a managed blob and
its origin survives in the data *and* in the graph.

## Run it

```bash
make e2e-governed-union  # the automated regression test: a bronze seed through the catalog → the governed cascade reaches gold, with its lineage
```

## What you can observe (all verified)

- **Lineage DAG** (Apache AGE): `bronze$events → silver$features → gold$catalog`. Query
  `GET /datasets/gold$catalog/upstream` — gold transitively derives from the upstream stages.
- **One distributed trace** (GreptimeDB `opentelemetry_traces`): a single `trace_id` spans `medallion-producer`,
  `bronze-to-silver`, `silver-to-gold`, **and** `lineage` — the event followed across
  every Dapr hop (the gRPC publish injects `traceparent`; each subscriber continues the trace).
- **Metrics** (PromQL): `medallion_stage_transitions_total{lance_medallion_transition}` counts each hop
  (`source->bronze`, `bronze->silver`, `silver->gold`); `medallion_stage_denied_total` and
  `medallion_stage_quality_blocked_total` count promotions stopped by the authz and quality gates.
- **Measured output** (when compute is on): each `WROTE` edge carries the runtime-measured `row_count` +
  `size_bytes` the stage actually wrote (the `outputStatistics` facet), surfaced in `producers()`.

## Why event-driven (vs. the old `scripts/medallion_demo.py`)

The script driver runs the stages in-process, sequentially. The event-driven version makes each stage an
independently deployable, independently scalable service that reacts to its upstream's completion — the
real microservices shape: a slow silver stage can't block bronze ingestion, each stage retries on its own
(Dapr redelivery + idempotent emit), and the whole flow is observable as one trace without any glue code. (True since 2026-08-22 and not before: `lance-tracing` set `samplingRate` and named NO exporter, so daprd registered its NullExporter and every sidecar span was created, sampled, propagated — and then dropped.)
