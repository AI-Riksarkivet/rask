"""The staged-event probe's shared setup: a bearer, the subject it names, and an owned output table.

TWO SUITES STAGE AN EVENT INTO THE OUTBOX AND EXPECT THE RELAY TO DRAIN IT — `test_outbox_e2e` and
`test_outbox_crash_e2e` — and `enforce_bus_authz` asks the same two questions of both: does the
event's author resolve to a real subject, and may that subject write the output table it names.

ONLY ONE OF THE TWO ANSWERED THEM, WHICH IS WHY THIS IS A MODULE RATHER THAN A COPY. Measured on the
deployed estate 2026-09-20: `s3://lance-catalog/_lineage_outbox` held five objects dated 2026-09-14,
one per run of the crash suite, each `author="e2e"` with `output_name="e2e_crash_ds"` — a role LITERAL
that no `sub` can ever match, and a BARE table id for which no catalog object exists. Neither can be
authorized by anyone, so every run added one and none ever left: `lineage_outbox_drained drained=0
stranded=0 refused=5`, unchanged for six days.

THE REMEDY IS THE ONE THE OTHER SUITE ALREADY CARRIED — create the table so it HAS an FGA object, and
stamp the identity that created it so the event's author is a subject the gate can resolve. Sharing it
is what stops the two from drifting again, which is the whole reason the crash suite was still
producing residue after the probe suite stopped.

AN OPEN STACK ANSWERS `""` and the caller keeps its old author: with authz off there is nothing to
authorize against, and demanding a token there would skip these suites on the deployments they were
written for.
"""

from __future__ import annotations

import base64
import json
import os

import pyarrow as pa
import requests


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
WAREHOUSE = os.environ.get("LANCE_E2E_WAREHOUSE", "")


def bearer() -> str:
    """The deployed stack's bearer token, or ``""`` when it serves the catalog open.

    PROBED RATHER THAN CONFIGURED, the same way `test_observability_e2e` does it: an open stack must
    keep working with no Dex forward, and a governed one must not be driven unauthenticated.

    `LANCE_E2E_DEX` is the estate's name for the issuer — `scripts/e2e_live.sh:145` exports it and the
    other live suites read it. A spelling only one module knows is one the script never sets, so the
    mint returns "" and the suite dies in setup on a 401 that reads like a governed door refusing.
    """
    if not CATALOG:
        return ""
    try:
        probe = requests.get(f"{CATALOG}/v1/namespace/$/list", timeout=5)
    except Exception:
        return ""
    if probe.status_code != 401:
        return ""
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


def subject_of(token: str) -> str:
    """The `sub` claim, which is the FGA subject verbatim — `deps.py` uses the raw IdP sub.

    Read from the TOKEN rather than configured, because the author the probe stamps and the owner the
    catalog recorded have to be the same string or the gate refuses an event whose output the author
    does hold.
    """
    payload = token.split(".")[1] if token.count(".") == 2 else ""
    if not payload:
        return ""
    padded = payload + "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(padded)).get("sub", ""))


def owned_output_table(namespace: str, table: str) -> str:
    """Create ``<namespace>$<table>`` through the catalog and answer the identity that now owns it.

    BOTH HALVES ARE NEEDED AND THE SECOND IS THE NON-OBVIOUS ONE. Creating the table gives it an FGA
    parent and owner tuples; but the bus door authorizes AS `author.sub`, so an event authored by
    anyone else is still refused on a table that now exists. Returning the creating subject is what
    lets the caller stamp an author the gate can actually resolve.
    """
    token = bearer()
    if not CATALOG:
        return ""
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    table_id = f"{namespace}${table}"
    if WAREHOUSE:
        requests.post(
            f"{CATALOG}/v1/warehouses/{WAREHOUSE}/namespaces",
            json={"namespace": namespace, "adopt_existing": True},
            headers=auth,
            timeout=30,
        )
    else:
        requests.post(f"{CATALOG}/v1/namespace/{namespace}/create", json={}, headers=auth, timeout=30)
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
    assert created.status_code == 200, f"could not create the probe's output table {table_id}: {created.status_code} {created.text[:300]}"
    return subject_of(token)
