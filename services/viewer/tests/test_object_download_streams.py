"""`GET /api/object/download` must stream, not materialise the object (VS-15).

docs/DECISIONS.md "The Python estate audit" VS-15. The route did `resp["Body"].read()` and handed the bytes to
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
