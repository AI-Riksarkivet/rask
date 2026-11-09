"""A commit on the deployed release is followed by its table's pass, which lists the compaction it does not run.

The offline suites drive each half over moto: the commit lane's queueing
(`services/maintenance/tests/test_an_announced_event_queues_the_task_it_asks_for.py`) and the pass
(`tests/integration/test_a_tables_pass_lists_what_is_due_and_rewrites_nothing.py`). This proves what only a deployed
release can: the catalog's announcement of a commit reaches maintenance's commit lane over the bus, the lane's task
reaches a worker through JetStream, and the worker's pass opens the real table and writes its due record, which the
catalog's due door answers to the table's owner (ADR 0198 §2, §3). The pass compacts nothing (ADR 0155).

Four commits through the stock client leave four small fragments, which Lance's own planner would merge, so the record
names a compaction, at the last commit's version: the first commit holds the table's pass the commit delay out
(`maintenance.queue.commitPassDelaySeconds`), and that pass reads the table after every commit of the burst. The wait is
bounded by the chart's default delay and what a busy queue may add to it.

The e2e-stack lane runs it on every push. Against a deployed release, `bash scripts/e2e_live.sh
tests/e2e-py/test_maintenance_e2e.py` discovers the addresses.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import byo_scenario as scenario
import lance
import pytest
from byo_scenario import CATALOG, Tenant, ok, rows_of


pytestmark = [pytest.mark.e2e, pytest.mark.compaction, pytest.mark.skipif(not scenario.LIVE, reason=scenario.REQUIRES)]


def _due_record(tenant: Tenant, table_uuid: str) -> dict[str, Any] | None:
    """The due door's record of the table ``table_uuid`` names, as its owner reads it, walking every page."""
    token: str | None = None
    while True:
        page = ok(tenant.pipeline.http.get(f"{CATALOG}/management/v1/maintenance/due", params={"page_token": token} if token else {}, timeout=60))
        if (found := next((due for due in page["tables"] if due["uuid"] == table_uuid), None)) is not None:
            return found
        if not (token := page.get("page_token")):
            return None


def test_a_commit_is_followed_by_its_tables_pass(byo_tenant: Tenant) -> None:
    table_id = [byo_tenant.bronze, f"maintained-{uuid.uuid4().hex[:6]}"]
    table = "$".join(table_id)
    stock = byo_tenant.pipeline.namespace()
    lance.write_dataset(rows_of(range(10)), namespace_client=stock, table_id=table_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    byo_tenant.tables.append(table)
    for start in (10, 20, 30):
        lance.write_dataset(rows_of(range(start, start + 10)), namespace_client=stock, table_id=table_id, mode="append")
    table_uuid = str(ok(byo_tenant.pipeline.http.post(f"{CATALOG}/v1/table/{table}/describe", json={}, timeout=60))["properties"]["rask.uuid"])

    deadline = time.monotonic() + scenario.COMMIT_PASS_SECONDS
    record = _due_record(byo_tenant, table_uuid)
    while (record is None or record["version"] < 4) and time.monotonic() < deadline:
        time.sleep(5)
        record = _due_record(byo_tenant, table_uuid)

    assert record is not None, f"no pass followed the commits on {table} within {scenario.COMMIT_PASS_SECONDS:.0f} s: its due record never appeared"
    assert (record["table"], record["version"]) == (table, 4), f"no pass followed the last commit on {table}: {record}"
    assert record["compaction"] is not None and record["compaction"]["candidate_fragments"] >= 2, f"the pass listed no compaction: {record}"
