# Orchestrator internals

Deep reference for the rask reconcile→derive→submit loop. The lean rules are in `SKILL.md`; this file is the data path, the invariants in detail, and the NATS roadmap. File paths are relative to `components/services/core/src/core/`.

## The tick control flow (`services/orchestrator/loop.py`)

`run_loop` ticks forever until cancelled; one failed `tick` is logged (`log.exception(...)`) and the loop continues — a bad tick never kills the loop. Cancellation (`asyncio.CancelledError`) is re-raised so lifespan shutdown is clean.

```
run_loop:
  client = ray_client                       # may be None if Ray was down at boot
  last_reconcile = now; first = True
  loop:
    if client is None:                       # cheap rebuild every tick while None
        client = build_client(ray_dashboard_url)   # via anyio.to_thread (sync SDK)
    do_reconcile = recon_secs == 0 or (not first and now - last_reconcile >= recon_secs)
    first = False
    tick(reconcile=do_reconcile)             # exceptions caught, loop continues
    if do_reconcile: last_reconcile = now
    asyncio.sleep(interval)                  # CancelledError → log + re-raise
```

`tick` (idempotent end-to-end):

1. `async with sessionmaker() as session:` — one session per tick.
2. If `reconcile and s3 is not None`: `await reconcile_from_s3(session, s3, cache_bucket, output_bucket)`.
3. `state = await derive_state(http, client, dashboard_url, session)` — pure read.
4. Early-return if `not state.ok`, or `ray_client is None`, or `state.prefetch is None`, or `state.htr is None`.
5. Prefetch lane: if `prefetch_pipeline.lower() not in PIPELINE_DISABLED` (`{"none","off","disabled",""}`), `submit_chunk` for every `cid` in `state.prefetch.eligible`.
6. HTR lane: apply the `htr_max_inflight` cap (`free = max(0, cap - len(state.htr.running))`, slice `eligible[:free]` when `cap > 0`), then `submit_chunk` for each.

A Ray client that connected once is **reused** across Ray restarts — it's a stateless HTTP wrapper that reconnects per request, so no rebuild is needed after a cluster bounce. Only a `None` client (never connected) is rebuilt each tick.

## Reconcile — the only writer phase (`services/sync.py`)

`reconcile_from_s3` lists each bucket once via `storage.iter_keys` (the canonical S3 wrapper — never `boto3` directly), groups keys by `<batch_id>/...` prefix, counts `.jpg` per batch in the cache bucket and `.xml` per batch in the output bucket, then for every `batches` row sets `cached_pages`, `transcribed_pages`, `htr_status` (via `_classify`), and stamps `started_at`/`finished_at`/`last_synced_at`. One `commit()` if any rows. The synchronous storage client is wrapped in `anyio.to_thread`. Same function backs `POST /batches/sync` and the loop — idempotent, needs no coordination.

`_classify` priority: `DONE` (`transcribed >= expected`) > `PARTIAL` (any transcripts) > `CACHED` (full cache, no transcripts) > `PARTIAL` (partial cache) > `PENDING`. When `expected` (page_count) is unknown, "any cache" counts as full.

## Derive — pure read, no writes (`services/orchestrator/derive.py`)

`derive_state` is consumed by BOTH `GET /orchestrator/state` (so the UI renders the *same* decision the loop would act on) and `tick`. It:

- Lists Ray jobs via `ray_kit.dashboard.list_jobs`; `OrchestratorState(ok=False, ...)` if the dashboard is unreachable.
- `active_jobs_for(slot)` = all jobs in the lane with status `RUNNING`/`PENDING`. `running_{pf,htr}_chunks` = their chunk ids. **This per-chunk in-flight set is the ONLY submission guard** — real concurrency limiting is Ray/Kueue's job.
- Cooldowns: every `FAILED` job whose `end_time` is within `FAIL_COOLDOWN_SECS` (600s) becomes a `Cooldown`, grouped into `cooldown_pf` / `cooldown_htr` by `_slot_for(submission_id)`.
- `prefetch_pending` = `batch_repo.prefetch_pending_chunk_ids` (chunks where any `manifest_status='ok'` batch has `cached_pages < page_count`). `ready_for_htr` from `batch_repo.chunks_with_progress`: `expected_pages` set, `transcribed < expected`, and `cached / expected >= HTR_READY_FRACTION` (0.95).
- `eligible = ready − in-flight − cooldown` per lane. `tick` submits ALL of these; overflow queues on Ray/Kueue.
- `_build_slot` enriches each running job with per-stage telemetry from `/api/v0/tasks/summarize` (filtered by `job_id`), reading `MapWorker(MapBatches(<stage>)).submit` state counts. `_stages_for` resolves stage names from the job's spec — an `htrflow-…` job has `stages=()`, so it shows NO per-stage bars rather than wrong ones force-classified against the actor-per-stage names.

The Ray State API can't filter task summary by `job_id` server-side reliably for all shapes, so derive walks the deeply-nested `/api/v0/tasks/summarize` envelope (`_cluster_summary`) defensively. `RAY_TRANSIENT_ERRORS` from `ray_kit` wraps the flaky SDK calls.

## Submit — guarded + uniquely id'd (`services/submission.py`)

`submit_chunk` reads chunk membership (`manifest_status='ok'` rows, ordered by `batch_id`), builds the entrypoint, and `ray_client.submit_job(...)` via `anyio.to_thread` (sync SDK). Key details:

- `chunk_name(chunk_id, chunk_total, spec)` → `<spec.name>-chunk-NNN-of-MMM-<ts>`. The `%Y%m%dT%H%M%S` suffix makes every submission unique because **Ray's REST API rejects duplicate `submission_id`s even for completed/deleted jobs** — without it, stop-and-resubmit would fail.
- `build_entrypoint`: `runner` specs run `uv run --project projects/runner runner --pipeline <name> ...`; `http` specs (e.g. `htr_http`) run `components/scripts/htr_chunk_job.py` POSTing to the deployed `/htr` endpoint and need `boto3` in `runtime_env` pip.
- `runtime_env` passes `working_dir`, env vars filtered to prefixes `AWS_`/`HCP_`/`IIIF_`/`RASK_`, and spec `pip`.
- When `spec.tracks_rayjob_id` (true for all current specs), tags `current_rayjob_id` + `current_rayjob_submitted_at` on every row in the chunk — that's what `stop_chunk` later reads to find the job.
- `RAY_TRANSIENT_ERRORS` → `ServiceUnavailableError`; empty membership → `NotFoundError`.

`stop_chunk` reads `current_rayjob_id` from any row in the chunk, calls `ray_client.stop_job`, and clears the markers. A pruned/restarted-cluster job that raises is treated as already-stopped so the markers still clear.

## The single `batches` table (`models/{batch,enums,pipelines}.py`)

- **No chunks table, no FKs.** A chunk = rows sharing `chunk_id` (+ `chunk_total`). All chunk membership/progress queries live in `repositories/batch.py` as group-bys on `chunk_id`.
- `HtrStatus` = `pending|cached|partial|done|verification_failed`; `ManifestStatus` = `ok|http_403|http_400|error|pending`. Both are `StrEnum` persisted **lowercase** via `_str_enum_col` = `Column(SAEnum(enum_cls, values_callable=lambda x: [e.value for e in x]))` — so they round-trip against postgres native ENUM types *or* sqlite VARCHAR with no driver assumption.
- `SQLModel.metadata.naming_convention` is set **before** the `Batch(table=True)` class — pinning `ix/uq/ck/fk/pk` names so Alembic autogenerate produces named constraints and downgrades work. Re-ordering this (defining the model first) silently breaks rollbacks.
- `PIPELINE_SPECS` (in `models/pipelines.py`) is the single source of truth for pipeline identity: `name` is simultaneously the registry key, the runner `--pipeline` value, AND the submission_id prefix — kept byte-identical to the runner's `PIPELINES` dict. `_validate_registry()` fails fast at import if names are empty/duplicate/mismatched. `Slot` (`prefetch`|`htr`) is the concurrency lane; `htr`/`htrflow`/`fake`/`htr_http` all map to `Slot.HTR`.

## Settings (`packages/service-kit/src/service_kit/config.py`)

| Env var | Default | Meaning |
|---|---|---|
| `RASK_ORCHESTRATOR_AUTOSTART` | `false` | Lifespan spawns the loop on boot. Fleet: core-api OFF / orchestrator ON. |
| `RASK_ORCHESTRATOR_INTERVAL_SECONDS` | `60` (`ge=10`) | Tick period. |
| `RASK_ORCHESTRATOR_RECONCILE_SECONDS` | `600` (`ge=0`) | Min gap between full S3 reconciles; 0 = every tick. |
| `RASK_HTR_PIPELINE` / `RASK_PREFETCH_PIPELINE` | `htr` / `prefetch` | Pipeline name per lane; must be in `PIPELINE_SPECS`. Set prefetch to a `PIPELINE_DISABLED` value (`none`/`off`/`disabled`/``) to run HTR-only. |
| `RASK_HTR_MAX_INFLIGHT` | `0` (`ge=0`) | Cap on concurrent HTR job drivers; 0 = unlimited. |
| `RAY_DASHBOARD_URL` | `http://localhost:8265` | Ray dashboard for submit + telemetry. |
| `RASK_CACHE_BUCKET` / `RASK_OUTPUT_BUCKET` | `images-batch` / `images-batch-alto` | Reconcile input/output buckets. |

## Singleton invariant & runtime control

The loop is an `asyncio.Task` on `app.state.orchestrator_task`, created by `create_orchestrator_task(app)` (the single factory used by both autostart and the `/start` endpoint) and torn down by `stop_orchestrator_task(app)` (used by `/stop` and lifespan shutdown). Because two concurrent loops would both write `batches` and double-submit, the `orchestrator` service is the only one pinned to `replicas: 1` with `strategy: Recreate` (never overlap two pods during a rollout). Extracting the loop into its own thin entrypoint is what let the API tier drop the `replicas: 1` constraint — `core-api` and `orchestrator` are two processes over one `core` brick, sharing the `batches` table transactionally.

## The NATS roadmap (why NATS, not Dapr)

Status: **transitional.** The in-process timer is explicitly a placeholder (`TODO(post-NATS)` in `loop.py`) until a **NATS JetStream consumer** replaces it — which survives core restarts and scales horizontally, giving the at-least-once guarantee without a hard `replicas: 1`.

The architecture decision (see `docs/architecture/microservices.md`) is to use **NATS JetStream, not Dapr Workflow**:

> Rule of thumb: if losing the message and re-running from scratch is fine → NATS. If you need "I was at step 3, resume at step 4" → Dapr Workflow.

The tick is idempotent end-to-end (reconcile upserts, derive is a pure read, submit is guarded + uniquely id'd), and the heavy multi-step pipeline runs on **Ray**, not in the orchestrator. So a crash mid-tick is safe to re-run *whole* — there is no partial-progress to resume. That is textbook JetStream "redeliver the whole message", and Dapr Workflow's activity-level checkpointing would be dead weight (a control plane + a sidecar per pod + a workflow state store). Decision: do not adopt Dapr; revisit only if a future per-chunk step becomes non-idempotent, is *not* delegated to Ray, and must resume exactly (a true saga).

For the generic JetStream consumer / durable-stream patterns, defer to `python-infrastructure/references/background-jobs.md` rather than reinventing them here.
