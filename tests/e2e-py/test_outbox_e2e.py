"""#4 — live lineage-outbox drain e2e against the DEPLOYED stack.

Simulates a producer that crashed AFTER the Lance commit but BEFORE the publish acked: a full RunEvent is
left staged in the object-store outbox. Triggering the reconcile sweep must DRAIN it — re-ingest the event
into the graph (the ``outbox_drained`` counter increments only on a successful ingest) and delete the
object. Combined with the unit test that proves the stage runner leaves the event staged on a publish failure,
this closes the commit→publish loss window end to end.

Skipped unless ``LANCE_E2E_LINEAGE_URL`` + ``LANCE_E2E_DAPR_TOKEN`` are set. Run via ``make e2e-live``,
which discovers both from the cluster, or ``make e2e-ci``, whose own help calls this suite #4 of five
(needs the stack deployed with ``services.lineage.outbox.enabled=true`` + ``reconcile.enabled=true``).

BOTH NAMED TARGETS EXIST, and that is the point of naming two: a per-suite ``e2e-outbox`` target does
not, so an invocation invented from this file's own name fails with ``No rule to make target`` and reads
as "the check cannot be run" rather than "the command was wrong".
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest
import requests
from outbox_probe import owned_output_table

from medallion.schemas.events import build_run_event
from service_kit.lakehouse import outbox


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
WAREHOUSE = os.environ.get("LANCE_E2E_WAREHOUSE", "")
#: The namespace + table the staged probe event names as its OUTPUT.
PROBE_NAMESPACE = "bronze"
PROBE_TABLE = "e2e_outbox_ds"
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")
BINDING = os.environ.get("LANCE_E2E_RECONCILE_BINDING", "lineage-reconcile-cron")
OUTBOX_URI = os.environ.get("LANCE_E2E_OUTBOX_URI", "s3://lance-catalog/_lineage_outbox")
S3 = os.environ.get("LANCE_E2E_S3", "http://localhost:9900")

pytestmark = pytest.mark.e2e


def _so() -> dict[str, str]:
    return {
        "endpoint": S3,
        "access_key_id": "minioadmin",
        "secret_access_key": "minioadmin",
        "region": "us-east-1",
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


@pytest.fixture(scope="module")
def probe_author() -> str:
    """Create the probe's OUTPUT table through the catalog, and answer the identity that now owns it.

    [[LH-152]], owner ruling 2026-09-15. An event naming a table no catalog object carries has NO FGA
    object, so no identity can be authorized for it and `enforce_bus_authz` refuses the drained event
    every run — which is why this is a fixture rather than a different assertion.

    IN `outbox_probe` RATHER THAN HERE because `test_outbox_crash_e2e` stages into the same outbox and
    needs the same two guarantees. It had neither, and left one permanently-refused object per run
    (five, dated 2026-09-14, measured 2026-09-20). One copy is what stops the two drifting again.
    """
    return owned_output_table(PROBE_NAMESPACE, PROBE_TABLE)


@pytest.fixture(scope="module")
def lineage() -> str:
    if not LINEAGE or not DAPR_TOKEN:
        pytest.skip("set LANCE_E2E_LINEAGE_URL + LANCE_E2E_DAPR_TOKEN (deployed stack with the outbox on)")
    try:
        requests.get(f"{LINEAGE}/livez", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("lineage service not reachable")
    return LINEAGE


def test_reconcile_sweep_drains_a_staged_outbox_event(lineage: str, probe_author: str) -> None:
    # A crashed producer's leftover: a full, valid RunEvent staged in the outbox.
    event = build_run_event(
        operation="e2e_outbox_probe",
        # The subject that OWNS the output table (see `probe_author`); `"e2e"` only where authz is off.
        author=probe_author or "e2e",
        job_namespace="medallion",
        # THE NAMESPACE IS THE EXTERNAL MARKER, and `"external"` is not one. `is_external_source`
        # exempts an input from `can_get_metadata` in exactly two forms — a namespace carrying a URI
        # scheme (`s3://…`), or one of the bare identifiers in `EXTERNAL_SOURCE_NAMESPACES`
        # (`{"file", "lance"}`). A namespace that is neither reads as a CATALOG namespace, so a
        # notional source outside the estate is authorized like a governed table and refused on an
        # object that can never carry a tuple. Measured live 2026-09-20:
        # `can_get_metadata required on inputs: e2e_crash_src`.
        inputs=[("s3://e2e-outbox-src", "e2e_outbox_src")],
        output_namespace=PROBE_NAMESPACE,
        # QUALIFIED, because `output_name` carries the CATALOG ID and `output_namespace` is the
        # OpenLineage domain label — two different things that both happen to be called a namespace.
        # Production emits the pair that way (`lineage_emit.py:58`: namespace `media`, name
        # `media$documents`), and the feed's own events read `inputs: ["bronze$events"]`.
        #
        # The bus door authorizes against the id in `name`, so a bare one resolves to
        # `table:e2e_outbox_ds` — an object no catalog ever created, therefore carrying no tuples and
        # deniable for every author. Measured live 2026-09-20: `lineage_outbox_drained drained=0
        # stranded=0 refused=6`, with `ingest_run_mutation_denied … outputs=['e2e_outbox_ds']`, while
        # the table this fixture creates one function up is `bronze$e2e_outbox_ds`.
        output_name=f"{PROBE_NAMESPACE}${PROBE_TABLE}",
        version=1,
        token="e2e-outbox-probe",
    )
    run_id = event["run"]["runId"]
    event_json = json.dumps(event)
    # The staged object is keyed per EVENT, not per run — `<run_id>@<EVENT_TYPE>` (`outbox._object_key`),
    # because a run id deliberately excludes the event type and one run has a START and then a COMPLETE
    # or a FAIL. `list_events` derives its key from the FILENAME, so that is what the outbox answers with.
    key = f"{run_id}@{event['eventType']}"
    outbox.stage_event(OUTBOX_URI, _so(), run_id, event_json)
    assert key in dict(outbox.list_events(OUTBOX_URI, _so()))  # staged

    # Trigger the reconcile sweep (the Dapr cron's manual equivalent — token-gated), RETRYING while the
    # cron's own tick holds the single-flight advisory lock. `_on_cron` then answers 200
    # `{"skipped": true}` and does no work — the documented contract ("the next tick retries"), not a
    # failure. Measured 2026-09-06 on the live estate: one sweep checks 347 datasets in 158 s against an
    # `@every 300s` cron, so a single blind trigger lands on a busy lock about half the time.
    # Re-stage before each attempt: a cron tick winning the lock while we wait would drain OUR event and
    # our own tick would then honestly report 0. Re-staging keeps the assertion below at full strength.
    body: dict[str, Any] = {}
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if key not in dict(outbox.list_events(OUTBOX_URI, _so())):
            outbox.stage_event(OUTBOX_URI, _so(), run_id, event_json)
        # The client timeout must cover a whole SWEEP, not a request: this route runs it inline.
        resp = requests.post(f"{lineage}/{BINDING}", headers={"dapr-api-token": DAPR_TOKEN}, timeout=600)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if not body.get("skipped"):
            break
        time.sleep(10)
    assert not body.get("skipped"), f"no free reconcile tick inside the budget: {body}"

    # The counter increments only after a successful graph ingest, so >=1 proves the event REACHED AGE...
    assert body.get("outbox_drained", 0) >= 1, body
    # ...and the drained object is deleted (not left to grow unbounded / re-ingest forever).
    assert key not in dict(outbox.list_events(OUTBOX_URI, _so()))
