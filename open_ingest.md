# open_ingest — where ingest, ETL and query live

Owner's ruling, 2026-07-29. Supersedes an earlier draft of this file that deferred ETL and query
wholesale and put the sink beside the tier; both were wrong and the corrections are noted inline.

> *"iiif should not be part of the medallion. ingest should probably run by an ETL process and we
> should [have] ETL as a thing in compute… and query in compute."*
> *"each tier should have generic payload."*

## R1 · The medallion is bronze→silver→gold and NOTHING else

`services/medallion` carries the IIIF harvester — reaching an external image API, paging a volume,
fetching bytes. That is **acquisition** wearing the medallion's badge. The medallion owns each tier's
schema, the transitions between them, and the lineage those emit. Where the bytes came from is not its
business.

Nine files hold IIIF today: `api/ingest_iiif.py`, `services/iiif_produce.py` (232 lines),
`services/s3_harvest.py`, `services/ingest_trigger.py`, `producer.py`, `core/config.py`,
`schemas/events.py`, `schemas/htr.py`, `services/ray_submit.py`.

## R2 · Ingest is an ETL job, and ETL lives in `compute`

`compute` is already the execution plane: it wraps the Ray Job SDK and owns submit, poll and the Serve
proxy. An ingest is a job — queue, status, retries, logs — so it belongs where jobs are, not beside the
tier it happens to write. IIIF then becomes ONE source type among several (S3 prefix, an API, a local
path), which is the shape the estate needs and the shape a medallion-hosted harvester cannot grow into.

`compute` gains **ETL** (submit a transform/ingest job, watch it) and **query** (ask the lakehouse a
question). Both are execution, both against Ray, both already have the client.

> **Correction.** An earlier draft said "the verb belongs where the data is; the run belongs where the
> jobs are", putting the trigger in the storage view. The ruling overrides it: ETL is a thing IN compute,
> not an action scattered across the surface that happens to hold the data.

## R3 · Every tier carries a GENERIC payload

`schemas/htr.py` pins an HTR-shaped gold contract **inside** the medallion. That makes the cascade a
transcription pipeline rather than a lakehouse: a second workload cannot use bronze→silver→gold without
bending its data into HTR's shape or forking the movers.

A tier should carry `{id, payload, stage, lineage, source_uri}` with the payload **opaque** — the
transform declares the shape, the tier does not. HTR's schema becomes one such declaration, owned by the
HTR job.

This is the piece that unblocks the others: while gold is HTR-shaped, moving ingest anywhere just moves
the same coupling to a new address.

---

## Shipped 2026-07-29

- **Per-store endpoint, credentials and TLS.** `Store` gained `endpoint`, `secret` and `insecure`; the
  object browser resolves a client per store. Raw lives on external HCP while the governed tiers are on
  the warehouse, and one process env holds one credential pair — which is why `images-batch` listed as
  empty against a bucket holding 3.5M objects. Credentials resolve through the Dapr secret store,
  **fail-closed**, no env fallback.
- **Attach a bucket from the UI.** `POST /v1/stores`, estate-admin gated, persisted as an estate
  document, attached stores forced read-only.
- **`compute` deploys and reaches Ray.** It was gated behind `singleTenant.enabled` — a legacy flag no
  install path sets — so `/api/ray/*` was `ERR_DIRECT_INVOKE` everywhere. Now `$svc.frontDoor`, pointed
  at the external cluster via `ray.dashboardUrl`, with its own 1536Mi tier (it was OOMKilled on the
  shared 512Mi one; it is the only fleet service importing the Ray SDK).

> **Correction.** An earlier draft of this file claimed "there is no `chart/templates/compute.yaml`".
> There never was one — `compute` renders from `fleet.yaml`, and the real defect was the gate.

## Still open

- **The move itself** (R1–R3). Three services, the chart, the gateway route table, and the tests pinning
  the current split.
- **An S3-prefix source.** Today's lanes are IIIF and object-by-object. **Idempotency is the hard part,
  not the transfer**: a re-run over a half-landed prefix must converge, or every retry doubles the table.
- **What the run shows.** The pieces exist and should be wired, not reinvented: Ray status (`compute`),
  the lineage event the write emits (`lakehouse` → Lineage), and the resulting table. A sync that
  "succeeded" with no lineage edge is a bug the UI should surface rather than report green.

**One thing makes the timing good:** the IIIF lane is the only producer of `bronze$pages`, and the Dapr
audit's M3 shows **nothing consumes its trigger** — `bronze-to-silver` expects `bronze$events` and drops
the page lane. So the move should not preserve a lane that already goes nowhere.

## Deferred — and this is narrower than it was

Not "ETL and query", which R2 places in `compute`. What is deferred is a **streaming ETL platform**:
Kafka + Flink or equivalents — a bus and a stateful stream processor, each with its own operator,
storage, checkpointing and failure modes. That lands on a plane which **already has a bus** (NATS
JetStream via Dapr), so whoever opens it decides first whether the estate gets a *second* messaging
system or grows the one it has. Two buses is the expensive mistake.

Batch ETL on Ray — which is what R2 describes — needs none of that.
