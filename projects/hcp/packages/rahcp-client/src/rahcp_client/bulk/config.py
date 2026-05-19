"""Configuration and stats models for bulk transfers."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from rahcp_tracker import TrackerProtocol

from rahcp_client.bulk.protocol import BulkClient

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB
DEFAULT_STREAM_THRESHOLD = (
    100 * 1024 * 1024
)  # 100 MB — files below this are read in one shot


class TransferStats(BaseModel):
    """Snapshot of a running bulk transfer."""

    ok: int = 0
    skipped: int = 0
    errors: int = 0
    total_bytes: int = 0
    elapsed: float = 0.0

    @property
    def done(self) -> int:
        """Total files processed (ok + skipped + errors)."""
        return self.ok + self.skipped + self.errors

    @property
    def files_per_sec(self) -> float:
        """Throughput in files per second."""
        return self.ok / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def mb_per_sec(self) -> float:
        """Throughput in megabytes per second."""
        return (
            (self.total_bytes / 1024 / 1024) / self.elapsed if self.elapsed > 0 else 0.0
        )


class BulkUploadConfig(BaseModel):
    """Configuration for a bulk upload job."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: SkipValidation[BulkClient]
    bucket: str
    source_dir: Path
    tracker: TrackerProtocol
    prefix: str = ""
    workers: int = 10
    queue_depth: int = 8
    skip_existing: bool = True
    retry_errors: bool = False
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    validate_file: Callable[[Path], None] | None = None
    verify_upload: bool = False
    presign_batch_size: int = 200
    chunk_size: int = DEFAULT_CHUNK_SIZE
    on_progress: Callable[[TransferStats], None] | None = None
    on_error: Callable[[str, Exception], None] | None = None
    progress_interval: float = 5.0


class BulkDownloadConfig(BaseModel):
    """Configuration for a bulk download job."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: SkipValidation[BulkClient]
    bucket: str
    dest_dir: Path
    tracker: TrackerProtocol
    prefix: str = ""
    workers: int = 10
    queue_depth: int = 8
    retry_errors: bool = False
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    validate_file: Callable[[Path], None] | None = None
    verify_download: bool = False
    presign_batch_size: int = 200
    chunk_size: int = DEFAULT_CHUNK_SIZE
    stream_threshold: int = DEFAULT_STREAM_THRESHOLD
    on_progress: Callable[[TransferStats], None] | None = None
    on_error: Callable[[str, Exception], None] | None = None
    progress_interval: float = 5.0
