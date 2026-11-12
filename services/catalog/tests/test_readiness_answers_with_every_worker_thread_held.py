"""The catalog's `/readyz` answers within the probe's timeout with every worker thread held (XC-201).

A sync door runs on the threadpool FastAPI shares (anyio's default limiter, 40 threads), so a burst of slow doors holds
every thread at once. Readiness asks nothing on that pool: the health watch reads the store, OpenFGA and the bus in the
background, off it (`lakehouse.health`), and `/readyz` answers what the watch last found. A readiness read queued on the
pool would wait behind the burst past the kubelet's timeout, on every replica the burst reaches, and take them all out of
the Service together. The threads are held as such a burst holds them, by borrowing every token of the pool's limiter on
the app's own event loop.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio.to_thread
import httpx
import pytest
from fastapi.testclient import TestClient

from catalog.core.config import get_settings


#: The kubelet's default `timeoutSeconds`: the chart's readiness probe sets none (`lance.appProbes`).
PROBE_SECONDS = 1.0


def _until_ready(client: TestClient, *, within: float) -> int:
    deadline = time.monotonic() + within
    while (status := client.get("/readyz").status_code) != 200 and time.monotonic() < deadline:
        time.sleep(0.05)
    return status


def test_readiness_answers_within_the_probe_timeout_with_every_worker_thread_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from catalog.main import app

    answered: httpx.Response | None = None
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
            probe = asking.submit(client.get, "/readyz")
            try:
                answered = probe.result(timeout=PROBE_SECONDS)
            except TimeoutError:
                answered = None
        finally:
            for borrower in borrowers:
                portal.call(limiter.release_on_behalf_of, borrower)
            asking.shutdown(wait=True)
    get_settings.cache_clear()

    assert answered is not None, f"/readyz did not answer within {PROBE_SECONDS} s with the {len(borrowers)} worker threads held"
    assert answered.status_code == 200, answered.text
