# Event-driven, durable image medallion — S3 → bronze → silver → gold (QC gate)

> ⚠️ **Aspirational design sketch — NOT the implemented mechanism.** This explores a `POST /jobs` + nats-py
> pull-worker + `ray.submit_job` + Dapr-Workflow QC-gate design with image-specific transforms. The **built**
> system uses Dapr pub/sub subscriptions, an in-process fake-Ray transform, `POST /produce`, the `medallion.*`
> topics, and FGA + row-count/not-null gates — read **[`FLOW.md`](FLOW.md)** for what actually runs.

Interactive version: **`docs/image-pipeline-event-driven.html`** (open in a browser; click a flow tab, press
Play, ←/→ to step). This markdown is the standalone description.

## The shape in one paragraph
An image lands in an **external S3 bucket**. S3's own **ObjectCreated** notification is the only trigger —
**nothing polls**. A tiny bridge republishes it onto **NATS JetStream** (durable, at-least-once, DLQ).
From there each medallion hop is a **durable NATS consumer that drives Ray** and **chains the next trigger**:
bronze → silver (Ray batch embed/caption) → **gold behind a Dapr-Workflow QC gate**. Every hop emits
**OpenLineage** (`START → RUNNING → COMPLETE/FAIL`) to the lineage service, and gold embeds the *whole*
upstream lineage as a JSONB column. Durability lives at four layers: **JetStream** (message), **Lance**
(atomic version commit), **Dapr Workflow** (activity-checkpointed gold gate), and the **lineage store**.

## Why this answers "how is Ray event-driven?"
Ray is **not** event-driven by itself — there is no queue primitive in Ray. Each worker is a `nats-py`
consumer (the *bridge*) that turns a message into `ray.submit_job(...)` and the job's completion back into a
published message. The bridge is the event-driven shell; Ray is the compute inside it.

## Components
| Node | Role | What it is |
|---|---|---|
| S3 image store | source | external landing bucket; emits `s3:ObjectCreated` |
| Notify bridge | service | consumes the S3 notification (SQS/SNS or webhook) → publishes `image.landed` to NATS |
| NATS JetStream | stream | durable stream `IMAGES`; subjects `images.*` + `lineage.events`; at-least-once, redelivery, DLQ |
| Ingest worker | worker | `nats-py` pull consumer; submits the decode→bronze Ray job; owns the ack |
| Silver worker | worker | consumes `bronze.ready`; submits the batch embed/caption Ray job |
| Gold QC gate | service | a **Dapr Workflow** — durable, resumable; runs QC assertions then promotes or quarantines |
| Ray cluster | compute | reads Lance, computes, writes the output Lance dataset |
| Bronze / Silver / Gold · Lance | storage | immutable Lance datasets on S3 (every write = a new atomic version) |
| Quarantine | storage | rejected silver versions (QC fail) — no gold written |
| Lineage svc | service | OpenLineage consumer → Apache AGE graph + live status board |

## Flow 1 — Image lands → bronze (the S3 trigger)
1. **S3 → Notify** `s3:ObjectCreated` — an image is written to the landing bucket; S3 emits a notification (push; no polling).
2. **Notify → NATS** `PUB images.image.landed` — the bridge republishes onto JetStream, `Nats-Msg-Id = etag` (dedupe).
3. **NATS → Ingest worker** `pull · ack_wait` — durable pull consumer; in-flight until acked, else redelivered.
4. **Ingest → Ray** `submit_job(submission_id="bronze-<etag>")` — the bridge submits; the deterministic id makes redelivery a no-op.
5. **Ray → Bronze Lance** `lance.write_dataset` — lands the image bytes as a blob column (+ id, payload_src); new files, no mutation.
6. **Bronze → Ray** *manifest commit* → **version N** atomically visible. This is when it durably landed.
7. **Ray → Ingest** `status = SUCCEEDED` — the worker polled (or got a Ray Event Export push) and reads the version back as proof, calling `msg.in_progress()` to keep the ack alive.
8. **Ingest → NATS** `PUB bronze.ready · COMPLETE · ACK` — publish the next trigger + OpenLineage COMPLETE, **then** ack (crash before ack → safe redelivery).
9. **NATS → Lineage** `SUB lineage.events` — `(:Run ingest)-[:WROTE {version:N}]->(:bronze$images)`; status board updates.

## Flow 2 — Batch: bronze → silver (Ray batch processing)
`bronze.ready` → **Silver worker** → `submit_job` a **Ray batch** job (`map_batches(embed)` over the images) →
writes silver with **+embedding** (later +caption), keys carried forward (add_columns = data evolution = new
version) → atomic commit → worker publishes `silver.ready` + OpenLineage COMPLETE (schema + version) → lineage
records the silver version + `DERIVED_FROM` bronze. A failed batch **NAKs** → redeliver (same idempotent job) →
DLQ + OpenLineage FAIL; no half-written version is ever visible.

## Flow 3 — QC gate → gold (passes) — *the durable promotion*
Promotion is multi-step and non-idempotent, so it is a **Dapr Workflow** (not a plain worker):
1. **NATS → Gold** `schedule_new_workflow(instance_id="gold-<etag>")` — idempotent; execution state persists in the Dapr sidecar (resume-on-crash).
2. **Gold → Silver** `ctx.call_activity(run_qc)` — a checkpointed activity reads silver vN and runs assertions (null-rate, dup-rate, embedding-norm, **PII scan**).
3. **Silver → Gold** `PASS` — all assertions within contract.
4. **Gold → Gold Lance** `ctx.call_activity(write_gold)` — writes gold + embeds the **whole upstream lineage JSONB** (full DAG + every producing run, incl. failures, pulled from AGE).
5. **Gold → NATS** `PUB gold.ready · COMPLETE` — OpenLineage COMPLETE carries a **`dataQualityAssertions`** facet (spec-true).
6. **NATS → Lineage** — gold run + version + QC result land in AGE; "passed QC at T" is provenance, not a log line.

## Flow 4 — QC fail → quarantine (the gate shuts)
Same durable start → `run_qc` → **FAIL** (e.g. `pii_hits=3`, or null-rate over threshold) →
`ctx.call_activity(quarantine)` records the rejected version + reason, **gold is not written** → the workflow
emits OpenLineage **ABORT** with the failed `dataQualityAssertions` + `errorMessage`, and **does not publish
`gold.ready`** (nothing downstream advances) → lineage records the failed run with the failing assertions but
**no version and no `DERIVED_FROM` to gold** (no gold node created). The audit shows what was attempted and why
it was blocked — no fabricated lineage.

## Trigger mechanism — push vs poll
The diagram shows the **push** path (S3 Event Notifications). The alternative is a **reconcile poller** (a
cron that `LIST`s the bucket and publishes `image.landed` for unseen keys) — useful as a backstop if a
notification is missed, or where bucket notifications aren't available. Push is primary (event-driven, no
latency); the poller is the safety net. Either way a **bridge service** is what turns S3 activity into a NATS
message — Ray is never triggered directly.

## Durability checklist
- **Message** — JetStream `FILE` storage, `WORK_QUEUE` retention, `max_deliver` + DLQ, `Nats-Msg-Id` dedupe.
- **Data** — Lance writes are atomic manifest commits → immutable, time-travelable versions; a failed write never commits.
- **Workflow** — the gold QC gate is activity-checkpointed (Dapr sidecar state store); a crash resumes at the last completed activity, never re-running a passed step.
- **Lineage** — persist run-state + the events feed (don't leave them in-memory) so the status board and audit survive restart; the AGE provenance graph is already durable.
