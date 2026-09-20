"""Chaos: lineage can be taken down and the events published while it was gone still land.

The estate's durability story for lineage is REPLAY, not retry — its subscriber is ephemeral with
`deliverPolicy: all`, so a restarted pod re-reads the retained stream (`chart/templates/dapr-component.yaml`).
Nothing proved that automatically; the row it closes was hand-driven only.

The events are produced by real catalog writes, never hand-published. A synthetic author is refused by
`enforce_bus_authz` and bumps a watched refusal counter, and an authorized one injects fabricated
provenance into the governed graph — so the only honest producer is the catalog itself.

Needs a deployed release and kubectl: this scales a Deployment to 0 and back. Env-gated, and out of
`e2e-ci`'s suite list on purpose.
"""

from __future__ import annotations

import io
import os
import subprocess
import time
import uuid
from urllib.parse import quote

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import requests
from topology import create_top_level


pytestmark = [pytest.mark.e2e, pytest.mark.chaos]

CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
TOKEN = os.environ.get("LANCE_E2E_TOKEN", "")
DEPLOY = os.environ.get("LANCE_E2E_LINEAGE_DEPLOY", "rask-lineage")
ARROW = "application/vnd.apache.arrow.stream"
WRITES_WHILE_DOWN = 3


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def _ipc(ids: list[int]) -> bytes:
    sink = io.BytesIO()
    table = pa.table({"id": pa.array(ids, pa.int64())})
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=True, timeout=300).stdout.strip()


def _scale(replicas: int) -> None:
    _kubectl("scale", f"deploy/{DEPLOY}", f"--replicas={replicas}")
    _kubectl("rollout", "status", f"deploy/{DEPLOY}", "--timeout=300s")


def _ready_replicas() -> int:
    return int(_kubectl("get", f"deploy/{DEPLOY}", "-o", "jsonpath={.status.readyReplicas}") or 0)


def _runs_naming(table_id: str, *, limit: int = 200) -> set[str]:
    """Run ids on the durable feed whose event names ``table_id``."""
    body = requests.get(f"{LINEAGE}/events", headers=_auth(), params={"limit": limit}, timeout=60).json()
    rows = body.get("events", body) if isinstance(body, dict) else body
    return {row["event"]["run"]["runId"] for row in rows if table_id in str(row.get("outputs", ""))}


@pytest.fixture(scope="module")
def live() -> str:
    if not (CATALOG and LINEAGE and TOKEN):
        pytest.skip("set LANCE_E2E_CATALOG_URL + LANCE_E2E_LINEAGE_URL + LANCE_E2E_TOKEN")
    try:
        _kubectl("get", f"deploy/{DEPLOY}")
    except Exception:
        pytest.skip(f"kubectl cannot reach deploy/{DEPLOY} — this suite scales it")
    return CATALOG


@pytest.fixture
def table(live: str) -> str:
    namespace = f"chaosns{uuid.uuid4().hex[:8]}"
    create_top_level(live, namespace, _auth())
    table_id = f"{namespace}$replay"
    response = requests.post(
        f"{live}/v1/table/{quote(table_id, safe='')}/create",
        headers={**_auth(), "content-type": ARROW},
        data=_ipc([0]),
        timeout=120,
    )
    assert response.status_code == 200, response.text
    return table_id


def test_events_published_while_lineage_is_down_are_replayed(table: str) -> None:
    quoted = quote(table, safe="")
    before = _runs_naming(table)

    _scale(0)
    try:
        assert _ready_replicas() == 0, "lineage did not go down — the rest of this proves nothing"
        for row in range(1, WRITES_WHILE_DOWN + 1):
            wrote = requests.post(
                f"{CATALOG}/v1/table/{quoted}/insert",
                headers={**_auth(), "content-type": ARROW},
                data=_ipc([row]),
                timeout=120,
            )
            assert wrote.status_code == 200, wrote.text
    finally:
        _scale(1)

    # A refused connection is part of the recovery, not a failure of it: the Service endpoint
    # re-registers after `rollout status` already reports ready, and the claim under test is that the
    # events arrive EVENTUALLY.
    deadline = time.monotonic() + 300
    seen: set[str] = set()
    while time.monotonic() < deadline:
        try:
            seen = _runs_naming(table) - before
        except requests.RequestException:
            time.sleep(5)
            continue
        if len(seen) >= WRITES_WHILE_DOWN:
            break
        time.sleep(5)

    assert len(seen) >= WRITES_WHILE_DOWN, f"only {len(seen)} of {WRITES_WHILE_DOWN} writes replayed after lineage returned"
