"""Independent dashboard reads must overlap, and `cluster_status`'s parsing must be reachable alone.

PS-16 — `cluster_status` and `overview` each issue TWO dashboard GETs whose second call reads nothing
from the first, and each awaited them one after the other. Every compute-zone poll therefore paid both
round trips end to end, on pages polled every 5 s. The proof has to be that the two requests are IN
FLIGHT AT THE SAME TIME, not that a total elapsed time looked small — so the fake transport makes each
handler wait for the other to arrive, and a sequential implementation deadlocks itself into the
barrier's timeout.

PS-19 — `cluster_status` was a 78-line body with a try inside a try inside a loop doing five things
(fetch aggregates, fetch nodes, parse node rows, back-fill totals from nodes, de-duplicate GPUs). The
two parsers are pure functions of a dashboard payload and are tested here as such; that is what makes
the fetch body short enough to read.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ray_kit import dashboard


_DASH = "http://ray-head:8265"
#: A sequential implementation waits this long and then fails — long enough that a slow CI box
#: cannot produce a false green, short enough that the RED run is not a hang.
_BARRIER_TIMEOUT = 5.0


class _Barrier:
    """Releases only once `parties` requests are simultaneously inside the transport."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._event.set()
        await asyncio.wait_for(self._event.wait(), timeout=_BARRIER_TIMEOUT)


_CLUSTER_STATUS = {"data": {"clusterStatus": {"loadMetricsReport": {"usage": {"CPU": [2, 8], "GPU": [1, 2], "memory": [100, 400]}}}}}
_NODES = {
    "data": {
        "summary": [
            {
                "raylet": {"nodeId": "n1", "nodeManagerAddress": "10.0.0.1", "state": "ALIVE", "resourcesTotal": {"CPU": 8, "GPU": 2}, "isHeadNode": True},
                "hostname": "head-0",
                "cpu": 12.5,
                "mem": [400, 300, 25, 100],
                "gpus": [],
            }
        ],
        # One resource per LINE — the shape Ray's dashboard actually emits.
        "nodeLogicalResources": {"n1": "2.0/8.0 CPU\n1.0/2.0 GPU"},
    }
}
_VERSION = {"ray_version": "2.58.0", "session_name": "session-1"}
_EVENTS = {"data": {"result": {"result": [{"event_id": "e1", "severity": "INFO", "message": "up", "time": "2026-08-29T00:00:00Z"}]}}}


def _paired_transport(routes: dict[str, dict]) -> httpx.MockTransport:
    barrier = _Barrier(len(routes))

    async def _handler(request: httpx.Request) -> httpx.Response:
        for fragment, payload in routes.items():
            if fragment in request.url.path or fragment in str(request.url):
                await barrier.wait()
                return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected request {request.url}")

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_cluster_status_issues_its_two_independent_reads_at_once() -> None:
    transport = _paired_transport({"/api/cluster_status": _CLUSTER_STATUS, "/nodes": _NODES})
    async with httpx.AsyncClient(transport=transport) as http:
        payload = await dashboard.cluster_status(http, _DASH)
    assert payload.ok is True
    assert payload.node_count == 1
    assert payload.total_resources["CPU"] == 8.0
    assert payload.used_resources["GPU"] == 1.0


@pytest.mark.asyncio
async def test_overview_issues_its_two_independent_reads_at_once() -> None:
    transport = _paired_transport({"/api/version": _VERSION, "/api/v0/cluster_events": _EVENTS})
    async with httpx.AsyncClient(transport=transport) as http:
        payload = await dashboard.overview(http, _DASH)
    assert payload.ok is True
    assert payload.ray_version == "2.58.0"
    assert [e.message for e in payload.events] == ["up"]


# ── the error policy the concurrency must not change ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cluster_status_still_fails_when_the_aggregate_read_fails() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if "cluster_status" in str(request.url) else 200, json=_NODES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        payload = await dashboard.cluster_status(http, _DASH)
    assert payload.ok is False
    assert payload.error and "503" in payload.error


@pytest.mark.asyncio
async def test_cluster_status_still_returns_aggregates_when_the_node_read_fails() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if "/nodes" in str(request.url) else httpx.Response(200, json=_CLUSTER_STATUS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        payload = await dashboard.cluster_status(http, _DASH)
    assert payload.ok is True
    assert payload.node_count == 0
    assert payload.total_resources["CPU"] == 8.0


@pytest.mark.asyncio
async def test_overview_still_returns_the_version_when_the_event_read_fails() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if "cluster_events" in str(request.url) else httpx.Response(200, json=_VERSION)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        payload = await dashboard.overview(http, _DASH)
    assert payload.ok is True
    assert payload.ray_version == "2.58.0"
    assert payload.events == []


# ── PS-19: the parsers, exercised without a transport at all ─────────────────────────────────


def test_usage_totals_reads_the_autoscaler_report() -> None:
    used, total = dashboard._usage_totals(_CLUSTER_STATUS)
    assert total == {"CPU": 8.0, "GPU": 2.0, "memory": 400.0}
    assert used == {"CPU": 2.0, "GPU": 1.0, "memory": 100.0}


def test_usage_totals_tolerates_a_non_autoscaling_cluster() -> None:
    """A non-autoscaling KubeRay reports `clusterStatus: null` — the crash this `or {}` chain fixed."""
    used, total = dashboard._usage_totals({"data": {"clusterStatus": None}})
    assert total == {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}
    assert used == {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}


def test_node_rows_skips_a_row_with_no_node_id() -> None:
    payload = {"data": {"summary": [{"raylet": {}}, *_NODES["data"]["summary"]], "nodeLogicalResources": _NODES["data"]["nodeLogicalResources"]}}
    rows = dashboard._node_rows(payload)
    assert [n.node_id for n in rows] == ["n1"]
    assert rows[0].hostname == "head-0"
    assert rows[0].is_head is True
    assert rows[0].resources_used["CPU"] == 2.0
    assert rows[0].host_mem_used == 100.0
