"""Serving ONE page must not read the whole corpus, and listing metadata must read no bytes at all.

VS-05 — "`/api/page` and `/api/pages` materialize every page blob in the dataset to serve one image
or one page of metadata".

Both routes issued the same unfiltered, unbounded `read_aligned_table(ds, columns=[..., "payload"])`
scan at `blob_handling="all_binary"`, which materialises EVERY row's payload bytes:

  * `/api/page` then picked one payload out of that table by `ids.index(page_id)`. Per-request cost
    is O(total corpus bytes) whichever page is asked for — and the lakehouse zone's `PagePreview`
    strip issues one such request per page, so the aggregate is O(N^2).
  * `/api/pages` applied its `limit` only in the Python loop AFTER the scan, so a route gated on
    `can_get_metadata` had its memory bounded by the DATASET rather than by the request. 10k pages at
    ~1 MB is ~10 GB materialised to return 100 metadata rows — an OOM, not a slow route. The route's
    own docstring already forbade exactly this ("A listing that inlined them would move hundreds of
    megabytes to render a contact sheet"); it was describing the response body while the
    implementation did it server-side.

WHY A REAL DATASET AND NOT A MOCK: the fix rests on two measured pylance 10.0.0 behaviours, and a
double would restate the assumption instead of testing it — a DEFAULT-`blob_handling` scan of a
blob-v2 column returns the descriptor struct `{kind, position, size, blob_id, blob_uri}` and reads no
object bytes, and `take_blobs(..., ids=[rowid])` puts `None` in a null payload's slot. Same idiom as
the sibling `test_media_null_payload.py`, for the same reason.

The instrument is a counting wrapper around the REAL dataset rather than a byte budget guessed from
wall time: what this finding is about is the VOLUME read, and the routes were always returning the
correct bytes. So each assertion pairs "the answer is right" with "the read was bounded" — a fix that
narrowed the read but broke the answer fails here just as loudly as the defect does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance import BlobFile, blob_array, blob_field
from viewer.api.v1.endpoints import pages as pg
from viewer.api.v1.endpoints.pages import router
from viewer.core.config import ViewerSettings, get_viewer_settings

from service_kit.exceptions import register_handlers


TABLE = "bronze$pages"

#: 256 KiB per row across 8 rows = a 2 MiB corpus. Large enough that reading all of it to serve one
#: page is unmistakable in the counter, small enough to stay a unit test.
PAYLOAD_BYTES = 256 * 1024
ROWS_WITH_PAYLOAD = 8
CORPUS_BYTES = PAYLOAD_BYTES * ROWS_WITH_PAYLOAD

#: The row whose payload is null — a registered page whose harvest produced no bytes. Kept in the
#: SAME dataset as the others on purpose: the null-preservation rule this module's cardinality
#: warning exists for only has teeth when a gap sits among real rows.
NULL_ROW_ID = 8

#: Real JPEG magic, so the media-type sniff is exercised on the fixture rather than defaulted. The
#: fix reads only a 12-byte prefix to sniff; a fixture that were not actually a JPEG would be served
#: as `application/octet-stream` and the assertion would be testing the fixture, not the route.
_JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00"


def _payload_for(row: int) -> bytes:
    """A distinct, individually identifiable 256 KiB payload for `row`."""
    body = bytes([row % 251]) * (PAYLOAD_BYTES - len(_JPEG_MAGIC))
    return _JPEG_MAGIC + body


@pytest.fixture(scope="module")
def dataset_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A REAL bronze-shaped blob-v2 dataset: 8 payload rows plus one null.

    `data_storage_version="2.2"` is mandatory (blob v2 refuses anything below it) and
    `enable_stable_row_ids=True` matches how the medallion writes — the `_rowid` the fixed route
    resolves is then the same stable id production would resolve.
    """
    ids = [*range(ROWS_WITH_PAYLOAD), NULL_ROW_ID]
    payloads: list[bytes | None] = [_payload_for(i) for i in range(ROWS_WITH_PAYLOAD)]
    payloads.append(None)
    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("source_uri", pa.string()),
            pa.field("stage", pa.string()),
            blob_field("payload"),
        ]
    )
    table = pa.table(
        {
            "id": ids,
            "source_uri": [f"iiif://vol/{i}" for i in ids],
            "stage": ["bronze"] * len(ids),
            "payload": blob_array(payloads),
        },
        schema=schema,
    )
    path = tmp_path_factory.mktemp("pages") / "pages.lance"
    lance.write_dataset(table, str(path), data_storage_version="2.2", enable_stable_row_ids=True)
    return path


class _CountingScanner:
    """A scanner that reports how many payload BYTES its `to_table()` actually materialised."""

    def __init__(self, inner: Any, counter: _CountingDataset) -> None:
        self._inner = inner
        self._counter = counter

    def to_table(self) -> pa.Table:
        return self._counter.observe(self._inner.to_table())


class _CountingDataset:
    """The real dataset, wrapped, accumulating payload bytes materialised by each read path.

    Delegates by `__getattr__` so `_open`, `schema` and everything else stay on the real object — the
    point is to measure the production code path, not to replace it.
    """

    def __init__(self, real: lance.LanceDataset) -> None:
        self._real = real
        self.payload_bytes = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def observe(self, table: pa.Table) -> pa.Table:
        """Count a scan's payload column, but ONLY when it came back as bytes.

        A default-`blob_handling` scan returns the descriptor struct and reads nothing from the
        object store; `blob_handling="all_binary"` returns `large_binary` and reads everything. That
        type distinction IS the finding, so it is what the counter keys on.
        """
        if "payload" in table.schema.names:
            field_type = table.schema.field("payload").type
            if pa.types.is_binary(field_type) or pa.types.is_large_binary(field_type):
                self.payload_bytes += sum(len(v) for v in table.column("payload").to_pylist() if v)
        return table

    def scanner(self, **kwargs: Any) -> _CountingScanner:
        return _CountingScanner(self._real.scanner(**kwargs), self)

    def to_table(self, **kwargs: Any) -> pa.Table:
        return self.observe(self._real.to_table(**kwargs))

    def take_blobs(self, column: str, **kwargs: Any) -> list[Any]:
        handles = self._real.take_blobs(column, **kwargs)
        self.payload_bytes += sum(h.size() for h in handles if h is not None)
        return handles


def _catalog_ok() -> Any:
    """`httpx.Client` stub so `_resolve` reaches a catalog that answers 200 with a location."""

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"location": "s3://bkt/tbl.lance"}

    class _Client:
        def __init__(self, **_kw: Any) -> None: ...
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None: ...
        def post(self, _path: str, **_kw: Any) -> _Resp:
            return _Resp()

    return _Client


def _app() -> FastAPI:
    """The pages router with the authorization plane satisfied — this file measures reads, not gates.

    The gate itself is pinned by `tests/unit/test_viewer_page_authz.py`; granting here keeps a
    permission failure from masquerading as a bounded read.
    """

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    settings = ViewerSettings.model_validate(
        {
            "LANCE_FGA_ENABLED": True,
            "LANCE_OIDC_ENABLED": True,
            "LANCE_OIDC_ISSUER": "https://issuer.test",
            "LANCE_OIDC_AUDIENCE": "rask",
        }
    )
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    app.dependency_overrides[pg.CheckerDep.__metadata__[0].dependency] = lambda: checker
    app.dependency_overrides[pg.CurrentSubject.__metadata__[0].dependency] = lambda: "gina"
    app.dependency_overrides[pg.RawBearerToken.__metadata__[0].dependency] = lambda: "caller-jwt"
    # `storage_options` is a METHOD on the real settings (it performs a blocking Dapr secret fetch),
    # so the double has to be one too or it tests a shape production does not have.
    state = type("_State", (), {"settings": type("S", (), {"catalog_uri": "http://catalog", "storage_options": lambda _self: {}})()})()
    app.dependency_overrides[pg.StateDep.__metadata__[0].dependency] = lambda: state
    return app


@pytest.fixture
def counted(dataset_path: Path, monkeypatch: pytest.MonkeyPatch) -> _CountingDataset:
    """Route `_open` at the counting wrapper, leaving `_resolve` and `_open` themselves real."""
    counter = _CountingDataset(lance.dataset(str(dataset_path)))
    monkeypatch.setattr(pg.httpx, "Client", _catalog_ok())
    monkeypatch.setattr(pg.lance, "dataset", lambda *_a, **_kw: counter)
    return counter


def test_serving_one_page_does_not_read_the_whole_corpus(counted: _CountingDataset) -> None:
    """One page's bytes must cost one page's bytes, not the volume's.

    The strip in `lakehouse/src/lib/storage/PagePreview.svelte` renders one `<img src=/api/page…>`
    per listed page, so an O(corpus) single-page read is realised as O(N^2) egress by a real client.
    """
    client = TestClient(_app())

    r = client.get("/api/page", params={"table": TABLE, "id": 3})

    assert r.status_code == 200
    assert r.content == _payload_for(3), "the narrowed read must still return the page that was asked for"
    assert r.headers["content-type"] == "image/jpeg", "the media type is still sniffed from the payload's own magic bytes"
    budget = 2 * PAYLOAD_BYTES
    assert counted.payload_bytes <= budget, (
        f"served one {PAYLOAD_BYTES}-byte page but read {counted.payload_bytes} bytes "
        f"({counted.payload_bytes / PAYLOAD_BYTES:.1f} pages) out of a {CORPUS_BYTES}-byte corpus"
    )


def test_the_listing_reads_no_payload_bytes_at_all(counted: _CountingDataset) -> None:
    """Metadata is answerable from the blob DESCRIPTORS, so a `can_get_metadata` route reads zero bytes.

    `== 0` rather than a budget: a default-`blob_handling` scan returns
    `struct<kind, position, size, blob_id, blob_uri>` and touches no object bytes, so both `size` and
    `has_payload` are answerable without a single payload read. Anything above zero means the scan
    went back to `all_binary` and the OOM is back with it.
    """
    client = TestClient(_app())

    r = client.get("/api/pages", params={"table": TABLE, "limit": 2})

    assert r.status_code == 200
    pages = r.json()["pages"]
    assert [p["id"] for p in pages] == [0, 1], "the limit must select rows in dataset order, as it did before"
    assert [p["size"] for p in pages] == [PAYLOAD_BYTES, PAYLOAD_BYTES], "descriptor `size` must still report the real payload length"
    assert all(p["has_payload"] for p in pages)
    assert counted.payload_bytes == 0, (
        f"listing 2 rows of metadata read {counted.payload_bytes} payload bytes out of a {CORPUS_BYTES}-byte corpus; "
        "the limit is applied after the scan instead of inside it"
    )


def test_the_listing_still_reports_a_null_payload_row(counted: _CountingDataset) -> None:
    """A row that failed to acquire is a real state of the dataset and must stay visible.

    Descriptor validity is the null signal at pylance >= 9; reading it wrong would make the listing
    report a corpus as complete when it is not — which is the exact thing `has_payload` exists to
    prevent.
    """
    client = TestClient(_app())

    r = client.get("/api/pages", params={"table": TABLE, "limit": 500})

    pages = {p["id"]: p for p in r.json()["pages"]}
    assert pages[NULL_ROW_ID]["has_payload"] is False
    assert pages[NULL_ROW_ID]["size"] == 0
    assert pages[0]["has_payload"] is True


def test_a_null_payload_still_404s_after_the_read_is_narrowed(counted: _CountingDataset) -> None:
    """The regression guard for the behaviour the new `take_blobs` path now depends on.

    `read_blobs`/`take_blobs` DROP null rows through pylance 9 and return `None` in the slot from
    10.0.0 — the landmine this module's docstring is about. Resolving `id` -> stable `_rowid` ->
    `take_blobs(ids=[rowid])` never pairs anything positionally, so `None` IS the null signal and the
    404 is decided at the ROUTE, before any response object exists.
    """
    client = TestClient(_app())

    r = client.get("/api/page", params={"table": TABLE, "id": NULL_ROW_ID})

    assert r.status_code == 404
    assert "has no payload" in r.text


@pytest.fixture(scope="module")
def big_page_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One row whose payload sits above the 1 MiB buffering threshold."""
    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("source_uri", pa.string()),
            pa.field("stage", pa.string()),
            blob_field("payload"),
        ]
    )
    body = _JPEG_MAGIC + b"\x5a" * (2 * 1024 * 1024)
    table = pa.table(
        {"id": [0], "source_uri": ["iiif://big/0"], "stage": ["bronze"], "payload": blob_array([body])},
        schema=schema,
    )
    path = tmp_path_factory.mktemp("bigpage") / "pages.lance"
    lance.write_dataset(table, str(path), data_storage_version="2.2", enable_stable_row_ids=True)
    return path


def test_a_payload_over_the_threshold_is_streamed_not_buffered(big_page_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`payload` is opaque and `table` is a free query parameter, so the response size is whatever
    the DATA names — an unbounded `Response(content=...)` would hand that number straight to memory.

    The exact `Content-Length` is part of the contract: it comes from the same handle the generator
    reads, so declaring it gives the browser a progress bar instead of a chunked stream of unknown
    length. Without this test the streaming branch is never entered by the suite at all.
    """
    real = lance.dataset(str(big_page_path))
    monkeypatch.setattr(pg.httpx, "Client", _catalog_ok())
    monkeypatch.setattr(pg.lance, "dataset", lambda *_a, **_kw: real)
    handle = real.take_blobs("payload", ids=[0])[0]
    assert handle is not None
    expected = handle.read()

    # SPY ON THE STREAMING SEAM, because every body-shaped assertion below is satisfied by a
    # buffered `Response(content=...)` too — the verifier measured exactly that against the unfixed
    # code, where this test passed. What discriminates is WHICH branch built the response.
    streamed: list[int] = []
    original = pg._stream_handle

    def _spied(blob: BlobFile, size: int) -> object:
        streamed.append(size)
        return original(blob, size)

    monkeypatch.setattr(pg, "_stream_handle", _spied)

    r = TestClient(_app()).get("/api/page", params={"table": TABLE, "id": 0})

    assert streamed == [len(expected)], (
        "the over-threshold payload was BUFFERED — the streaming branch never ran, and the response held the whole blob in memory"
    )
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(expected)
    assert r.content == expected
    assert r.headers["content-type"] == "image/jpeg", "the sniff reads a 12-byte prefix, so it must survive the rewind before streaming"


def test_an_absent_id_is_still_a_404(counted: _CountingDataset) -> None:
    """A filter that matches nothing must 404 rather than serve row 0 — the mis-selection this
    module's "never by row position" promise exists to rule out."""
    client = TestClient(_app())

    r = client.get("/api/page", params={"table": TABLE, "id": 999})

    assert r.status_code == 404
    assert "no page with id 999" in r.text
