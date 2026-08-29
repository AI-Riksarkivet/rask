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

import asyncio
import logging
import math
import re
from collections.abc import Iterator
from enum import Enum
from io import BytesIO
from typing import Annotated
from urllib.parse import quote

import lance
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from lance.blob import BlobFile
from pydantic import BaseModel

from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError, ValidationError
from service_kit.governed.audit import FAILURE, audit
from service_kit.lancekit.keys import chunk_key_filter, validate_doc_key
from service_kit.lancekit.predicate import eq
from service_kit.lancekit.registry import DatasetHandle, table_dataset
from service_kit.media.authz import corpus_object
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle
from viewer.api.security import READ_DATA, REQUIRE_MEDIA_BYTES, CheckerDep, CurrentSubject, SettingsDep
from viewer.services.clips import MAX_CLIP_S, ClipBusyError, build_clip


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["media"])

_STREAM_CHUNK = 1 << 20  # 1 MiB: amortizes seek cost, bounds per-stream memory

#: Above this a derivative is streamed instead of buffered. file-handling.md's response-type table
#: puts `Response(content=bytes)` at "tiny file (< 1 MB), already in memory" and everything larger in
#: a `StreamingResponse`. The thumbnail and frame routes serve cached derivatives that sit far below
#: it — but "far below it" was the descriptor's promise, not this service's decision: both columns
#: are resolved from the dataset descriptor at request time, so an unbounded `read()` there
#: materialises whatever the DATA names. A threshold rather than a refusal, because a large
#: derivative is still a legitimate one and 413 is defined against the request body, not the response.
MAX_BUFFERED_BLOB_BYTES = 1 << 20

#: Both derivative routes are cacheable for a day; the media route deliberately is not.
_DERIVATIVE_CACHE = {"Cache-Control": "public, max-age=86400"}

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
    blob = ds.take_blobs(column, ids=[rowid])[0]
    if blob is None:
        # THIS NO LONGER RAISES, and that is the fix. `payload_size` has already refused a null
        # payload with a 404, before any response object existed. Reaching here means that guard was
        # bypassed — and by now the headers are on the wire, so an exception cannot become a status:
        # raising is what produced a 200 with `Content-Length: 0` followed by an unhandled-ASGI
        # traceback. An empty body is the only outcome that cannot contradict what was already sent,
        # so the violation is reported where it can still be acted on: the log.
        logger.error("null payload reached the stream for rowid %s in %r — the route guard was bypassed", rowid, column)
        return
    with blob as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _handle_size(f: BlobFile) -> int:
    """Length of an open blob handle, leaving the read position at the start.

    The `size()` fallback is for handles that predate it; the re-seek is not cosmetic — the fallback
    leaves the cursor at EOF, and `blob_response` reads from the same handle it probed.
    """
    try:
        return f.size()
    except AttributeError:
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        return size


def blob_response(blob: BlobFile, *, mime: str, empty_detail: str) -> Response:
    """Serve a derivative blob, buffering it only while it stays small.

    Takes the OPEN HANDLE rather than `(ds, column, rowid)`: both callers have already taken it and
    null-checked it with their own 404 wording, and `take_blobs` is object-store IO on a media route
    — re-taking it here would double that on the common path to save passing one argument.
    """
    size = _handle_size(blob)
    if size > MAX_BUFFERED_BLOB_BYTES:
        return StreamingResponse(
            _stream_handle(blob, size),
            media_type=mime,
            # Known exactly from the probe, so the caller gets a progress bar rather than a chunked
            # stream of unknown length. Safe to declare because the generator owns this handle: the
            # bytes it yields and the size measured here come from the same open blob.
            headers={**_DERIVATIVE_CACHE, "Content-Length": str(size)},
        )
    with blob as f:
        data = f.read(size)
    if not data:
        raise NotFoundError(empty_detail)
    return Response(content=data, media_type=mime, headers=_DERIVATIVE_CACHE)


def _stream_handle(blob: BlobFile | BytesIO, size: int) -> Iterator[bytes]:
    """Yield a whole payload handle in bounded chunks, closing it when the client goes away.

    ``BytesIO`` beside ``BlobFile`` because `pages._take_page` serves a plain ``large_binary``
    payload — a shape with no blob take-path — through the same streaming branch; the generator only
    ever reads, seeks and closes, which both handles honour.
    """
    with blob as f:
        remaining = size
        while remaining > 0:
            chunk = f.read(min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


class RangeVerdict(Enum):
    """A Range header the server will not serve as a 206 — and WHY, since the two
    answers differ: IGNORE means serve the full 200 body (RFC 9110 §14.2 — an
    unrecognized unit or an unsupported form must not become a 416); UNSATISFIABLE
    means a well-formed single ``bytes=`` range the body cannot satisfy (→ 416).

    An enum plus :class:`ByteRange` rather than the old ``tuple | str-sentinel |
    None`` (VS-20): three anonymous shapes forced the caller to decode the answer
    by rebinding its header variable, and a string sentinel is one typo away from
    a header value.
    """

    IGNORE = "ignore"
    UNSATISFIABLE = "unsatisfiable"


class ByteRange(BaseModel):
    """A satisfiable inclusive byte range — the 206 answer."""

    start: int
    end: int


def parse_range(header: str, total: int) -> ByteRange | RangeVerdict:
    """Classify a Range header into exactly one of the three RFC outcomes.

    Returns a :class:`ByteRange` for a satisfiable single ``bytes=`` range,
    :attr:`RangeVerdict.IGNORE` for a header to ignore (unknown unit, malformed,
    or a valid-but-unsupported multi-range → serve 200 per RFC 9110 §14.2), or
    :attr:`RangeVerdict.UNSATISFIABLE` only for a well-formed single ``bytes=``
    range the body cannot satisfy (→ 416).
    """
    m = re.match(r"^\s*bytes=(\d*)-(\d*)\s*$", header)
    if not m:
        # Not a single bytes= range: unknown unit, junk, or multipart. The RFC
        # requires ignoring it (200), never answering 416.
        return RangeVerdict.IGNORE
    s, e = m.group(1), m.group(2)
    if s == "" and e == "":
        return RangeVerdict.IGNORE
    if s == "":
        length = int(e)
        start = max(0, total - length)
        end = total - 1
    else:
        start = int(s)
        end = int(e) if e else total - 1
    if start > end or start >= total:
        return RangeVerdict.UNSATISFIABLE
    return ByteRange(start=start, end=min(end, total - 1))


def payload_size(ds: lance.LanceDataset, column: str, rowid: int) -> int:
    """Probe a blob's size without reading its contents; refuse a row that has no payload.

    ONE PROBE, ONE DECISION, and that is the fix rather than an optimisation. This was `blob_size`,
    which returned 0 for a null payload under a comment calling zero "the honest answer" — while its
    sibling `stream_blob_range` raised `NotFoundError` for the same value under a comment calling 404
    "the honest answer and the one every other absence on this path already gives". The non-range
    branch of `media()` called both, in that order, so a null-payload document got `total = 0`, fell
    through to a `StreamingResponse`, and then raised on the generator's FIRST step — after starlette
    had already sent `http.response.start` with a 200 and `Content-Length: 0`. By then the exception
    handlers are out of reach: the caller received a 200 with an empty body where every sibling
    absence gives a 404 problem+json, and the server logged an unhandled-ASGI traceback each time.

    So the question is answered once, here, where a status can still be chosen — and answered the way
    the rest of the path answers it. Merged into the size probe rather than added beside it because
    `take_blobs` is object-store IO on a media route, and a separate guard would have doubled it while
    leaving this function's zero branch unreachable from its only caller.
    """
    blob = ds.take_blobs(column, ids=[rowid])[0]
    # pylance 10.0.0 puts `None` in a null payload's slot; 9.0.0 omitted the row entirely.
    if blob is None:
        raise NotFoundError("media not available")
    with blob as f:
        return _handle_size(f)


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


@router.get("/thumbnail/{doc_id}", dependencies=[REQUIRE_MEDIA_BYTES])
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
    return blob_response(blob, mime=mime, empty_detail="no thumbnail for doc_id")


@router.get("/chunk-frame/{doc_id}/{group_id}/{chunk_id}", dependencies=[REQUIRE_MEDIA_BYTES])
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
    return blob_response(blob, mime=mime, empty_detail="frame body empty")


@router.get("/media-clip/{doc_id}")
async def media_clip(
    doc_id: str,
    request: Request,
    state: StateDep,
    subject: CurrentSubject,
    checker: CheckerDep,
    settings: SettingsDep,
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

    # AUTHZ, after shape. `can_read_data`, not `can_get_metadata`: a clip IS the media, so this is
    # the rung `pages.py` uses for image bytes rather than the one `datasets.py` uses for a listing.
    # The whole module carried no auth constructs at all until 2026-08-26, while its two siblings
    # gated the same corpus objects — and the viewer is published at the edge through the gateway's
    # `/api/explorer` row.
    obj = corpus_object(settings, handle.id, binding.table)
    if not await checker(user=subject, relation=READ_DATA, obj=obj):
        audit("viewer.clip.read", FAILURE, subject=subject, resource=handle.id, relation=READ_DATA)
        raise ForbiddenError(f"{subject} lacks {READ_DATA} on {obj}")

    # OFF THE LOOP, and refusing rather than queueing. This route was a plain `def`, so FastAPI
    # threadpooled it — and every caller waiting on the old global build lock held a POOL THREAD for
    # as long as the queue ahead of it took, 120 s ffmpeg timeout at the head. Measured: 41 concurrent
    # requests exhausted the pool while `/livez` stayed green, because liveness does not know the pool
    # is gone. It is `async def` now for the authz above, so the blocking build must go to a thread
    # explicitly; `build_clip` takes a bounded slot and raises `ClipBusyError` instead of waiting.
    try:
        path = await asyncio.to_thread(build_clip, source, f"{handle.id}--{doc_id}", lo, hi)
    except ClipBusyError as exc:
        # 503 + Retry-After: a fact the caller can act on. Queueing would have told them nothing and
        # cost a thread to say it.
        raise ServiceUnavailableError(str(exc), headers={"Retry-After": "5"}) from exc
    # THE STAT WE ALREADY TOOK, handed to starlette so it does not take a second one.
    #
    # `build_clip` returns a path it has just confirmed exists; `FileResponse` stats it again when the
    # response is sent. Between those two moments `evict_old_clips` can unlink it — the cache is bounded
    # and every build evicts — and the caller gets a 500 for a request that was valid and a file that
    # was there when it was checked. With the `stat_result` supplied there is no second stat, and on
    # Linux the inode outlives the unlink for an open handle, so the bytes are still served.
    #
    # Best-effort: if the clip is ALREADY gone by the time we stat it, fall through to starlette's own
    # handling rather than invent a response — that is a genuine 404, not the race.
    try:
        stat_result = await asyncio.to_thread(path.stat)
    except OSError:
        stat_result = None
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-store"}, stat_result=stat_result)


@router.get("/media/{doc_id}", dependencies=[REQUIRE_MEDIA_BYTES])
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
        total = payload_size(ds, binding.media_blob, rowid)
    except OSError as exc:
        # External-URI media blob that doesn't resolve (dangling file://…): a
        # 404 through the problem+json contract, not a bare 500 (the sibling
        # chunk-frame path guards the identical take_blobs the same way).
        raise NotFoundError("media not available") from exc
    range_hdr = request.headers.get("range")
    if range_hdr:
        rng = parse_range(range_hdr, total)
        if rng is RangeVerdict.UNSATISFIABLE:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        if isinstance(rng, ByteRange):
            return StreamingResponse(
                stream_blob_range(ds, binding.media_blob, rowid, start=rng.start, end=rng.end),
                status_code=206,
                media_type=mime,
                headers={
                    "Content-Length": str(rng.end - rng.start + 1),
                    "Content-Range": f"bytes {rng.start}-{rng.end}/{total}",
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-store",
                },
            )
        # RangeVerdict.IGNORE: fall through to the full 200 body.

    return StreamingResponse(
        stream_blob_range(ds, binding.media_blob, rowid, start=0, end=total - 1),
        media_type=mime,
        headers={
            "Content-Length": str(total),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        },
    )
