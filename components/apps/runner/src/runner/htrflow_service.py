"""HTRflow as a Ray Serve deployment — wraps the upstream pipeline as-is.

Approach (no upstream patches):
- One replica = one HTRflow Pipeline (`Segmentation -> Segmentation ->
  TextRecognition -> OrderLines`, configured in htrflow_pipeline.yaml).
- Each `Inference` step in HTRflow already runs a daemon `BatchedQueue`
  thread for free GPU/CPU batching, so concurrent Serve requests landing
  on the same replica are batched naturally — we don't need to add
  batching at the deployment layer.
- The stock `Export` step writes ALTO files to disk; we drop it from the
  YAML and call `get_serializer("alto").serialize(doc)` ourselves so the
  ALTO string comes back in-process.
- 3 replicas x 1 GPU each (htrflow auto-detects CUDA via the YAML).
  SHARDS in the pipeline-side wrappers below matches this replica count
  so a single Ray Data task can saturate all three replicas in parallel.

This module also exposes `HTRFlowViaServe` — an actor analogous to
`TranscribeViaServe` in `transcribe_service.py` — that the HTR pipeline
can use as a single replacement for the (Layout + Line + Transcribe +
AltoExport) chain.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
from ray import serve


logger = logging.getLogger(__name__)

# Replica/GPU sizing shares the same env knobs as transcribe_service so both
# HTR Serve apps co-reside on a 2-GPU pool: 2 apps x 2 replicas x 0.49 = 1.96
# GPU total. See transcribe_service.SERVE_REPLICAS for the budgeting rationale.
SERVE_REPLICAS = int(os.environ.get("RASK_SERVE_REPLICAS", "2"))
SERVE_GPU_FRAC = float(os.environ.get("RASK_SERVE_GPU_FRAC", "0.49"))

# Optional GPU-tier pin. When set to a custom Ray resource advertised by a
# worker group (e.g. "gpu_ada" on the dev-kuberay cluster), HTRFlow replicas
# are constrained to that hardware tier. Unset locally — a single-node
# serve-up advertises no such resource, so the default places replicas on any
# available GPU. The value is a placement tag, reserved as a tiny fraction;
# `num_gpus` above is the real GPU budget.
SERVE_GPU_RESOURCE = os.environ.get("RASK_SERVE_GPU_RESOURCE") or None

_HTR_ACTOR_OPTIONS: dict[str, Any] = {"num_cpus": 2, "num_gpus": SERVE_GPU_FRAC}
if SERVE_GPU_RESOURCE:
    _HTR_ACTOR_OPTIONS["resources"] = {SERVE_GPU_RESOURCE: 0.001}


PIPELINE_YAML = Path(__file__).parent / "htrflow_pipeline.yaml"
# Custom ALTO template dir. Same alto-4-4 template as upstream htrflow but with
# WC (per-line/word text confidence) and PC (page confidence) emitted — the
# stock template drops them, so the viewer showed conf 0.00. Named "alto-4-4"
# so htrflow still resolves the matching alto-4-4.xsd from its own schema dir.
ALTO_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _shard(items: list, num_shards: int) -> list[list]:
    """Round-robin partition `items` into up to `num_shards` non-empty shards.

    Returned shards may be fewer than `num_shards` if `len(items) <= num_shards`
    or if the round-robin leaves some buckets empty. Used by the pipeline-side
    Serve actors to fan one Ray Data task out across multiple Serve replicas.
    """
    if num_shards <= 1 or len(items) <= num_shards:
        return [items]
    out: list[list] = [[] for _ in range(num_shards)]
    for i, x in enumerate(items):
        out[i % num_shards].append(x)
    return [s for s in out if s]


@serve.deployment(
    name="HTRFlowService",
    num_replicas=SERVE_REPLICAS,
    ray_actor_options=_HTR_ACTOR_OPTIONS,
    max_ongoing_requests=4,
)
class HTRFlowDeployment:
    """Wraps an HTRflow Pipeline. One replica holds region YOLO + line YOLO + TrOCR."""

    def __init__(self) -> None:
        # Imports are deferred into __init__ so the deployment class itself
        # stays cheaply picklable — torch/cudnn modules misbehave when
        # imported at module scope and then pickled by Ray (we hit this
        # exact pattern with the TrOCR-only deployment).
        from htrflow.pipeline.pipeline import Pipeline
        from htrflow.serialization.serialization import get_serializer

        logger.info("HTRFlowDeployment: loading pipeline from %s", PIPELINE_YAML)
        self._pipeline = Pipeline.from_config(str(PIPELINE_YAML))
        self._serializer = get_serializer("alto", template_dir=str(ALTO_TEMPLATE_DIR), template_name="alto-4-4")
        logger.info("HTRFlowDeployment: ready (%d steps)", len(self._pipeline.steps))

    def transcribe(self, image_path: str) -> str:
        """Run the full HTR pipeline on one image at `image_path`, return ALTO XML.

        Stock `Document` re-opens the file on every `document.image` access
        (region pass, line pass, line crops); fine for one-off CLI use,
        wasteful in a Serve replica handling thousands of pages. Prefer
        `transcribe_bytes` for the pipeline path — it skips disk entirely.
        """
        from htrflow.document import Document

        doc = self._pipeline.run(Document(image_path))
        return self._serializer.serialize(doc) or ""

    def transcribe_bytes(self, data: bytes, name: str = "page") -> str:
        """In-memory variant — bytes -> PIL.Image -> Pipeline, no tempfile.

        Builds an `InMemoryDocument` (subclass of htrflow's `Document` that
        caches the decoded image) so the page is opened once and reused
        across the segmentation/recognition passes. This is the path used
        by `pipeline.py` once we wire `HTRFlowViaServe` in — pages come
        from S3 as bytes and never touch local disk.
        """
        doc = self._pipeline.run(_build_in_memory_document(data, name=name))
        return self._serializer.serialize(doc) or ""


def _build_in_memory_document(image_bytes: bytes, name: str = "page") -> object:
    """Factory for an htrflow.Document subclass that holds a cached PIL.Image.

    Defined as a factory so the htrflow imports stay lazy (the deployment
    class is built / pickled before workers actually load torch). The
    returned instance behaves like `Document(path)` from htrflow's POV —
    it has `image_name`, `image`, `polygon`, `regions`, etc. — but
    sources its image from RAM instead of re-opening a file.
    """
    from io import BytesIO

    from htrflow.document import Document, Region
    from htrflow.utils.geometry import Bbox
    from PIL import Image

    class InMemoryDocument(Document):
        def __init__(self, data: bytes, image_name: str) -> None:
            self._cached = Image.open(BytesIO(data)).convert("RGB")
            self.image_name = image_name
            self._image_path = None
            polygon = Bbox(0, 0, self._cached.width, self._cached.height).polygon()
            Region.__init__(self, polygon)

        @property
        def image(self) -> object:
            return self._cached

    return InMemoryDocument(image_bytes, image_name=name)


def _init_otel() -> None:
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return  # already initialised
    provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "ray-htrflow")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


_init_otel()

htrflow_app = HTRFlowDeployment.bind()


# ---------------------------------------------------------------------------
# Pipeline-side wrapper (mirrors TranscribeViaServe).
# ---------------------------------------------------------------------------


class HTRFlowViaServe:
    """Ray Data actor that calls the HTRFlowDeployment for each input image.

    Drop-in for the (Layout + Line + Transcribe + AltoExport) actor chain
    in pipeline.py — input batches contain image paths, output batches
    contain ALTO XML strings keyed by the same image_id. Uses the same
    intra-task fan-out trick as TranscribeViaServe so a single Ray Data
    task can saturate multiple Serve replicas in parallel.
    """

    SHARDS = 3  # match the eventual GPU replica count once we move off CPU

    def __init__(self) -> None:
        from ray import serve as _serve

        # `get_app_handle` raises if the app isn't deployed; the pipeline
        # must run `make serve-up` (or scripts/deploy_serve.py) first.
        self._handle = _serve.get_app_handle("htrflow")

    def __call__(self, batch: dict) -> dict:
        # Expect `image_path` per row; emit `alto` per row.
        paths = list(batch["image_path"])
        shards = _shard(paths, self.SHARDS)
        # Fan out: each shard fires one remote call sequentially through
        # the items in that shard. With multiple replicas Serve's router
        # spreads the shards, giving us per-task parallelism beyond what
        # Ray Data's streaming executor would schedule on its own.
        responses = [[self._handle.transcribe.remote(p) for p in shard] for shard in shards if shard]
        results: dict[str, str] = {}
        flat_paths = [p for shard in shards for p in shard]
        flat_refs = [r for shard_refs in responses for r in shard_refs]
        for path, ref in zip(flat_paths, flat_refs, strict=True):
            try:
                results[path] = ref.result()
            except Exception as exc:
                logger.warning("HTRFlowViaServe: %s failed: %s", path, exc)
                results[path] = ""
        return {
            "image_path": paths,
            "alto": [results.get(p, "") for p in paths],
        }


class HTRFlowViaServeBytes:
    """Ray Data actor that calls HTRFlowDeployment with in-memory image bytes.

    Drop-in for the (Layout + Line + Transcribe + AltoExport) actor chain
    used by `htr_pipeline`. Consumes `{key, image_bytes}` from
    `PageLoaderActor` and emits `{output_key, alto_xml}` — the same shape
    `AltoExportActor` produces today, so `AltoWriterActor` is unchanged.

    Mirrors `HTRFlowViaServe` but uses `transcribe_bytes` (no local disk)
    and adopts the writer's key conventions inline so we can drop the
    Layout/Line/Transcribe/AltoExport quartet.

    Constructor accepts an optional `handle` for testing; when `None`
    (Ray Data's call path) it resolves the running Serve app.
    """

    SHARDS = 3  # match the eventual GPU replica count once we move off CPU

    def __init__(self, handle: Any | None = None) -> None:  # noqa: ANN401
        if handle is None:
            # Imports deferred so the class is cheaply importable without
            # a running Ray cluster (mirrors HTRFlowViaServe).
            from ray import serve as _serve

            handle = _serve.get_app_handle("htrflow")
        self._handle = handle

    def __call__(self, batch: dict) -> dict:
        keys = list(batch["key"])
        bytes_list = list(batch["image_bytes"])
        pairs = list(zip(keys, bytes_list, strict=True))
        shards = _shard(pairs, self.SHARDS)
        # Fan out per shard so a single Ray Data task can saturate multiple
        # Serve replicas in parallel (same trick as TranscribeViaServe).
        responses = [[self._handle.transcribe_bytes.remote(data, name=key) for key, data in shard] for shard in shards if shard]
        results: dict[str, str] = {}
        flat_pairs = [p for shard in shards for p in shard]
        flat_refs = [r for shard_refs in responses for r in shard_refs]
        for (key, _data), ref in zip(flat_pairs, flat_refs, strict=True):
            try:
                results[key] = ref.result()
            except Exception as exc:
                logger.warning("HTRFlowViaServeBytes: %s failed, will write empty .xml: %s", key, exc)
                results[key] = ""
        out_keys = [k.rsplit(".", 1)[0] + ".xml" for k in keys]
        out_xml = [results.get(k, "").encode("utf-8") for k in keys]
        return {
            "output_key": np.array(out_keys, dtype=object),
            "alto_xml": np.array(out_xml, dtype=object),
        }
