"""SQLModel trinity for the batches table.

Domain enums (HtrStatus, ManifestStatus, BrowseTier, Pipeline, …) live in
`models.enums` so every StrEnum has one import surface.

  - `BatchBase`   — shared field definitions (single source of truth).
  - `Batch`       — DB row (`table=True`); the only thing repositories touch.
  - `BatchPublic` — what's exposed via the API.

Schema mirrors what `scripts/build_batches_db.py` creates; viewer reads, the
script seeds. Once the heavy refactor merges, both come from one place.
"""

from sqlmodel import Field, SQLModel

from viewer.models.enums import HtrStatus, ManifestStatus


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
