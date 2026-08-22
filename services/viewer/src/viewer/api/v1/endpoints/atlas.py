"""Atlas endpoints — serve the precomputed 2-D projection for the map view.

Ported from the pre-split ``backend/atlas/router.py`` onto the descriptor: the
projection spaces come from ``declared.atlas`` (each naming its x/y/cluster
columns and source table), so the wire surface is unchanged while nothing here
knows the corpus:

* ``GET /status?space=`` — which declared spaces are built (x column has
  non-null rows), and how many rows carry the requested one.
* ``GET /points?space=`` — one Apache Arrow IPC **stream** (binary,
  parse-free): f16 coords, per-point keys, ``DICTIONARY<int32, utf8>``
  categorical channels, int64 ``rowid``, per-doc labels in ``docFiles``
  metadata. Serialization lives in :mod:`viewer.services.points`; the bytes
  are memoized on ``state.points_cache`` keyed (dataset, space, version).
* ``GET /chunk/..`` / ``POST /chunks`` — full hits for one chunk / a lasso
  selection (detail pane + playback), the latter addressed by ``_rowid``.
* ``POST /chunks/by-key`` — the same full hits addressed BY KEY, for callers
  that hold a descriptor key-path rather than a row address. A labelling task
  is exactly that: ``_rowid`` is only stable for the table version it was read
  at, so a task cannot carry one (#60).
"""

import logging
from typing import Annotated, Any

import lance
from fastapi import APIRouter, Query, Response

from service_kit.exceptions import NotFoundError, ValidationError
from service_kit.lancekit.alignments import parse_alignments_json
from service_kit.lancekit.descriptor import AtlasSpace, Declared
from service_kit.lancekit.keys import chunk_key_filter, validate_doc_key
from service_kit.lancekit.predicate import and_, eq, isin, or_
from service_kit.lancekit.registry import DatasetHandle, table_dataset
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle
from viewer.api.v1.endpoints.chunks import alignments_binding
from viewer.api.v1.endpoints.media import FRAME_INDEX_COLUMN
from viewer.api.v1.endpoints.system import DURATION_COLUMN
from viewer.schemas.atlas import ChunkKeys, ChunkRowIds
from viewer.services.points import build_points


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/atlas", tags=["atlas"])

#: Media type for the /points Arrow IPC stream response.
_ARROW_STREAM_MEDIA_TYPE = "application/vnd.apache.arrow.stream"
#: Max memoized /points payloads before oldest-first eviction (each is multi-MB).
_POINTS_CACHE_MAX = 12

#: Rows per caption-attach scan when decorating a selection with frame captions.
_FRAME_CAPTION_BATCH_SIZE = 500

SpaceParam = Annotated[str | None, Query(description="Projection space to read (first declared when omitted).")]


def _space(declared: Declared, name: str | None) -> AtlasSpace:
    """Resolve a declared atlas space by name (None → the first declared)."""
    if not declared.atlas:
        raise NotFoundError("no atlas spaces declared for dataset")
    if name is None:
        return declared.atlas[0]
    space = next((s for s in declared.atlas if s.name == name), None)
    if space is None:
        known = "|".join(s.name for s in declared.atlas)
        raise ValidationError(f"unknown space ({known})")
    return space


def _built_rows(ds: lance.LanceDataset, space: AtlasSpace) -> int:
    """Non-null projected rows — the 'is this space built?' signal. Descriptor
    validation guarantees the columns exist, so presence alone can't gate."""
    if space.x not in ds.schema.names:
        return 0
    return ds.count_rows(filter=f"{space.x} IS NOT NULL")


@router.get("/status")
def atlas_status(state: StateDep, space: SpaceParam = None, dataset: DatasetParam = None) -> dict[str, Any]:
    """Which projection spaces are built, plus the requested space's row count.

    ``spaces`` always reports every declared space's built-ness (so the UI can
    gate its toggle); ``projected``/``rows`` reflect the requested ``space``
    (back-compatible with the old single-space shape).
    """
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    requested = _space(declared, space)
    datasets: dict[str, lance.LanceDataset] = {}
    rows_by_space: dict[str, int] = {}
    for s in declared.atlas:
        if s.table not in datasets:
            datasets[s.table] = table_dataset(handle, s.table)
        rows_by_space[s.name] = _built_rows(datasets[s.table], s)
    rows = rows_by_space[requested.name]
    return {
        "projected": rows > 0,
        "rows": rows,
        "space": requested.name,
        "spaces": {name: n > 0 for name, n in rows_by_space.items()},
    }


@router.get("/points")
def atlas_points(state: StateDep, space: SpaceParam = None, dataset: DatasetParam = None) -> Response:
    """One Apache Arrow IPC stream for the scatter renderer (coords + codes + keys)."""
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    requested = _space(declared, space)
    ds = table_dataset(handle, requested.table)
    if _built_rows(ds, requested) == 0:
        raise ValidationError(f"{requested.name} 2D projection not built yet")

    # Memoize on (dataset, space, table version): the scan+encode is identical
    # until the table is rewritten (version bump) or the backend restarts. The
    # cache lives on AppState (per app instance), never module-global.
    cache = state.points_cache
    key = (handle.id, requested.name, ds.version)
    body = cache.get(key)
    if body is None:
        body = build_points(declared, requested, ds)
        # Bound the cache: each entry is multi-MB and a superseded table version
        # would otherwise strand its payload forever. Evict oldest-first.
        while len(cache) >= _POINTS_CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[key] = body

    return Response(
        content=body,
        media_type=_ARROW_STREAM_MEDIA_TYPE,
        headers={"Cache-Control": "public, max-age=300"},
    )


def _hit_columns(declared: Declared, present: set[str]) -> list[str]:
    """Full-hit projection for the detail pane — every declared display-worthy
    field that exists on the row table (matches the search hit shape)."""
    time_cols = [declared.time.start, declared.time.end] if declared.time else []
    align = alignments_binding(declared)
    row_table = declared.search.row_table if declared.search else None
    align_col = align[1] if align is not None and align[0] == row_table else None
    filterable = declared.search.filterable if declared.search else []
    wanted = [
        *declared.identity.key_fields,
        *declared.display.title,
        *time_cols,
        DURATION_COLUMN,
        declared.display.body,
        declared.display.caption,
        *[m.field for m in declared.display.metadata],
        *filterable,
        align_col,
    ]
    return [c for c in dict.fromkeys(wanted) if c is not None and c in present]


def _finalize_hits(handle: DatasetHandle, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declared = handle.descriptor.declared
    align = alignments_binding(declared)
    align_col = align[1] if align is not None else None
    for hit in rows:
        hit["alignments"] = parse_alignments_json(hit.pop(align_col, None) if align_col else None)
    _attach_frame_captions(handle, rows)
    return rows


def _attach_frame_captions(handle: DatasetHandle, rows: list[dict[str, Any]]) -> None:
    """Attach each chunk's representative-frame (``frame_idx=0``) caption from
    the declared ``captions`` capability — captions live on the frames table,
    not the row table. Batched to keep the number of scans low per selection;
    a no-op when the capability, table, or column is absent (captions are
    decorative, never a reason to fail).
    """
    declared = handle.descriptor.declared
    target = declared.capabilities.get("captions")
    if not target or not rows:
        return
    table, _, column = target.partition(".")
    info = handle.descriptor.tables.get(table)
    if not column or info is None or info.column(column) is None:
        return
    identity = declared.identity
    other_keys = [f for f in identity.key_fields if f != identity.doc_key]
    caption_key = declared.display.caption or column

    def hit_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return (str(row[identity.doc_key]), *(int(row[k]) for k in other_keys))

    try:
        ds = table_dataset(handle, table)
        for start in range(0, len(rows), _FRAME_CAPTION_BATCH_SIZE):
            batch = rows[start : start + _FRAME_CAPTION_BATCH_SIZE]
            # One `doc IN (...)` scan, not a per-hit OR-of-ANDs: IN is a hash-
            # membership scan (~2.4x faster, verified on the old corpus). We
            # over-fetch the matched docs' frame-0 rows and pick out the exact
            # chunks in Python. frame_idx=0 = the representative caption frame.
            docs = {str(r[identity.doc_key]) for r in batch}
            frame_rows = ds.to_table(
                columns=[identity.doc_key, *other_keys, column],
                filter=and_(isin(identity.doc_key, docs), eq(FRAME_INDEX_COLUMN, 0)),
            ).to_pylist()
            by_key = {hit_key(r): r.get(column) for r in frame_rows}
            for hit in batch:
                hit[caption_key] = by_key.get(hit_key(hit))
    except Exception:
        logger.warning("frame caption attach failed", exc_info=True)


@router.get("/chunk/{doc_id}/{group_id}/{chunk_id}")
def atlas_chunk(
    state: StateDep,
    doc_id: str,
    group_id: int,
    chunk_id: int,
    dataset: DatasetParam = None,
) -> dict[str, Any]:
    """Full hit for one chunk (detail pane + playback), looked up by key. The
    path params map positionally onto the descriptor's identity key fields."""
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    if declared.search is None:
        raise NotFoundError("no row table declared for dataset")
    ds = table_dataset(handle, declared.search.row_table)
    columns = _hit_columns(declared, set(ds.schema.names))
    where = chunk_key_filter(declared, doc_id, (group_id, chunk_id))
    rows = ds.to_table(columns=columns, filter=where).to_pylist()
    if not rows:
        raise NotFoundError("chunk not found")
    return _finalize_hits(handle, rows)[0]


@router.post("/chunks/by-key")
def atlas_chunks_by_key(state: StateDep, body: ChunkKeys, dataset: DatasetParam = None) -> dict[str, Any]:
    """Full hits for rows addressed BY KEY — the join a labelling queue needs (#60).

    ``POST /chunks`` beside this is cheaper and stays the right door for anything that came from
    /points: the atlas hands back the very ``_rowid``s it was given. A TASK cannot use it. A task
    carries the descriptor key-path it was sent with, and a row address is only stable for the
    table version it was read at — so a task minted last week holds no usable ``_rowid``, and the
    queue could show a task's KEYS but never the row behind them ("filter to the 1890s" was
    unanswerable for exactly this reason).

    One OR-ed predicate rather than N round trips: the queue joins a PAGE of tasks at a time, and
    a request per row would put 50 sequential fetches behind every page turn. Capped at the same
    1000 as the rowid door — the table render budget is the same budget.

    Rows are returned UNORDERED and possibly SHORT: a key whose row was deleted, or which belongs
    to a different dataset, simply does not come back. The caller re-associates by key and must
    treat a missing row as "no corpus columns for this task" rather than an error — a task can
    legitimately outlive the row it points at, and #37 already made that a first-class state.

    The response is an OBJECT carrying ``key_fields`` beside the rows, unlike the bare list the
    rowid door returns. Re-associating requires reading each row's own key back out of it (order
    cannot be trusted), which means knowing WHICH columns form the key — and a caller holding a
    task has no reason to have fetched the descriptor. Making it fetch one just to use this door
    would be handing it a second source of truth for the identity it is already addressing by.
    """
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    if declared.search is None:
        raise NotFoundError("no row table declared for dataset")
    key_fields = list(declared.identity.key_fields)
    wanted = body.keys[:1000]
    if not wanted:
        return {"rows": [], "key_fields": key_fields}
    ds = table_dataset(handle, declared.search.row_table)
    columns = _hit_columns(declared, set(ds.schema.names))
    # `validate_doc_key` per key, not once: the doc key is the only free-text part of the address,
    # and it is what `chunk_key_filter` escapes into the predicate.
    where = or_(*(chunk_key_filter(declared, validate_doc_key(declared, k.doc_id), k.keys) for k in wanted))
    rows = ds.to_table(columns=columns, filter=where).to_pylist()
    return {"rows": _finalize_hits(handle, rows), "key_fields": key_fields}


@router.post("/chunks")
def atlas_chunks(state: StateDep, body: ChunkRowIds, dataset: DatasetParam = None) -> list[dict[str, Any]]:
    """Full hits for a lasso/box/legend selection, addressed by ``_rowid``.

    A flat ``_rowid IN (...)`` predicate lets Lance fetch exactly the selected
    rows by address (~one random-access take, not a filtered full scan), so a
    1000-point selection resolves in a few ms instead of seconds. Addresses are
    stable for the served table version; stale ids simply don't match. Capped at
    1000 (the table render budget); the full selection still drives map dimming.
    """
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    if declared.search is None:
        raise NotFoundError("no row table declared for dataset")
    rowids = body.rowids[:1000]
    if not rowids:
        return []
    ds = table_dataset(handle, declared.search.row_table)
    columns = _hit_columns(declared, set(ds.schema.names))
    csv = ",".join(str(int(r)) for r in rowids)
    rows = ds.to_table(columns=columns, filter=f"_rowid IN ({csv})").to_pylist()
    return _finalize_hits(handle, rows)
