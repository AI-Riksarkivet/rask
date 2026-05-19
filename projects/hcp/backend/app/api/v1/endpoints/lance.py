"""Lance dataset explorer endpoints.

Uses Pydantic query models for request validation — all constraints
(table name pattern, limit range, etc.) are enforced at the API boundary.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_lance_service
from app.schemas.lance import (
    LanceCellParams,
    LanceDatasetParams,
    LanceRowsParams,
    LanceRowsResponse,
    LanceSchemaResponse,
    LanceSearchParams,
    LanceSearchResponse,
    LanceTableParams,
    LanceTablesResponse,
    LanceVectorParams,
    VectorPreviewResponse,
)
from app.services.cached_lance import CachedLanceService
from app.services.lance_service import LanceError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lance", tags=["Lance Explorer"])


def _handle_lance_error(exc: Exception, context: str) -> HTTPException:
    """Map LanceError / ValueError to appropriate HTTP status codes."""
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, LanceError):
        logger.warning("Lance error (%s): %s", context, exc)
        return HTTPException(status_code=422, detail=str(exc))
    logger.error("Unexpected error (%s): %s", context, exc)
    return HTTPException(status_code=500, detail=f"Internal error: {context}")


@router.get("/tables", response_model=LanceTablesResponse)
async def list_tables(
    params: LanceDatasetParams = Depends(),
    lance: CachedLanceService = Depends(get_lance_service),
):
    """List all tables in the Lance dataset directory."""
    try:
        tables = await lance.list_tables()
    except Exception as exc:
        raise _handle_lance_error(exc, "list tables")
    return LanceTablesResponse(tables=tables)


@router.get("/schema", response_model=LanceSchemaResponse)
async def get_schema(
    params: LanceTableParams = Depends(),
    lance: CachedLanceService = Depends(get_lance_service),
):
    """Return schema for a Lance table."""
    try:
        result = await lance.get_schema(params.table)
    except Exception as exc:
        raise _handle_lance_error(exc, f"schema for {params.table}")
    return result


@router.get("/rows", response_model=LanceRowsResponse)
async def get_rows(
    params: LanceRowsParams = Depends(),
    lance: CachedLanceService = Depends(get_lance_service),
):
    """Return paginated rows from a Lance table."""
    col_list = (
        [c.strip() for c in params.columns.split(",") if c.strip()]
        if params.columns
        else None
    )
    try:
        result = await lance.get_rows(
            params.table,
            params.limit,
            params.offset,
            col_list,
            params.filter,
        )
    except Exception as exc:
        raise _handle_lance_error(exc, f"rows for {params.table}")
    return result


@router.get("/vector-preview", response_model=VectorPreviewResponse)
async def get_vector_preview(
    params: LanceVectorParams = Depends(),
    lance: CachedLanceService = Depends(get_lance_service),
):
    """Return stats and sample vectors for a vector column."""
    try:
        result = await lance.get_vector_preview(
            params.table,
            params.column,
            params.limit,
        )
    except Exception as exc:
        raise _handle_lance_error(exc, f"vector preview {params.table}.{params.column}")
    return result


@router.get("/search", response_model=LanceSearchResponse)
async def search(
    params: LanceSearchParams = Depends(),
    lance: CachedLanceService = Depends(get_lance_service),
):
    """Search a Lance table using FTS, vector, or hybrid search."""
    query_vector = None
    if params.vector:
        try:
            query_vector = json.loads(params.vector)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid vector JSON")

    try:
        result = await lance.search(
            params.table,
            query_text=params.query,
            query_vector=query_vector,
            vector_column=params.vector_column,
            query_type=params.query_type,
            limit=params.limit,
            filter_expr=params.filter,
            weight=params.weight,
        )
    except Exception as exc:
        raise _handle_lance_error(exc, f"search {params.table}")
    return result


@router.get("/cell")
async def get_cell(
    params: LanceCellParams = Depends(),
    lance: CachedLanceService = Depends(get_lance_service),
):
    """Stream raw binary content for a single cell."""
    try:
        data = await lance.get_cell_bytes(
            params.table,
            params.column,
            params.row,
        )
    except Exception as exc:
        raise _handle_lance_error(
            exc, f"cell {params.table}.{params.column}[{params.row}]"
        )
    if data is None:
        raise HTTPException(status_code=404, detail="Cell is null")
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Length": str(len(data))},
    )
