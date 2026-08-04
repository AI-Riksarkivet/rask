"""Build the HTR Ray Data pipeline from sources/sinks."""

from collections.abc import Callable
from pathlib import Path

import ray.data

from htr.actors.alto_export import AltoExportActor
from htr.actors.fake import FakeAltoActor
from htr.actors.io import AltoWriterActor, PageLoaderActor, PrefetchActor
from htr.actors.layout import LayoutActor
from htr.actors.lines import LineActor
from htr.models import LINE_MODEL, REGION_MODEL
from runner.htrflow_service import HTRFlowViaServeBytes
from runner.transcribe_service import TranscribeViaServe


def htr_pipeline(
    keys: list[str],
    source: object,
    sink: object,
    *,
    transcribe_profile_dir: Path | None = None,
) -> ray.data.Dataset:
    """Real HTR pipeline, sized for a 3-GPU node.

    GPU-fraction packing: each physical GPU has 1.0 budget. 4 Transcribe actors
    each take 0.499 of a GPU (Ray packs 2 per physical card; one card stays
    free for Layout/Lines token fractions of 0.001). Total claim:
    0.002 + 4 * 0.499 = 1.998 <= 3.0. Dropped from 6 actors after chunk 021's
    OOM cascade — 6 * ~4GB TrOCR-in-RAM saturated host memory and the kernel
    OOM killer reaped dashboard_agent, fate-killing the raylet.

    `transcribe_batch=64` is fixed (not derived from len(keys)). This gates
    how early ALTO files start landing in S3: each Transcribe block must fully
    transcribe before AltoExport/AltoWriter see it. With the previous
    `len(keys) / concurrency` heuristic, a 7,348-page chunk meant the first
    ALTO write didn't happen until ~85 min in. Fixed batch=64 → first writes
    within ~5 min regardless of chunk size, and Ray Data still has enough
    blocks to keep all 4 actors busy (any chunk >= 256 pages fans out fully).

    Per-actor TrOCR throughput is tuned in `TranscribeActor` itself:
    MAX_BATCH=256 lines/chunk and PREPROCESS_WORKERS=4 keep the GPU fed.

    Tune for larger clusters by raising Transcribe concurrency (keep the
    fractional sum <= total GPUs and watch host RAM headroom).
    """
    transcribe_concurrency = 3
    transcribe_batch = 64
    # Ray Data's streaming executor (`select_operator_to_run` in
    # streaming_executor_state.py) ranks operators by smallest out-queue and
    # schedules whichever is smallest first. With a tight 6-stage pipeline
    # this keeps every queue at ~1 block, so only ~1 actor per stage gets work
    # at any moment. We try to widen the queues:
    #   - actor_locality_enabled=False: stop biasing dispatch toward the
    #     actor that produced the most recent block (sticky-warm pattern).
    #   - target_max_block_size smaller: more, smaller blocks → more bundles
    #     in flight at the slow Transcribe stage → select_actors actually
    #     fans out across the 3-actor pool instead of always picking the
    #     first warm one.
    ctx = ray.data.DataContext.get_current()
    ctx.execution_options.actor_locality_enabled = False
    ctx.target_max_block_size = 16 * 1024 * 1024  # 16 MiB (default 128 MiB)
    ds = ray.data.from_items([{"key": k} for k in keys], override_num_blocks=max(transcribe_concurrency, len(keys)))
    # PageLoader is CPU/network-bound (S3 GET on cache hit, IIIF on miss). At
    # concurrency=2 it caps the whole pipeline at ~60 pages/min, which is
    # slow enough that Ray Data never fans the TranscribeActor pool out to
    # GPUs 1/2 — one actor alone keeps up. Widening the head lets Transcribe
    # buffer fill, fanning out across the available GPUs.
    #
    # Pools use `compute=ActorPoolStrategy(size=N)` (fixed, autoscaler off)
    # instead of the deprecated `concurrency=` argument. Empirically with
    # `concurrency=(N, N)` Ray Data still kept the autoscaler in play and
    # heavily biased work to whichever actor warmed first — work flowed
    # through one actor per stage and 2/3 GPUs sat idle. `size=N` removes
    # the autoscaler entirely so blocks are dispatched across the full pool.
    ds = ds.map_batches(
        PageLoaderActor,
        fn_constructor_kwargs={"source": source},
        batch_size=8,
        compute=ray.data.ActorPoolStrategy(size=6),
    )
    ds = ds.map_batches(
        LayoutActor,
        fn_constructor_kwargs={"model": REGION_MODEL.repo},
        num_gpus=0.001,
        batch_size=8,
        compute=ray.data.ActorPoolStrategy(size=2),
    )
    ds = ds.map_batches(
        LineActor,
        fn_constructor_kwargs={"model": LINE_MODEL.repo},
        num_gpus=0.001,
        batch_size=8,
        compute=ray.data.ActorPoolStrategy(size=2),
    )
    # GPU work (TrOCR encode + autoregressive decode) is handled by a Ray Serve
    # deployment (`make serve-up` to deploy). This map_batches step is CPU-only
    # — it decodes JPEGs, crops lines, calls the Serve handle, and reassembles.
    # By taking the GPU work out of Ray Data's streaming executor, the 3 Serve
    # replicas can run concurrently (Ray Data's executor would have rotated
    # them one at a time).
    #
    # `transcribe_profile_dir` is silently ignored in the Serve path for now;
    # profiling moves to the Serve replicas (re-add if needed).
    _ = transcribe_profile_dir
    transcribe_concurrency_serve = 8  # CPU map_batches workers; each blocks on Serve handle
    ds = ds.map_batches(
        TranscribeViaServe,
        batch_size=transcribe_batch,
        compute=ray.data.ActorPoolStrategy(size=transcribe_concurrency_serve),
    )
    ds = ds.map_batches(
        AltoExportActor,
        fn_constructor_kwargs={"emit_words": True},
        batch_size=32,
        compute=ray.data.ActorPoolStrategy(size=2),
    )
    ds = ds.map_batches(
        AltoWriterActor,
        fn_constructor_kwargs={"sink": sink},
        batch_size=32,
        compute=ray.data.ActorPoolStrategy(size=2),
    )
    return ds


def fake_pipeline(
    keys: list[str],
    source: object,
    sink: object,
    *,
    transcribe_profile_dir: Path | None = None,
) -> ray.data.Dataset:
    """No-GPU pipeline for smoke-testing source/sink wiring."""
    ds = ray.data.from_items([{"key": k} for k in keys])
    ds = ds.map_batches(PageLoaderActor, fn_constructor_kwargs={"source": source}, batch_size=8, concurrency=2)
    ds = ds.map_batches(FakeAltoActor, batch_size=8, concurrency=2)
    ds = ds.map_batches(AltoWriterActor, fn_constructor_kwargs={"sink": sink}, batch_size=8, concurrency=2)
    return ds


def prefetch_pipeline(
    keys: list[str],
    source: object,
    sink: object,
    *,
    transcribe_profile_dir: Path | None = None,
) -> ray.data.Dataset:
    """Cache-warm pipeline: pull every IIIF image into the S3 cache, do nothing else.

    Runs the work that the ``htr`` pipeline does serially on its first
    PageLoader stage, but as a standalone job that can run far ahead of
    transcription (and at much higher concurrency since there's no GPU
    contention). Pair with `IIIFCachedSource` — the side effect of
    ``source.read(key)`` is the cache write-through.

    Concurrency is sized for the IIIF server's connection limits, not the
    GPU box. Empirically the server resets connections somewhere above
    ~32 concurrent fetches — 8 actors per job and ~4 simultaneous prefetch
    jobs sits comfortably below that.
    """
    ds = ray.data.from_items([{"key": k} for k in keys])
    ds = ds.map_batches(
        PrefetchActor,
        fn_constructor_kwargs={"source": source},
        batch_size=8,
        compute=ray.data.ActorPoolStrategy(size=8),
    )
    return ds


def htrflow_pipeline(
    keys: list[str],
    source: object,
    sink: object,
    *,
    transcribe_profile_dir: Path | None = None,
) -> ray.data.Dataset:
    """HTR pipeline that delegates the full image→ALTO transformation to a
    single HTRflow Serve deployment.

    Three stages: PageLoader → HTRFlowViaServeBytes → AltoWriter. Bypasses
    the per-step Layout/Line/Transcribe/AltoExport actor chain — the HTRflow
    Pipeline inside the Serve replica owns those four passes (region YOLO
    → line YOLO → TrOCR → ALTO serializer).

    Requires the `htrflow` Serve app to be deployed first
    (`make serve-up --app htrflow`). `transcribe_profile_dir` is not
    supported in this variant — profiling lives in the Serve replica.

    Pool sizes are starting points for the smoke test:
      - PageLoader=6: same as `htr_pipeline`, IIIF/S3-bound.
      - HTRFlowViaServeBytes=8: CPU map_batches workers that block on the
        Serve handle. Matches `transcribe_concurrency_serve` in
        `htr_pipeline`. With SHARDS=3 inside the actor, each task can drive
        up to 3 Serve replicas concurrently.
      - AltoWriter=2: S3-bound, low concurrency is plenty.
    """
    _ = transcribe_profile_dir  # explicitly unused; see docstring
    ds = ray.data.from_items(
        [{"key": k} for k in keys],
        override_num_blocks=max(8, len(keys)),
    )
    ds = ds.map_batches(
        PageLoaderActor,
        fn_constructor_kwargs={"source": source},
        batch_size=8,
        compute=ray.data.ActorPoolStrategy(size=6),
    )
    ds = ds.map_batches(
        HTRFlowViaServeBytes,
        batch_size=16,
        compute=ray.data.ActorPoolStrategy(size=8),
    )
    ds = ds.map_batches(
        AltoWriterActor,
        fn_constructor_kwargs={"sink": sink},
        batch_size=32,
        compute=ray.data.ActorPoolStrategy(size=2),
    )
    return ds


PipelineFn = Callable[..., ray.data.Dataset]
PIPELINES: dict[str, PipelineFn] = {
    "htr": htr_pipeline,
    "htrflow": htrflow_pipeline,
    "fake": fake_pipeline,
    "prefetch": prefetch_pipeline,
}
