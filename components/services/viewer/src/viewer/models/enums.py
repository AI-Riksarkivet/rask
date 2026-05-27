"""All viewer-defined StrEnums in one place.

Re-exports `JobStatus` / `JobType` / `DriverInfo` from Ray for callers that
want a single import surface. Viewer's own enums live here directly.
"""

from enum import StrEnum

from ray.dashboard.modules.job.common import JobStatus
from ray.dashboard.modules.job.pydantic_models import DriverInfo, JobType


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


# ── HTR pipeline + Ray actor names ───────────────────────────────────────


class Pipeline(StrEnum):
    """The two Ray Data pipelines viewer tracks. Submission IDs are namespaced
    `<pipeline>-chunk-<id>-of-<total>` — the prefix identifies which pipeline
    a running job belongs to."""

    PREFETCH = "prefetch"
    HTR = "htr"

    @property
    def submission_id_prefix(self) -> str:
        return f"{self.value}-"


class RayStage(StrEnum):
    """Ray actor names matching the pipeline stages in
    `components/apps/runner/src/runner/pipeline.py`. Used to read per-stage
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
