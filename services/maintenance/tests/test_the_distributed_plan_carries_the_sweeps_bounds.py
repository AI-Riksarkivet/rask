"""The sweep's off-pod rewrite plans under the sweep's OWN memory bounds and mode, and the tasks carry them.

The catalog refuses a compaction plan that does not state `batch_size`, `num_threads` and
`max_source_bytes`: Lance bakes all three into every task at plan time and `CompactionTask.execute`
takes only the dataset, so the plan is the executor's one chance to bound its read. That makes this
executor responsible for sending them, and the numbers it sends must be the estate's configured ones
(`MAINTENANCE_SCAN_BATCH_SIZE`, `MAINTENANCE_COMPACT_THREADS`, `MAINTENANCE_MAX_SOURCE_BYTES`) — the
bounds the in-pod rewrite runs under. `MAINTENANCE_REPACK_MODE` is baked the same way and crosses when
set; unset, both paths run Lance's own default.

A dropped bound skips the dataset's rewrite: the door answers 400, which is this executor's bug, so no
in-pod rewrite hides it. `compaction_mode` is asserted as the witness that the off-pod path ran.

Driven from `_resolve_plan` through `_maintain_one`, so every hop between the settings and the request
body is the production one. Only the transport is replaced: respx hands each request to the CATALOG'S
OWN APP over a real `dir` namespace, so the door that turns the body into baked tasks is production
code too, and the options read back out of those tasks are what a worker would execute.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
import respx
from fastapi.testclient import TestClient

from maintenance.core.config import DEFAULT_COMPACT_THREADS, DEFAULT_MAX_SOURCE_BYTES, DEFAULT_SCAN_BATCH_SIZE, MaintenanceSettings
from maintenance.services import catalog_compaction, sweep
from service_kit.lakehouse.base_refs import BaseRefs
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
#: Neither is a default, so a hop that forgot the setting and fell back to the floor cannot pass.
SCAN_BATCH_SIZE = 37
COMPACT_THREADS = 3
MAX_SOURCE_BYTES = 3 * 1024 * 1024 + 7


def _fragmented(catalog: TestClient, *, writes: int = 6, rows: int = 10) -> str:
    """`TABLE_ID`, created through the catalog and split across `writes` fragments; its location."""
    assert catalog.post("/v1/namespace/acme-bronze/create", json={}).status_code == 200
    created = catalog.post(
        f"/v1/table/{TABLE_ID}/create",
        content=encode_arrow_stream(pa.table({"id": pa.array(range(rows), pa.int64())})),
        headers={"content-type": ARROW_STREAM_MEDIA_TYPE},
    )
    assert created.status_code == 200, created.text
    uri = str(catalog.post(f"/v1/table/{TABLE_ID}/describe", json={}).json()["location"])
    for i in range(1, writes):
        lance.write_dataset(pa.table({"id": pa.array(range(i * rows, (i + 1) * rows), pa.int64())}), uri, mode="append")
    assert len(lance.dataset(uri).get_fragments()) == writes
    return uri


def _settings(repack_mode: str | None = None) -> MaintenanceSettings:
    return MaintenanceSettings.model_validate(
        {
            "s3_bucket": "lake",
            "distributed_compaction": True,
            "catalog_url": CATALOG,
            "scan_batch_size": SCAN_BATCH_SIZE,
            "compact_threads": COMPACT_THREADS,
            "max_source_bytes": MAX_SOURCE_BYTES,
            "repack_mode": repack_mode,
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


@pytest.mark.parametrize(("repack_mode", "baked_mode"), [(None, None), ("try_binary_copy", "TryBinaryCopy")])
def test_the_off_pod_plan_is_asked_for_under_the_configured_bounds(catalog: TestClient, repack_mode: str | None, baked_mode: str | None) -> None:
    assert (SCAN_BATCH_SIZE, COMPACT_THREADS, MAX_SOURCE_BYTES) != (DEFAULT_SCAN_BATCH_SIZE, DEFAULT_COMPACT_THREADS, DEFAULT_MAX_SOURCE_BYTES)
    uri = _fragmented(catalog)
    wire = _Wire(catalog)
    settings = _settings(repack_mode)
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
    assert sent.get("max_source_bytes") == MAX_SOURCE_BYTES, f"the plan did not carry MAINTENANCE_MAX_SOURCE_BYTES: {sent}"
    assert sent.get("compaction_mode") == repack_mode, f"the plan did not carry MAINTENANCE_REPACK_MODE as the in-pod rewrite would: {sent}"
    assert wire.planned_tasks, "the catalog planned no work, so nothing below was read from a task"
    baked = [json.loads(task)["options"] for task in wire.planned_tasks]
    found = [(options.get("batch_size"), options.get("num_threads"), options.get("max_source_bytes"), options.get("compaction_mode")) for options in baked]
    assert found == [(SCAN_BATCH_SIZE, COMPACT_THREADS, MAX_SOURCE_BYTES, baked_mode)] * len(baked), baked
    assert result.error is None, result.error
    assert result.compaction_mode == "distributed", "the off-pod path did not run"
    assert len(lance.dataset(uri).get_fragments()) == 1


def test_a_tier_policy_outranks_the_settings_on_the_off_pod_plan(catalog: TestClient) -> None:
    """A policy record that names its own byte bound and mode is what the plan carries, not the global setting."""
    policy_bytes = 5 * 1024 * 1024 + 11
    uri = _fragmented(catalog)
    wire = _Wire(catalog)
    settings = _settings("try_binary_copy")
    record = {"kind": "table", "id": TABLE_ID, "path": uri.rstrip("/"), "max_source_bytes": policy_bytes, "repack_mode": "force_binary_copy"}
    plan = sweep._resolve_plan(uri, policy_records=[record], settings=settings, options={}, now=datetime.now(UTC), older_than=timedelta(days=7))
    assert (plan.max_source_bytes, plan.repack_mode) == (policy_bytes, "force_binary_copy"), "the record did not resolve, so nothing below tests it"

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_plan").mock(side_effect=wire.plan)
        router.post(f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_commit").mock(side_effect=wire.commit)
        sweep._maintain_one(uri, plan, settings=settings, options={}, protected=BaseRefs(), table_id=TABLE_ID)

    assert len(wire.plan_bodies) == 1, wire.plan_bodies
    sent = wire.plan_bodies[0]
    assert (sent.get("max_source_bytes"), sent.get("compaction_mode")) == (policy_bytes, "force_binary_copy"), (
        f"the plan carried the settings, not the policy: {sent}"
    )
    assert wire.planned_tasks, "the catalog planned no work, so nothing below was read from a task"
    baked = [json.loads(task)["options"] for task in wire.planned_tasks]
    found = [(options.get("max_source_bytes"), options.get("compaction_mode")) for options in baked]
    assert found == [(policy_bytes, "ForceBinaryCopy")] * len(baked), baked
