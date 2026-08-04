# The ingest plane — how it actually works

`services/ingest`, branch `ingest-plane`. This is the sketch to check my understanding against
yours: what the plane is, what each part does, what runs where, and what is NOT done.

## The one-line summary

**Acquisition became a platform plane instead of a route.** One source-agnostic door accepts a run
and returns immediately; a durable workflow enumerates the source, queues its units, drains them
through workers, and commits the whole run as exactly ONE governed Lance version — which the
catalog announces, and the cascade rides.

## Why it exists (the defect it replaces)

The medallion's IIIF head declared `202 Accepted` on a **fully synchronous** handler: the request
was held open through a sequential per-page harvest. It accepted an `Idempotency-Key` that
deduplicated *nothing* — the token made the run id converge while the work re-ran in full. And IIIF
was welded across twelve files, so adding S3-prefix ingest meant repeating all of it. `S3PrefixSource`
sat written, unit-tested, and unreachable for months.

## The shape

```
   POST /api/ingest                 (202 in ~50ms — measured, contract)
        │
        ▼
   ┌──────────────┐   schedules    ┌───────────────────────┐
   │ ingest API   │───────────────▶│  Dapr Workflow        │  durable; survives the pod
   │  :8830       │                │  `ingest_run`         │
   └──────────────┘                └───────────┬───────────┘
        ▲  GET status                          │
        │  (reads the ENGINE, not a cache)     │ 1. emit_start        → lineage START
        │                                      │ 2. ensure_dataset    → catalog CREATE (empty)
        │                                      │ 3. enumerate_chunks  → keys only, no bytes
        │                                      │ 4. child per chunk ──┐
        │                                      │ 5. finalize          │
        │                                      │ 6. emit_terminal     │
        │                                      └──────────────────────┘
        │                                                  │
        │                        ┌─────────────────────────▼──────────────────┐
        │                        │  chunk_run (child workflow, one per 1000)  │
        │                        │    publish_units → NATS JetStream          │
        │                        │    drain_chunk   → the worker              │
        │                        └─────────────────────────┬──────────────────┘
        │                                                  │
        │        ┌─────────────────────────────────────────▼───────────────────┐
        │        │ WORKER, per unit:                                            │
        │        │   fetch (scheme-resolved: file:// s3:// https://)             │
        │        │   validate (packages/validate — corruption, pre-bronze)       │
        │        │   write fragment  ─────────────▶ object store                 │
        │        │   STAGE its identity beside it ─▶ object store   ◀── the ack  │
        │        │   ack  (only now)                                   contract  │
        │        └──────────────────────────────────────────────────────────────┘
        │
        └── finalize: discover staged fragments from STORAGE, commit ONE version
                      through the CATALOG → catalog emits its own write lineage
                                                │
                                                ▼
                             medallion /bronze-arrival  →  medallion.bronze
                                                │
                                     bronze→silver→gold movers
```

## The invariants, and what each one prevents

| | Rule | The failure it prevents |
|---|---|---|
| **I1** | Sources are REGISTERED, never hardcoded | IIIF welded across 12 files; a written-but-unreachable adapter |
| **I2** | No dataset PATHS — the caller names `{project, dataset}`, the catalog vends the location | two callers composing the same table differently; volume B overwriting volume A |
| **I3** | Exactly ONE module may import `nats` | the broker becoming a code dependency instead of a chart value |
| **I4** | Exactly ONE module may write Lance | two commit paths; an unexplainable version |
| **D6** | ONE commit per run | an observable half-ingested tier; silver asking "is ingest finished?" instead of "did a publication happen?" |
| **A1** | 202 in under a second | the declared-async handler that blocks |
| **A2** | The key dedupes the WORK | a retry re-harvesting an entire volume |
| **A8** | Green run + no lineage = a DEFECT the UI shows | data that landed with nothing able to explain where it came from |
| **A13** | No completion polling | an ack held across a job's runtime → redelivery forever |

## The three design decisions worth checking

**1. Chunks, never units.** A run is millions of page images. Persisting and replaying a million
activity results would melt the state store — which is what made a separate tracker look necessary.
One child workflow per ~1000 keys returns ONE compact result, and the workflow's own durable history
becomes the ledger. *That is what dissolved `packages/tracker`* — it now has zero consumers.

**2. The ack means the work survived — so identity is staged, not returned.** A worker writes the
bytes, then writes the fragment's IDENTITY beside them, and only then acks. Before staging existed,
the identity lived solely in the worker's return value: a pod dying after an ack left the bytes on
the store with no name, and the unit already gone from a WORK_QUEUE stream. Unrecoverable, and
invisible — the run completes and reports fewer rows than it fetched. `finalize` reads the staging
prefix, so storage is the truth.

**3. The run's state is the WORKFLOW's, not a cache.** `GET /api/ingests/{id}` reads the Dapr engine
and rebuilds the accepted record from the workflow's own input. That is why a killed pod does not
make a live run vanish, and why status is right across replicas.

## What runs where

| Piece | Where |
|---|---|
| ingest API + workflow worker | one pod, `rask-ingest:8830`, with a daprd sidecar |
| the workflow engine | the daprd sidecar, on the actor state store |
| the unit queue | NATS JetStream, `INGEST` stream, WORK_QUEUE retention |
| fragments + staging | RustFS (S3), inside the dataset the catalog vends |
| the commit | the catalog service `:2333` (`create` server-side, `commit` client-direct) |
| provenance | the lineage service `:8000` → AGE |
| public route | gateway `:8888` → `/api/ingest/*` → `/api/*` on the service |
| the UI | compute zone, `/compute/ingest/<run_id>` |

## Verified in-cluster (not inferred)

```
OK  A1 — 202 in 0.052s            OK  A8 — no provenance defect
OK  run reached COMPLETE          OK  A2 — deduplicated onto the same run
OK  bronze committed at version 2 OK  A5 — corrupt page named, 3 good pages landed
OK  4 rows landed                 OK  A3 — pod force-killed, all 4 rows still landed
```
Plus 5 Playwright tests against the deployed lane, and 2075 Python tests.

Re-runnable: `scripts/ingest-lane.sh {deploy|run|corrupt|kill}`.

## NOT done — read this before trusting the diagram

* **The cascade moves no DATA.** The trigger chain is proven (`POST /bronze-arrival 200` →
  `POST /medallion-event 200`), but the movers' `MEDALLION_FROM_URI`/`TO_URI` are unset in this
  slice, so `handle_stage` skips its compute path entirely (`transform.py:210`). bronze→silver→gold
  is proven as TESTS, not as a running lane.
* **The trigger fires on table CREATE, not on every INSERT.** Measured: the first ingest into a table
  cascades; the next does not.
* **`packages/tracker` still exists with zero consumers.** Dapr Workflow dissolved its reason to be.
  Deleting it is an owner call.
