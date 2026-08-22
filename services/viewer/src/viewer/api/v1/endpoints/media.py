"""Media endpoints + blob/key primitives — descriptor-driven Blob V2 serving.

Ported from the pre-split ``backend/media/{blobs,router}.py`` with every table
and column name resolved from the dataset descriptor at request time: the
document table/blob/mime/thumbnail bindings come from ``declared.document``,
the frames table from the ``visual`` vector binding (else the ``frames``
capability), and the doc-key whitelist pattern from ``declared.identity`` —
that regex is the SQL-injection guard for every interpolated doc key.

Blob reads go through ``ds.take_blobs(..., ids=[rowid])`` — lazy, seekable
``BlobFile`` handles — so an HTTP Range maps directly to ``seek(start) +
read(length)``. ``ids`` are stable logical row ids that survive deletes and
compaction; positional ``indices`` are not, so they are never used here.
"""

import logging
import math
import re
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

import lance
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from service_kit.exceptions import NotFoundError, ValidationError
from service_kit.lancekit.keys import chunk_key_filter, validate_doc_key
from service_kit.lancekit.predicate import eq
from service_kit.lancekit.registry import DatasetHandle, table_dataset
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle
from viewer.services.clips import MAX_CLIP_S, build_clip


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["media"])

_STREAM_CHUNK = 1 << 20  # 1 MiB: amortizes seek cost, bounds per-stream memory

#: The frame-index column of a frames table. A reserved contract name (like
#: ``_rowid``), not corpus knowledge: frame keys are identity.key_fields plus
#: this column, and it is matched in Python (see chunk_frame) per the planner bug.
FRAME_INDEX_COLUMN = "frame_idx"

# ── key + table primitives shared by the media_api routers ──────────────────


def rowid_for_doc(ds: lance.LanceDataset, doc_key: str, doc_id: str) -> int | None:
    """Resolve a validated doc key to a single stable ``_rowid`` (None if absent)."""
    t = ds.to_table(columns=[doc_key], filter=eq(doc_key, doc_id), with_row_id=True)
    if t.num_rows == 0:
        return None
    return int(t.column("_rowid")[0].as_py())


def stream_blob_range(ds: lance.LanceDataset, column: str, rowid: int, *, start: int, end: int) -> Iterator[bytes]:
    """Yield bytes of the inclusive ``[start, end]`` range from a blob column."""
    # `[0]` can be None from pylance 10.0.0: `take_blobs` returns a same-length list with `None`
    # in a null payload's slot, where 9.0.0 omitted the row entirely. Unguarded, `with blob as f:`
    # raises AttributeError on `__enter__` and this endpoint answers 500 for a document whose
    # payload is legitimately absent — a 404 is the honest answer and the one every other
    # absence on this path already gives.
    blob = ds.take_blobs(column, ids=[rowid])[0]
    if blob is None:
        raise NotFoundError("no payload for this row")
    with blob as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


#: A Range header the server does not honor as a single byte range: the
#: caller should IGNORE it and serve the full 200 body (RFC 9110 §14.2 — an
#: unrecognized unit or a form we don't support must not become a 416).
IGNORE_RANGE = "ignore"


def parse_range(header: str, total: int) -> tuple[int, int] | str | None:
    """Classify a Range header.

    Returns ``(start, end)`` for a satisfiable single ``bytes=`` range,
    :data:`IGNORE_RANGE` for a header to ignore (unknown unit, malformed, or a
    valid-but-unsupported multi-range → serve 200 per RFC 9110 §14.2), or
    ``None`` only for a well-formed single ``bytes=`` range that is
    *unsatisfiable* (→ 416).
    """
    m = re.match(r"^\s*bytes=(\d*)-(\d*)\s*$", header)
    if not m:
        # Not a single bytes= range: unknown unit, junk, or multipart. The RFC
        # requires ignoring it (200), never answering 416.
        return IGNORE_RANGE
    s, e = m.group(1), m.group(2)
    if s == "" and e == "":
        return IGNORE_RANGE
    if s == "":
        length = int(e)
        start = max(0, total - length)
        end = total - 1
    else:
        start = int(s)
        end = int(e) if e else total - 1
    if start > end or start >= total:
        return None  # well-formed but unsatisfiable → 416
    return start, min(end, total - 1)


def blob_size(ds: lance.LanceDataset, column: str, rowid: int) -> int:
    """Probe a blob's size without reading its contents. A null payload is size 0, not an error."""
    blob = ds.take_blobs(column, ids=[rowid])[0]
    # pylance 10.0.0 puts `None` in a null payload's slot (9.0.0 omitted the row). Zero is the honest
    # answer here — the callers use it for Content-Length and range satisfiability, and an absent
    # payload is a zero-length body, which is exactly what the serving contract already promises.
    if blob is None:
        return 0
    with blob as f:
        try:
            return f.size()  # type: ignore[attr-defined]
        except AttributeError:
            f.seek(0, 2)
            return f.tell()


# ── descriptor lookups local to the media routes ────────────────────────────


def _cell_value(ds: lance.LanceDataset, doc_key: str, doc_id: str, column: str | None, default: str) -> str:
    """A single scalar cell for a validated doc key, or ``default`` when the
    column is undeclared or the cell is absent/null."""
    if column is None:
        return default
    t = ds.to_table(columns=[column], filter=eq(doc_key, doc_id), limit=1)
    if t.num_rows > 0 and t.column(column)[0].is_valid:
        return t.column(column)[0].as_py()
    return default


def _frames_binding(handle: DatasetHandle) -> tuple[str, str, str | None] | None:
    """(table, blob column, mime column) of the per-chunk frames, or None.

    The table comes from the ``visual`` vector binding when declared (frames
    and frame embeddings share a table), else from the ``frames`` capability's
    table part; the blob column is always the capability's column part. The
    mime column is discovered: the first ``*_mime`` string column beside the
    blob (None → the JPEG default applies).
    """
    declared = handle.descriptor.declared
    target = declared.capabilities.get("frames")
    if not target:
        return None
    cap_table, _, blob_column = target.partition(".")
    if not blob_column:
        return None
    table = cap_table
    if declared.search is not None and "visual" in declared.search.vectors:
        table = declared.search.vectors["visual"].table
    info = handle.descriptor.tables.get(table)
    if info is None or info.column(blob_column) is None:
        return None
    mime_column = next((c.name for c in info.columns if c.name.endswith("_mime") and not c.is_blob), None)
    return table, blob_column, mime_column


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/thumbnail/{doc_id}")
def thumbnail(doc_id: str, state: StateDep, dataset: DatasetParam = None) -> Response:
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    binding = declared.document
    if binding is None:
        raise NotFoundError("documents table missing")
    if binding.thumbnail is None:
        raise NotFoundError("no thumbnail for doc_id")
    ds = table_dataset(handle, binding.table)
    rowid = rowid_for_doc(ds, declared.identity.doc_key, doc_id)
    if rowid is None:
        raise NotFoundError("doc_id not found")
    mime = _cell_value(ds, declared.identity.doc_key, doc_id, binding.thumbnail_mime, "image/jpeg")
    # `[0]` can be None from pylance 10.0.0: `take_blobs` returns a same-length list with `None`
    # in a null payload's slot, where 9.0.0 omitted the row entirely. Unguarded, `with blob as f:`
    # raises AttributeError on `__enter__` and this endpoint answers 500 for a document whose
    # payload is legitimately absent — a 404 is the honest answer and the one every other
    # absence on this path already gives.
    blob = ds.take_blobs(binding.thumbnail, ids=[rowid])[0]
    if blob is None:
        raise NotFoundError("no thumbnail for doc_id")
    with blob as f:
        data = f.read()
    if not data:
        raise NotFoundError("no thumbnail for doc_id")
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/chunk-frame/{doc_id}/{group_id}/{chunk_id}")
def chunk_frame(
    doc_id: str,
    group_id: int,
    chunk_id: int,
    state: StateDep,
    frame_idx: Annotated[int, Query(ge=0)] = 0,
    dataset: DatasetParam = None,
) -> Response:
    """A chunk's frame from the frames table; 404 until frames are extracted.
    ``frame_idx`` selects which frame when a chunk was sampled multiple times
    (0 = the representative frame). The path params map positionally onto the
    descriptor's identity key fields (doc key first)."""
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    binding = _frames_binding(handle)
    if binding is None:
        raise NotFoundError("frame not extracted yet")
    table, blob_column, mime_column = binding
    ds = table_dataset(handle, table)

    # frame_idx is matched in Python, NOT in the SQL predicate. With a scalar
    # (BTREE) index on the frames table's doc key, the combination of
    # `with_row_id=True` + a multi-clause filter that *also* constrains
    # frame_idx trips a Lance planner bug that silently returns 0 rows (the row
    # is there — verified via a filter without the frame_idx clause). Keying on
    # the identity fields in SQL and selecting frame_idx here sidesteps it; a
    # chunk has only a handful of frames, so the extra rows are negligible.
    columns = [FRAME_INDEX_COLUMN] + ([mime_column] if mime_column else [])
    keyed = ds.to_table(
        columns=columns,
        filter=chunk_key_filter(declared, doc_id, (group_id, chunk_id)),
        with_row_id=True,
    )
    row = next(
        (i for i in range(keyed.num_rows) if keyed.column(FRAME_INDEX_COLUMN)[i].as_py() == int(frame_idx)),
        None,
    )
    if row is None:
        raise NotFoundError("frame not extracted yet")
    rowid = keyed.column("_rowid")[row].as_py()
    mime = "image/jpeg"
    if mime_column is not None:
        mime_cell = keyed.column(mime_column)[row]
        mime = mime_cell.as_py() if mime_cell.is_valid else "image/jpeg"
    try:
        blob = ds.take_blobs(blob_column, ids=[rowid])[0]
    except Exception as e:
        logger.warning("frame blob read failed", exc_info=True)
        raise NotFoundError("no frame for chunk") from e
    # Same pylance-10 null slot as the sites above; the surrounding try only covers the take itself.
    if blob is None:
        raise NotFoundError("no frame for chunk")
    with blob as f:
        data = f.read()
    if not data:
        raise NotFoundError("frame body empty")
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/media-clip/{doc_id}")
def media_clip(
    doc_id: str,
    request: Request,
    state: StateDep,
    lo: Annotated[float, Query(ge=0)],
    hi: Annotated[float, Query(gt=0)],
    dataset: DatasetParam = None,
) -> FileResponse:
    """A windowed excerpt of the media with MP3 audio — built for MCP-app
    iframes in hosts whose webview lacks the AAC codec (VS Code). The clip is
    transcoded on first request (ffmpeg over the Range-streaming media
    endpoint) and disk-cached; FileResponse serves it with Range support so
    in-clip seeking works. Timestamps are clip-local: second 0 == ``lo``.
    """
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        raise ValidationError("need finite seconds with hi > lo")
    if hi - lo > MAX_CLIP_S:
        raise ValidationError(f"clip window capped at {MAX_CLIP_S:.0f}s")
    binding = declared.document
    if binding is None:
        raise NotFoundError("doc_id not found")
    ds = table_dataset(handle, binding.table)
    if rowid_for_doc(ds, declared.identity.doc_key, doc_id) is None:
        raise NotFoundError("doc_id not found")
    # build_clip always emits an MP4 container (libx264 + mp3), so the response
    # mime is video/mp4 regardless of the source doc's stored mime.
    mime = "video/mp4"
    # ffmpeg pulls the source through the SAME origin this request arrived on, so
    # the loopback works on every launch path (direct :8101, dev proxy, prod
    # gateway) without a bind-port write-back — the split dropped the monolith's.
    source = f"{str(request.base_url).rstrip('/')}/api/explorer/{doc_id}"
    if dataset:
        source += f"?dataset={quote(dataset, safe='')}"
    path = build_clip(source, f"{handle.id}--{doc_id}", lo, hi)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-store"})


@router.get("/media/{doc_id}")
def media(doc_id: str, request: Request, state: StateDep, dataset: DatasetParam = None) -> Response:
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    binding = declared.document
    if binding is None:
        raise NotFoundError("documents table missing")
    ds = table_dataset(handle, binding.table)
    doc_key = declared.identity.doc_key
    rowid = rowid_for_doc(ds, doc_key, doc_id)
    if rowid is None:
        raise NotFoundError("doc_id not found")

    mime = _cell_value(ds, doc_key, doc_id, binding.mime, "application/octet-stream")
    try:
        total = blob_size(ds, binding.media_blob, rowid)
    except OSError as exc:
        # External-URI media blob that doesn't resolve (dangling file://…): a
        # 404 through the problem+json contract, not a bare 500 (the sibling
        # chunk-frame path guards the identical take_blobs the same way).
        raise NotFoundError("media not available") from exc
    range_hdr = request.headers.get("range")
    if range_hdr:
        rng = parse_range(range_hdr, total)
        if rng is None:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        if rng == IGNORE_RANGE:
            range_hdr = None  # fall through to the full 200 body
    if range_hdr and isinstance(rng, tuple):
        start, end = rng
        return StreamingResponse(
            stream_blob_range(ds, binding.media_blob, rowid, start=start, end=end),
            status_code=206,
            media_type=mime,
            headers={
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
            },
        )

    return StreamingResponse(
        stream_blob_range(ds, binding.media_blob, rowid, start=0, end=total - 1),
        media_type=mime,
        headers={
            "Content-Length": str(total),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        },
    )
