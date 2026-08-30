"""The staging S3 client is built once, not fresh on every call.

`_client()` called `storage.s3_client(...)` on every invocation — twice per read/purge cycle, and once
per manifest across a run's whole staging life. The endpoint is process-stable and the wrapped client
is thread-safe, so constructing one per call is pure waste (a fresh connection pool each time). The
response bodies read at `read_unit_slice`/`_read_all` were also never closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


class _FakePaginator:
    def paginate(self, **_kw: object) -> list[dict[str, list[dict[str, str]]]]:
        return [{"Contents": []}]


class _FakeClient:
    def __init__(self) -> None:
        self.bodies: list[_FakeBody] = []

    def get_object(self, **_kw: object) -> dict[str, _FakeBody]:
        body = _FakeBody(b'{"units": []}')
        self.bodies.append(body)
        return {"Body": body}

    def delete_object(self, **_kw: object) -> None: ...

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator()


@pytest.fixture
def _clear_client_cache() -> Iterator[None]:
    from ingest import staging

    for name in ("_client", "_client_for"):
        fn = getattr(staging, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()
    yield
    for name in ("_client", "_client_for"):
        fn = getattr(staging, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()


@pytest.mark.usefixtures("_clear_client_cache")
def test_a_read_and_purge_cycle_builds_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import storage
    from ingest import staging

    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", "http://staging-reuse-13:9000")
    built: list[_FakeClient] = []

    def _fake_s3_client(_endpoint: str | None) -> _FakeClient:
        client = _FakeClient()
        built.append(client)
        return client

    monkeypatch.setattr(storage, "s3_client", _fake_s3_client)

    dataset = "s3://bucket/proj/ds.lance"
    staging.read_unit_slice(dataset, "run-13", 0, 10)
    staging.purge_staged(dataset, "run-13")

    assert len(built) == 1, f"the staging client was constructed {len(built)} times across one read+purge cycle"
    # The flow-13 fix's second half: a pooled client makes an unclosed body a LEAK, not a one-off —
    # every response holds a pool connection until GC, so the reuse above would starve its own pool.
    unclosed = [i for i, body in enumerate(built[0].bodies) if not body.closed]
    assert not unclosed, f"response bodies {unclosed} were read but never closed — each pins a pooled connection"
    assert built[0].bodies, "the read path returned no bodies — the close assertion above checked nothing"
