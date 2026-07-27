# Event-driven medallion — how a job completion flows across NATS

> ⚠️ **Aspirational design sketch — NOT the implemented mechanism.** This explores a `POST /jobs` + nats-py
> pull-worker + `ray.submit_job` + Dapr-Workflow QC-gate design. The **built** system uses Dapr pub/sub
> subscriptions, an in-process fake-Ray transform, `POST /produce`, the `medallion.*` topics, and FGA +
> row-count/not-null gates — read **[`FLOW.md`](FLOW.md)** for what actually runs.

Interactive version: **`docs/event-driven-pipeline.html`** (open in a browser; click a flow tab, press
Play, use ←/→ to step). This markdown is the standalone description.

## The one idea
**NATS never watches S3 and knows nothing about Lance.** It only guarantees a *message* is durably
stored and redelivered until acked. "Did the Lance table finish landing?" is answered by the
**consumer/worker**, which:
1. *submitted* the Ray job, then
2. *polled* its status until `SUCCEEDED` (and read `ds.version` back as proof), then
3. **re-published** that fact as a *new* message (`silver.ready`) — which is the next stage's trigger —
4. and only then **acked** the original trigger.

So a "completion" is just another published message. Ray computes, NATS transports, the worker is the
bridge that turns "the write committed a new Lance version" into an event.

## Components (nodes)
| Node | Role | What it is |
|---|---|---|
| Person / client | user | Orders a job (or a scheduler / the UI does) — `POST /jobs`, returns a job id immediately |
| Job API | service | FastAPI; verifies **OIDC + OpenFGA**, then publishes the trigger to NATS |
| NATS JetStream | stream | Durable stream `MEDALLION`; subjects `medallion.*` + `lineage.events`; at-least-once, redelivery, DLQ |
| Embed worker | worker | `nats-py` durable **pull consumer** — the bridge; submits Ray jobs, owns the ack |
| Ray job | compute | Reads input Lance, computes features, writes the output Lance dataset to S3 |
| Lance · S3 | storage | RustFS (S3-compatible); append-only, immutable versions (commit = a new version) |
| Lineage svc | service | A JetStream consumer of `lineage.events`; OpenLineage → Apache AGE → the SvelteKit UI |

## Flow 1 — Job handoff (bronze → silver) — *the answer to "how does it know it finished?"*
1. **Person → Job API** `POST /jobs` — returns a job id fast; does not wait.
2. **API → NATS** `PUB medallion.bronze.ready` — server ACK = durably stored (`Nats-Msg-Id` for dedupe).
3. **NATS → Worker** — durable pull consumer fetches it; message is *in-flight*, ack pending (`ack_wait`).
4. **Worker → Ray** `submit_job(submission_id="embed-<msg-id>")` — the bridge; id makes redelivery safe.
5. **Ray → S3** `lance.write_dataset(...)` — writes *new* data files (no in-place mutation).
6. **S3 → Ray** *manifest commit* → **version 1** becomes visible atomically. **This is when it "landed."**
7. **Ray → Worker** `get_job_status → SUCCEEDED`, `version == 1`. The worker *polls* — calling
   `msg.in_progress()` to keep the NATS ack alive during the long job. **This is how it knows.**
8. **Worker → NATS** `PUB medallion.silver.ready {uri, version:1}` + `PUB lineage.events COMPLETE`,
   **then** `msg.ack()`. Publish-then-ack: a crash before the ack → NATS redelivers → idempotent re-run.
9. **NATS → Lineage** — the lineage consumer ingests COMPLETE; `(:Run)-[:WROTE {version:1}]->(:Dataset)`.

## Flow 2 — Failure → redelivery → DLQ (why nothing corrupts)
1. Deliver (attempt #1). 2. Submit Ray. 3. **Write aborts** (CUDA OOM) — Lance never commits the
manifest, so **no new version**; half-written files are orphaned, never visible (the table stays at its
last good version). 4. `status = FAILED`. 5. Worker **NAK** + `PUB lineage.events FAIL` — but **no**
`silver.ready`, so the pipeline does not advance. 6. NATS **redelivers** (same `submission_id` → the
whole idempotent job re-runs), up to `max_deliver`. 7. Exhausted → **TERM** + publish to `medallion.dlq`
for an operator. Nothing is silently dropped; nothing downstream sees a partial write.

## Two different views (don't conflate them)
What we built in the demo is a **provenance graph**: who derived what, from where, final versions —
append-only, terminal. It deliberately does **not** show *progress*, *in-flight failures*, or *where the
pipeline is right now*. Those are a **live run-status** view, and the event-driven design needs both:

| View | Question | Source |
|---|---|---|
| **Provenance** (AGE graph) | who derived what, final versions, by whom | terminal `COMPLETE`/`FAIL` OpenLineage |
| **Live status** | what's queued / running (%) / failed / done — *now* | full `START`→`RUNNING`→`COMPLETE`/`FAIL` lifecycle + NATS queue depth |

The fix is to capture the **whole run lifecycle**, not just the terminal event, and keep the **current
state per run** (last transition wins) plus a transition log.

## Flow 3 — Live run status (progress + failures)
1. **START** on pickup → run state = `RUNNING` (otherwise the graph only shows finished work).
2. **RUNNING** heartbeats carry a progress facet (`batch 7/12`).
3. **FAIL** mid-flight carries the error + attempt → state flips to `FAILED` (doesn't vanish or hang
   "pending").
4. The service stores `run.state` (last-wins) + `progress`/`attempt`/`error`/`version`.
5. UI = a **status board**: each stage coloured by current state (queued / running+% / failed+error /
   done+version) + **NATS queue depth** (pending / redelivered / DLQ) for what's backed up or stuck.

## Flow 4 — Ray Event Export (push) — Ray 2.49+, alpha
Instead of the worker *polling* `get_job_status`, each Ray node's **aggregator agent** POSTs
`TASK_LIFECYCLE_EVENT` / `DRIVER_JOB_LIFECYCLE_EVENT` (state transitions + timestamps + retries) to a
configured HTTP endpoint — same webhook shape as OpenLineage's HTTP transport.
```
RAY_enable_core_worker_ray_event_to_aggregator=1
RAY_DASHBOARD_AGGREGATOR_AGENT_EVENTS_EXPORT_ADDR=http://<receiver>/ray-events
```
- **Push (Event Export)** → auto-feeds the lifecycle (`RUNNING`/`FINISHED`/`FAILED`) → OpenLineage
  RunState, **no polling, the worker can fire-and-forget**. *This is what gives you progress + failures
  + current-state for free.*
- **Pull (State API)** → `ray.util.state.get_task` / `list_tasks` / `summarize_tasks` is the reconcile
  path (recover a missed push; UI on-demand).
- **Caveat:** Ray's `FINISHED` means the *compute task returned* — it does **not** know about the Lance
  write or its version. So Ray events = the **lifecycle/timing** source; the **dataset facets (which
  table, which version)** still come from the job's reported output, joined by `jobId`. Keep the job's
  own `*.ready{version}` publish as the **authoritative pipeline trigger** (Event Export is alpha). The
  lineage service stitches Ray-lifecycle + job-output.

## Is this faithful to the OpenLineage spec + Marquez? (grounding)
Yes — with one honest caveat on *progress*.

- **OpenLineage spec (`run-cycle`):** six run states `START`/`RUNNING`/`COMPLETE`/`ABORT`/`FAIL`/`OTHER`;
  events for a run are **accumulative**; `COMPLETE`/`ABORT`/`FAIL` are terminal. The spec explicitly
  describes a long-running job as "a `START` event followed by a series of `RUNNING` events that report
  changes in the run or emit performance metrics." So the full lifecycle (progress via `RUNNING`,
  failures via `FAIL`/`ABORT`, current state) is exactly the spec's design — **our terminal-only emit is
  a demo simplification, not a spec limit.** (Also: the spec has 3 event types — `RunEvent`/`JobEvent`/
  `DatasetEvent` — we only handle `RunEvent`.)
- **Marquez (reference server):** ingests all run states and shows a **run-status view** — runs as
  `NEW`/`RUNNING`/`COMPLETED`/`FAILED`/`ABORTED` + duration + the job's `latestRun.state` (its
  `RunStatus`/`Runs` UI). A status board is Marquez-native; we simply hadn't modelled `RUNNING`/
  current-state yet.
- **Caveat — granular % progress is NOT a standard facet.** `RUNNING` reports "changes/metrics" and
  Marquez shows *state + duration*, not a % bar. So the `progress:{done,total}` in flows 3/4 is a
  **custom facet** (spec-legal: needs `_producer` + `_schemaURL`), not a built-in.
- **Spec facets for "where/why" we don't yet capture** (Marquez surfaces them): `parent` (job hierarchy),
  `jobDependencies` (control-flow: why a run waits on another), `processingEngine` (Ray/Spark version),
  `test`/`dataQualityAssertions`. Tracked in todo #10b / #12b / #17.

## How this maps to the current demo
Today `scripts/medallion_demo.py --step N` is the **synchronous** version: it does the Lance write *and*
emits OpenLineage inline. The event-driven version keeps the exact same Lance ops and OpenLineage
payloads, but splits them across NATS: the `--step` becomes a published trigger, the write becomes a Ray
job a worker submits, and "done" becomes a published `*.ready` event. **Where Dapr fits:** the
idempotent batch legs (bronze→silver, silver→silver) need only NATS + Ray. The human-ordered,
multi-step silver→gold *promotion* uses a **Dapr Workflow** (durable, resumable); auth is checked once at
the scheduling edge (OIDC) + per-activity (OpenFGA, token-independent), with the verified `sub` captured
as durable workflow input. See the chat thread / `docs/DECISIONS.md` #16 (Dapr workflow for silver→gold promotion).
