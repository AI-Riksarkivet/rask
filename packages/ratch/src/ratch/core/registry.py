"""Stage registry — what each pipeline stage reads, writes, gates on, and needs.

A stage is a *declarative* record and the media-agnosticism boundary of the
pipeline. The core owns WHAT a stage consumes and produces (tables, columns,
MIME gate, client capability, actor sizing); the composition root
(:mod:`ratch.features.columns` / the ``ratch pipeline`` CLI) owns HOW, by
injecting the concrete compute factory when it hands the stage to a driver in
:mod:`ratch.core.driver`. Nothing here imports modality or client code.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class StageShape(StrEnum):
    """How a stage's output lands in Lance (drives which driver runs it)."""

    SCAN_COLUMN = "scan_column"  # derive a column from scanned columns (NULL-fill resumable)
    BLOB_COLUMN = "blob_column"  # derive a column from a blob column's payloads (two-pass by _rowid)
    APPEND_ROWS = "append_rows"  # produce new rows for a (possibly different) table


class MediaGate(BaseModel):
    """MIME predicate deciding which *documents* a stage touches.

    Gated stages skip non-matching docs (counted and logged by the driver),
    they never crash on them — an image corpus simply yields zero work for a
    diarization stage.
    """

    model_config = {"frozen": True}

    mime_prefixes: tuple[str, ...]

    def admits(self, mime: str | None) -> bool:
        return mime is not None and mime.startswith(self.mime_prefixes)


AUDIO_VIDEO = MediaGate(mime_prefixes=("audio/", "video/"))
VIDEO_ONLY = MediaGate(mime_prefixes=("video/",))
IMAGE_ONLY = MediaGate(mime_prefixes=("image/",))


class ActorConfig(BaseModel):
    """Ray actor-pool sizing for a stage's ``map_batches`` fan-out."""

    model_config = {"frozen": True}

    min_actors: int = 1
    max_actors: int = 4
    num_cpus: float = 1.0
    num_gpus: float = 0.0
    batch_rows: int = Field(default=256, ge=1)


class Stage(BaseModel):
    """One declarative pipeline stage."""

    model_config = {"frozen": True}

    name: str
    shape: StageShape
    table: str
    read_columns: tuple[str, ...] = ()
    key_columns: tuple[str, ...] = ()
    blob_column: str | None = None
    output_columns: tuple[str, ...] = ()
    output_table: str | None = None
    media_gate: MediaGate | None = None
    client: str | None = None  # capability name the composition root must bind ("embed", …)
    # The runners/<name>/ backing this stage's model (None = pure compute). THE
    # legible map: a stage with runner= runs that runner's actor via map_batches;
    # its deps come from the runner's env (per-stage runtime_env on a cluster).
    runner: str | None = None
    actor: ActorConfig = ActorConfig()

    @model_validator(mode="after")
    def _shape_requirements(self) -> Stage:
        if self.shape is StageShape.BLOB_COLUMN and not self.blob_column:
            raise ValueError(f"stage {self.name}: blob_column is required for BLOB_COLUMN stages")
        if self.shape is StageShape.APPEND_ROWS and not self.output_table:
            raise ValueError(f"stage {self.name}: output_table is required for APPEND_ROWS stages")
        if self.shape is not StageShape.APPEND_ROWS and not self.output_columns:
            raise ValueError(f"stage {self.name}: column stages must declare output_columns")
        return self
