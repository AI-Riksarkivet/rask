"""A lakehouse app reports how much of its request thread pool is in use.

Every blocking call a door makes runs on the serving loop's bounded thread pool, so a pool with every thread held
queues requests with no error anywhere. `build_service_app` reports the pool of the loop it serves on
(`lakehouse.service_app.thread_pool`); the gauges are read through an OpenTelemetry reader, as the Collector reads them.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from lakehouse.process.lifecycle import mark_started
from lakehouse.schemas.health import Readiness, ReadinessStatus
from lakehouse.service_app import thread_pool
from lakehouse.service_app.factory import build_service_app


def _exported(reader: InMemoryMetricReader) -> dict[str, float]:
    data = reader.get_metrics_data()
    return {
        metric.name: float(point.value)
        for resource in (data.resource_metrics if data is not None else [])
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    }


async def _ready(_request: Request) -> Readiness:
    return Readiness(status=ReadinessStatus.ready)


@pytest.mark.parametrize(
    ("request_threads", "limit"),
    [pytest.param(None, 40.0, id="the-pools-own-bound"), pytest.param(3, 3.0, id="the-bound-the-service-is-given")],
)
def test_a_blocking_door_is_counted_as_a_busy_thread_while_it_runs(request_threads: int | None, limit: float, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = InMemoryMetricReader()
    monkeypatch.setattr(thread_pool, "_meter", MeterProvider(metric_readers=[reader]).get_meter("lance.app"))
    monkeypatch.setattr(thread_pool, "_registered", False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        mark_started(app)
        yield

    app = build_service_app(
        title="t",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=lifespan,
        log=logging.getLogger("t"),
        ready_check=_ready,
        max_body_bytes=1024,
        request_deadline_seconds=30,
        request_threads=request_threads,
    )

    @app.get("/blocking")
    def blocking() -> dict[str, float]:
        """A blocking door, run on a thread of the pool, answering what the pool reports while it holds that thread."""
        return _exported(reader)

    with TestClient(app) as client:
        during = client.get("/blocking").json()
        after = _exported(reader)

    assert during == {"threadpool.busy": 1.0, "threadpool.limit": limit, "threadpool.waiting": 0.0}
    assert after == {"threadpool.busy": 0.0, "threadpool.limit": limit, "threadpool.waiting": 0.0}
