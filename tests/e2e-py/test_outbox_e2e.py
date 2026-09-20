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

import base64
import json
import os
import time
from typing import Any

import pyarrow as pa
import pytest
import requests

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


def _bearer() -> str:
    """The deployed stack's bearer token, or ``""`` when it serves the catalog open.

    Probed rather than configured, the same way `test_observability_e2e` does it: an open stack must
    keep working with no Dex forward, and a governed one must not be driven unauthenticated.
    """
    if not CATALOG:
        return ""
    try:
        probe = requests.get(f"{CATALOG}/v1/namespace/$/list", timeout=5)
    except Exception:
        return ""
    if probe.status_code != 401:
        return ""
    # `LANCE_E2E_DEX` is the estate's name for this: `scripts/e2e_live.sh:145` exports it and nine
    # other live suites read it. A spelling only this module knows is one the script never sets, so
    # the mint returns "" and the suite dies in setup on `401 Missing bearer token` — which reads as a
    # governed door refusing rather than as a variable nobody assigned (measured 2026-09-20).
    dex = os.environ.get("LANCE_E2E_DEX", "").rstrip("/")
    if not dex:
        return ""
    form = {
        "grant_type": "password",
        "client_id": os.environ.get("LANCE_E2E_OIDC_CLIENT_ID", "lance-catalog"),
        "client_secret": os.environ.get("LANCE_E2E_OIDC_CLIENT_SECRET", "lance-catalog-secret"),
        "scope": "openid email",
        "username": os.environ.get("LANCE_E2E_OIDC_USERNAME", "alice@example.com"),
        "password": os.environ.get("LANCE_E2E_OIDC_PASSWORD", "password"),
    }
    try:
        resp = requests.post(f"{dex}/token", data=form, timeout=15)
        resp.raise_for_status()
    except Exception:
        return ""
    return str(resp.json().get("id_token", ""))


def _subject_of(token: str) -> str:
    """The `sub` claim, which is the FGA subject verbatim — `deps.py` uses the raw IdP sub.

    Read from the token rather than configured, because the AUTHOR the probe stamps and the OWNER the
    catalog grants have to be the same principal or the bus door refuses an event about a table the
    author does not hold.
    """
    payload = token.split(".")[1] if token.count(".") == 2 else ""
    if not payload:
        return ""
    padded = payload + "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(padded)).get("sub", ""))


@pytest.fixture(scope="module")
def probe_author() -> str:
    """Create the probe's OUTPUT table through the catalog, and answer the identity that now owns it.

    [[LH-152]], owner ruling 2026-09-15. The suite used to stage an event naming `bronze$e2e_outbox_ds`
    — a name no catalog object ever carried — so the table had NO FGA object and therefore no identity
    could be authorized for it. `enforce_bus_authz` refused the drained event every run, and the leg
    could not pass for any author, which is why it is a fixture rather than a different assertion.

    BOTH HALVES ARE NEEDED AND THE SECOND IS THE NON-OBVIOUS ONE. Creating the table gives it an FGA
    parent and owner tuples; but the bus door authorizes AS `author.sub`, so an event authored by
    anyone else is still refused on a table that now exists. The fixture therefore returns the creating
    subject and the probe stamps it, which is what makes the drained event land.

    An OPEN stack answers `""` and the probe keeps its old author: with authz off there is nothing to
    authorize against, and demanding a token there would skip the suite on the deployments it was
    written for.
    """
    token = _bearer()
    if not CATALOG:
        return ""
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    table_id = f"{PROBE_NAMESPACE}${PROBE_TABLE}"
    if WAREHOUSE:
        requests.post(
            f"{CATALOG}/v1/warehouses/{WAREHOUSE}/namespaces",
            json={"namespace": PROBE_NAMESPACE, "adopt_existing": True},
            headers=auth,
            timeout=30,
        )
    else:
        requests.post(f"{CATALOG}/v1/namespace/{PROBE_NAMESPACE}/create", json={}, headers=auth, timeout=30)
    rows = pa.table({"id": pa.array([1], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    # `mode=overwrite` so a re-run adopts the table a previous run created rather than 409ing in setup,
    # which would report every test in the file as an ERROR.
    created = requests.post(
        f"{CATALOG}/v1/table/{table_id}/create?mode=overwrite",
        data=sink.getvalue().to_pybytes(),
        headers={**auth, "Content-Type": "application/vnd.apache.arrow.stream"},
        timeout=60,
    )
    assert created.status_code == 200, f"could not create the probe's output table: {created.status_code} {created.text[:300]}"
    return _subject_of(token)


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
        inputs=[("external", "e2e_outbox_src")],
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
