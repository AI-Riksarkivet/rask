"""End-to-end test for the compaction/GC service against REAL Lance datasets.

The unit tests cover discovery + summary logic with fakes; this proves the part that can't be unit-tested:
the service actually lists the lakehouse bucket, opens the real Lance datasets the catalog wrote on
RustFS, and runs compact_files() + cleanup_old_versions() without error — and that the OTel sweep metric
lands in GreptimeDB. This is the only signal that the Dapr-cron maintenance path works against real
storage. (The cron route is invoked directly here; the Dapr binding fires the same route on schedule.)

Run (port-forward compaction + greptime), or `make e2e-compaction`:

    kubectl port-forward svc/lance-ns-compaction 8000:8000 &
    kubectl port-forward svc/lance-ns-greptimedb-standalone 4000:4000 &
    LANCE_E2E_COMPACTION_URL=http://localhost:8000 LANCE_E2E_GREPTIME_URL=http://localhost:4000 \
    uv run pytest tests/e2e-py/test_compaction_e2e.py -v
"""

from __future__ import annotations

import os
import time

import pytest
import requests


COMPACTION = os.environ.get("LANCE_E2E_COMPACTION_URL", "")
GREPTIME = os.environ.get("LANCE_E2E_GREPTIME_URL", "")
BINDING = os.environ.get("LANCE_E2E_COMPACTION_BINDING", "compaction-cron")
# The cron route is sidecar-only (§1 fail-closed): a direct POST must present the same
# `dapr-api-token` the sidecar stamps. `make e2e-compaction` reads it from the app-token secret.
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")
_TOKEN_HEADER = {"dapr-api-token": DAPR_TOKEN} if DAPR_TOKEN else {}

pytestmark = [pytest.mark.e2e, pytest.mark.compaction]


def _prom_sum(query: str) -> float:
    r = requests.get(f"{GREPTIME}/v1/prometheus/api/v1/query", params={"query": query}, timeout=10)
    r.raise_for_status()
    return sum(float(s["value"][1]) for s in r.json()["data"]["result"])


@pytest.fixture(scope="module")
def urls() -> tuple[str, str]:
    if not (COMPACTION and GREPTIME):
        pytest.skip("set LANCE_E2E_COMPACTION_URL and LANCE_E2E_GREPTIME_URL (see module docstring)")
    try:
        requests.get(f"{COMPACTION.rstrip('/')}/livez", timeout=5).raise_for_status()
        requests.get(f"{GREPTIME.rstrip('/')}/health", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("compaction or greptime not reachable")
    return COMPACTION.rstrip("/"), GREPTIME.rstrip("/")


def test_sweep_compacts_real_datasets_and_meters(urls: tuple[str, str]) -> None:
    compaction, _greptime = urls  # _prom_sum reads the module-level GREPTIME
    before = _prom_sum("sum(compaction_runs_total)")

    # Trigger one sweep (the same route the Dapr cron binding POSTs on schedule),
    # authenticating exactly like the sidecar does (dapr-api-token header).
    resp = requests.post(f"{compaction}/{BINDING}", headers=_TOKEN_HEADER, timeout=30)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # It discovered the real Lance datasets the catalog wrote on RustFS, and none errored.
    assert body["datasets"] >= 1, f"no datasets discovered in the bucket: {body}"
    assert body["errors"] == {} or body["errors"] == [], f"a dataset failed to compact: {body['errors']}"
    assert "fragments_removed" in body and "versions_removed" in body

    # The OTel sweep counter incremented in GreptimeDB (the maintenance path is observable).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _prom_sum("sum(compaction_runs_total)") > before:
            return
        time.sleep(3)
    pytest.fail("compaction_runs_total did not increment in GreptimeDB after the sweep")
