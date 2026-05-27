"""SQLModel trinity for the batches table + domain enums.

  - `BatchBase`   — shared field definitions (single source of truth).
  - `Batch`       — DB row (`table=True`); the only thing repositories touch.
  - `BatchPublic` — what's exposed via the API.

Schema mirrors what `scripts/build_batches_db.py` creates; viewer reads, the
script seeds. Once the heavy refactor merges, both come from one place.
"""

from enum import StrEnum

from sqlmodel import Field, SQLModel


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


class Pipeline(StrEnum):
    """The two Ray Data pipelines viewer tracks. Submission IDs are namespaced
    with `<pipeline>-chunk-<id>-of-<total>` so the prefix identifies which
    pipeline a running job belongs to."""

    PREFETCH = "prefetch"
    HTR = "htr"

    @property
    def submission_id_prefix(self) -> str:
        return f"{self.value}-"


class BatchBase(SQLModel):
    batch_id: str
    arkiv_referenskod: str | None = None
    arkiv_titel: str | None = None
    volym: str | None = None
    rattighetsmarkning_volym: str | None = None
    rattighetsmarkning_batch: str | None = None
    startdatum: str | None = None
    slutdatum: str | None = None
    htrad_tidigare: str | None = None

    page_count: int | None = None
    iiif_endpoint: str | None = None
    manifest_status: ManifestStatus | None = None
    manifest_error: str | None = None
    fetched_at: str | None = None

    cached_pages: int = 0
    transcribed_pages: int = 0
    htr_status: HtrStatus = HtrStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    last_error: str | None = None
    last_synced_at: str | None = None

    chunk_id: int | None = None
    chunk_total: int | None = None

    current_rayjob_id: str | None = None
    current_rayjob_submitted_at: str | None = None


class Batch(BatchBase, table=True):
    __tablename__ = "batches"

    batch_id: str = Field(primary_key=True)


class BatchPublic(BatchBase):
    pass
