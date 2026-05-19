"""Pydantic schemas for Lance dataset operations."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TableInfo(BaseModel):
    """Metadata about a Lance table."""

    name: str
    num_rows: int = 0
    schema_fields: list[FieldInfo] = Field(default_factory=list)


class FieldInfo(BaseModel):
    """A single field in a Lance table schema."""

    name: str
    dtype: str
    nullable: bool = True


# Rebuild TableInfo now that FieldInfo is defined
TableInfo.model_rebuild()


class IngestResult(BaseModel):
    """Result of a batch ingest operation."""

    table: str
    rows_added: int
    total_rows: int


class ScanParams(BaseModel):
    """Parameters for scanning a Lance table."""

    columns: list[str] | None = None
    filter: str | None = None
    limit: int | None = None
    offset: int | None = None


class VectorSearchParams(BaseModel):
    """Parameters for vector similarity search."""

    vector: list[float]
    column: str
    k: int = 10
    filter: str | None = None
    columns: list[str] | None = None


class SearchResult(BaseModel):
    """A single vector search result row."""

    data: dict[str, Any]
    distance: float
