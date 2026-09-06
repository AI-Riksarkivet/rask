"""End-to-end test for the compaction/GC service against REAL Lance datasets.

The unit tests cover discovery + summary logic with fakes; this proves the part that can't be unit-tested:
the service actually lists the lakehouse bucket, opens the real Lance datasets the catalog wrote on
RustFS, and runs compact_files() + cleanup_old_versions() without error — and that the OTel sweep metric
lands in GreptimeDB. This is the only signal that the Dapr-cron maintenance path works against real
storage. (The cron route is invoked directly here; the Dapr binding fires the same route on schedule.)

Run (port-forward maintenance + greptime), or `make e2e-compaction`:

    kubectl port-forward svc/rask-maintenance 8000:8000 &
    kubectl port-forward svc/rask-greptimedb-standalone 4000:4000 &
    LANCE_E2E_MAINTENANCE_URL=http://localhost:8000 LANCE_E2E_GREPTIME_URL=http://localhost:4000 \
    uv run pytest tests/e2e-py/test_maintenance_e2e.py -v

Those Service names were `lance-ns-compaction` / `lance-ns-greptimedb-standalone` and were stale on
both halves: the chart renders every object as `{{ include "lance.fullname" . }}-…` (= `rask-…`, the
release name), and the service was renamed `compaction` -> `maintenance` because it does four things,
not one. A port-forward against a Service that does not exist fails with `services … not found`,
which reads as "the release is broken" rather than "this instruction is old".
"""

from __future__ import annotations

import os
import time

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


def test_sweep_compacts_real_datasets_and_meters(urls: tuple[str, str]) -> None:
    compaction, _greptime = urls  # _prom_sum reads the module-level GREPTIME
    before = _prom_sum("sum(compaction_runs_total)")

    # Trigger one sweep (the same route the Dapr cron binding POSTs on schedule),
    # authenticating exactly like the sidecar does (dapr-api-token header).
    # `plan_sweep` opens one manifest per dataset, so a tick costs what the estate is worth:
    # measured 288 datasets at ~12 s, and 30 s left no headroom on a busier one.
    resp = requests.post(f"{compaction}/{BINDING}", headers=_TOKEN_HEADER, timeout=120)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # TWO LANES answer this route and they report different things, because on one of them the tick
    # maintains nothing itself. `on_cron` chooses on `MAINTENANCE_WORK_TOPIC`: SET — as the deployed
    # release sets it — the tick PLANS and publishes one unit per dataset and a subscription compacts
    # each later, so this leg covers discovery and enqueue and the reclamation counts are not its to
    # report. UNSET (the chart default, and every local run) it compacts serially and reports what it
    # reclaimed. Asserting the serial shape against the queue lane is how this leg read as "the sweep
    # is broken" while the sweep was fine.
    assert body.get("status") != "skipped", f"an overlapping sweep was still running: {body}"
    if body.get("status") == "enqueued":
        # `planned` is the units, `skipped` the trash exclusions decided WITHOUT work — together they
        # are everything discovered.
        assert body["planned"] + body["skipped"] >= 1, f"no datasets discovered in the bucket: {body}"
        # `not_queued` is this lane's `errors`: a unit that never reached the broker is a dataset that
        # goes unmaintained this tick.
        assert body["not_queued"] == 0, f"units never reached the broker: {body}"
        assert body["published"] == body["planned"], f"published does not account for every unit: {body}"
    else:
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
