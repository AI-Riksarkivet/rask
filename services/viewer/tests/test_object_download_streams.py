"""`GET /api/object/download` must stream, not materialise the object (VS-15).

open_python-audit VS-15. The route did `resp["Body"].read()` and handed the bytes to
`Response(content=...)`: the whole object lived in the process before a single byte was sent, with
no cap. The docstring defended it — "the two rask buckets hold page images (~MBs) and ALTO XML
(small)" — but that premise was deleted when the bucket list became configuration (`LANCE_STORES`,
`DEFAULT_STORES`): a deployment registering the warehouse or the observability store exposes
multi-GB objects to this route, and concurrent requests each hold a full copy. The stale
justification is what made it invisible.

Pinned here: the response streams (bounded memory per request, first byte out before the last byte
is read), the payload still arrives whole and in order, and the not-found split the route already
had still happens BEFORE any streaming starts — a 404 must not become a 200 with an error body.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest
from starlette.responses import StreamingResponse
from viewer.api.v1.endpoints import objects as objects_ep


if TYPE_CHECKING:
    from viewer.core.config import ViewerSettings

#: Four chunks of a "large" object — enough that buffering vs streaming is observable.
_CHUNKS = [b"a" * 8, b"b" * 8, b"c" * 8, b"d" * 8]


class _Settings:
    """Only what `_require_browse` reads."""

    fga_root_object = "system:rask"


class _Body:
    """A botocore StreamingBody stand-in that records how it was consumed."""

    def __init__(self) -> None:
        self.read_whole = False
        self.chunks_yielded = 0

    def read(self, amt: int | None = None) -> bytes:
        self.read_whole = True
        return b"".join(_CHUNKS)

    def iter_chunks(self, chunk_size: int = 1024) -> object:
        def _gen():
            for chunk in _CHUNKS:
                self.chunks_yielded += 1
                yield chunk

        return _gen()

    def close(self) -> None:
        return None


class _Client:
    def __init__(self, body: _Body) -> None:
        self._body = body

    def get_object(self, **_kw: object) -> dict[str, object]:
        return {"ContentType": "image/jpeg", "ContentLength": sum(len(c) for c in _CHUNKS), "Body": self._body}


def _download(monkeypatch: pytest.MonkeyPatch) -> tuple[StreamingResponse, _Body]:
    body = _Body()
    monkeypatch.setattr(objects_ep, "_client_for", lambda _b: _Client(body))
    monkeypatch.setattr(objects_ep, "_registered_bucket", lambda b: b)

    async def _allow(**_kw: object) -> bool:
        return True

    resp = asyncio.run(
        objects_ep.download_object(
            checker=_allow,
            subject="gina",
            settings=cast("ViewerSettings", _Settings()),
            bucket="warehouse",
            key="dir/big.tif",
        )
    )
    return resp, body


def test_the_object_is_not_materialised_before_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    resp, body = _download(monkeypatch)
    assert not body.read_whole, "the whole object was read into memory before a byte was sent — a multi-GB object in a registered store is an OOM on this route"
    assert isinstance(resp, StreamingResponse), f"the route answered with {type(resp).__name__}, which carries a fully-buffered body"
    assert body.chunks_yielded == 0, f"{body.chunks_yielded} chunks were already pulled before the response was returned"


def test_the_whole_payload_still_arrives_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming must not truncate or reorder: this is the same bytes, later."""
    resp, _ = _download(monkeypatch)

    async def _drain() -> bytes:
        # `body_iterator` is typed as str-or-bytes (starlette allows a text stream); this route's
        # generator yields S3 chunks, so the bytes branch is the only one reachable.
        chunks: list[bytes] = [chunk async for chunk in resp.body_iterator if isinstance(chunk, bytes)]
        return b"".join(chunks)

    assert asyncio.run(_drain()) == b"".join(_CHUNKS)


def test_the_content_length_is_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size is known from the GET response, so the browser still gets a progress bar."""
    resp, _ = _download(monkeypatch)
    assert resp.headers["content-length"] == str(sum(len(c) for c in _CHUNKS))


def test_a_missing_object_is_still_a_404_before_any_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absence split must stay ahead of the stream — a 404 cannot be delivered mid-body."""
    from service_kit.exceptions import NotFoundError
    from storage import ObjectNotFoundError

    class _Missing:
        def get_object(self, **_kw: object) -> dict[str, object]:
            raise ObjectNotFoundError(bucket="warehouse", key="dir/gone.tif")

        def head_bucket(self, **_kw: object) -> dict[str, object]:
            return {}

    monkeypatch.setattr(objects_ep, "_client_for", lambda _b: _Missing())
    monkeypatch.setattr(objects_ep, "_registered_bucket", lambda b: b)

    async def _allow(**_kw: object) -> bool:
        return True

    with pytest.raises(NotFoundError):
        asyncio.run(
            objects_ep.download_object(
                checker=_allow,
                subject="gina",
                settings=cast("ViewerSettings", _Settings()),
                bucket="warehouse",
                key="dir/gone.tif",
            )
        )
