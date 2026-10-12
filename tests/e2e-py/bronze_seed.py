"""Seed a project's bronze through the catalog's namespace doors: the one way data enters the lakehouse (ADR 0154).

The cascade head is the medallion producer's ``/bronze-arrival``, and it acts on the catalog's own announcement of a
committed write: a ``COMPLETE`` lineage event whose output is ``<project>-bronze$events`` in namespace
``<project>-bronze`` (``medallion.services.ingest_trigger._bronze_write``). So a suite that needs a cascade writes rows
into that table through ``POST /v1/table/{id}/insert`` (spec ``InsertIntoTable``, Arrow IPC body) and nothing else.

TWO CALLS, AND THE SPLIT IS LOAD-BEARING. The table is created EMPTY first (``CreateTable`` with ``mode=exist_ok``): the
catalog announces a create as a DDL change, a ``DatasetEvent`` with no run (``catalog.core.lineage_emit._is_ddl``), and
the head ignores it, so rows landed by the create would sit in bronze without starting a cascade. The ``insert`` is a
data write, announced as a ``COMPLETE`` run, and that is the event the head fires on.

THE CASCADE TOKEN IS THE INSERT'S RUN ID. The catalog stamps no ``lance.token`` on its events, so the head threads the
cascade on the event's ``run.runId`` (``ingest_trigger._cascade_token``), and every stage's run id is derived from it.
The insert's response names only the version it committed, so :func:`cascade_token` reads the run id back off the
lineage graph by that version.

Kept a plain module (not ``test_*.py``) so every suite and the integration test share one request shape.
"""

from __future__ import annotations

import io
import time
from collections.abc import Mapping

import pyarrow as pa
import requests


#: The tier and the table the head is configured for (`MEDALLION_BRONZE_NAMESPACE`, `MEDALLION_BRONZE_DATASET`).
BRONZE_TIER = "bronze"
BRONZE_TABLE = "events"

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}

#: The governed tier row the stage runners read (`medallion/schemas/tier.py`): `id` is the row key the stages merge on,
#: `payload` the opaque content, and `stage` the tier stamp every tier carries.
BRONZE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("payload", pa.string()),
        pa.field("stage", pa.string()),
    ]
)


def bronze_ids(project: str, delimiter: str = "$") -> tuple[str, str]:
    """``(namespace, table_id)`` of a project's bronze events table: ``acme-bronze`` and ``acme-bronze$events``.

    An empty project is the single-tenant estate, whose pair is ``bronze`` / ``bronze$events``.
    """
    namespace = f"{project}-{BRONZE_TIER}" if project else BRONZE_TIER
    return namespace, f"{namespace}{delimiter}{BRONZE_TABLE}"


def bronze_rows(rows: int, *, first_id: int | None = None) -> pa.Table:
    """``rows`` bronze rows with ids no earlier seed used.

    Ids are offset by the wall clock in microseconds, because an insert APPENDS and the stages merge on ``id``: a second
    seed reusing ids would land rows the cascade reads as updates of the first seed's.
    """
    start = time.time_ns() // 1_000 if first_id is None else first_id
    return pa.table(
        {
            "id": pa.array(range(start, start + rows), pa.int64()),
            "payload": pa.array([f"event-{start + i}" for i in range(rows)], pa.string()),
            "stage": pa.array([BRONZE_TIER] * rows, pa.string()),
        },
        schema=BRONZE_SCHEMA,
    )


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _check(response: requests.Response, what: str) -> None:
    if response.status_code >= 400:
        raise AssertionError(f"{what}: HTTP {response.status_code} {response.text[:400]}")


def seed_bronze_events(
    catalog_url: str,
    *,
    project: str,
    headers: Mapping[str, str] | None = None,
    rows: int = 8,
    delimiter: str = "$",
    timeout: float = 60.0,
) -> int:
    """Insert ``rows`` rows into the project's bronze events table through the catalog and return the version committed.

    The namespace is created only when it is absent and the estate is single-tenant: a project's tier namespaces are
    its warehouse's to create, and the catalog refuses a top-level create once warehouses are on.

    ``headers`` carries the caller's credential (a project admin's bearer on an authenticated estate).

    Raises:
        AssertionError: A door refused, naming the door and the catalog's answer.
    """
    base = catalog_url.rstrip("/")
    auth = dict(headers or {})
    namespace, table_id = bronze_ids(project, delimiter)
    if not project:
        exists = requests.post(f"{base}/v1/namespace/{namespace}/exists", headers=auth, json={}, timeout=timeout)
        if exists.status_code == 404:
            _check(
                requests.post(f"{base}/v1/namespace/{namespace}/create", headers=auth, json={"id": [namespace], "mode": "exist_ok"}, timeout=timeout),
                f"create namespace {namespace}",
            )
    _check(
        requests.post(
            f"{base}/v1/table/{table_id}/create",
            params={"mode": "exist_ok"},
            headers={**auth, **ARROW_STREAM},
            data=_ipc(BRONZE_SCHEMA.empty_table()),
            timeout=timeout,
        ),
        f"create {table_id}",
    )
    inserted = requests.post(
        f"{base}/v1/table/{table_id}/insert",
        headers={**auth, **ARROW_STREAM},
        data=_ipc(bronze_rows(rows)),
        timeout=timeout,
    )
    _check(inserted, f"insert into {table_id}")
    return int(inserted.json()["version"])


def cascade_token(
    lineage_url: str,
    *,
    project: str,
    version: int,
    headers: Mapping[str, str] | None = None,
    delimiter: str = "$",
    timeout: float = 120.0,
) -> str:
    """The cascade token of the seed that committed ``version``: the run id the graph recorded for that insert.

    Polls ``GET /datasets/{table}/producers`` until the run that wrote ``version`` appears, because the catalog's
    announcement reaches the graph asynchronously.

    Raises:
        AssertionError: No run naming that version appeared within ``timeout`` seconds.
    """
    _, table_id = bronze_ids(project, delimiter)
    base = lineage_url.rstrip("/")
    deadline = time.monotonic() + timeout
    seen: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{base}/datasets/{table_id}/producers", headers=dict(headers or {}), timeout=10)
        except (requests.ConnectionError, requests.Timeout):
            resp = None
        if resp is not None and resp.status_code == 200:
            seen = resp.json().get("producers", [])
            for producer in seen:
                if producer.get("dataset_version") == str(version) and producer.get("event_type") == "COMPLETE":
                    return str(producer["run_id"])
        time.sleep(3)
    raise AssertionError(f"no COMPLETE run wrote {table_id} at version {version} within {timeout:.0f}s; producers seen: {seen[-5:]}")
