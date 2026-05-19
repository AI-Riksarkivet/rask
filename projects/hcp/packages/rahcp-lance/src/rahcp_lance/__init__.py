"""rahcp-lance — Lance dataset operations over HCP S3."""

from __future__ import annotations

from rahcp_lance.dataset import LanceDataset
from rahcp_lance.query import scan, take, vector_search
from rahcp_lance.schemas import (
    FieldInfo,
    IngestResult,
    ScanParams,
    SearchResult,
    TableInfo,
    VectorSearchParams,
)

__all__ = [
    "LanceDataset",
    "FieldInfo",
    "IngestResult",
    "ScanParams",
    "SearchResult",
    "TableInfo",
    "VectorSearchParams",
    "scan",
    "take",
    "vector_search",
]
