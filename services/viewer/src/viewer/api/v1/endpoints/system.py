"""System endpoints — the health badge, filterable columns, documents gallery.

Ported from the pre-split ``backend/system/router.py`` onto the descriptor:
DB facts come from the resolved dataset handle, filterable columns are the
discovered scalar columns of the declared row table (vector, blob, and
alignments columns excluded), and the gallery projects the doc-level fields
the descriptor declares (metadata + doc key + duration) that actually exist
on the documents table.
"""

import logging
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import computed_field

from service_kit.lancekit.introspect import ColumnInfo
from service_kit.lancekit.registry import table_dataset
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle
from service_kit.pagination import Page, PaginationDep, build_page
from viewer.api.v1.endpoints.chunks import alignments_binding
from viewer.schemas.system import ColumnKind, DbFacts, FilterColumn, HealthResponse, VllmPing


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["system"])

#: A doc-level duration column, when the corpus has one (seconds). A reserved
#: contract name for the gallery projection, not a corpus binding.
DURATION_COLUMN = "duration"


@router.get("/health")
def health(state: StateDep, dataset: DatasetParam = None) -> HealthResponse:
    """Frontend status badge: pings vLLM embed/rerank, reports DB facts.

    ALWAYS 200 — this is the media plane's liveness/capability probe, not a dataset read.

    It used to resolve the dataset FIRST, so a deployment whose corpus volume is empty (now the chart
    default: ``explorer.corpus.mode=emptyDir``, since the old hostPath wedged every fresh cluster) answered
    404 "dataset 'transcripts_v2' not found" to the one endpoint the media and annotator zones poll on
    every page load. Observed live 2026-07-28 through both zones' BFF proxies: a console 404 per load,
    the sidebar dot permanently red, and — because the descriptor store derives the default dataset id
    from ``db.path`` — no dataset at all, all reported as "backend unreachable" when the backend was
    perfectly healthy and simply had nothing loaded.

    A probe that cannot distinguish "service down" from "no corpus mounted" is worse than no probe, so
    the dataset became OPTIONAL here: encoder reachability is always reported (it does not depend on a
    dataset), and ``db`` is ``None`` with ``db_error`` carrying the resolution failure verbatim. The
    dataset-bound endpoints beside this one still 404 — that is correct for a read of a thing that isn't
    there; it is only wrong for the probe that asks whether anything is there at all.
    """
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

    # Pinged FIRST and unconditionally: encoder reachability is what the search-mode gating reads and it
    # has nothing to do with whether a dataset is loaded.
    embed = _ping(state.settings.embed_url)
    rerank = _ping(state.settings.rerank_url)

    try:
        handle = dataset_handle(state, dataset)
    except Exception as e:
        # NotFoundError (unknown id / missing-or-invalid descriptor) is the expected case on an
        # un-loaded corpus; anything else (a broken S3 endpoint, a corrupt manifest) is equally a
        # "no db facts" answer and equally not a reason to fail the probe.
        logger.info("health: no dataset resolved (%s: %s)", type(e).__name__, e)
        reason = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        return HealthResponse(db=None, db_error=reason, embed=embed, rerank=rerank)

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
        embed=embed,
        rerank=rerank,
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


class DocumentsPage(Page[dict[str, Any]]):
    """The shared envelope plus the key this route used to send.

    `docs` is DEPRECATED and mirrors `items`. It stays for one release because a rename is a wire
    change and the web pods roll separately from this service — `annotator`'s `DataSelection.svelte`
    reads `docsPage.docs`, so an un-rolled pod talking to a new viewer would render an empty gallery.
    Remove it once the zones are known to be past this version.
    """

    @computed_field
    @property
    def docs(self) -> list[dict[str, Any]]:
        return self.items


@router.get("/documents")
def documents(
    state: StateDep,
    # THE ESTATE'S SHARED OFFSET PARAMS, and the reason they are a dependency rather than two
    # `Query(...)` arguments: `page: Query(ge=1)` bounds the page NUMBER and therefore bounds nothing
    # — `?page=1000000` derived an offset of 99,999,900 and was served. The guard has to run before
    # the body, which is the only place refusing is cheap. Wire names are unchanged (`page`,
    # `per_page`) and so is the 100 ceiling.
    pagination: PaginationDep,
    dataset: DatasetParam = None,
) -> DocumentsPage:
    """Documents gallery: the declared doc-level display fields, paged."""
    per_page = pagination.page_size
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    binding = declared.document
    info = handle.descriptor.tables.get(binding.table) if binding is not None else None
    if binding is None or info is None:
        return DocumentsPage.model_validate(build_page([], total=0, params=pagination).model_dump())
    wanted = [
        declared.identity.doc_key,
        *[m.field for m in declared.display.metadata],
        DURATION_COLUMN,
    ]
    columns = [c for c in dict.fromkeys(wanted) if info.column(c) is not None]
    ds = table_dataset(handle, binding.table)
    # `count_rows()` STAYS unconditional. The audit proposed `total if page == 1 else None` and its own
    # verifier withdrew the cost model: an unfiltered Lance count is answered from fragment metadata,
    # not a table scan. Making `total` null on every later page would degrade the envelope for an
    # unproven saving.
    total = ds.count_rows()
    tbl = ds.to_table(columns=columns, limit=per_page, offset=pagination.offset)
    return DocumentsPage.model_validate(build_page(tbl.to_pylist(), total=total, params=pagination).model_dump())
