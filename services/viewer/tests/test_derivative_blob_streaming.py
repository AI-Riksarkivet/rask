"""A derivative blob must not be read whole into memory just because it is usually small.

open_fastapi-audit — "`/api/thumbnail` and `/api/chunk-frame` read a descriptor-declared blob whole
into memory with no ceiling and no `Accept-Ranges`, while their sibling on the same table streams".

The audit ADJUSTS its own finding twice, and both adjustments are honoured here.

**The Accept-Ranges half is dropped entirely.** These two routes serve small cached derivatives with
`public, max-age=86400`; no client range-requests a thumbnail, and the contrast with `/api/media`
(a full-length audio or video payload a player seeks through) is not apples-to-apples.

**What is left is hardening, not a defect at HEAD.** No descriptor in this repo binds `thumbnail` or
the frames blob to a full-resolution derivative, so nothing currently fails. The point is that the
ceiling was *the descriptor's promise* rather than anything this service enforced: both columns are
resolved from `declared.document` / the frames binding at request time, so what those routes read is
decided by data, and `f.read()` with no argument will hand back whatever that data names.

file-handling.md's response-type table draws the line at 1 MiB — `Response(content=bytes)` is for a
"tiny file (< 1 MB), already in memory", and anything larger is a `StreamingResponse`. So the fix is
the threshold, not a refusal: a small derivative keeps its buffered single-shot response (cheap, and
`Content-Length` comes free), and a large one streams in bounded chunks. Refusing with a 413 was the
audit's alternative and is the worse of the two — 413 is defined against the REQUEST body, and it
would answer a legitimately-large derivative with an error rather than the bytes.

The property under test is the one that matters: no single `read()` on the handle is unbounded.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

import lance
import pyarrow as pa
import pytest
from fastapi.responses import StreamingResponse
from lance import blob_array, blob_field
from lance.blob import BlobFile
from viewer.api.v1.endpoints import media as media_ep

from service_kit.exceptions import NotFoundError


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"


def _handle(tmp_path: Path, payload: bytes) -> BlobFile:
    """A REAL pylance blob handle, because the defect is about what `read()` does to one.

    Same shape as `test_media_null_payload`'s fixture: blob v2 refuses a file version below 2.2, and
    stable row ids match how the medallion writes, so the `_rowid` taken here is the one a route
    would resolve.
    """
    schema = pa.schema([pa.field("doc_id", pa.string()), blob_field("thumb")])
    table = pa.table({"doc_id": ["doc-1"], "thumb": blob_array([payload])}, schema=schema)
    ds = lance.write_dataset(
        table,
        str(tmp_path / "docs.lance"),
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return cast("BlobFile", ds.take_blobs("thumb", ids=[0])[0])


class _Recording:
    """Wraps a blob handle to record the LENGTH ARGUMENT of every read, which is the whole property.

    `f.read()` with no argument is the defect — it materialises whatever the descriptor named. A spy
    on the argument distinguishes that from `f.read(n)` in a way that reading the returned bytes
    cannot: a 3 MiB payload read unbounded and a 3 MiB payload read in 1 MiB chunks produce the same
    body, and only one of them holds 3 MiB in a single buffer.
    """

    def __init__(self, inner: BlobFile) -> None:
        self._inner = inner
        self.reads: list[int] = []

    def size(self) -> int:
        return self._inner.size()

    def read(self, n: int = -1) -> bytes:
        self.reads.append(n)
        return self._inner.read(n)

    def __enter__(self) -> _Recording:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.close()


def test_a_small_derivative_is_still_served_buffered(tmp_path: Path) -> None:
    """The common case must not pay for the fix: one read, one `Content-Length`, no chunking."""
    handle = _Recording(_handle(tmp_path, JPEG))
    response = media_ep.blob_response(cast("BlobFile", handle), mime="image/jpeg", empty_detail="no thumbnail for doc_id")

    assert response.body == JPEG
    assert response.media_type == "image/jpeg"
    assert response.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.asyncio
async def test_a_large_derivative_is_never_read_whole_into_memory(tmp_path: Path) -> None:
    """The finding itself: the ceiling was the descriptor's promise, not the service's decision."""
    payload = JPEG + b"x" * (3 << 20)
    handle = _Recording(_handle(tmp_path, payload))
    response = media_ep.blob_response(cast("BlobFile", handle), mime="image/jpeg", empty_detail="no thumbnail for doc_id")

    assert isinstance(response, StreamingResponse), "a 3 MiB derivative was still buffered whole into one Response body"
    chunks = [cast("bytes", chunk) async for chunk in response.body_iterator]
    assert b"".join(chunks) == payload, "streaming must not change the bytes the caller receives"
    assert handle.reads, "the handle was never read through"
    assert all(0 < n <= media_ep._STREAM_CHUNK for n in handle.reads), (
        f"an unbounded read reached a {len(payload)}-byte derivative: read sizes {handle.reads} — "
        "`f.read()` with no argument materialises whatever the descriptor named"
    )
    assert response.headers["content-length"] == str(len(payload)), "the size is known exactly from the probe, so the caller should not be left guessing"


def test_the_threshold_matches_the_reference() -> None:
    """file-handling.md's table puts `Response(content=bytes)` at 'tiny file (< 1 MB)'."""
    assert media_ep.MAX_BUFFERED_BLOB_BYTES <= 1 << 20


def test_an_empty_derivative_is_still_a_404(tmp_path: Path) -> None:
    """The absence answer both routes already give must survive the restructure."""
    handle = _Recording(_handle(tmp_path, b""))
    with pytest.raises(NotFoundError):
        media_ep.blob_response(cast("BlobFile", handle), mime="image/jpeg", empty_detail="no thumbnail for doc_id")


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(Path(media_ep.__file__).read_text())
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.mark.parametrize("route", ["thumbnail", "chunk_frame"])
def test_neither_route_still_reads_a_blob_unbounded(route: str) -> None:
    """Parsed, not grepped: a source scan would match the comments that EXPLAIN the fix."""
    fn = _function(route)
    unbounded = [
        node for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read" and not node.args
    ]
    assert not unbounded, f"`{route}` still calls read() with no length bound at line {unbounded[0].lineno}"
