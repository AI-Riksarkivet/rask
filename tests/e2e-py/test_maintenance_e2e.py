"""End-to-end test for the maintenance sweep against REAL Lance datasets.

The offline suite drives the sweep over moto; this proves what only the deployed release can: the service lists the
lakehouse bucket, opens the real Lance datasets the catalog wrote, expires their versions and evaluates their due
records without error (it compacts nothing, ADR 0155) — and the OTel sweep metric lands in GreptimeDB. This is the only signal that the Dapr-cron maintenance path works against real
storage. (The cron route is invoked directly here; the Dapr binding fires the same route on schedule.)

Run (port-forward maintenance + greptime), or `make e2e-compaction`:

    kubectl port-forward svc/rask-maintenance 8000:8000 &
    kubectl port-forward -n observability svc/greptimedb 4000:4000 &
    LANCE_E2E_MAINTENANCE_URL=http://localhost:8000 LANCE_E2E_GREPTIME_URL=http://localhost:4000 \
    uv run pytest tests/e2e-py/test_maintenance_e2e.py -v

The chart renders every object as `{{ include "lance.fullname" . }}-…` (= `rask-…`, the release name).
The telemetry store is the platform's; on a dev estate it is the observability fixture's `greptimedb`
in namespace `observability`. A port-forward against a Service that does not exist fails with
`services … not found`, which reads as "the release is broken" rather than "this instruction is old".
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
import requests


MAINTENANCE = os.environ.get("LANCE_E2E_MAINTENANCE_URL", "")
GREPTIME = os.environ.get("LANCE_E2E_GREPTIME_URL", "")
BINDING = os.environ.get("LANCE_E2E_MAINTENANCE_BINDING", "maintenance-cron")
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
    if not (MAINTENANCE and GREPTIME):
        pytest.skip("set LANCE_E2E_MAINTENANCE_URL and LANCE_E2E_GREPTIME_URL (see module docstring)")
    try:
        requests.get(f"{MAINTENANCE.rstrip('/')}/livez", timeout=5).raise_for_status()
        requests.get(f"{GREPTIME.rstrip('/')}/health", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("compaction or greptime not reachable")
    return MAINTENANCE.rstrip("/"), GREPTIME.rstrip("/")


def test_the_sweep_expires_real_datasets_and_meters(urls: tuple[str, str]) -> None:
    maintenance, _greptime = urls  # _prom_sum reads the module-level GREPTIME
    before = _prom_sum("sum(compaction_runs_total)")

    # Trigger one sweep (the same route the Dapr cron binding POSTs on schedule),
    # authenticating exactly like the sidecar does (dapr-api-token header).
    # The tick opens one manifest per dataset, so it costs what the estate is worth:
    # measured 288 datasets at ~12 s, and 30 s left no headroom on a busier one.
    # RETRY WHILE THE CRON HOLDS THE LOCK, which a single blind trigger cannot survive on a live
    # estate. The sweep is single-flight: a tick that finds one in progress answers 200 `skipped` and
    # does no work — the documented contract ("the next tick retries"), not a failure. What that costs a
    # one-shot caller, measured: "one sweep checks 347 datasets in 158 s against an `@every 300s` cron,
    # so a single blind trigger lands on a busy lock about half the time". A leg that fires once and
    # asserts on the answer reports a healthy estate as an overlapping sweep roughly every other drive.
    body: dict[str, Any] = {}
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        resp = requests.post(f"{maintenance}/{BINDING}", headers=_TOKEN_HEADER, timeout=600)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body.get("status") != "skipped":
            break
        time.sleep(10)

    assert body.get("status") != "skipped", f"an overlapping sweep was still running: {body}"
    assert body["datasets"] >= 1, f"no datasets discovered in the bucket: {body}"
    assert body["errors"] == {}, f"a dataset failed its pass: {body['errors']}"
    assert "versions_removed" in body and "due" in body, f"the tick reports neither its reclamation nor its due decisions: {body}"
    assert "failed" not in body["due"], f"a due evaluation could not decide: {body['due']}"

    # The OTel sweep counter incremented in GreptimeDB (the maintenance path is observable).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _prom_sum("sum(compaction_runs_total)") > before:
            return
        time.sleep(3)
    pytest.fail("compaction_runs_total did not increment in GreptimeDB after the sweep")
