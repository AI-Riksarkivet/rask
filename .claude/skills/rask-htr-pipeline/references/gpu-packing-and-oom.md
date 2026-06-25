# GPU packing, the OOM cascade, and the fan-out workaround

Deep reference for the load-bearing rules in SKILL.md. Every number here is from
`components/cli/runner/src/runner/{pipeline.py,transcribe_service.py,htrflow_service.py}`.

## The GPU-fraction budget

Each physical GPU has a `1.0` budget in Ray's resource accounting. The HTR
pipeline splits its GPU consumers into two classes:

1. **Token slots** — the Layout (`Riksarkivet/yolov9-regions-1`) and Lines
   (`Riksarkivet/yolov9-lines-within-regions-1`) actors each request
   `num_gpus=0.001`. This does **not** reserve meaningful VRAM; it exists only
   to make the Ray scheduler place those actors **on the GPU node** (the YOLO
   models are small enough to share a card). Two stages × 0.001 = 0.002.

2. **Real GPU budget** — lives entirely in **Ray Serve**, not in the pipeline.
   `transcribe_service.py` and `htrflow_service.py` share two env knobs:

   ```python
   SERVE_REPLICAS = int(os.environ.get("RASK_SERVE_REPLICAS", "2"))
   SERVE_GPU_FRAC = float(os.environ.get("RASK_SERVE_GPU_FRAC", "0.49"))
   ```

   `@serve.deployment(num_replicas=SERVE_REPLICAS, ray_actor_options={"num_gpus": SERVE_GPU_FRAC, ...})`.

### The packing arithmetic (default 2-GPU co-residence)

The defaults are tuned so **both** Serve apps (`transcribe` + `htrflow`) fit on a
2-GPU pool simultaneously:

```
2 apps × 2 replicas × 0.49 GPU = 1.96 GPU   (≤ 2.0 physical) ✓
+ pipeline token slots: 2 × 0.001 = 0.002
                              total ≈ 1.962 GPU
```

`0.49` (not `0.5`) leaves a sliver of headroom on each card for the `0.001`
token-slot fractions to land. **The invariant: the sum of every `num_gpus`
claim across the pipeline AND every running Serve app must stay ≤ the physical
GPU count.** Break it and replicas/actors hang `PENDING` forever (Ray won't
schedule a fractional request it can't satisfy).

`max_ongoing_requests=2` on `TranscribeService` lets each replica pipeline two
batches (one preprocessing on CPU while the previous decodes on GPU);
`HTRFlowDeployment` uses `max_ongoing_requests=4`.

### Retargeting to a 3-GPU node — edit ALL THREE files

GPU sizing is hardcoded for a 3-GPU node. To retarget hardware you touch:

- `transcribe_service.py` — `RASK_SERVE_REPLICAS` / `RASK_SERVE_GPU_FRAC` defaults (env-overridable).
- `htrflow_service.py` — same two knobs (it deliberately shares them) + the optional `RASK_SERVE_GPU_RESOURCE` tier pin.
- `pipeline.py` — the literal `ActorPoolStrategy(size=...)` counts and `num_gpus=0.001` token slots (NOT env-driven).

You can raise replicas via env without code edits, **but** re-check the
fractional sum and host-RAM headroom (next section) every time.

## The 6→4→2 OOM cascade (the expensive lesson)

The GPU transcribe stage was originally a Ray Data **actor pool** of 6.
`pipeline.py`'s docstring records the postmortem: **6 GPU workers each held
~4 GB of TrOCR weights in host RAM. That saturated host memory, the kernel OOM
killer fired, reaped `dashboard_agent`, and that fate-killed the raylet** —
taking the whole job down. It was dropped 6 → 4, then the GPU work was moved out
of Ray Data into Ray Serve entirely; the Serve default is now **2 replicas**.

Lessons that survive into any future edit:

- **Host RAM, not VRAM, is the binding constraint when scaling GPU workers.**
  Each worker that loads TrOCR costs ~4 GB resident before any inference. The
  OOM killer doesn't pick your worker — it picks Ray's `dashboard_agent`, and
  losing that kills the raylet.
- **Don't size by VRAM headroom alone.** `0.49 × 2` looks like it fits 2 cards
  trivially; the failure mode was system memory pressure, invisible in
  `nvidia-smi`.
- A leftover `transcribe_concurrency = 3` in `pipeline.py` is **vestigial** —
  used only for `from_items(..., override_num_blocks=max(transcribe_concurrency, len(keys)))`.
  It is **not** the GPU actor count. The GPU parallelism is `RASK_SERVE_REPLICAS`.

## `transcribe_batch = 64` gates ALTO latency

`transcribe_batch = 64` is a **fixed literal** in `htr_pipeline` (passed as
`batch_size` to the `TranscribeViaServe` `map_batches`), and mirrored as
`MAX_BATCH = 64` inside `transcribe_service.py`. It is **not** derived from
`len(keys)`.

Why fixed matters: a Transcribe block must fully transcribe before
`AltoExportActor`/`AltoWriterActor` see it, so batch size sets how early the
**first ALTO files land in S3**. The old `len(keys) / concurrency` heuristic
meant a 7,348-page chunk produced its first ALTO write only **~85 min** in.
Fixed batch=64 → first writes within **~5 min** regardless of chunk size, and
Ray Data still has enough blocks to keep all actors busy (any chunk ≥ 256 pages
fans out fully). Per-actor TrOCR throughput is tuned separately inside the Serve
replica (preprocess `ThreadPoolExecutor(max_workers=PREPROCESS_WORKERS)`,
`PREPROCESS_WORKERS = 4`, `max_new_tokens=128`).

> Note: `pipeline.py`'s docstring mentions `MAX_BATCH=256` / `PREPROCESS_WORKERS=4`
> "in `TranscribeActor`", but the live Serve constants in `transcribe_service.py`
> are `MAX_BATCH = 64` and `PREPROCESS_WORKERS = 4`. Trust the Serve module — the
> docstring describes the pre-Serve actor.

## The streaming-executor fan-out workaround

Ray Data's streaming executor (`select_operator_to_run` in
`streaming_executor_state.py`) ranks operators by **smallest out-queue** and
schedules whichever is smallest — deliberately keeping queues short. In a tight
6-stage pipeline that pins every queue at ~1 block, so **only ~1 actor per stage
ever has work** and 2/3 GPUs sit idle. Three independent levers beat this:

1. **`ctx.execution_options.actor_locality_enabled = False`** — stop biasing
   dispatch toward the actor that produced the most recent block (the
   sticky-warm pattern). Set on the `DataContext` at the top of `htr_pipeline`.

2. **`ctx.target_max_block_size = 16 * 1024 * 1024`** (16 MiB, down from the
   128 MiB default) — more, smaller blocks → more bundles in flight at the slow
   Transcribe stage → `select_actors` actually fans out across the pool instead
   of always picking the first warm one.

3. **`ActorPoolStrategy(size=N)` with the autoscaler OFF** — `concurrency=(N,N)`
   *empirically* kept the autoscaler in play and biased work to whichever actor
   warmed first (one actor per stage, 2/3 GPUs idle). `size=N` removes the
   autoscaler entirely so blocks dispatch across the full fixed pool. Used on
   every `map_batches` in `htr_pipeline`.

4. **Intra-task `SHARDS = 3` fan-out** — even with the above, the executor only
   ever has ~1 `TranscribeViaServe` / `HTRFlowViaServe*` **task** in flight. So
   each task itself splits its crops into 3 shards and fires them to Serve
   simultaneously (`self._handle.transcribe.remote(shard) for shard in shards`),
   collecting `.result()` after. Serve's round-robin router spreads the shards
   across replicas, so all replicas run concurrently. `_shard` is **round-robin,
   not contiguous**, because crops are length-bucketed (sorted by width) first —
   a contiguous split would dump all the long lines into one shard and blow that
   replica's decode wall time.

   `SHARDS = 3` is hardcoded to the *target* replica count; with the current
   default of 2 replicas the third shard just shares a replica — harmless, and
   correct again the moment you bump replicas to 3.

## Symptom → cause cheat sheet

| Symptom | Likely cause | Fix |
|---|---|---|
| Raylet dies mid-job, `dashboard_agent` gone from logs | Host-RAM OOM from too many TrOCR-loaded replicas | Lower `RASK_SERVE_REPLICAS`; check RSS, not VRAM |
| Serve replicas stuck `PENDING` | Fractional GPU sum > physical GPUs | Re-do the budget arithmetic; lower `RASK_SERVE_GPU_FRAC` or replica count |
| Only 1 GPU busy, others idle | Streaming-executor queue/locality bias, or PageLoader head too narrow | Confirm the 3 fan-out levers; widen PageLoader `size=` |
| First ALTO lands very late in S3 | `transcribe_batch` too large / derived from chunk size | Keep it fixed at 64 |
| `CudnnModule` / pickling error on deploy | torch imported at module scope in a Serve file | Move all torch/transformers imports inside method bodies |
