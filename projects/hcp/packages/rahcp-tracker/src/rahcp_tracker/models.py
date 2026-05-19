"""Shared data models for transfer tracking — used by all backends."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


class TransferStatus(str, Enum):
    pending = "pending"
    done = "done"
    error = "error"


class Transfer(SQLModel, table=True):
    """One tracked file in a bulk transfer.

    This table definition is shared across all SQLModel-compatible backends
    (SQLite, Postgres, etc.). The backend chooses the engine/connection string.
    """

    key: str = Field(primary_key=True)
    size: int
    status: TransferStatus = TransferStatus.pending
    error: str | None = None
    etag: str | None = None
    validated: bool = False
    verified: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
