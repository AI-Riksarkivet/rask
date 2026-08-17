---
name: rask-htr-pipeline
description: 'runners/htr — the HTR image→ALTO pipeline: Ray Data actor fan-out, Ray Serve TrOCR/HTRflow GPU packing, HF model-revision pinning. Use when editing pipeline.py / transcribe_service.py / htrflow_service.py / deploy_serve.py / htr/models.py; deploying or submitting a run (`make serve-up`, the `runner` CLI, KubeRay `--address ray://`); sizing GPU fractions, Serve replicas, actor-pool sizes or transcribe batch — including for new GPU hardware; changing an HTR model repo or `RASK_HTR_MODEL_REVISION`; or debugging a raylet-killing OOM, Serve replicas stuck PENDING, idle GPUs, ALTO landing late in S3, or a `CudnnModule` pickling failure on deploy.'
---

# rask HTR pipeline (Ray Data + Ray Serve)

Distributed image→ALTO HTR — **ONE example workload on an agnostic multimodal platform, not what rask is.** Everything in this file is scoped to `runners/htr`, a sealed project with its own lockfile; if a rule here needs to hold for the catalog, the cascade, the sweep, lineage or notifications, it is in the wrong file, because those seams must serve audio and video equally. A `runner` CLI invocation submits **one Ray Data pipeline** and blocks on `.materialize()`; the GPU stage is fronted by a **persistent Ray Serve** deployment that stays warm across submissions. **A naive edit to the GPU math reintroduces a raylet-killing OOM** — read the rules before touching fractions, pool sizes, or batch.

## When to use

- Editing `runners/htr/src/runner/{pipeline.py,transcribe_service.py,htrflow_service.py}`.
- Tuning GPU fractions, Serve replica counts, actor-pool sizes, or `transcribe_batch`.
- Retargeting to different GPU hardware (the live Serve packing targets a **2-GPU** pool: `0.49 × 2 replicas × 2 apps = 1.96`; the pipeline's actor-pool sizes are separately hardcoded literals).
- Debugging: OOM-killed raylet, idle GPUs (work stuck on one actor/replica), or ALTO landing late in S3.

## Architecture in one breath

Two pipeline shapes, both starting at `PageLoaderActor` (S3 read-through cache, IIIF on miss):
- **Actor-per-stage** (`htr_pipeline`): `PageLoader → Layout → Lines → TranscribeViaServe → AltoExport → AltoWriter`. GPU for YOLO regions/lines (`num_gpus=0.001` token slots) + TrOCR via Serve.
- **`/htrflow` collapse** (`htrflow_pipeline`): `PageLoader → HTRFlowViaServeBytes → AltoWriter`. One Serve replica owns region-YOLO + line-YOLO + TrOCR + ALTO serialize.

**The GPU work is NOT a Ray Data actor.** It lives in Ray Serve (`TranscribeService` / `HTRFlowDeployment`), deployed independently by `make serve-up` (= `runners/htr/scripts/deploy_serve.py up` — the script lives **inside the sealed runner**, so every invocation carries `uv run --project runners/htr`; app names `transcribe`→`/transcribe`, `htrflow`→`/htrflow`). `TranscribeViaServe` and `HTRFlowViaServe*` are **CPU-only** `map_batches` steps that block on a `serve.get_app_handle(...)`. Serve replicas keep TrOCR weights warm, so each `runner` invocation skips the ~30 s cold-start.

## Invariants (every row holds — check all of them before shipping a GPU-math edit)

| Rule | Where | Why |
|---|---|---|
| GPU budget lives in **Serve**, not the pipeline | `transcribe_service.py` / `htrflow_service.py`: `RASK_SERVE_GPU_FRAC=0.49`, `RASK_SERVE_REPLICAS=2` | `0.49 × 2 replicas × 2 apps = 1.96 GPU` on a 2-GPU pool, leaving ~0.04 for the pipeline's `num_gpus=0.001` Layout/Line token slots. Fractional sum **must stay ≤ physical GPUs**. |
| Layout/Lines take `num_gpus=0.001` | `pipeline.py` | Token slots — just enough to *land on the GPU node*, not to reserve real VRAM. |
| Serve replica count was **dropped to 2** | history: 6→4→2 | 6 GPU workers each holding ~4 GB TrOCR-in-RAM saturated host memory; the kernel OOM killer reaped `dashboard_agent` and **fate-killed the raylet**. Watch host-RAM headroom, not just VRAM, when raising replicas. |
| `transcribe_batch = 64` is **fixed** | `pipeline.py` (and `MAX_BATCH=64` in `transcribe_service.py`) | Gates how early ALTO lands in S3. The old `len(keys)/concurrency` heuristic delayed the first write ~85 min on a 7,348-page chunk; fixed-64 → first writes ~5 min, any chunk ≥256 pages still fans out fully. |
| `actor_locality_enabled=False` + `target_max_block_size=16 MiB` | `pipeline.py`, top of `htr_pipeline` | The streaming executor ranks operators by smallest out-queue and sticks to the warm actor — so only ~1 actor/replica per stage gets work. Disabling locality + smaller blocks widens the queues so the pool fans out. |
| Pools use `ActorPoolStrategy(size=N)`, **autoscaler OFF** | every `map_batches` in `htr_pipeline` | `concurrency=(N,N)` kept the autoscaler in play and biased work to the first warm actor (2/3 GPUs idle). `size=N` removes the autoscaler so blocks dispatch across the full pool. |
| `SHARDS = 3` intra-task fan-out | `TranscribeViaServe.__call__` (an inline `num_shards=3` — there is **no** `SHARDS` constant in `transcribe_service.py`) and `HTRFlowViaServeBytes.SHARDS` (the class `htrflow_pipeline` actually wires; `HTRFlowViaServe.SHARDS` is the path-based sibling nothing calls) | The executor only ever has ~1 `*ViaServe` task in flight. Each task splits its crops into 3 shards (round-robin, length-bucketed) and fires them to Serve **simultaneously** so all replicas run concurrently. Without it the Serve stage serializes exactly like the old actor pool did. |
| Retargeting GPUs = edit **all three** files | `pipeline.py` + `transcribe_service.py` + `htrflow_service.py` | GPU sizing is **split**: Serve fractions are env-overridable (`RASK_SERVE_GPU_FRAC`/`RASK_SERVE_REPLICAS`, defaulting to `0.49`/`2` in *both* service files — a 2-GPU packing); the pipeline's pool sizes are literals in `pipeline.py`. Changing target hardware means touching every one. |
| Model identities are declared **once**, in `htr/models.py` | `runners/htr/src/htr/models.py` → `REGION_MODEL` / `LINE_MODEL` / `TEXT_MODEL` (`ModelRef(step, repo, revision)`) | `pipeline.py` passes `REGION_MODEL.repo` / `LINE_MODEL.repo` as `fn_constructor_kwargs`, the actors default to the same refs, and `transcribe_service.DEFAULT_MODEL = TEXT_MODEL.repo`. The published ALTO **records** them, so a second copy of the constant is a provenance lie the moment the copies disagree. `tests/test_models.py::test_model_repos_appear_only_in_this_module` fails the suite if a `Riksarkivet/` literal reappears in any `src/**/*.py` outside `models.py` — never re-inline a repo string. |
| Every Hugging Face load pins `revision=` | `layout.py`, `lines.py`, `transcription.py`, `transcribe_service.py`, and the injected htrflow config | All of them pin `revision=MODEL_REVISION` (`RASK_HTR_MODEL_REVISION`, default `main`), read **once at import** so a run cannot straddle two revisions. `tests/test_models.py::test_every_hugging_face_load_pins_a_revision` is an AST guard over `hf_hub_download` / `list_repo_files` / `snapshot_download` / `from_pretrained` — a call site added without `revision=` reds the suite at the edit, not months later in an unreproducible transcription. |

## Gotchas

- **`import torch` at module scope breaks Serve.** `@serve.deployment` pickles the class + its module globals; torch's `CudnnModule` isn't picklable. **All torch/transformers imports go inside method bodies** (both Serve files do this; `TrOCRSinusoidalPositionalEmbedding` even needs a meta-tensor materialize workaround).
- **Idle GPUs ≠ undersized pool.** Almost always the streaming-executor queue/locality bias or a too-narrow PageLoader head starving Transcribe — check all four fan-out levers (locality, block size, fixed pool, shards) before adding replicas.
- **`transcribe_concurrency = 3` in `pipeline.py` is a vestigial local** — it only feeds `from_items(..., override_num_blocks=max(transcribe_concurrency, len(keys)))`. GPU parallelism is `RASK_SERVE_REPLICAS`.
- **Co-residence is the *Serve* default, sized for 2 GPUs.** `0.49 × 2 replicas` per app lets `/transcribe` and `/htrflow` share a 2-GPU pool (1.96 ≤ 2.0). A larger node can run more — but raise `RASK_SERVE_REPLICAS`/`RASK_SERVE_GPU_FRAC` deliberately and re-check both the fractional-sum ≤ physical-GPUs invariant and host-RAM headroom (the 6→4→2 OOM postmortem is in `references/gpu-packing-and-oom.md`).
- **`pipeline.py:42`'s docstring says `MAX_BATCH=256`; the live constant is `64`** (`transcribe_service.py:38`). Trust the Serve module — and fix the docstring when you next touch that file.

## When to load each reference

| If you need to… | Read |
|---|---|
| The full GPU-packing math, the 6→4→2 host-RAM OOM postmortem, the four fan-out levers, **and the symptom→cause cheat sheet** (raylet died / replicas stuck PENDING / one GPU busy / late ALTO / pickling error on deploy) | `references/gpu-packing-and-oom.md` |
| The exact actor/Serve topology and pool sizes, every `RASK_*` env knob, the `make serve-up*` commands, **and the ALTO `<Processing>` provenance contract** (`serialize_alto(..., models=PIPELINE_MODELS)`) | `references/topology-and-deploy.md` |

## Sibling runners

This skill covers `runners/htr` only. Eight other sealed runners exist — `asr`, `assist`, `diarize`, `dummy`, `insid3`, `kg`, `topics`, `voiceprint` — each with its own `pyproject.toml`, matched by **no** workspace glob (`members = ["packages/*", "services/*"]`) so their heavy pins never enter the fleet's resolution. Only `assist`, `dummy` and `htr` carry their own `uv.lock`. `runners/assist` is a substantial project with its own `.docker/assist-runner.dockerfile`; `runners/dummy` is the trivial-transform reference runner. For where they sit in the tree, see `rask-architecture`.

## Cross-skill

- **`python-infrastructure`** — generic retries/backoff/observability around the pipeline; this skill is the rask-specific Ray Data/Serve GPU choreography.
- **`writing-python`** — language idioms; this skill is the operational invariants.
- **`rask-architecture`** — the sealed-runner plane and why it sits outside every glob.
- **`rask-services-fleet`** — the `/api/ray` + `/api/serve` surface that fronts this cluster.
