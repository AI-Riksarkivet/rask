"""An append its caller gave up on and sent again under the same key lands once, on the deployed release ([[LH-577]]).

The console aborts a write it waits too long for, and the catalog may commit it anyway; a person then sends it again.
The offline suites drive the insert door's key over real pylance
(`tests/integration/test_a_retried_create_does_not_rewrite_the_table.py`) and the console's retry against a stand-in
(`frontend/console/e2e/admin/tables-detail.spec.ts`). This proves what only the release can: the first send is given up on
long before the catalog answers it, it commits on the estate's own store all the same, and the resend under its key is
answered with that commit and appends nothing. The requests go through the Gateway, as the browser's do, where the run
names one (`RASK_E2E_EDGE_URL`, the `/api/catalog` route); a lane that stands up no Gateway reaches the catalog directly.

The e2e-stack lane runs it on every push. Against a deployed release, `bash scripts/e2e_live.sh
tests/e2e-py/test_a_retried_insert_appends_once.py` discovers the addresses.
"""

from __future__ import annotations

import os
import time
import uuid

import byo_scenario as scenario
import pyarrow as pa
import pytest
import requests
from byo_scenario import CATALOG, Tenant, arrow_ipc, ok, rows_of


pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not scenario.LIVE, reason=scenario.REQUIRES)]

#: The catalog as the browser reaches it: the Gateway's `/api/catalog` route, else the catalog itself.
_EDGE = os.environ.get("RASK_E2E_EDGE_URL", "").rstrip("/")
_CATALOG = f"{_EDGE}/api/catalog" if _EDGE else CATALOG
#: Rows in the append: enough bytes that the catalog cannot have answered within the first send's read timeout.
_ROWS = 20_000
#: How long the first send waits for an answer before its caller gives up on it, in seconds.
_GIVEN_UP_AFTER = 0.05
#: How long the first send is given to commit on the estate after its caller gave up on it, in seconds.
_LANDS_WITHIN = 60.0
_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _state(tenant: Tenant, table: str) -> tuple[int, int]:
    """The table's latest version and its row count, as its writer reads them."""
    described = ok(tenant.pipeline.http.post(f"{_CATALOG}/v1/table/{table}/describe", json={"load_detailed_metadata": True}, timeout=60))
    counted = tenant.pipeline.http.post(f"{_CATALOG}/v1/table/{table}/count_rows", json={}, timeout=60)
    assert counted.status_code == 200, counted.text
    return int(described["version"]), int(counted.text)


def test_an_append_sent_again_under_its_key_after_its_caller_gave_up_lands_once(byo_tenant: Tenant) -> None:
    table = f"{byo_tenant.bronze}$retried-{uuid.uuid4().hex[:6]}"
    ok(byo_tenant.pipeline.http.post(f"{_CATALOG}/v1/table/{table}/create", data=arrow_ipc(rows_of(range(3))), headers=_ARROW, timeout=60))
    byo_tenant.tables.append(table)
    before = _state(byo_tenant, table)
    landed = (before[0] + 1, before[1] + _ROWS)
    batch = arrow_ipc(pa.table({"id": pa.array(range(_ROWS), pa.int64()), "payload": pa.array([f"row-{i}" for i in range(_ROWS)])}))
    keyed = {**_ARROW, "Idempotency-Key": str(uuid.uuid4())}
    insert = f"{_CATALOG}/v1/table/{table}/insert?mode=append"

    with pytest.raises(requests.exceptions.ReadTimeout):
        byo_tenant.pipeline.http.post(insert, data=batch, headers=keyed, timeout=(10, _GIVEN_UP_AFTER))
    deadline = time.monotonic() + _LANDS_WITHIN
    state = _state(byo_tenant, table)
    while state != landed and time.monotonic() < deadline:
        time.sleep(1)
        state = _state(byo_tenant, table)
    assert state == landed, f"the append given up on never committed, so its resend would prove nothing: {state}"
    # Between its commit and its recorded answer the first attempt still holds the key, which the catalog answers 409.
    again = byo_tenant.pipeline.http.post(insert, data=batch, headers=keyed, timeout=120)
    while again.status_code == 409 and time.monotonic() < deadline:
        time.sleep(1)
        again = byo_tenant.pipeline.http.post(insert, data=batch, headers=keyed, timeout=120)

    assert (again.status_code, again.json()) == (200, {"num_inserted_rows": _ROWS, "version": landed[0]}), again.text
    assert _state(byo_tenant, table) == landed, "the resent append landed a second time"
