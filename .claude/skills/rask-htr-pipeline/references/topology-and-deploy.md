# Topology, env knobs, and deploy

The concrete shape of the pipeline + the two Serve apps, with exact symbols,
ports, and commands. Source: `components/apps/runner/src/runner/` and
`components/scripts/deploy_serve.py`.

## The two pipeline shapes

`PIPELINES` in `pipeline.py` maps four names → builders:

| Name | Builder | Stages |
|---|---|---|
| `htr` | `htr_pipeline` | `PageLoader → Layout → Lines → TranscribeViaServe → AltoExport → AltoWriter` |
| `htrflow` | `htrflow_pipeline` | `PageLoader → HTRFlowViaServeBytes → AltoWriter` |
| `fake` | `fake_pipeline` | `PageLoader → FakeAlto → AltoWriter` (no GPU, smoke-tests source/sink wiring) |
| `prefetch` | `prefetch_pipeline` | `Prefetch` only — warms the S3 cache from IIIF, no transcription |

### Actor-per-stage (`htr_pipeline`) — pool sizes

All literals, autoscaler off (`ActorPoolStrategy(size=N)`):

| Stage | Actor | `size` | `batch_size` | GPU |
|---|---|---|---|---|
| Page load | `PageLoaderActor` | 6 | 8 | — (S3/IIIF-bound) |
| Region detect | `LayoutActor` (`yolov9-regions-1`) | 2 | 8 | `num_gpus=0.001` |
| Line detect | `LineActor` (`yolov9-lines-within-regions-1`) | 2 | 8 | `num_gpus=0.001` |
| Transcribe | `TranscribeViaServe` (CPU; blocks on Serve handle) | 8 (`transcribe_concurrency_serve`) | 64 (`transcribe_batch`) | — (GPU in Serve) |
| ALTO build | `AltoExportActor` (`emit_words=True`) | 2 | 32 | — |
| ALTO write | `AltoWriterActor` (`sink=...`) | 2 | 32 | — |

PageLoader is sized 6 because at `concurrency=2` it capped the pipeline at
~60 pages/min — slow enough that Transcribe never fanned out to GPUs 1/2. A wide
head lets the Transcribe buffer fill so the Serve fan-out kicks in.

### `/htrflow` collapse (`htrflow_pipeline`) — pool sizes

| Stage | Actor | `size` | `batch_size` |
|---|---|---|---|
| Page load | `PageLoaderActor` | 6 | 8 |
| Full HTR | `HTRFlowViaServeBytes` (CPU; blocks on `htrflow` handle) | 8 | 16 |
| ALTO write | `AltoWriterActor` | 2 | 32 |

One `htrflow` Serve replica owns the whole `Segmentation → Segmentation →
TextRecognition → OrderLines` chain (configured in `htrflow_pipeline.yaml`) and
serializes ALTO in-process via `get_serializer("alto")`. Pick this shape when
per-step actor fan-out isn't worth it for the batch.

## The two Serve apps

Deployed by `components/scripts/deploy_serve.py` (`APPS` dict):

| App name | Route prefix | Deployment class | Build |
|---|---|---|---|
| `transcribe` | `/transcribe` | `TranscribeService` (TrOCR-only) | `runner.transcribe_service.build_app()` |
| `htrflow` | `/htrflow` | `HTRFlowDeployment` (full HTRflow pipeline) | `runner.htrflow_service.htrflow_app` |

Both are reached from the pipeline by `serve.get_app_handle("transcribe" | "htrflow")`
(raises if not deployed — run `make serve-up` first). `TranscribeService`'s
default model is `Riksarkivet/trocr-base-handwritten-hist-swe-2` (`dtype="bf16"`,
`attn_implementation={"encoder": "sdpa", "decoder": "eager"}`); the
`TranscribeViaServe._handle.transcribe.remote(...)` entrypoint returns
`list[(text, confidence)]` where confidence is `exp(mean log-prob)` over emitted
tokens (via `compute_transition_scores`, beam-index-aware).

**Serve persists across job submissions.** Weights stay warm; each `runner`
invocation skips the ~30 s cold-start. That's the whole reason the GPU stage was
moved out of Ray Data and into Serve — Ray Data's executor would have rotated
the actors one at a time (see `references/gpu-packing-and-oom.md` § fan-out).

## Env knobs

| Var | Default | Effect |
|---|---|---|
| `RASK_SERVE_REPLICAS` | `2` | `num_replicas` for **both** Serve apps |
| `RASK_SERVE_GPU_FRAC` | `0.49` | `num_gpus` per replica for **both** Serve apps |
| `RASK_SERVE_GPU_RESOURCE` | unset | Optional custom Ray resource tag to pin `htrflow` replicas to a GPU tier (e.g. `gpu_ada` on dev-kuberay); reserved as a `0.001` placement tag, not the real GPU budget |
| `RAY_ADDRESS` | `auto` | Cluster to attach to (`deploy_serve._connect`) |

`deploy_serve._connect` pins `runtime_env={"py_executable": sys.executable}` so
Ray's runtime-env plugin doesn't rebuild a worker venv via `uv run` and strip
`ray` out (root `pyproject` has `dependencies = []`).

## Commands

```bash
make serve-up        # deploy_serve.py up           → app 'transcribe' at /transcribe
make serve-up-both   # up --app transcribe + up --app htrflow (2-GPU co-residence)
make serve-status    # deploy_serve.py status (all apps + replica states)
make serve-down      # deploy_serve.py down
```

`serve-up-both` runs each `up` with `RASK_SERVE_REPLICAS`/`RASK_SERVE_GPU_FRAC`
and `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync` (same runtime-env
avoidance as `_connect`). Defaults in the Makefile: `RASK_SERVE_REPLICAS ?= 2`,
`RASK_SERVE_GPU_FRAC ?= 0.49`.

Direct invocation (repo root, local Ray up):

```bash
uv run python components/scripts/deploy_serve.py up                # default: transcribe
uv run python components/scripts/deploy_serve.py up --app htrflow
uv run python components/scripts/deploy_serve.py down --app htrflow
uv run python components/scripts/deploy_serve.py status
```

Submit work through the `runner` CLI (`components/apps/runner`), which builds one
of the `PIPELINES` and blocks on `.materialize()`. For remote KubeRay pass
`--address ray://...:10001`.
