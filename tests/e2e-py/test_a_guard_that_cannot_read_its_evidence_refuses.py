"""A guard that cannot read its evidence refuses, never answers as if there were none ([[LH-572]], audit C1, C2).

Each leg plants the unreadable evidence from inside the catalog pod, under the catalog's own store session, on an object
of this module's own scratch tenant (`plant`), asks the door while it stands, and puts it back. A store that answers and
holds evidence that cannot be read as itself is 500 code 18 (`lance_docs/ns_catalog/namespace/operations/errors.md`):
only writing it again clears it. The refusal changes nothing it guards (`refusals.assert_refused`).

- C1: a classified table whose latest manifest does not decode is vended to no one. The labels are read off the open,
  so an open that fails says nothing about them.
- C2: a project delete is refused while one warehouse record cannot be read. The project still holds its warehouse, so
  a guard reading the list short would answer 409 and delete nothing: the leg never risks the tenant.

During C2's request, the junk record stands in the estate's registry, and any warehouse listing the estate makes in that
window (another project's delete, a policy set) is refused the same way; the window is that one request.

The pre-fix answers are read off the code at 31f9c383f^, since #793's read-back ran on the fixed image: the vend turned
any `ValueError` or `OSError` from the open into empty facts (`vending.dataset_facts`, `except (ValueError, OSError):
return VendFacts()`) and answered 200, and the project delete read the warehouse list with the unreadable record skipped
and answered 409 naming the warehouse it still held. Both are the answers these legs give with no fault planted.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pyarrow as pa
import pytest
from lance_namespace import ErrorCode
from plant import planted
from refusals import assert_refused
from scratch_tenant import CATALOG, DEX, Tenant, ok, provisioned, tuples_on

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: Lance's manifest naming for a table written with V2 manifest paths: version ``v`` is ``_versions/{U64_MAX - v}.manifest``
#: (`lance_docs/lance/format/table/layout.md`), so the latest version sorts first.
U64_MAX = 2**64 - 1

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (CATALOG and DEX), reason="set LANCE_E2E_CATALOG_URL and LANCE_E2E_DEX (a deployed estate)"),
]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("guard", grants=())


@pytest.fixture(scope="module")
def label(tenant: Tenant) -> Iterator[str]:
    """A classification label of this run's own, its curator the tenant's admin, named through the estate admin's tuple
    editor (the one door that writes `curator`); the tuple is deleted at the end, so no label outlives the run."""
    name = f"guard-{uuid.uuid4().hex[:8]}"
    curator = {"user": tenant.admin.principal, "relation": "curator", "object": f"classification:{name}"}
    ok(tenant.admin.http.post(f"{CATALOG}/management/v1/access/tuples", json=curator, timeout=60))
    try:
        yield name
    finally:
        answer = tenant.admin.http.delete(f"{CATALOG}/management/v1/access/tuples", json=curator, timeout=60)
        assert answer.status_code == 200, f"the run's label {name} kept its curator: {answer.status_code} {answer.text[:300]}"


def _classified_table(tenant: Tenant, label: str) -> tuple[str, str, str]:
    """A table of the tenant's whose ``id`` column carries ``label``: its id, its uuid, and its latest manifest's key."""
    table = f"{tenant.bronze}$labelled"
    admin = tenant.admin.http
    rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())}))
    ok(admin.post(f"{CATALOG}/v1/table/{table}/create", data=rows, headers={"content-type": ARROW_STREAM_MEDIA_TYPE}, timeout=60))
    tenant.tables.append(table)
    labelled = {"updates": [{"path": "id", "metadata": {"rask.classification": label}}]}
    deadline = time.monotonic() + 30
    while (answer := admin.post(f"{CATALOG}/v1/table/{table}/update_field_metadata", json=labelled, timeout=60)).status_code == 403:
        assert time.monotonic() < deadline, f"the curator of {label} was never seen: {answer.text[:300]}"
        time.sleep(1)
    ok(answer)
    described = ok(admin.post(f"{CATALOG}/v1/table/{table}/describe", json={"load_detailed_metadata": True}, timeout=60))
    manifest = f"{str(described['location']).rstrip('/')}/_versions/{U64_MAX - int(described['version'])}.manifest"
    return table, str(described["properties"]["rask.uuid"]), manifest


def test_a_classified_table_whose_open_fails_is_vended_to_no_one(tenant: Tenant, label: str) -> None:
    table, table_uuid, manifest = _classified_table(tenant, label)
    root, _, key = manifest.removeprefix("s3://").partition("/")
    admin = tenant.admin

    with planted(f"s3://{root}", key, replacing=True):
        assert_refused(
            lambda: admin.http.post(f"{CATALOG}/management/v1/table/{table}/credentials", params={"tier": "read"}, timeout=60),
            status=500,
            code=ErrorCode.INTERNAL,
            unchanged=lambda: tuples_on(admin, f"table:{table_uuid}"),
            foreign=(),
        )


def test_a_project_delete_is_refused_while_a_warehouse_record_cannot_be_read(tenant: Tenant) -> None:
    admin = tenant.admin
    unreadable = f"{tenant.project}-unreadable"

    with planted("control", f"_warehouses/{unreadable}.json", replacing=False):
        assert_refused(
            lambda: admin.http.delete(f"{CATALOG}/management/v1/projects/{tenant.project}", timeout=60),
            status=500,
            code=ErrorCode.INTERNAL,
            unchanged=lambda: (
                tuples_on(admin, f"project:{tenant.project}"),
                ok(admin.http.get(f"{CATALOG}/v1/namespace/{tenant.bronze}/table/list", timeout=60)),
            ),
            foreign=(unreadable,),
        )
