"""Pydantic models for Lance data explorer endpoints.

Uses Pydantic v2 for all request validation and response serialization.
Field constraints (ge, le, pattern) enforce invariants at the API boundary.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field


# ── Request query models ─────────────────────────────────────────


class LanceDatasetParams(BaseModel):
    """Common params identifying an S3-hosted Lance dataset."""

    bucket: Annotated[str, Field(min_length=1, description="S3 bucket")]
    path: str = Field("", description="Path prefix within bucket")


class LanceTableParams(LanceDatasetParams):
    """Dataset + table name."""

    table: Annotated[
        str, Field(min_length=1, pattern=r"^[\w\-.]+$", description="Table name")
    ]


class LanceRowsParams(LanceTableParams):
    """Rows query with pagination and optional filter."""

    limit: Annotated[int, Field(ge=1, le=200)] = 50
    offset: Annotated[int, Field(ge=0)] = 0
    columns: str | None = Field(None, description="Comma-separated column names")
    filter: str | None = Field(
        None, description="SQL filter expression (Lance push-down)"
    )


class LanceVectorParams(LanceTableParams):
    """Vector preview query."""

    column: Annotated[str, Field(min_length=1, description="Vector column name")]
    limit: Annotated[int, Field(ge=1, le=200)] = 100


class LanceCellParams(LanceTableParams):
    """Single cell content query."""

    column: Annotated[str, Field(min_length=1, description="Column name")]
    row: Annotated[int, Field(ge=0, description="Row index")]


# ── Response models ──────────────────────────────────────────────


class LanceField(BaseModel):
    """Schema field descriptor, with vector/binary detection."""

    name: str
    type: str
    nullable: bool
    is_vector: bool = False
    is_binary: bool = False
    vector_dim: int | None = None


class LanceSchemaResponse(BaseModel):
    table_name: str
    fields: list[LanceField]


class LanceTablesResponse(BaseModel):
    tables: list[str]


class LanceRowsResponse(BaseModel):
    rows: list[dict[str, Any]]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1)]
    offset: Annotated[int, Field(ge=0)]


class VectorPreviewStats(BaseModel):
    count: Annotated[int, Field(ge=0)]
    dim: Annotated[int, Field(ge=1)]
    min: float
    max: float
    mean: float


class VectorPreviewEntry(BaseModel):
    norm: float
    sample: list[float | None]


class VectorPreviewResponse(BaseModel):
    stats: VectorPreviewStats | None
    preview: list[VectorPreviewEntry]


class LanceSearchParams(LanceTableParams):
    """Search query params."""

    query: str | None = Field(None, description="Text query for FTS/hybrid search")
    vector: str | None = Field(
        None, description="JSON-encoded vector for vector search"
    )
    vector_column: str | None = Field(None, description="Vector column name")
    query_type: str = Field("fts", description="Search type: fts, vector, hybrid")
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    filter: str | None = Field(None, description="Optional filter expression")
    weight: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        None, description="Hybrid reranker weight: 0.0=all vector, 1.0=all FTS"
    )


class LanceSearchResponse(BaseModel):
    rows: list[dict[str, Any]]
    total: Annotated[int, Field(ge=0)]
