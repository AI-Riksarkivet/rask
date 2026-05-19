"""Read helpers — scan, take, vector search."""

from __future__ import annotations


import lancedb  # ty: ignore[unresolved-import]
import pyarrow as pa

from rahcp_lance.schemas import ScanParams, SearchResult, VectorSearchParams


async def scan(
    table: lancedb.table.AsyncTable,
    params: ScanParams | None = None,
) -> pa.Table:
    """Scan a table with optional projection, filter, and pagination."""
    p = params or ScanParams()
    query = table.query()
    if p.columns:
        query = query.select(p.columns)
    if p.filter:
        query = query.where(p.filter)
    if p.limit:
        query = query.limit(p.limit)
    if p.offset:
        query = query.offset(p.offset)
    return await query.to_arrow()


async def take(
    table: lancedb.table.AsyncTable,
    indices: list[int],
) -> pa.Table:
    """Take specific rows by index."""
    return (
        await table.query()
        .where(f"_rowid IN ({','.join(str(i) for i in indices)})")
        .to_arrow()
    )


async def vector_search(
    table: lancedb.table.AsyncTable,
    params: VectorSearchParams,
) -> list[SearchResult]:
    """Perform vector similarity search."""
    query = (
        table.query().nearest_to(params.vector).column(params.column).limit(params.k)
    )
    if params.filter:
        query = query.where(params.filter)
    if params.columns:
        query = query.select(params.columns)
    result = await query.to_arrow()
    rows: list[SearchResult] = []
    for batch in result.to_batches():
        for row in batch.to_pylist():
            distance = row.pop("_distance", 0.0)
            rows.append(SearchResult(data=row, distance=distance))
    return rows
