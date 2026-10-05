"""A store that stops answering costs a catalog read a prompt 503, and a slow one still moves the data ([[LH-247]]).

The real app, the real native backend and real pylance, against a moto server reached through a proxy
the test can black-hole: once the table exists, the proxy keeps accepting connections and forwards
nothing, on new and pooled connections alike, which is a store that has dropped off the network
without closing anything.

MEASURED on pylance 12.0.0 with object_store's defaults (connect 5 s, request 30 s, 3 retries): one
dataset open against a store that drops packets took 124.9 s, and against one that accepts and never
answers it outlasted a 300 s harness. The catalog's own bounds (`CatalogReadBounds`) are set small here
so the deadline is seconds; the deadline is what those bounds promise for the calls one read makes, and
the answer must be the 503 that says the store is down, not a 404 claiming the table is gone or a 500
claiming the catalog broke.

The second case is the other side of the same bounds: a store that answers SLOWLY is not an outage. The
metadata plane's whole-request bound is seconds, and object_store's request bound covers the body, so
under it one 5 MiB upload part at a few MB/s is cut off. A legitimate write and full read through a
proxy capped at 4 MB/s must succeed on the data plane's bounds while the metadata plane stays tight.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import boto3
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


_BUCKET = "black-hole"
_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
_CONNECT_S, _REQUEST_S = 0.5, 1.0
#: Lance re-issues a failed listing six times per open (measured, `objectfs.StoreTimeouts`), and a read
#: makes a handful of store calls before the first fails; this is the most the bounds allow those to cost.
_DEADLINE_S = 20.0
#: A modest store: 4 MB/s each way, shared by every connection, and a 16 MB table of 4 KiB rows.
_RATE_BYTES_S = 4_000_000
_THROTTLED_ROWS, _DIM = 4_000, 1024


class _BlackHoleProxy:
    """A TCP proxy to ``upstream`` that, once black-holed, accepts and holds every connection silently."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self._upstream = upstream
        self._dark = threading.Event()
        self._rate: float | None = None
        self._paced_until = 0.0
        self._pace = threading.Lock()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._held: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def black_hole(self) -> None:
        self._dark.set()

    def throttle(self, bytes_per_second: float) -> None:
        """Cap every byte through the proxy, both ways and across connections, at ``bytes_per_second``."""
        self._rate = bytes_per_second

    def _wait_for_bandwidth(self, size: int) -> None:
        if self._rate is None:
            return
        with self._pace:
            start = max(time.monotonic(), self._paced_until)
            self._paced_until = start + size / self._rate
        time.sleep(max(0.0, start - time.monotonic()))

    def close(self) -> None:
        self._listener.close()
        for held in self._held:
            held.close()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            self._held.append(client)
            if self._dark.is_set():
                continue
            upstream = socket.create_connection(self._upstream)
            self._held.append(upstream)
            threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()

    def _pipe(self, source: socket.socket, sink: socket.socket) -> None:
        while True:
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            if self._dark.is_set():
                continue
            self._wait_for_bandwidth(len(data))
            try:
                sink.sendall(data)
            except OSError:
                return


@pytest.fixture
def proxy(moto_url: str) -> Iterator[_BlackHoleProxy]:
    host, port = moto_url.removeprefix("http://").rsplit(":", 1)
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=_BUCKET)
    black_hole = _BlackHoleProxy((host, int(port)))
    yield black_hole
    black_hole.close()


@pytest.fixture
def client(proxy: _BlackHoleProxy, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{_BUCKET}",
        "LANCE_S3_ENDPOINT": f"http://127.0.0.1:{proxy.port}",
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
        "LANCE_STORE_CONNECT_TIMEOUT_S": str(_CONNECT_S),
        "LANCE_STORE_REQUEST_TIMEOUT_S": str(_REQUEST_S),
        "LANCE_STORE_MAX_RETRIES": "0",
        "LANCE_STORE_RETRY_WINDOW_S": "1",
    }.items():
        monkeypatch.setenv(key, value)

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def test_a_read_against_a_black_holed_store_answers_503_within_the_deadline(client: TestClient, proxy: _BlackHoleProxy) -> None:
    assert client.post("/v1/namespace/bh/create", json={}).status_code == 200
    created = client.post("/v1/table/bh$t/create", content=_ipc(pa.table({"id": pa.array([1, 2, 3], pa.int64())})), headers=_ARROW)
    assert created.status_code == 200, created.text

    proxy.black_hole()
    started = time.monotonic()
    response = client.post("/management/v1/table/bh$t/changes", json={"begin_version": 0, "kind": "inserted"})
    elapsed = time.monotonic() - started

    assert (response.status_code, response.json()["code"]) == (503, 17), response.text
    assert elapsed < _DEADLINE_S, f"the store's silence held the request {elapsed:.1f} s, past the {_DEADLINE_S} s its bounds promise"


def test_a_slow_store_still_completes_a_large_write_and_full_read(client: TestClient, proxy: _BlackHoleProxy) -> None:
    assert client.post("/v1/namespace/slow/create", json={}).status_code == 200
    values = pa.array([0.5] * (_THROTTLED_ROWS * _DIM), pa.float32())
    table = pa.table({"id": pa.array(range(_THROTTLED_ROWS), pa.int64()), "vec": pa.FixedSizeListArray.from_arrays(values, _DIM)})

    proxy.throttle(_RATE_BYTES_S)
    # The same app, answering a store failure as the status a caller would receive rather than re-raising it here.
    caller = TestClient(client.app, raise_server_exceptions=False)
    created = caller.post("/v1/table/slow$t/create", content=_ipc(table), headers=_ARROW)
    feed = caller.post("/management/v1/table/slow$t/changes", json={"begin_version": 0, "kind": "inserted"})

    assert created.status_code == 200, created.text[:300]
    assert feed.status_code == 200, feed.text[:300]
    assert ipc.open_file(pa.py_buffer(feed.content)).read_all().num_rows == _THROTTLED_ROWS
