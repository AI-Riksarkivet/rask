"""The sweep's off-pod rewrite plans under the sweep's OWN memory bounds.

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
body is the production one. Only the catalog's transport is replaced (respx), and what answers is the
catalog's own request model and dataplane functions against a real local dataset.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
import respx
from lance_namespace import InvalidInputError

from catalog.schemas import CompactionPlanRequest
from catalog.services.dataplane import commit_compaction, plan_compaction
from maintenance.core.config import DEFAULT_COMPACT_THREADS, DEFAULT_SCAN_BATCH_SIZE, MaintenanceSettings
from maintenance.services import catalog_compaction, sweep
from service_kit.lakehouse.base_refs import BaseRefs


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
#: Neither is a default, so a hop that forgot the setting and fell back to the floor cannot pass.
SCAN_BATCH_SIZE = 37
COMPACT_THREADS = 3


def _fragmented(tmp_path: Path, *, writes: int = 6, rows: int = 10) -> str:
    uri = str(tmp_path / "t.lance")
    for i in range(writes):
        table = pa.table({"id": pa.array(range(i * rows, (i + 1) * rows), pa.int64())})
        if i == 0:
            lance.write_dataset(table, uri, data_storage_version="2.2", enable_stable_row_ids=True)
        else:
            lance.write_dataset(table, uri, mode="append")
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


class _Catalog:
    """The two metadata doors, answered by the catalog's own model and dataplane over a local dataset."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.plan_bodies: list[dict[str, Any]] = []

    def plan(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.plan_bodies.append(body)
        asked = CompactionPlanRequest.model_validate(body).model_dump(exclude_none=True)
        try:
            planned = plan_compaction(self.uri, {}, batch_size=asked.pop("batch_size", None), num_threads=asked.pop("num_threads", None), **asked)
        except InvalidInputError as exc:
            return httpx.Response(400, json={"code": 13, "detail": str(exc)})
        return httpx.Response(200, json=planned.model_dump())

    def commit(self, request: httpx.Request) -> httpx.Response:
        outcome = commit_compaction(self.uri, {}, json.loads(request.content)["results"])
        return httpx.Response(200, json=outcome.model_dump())


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # The identity headers are `catalog_identity`'s own subject; what is under test is the body.
    monkeypatch.setattr(catalog_compaction, "service_headers", lambda _settings: {})


def test_the_off_pod_plan_is_asked_for_under_the_configured_bounds(tmp_path: Path) -> None:
    assert (SCAN_BATCH_SIZE, COMPACT_THREADS) != (DEFAULT_SCAN_BATCH_SIZE, DEFAULT_COMPACT_THREADS)
    uri = _fragmented(tmp_path)
    catalog = _Catalog(uri)
    settings = _settings()
    plan = sweep._resolve_plan(uri, policy_records=[], settings=settings, options={}, now=datetime.now(UTC), older_than=timedelta(days=7))

    # `assert_all_called` off: an unused commit route would raise at the block exit and hide the
    # assertions below, which name the hop that failed.
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_plan").mock(side_effect=catalog.plan)
        router.post(f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_commit").mock(side_effect=catalog.commit)
        result = sweep._maintain_one(uri, plan, settings=settings, options={}, protected=BaseRefs(), table_id=TABLE_ID)

    assert len(catalog.plan_bodies) == 1, catalog.plan_bodies
    sent = catalog.plan_bodies[0]
    assert sent.get("batch_size") == SCAN_BATCH_SIZE, f"the plan did not carry MAINTENANCE_SCAN_BATCH_SIZE: {sent}"
    assert sent.get("num_threads") == COMPACT_THREADS, f"the plan did not carry MAINTENANCE_COMPACT_THREADS: {sent}"
    assert result.error is None, result.error
    assert result.compaction_mode == "distributed", "the catalog refused the plan and the rewrite fell back in-pod"
    assert len(lance.dataset(uri).get_fragments()) == 1
