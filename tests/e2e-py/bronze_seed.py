"""Seed a project's bronze through the catalog's namespace doors: the one way data enters the lakehouse (ADR 0154).

A client writes rows into ``<project>-bronze$events`` through ``POST /v1/table/{id}/insert`` (spec ``InsertIntoTable``,
Arrow IPC body), and the catalog announces the committed write on the lineage feed. Moving those rows on to silver and
gold is the user's own pipeline (ADR 0155); this module only lands bronze.

TWO CALLS, AND THE SPLIT IS LOAD-BEARING. The table is created EMPTY first (``CreateTable`` with ``mode=exist_ok``): the
catalog announces a create as a DDL change, a ``DatasetEvent`` with no run (``catalog.core.lineage_emit._is_ddl``), while
the ``insert`` is a data write announced as a ``COMPLETE`` run, the event a pipeline polling the feed acts on.

Kept a plain module (not ``test_*.py``) so every suite that seeds bronze shares one request shape.
"""

from __future__ import annotations

import io
import time
from collections.abc import Mapping

import pyarrow as pa
import requests


#: The tier and the table a project's bronze events land in.
BRONZE_TIER = "bronze"
BRONZE_TABLE = "events"

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}

#: The governed tier row: `id` is the row key a downstream merge keys on, `payload` the opaque content, and `stage` the
#: tier stamp every tier carries.
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

    Ids are offset by the wall clock in microseconds, because an insert APPENDS and a downstream merge keys on ``id``: a
    second seed reusing ids would land rows a merge reads as updates of the first seed's.
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
