"""Arrow-IPC wire serving — the annotator's READ path.

The PixiJS engine consumes Arrow IPC directly (zero-copy geometry views), so we stream
the Lance annotations table as an Arrow IPC stream — the same wire format the atlas
``/points`` uses. A missing table degrades to an empty stream (a dataset with no
annotations yet is not an error).
"""

from typing import Annotated

import pyarrow as pa
from common.core.exceptions import NotFoundError
from common.deps import DatasetParam, StateDep
from common.lancekit.keys import chunk_key_filter, validate_doc_key
from common.lancekit.reader import open_catalog_reader, open_reader
from common.lancekit.registry import table_dataset
from common.state import dataset_handle
from fastapi import APIRouter, Query, Response

from annotator.annotations.schema import ANNOTATIONS_TABLE, EMPTY_SCHEMA
from annotator.annotations.versions import VERSION_SOURCE_HEADER, checkout


router = APIRouter(tags=["annotate"])

ARROW_STREAM = "application/vnd.apache.arrow.stream"


def ipc_stream(table: pa.Table) -> bytes:
    """Serialize an Arrow table to an IPC stream (the wire the engine parses)."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@router.get("/annotations/{doc_id}/{speech_id}/{chunk_id}")
def annotations(
    state: StateDep,
    doc_id: str,
    speech_id: int,
    chunk_id: int,
    dataset: DatasetParam = None,
    version: Annotated[int | None, Query(ge=1, description="Read a historical version (time-travel).")] = None,
) -> Response:
    """Arrow IPC stream of the annotations for one media unit (doc + identity keys).

    The keys map positionally onto the descriptor's identity fields (same shape as
    the chunk / chunk-frame routes). ``version`` reads a HISTORICAL snapshot (Lance
    time-travel) for the compare-versions view; omitted ⇒ the latest."""
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    settings = state.settings
    # In full catalog mode the catalog table is the truth — the local replica is
    # not opened at all (a catalog-only deployment must not 404 on a missing local
    # directory). Any other configuration keeps today's local resolution.
    ds = None
    if not settings.rest_catalog_mode:
        try:
            ds = table_dataset(handle, ANNOTATIONS_TABLE)
        except NotFoundError:
            # No annotations table yet — serve an empty stream so the client renders 0.
            return _empty_stream_response()
    where = chunk_key_filter(declared, doc_id, (speech_id, chunk_id))
    if version is not None:
        if settings.rest_catalog_mode:
            # Catalog-mode time-travel: the version pin on the catalog's /query —
            # the SAME number-space the current-read header reports, so the panel
            # can feed a listed version straight back here. A bad/reclaimed
            # version (or a table the catalog doesn't know) is the catalog's 404,
            # translated to ours — never a silent empty local fallback.
            reader = open_catalog_reader(table_id=settings.catalog_table_id(handle.id, ANNOTATIONS_TABLE), settings=settings)
            table = reader.to_table(filter=where, version=version)
            return _annotations_response(table, version, source="catalog")
        # A historical read is a direct time-travel snapshot of the LOCAL lineage
        # (read-only, off the hot path) — its own number-space, named by the header.
        assert ds is not None  # narrowed: the local branch above always opened it
        table = checkout(ds, version).to_table(filter=where)
        return _annotations_response(table, version, source="local")
    # Reads flow through the reader seam (direct default = byte-identical; the
    # catalog's /query in catalog mode) — and so does the VERSION the client echoes
    # back on Save. The version is read BEFORE the rows: with monotonic versions an
    # interleaved commit then yields a spurious 409 on the next save (client
    # reloads) instead of a header claiming a version whose rows were never served
    # — the fail-safe direction of the optimistic-concurrency contract.
    reader = open_reader(
        dataset=ds,
        table_id=settings.catalog_table_id(handle.id, ANNOTATIONS_TABLE),
        settings=settings,
    )
    try:
        served_version = reader.table_version()
        table = reader.to_table(filter=where)
    except NotFoundError:
        # Catalog mode, table not created in the catalog yet — same degrade as a
        # missing local table: the client renders 0 annotations.
        return _empty_stream_response()
    source = "catalog" if settings.rest_catalog_mode else "direct"
    return _annotations_response(table, served_version, source=source)


def _empty_stream_response() -> Response:
    return Response(
        content=ipc_stream(EMPTY_SCHEMA.empty_table()),
        media_type=ARROW_STREAM,
        headers={"Cache-Control": "no-store", "X-Annotations-Version": "0"},
    )


def _annotations_response(table: pa.Table, version: int, *, source: str) -> Response:
    """The wire response: rows + the version the client echoes on Save. The
    ``source`` header names which version number-space the number belongs to
    (``catalog`` vs the local ``direct``/``local`` lineage) so compare-versions
    tooling can never silently mix the two pre-merge."""
    return Response(
        content=ipc_stream(table),
        media_type=ARROW_STREAM,
        headers={
            "Cache-Control": "no-store",
            "X-Annotations-Version": str(version),
            VERSION_SOURCE_HEADER: source,
        },
    )
