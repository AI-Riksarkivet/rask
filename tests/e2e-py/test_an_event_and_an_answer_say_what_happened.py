"""An event names the tenant it is about at any depth, and a door that cannot carry a field refuses it ([[LH-573]],
audit C3, L1).

- C3: the create event of a table three segments deep (`<bronze>$<child>$t`) carries its project, as an insert's does:
  an event that names no tenant reaches no watcher (`docs/COMMIT-EVENTS.md` § Telling people). Read off lineage's feed,
  as any follower reads it.
- L1: a query naming `minimum_nprobes`, which the vendored spec declares and the installed request model drops, is
  refused 406 code 0 (Unsupported) rather than run without it (`lance_docs/ns_catalog/spec.yaml` QueryTableRequest).

The pre-fix answers were measured on helm revision 48 (#795): the create event carried no `project`, and the query
answered 200.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest
from byo_scenario import LINEAGE, SETTLE_SECONDS, lance_facet
from lance_namespace import ErrorCode
from refusals import assert_refused, described
from scratch_tenant import CATALOG, DEX, Tenant, ok, provisioned

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (CATALOG and LINEAGE and DEX), reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL and LANCE_E2E_DEX (a deployed estate)"),
]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("said", grants=())


def _create(tenant: Tenant, table: str, rows: pa.Table) -> None:
    ok(
        tenant.admin.http.post(
            f"{CATALOG}/v1/table/{table}/create", data=encode_arrow_stream(rows), headers={"content-type": ARROW_STREAM_MEDIA_TYPE}, timeout=60
        )
    )
    tenant.tables.append(table)


def _recorded_create(tenant: Tenant, table: str) -> dict[str, Any]:
    """The `rask_lance` facet of the create event lineage's feed holds for ``table``, once it is recorded."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        rows = ok(tenant.admin.http.get(f"{LINEAGE}/events", params={"table": table, "limit": 500}, timeout=30))["events"]
        created = [lance_facet(row) for row in rows if lance_facet(row).get("operation") == "create_table"]
        if created:
            return created[0]
        assert time.monotonic() < deadline, f"no create event of {table} was recorded within {SETTLE_SECONDS:.0f} s: {[lance_facet(r) for r in rows]}"
        time.sleep(2)


def test_a_tables_create_event_names_its_project_three_segments_deep(tenant: Tenant) -> None:
    child = f"{tenant.bronze}$child"
    ok(tenant.admin.http.post(f"{CATALOG}/v1/namespace/{child}/create", json={}, timeout=60))
    table = f"{child}$t"

    _create(tenant, table, pa.table({"id": pa.array([1, 2, 3], pa.int64())}))

    created = _recorded_create(tenant, table)
    assert created.get("project") == tenant.project, f"the create event of {table} named the project {created.get('project')!r}: {created}"


def test_a_query_naming_a_field_the_door_cannot_carry_is_refused(tenant: Tenant) -> None:
    table = f"{tenant.bronze}$vectors"
    vectors = pa.FixedSizeListArray.from_arrays(pa.array([0.0, 1.0, 1.0, 0.0], pa.float32()), 2)
    _create(tenant, table, pa.table({"id": pa.array([1, 2], pa.int64()), "vec": vectors}))
    query = {"k": 1, "vector": {"single_vector": [0.0, 1.0]}, "vector_column": "vec", "minimum_nprobes": 1}

    admin = tenant.admin

    assert_refused(
        lambda: admin.http.post(f"{CATALOG}/v1/table/{table}/query", json=query, timeout=60),
        status=406,
        code=ErrorCode.UNSUPPORTED,
        unchanged=lambda: described(CATALOG, table, dict(admin.http.headers)),
        foreign=(),
    )
