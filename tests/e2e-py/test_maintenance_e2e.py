"""End-to-end test for the maintenance sweep against REAL Lance datasets.

The offline suite drives the sweep over moto; this proves what only the deployed release can: the service lists the
lakehouse bucket, opens the real Lance datasets the catalog wrote, expires their versions and evaluates their due
records without error (it compacts nothing, ADR 0155). This is the only signal that the Dapr-cron maintenance path
works against real storage. (The cron route is invoked directly here; the Dapr binding fires the same route on
schedule.)

What the sweep's metric does once it leaves the service is not asserted here, or by any test: OpenTelemetry is rask's
boundary, and the store behind the Collector is the platform's (ADR 0167). `test_observability_e2e.py` proves that a
service's metric crosses rask's Collector, with lineage's.

The e2e-stack lane runs it on every push. Against a deployed release, `bash scripts/e2e_live.sh
tests/e2e-py/test_maintenance_e2e.py` discovers the address and the token, or by hand:

    kubectl port-forward svc/rask-maintenance 8000:8000 &
    LANCE_E2E_MAINTENANCE_URL=http://localhost:8000 LANCE_E2E_DAPR_TOKEN=<the app token> \
    uv run pytest tests/e2e-py/test_maintenance_e2e.py -v

The chart renders every object as `{{ include "lance.fullname" . }}-…` (= `rask-…`, the release name). A port-forward
against a Service that does not exist fails with `services … not found`, which reads as "the release is broken"
rather than "this instruction is old".
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
import requests


MAINTENANCE = os.environ.get("LANCE_E2E_MAINTENANCE_URL", "")
BINDING = os.environ.get("LANCE_E2E_MAINTENANCE_BINDING", "maintenance-cron")
# The cron route is sidecar-only (§1 fail-closed): a direct POST must present the same `dapr-api-token` the sidecar
# stamps. scripts/e2e_stack.sh and scripts/e2e_live.sh read it from the app-token secret.
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")
_TOKEN_HEADER = {"dapr-api-token": DAPR_TOKEN} if DAPR_TOKEN else {}

pytestmark = [pytest.mark.e2e, pytest.mark.compaction]


@pytest.fixture(scope="module")
def maintenance() -> str:
    if not MAINTENANCE:
        pytest.skip("set LANCE_E2E_MAINTENANCE_URL (see module docstring)")
    try:
        requests.get(f"{MAINTENANCE.rstrip('/')}/livez", timeout=5).raise_for_status()
    except requests.RequestException:
        pytest.skip("maintenance not reachable")
    return MAINTENANCE.rstrip("/")


def test_the_sweep_expires_real_datasets(maintenance: str) -> None:
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
