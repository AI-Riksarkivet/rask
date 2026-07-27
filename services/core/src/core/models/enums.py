"""Viewer-defined StrEnums.

Ray's own enums (`JobStatus`, `JobType`, `DriverInfo`) are imported directly
from `ray.dashboard.modules.job.*` at the call site — they're not re-exported
here, since the source-of-truth import is more honest than a viewer-side alias.
"""

from enum import StrEnum


# ── batches.db domain ────────────────────────────────────────────────────


class HtrStatus(StrEnum):
    PENDING = "pending"
    CACHED = "cached"
    PARTIAL = "partial"
    DONE = "done"
    VERIFICATION_FAILED = "verification_failed"


class ManifestStatus(StrEnum):
    OK = "ok"
    HTTP_403 = "http_403"
    HTTP_400 = "http_400"
    ERROR = "error"
    PENDING = "pending"


class BrowseTier(StrEnum):
    LISTED = "listed"
    CACHED = "cached"
    TRANSCRIBED = "transcribed"


# ── Ray actor names ──────────────────────────────────────────────────────

# Pipeline identity + concurrency lanes now live in
# `core.models.pipelines` (the `Slot` StrEnum + `PIPELINE_SPECS` registry),
# which replaced the old `Pipeline` StrEnum that lived here.


class RayStage(StrEnum):
    """Ray actor names matching the pipeline stages in
    `runners/htr/src/runner/pipeline.py`. Used to read per-stage
    task counts from `/api/v0/tasks/summarize` — string-equality with Ray's
    task naming is the contract."""

    PAGE_LOADER = "PageLoaderActor"
    LAYOUT = "LayoutActor"
    LINE = "LineActor"
    TRANSCRIBE = "TranscribeViaServe"
    ALTO_EXPORT = "AltoExportActor"
    ALTO_WRITER = "AltoWriterActor"
    PREFETCH = "PrefetchActor"


class TaskState(StrEnum):
    """Ray Data task-scheduler state keys from `/api/v0/tasks/summarize`'s
    `state_counts`. Not a Ray public enum — these are external API keys.

    `PENDING` is the **prefix** for several substates (PENDING_NODE_ASSIGNMENT,
    PENDING_ARGS_AVAIL, …), used with `str.startswith()` rather than equality.
    """

    FINISHED = "FINISHED"
    RUNNING = "RUNNING"
    SCHEDULED = "SUBMITTED_TO_WORKER"
    FAILED = "FAILED"
    WAITING = "WAITING"
    PENDING = "PENDING"
