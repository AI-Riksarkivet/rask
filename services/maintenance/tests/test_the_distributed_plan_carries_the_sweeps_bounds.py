"""The sweep's off-pod rewrite plans under the sweep's OWN memory bounds, and the tasks carry them.

The catalog refuses a compaction plan that does not state `batch_size` and `num_threads`: Lance bakes
both into every task at plan time and `CompactionTask.execute` takes only the dataset, so the plan is
the executor's one chance to bound its read. That makes this executor responsible for sending them,
and the numbers it sends must be the estate's configured ones (`MAINTENANCE_SCAN_BATCH_SIZE`,
`MAINTENANCE_COMPACT_THREADS`) — the same bounds the in-pod rewrite runs under.

A dropped bound would not fail loudly. `plan_via_catalog` reads the door's 400 as an unavailable plane,
and the caller answers that by compacting in-pod, so every dataset would quietly fall back and the
off-pod path would stop running with `compaction_mode` the only witness. The mode is asserted here for
that reason.

Driven from `_resolve_plan` through `_maintain_one`, so every hop between the settings and the request
body is the production one. Only the transport is replaced: respx hands each request to the CATALOG'S
OWN APP over a real `dir` namespace, so the door that turns the body into baked tasks is production
code too, and the options read back out of those tasks are what a worker would execute.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import respx
from fastapi.testclient import TestClient
from lance_namespace import connect

from maintenance.core.config import DEFAULT_COMPACT_THREADS, DEFAULT_SCAN_BATCH_SIZE, MaintenanceSettings
from maintenance.services import catalog_compaction, sweep
from service_kit.lakehouse.base_refs import BaseRefs


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
#: Neither is a default, so a hop that forgot the setting and fell back to the floor cannot pass.
SCAN_BATCH_SIZE = 37
COMPACT_THREADS = 3


def _arrow(rows: range) -> bytes:
    table = pa.table({"id": pa.array(rows, pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The catalog app over a real pylance `dir` namespace, the shape `tests/integration`'s `real_ns_client` builds."""
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")
    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.core.config import get_settings
    from catalog.main import app

    get_settings.cache_clear()
    ns = connect("dir", {"root": str(tmp_path)})
    app.dependency_overrides[get_namespace] = lambda: ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _fragmented(catalog: TestClient, *, writes: int = 6, rows: int = 10) -> str:
    """`TABLE_ID`, created through the catalog and split across `writes` fragments; its location."""
    assert catalog.post("/v1/namespace/acme-bronze/create", json={}).status_code == 200
    created = catalog.post(f"/v1/table/{TABLE_ID}/create", content=_arrow(range(rows)), headers={"content-type": "application/vnd.apache.arrow.stream"})
    assert created.status_code == 200, created.text
    uri = str(catalog.post(f"/v1/table/{TABLE_ID}/describe", json={}).json()["location"])
    for i in range(1, writes):
        lance.write_dataset(pa.table({"id": pa.array(range(i * rows, (i + 1) * rows), pa.int64())}), uri, mode="append")
    assert len(lance.dataset(uri).get_fragments()) == writes
    return uri


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings.model_validate(
        {
            "s3_bucket": "lake",
            "distributed_compaction": True,
            "catalog_url": CATALOG,
            "scan_batch_size": SCAN_BATCH_SIZE,
            "compact_threads": COMPACT_THREADS,
        }
    )


class _Wire:
    """Hands each intercepted request to the catalog app and keeps what crossed, both ways."""

    def __init__(self, catalog: TestClient) -> None:
        self.catalog = catalog
        self.plan_bodies: list[dict[str, Any]] = []
        self.planned_tasks: list[str] = []

    def plan(self, request: httpx.Request) -> httpx.Response:
        self.plan_bodies.append(json.loads(request.content))
        answered = self._forward(request)
        if answered.status_code == 200:
            self.planned_tasks.extend(answered.json()["tasks"])
        return answered

    def commit(self, request: httpx.Request) -> httpx.Response:
        return self._forward(request)

    def _forward(self, request: httpx.Request) -> httpx.Response:
        answered = self.catalog.post(request.url.raw_path.decode(), content=request.content, headers={"content-type": "application/json"})
        return httpx.Response(answered.status_code, content=answered.content, headers={"content-type": answered.headers["content-type"]})


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # The identity headers are `catalog_identity`'s own subject; what is under test is the body.
    monkeypatch.setattr(catalog_compaction, "service_headers", lambda _settings: {})


def test_the_off_pod_plan_is_asked_for_under_the_configured_bounds(catalog: TestClient) -> None:
    assert (SCAN_BATCH_SIZE, COMPACT_THREADS) != (DEFAULT_SCAN_BATCH_SIZE, DEFAULT_COMPACT_THREADS)
    uri = _fragmented(catalog)
    wire = _Wire(catalog)
    settings = _settings()
    plan = sweep._resolve_plan(uri, policy_records=[], settings=settings, options={}, now=datetime.now(UTC), older_than=timedelta(days=7))

    # `assert_all_called` off: an unused commit route would raise at the block exit and hide the
    # assertions below, which name the hop that failed.
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_plan").mock(side_effect=wire.plan)
        router.post(f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_commit").mock(side_effect=wire.commit)
        result = sweep._maintain_one(uri, plan, settings=settings, options={}, protected=BaseRefs(), table_id=TABLE_ID)

    assert len(wire.plan_bodies) == 1, wire.plan_bodies
    sent = wire.plan_bodies[0]
    assert sent.get("batch_size") == SCAN_BATCH_SIZE, f"the plan did not carry MAINTENANCE_SCAN_BATCH_SIZE: {sent}"
    assert sent.get("num_threads") == COMPACT_THREADS, f"the plan did not carry MAINTENANCE_COMPACT_THREADS: {sent}"
    assert wire.planned_tasks, "the catalog planned no work, so nothing below was read from a task"
    baked = [json.loads(task)["options"] for task in wire.planned_tasks]
    assert [(options.get("batch_size"), options.get("num_threads")) for options in baked] == [(SCAN_BATCH_SIZE, COMPACT_THREADS)] * len(baked), baked
    assert result.error is None, result.error
    assert result.compaction_mode == "distributed", "the catalog refused the plan and the rewrite fell back in-pod"
    assert len(lance.dataset(uri).get_fragments()) == 1
