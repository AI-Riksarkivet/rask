"""The catalog refuses a request thread pool its own writers could hold whole.

An admitted bulk write holds one request thread while it commits and a door's commit waiting for its ref's turn sleeps in
another, so the cap on the first and the seats of the second together stay below the pool the catalog is given, and a
read always finds a thread ([[XC-204]]). A pool the catalog is not given a bound for is anyio's own, which it does not judge.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from catalog.core.config import Settings
from lakehouse.records.commit_turns import THREAD_WAITERS


@pytest.mark.parametrize(
    ("threads", "writes"),
    [
        pytest.param("16", str(16 - THREAD_WAITERS - 1), id="writers-one-thread-short-of-the-pool"),
        pytest.param(None, "0", id="a-pool-the-catalog-is-given-no-bound-for"),
    ],
)
def test_a_pool_that_keeps_a_thread_from_its_writers_is_taken(threads: str | None, writes: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANCE_MAX_CONCURRENT_WRITES", writes)
    if threads is not None:
        monkeypatch.setenv("LANCE_REQUEST_THREADS", threads)

    settings = Settings()

    assert (settings.request_threads, settings.max_concurrent_writes) == (int(threads) if threads else None, int(writes))


@pytest.mark.parametrize(
    ("writes", "refusal"),
    [
        pytest.param(str(16 - THREAD_WAITERS), "must stay below LANCE_REQUEST_THREADS", id="writers-reach-the-pool"),
        pytest.param("0", "admits every write at once", id="no-cap-on-writes"),
    ],
)
def test_a_pool_its_writers_could_hold_whole_is_refused(writes: str, refusal: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANCE_REQUEST_THREADS", "16")
    monkeypatch.setenv("LANCE_MAX_CONCURRENT_WRITES", writes)

    with pytest.raises(ValidationError, match=refusal):
        Settings()
