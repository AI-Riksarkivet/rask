"""The catalog's `/readyz` answers within the probe's timeout with every worker thread held (XC-201).

A sync door runs on the threadpool FastAPI shares (anyio's default limiter, 40 threads), so a burst of slow doors holds
every thread at once. Readiness asks nothing on that pool: the health watch reads the store, OpenFGA and the bus in the
background, off it (`lakehouse.process.health`), and `/readyz` answers what the watch last found. A readiness read queued on the
pool would wait behind the burst past the kubelet's timeout, on every replica the burst reaches, and take them all out of
the Service together. The threads are held as such a burst holds them, by borrowing every token of the pool's limiter on
the app's own event loop.
"""

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio.to_thread
import pytest
from fastapi.testclient import TestClient

from catalog.core.config import get_settings
from lakehouse.storage.objectfs import store_dependency


#: The kubelet's default `timeoutSeconds`: the chart's readiness probe sets none (`lance.appProbes`).
PROBE_SECONDS = 1.0


def _until_ready(client: TestClient, *, within: float) -> int:
    deadline = time.monotonic() + within
    while (status := client.get("/readyz").status_code) != 200 and time.monotonic() < deadline:
        time.sleep(0.05)
    return status


class _Burst:
    """A ready catalog with every thread of its request pool held, and the one way a test asks it something."""

    def __init__(self, client: TestClient, asking: ThreadPoolExecutor, *, held: int) -> None:
        self.client = client
        self._asking = asking
        self._held = held

    def answer_within_the_probe_timeout[T](self, call: Callable[[], T], *, what: str) -> T:
        """``call``'s answer, asked from a thread of the test's own; the test fails when none comes within the probe's timeout."""
        try:
            return self._asking.submit(call).result(timeout=PROBE_SECONDS)
        except TimeoutError:
            pytest.fail(f"{what} did not answer within {PROBE_SECONDS} s with the {self._held} worker threads held")


@pytest.fixture
def burst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Burst]:
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        assert _until_ready(client, within=10.0) == 200, "precondition: the catalog never became ready"
        portal = client.portal
        assert portal is not None, "the client runs the app's event loop only inside its `with`"
        limiter = portal.call(anyio.to_thread.current_default_thread_limiter)
        borrowers = [object() for _ in range(int(limiter.total_tokens))]
        for borrower in borrowers:
            portal.call(limiter.acquire_on_behalf_of, borrower)
        asking = ThreadPoolExecutor(max_workers=1)
        try:
            yield _Burst(client, asking, held=len(borrowers))
        finally:
            # The threads go back before the asking thread is joined: a question still waiting for one ends then.
            for borrower in borrowers:
                portal.call(limiter.release_on_behalf_of, borrower)
            asking.shutdown(wait=True)
    get_settings.cache_clear()


def test_readiness_answers_within_the_probe_timeout_with_every_worker_thread_held(burst: _Burst) -> None:
    answered = burst.answer_within_the_probe_timeout(lambda: burst.client.get("/readyz"), what="/readyz")

    assert answered.status_code == 200, answered.text


def test_a_call_on_the_process_own_threads_answers_with_every_worker_thread_held(burst: _Burst) -> None:
    """What the process runs on its own threads (`lakehouse.process.own_threads`) never waits behind the burst: the health
    watch's read of the store, taken as the watch takes it, on the app's event loop."""
    settings = get_settings()
    store = store_dependency(settings.registry_root, settings.storage_options, required=True)
    portal = burst.client.portal
    assert portal is not None, "the client runs the app's event loop only inside its `with`"

    burst.answer_within_the_probe_timeout(lambda: portal.call(store.read), what="the health watch's read of the store")
