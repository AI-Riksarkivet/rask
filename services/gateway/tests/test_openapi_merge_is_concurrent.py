"""The merged-openapi fan-out must be concurrent, not a serial per-target wait.

`GW-OPENAPI-SEQUENTIAL`. `_merged_openapi` fetched each backend's spec in a plain `for` loop with a
per-target timeout and no overall deadline, so a slow (or dead-but-hanging) upstream added its whole
timeout to the wall-clock of every request that reached the docs endpoint — worst case N times the
per-target budget. Fetching them together makes the merge cost max(one target), not the sum.
"""

from __future__ import annotations

import asyncio
import importlib
import time

import httpx
import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    import gateway

    return importlib.reload(gateway)


class _SlowClient:
    """Every `.get` sleeps `delay` before answering, standing in for N slow upstreams."""

    def __init__(self, delay: float, spec: dict) -> None:
        self._delay = delay
        self._spec = spec

    async def get(self, url: str, timeout: float | None = None) -> httpx.Response:
        await asyncio.sleep(self._delay)
        return httpx.Response(200, json=self._spec, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_merge_latency_is_max_not_sum(gw) -> None:
    delay = 0.2
    n = 5
    targets = [(f"svc{i}", f"http://127.0.0.1:90{i:02d}") for i in range(n)]
    spec = {"openapi": "3.1.0", "paths": {}, "components": {"schemas": {}}}

    started = time.perf_counter()
    await gw._merged_openapi(_SlowClient(delay, spec), "/api", targets)
    elapsed = time.perf_counter() - started

    # Concurrent: ~max(delay) ≈ 0.2s. Serial (today): ~n*delay ≈ 1.0s. Half the serial floor is a
    # margin no serial loop can hit and no concurrent one can miss.
    assert elapsed < (n * delay) / 2, f"merge took {elapsed:.3f}s — the fan-out is still serial"
