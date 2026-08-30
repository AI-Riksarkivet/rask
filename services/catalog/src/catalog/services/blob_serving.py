"""The credential-less blob serving path: RFC 9110 HTTP read semantics over one Lance blob payload.

The catalog's data plane (``dataplane.py``) speaks pylance — open a dataset, mutate it, commit,
read schema. This module speaks HTTP: byte ranges and their clamping, suffix ranges, ``If-Range``
validators, strong ETags, satisfiability, and a bounded streaming window sized for a response body.
It changes when the spec's read is refined or a client's resume behaviour must be honoured, not when
pylance's dataset API moves — which is why it is its own module.

``read_blob`` resolves one ``(table, column, row[, version])`` address plus the request's range into
a :class:`BlobStream`: the resolved window, the validator, and a lazy chunked reader. It performs
exactly one dataset open, because a suffix range cannot be resolved without the payload's size. The
endpoint in ``api/v1/endpoints/data.py`` turns that value object into status, headers and body; it
computes no range arithmetic of its own.

Nothing here buffers a payload — a multi-GB blob is served through bounded ``read_range`` windows,
the read-side counterpart of the write-side body-limit OOM guard.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lance_namespace import (
    InvalidInputError,
    LanceNamespace,
    TableColumnNotFoundError,
    TableNotFoundError,
    TableVersionNotFoundError,
)
from pydantic import BaseModel, ConfigDict, Field

from catalog.core.namespace import open_dataset
from service_kit.lakehouse import blobs
from service_kit.lakehouse.objectfs import StorageOptions


# Streaming window for the blob serving path: each chunk is one ``read_range`` call against
# storage, so the catalog never holds more than this per in-flight blob response (the read-side
# counterpart of the write-side ``BodySizeLimitMiddleware`` OOM guard).
_BLOB_CHUNK_BYTES = 8 * 1024 * 1024


class BlobStream(BaseModel):
    """One served blob read: the resolved window + a chunked reader, never the buffered payload.

    ``start``/``length`` are the RESOLVED window (``end`` derives the inclusive RFC 9110
    Content-Range position); ``ranged`` says whether the caller asked for a range (→ 206 +
    Content-Range) or the whole payload (→ 200); ``satisfiable=False`` means the requested range
    starts beyond the blob (→ 416 with ``bytes */size``). ``etag`` is the strong validator for the
    served ``(version, column, row)`` address. ``handle``/``dataset`` are the live pylance refs — the
    lazy ``BlobFile`` :meth:`chunks` reads through, and the dataset that owns it — held so neither is
    collected until the response finishes streaming.

    A ``BaseModel``, not a ``@dataclass`` (CAT-CORE-16): the estate builds its value objects on
    pydantic. ``arbitrary_types_allowed`` because those two refs are opaque pylance objects with no
    schema, and both are ``exclude``d from a dump — this never crosses the wire, the endpoint reads
    the fields directly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    size: int
    start: int
    length: int
    etag: str
    ranged: bool
    satisfiable: bool
    handle: Any = Field(default=None, exclude=True)
    dataset: Any = Field(default=None, exclude=True)

    @property
    def end(self) -> int:
        """Inclusive last byte position of the window (Content-Range form; meaningful when length > 0)."""
        return self.start + self.length - 1

    def chunks(self) -> Iterator[bytes]:
        """Yield the window in ``_BLOB_CHUNK_BYTES`` pieces — each piece one bounded ``read_range``.

        Blocking storage IO: the endpoint hands this to ``StreamingResponse``, which iterates sync
        generators in a threadpool (starlette ``iterate_in_threadpool``), so the event loop never
        blocks and at most one chunk per response is in memory.
        """
        offset, remaining = self.start, self.length
        while remaining > 0:
            step = min(_BLOB_CHUNK_BYTES, remaining)
            yield self.handle.read_range(offset, step)
            offset += step
            remaining -= step


def read_blob(
    ns: LanceNamespace,
    so: StorageOptions,
    table_id: list[str],
    *,
    column: str,
    row: int,
    version: int | None = None,
    range_spec: tuple[int | None, int | None] | None = None,
    if_range: str | None = None,
) -> BlobStream:
    """Resolve one blob payload (or a byte range of it) for the credential-less serving path (§9 P1).

    ``take_blobs`` opens the payload as a lazy ``BlobFile`` and :meth:`BlobStream.chunks` reads it in
    bounded ``read_range`` windows — the catalog never buffers the payload, so a multi-GB video is
    servable without the write-side OOM the body-limit middleware guards against. ``range_spec`` is
    the parsed Range header: ``(first, last)`` with ``last`` inclusive, ``(first, None)`` open-ended,
    ``(None, n)`` an RFC suffix range (the final ``n`` bytes). Resolution happens HERE (not in the
    endpoint) because a suffix range needs the blob's size, which only this dataset-open knows — one
    open serves the whole request. ``if_range`` is the raw ``If-Range`` validator: when it does not
    match this read's etag (``"<version>-<column>-<row>"``), the range is IGNORED and the full
    current payload served (RFC 9110 §13.1.5) — so a client resuming a download across an overwrite
    gets whole consistent bytes, never a silent splice of two incarnations.

    Guards, each a precise client error instead of the raw pylance panic it would otherwise be:
    unknown column → 404 ``TableColumnNotFoundError``; a column that exists but is not blob-typed →
    400 (``take_blobs`` would ValueError); ``row`` past the end → 400 (pylance's message is a
    row-address internals dump); a ``version`` with no manifest → 404 ``TableVersionNotFoundError``
    and a table whose declared location holds NO dataset (declared-only, or wiped storage) → 404
    ``TableNotFoundError`` (both are bare ValueErrors from lance, told apart by the manifest-path
    shape). A ZERO-LENGTH payload streams as an empty 200 — at pylance 8.0.0 a NULL blob is stored
    as a size-0 descriptor (probed: input ``null_count=1`` → stored ``null_count=0``), so null and
    ``b""`` are the same row state and ``take_blobs`` returns an EMPTY list for both (unguarded
    ``[0]`` would 500); any Range against it is unsatisfiable. Range resolution clamps ``last`` to
    the blob size (RFC 9110 §14.1.2) and reports ``first >= size`` as unsatisfiable rather than
    erroring (``read_range`` rejects over-length windows).
    """
    try:
        dataset = open_dataset(ns, so, table_id, version=version)
    except ValueError as exc:
        # lance raises bare ValueErrors for two distinct missing-things: a pinned version with no
        # manifest ("…/_versions/N.manifest was not found…") and a location with no dataset at all
        # ("Dataset at path … was not found…" — a declared-but-never-written table, or wiped
        # storage). Told apart by the manifest-path shape so neither is a 500 and neither is
        # mislabeled as the other; any other ValueError stays a 500.
        message = str(exc).lower()
        if version is not None and "_versions/" in message and ".manifest" in message:
            raise TableVersionNotFoundError(f"table version {version} was not found") from exc
        if "dataset at path" in message and "not found" in message:
            raise TableNotFoundError("table has no readable dataset at its declared location") from exc
        raise
    schema = dataset.schema
    if column not in schema.names:
        raise TableColumnNotFoundError(f"column {column!r} does not exist")
    if not blobs.is_blob_field(schema.field(column)):
        raise InvalidInputError(f"column {column!r} is not a blob column")
    rows = dataset.count_rows()
    if row >= rows:
        raise InvalidInputError(f"row {row} is out of range (table has {rows} rows)")
    etag = f'"{int(dataset.version)}-{column}-{row}"'
    if if_range is not None and if_range.strip() != etag:
        range_spec = None  # validator mismatch (or an If-Range date) → full current payload
    files = dataset.take_blobs(column, indices=[row])
    # `not files` OR a `None` in the slot — the two spellings of "no payload here", one per pylance
    # major. Through 9.0.0 `take_blobs` OMITTED a null row, so the empty-list test below was the whole
    # check; 10.0.0 returns a same-length list with `None` instead (measured 2026-08-16). Without the
    # `files[0] is None` half, serving a null payload reached `None.size()` and answered 500 on a row
    # the endpoint is supposed to serve as an empty body — a user-facing break, on the exact case the
    # zero-length contract exists for.
    if not files or files[0] is None:  # zero-length payload (null collapses to size-0 at write)
        if range_spec is None:
            return BlobStream(size=0, start=0, length=0, etag=etag, ranged=False, satisfiable=True)
        return BlobStream(size=0, start=0, length=0, etag=etag, ranged=True, satisfiable=False)
    handle = files[0]
    size: int = handle.size()

    if range_spec is None:
        return BlobStream(
            size=size,
            start=0,
            length=size,
            etag=etag,
            ranged=False,
            satisfiable=True,
            handle=handle,
            dataset=dataset,
        )
    first, last = range_spec
    if first is None:  # suffix range: the final `last` bytes
        if last is None or last <= 0 or size == 0:
            return BlobStream(size=size, start=0, length=0, etag=etag, ranged=True, satisfiable=False)
        start = max(size - last, 0)
        end = size - 1
    else:
        if first >= size:
            return BlobStream(size=size, start=0, length=0, etag=etag, ranged=True, satisfiable=False)
        start = first
        end = size - 1 if last is None else min(last, size - 1)
    return BlobStream(
        size=size,
        start=start,
        length=end - start + 1,
        etag=etag,
        ranged=True,
        satisfiable=True,
        handle=handle,
        dataset=dataset,
    )
