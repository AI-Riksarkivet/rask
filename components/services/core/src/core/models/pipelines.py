"""Pipeline registry — the single source of truth for pipeline identity.

One `PipelineSpec` per runner pipeline collapses what used to be ~6 hardcoded
sites across submission / derive / loop / enums: the concurrency lane (`slot`),
the per-stage Ray actor names (`stages`), whether the pipeline tags a row's
`current_rayjob_id` (`tracks_rayjob_id`), and any author-curated extra CLI flags
(`extra_args`).

The registry keys are byte-identical to the runner's `PIPELINES` dict
(`components/cli/runner/src/runner/pipeline.py`) — `name` is simultaneously the
registry key, the `--pipeline` value passed to the runner, and the submission_id
prefix. Keeping the two in sync ends the silent divergence where the core's
`Pipeline` StrEnum only knew about a subset of the runner's pipelines.

This is a dict + frozen model, not a plugin framework. Specs are authored here;
nothing is discovered or registered at runtime.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.models.enums import RayStage


class Slot(StrEnum):
    """Orchestrator concurrency lane.

    A slot caps how many jobs of a shape may run at once. `htr`, `htrflow`,
    `fake`, and any future HTR-shaped runner share the single `HTR` lane so they
    never contend for the same GPUs; `prefetch` is CPU/network-bound and runs in
    its own lane. Replaces the old `Pipeline` StrEnum's dual role as both
    pipeline identity and concurrency lane.
    """

    PREFETCH = "prefetch"
    HTR = "htr"

    @property
    def submission_id_prefix(self) -> str:
        return f"{self.value}-"


class PipelineSpec(BaseModel):
    """Immutable description of one runner pipeline.

    `name` is the registry key, the runner `--pipeline` value, and the
    submission_id prefix — all the same string by construction.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    slot: Slot
    stages: tuple[RayStage, ...]
    tracks_rayjob_id: bool = True
    extra_args: tuple[tuple[str, str | int], ...] = ()
    # "runner" (default) → `uv run … runner --pipeline <name>`. "http" → a
    # standalone python job (htr_chunk_job.py) that POSTs to the /htr endpoint;
    # http specs are viewer-only and have no matching runner --pipeline.
    entrypoint_kind: Literal["runner", "http"] = "runner"
    # Extra pip packages for the job's runtime_env (http specs need boto3).
    pip: tuple[str, ...] = ()


_HTR_STAGES: tuple[RayStage, ...] = (
    RayStage.PAGE_LOADER,
    RayStage.LAYOUT,
    RayStage.LINE,
    RayStage.TRANSCRIBE,
    RayStage.ALTO_EXPORT,
    RayStage.ALTO_WRITER,
)


PIPELINE_SPECS: dict[str, PipelineSpec] = {
    "prefetch": PipelineSpec(
        name="prefetch",
        label="Prefetch (IIIF → S3 cache)",
        slot=Slot.PREFETCH,
        stages=(RayStage.PREFETCH,),
        # Tag current_rayjob_id so prefetch jobs can be stopped via stop_chunk.
        # Previously prefetch was excluded from the HTR-only tag set, so
        # stop_chunk raised "no running job" for prefetch (the prefetch-stop bug).
        tracks_rayjob_id=True,
    ),
    "htr": PipelineSpec(
        name="htr",
        label="HTR (actor-per-stage)",
        slot=Slot.HTR,
        stages=_HTR_STAGES,
        tracks_rayjob_id=True,
    ),
    "htrflow": PipelineSpec(
        name="htrflow",
        label="HTRflow (single Serve deployment)",
        slot=Slot.HTR,
        # The runner's htrflow_pipeline collapses Layout/Line/Transcribe/AltoExport
        # into one HTRFlowViaServeBytes MapBatches step, which carries no
        # actor-per-stage telemetry under the names derive.py queries. Empty stages
        # means the UI shows no per-stage bars rather than WRONG ones (no longer
        # force-classified against PageLoaderActor/.../AltoExportActor).
        stages=(),
        tracks_rayjob_id=True,
    ),
    "fake": PipelineSpec(
        name="fake",
        label="Fake (no-GPU smoke test)",
        slot=Slot.HTR,
        # Smoke pipeline shares the HTR lane and writes ALTO, so tagging the
        # rayjob id keeps stop/telemetry working the same way as a real HTR job.
        stages=(),
        tracks_rayjob_id=True,
    ),
    "htr_http": PipelineSpec(
        name="htr_http",
        label="HTR (HTTP → /htr endpoint)",
        slot=Slot.HTR,
        # No Ray actors → no per-stage telemetry; progress comes from S3 reconcile.
        stages=(),
        entrypoint_kind="http",
        pip=("boto3",),
        tracks_rayjob_id=True,
    ),
}

DEFAULT_PIPELINE = "htr"


def spec_for_submission_id(submission_id: str) -> PipelineSpec | None:
    """Resolve the spec from a submission_id's ``<name>-`` prefix, or None.

    Submission IDs are ``<name>-chunk-NNN-of-MMM-<ts>`` (see
    `core.services.submission.chunk_name`), so the registry name is the
    longest key the id starts with followed by ``-``. Longest-match guards
    against a future short name being a prefix of a longer one. Returns None
    for ids that match no registered pipeline (e.g. jobs submitted out-of-band).
    """
    candidates = [spec for name, spec in PIPELINE_SPECS.items() if submission_id.startswith(f"{name}-")]
    if not candidates:
        return None
    return max(candidates, key=lambda s: len(s.name))


def _validate_registry() -> None:
    """Fail fast at import: names non-empty, unique, and match their registry key."""
    seen: set[str] = set()
    for key, spec in PIPELINE_SPECS.items():
        if not spec.name:
            raise ValueError(f"pipeline spec under key {key!r} has an empty name")
        if spec.name != key:
            raise ValueError(f"pipeline spec name {spec.name!r} does not match its registry key {key!r}")
        if spec.name in seen:
            raise ValueError(f"duplicate pipeline name {spec.name!r}")
        seen.add(spec.name)
    if DEFAULT_PIPELINE not in PIPELINE_SPECS:
        raise ValueError(f"DEFAULT_PIPELINE {DEFAULT_PIPELINE!r} is not a registered pipeline")


_validate_registry()
