"""System endpoints — the health badge, filterable columns, documents gallery.

Ported from the pre-split ``backend/system/router.py`` onto the descriptor:
DB facts come from the resolved dataset handle, filterable columns are the
discovered scalar columns of the declared row table (vector, blob, and
alignments columns excluded), and the gallery projects the doc-level fields
the descriptor declares (metadata + doc key + duration) that actually exist
on the documents table.
"""

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Query

from service_kit.lancekit.introspect import ColumnInfo
from service_kit.lancekit.registry import table_dataset
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle
from service_kit.schemas.system import ColumnKind, DbFacts, FilterColumn, HealthResponse, VllmPing
from viewer.api.v1.endpoints.transcripts import alignments_binding


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["system"])

#: A doc-level duration column, when the corpus has one (seconds). A reserved
#: contract name for the gallery projection, not a corpus binding.
DURATION_COLUMN = "duration"


@router.get("/health")
def health(state: StateDep, dataset: DatasetParam = None) -> HealthResponse:
    """Frontend status badge: pings vLLM embed/rerank, reports DB facts."""
    handle = dataset_handle(state, dataset)
    # One pooled client per process (state.http); the module fallback only fires
    # for bare AppState constructions in unit tests.
    http = state.http if state.http is not None else httpx

    def _ping(url: str) -> VllmPing:
        try:
            r = http.get(f"{url}/health", timeout=1.5)
            return VllmPing(ok=r.status_code == 200, url=url)
        except Exception as e:
            # Keep the exception TYPE — httpx messages alone (e.g. a bare host)
            # don't identify whether it was a timeout, refusal, or DNS failure.
            first_line = str(e).split("\n")[0][:100]
            return VllmPing(ok=False, url=url, error=f"{type(e).__name__}: {first_line}")

    declared = handle.descriptor.declared
    tables = handle.descriptor.tables
    row_table = declared.search.row_table if declared.search is not None else None
    row_info = tables.get(row_table) if row_table else None
    doc_info = tables.get(declared.document.table) if declared.document is not None else None
    return HealthResponse(
        db=DbFacts(
            # handle.uri, not str(handle.path): Path() collapses an s3:// URI's
            # double slash to "s3:/…", so the badge would show a mangled root.
            path=handle.uri,
            tables=sorted(tables),
            chunks=row_info.row_count if row_info is not None else 0,
            documents=doc_info.row_count if doc_info is not None else 0,
        ),
        embed=_ping(state.settings.embed_url),
        rerank=_ping(state.settings.rerank_url),
    )


def _column_kind(arrow_type: str) -> ColumnKind | None:
    """Friendly filter kind for a discovered Arrow type (None = not filterable).

    Works on the stringified type carried by :class:`ColumnInfo` — the same
    buckets the old pyarrow-based check used; list/struct/binary/json fall
    through to None because they can't appear in a SQL filter anyway.
    """
    if arrow_type == "bool":
        return ColumnKind.boolean
    if arrow_type.startswith(("int", "uint")) or arrow_type in {"float", "double", "halffloat"}:
        return ColumnKind.number
    if arrow_type.startswith(("timestamp", "date32", "date64", "time32", "time64", "duration")):
        return ColumnKind.time
    if arrow_type in {"string", "large_string", "string_view"}:
        return ColumnKind.text
    return None


def _filterable(column: ColumnInfo, exclude: set[str]) -> ColumnKind | None:
    if column.name in exclude or column.vector_dim is not None or column.is_blob:
        return None
    return _column_kind(column.arrow_type)


@router.get("/columns")
def columns(state: StateDep, dataset: DatasetParam = None) -> list[FilterColumn]:
    """Filterable scalar columns of the declared row table (name + friendly kind).

    Lets the UI show *what* can go in a WHERE filter. Vector / blob / list /
    alignments columns are omitted — they can't appear in a SQL filter anyway.
    """
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    if declared.search is None:
        return []
    info = handle.descriptor.tables.get(declared.search.row_table)
    if info is None:
        return []
    align = alignments_binding(declared)
    exclude = {align[1]} if align is not None and align[0] == declared.search.row_table else set()
    out: list[FilterColumn] = []
    for column in info.columns:
        kind = _filterable(column, exclude)
        if kind is not None:
            out.append(FilterColumn(name=column.name, type=kind))
    return out


@router.get("/documents")
def documents(
    state: StateDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 24,
    dataset: DatasetParam = None,
) -> dict[str, Any]:
    """Documents gallery: the declared doc-level display fields, paged."""
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    binding = declared.document
    info = handle.descriptor.tables.get(binding.table) if binding is not None else None
    if binding is None or info is None:
        return {"total": 0, "page": 1, "docs": []}
    wanted = [
        declared.identity.doc_key,
        *[m.field for m in declared.display.metadata],
        DURATION_COLUMN,
    ]
    columns = [c for c in dict.fromkeys(wanted) if info.column(c) is not None]
    ds = table_dataset(handle, binding.table)
    total = ds.count_rows()
    offset = max(0, (page - 1) * per_page)
    tbl = ds.to_table(columns=columns, limit=per_page, offset=offset)
    return {"total": total, "page": page, "docs": tbl.to_pylist()}
