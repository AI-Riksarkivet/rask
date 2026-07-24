---
name: rask-orchestrator
description: The rask orchestrator — the reconcile→derive→submit loop that drives the HTR pipeline (IIIF → ALTO) for the Swedish National Archives. Use when changing the orchestrator tick, its idempotency/single-writer invariants, the two-lane prefetch/htr slot model, the htr-readiness gate, the runtime start/stop + per-chunk stop endpoints, or the NATS JetStream roadmap. Covers `core/services/orchestrator/{loop,derive}.py`, `services/{submission,sync}.py`, and the single `batches` table the loop reads.
---

# rask Orchestrator

The transitional control loop that turns S3 + DB state into Ray job submissions. A lifespan-managed `asyncio.Task` that ticks every `RASK_ORCHESTRATOR_INTERVAL_SECONDS` (default 60, `ge=10`). Lives in the `core` package (`components/services/core/src/core/services/orchestrator/`), composed by the thin `orchestrator` entrypoint (`:8810`).

This is domain-specific glue. For the *generic* patterns it leans on — NATS JetStream, retries, OTel — defer to `python-infrastructure`; for FastAPI/SQLModel/Alembic mechanics, `fastapi`. This skill encodes only what's non-obvious about *this* loop.

## When to use

- Editing `loop.py` (`tick` / `run_loop`), `derive.py` (`derive_state`), `submission.py` (`submit_chunk`/`stop_chunk`/`chunk_name`), or `sync.py` (`reconcile_from_s3`).
- Touching `core/models/{batch,enums,pipelines}.py` — the single `batches` table the loop reads, the `Slot` lanes, or `PIPELINE_SPECS`.
- Reasoning about why the loop must be a singleton, or planning the NATS replacement.
- Adding/changing a `RASK_ORCHESTRATOR_*` / `RASK_HTR_*` / `RASK_PREFETCH_*` setting (all in `service-kit/config.py`).

## Load-bearing facts (grounded in source)

- **The three-phase tick is `reconcile → derive → submit`.** `tick()` opens one `AsyncSession`, optionally `reconcile_from_s3` (idempotent upsert of `cached_pages`/`transcribed_pages`/`htr_status`), then `derive_state` (pure read, **no writes**), then loops `submit_chunk` over `state.{prefetch,htr}.eligible`. If `state.ok` is false, or `ray_client`/`state.prefetch`/`state.htr` is `None`, it returns early.
- **End-to-end idempotency is the whole design.** reconcile upserts (row-by-row, last-writer-wins), derive only reads, and submit is *guarded* (derive excludes in-flight + cooled-down chunks) **and** *uniquely id'd* (`chunk_name` appends a `%Y%m%dT%H%M%S` suffix because Ray's REST API rejects duplicate `submission_id`s, even for completed/deleted jobs). A crash mid-tick is safe to re-run whole. This is *exactly* the "lose-the-message-and-rerun-whole is fine" property that justifies **NATS over Dapr Workflow** — see `references/orchestrator-internals.md`.
- **Singleton invariant.** The loop is a lifespan `asyncio.Task` that MUST run in exactly ONE process. `RASK_ORCHESTRATOR_AUTOSTART` (default `false`) gates whether lifespan spawns it on boot. The fleet runs **`core-api` OFF / `orchestrator` ON**, so the loop runs in one process. This is why `orchestrator` is the *only* `replicas: 1` + `strategy: Recreate` service — never overlap two loops writing `batches`. Do not enable autostart anywhere a second copy could run.
- **Runtime control + per-chunk stop.** `POST /api/v1/orchestrator/{start,stop}` flip `app.state.orchestrator_task` via `create_orchestrator_task` / `stop_orchestrator_task` (both idempotent; `GET /state` reports `running`). `stop` cancels the tick only — already-submitted Ray jobs keep running. `POST /api/v1/chunks/{id}/stop` → `stop_chunk` calls Ray `stop_job` and clears `current_rayjob_id` on every row of the chunk.
- **Two lanes, one gate.** `Slot.PREFETCH` (CPU/network: IIIF → S3 cache) and `Slot.HTR` (GPU: `htr`/`htrflow`/`fake`/`htr_http` all share the lane so they never contend for GPUs). HTR-readiness gate: a chunk is eligible only when `cached_pages / expected_pages >= HTR_READY_FRACTION` (**0.95**) and not fully transcribed. Failed jobs get a `FAIL_COOLDOWN_SECS` (**600s**) cooldown, keyed by lane via the `submission_id` prefix.
- **The loop submits ALL eligible chunks per lane each tick** — Ray/Kueue queue the overflow. The *only* concurrency guard inside derive is per-chunk in-flight exclusion; the optional `RASK_HTR_MAX_INFLIGHT` cap (0 = unlimited) tops up to `cap - running` so a batch run can't flood the head with driver subprocesses.
- **Reconcile is throttled, not per-tick.** `reconcile_from_s3` walks the whole cache bucket (~600k keys) so `run_loop` reconciles at most every `RASK_ORCHESTRATOR_RECONCILE_SECONDS` (default 600); the *first* tick skips reconcile to start submitting immediately from existing DB state. Other ticks pass `reconcile=False`.
- **Single-table data model the loop depends on.** There is **no chunks table** — a "chunk" is the set of `batches` rows sharing a `chunk_id` (with `chunk_total`). **No foreign keys.** `manifest_status`/`htr_status` are `StrEnum`s persisted lowercase via `SAEnum(values_callable=lambda x: [e.value for e in x])` so they round-trip against postgres native ENUM or sqlite VARCHAR. The `naming_convention` is pinned on `SQLModel.metadata` **before** the first `table=True` class — otherwise Alembic autogenerates anonymous constraint names and rollbacks fail.
- **Schema changes go through Alembic.** Never `SQLModel.metadata.create_all` at startup — `make pg-migrate` (= `uv run --package core alembic upgrade head`). The ORM is backend-agnostic (sqlite dev / postgres prod via `DATABASE_URL`); the loop must not assume a driver.
- **Submission id grammar is a contract.** `<spec.name>-chunk-NNN-of-MMM-<ts>`. `spec_for_submission_id` does longest-prefix match to recover the `PipelineSpec`; `_slot_for` classifies the lane (unknown/unprefixed → `Slot.HTR`); `_chunk_id_of` regex-extracts the chunk. Break this grammar and lane classification, cooldown grouping, and per-stage telemetry all silently misroute.

## References

| Need | Read |
|---|---|
| The exact tick control-flow, the reconcile/derive/submit data path, the Ray dashboard telemetry walk, and the full NATS-over-Dapr rationale (why this loop is "lose-and-rerun-whole") | `references/orchestrator-internals.md` |
