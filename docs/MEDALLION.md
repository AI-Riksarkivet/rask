# Event-driven medallion pipeline

The medallion lakehouse pattern — **raw → bronze → silver → gold** — implemented as **event-driven
microservices** on Dapr pub/sub (over NATS JetStream), not a script. One trigger cascades the whole
chain, each hop emits OpenLineage (so the graph grows the DAG), and Dapr propagates the trace context
so the whole cascade is **one distributed trace**.

## Is `lance-ray` the head of the pipeline? — Yes, and it's event-driven.

`lance-ray` is the **head of the pipeline** (a dummy Ray ingest job), **one hop upstream of bronze**. It is
NOT poked by a manual RPC: `POST /produce` (with compute) seeds `raw_events` and emits ONE OpenLineage event
*announcing that write*. `lance-ray` also **subscribes** to the shared lineage topic (`/raw-arrival`) and —
only for a write to the raw dataset — publishes the first trigger `medallion.raw`. So the cascade is driven
by the raw-data **arrival event**, not the call; any raw writer (this dummy, or the catalog) that emits a
raw-write event drives it. Loop-guarded: the movers' own bronze/silver/gold events on that same topic are
ignored, so the head can't self-trigger. `raw→bronze` subscribes to `medallion.raw` and produces bronze:

```
   POST /produce ─▶ lance-ray ──emits raw_events write event──▶ lineage.events.v1 ─▶ (lineage svc → AGE)
   (any raw writer)     ▲                                              │
                        └────────── /raw-arrival subscription ◀────────┘   (reacts ONLY to a RAW write)
                                     publishes medallion.raw  (the FIRST trigger)
                                          │
                                          ▼
              ┌─────────────────┐   medallion.bronze   ┌───────────────────┐   medallion.silver   ┌─────────────────┐
              │  raw→bronze     │ ───────────────────▶ │  bronze→silver    │ ───────────────────▶ │  silver→gold    │
              │  (ingest_events)│                      │  (embed_features) │                      │ (aggregate_gold)│
              └────────┬────────┘                      └─────────┬─────────┘                      └────────┬────────┘
        emits          │ raw_events → bronze$events              │ bronze$events → silver$features         │ silver$features → gold$catalog
        lineage ───────┴──▶ (lineage svc → AGE DERIVED_FROM edge) every hop; the lineage DAG ends up:      ▼
                                                                                              (terminal — no next trigger)

   resulting lineage DAG:   raw_events ─▶ bronze$events ─▶ silver$features ─▶ gold$catalog
```

`lance-ray` does **not** produce bronze itself — it produces `raw_events`; its `/raw-arrival` subscription
then *triggers* the `raw→bronze` mover, which produces bronze. Because the head is now a subscriber like
every other stage, the pipeline is event-driven end to end — nothing polls or waits on a timer (GOAL 4 B2).

> **`/produce` is the demo entry point, not a prod surface.** It carries no auth (it sidesteps
> `enforce_author` — a caller could trigger cascades and forge medallion provenance), so its gateway
> route is values-gated: `medallion.producer.expose` (on for the dev demo, **off in `values-prod.yaml`**).
> That closes the **external/edge** path — from the gateway, the prod head fires only from real
> raw-namespace writes via `/raw-arrival`. It does **not** close the in-cluster path: the lance-ray pod
> still serves the unauthenticated `/produce` on its ClusterIP, and this route is *not* sidecar-delivered
> so it skips `require_dapr_token`; no NetworkPolicy ships either. So an in-cluster workload can still
> reach it. Hardening that (a NetworkPolicy, or a token/authz on `/produce`) is tracked in
> `docs/DECISIONS.md`.

### Does the cascade produce real data, or just lineage?

Both modes, by a flag (`MEDALLION_COMPUTE_ENABLED`, chart toggle `medallion.compute`, **off by default**).
Off: the producer + movers are pure **emitters** — they grow the lineage DAG but write no data (all the
event-driven *choreography* demo needs), so the graph asserts datasets that aren't on disk (`#23` reconcile
would flag them `missing_on_storage`). **On** (`--set medallion.compute=true`): each stage runs the
**fake-Ray compute** (`services/medallion/services/compute.py`) — a
REAL in-process Lance write: `lance-ray` seeds `raw_events`, then each mover reads its upstream Lance
dataset, stamps a `stage` provenance column, and writes the downstream dataset — so the whole loop produces
**actual versioned data** and the emitted OpenLineage carries the **real** Lance version (not a hardcoded
`1`). This is the **lance-ray seam**: the exact `read → transform → write → version` contract a
distributed Ray Data job (`lance-ray` on rask's KubeRay) swaps into in production; in-process here so the
loop is end-to-end testable without a Ray cluster (`tests/unit/test_medallion_cascade.py` runs the full
raw→gold cascade and asserts both the data and the `DERIVED_FROM` chain).

> **The real Ray seam** (`docs/RAY.md`, `make ray-demo`): a genuine Ray cluster in kind + `ray job submit`
> runs a distributed `lance_ray` job proving Lance's distributed **write** (fragment-parallel + one commit),
> **indexing**, data **evolution** (`add_columns` + version pinning), and **compaction** against RustFS —
> the production shape this in-process `transform_stage` stands in for. Wiring the movers to submit Ray jobs
> (and the KubeRay operator) is the rask-merge step.

> **Compute + OpenBao:** compute-on writes to RustFS with the plaintext S3 secret, so it **requires OpenBao
> off** (`--set openbao.enabled=false medallion.compute=true`). The medallion is a dummy producer with no
> OpenBao secret-fetch (unlike the catalog), so with OpenBao on it **fails fast at boot** rather than 403'ing
> at first write. The event-driven choreography (below) runs identically with compute off.

## The services (all share the catalog image; different entrypoint)

| Service | App-id | Module | Subscribes | Publishes |
| ------- | ------ | ------ | ---------- | --------- |
| **lance-ray** (producer) | `lance-ray` | `medallion.producer:app` | `lineage.events.v1` (raw filter, `/raw-arrival`) + `POST /produce` | raw-write lineage → then `medallion.raw` on a raw arrival |
| **raw→bronze** | `raw-to-bronze` | `medallion.mover:app` | `medallion.raw` | `medallion.bronze` + lineage |
| **bronze→silver** | `bronze-to-silver` | `medallion.mover:app` | `medallion.bronze` | `medallion.silver` + lineage |
| **silver→gold** | `silver-to-gold` | `medallion.mover:app` | `medallion.silver` | — (terminal) + lineage |
| **lance-ray** (media head) | `lance-ray` | `medallion.producer:app` | `POST /ingest-media` | bronze-media write lineage + `medallion.media` |
| **media→silver** (media lane) | `media-to-silver` | `medallion.mover:app` | `medallion.media` | — (terminal) + lineage |

The 4 movers are the **same module**, differing only by `MEDALLION_*` env (from/to dataset, sub/pub
topic, operation, author) — see `chart/values.yaml` `medallion.movers`. Triggers ride a dedicated
`MEDALLION` JetStream stream (`medallion.>`); the OpenLineage events ride the existing `LINEAGE` stream.

**The MEDIA lane (multimodal §9).** `POST /ingest-media` on lance-ray (token-guarded like `/produce`;
compute-on only — 409 otherwise) lands external media as a bronze **blob-v2** table at format 2.2
(`bronze-media$objects`, one lineage input per source URI) and publishes `medallion.media`; the
`media-to-silver` mover — the SAME generic mover binary, zero media config — carries the blob forward
and derives whatever the blob **content** supports (`medallion/services/derivers.py`: image →
inline `thumbnail` + `embedding`; unrecognised media carries through untouched; tabular datasets are a
no-op), writing `silver-media$features`. Undecodable-after-probe payloads are deterministic bad data:
the run FAILs in lineage and the trigger is DROPPED (the quality-gate contract), never retried.
Live-regression-guarded by `make e2e-media` (part of the `make e2e` umbrella; skips on compute-off
stacks). Governed mode needs the media grants — `scripts/seed_medallion_fga.sh` seeds them. Ray note:
lance-ray 0.4.2 reads blob BYTES correctly (its datasource reconstructs them via `take_blobs`), but
exposes blob-v2 columns as plain LargeBinary — so until the stage job re-attaches `blob_field` on
write and ships the deriver (+Pillow) in the ray image, blob upstreams take the in-process path.
Consumers with NO storage credentials (browser, notebook) fetch the blob bytes back through the
catalog: `GET /v1/table/{id}/blobs?column=payload&row=N[&version=]`, Range-capable (206/416) and
governed at reader-tier `can_read_data` — see `docs/DECISIONS.md` "FEATURE-GAP §1 (serving)" for details.

**`/produce` auth (the cascade head).** `/produce` is a direct operator trigger (not sidecar-delivered), so
it is guarded by `require_dapr_token` (the shared `APP_API_TOKEN`): **no-op in dev** (unset token — `make
medallion` works), **enforced in prod** so an in-cluster workload can't forge the cascade head. The
network-isolation layer is a gated `NetworkPolicy` (`networkPolicy.enabled`, needs a policy-enforcing CNI)
restricting ingress to `lance-ray` to in-release pods — defense-in-depth, the same shape KubeRay's token
auth prescribes (network isolation primary + token secondary).

## Per-tenant zones (#84) — enabling a project's own cascade

With `medallion.projectsEnabled`, `/produce?project=<p>` runs the whole cascade inside the **tenant's own
bucket** (the project's zone warehouse from the registry — multiple actives resolve deterministically to
the lowest warehouse id) with every lineage/FGA identity **project-qualified** (`<p>-raw_events`,
`<p>-bronze$events`, …). The trigger's auth is per-project (`can_administer` on `project:<p>`); `/train`
deliberately declares **no** project parameter and stays pinned to `medallion.produceAdminProject`.

The qualified namespaces inherit **nothing** from the estate seed, so out of the box the movers are
denied on `namespace:<p>-bronze` and the trigger is dead-lettered — fail-closed by design (live-proven
2026-07-23: sidecar `DROP`, DLQ parking, no fallback into another tenant's roots). Enabling a tenant is
one idempotent command once its zone warehouse exists:

```bash
kubectl port-forward svc/lance-ns-openfga 8081:8080 &
OPENFGA_API_URL=http://localhost:8081 scripts/seed_medallion_fga.sh <project> <zone-warehouse-id>
```

which writes the three tuple groups: stage-namespace parents under the zone warehouse (project
admins/readers inherit visibility over their zone data), the mover service rungs on the qualified target
stages, and the table→namespace parent links (movers write Lance directly; nothing else seeds tables).
After the seed the runs appear in `/runs` for the project's members — before it they exist but are
governance-hidden, which is the same fail-closed lens, not data loss. Media lanes stay estate-only.

## Promotion gates — who *may* promote, and whether the data is *good enough* to

A stage moves data forward (fires the next trigger) only when it passes **two independent, opt-in gates** —
the distinction between a *registered validator that gates movement* and the *event-driven transform itself*:

| Gate | Flag | Question | Mechanism | Fail action |
| ---- | ---- | -------- | --------- | ----------- |
| **Authorization** | `MEDALLION_FGA_ENABLED` | *May this identity promote?* | the mover CHECKs OpenFGA as its **own service identity** — silver→gold needs `can_promote` (validator rung), the others `can_create_table` (writer) | `DROP` (redelivery won't grant the role) + `medallion.stage.denied` |
| **Data quality** | `MEDALLION_QUALITY_ENABLED` (chart `medallion.quality`) | *Is the produced data good enough?* | after the compute writes the downstream dataset, the mover runs cheap, exact assertions on it (`row_count_positive`, `not_null` on the key column) via `services/medallion/services/quality.py` | `DROP` (deterministically bad) + `medallion.stage.quality_blocked`; the failed run + its `dataQualityAssertions` facet are **still emitted** so the bad batch is auditable in lineage |

Both gate the **same act** (promotion) from different angles, and both compose: a stage promotes only when
it is *authorized* **and** the data *passes quality*. The quality gate requires compute (there is no data to
check otherwise). A quality block is recorded on the `WROTE` edge as `quality_passed=false` *with* the real
version, so `producers()` shows exactly which batch was stopped and why. This is the automated **validator**
half of governance the [lineage doc](LINEAGE.md#runtime-measured-facets--declared--measured-lineage-marquez-goal-1--2)
describes; a future human-approval rung can layer on the same `quality_passed` signal without new plumbing.

## External edges — the ingest & egress seam

The cascade above is the *interior* (`raw → bronze → silver → gold`). Its two edges to the outside world
are a **provider-agnostic seam** so no provider SDK leaks into the pipeline:

| Edge | Contract | Adapters (concrete) | Provenance |
| ---- | -------- | ------------------- | ---------- |
| **Ingest** (source → bronze) | `SourceAdapter.iter_objects() -> SourceObject{uri, data}` (`services/common/sources.py`) | `LocalDirSource`, `S3Source` | each object's **source URI** is stamped as a `source_uri` column and emitted as the bronze `DERIVED_FROM` input |
| **Egress** (gold → sink) | `SinkAdapter.put(key, data) -> uri` (`services/common/sinks.py`) | `LocalDirSink`, `S3Sink` | the returned sink URI is the **terminal** lineage output |

Real providers (IIIF / GCS / HuggingFace / HCP / any S3) are **plugins outside the lakehouse** that
implement the `SourceAdapter`/`SinkAdapter` Protocol — the `S3Source`/`S3Sink` take a configured
`pyarrow.fs.S3FileSystem`, so MinIO, RustFS, or AWS is just a different filesystem, not different code.

`services/medallion/services/ingest.py::ingest_to_bronze(source, bronze_uri, so)` is the ingest head: it
writes every object's bytes into a **bronze blob-v2 table at file format 2.2** (`id, payload` (blob),
`source_uri`) with **`enable_stable_row_ids=True`** and returns the source URIs for the lineage edge. The
write is **Lance's native streaming write** — a bounded-memory `Iterator[RecordBatch]` (batched by
`MEDALLION_INGEST_CHUNK_OBJECTS`/`_CHUNK_BYTES`, byte bound first) into **one atomic commit**: a mid-ingest
failure commits nothing and a failed re-ingest leaves the previous bronze fully readable; the refusal
ceilings (`MEDALLION_INGEST_MAX_OBJECTS`/`_MAX_TOTAL_BYTES` → 400) still guard against a mis-pointed prefix. Every
cascade write (bronze/silver/gold) sets that flag — it is *create-time-only* (cannot be turned on later), so
we set it up front to keep a durable `_rowid` across compaction, which rewrites fragments and invalidates row
*addresses*. Today `id` is still positional (the cascade is overwrite-only); the stable `_rowid` is the seam a
future append/upsert would key blob carry-forward on instead of `range(rows)`. The blob then flows forward through the
existing cascade (`compute._carry_forward` reads it via `read_blobs` and re-wraps with `blob_array`), and
the silver stage derives the thumbnail + embedding — so an external image lands as a managed blob and its
origin survives in the data *and* in the graph.

**Live-proven** by `scripts/media_pipeline_e2e.py` (run in-pod against RustFS): it seeds an external S3
prefix, ingests → bronze → silver → gold, egresses gold to an S3 sink, and emits lineage each hop. The
resulting AGE chain (`GET /datasets/<gold>/graph`) is `source-URI → bronze → silver → gold → sink`, every
Lance dataset at `dsv=2.2`. The `LocalDir*` adapters are unit-tested and the `S3*` adapters are unit-tested
against an in-memory fake filesystem (`tests/unit/test_ingest_seam.py`), with the S3 path proven live by the e2e.

## Run it

```bash
make medallion        # fire lance-ray /produce, then print gold's provenance (the cascade result)
make e2e-medallion    # the automated regression test: produce → assert gold derives from raw end-to-end
```

## What you can observe (all verified)

- **Lineage DAG** (Apache AGE): `raw_events → bronze$events → silver$features → gold$catalog`. Query
  `GET /datasets/gold$catalog/upstream` — gold transitively derives from all three upstream stages.
- **One distributed trace** (GreptimeDB `opentelemetry_traces`): a single `trace_id` spans `lance-ray`,
  `raw-to-bronze`, `bronze-to-silver`, `silver-to-gold`, **and** `lineage` — the event followed across
  every Dapr hop (the gRPC publish injects `traceparent`; each subscriber continues the trace).
- **Metrics** (PromQL): `medallion_stage_transitions_total{lance_medallion_transition}` counts each hop
  (`source->raw`, `raw->bronze`, `bronze->silver`, `silver->gold`); `medallion_stage_denied_total` and
  `medallion_stage_quality_blocked_total` count promotions stopped by the authz and quality gates.
- **Measured output** (when compute is on): each `WROTE` edge carries the runtime-measured `row_count` +
  `size_bytes` the stage actually wrote (the `outputStatistics` facet), surfaced in `producers()`.

## Why event-driven (vs. the old `scripts/medallion_demo.py`)

The script driver runs the stages in-process, sequentially. The event-driven version makes each stage an
independently deployable, independently scalable service that reacts to its upstream's completion — the
real microservices shape: a slow silver stage can't block bronze ingestion, each stage retries on its own
(Dapr redelivery + idempotent emit), and the whole flow is observable as one trace without any glue code.
