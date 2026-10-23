"""[[LH-528]] A table registered at a nested location is judged by its own storage on the estate.

The `dir` backend judges whether a table holds storage by probing ``base_path.join(location)``, which object_store
encodes as one segment, so a table registered over data at ``<namespace>-outside/held`` reads to it as only declared
(lance-format/lance#10281). The catalog judges each table's storage itself (``dataplane.holds_a_version``), so on the
deployed estate a stock client finds such a table among the tables that hold storage, and every door that asks keeps it:

- ``ListTables`` with ``include_declared=false`` lists it;
- ``DescribeTable`` with ``check_declared`` answers ``is_only_declared`` false;
- ``CreateTable`` with ``mode=exist_ok`` keeps it, its version and its rows;
- ``RenameTable`` moves the name and keeps the table at its location.

Driven as the estate's admin in a scratch tenant of its own, removed at the end with its bucket. The dataset is written
outside the catalog's doors with the store's own pair, as a pipeline writes one. Run with
`bash scripts/e2e_live.sh tests/e2e-py/test_a_registered_table_is_judged_by_its_own_storage.py`.
"""

from __future__ import annotations

from collections.abc import Iterator

import correctness
import lance
import lance_namespace as ln
import pyarrow as pa
import pytest
from scratch_tenant import CATALOG, DEX, Tenant, ok, provisioned

from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lancekit.arrow_ipc import encode_arrow_stream


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (CATALOG and DEX and correctness.STORE),
        reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX and LANCE_E2E_S3 (a deployed estate)",
    ),
]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("lh528", grants=())


def test_a_table_registered_at_a_nested_location_is_judged_by_its_own_storage(tenant: Tenant) -> None:
    admin, client = tenant.admin, tenant.admin.namespace()
    held, moved = [tenant.bronze, "held"], [tenant.bronze, "moved"]
    bucket = ok(admin.http.get(f"{CATALOG}/management/v1/warehouses/{tenant.project}"))["bucket"]
    location = f"{tenant.bronze}-outside/held"
    lance.write_dataset(
        pa.table({"id": pa.array([1, 2], pa.int64())}),
        f"s3://{bucket}/{location}",
        storage_options=correctness.store_options(),
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    client.register_table(ln.RegisterTableRequest(id=held, location=location))
    tenant.tables.append(CATALOG_DELIMITER.join(held))

    # It is among the tables that hold storage, and described as holding a version.
    assert client.list_tables(ln.ListTablesRequest(id=[tenant.bronze], include_declared=False)).tables == ["held"]
    described = client.describe_table(ln.DescribeTableRequest(id=held, check_declared=True))
    assert described.is_only_declared is False

    # A create that may find it existing keeps it.
    kept = client.create_table(ln.CreateTableRequest(id=held, mode="exist_ok"), encode_arrow_stream(pa.table({"id": pa.array([9], pa.int64())})))
    assert kept.version == 1

    # A rename moves its name and keeps it where it is, with its rows.
    client.rename_table(ln.RenameTableRequest(id=held, new_table_name=moved[-1]))
    tenant.tables[-1] = CATALOG_DELIMITER.join(moved)
    assert admin.http.post(f"{CATALOG}/v1/table/{CATALOG_DELIMITER.join(held)}/describe", json={}).status_code == 404
    assert client.describe_table(ln.DescribeTableRequest(id=moved)).location == described.location
    assert sorted(lance.dataset(namespace_client=client, table_id=moved).to_table(columns=["id"]).column("id").to_pylist()) == [1, 2]
